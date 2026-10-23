"""Read-only JSON API for the HippoMind Atlas web console.

The daemon already speaks a small, stable HTTP surface for agents
(/recall, /context-pack, /ingest, ...).  A console needs a different shape: one
first paint instead of six round-trips, bounded pagination over the projection
table, provenance for a single memory, and the maintenance views an operator
actually reads.  This module adds that read-only surface under /api/v1 without
touching the agent-facing contracts.

Security model (unchanged, but stated here because the surface is larger):

* every endpoint requires the daemon's local admin token; a scoped agent
  credential is refused with 403.  The console is an operator instrument, not
  an agent API;
* GET endpoints are read-only.  The only writes are pin/unpin and feedback,
  which already have audited ledger operations;
* every list is bounded, every content field is truncated, and query
  parameters are clamped; responses stay under the daemon's response budget;
* SQL is parameterised.  The few custom queries below read the same tables the
  ledger mixins own, via the ledger's read connection, and never write.

Wire format is documented in docs/GUI_ATLAS.md section 4.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import secrets
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import unquote

from . import __version__
from .access import AccessPolicy
from .ledger import LedgerError, SCHEMA_VERSION
from .ledger._helpers import (
    DEFAULT_RAW_CAPACITY,
    DEFAULT_RAW_RETENTION_DAYS,
    RAW_ARCHIVE_DIR_NAME,
)

ATLAS_VERSION = "1.0"

#: A list row stays scannable; the full text lives in the detail endpoint.
MAX_CONTENT_PREVIEW = 600
#: Detail endpoint ceiling: a memory can be 2 MB, a browser drawer cannot.
MAX_DETAIL_CONTENT = 12_000
MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 50
MAX_QUERY_CHARS = 200
UI_NONCE_TTL_SECONDS = 120
MAX_PENDING_UI_NONCES = 8
WAL_WARN_BYTES = 64 * 1024 * 1024

#: Endpoint catalogue served by /api/v1/meta and rendered by the reference view.
ENDPOINTS: Tuple[Dict[str, Any], ...] = (
    {"method": "GET", "path": "/health", "summary": "存活与账本指纹（无需令牌）", "auth": "none"},
    {"method": "GET", "path": "/stats", "summary": "账本/归档/来源/缓存统计", "auth": "token"},
    {"method": "POST", "path": "/recall", "summary": "关键词召回（FTS + LIKE 双通道）", "auth": "token"},
    {"method": "POST", "path": "/context-pack", "summary": "组装最小、带证据的上下文包", "auth": "token"},
    {"method": "POST", "path": "/feedback", "summary": "以外部反馈调整工作记忆权重", "auth": "token"},
    {"method": "GET", "path": "/graph", "summary": "实体图谱邻域", "auth": "token"},
    {"method": "GET", "path": "/conflicts", "summary": "冲突/被取代 claim 谱系", "auth": "token"},
    {"method": "GET", "path": "/metrics", "summary": "Prometheus 文本指标", "auth": "none"},
    {"method": "GET", "path": "/api/v1/meta", "summary": "版本、上限与接口目录", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/overview", "summary": "首屏体征：统计+增长+最近入档", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/memories", "summary": "记忆库分页浏览与筛选", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/memories/{id}", "summary": "单条记忆的完整来源与谱系", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/timeline", "summary": "记忆随时间的生长序列", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/claims", "summary": "claims 列表与状态计数", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/conflicts", "summary": "冲突/取代谱系", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/facts", "summary": "事实投影（活跃 claim）", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/skills", "summary": "程序性技能投影", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/subjects", "summary": "最近活跃的 claim 主语", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/graph", "summary": "实体图谱邻域", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/agents", "summary": "来源 Agent、游标与接入状态", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/health", "summary": "完整性/索引/容量体检", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/environment", "summary": "运行环境、磁盘、备份与登录项", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/events", "summary": "结构化事件流（排除 raw/tool/system）", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/pins", "summary": "置顶记忆列表", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/duplicates", "summary": "近似重复候选（只读）", "auth": "admin"},
    {"method": "POST", "path": "/api/v1/pin", "summary": "置顶/取消置顶一条记忆", "auth": "admin"},
    {"method": "POST", "path": "/api/v1/feedback", "summary": "对一条记忆给出正负反馈", "auth": "admin"},
    {"method": "POST", "path": "/api/v1/ui-token", "summary": "签发一次性 UI 引导令牌", "auth": "admin"},
    {"method": "POST", "path": "/api/v1/ui-token/exchange", "summary": "用引导令牌换取本机令牌", "auth": "nonce"},
)

_MEMORY_COLUMNS = (
    "memory_id, kind, content, source_agent, session_id, task_id, project, role, "
    "scope, created_at, updated_at, lifecycle, quality_state, raw_chat, weight, "
    "importance, confidence, utility, touch_count, pinned"
)


class GuiApiError(Exception):
    """An API-level failure the HTTP layer can render as JSON + status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ledger_identity(path: os.PathLike | str) -> str:
    """Same non-reversible id the daemon exposes in /health."""
    try:
        value = str(Path(path).expanduser().resolve())
    except OSError:
        value = str(Path(path).expanduser())
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _clamp_int(params: Mapping[str, List[str]], name: str, default: int,
               low: int, high: int) -> int:
    raw = (params.get(name) or [""])[0]
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _text_param(params: Mapping[str, List[str]], name: str, default: str = "",
                limit: int = 256) -> str:
    raw = (params.get(name) or [default])[0]
    return str(raw or "").strip()[:limit]


def _truthy_param(params: Mapping[str, List[str]], name: str) -> bool:
    raw = (_text_param(params, name) or "").lower()
    return raw in {"1", "true", "yes", "on"}


def _preview(content: Any, limit: int = MAX_CONTENT_PREVIEW) -> str:
    text = str(content or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _json_value(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return fallback
    return value


def _iso_since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0, days - 1))).strftime("%Y-%m-%d")


def _date_range(days: int) -> List[str]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=max(0, days - 1))
    output: List[str] = []
    cursor = start
    while cursor <= today:
        output.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return output


def _like_term(term: str) -> str:
    """Escape LIKE wildcards with an explicit '!' escape character.

    The escape character is chosen to avoid backslash literals entirely: this
    file must stay readable on the exFAT workspace and SQLite's ESCAPE clause
    is happy with any single character.
    """
    escaped = term.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return "%" + escaped + "%"


def _public_memory(row: Mapping[str, Any], *, has_claims: bool = False) -> Dict[str, Any]:
    row = dict(row)
    return {
        "memory_id": str(row.get("memory_id", "")),
        "kind": str(row.get("kind", "")),
        "content": _preview(row.get("content", "")),
        "source_agent": str(row.get("source_agent", "")),
        "session_id": str(row.get("session_id", "")),
        "task_id": str(row.get("task_id", "")),
        "project": str(row.get("project", "")),
        "scope": str(row.get("scope", "")),
        "role": str(row.get("role", "")),
        "created_at": str(row.get("created_at", "")),
        "updated_at": str(row.get("updated_at", "")),
        "lifecycle": str(row.get("lifecycle", "")),
        "quality_state": str(row.get("quality_state", "")),
        "raw_chat": bool(row.get("raw_chat", 0)),
        "weight": round(float(row.get("weight", 0.0) or 0.0), 4),
        "importance": int(row.get("importance", 0) or 0),
        "confidence": round(float(row.get("confidence", 0.0) or 0.0), 4),
        "utility": round(float(row.get("utility", 0.0) or 0.0), 4),
        "touch_count": int(row.get("touch_count", 0) or 0),
        "pinned": bool(row.get("pinned", 0)),
        "has_claims": bool(has_claims),
    }


def _relation_memory(row: Mapping[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    return {
        "memory_id": str(row.get("memory_id", "")),
        "kind": str(row.get("kind", "")),
        "content": _preview(row.get("content", ""), 240),
        "source_agent": str(row.get("source_agent", "")),
        "created_at": str(row.get("created_at", "")),
        "lifecycle": str(row.get("lifecycle", "")),
    }


class GuiApi:
    """Admin-scoped read model for the console; mounted by the daemon handler."""

    def __init__(self, hub: Any):
        self.hub = hub
        self.ledger = hub.ledger
        self._ui_nonces: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    def get(self, path: str, params: Mapping[str, List[str]],
            policy: AccessPolicy) -> Dict[str, Any]:
        self._require_admin(policy)
        route = path[len("/api/v1"):].strip("/")
        if route == "meta":
            return self.meta()
        if route == "overview":
            return self.overview(params)
        if route == "memories":
            return self.memories(params)
        if route.startswith("memories/"):
            return self.memory_detail(unquote(route[len("memories/"):]))
        if route == "timeline":
            return self.timeline(params)
        if route == "claims":
            return self.claims(params)
        if route == "conflicts":
            return self.conflicts(params)
        if route == "facts":
            return self.facts(params)
        if route == "skills":
            return self.skills(params)
        if route == "subjects":
            return self.subjects(params)
        if route == "graph":
            return self.graph(params)
        if route == "agents":
            return self.agents(params)
        if route == "health":
            return self.health(params)
        if route == "environment":
            return self.environment(params)
        if route == "events":
            return self.events(params)
        if route == "pins":
            return self.pins(params)
        if route == "duplicates":
            return self.duplicates(params)
        raise GuiApiError(404, "unknown endpoint")

    def post(self, path: str, body: Mapping[str, Any],
             policy: AccessPolicy) -> Dict[str, Any]:
        route = path[len("/api/v1"):].strip("/")
        if route == "ui-token":
            self._require_admin(policy)
            return self.mint_ui_token()
        if route == "pin":
            self._require_admin(policy)
            return self.pin(body)
        if route == "feedback":
            self._require_admin(policy)
            return self.feedback(body)
        raise GuiApiError(404, "unknown endpoint")

    def exchange_ui_token(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        """Unauthenticated by design, but gated by a single-use random nonce."""
        self._prune_ui_nonces()
        nonce = str(body.get("nonce", ""))[:256]
        expiry = self._ui_nonces.pop(nonce, None) if nonce else None
        if expiry is None or expiry < time.time():
            raise GuiApiError(401, "引导令牌无效或已过期")
        return {"token": str(getattr(self.hub, "token", "") or "")}

    @staticmethod
    def _require_admin(policy: AccessPolicy) -> None:
        if not policy.admin:
            raise GuiApiError(403, "GUI API 需要本机管理员令牌")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _previews_for(self, memory_ids: Iterable[str]) -> Dict[str, str]:
        ids = sorted({str(mid) for mid in memory_ids if mid})[:200]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT memory_id, content FROM memories WHERE memory_id IN (" +
                placeholders + ")", tuple(ids)).fetchall()
        return {str(row["memory_id"]): str(row["content"] or "") for row in rows}

    def _has_claims_for(self, memory_ids: Iterable[str]) -> set:
        ids = sorted({str(mid) for mid in memory_ids if mid})[:200]
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT DISTINCT memory_id FROM memory_claims WHERE memory_id IN (" +
                placeholders + ")", tuple(ids)).fetchall()
        return {str(row["memory_id"]) for row in rows}

    def _archive_stats(self) -> Dict[str, int]:
        path = getattr(self.ledger, "path", None)
        if path is None:
            return {"archive_files": 0, "archive_bytes": 0}
        root = Path(path).parent / (Path(path).stem + "." + RAW_ARCHIVE_DIR_NAME)
        files = 0
        total = 0
        if root.is_dir() and not root.is_symlink():
            for archive in root.glob("raw-*.jsonl.gz"):
                try:
                    files += 1
                    total += archive.stat().st_size
                except OSError:
                    continue
        return {"archive_files": files, "archive_bytes": total}

    def _storage(self) -> Dict[str, int]:
        report: Dict[str, int] = {}
        try:
            with self.ledger._reading() as conn:
                page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
                page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
                freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            report = {"page_size": page_size, "page_count": page_count,
                      "db_bytes": page_size * page_count, "freelist": freelist,
                      "reclaimable_bytes": page_size * freelist}
        except (sqlite3.Error, TypeError, ValueError):
            report = {}
        wal_path = Path(str(self.ledger.path) + "-wal")
        wal_bytes = 0
        try:
            wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        except OSError:
            wal_bytes = 0
        report["wal_bytes"] = wal_bytes
        report.update(self._archive_stats())
        report["retention_days"] = DEFAULT_RAW_RETENTION_DAYS
        report["capacity"] = DEFAULT_RAW_CAPACITY
        return report

    # ------------------------------------------------------------------
    # meta + overview
    # ------------------------------------------------------------------
    def meta(self) -> Dict[str, Any]:
        hub = self.hub
        started = float(getattr(hub, "_started_at", time.time()))
        return {
            "server_version": __version__,
            "api_version": "v1",
            "schema_version": SCHEMA_VERSION,
            "ledger_id": _ledger_identity(self.ledger.path),
            "started_at": getattr(hub, "started_at", ""),
            "uptime_s": round(max(0.0, time.time() - started), 1),
            "pid": os.getpid(),
            "limits": {"memories_page": [1, MAX_LIST_LIMIT],
                       "content_preview": MAX_CONTENT_PREVIEW,
                       "detail_content": MAX_DETAIL_CONTENT,
                       "graph_nodes": 200},
            "endpoints": [dict(item) for item in ENDPOINTS],
            "ui": {"name": "HippoMind Atlas", "version": ATLAS_VERSION},
        }

    def overview(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        days = _clamp_int(params, "days", 30, 1, 365)
        stats = self.hub.stats(AccessPolicy.administrator())
        ledger_stats = stats.get("ledger", {}) if isinstance(stats, Mapping) else {}
        started = float(getattr(self.hub, "_started_at", time.time()))
        growth = self._growth(days)
        recent = self._recent_memories(6)
        return {
            "server": {
                "status": "ok",
                "server_version": __version__,
                "watch_enabled": bool(getattr(self.hub, "watch_enabled", False)),
                "ledger_id": _ledger_identity(self.ledger.path),
                "pid": os.getpid(),
                "started_at": getattr(self.hub, "started_at", ""),
                "uptime_s": round(max(0.0, time.time() - started), 1),
            },
            "ledger": ledger_stats,
            "watcher": stats.get("watcher", {}),
            "agent_discovery": stats.get("agent_discovery", {}),
            "auto_install": stats.get("auto_install", {}),
            "context_cache": stats.get("context_cache", {}),
            "daemon_health": stats.get("health", {}),
            "wal_bytes": int(stats.get("wal_bytes", 0) or 0),
            "growth": growth,
            "recent": recent,
            "health_flags": self._health_flags(stats, ledger_stats),
        }

    def _growth(self, days: int) -> Dict[str, Any]:
        since = _iso_since(days)
        with self.ledger._reading() as conn:
            memory_rows = conn.execute(
                "SELECT substr(created_at,1,10) AS day, COUNT(*) AS count FROM memories "
                "WHERE raw_chat=0 AND created_at>=? GROUP BY day", (since,)).fetchall()
            event_rows = conn.execute(
                "SELECT substr(occurred_at,1,10) AS day, COUNT(*) AS count FROM ledger_events "
                "WHERE occurred_at>=? GROUP BY day", (since,)).fetchall()
        memories = {str(row["day"]): int(row["count"]) for row in memory_rows}
        events = {str(row["day"]): int(row["count"]) for row in event_rows}
        buckets = [{"date": day, "memories": memories.get(day, 0),
                    "events": events.get(day, 0)} for day in _date_range(days)]
        return {"days": days, "buckets": buckets,
                "total_memories": sum(item["memories"] for item in buckets),
                "total_events": sum(item["events"] for item in buckets)}

    def _recent_memories(self, limit: int) -> List[Dict[str, Any]]:
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT " + _MEMORY_COLUMNS + " FROM memories "
                "WHERE raw_chat=0 AND scope='shared' ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(20, limit)),)).fetchall()
        claims = self._has_claims_for([row["memory_id"] for row in rows])
        return [_public_memory(row, has_claims=str(row["memory_id"]) in claims) for row in rows]

    def _health_flags(self, stats: Mapping[str, Any],
                      ledger_stats: Mapping[str, Any]) -> List[Dict[str, Any]]:
        flags: List[Dict[str, Any]] = []
        try:
            overview = self.ledger.health_overview()
        except (LedgerError, sqlite3.Error):
            overview = {}
        fts = overview.get("fts", {}) if isinstance(overview, Mapping) else {}
        if fts:
            drift = bool(fts.get("drift"))
            flags.append({"id": "fts", "label": "FTS 同步",
                          "severity": "warn" if drift else "ok",
                          "detail": ("索引漂移：%s；%s" % (fts.get("rows"), fts.get("note", "")))
                          if drift else "索引与应索引集合一致（%s 行）" % fts.get("rows")})
        try:
            claims = self.ledger.claims_consistency_report()
        except (LedgerError, sqlite3.Error):
            claims = {}
        if claims:
            flags.append({"id": "claims", "label": "Claims 一致性",
                          "severity": "warn" if claims.get("status") != "ok" else "ok",
                          "detail": ("活跃 claim %s 条，悬空 %s 条" %
                                     (claims.get("active_claims"), claims.get("dangling")))})
        wal = int(stats.get("wal_bytes", 0) or 0)
        flags.append({"id": "wal", "label": "WAL 侧车",
                      "severity": "warn" if wal > WAL_WARN_BYTES else "ok",
                      "detail": "WAL %.1f MiB" % (wal / (1024 * 1024))})
        quarantine = int(overview.get("quarantined", 0) or 0) if isinstance(overview, Mapping) else 0
        flags.append({"id": "quarantine", "label": "隔离行",
                      "severity": "warn" if quarantine else "ok",
                      "detail": "%s 条 quarantine 投影不参与召回" % quarantine})
        return flags

    # ------------------------------------------------------------------
    # memory archive
    # ------------------------------------------------------------------
    def memories(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        limit = _clamp_int(params, "limit", DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT)
        offset = _clamp_int(params, "offset", 0, 0, 100_000)
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        raw = _text_param(params, "raw", "exclude", 16) or "exclude"
        query = _text_param(params, "q", "", MAX_QUERY_CHARS)
        base_where: List[str] = ["scope=?"]
        base_args: List[Any] = [scope]
        if raw == "exclude":
            base_where.append("raw_chat=0")
        elif raw == "only":
            base_where.append("raw_chat=1")
        if query:
            base_where.append("(content LIKE ? ESCAPE '!' OR metadata_json LIKE ? ESCAPE '!')")
            base_args.extend([_like_term(query), _like_term(query)])
        if _truthy_param(params, "pinned"):
            base_where.append("pinned=1")
        self._append_time_bounds(params, base_where, base_args)

        where = list(base_where)
        args = list(base_args)
        for column, name in (("kind", "kind"), ("source_agent", "agent"),
                             ("lifecycle", "lifecycle"), ("project", "project")):
            value = _text_param(params, name, "", 128)
            if value:
                where.append(column + "=?")
                args.append(value)

        sort_key = _text_param(params, "sort", "updated", 16)
        sort_column = {"updated": "updated_at", "created": "created_at",
                       "weight": "weight"}.get(sort_key, "updated_at")
        order = "ASC" if _text_param(params, "order", "desc", 8).lower() == "asc" else "DESC"
        clause = " AND ".join(where)
        with self.ledger._reading() as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE " + clause, tuple(args)).fetchone()[0])
            rows = conn.execute(
                "SELECT " + _MEMORY_COLUMNS + " FROM memories WHERE " + clause +
                " ORDER BY " + sort_column + " " + order + ", memory_id LIMIT ? OFFSET ?",
                tuple(args) + (limit, offset)).fetchall()
            has_claims = self._has_claims_for([row["memory_id"] for row in rows])
            facets: Dict[str, Any] = {}
            if _truthy_param(params, "facets"):
                facets = self._facets(conn, base_where, base_args)
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [_public_memory(row, has_claims=str(row["memory_id"]) in has_claims)
                      for row in rows],
            "facets": facets,
        }

    @staticmethod
    def _append_time_bounds(params: Mapping[str, List[str]],
                            where: List[str], args: List[Any]) -> None:
        from_raw = _text_param(params, "from", "", 32)
        if from_raw:
            where.append("created_at >= ?")
            args.append(from_raw)
        to_raw = _text_param(params, "to", "", 32)
        if not to_raw:
            return
        if len(to_raw) == 10:
            try:
                day = datetime.strptime(to_raw, "%Y-%m-%d").date()
                where.append("created_at < ?")
                args.append((day + timedelta(days=1)).isoformat())
                return
            except ValueError:
                pass
        where.append("created_at <= ?")
        args.append(to_raw)

    @staticmethod
    def _facets(conn: sqlite3.Connection, where: List[str],
                args: List[Any]) -> Dict[str, Any]:
        def collect(column: str, limit: int) -> List[Dict[str, Any]]:
            sql = ("SELECT " + column + " AS value, COUNT(*) AS count FROM memories WHERE " +
                   " AND ".join(where + [column + " != ''"]) +
                   " GROUP BY " + column + " ORDER BY count DESC LIMIT ?")
            return [{"value": str(row["value"]), "count": int(row["count"])}
                    for row in conn.execute(sql, tuple(args) + (limit,)).fetchall()]

        return {"kinds": collect("kind", 24), "agents": collect("source_agent", 30),
                "lifecycles": collect("lifecycle", 8), "projects": collect("project", 24)}

    def memory_detail(self, memory_id: str) -> Dict[str, Any]:
        memory_id = str(memory_id or "").strip()[:80]
        if not memory_id:
            raise GuiApiError(400, "memory_id 不能为空")
        row = self.ledger.get_memory(memory_id)
        if row is None:
            raise GuiApiError(404, "未找到该记忆")
        memory = dict(row)
        memory["content"] = _preview(memory.get("content", ""), MAX_DETAIL_CONTENT)
        origin_id = str(row.get("origin_event_id") or "")
        with self.ledger._reading() as conn:
            event = None
            if origin_id:
                event = conn.execute(
                    "SELECT event_id,event_type,occurred_at,ingested_at,source_agent,"
                    "session_id,task_id,project,role,scope,content,metadata_json "
                    "FROM ledger_events WHERE event_id=?", (origin_id,)).fetchone()
            parents = conn.execute(
                "SELECT r.relation_type,r.confidence,m.memory_id,m.kind,m.content,"
                "m.source_agent,m.created_at,m.lifecycle FROM memory_relations r "
                "JOIN memories m ON m.memory_id=r.parent_memory_id "
                "WHERE r.child_memory_id=? ORDER BY r.confidence DESC LIMIT 50",
                (memory_id,)).fetchall()
            children = conn.execute(
                "SELECT r.relation_type,r.confidence,m.memory_id,m.kind,m.content,"
                "m.source_agent,m.created_at,m.lifecycle FROM memory_relations r "
                "JOIN memories m ON m.memory_id=r.child_memory_id "
                "WHERE r.parent_memory_id=? ORDER BY r.confidence DESC LIMIT 50",
                (memory_id,)).fetchall()
            feedback = conn.execute(
                "SELECT feedback_id,signal,note,created_at,source_agent "
                "FROM memory_feedback WHERE memory_id=? ORDER BY created_at DESC LIMIT 100",
                (memory_id,)).fetchall()
        origin: Dict[str, Any] = {}
        if event is not None:
            origin = {"event_id": str(event["event_id"]),
                      "event_type": str(event["event_type"]),
                      "occurred_at": str(event["occurred_at"]),
                      "ingested_at": str(event["ingested_at"]),
                      "source_agent": str(event["source_agent"]),
                      "session_id": str(event["session_id"]),
                      "task_id": str(event["task_id"]),
                      "project": str(event["project"]),
                      "role": str(event["role"]),
                      "scope": str(event["scope"]),
                      "content_preview": _preview(event["content"], 400),
                      "metadata": _json_value(event["metadata_json"], {})}
        claims = self.ledger.list_claims(
            memory_id=memory_id, scope=str(row.get("scope", "shared") or "shared"), limit=200)
        return {
            "memory": memory,
            "origin_event": origin,
            "claims": claims,
            "relations": {
                "parents": [{"relation_type": str(item["relation_type"]),
                             "confidence": round(float(item["confidence"]), 3),
                             "memory": _relation_memory(item)} for item in parents],
                "children": [{"relation_type": str(item["relation_type"]),
                              "confidence": round(float(item["confidence"]), 3),
                              "memory": _relation_memory(item)} for item in children],
            },
            "feedback": [{"feedback_id": str(item["feedback_id"]),
                          "signal": round(float(item["signal"]), 3),
                          "note": str(item["note"] or "")[:2_000],
                          "created_at": str(item["created_at"]),
                          "source_agent": str(item["source_agent"])} for item in feedback],
        }

    # ------------------------------------------------------------------
    # timeline
    # ------------------------------------------------------------------
    def timeline(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        days = _clamp_int(params, "days", 90, 1, 365)
        bucket = _text_param(params, "bucket", "day", 8)
        if bucket not in {"day", "week"}:
            bucket = "day"
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        raw = _text_param(params, "raw", "exclude", 16) or "exclude"
        agent = _text_param(params, "agent", "", 128)
        kind = _text_param(params, "kind", "", 64)
        since = _iso_since(days)
        clause = "scope=? AND created_at>=?"
        args: List[Any] = [scope, since]
        if raw != "include":
            clause += " AND raw_chat=0"
        if agent:
            clause += " AND source_agent=?"
            args.append(agent)
        if kind:
            clause += " AND kind=?"
            args.append(kind)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT substr(created_at,1,10) AS day, source_agent, kind, COUNT(*) AS count "
                "FROM memories WHERE " + clause + " GROUP BY day, source_agent, kind",
                tuple(args)).fetchall()
            event_rows = conn.execute(
                "SELECT substr(occurred_at,1,10) AS day, COUNT(*) AS count "
                "FROM ledger_events WHERE occurred_at>=? GROUP BY day", (since,)).fetchall()
        day_events = {str(row["day"]): int(row["count"]) for row in event_rows}
        day_agents: Dict[str, Dict[str, int]] = {}
        day_kinds: Dict[str, Dict[str, int]] = {}
        agent_totals: Dict[str, int] = {}
        for row in rows:
            day = str(row["day"])
            count = int(row["count"])
            source = str(row["source_agent"] or "unknown")
            day_agents.setdefault(day, {})
            day_agents[day][source] = day_agents[day].get(source, 0) + count
            kind_name = str(row["kind"] or "unknown")
            day_kinds.setdefault(day, {})
            day_kinds[day][kind_name] = day_kinds[day].get(kind_name, 0) + count
            agent_totals[source] = agent_totals.get(source, 0) + count
        buckets: Dict[str, Dict[str, Any]] = {}
        for day in _date_range(days):
            if bucket == "week":
                parsed = datetime.strptime(day, "%Y-%m-%d").date()
                key = (parsed - timedelta(days=parsed.weekday())).isoformat()
            else:
                key = day
            entry = buckets.setdefault(key, {"date": key, "memories": 0, "events": 0,
                                             "agents": {}, "kinds": {}})
            entry["events"] += day_events.get(day, 0)
            for source, count in day_agents.get(day, {}).items():
                entry["agents"][source] = entry["agents"].get(source, 0) + count
                entry["memories"] += count
            for kind_name, count in day_kinds.get(day, {}).items():
                entry["kinds"][kind_name] = entry["kinds"].get(kind_name, 0) + count
        ordered = [buckets[key] for key in sorted(buckets)]
        total_memories = sum(item["memories"] for item in ordered)
        peak = max((item["memories"] for item in ordered), default=0)
        return {
            "days": days,
            "bucket": bucket,
            "totals": {"memories": total_memories,
                       "events": sum(item["events"] for item in ordered),
                       "peak": peak,
                       "average": round(total_memories / max(1, len(ordered)), 1)},
            "buckets": ordered,
            "agents": [{"agent": name, "total": count}
                       for name, count in sorted(agent_totals.items(),
                                                 key=lambda item: -item[1])],
        }

    # ------------------------------------------------------------------
    # knowledge projections
    # ------------------------------------------------------------------
    def claims(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        state = _text_param(params, "state", "all", 32) or "all"
        if state not in {"active", "conflict", "superseded"}:
            state = "all"
        subject = _text_param(params, "subject", "", 256)
        limit = _clamp_int(params, "limit", 100, 1, 200)
        items = self.ledger.list_claims(
            scope=scope, state="" if state == "all" else state,
            subject=subject, limit=limit)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM memory_claims WHERE scope=? "
                "GROUP BY state", (scope,)).fetchall()
        counts = {"active": 0, "conflict": 0, "superseded": 0}
        for row in rows:
            counts[str(row["state"])] = int(row["count"])
        counts["total"] = sum(counts.values())
        previews = self._previews_for(str(item.get("memory_id", "")) for item in items)
        for item in items:
            item["memory_preview"] = _preview(
                previews.get(str(item.get("memory_id", "")), ""), 400)
        return {"scope": scope, "state": state, "counts": counts, "items": items}

    def conflicts(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        limit = _clamp_int(params, "limit", 50, 1, 200)
        report = self.ledger.list_conflicts(scope=scope, limit=limit)
        for item in report.get("items", []):
            item["content"] = _preview(item.get("content", ""), 120)
        return report

    def facts(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        subject = _text_param(params, "subject", "", 256)
        limit = _clamp_int(params, "limit", 200, 1, 500)
        items = self.ledger.list_facts(scope=scope, subject=subject, limit=limit)
        return {"scope": scope, "items": items}

    def skills(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        limit = _clamp_int(params, "limit", 100, 1, 200)
        return {"scope": scope, "items": self.ledger.list_skills(scope=scope, limit=limit)}

    def subjects(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        limit = _clamp_int(params, "limit", 200, 1, 500)
        return {"scope": scope, "items": self.ledger.recent_subjects(scope=scope, limit=limit)}

    def graph(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        subject = _text_param(params, "subject", "", 256)
        if not subject:
            raise GuiApiError(400, "subject 查询参数必填")
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        hops = _clamp_int(params, "hops", 1, 1, 2)
        limit = _clamp_int(params, "limit", 200, 10, 2_000)
        return self.ledger.entity_graph(subject, scope=scope, hops=hops, limit=limit)

    # ------------------------------------------------------------------
    # agents + health
    # ------------------------------------------------------------------
    def agents(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        hub = self.hub
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT source_agent, COUNT(*) AS memories, "
                "SUM(CASE WHEN lifecycle='active' THEN 1 ELSE 0 END) AS active, "
                "SUM(CASE WHEN raw_chat=1 THEN 1 ELSE 0 END) AS raw, "
                "MAX(created_at) AS last_seen FROM memories "
                "GROUP BY source_agent ORDER BY memories DESC LIMIT 200").fetchall()
            event_rows = conn.execute(
                "SELECT source_agent, COUNT(*) AS count FROM ledger_events "
                "GROUP BY source_agent LIMIT 200").fetchall()
            trust_rows = conn.execute(
                "SELECT agent_id, baseline FROM agent_trust LIMIT 200").fetchall()
            credential_rows = conn.execute(
                "SELECT agent_id, COUNT(*) AS count FROM agent_credentials "
                "WHERE revoked_at='' GROUP BY agent_id LIMIT 200").fetchall()
            cursor_rows = conn.execute(
                "SELECT source_id, cursor, updated_at, last_success_at, last_error "
                "FROM source_cursors ORDER BY updated_at DESC LIMIT 500").fetchall()
        events = {str(row["source_agent"]): int(row["count"]) for row in event_rows}
        trust = {str(row["agent_id"]): round(float(row["baseline"]), 3) for row in trust_rows}
        credentials = {str(row["agent_id"]): int(row["count"]) for row in credential_rows}
        merged: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            name = str(row["source_agent"] or "unknown")
            merged[name] = {"agent": name, "memories": int(row["memories"]),
                            "active": int(row["active"] or 0), "raw": int(row["raw"] or 0),
                            "last_seen": str(row["last_seen"] or "")}
        for name in sorted(set(events) | set(trust) | set(credentials)):
            entry = merged.setdefault(name, {"agent": name, "memories": 0, "active": 0,
                                             "raw": 0, "last_seen": ""})
            entry.setdefault("last_seen", "")
        for entry in merged.values():
            name = entry["agent"]
            entry["events"] = events.get(name, 0)
            entry["trust"] = trust.get(name, 0.5)
            entry["credentials"] = credentials.get(name, 0)
        cursors = [{"source_id": str(row["source_id"]),
                    "updated_at": str(row["updated_at"]),
                    "last_success_at": str(row["last_success_at"]),
                    "last_error": str(row["last_error"] or "")[:200],
                    "cursor_length": len(str(row["cursor"] or ""))} for row in cursor_rows]
        try:
            discovery = hub.discovery.summary(ensure=True)
        except Exception:
            discovery = {"available": False}
        try:
            injection = hub.injection.status("all")
        except Exception:
            injection = {"agent": "all", "enabled": False, "error": "invalid_state"}
        return {
            "agents": sorted(merged.values(), key=lambda item: (-item["memories"], item["agent"])),
            "cursors": cursors,
            "discovery": discovery,
            "auto_install": getattr(hub, "auto_install", {}) or {},
            "watcher": {"available": bool(getattr(hub.watcher, "available", False)),
                        "poll_interval": getattr(hub.watcher, "poll_interval", None)},
            "injection": injection,
            "credentials": sum(credentials.values()),
        }

    def health(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        hub = self.hub
        overview = self.ledger.health_overview()
        fts = self.ledger.fts_sync_report()
        claims_report = self.ledger.claims_consistency_report()
        storage = self._storage()
        with self.ledger._reading() as conn:
            journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()
            tool_output = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE kind='tool_output'").fetchone()[0])
        checks: List[Dict[str, Any]] = []
        schema = int(overview.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION)
        checks.append({"id": "ledger", "label": "账本", "severity": "ok",
                       "detail": "schema v%s · %s · %s 条事件" %
                                 (schema, journal or "未知日志模式", overview.get("events", 0)),
                       "fix": None})
        started = float(getattr(hub, "_started_at", time.time()))
        checks.append({"id": "daemon", "label": "daemon", "severity": "ok",
                       "detail": "运行中 · v%s · watcher=%s · 已运行 %s" %
                                 (__version__, "开" if getattr(hub, "watch_enabled", False) else "关",
                                  _format_uptime(time.time() - started)),
                       "fix": None})
        token_ok, token_detail = self._token_check()
        checks.append({"id": "token", "label": "本地令牌", "severity": "ok" if token_ok else "warn",
                       "detail": token_detail, "fix": None})
        fts_ok = str(fts.get("status", "warn")) == "ok"
        fts_detail = "索引 %s 行；应索引 %s 行；缺失 %s；陈旧 %s" % (
            fts.get("fts_rows", 0), fts.get("projections", 0),
            fts.get("unindexed", 0), fts.get("stale", 0))
        checks.append({"id": "fts", "label": "FTS 同步", "severity": "ok" if fts_ok else "warn",
                       "detail": fts_detail,
                       "fix": None if fts_ok else {"label": "收敛索引", "command": "hippomind maintain"}})
        claims_ok = str(claims_report.get("status", "warn")) == "ok"
        checks.append({"id": "claims", "label": "Claims 一致性",
                       "severity": "ok" if claims_ok else "warn",
                       "detail": "活跃 claim %s 条，悬空 %s 条" %
                                 (claims_report.get("active_claims", 0),
                                  claims_report.get("dangling", 0)),
                       "fix": None if claims_ok else
                              {"label": "体检并修复", "command": "hippomind checkup --repair"}})
        wal = int(storage.get("wal_bytes", 0) or 0)
        reclaimable = int(storage.get("reclaimable_bytes", 0) or 0)
        storage_severity = "warn" if wal > WAL_WARN_BYTES or reclaimable > 256 * 1024 * 1024 else "ok"
        checks.append({"id": "storage", "label": "容量",
                       "severity": storage_severity,
                       "detail": "DB %s · WAL %s · 可回收 %s" % (
                           _format_bytes(storage.get("db_bytes", 0)),
                           _format_bytes(wal), _format_bytes(reclaimable)),
                       "fix": None if storage_severity == "ok" else
                              {"label": "执行维护", "command": "hippomind maintain"}})
        checks.append({"id": "archive", "label": "冷归档", "severity": "ok",
                       "detail": "%s 个归档文件 · %s · 保留 %s 天" % (
                           storage.get("archive_files", 0),
                           _format_bytes(storage.get("archive_bytes", 0)),
                           storage.get("retention_days", DEFAULT_RAW_RETENTION_DAYS)),
                       "fix": None})
        quarantine = int(overview.get("quarantined", 0) or 0)
        checks.append({"id": "privacy", "label": "隐私面",
                       "severity": "warn" if tool_output else "ok",
                       "detail": (("历史账本仍有 %s 条 tool_output 投影，需要时运行 scan-raw/redact-raw；"
                                   "隔离行 %s 条") % (tool_output, quarantine))
                                 if tool_output else
                                 ("结构化记忆默认隔离 raw/tool/system；隔离行 %s 条" % quarantine),
                       "fix": None if not tool_output else
                              {"label": "扫描敏感值", "command": "hippomind scan-raw"}})
        integrity: Dict[str, Any] = {"ran": False, "ok": None, "status": "", "elapsed_ms": None}
        if _truthy_param(params, "integrity"):
            started_at = time.perf_counter()
            with self.ledger._reading() as conn:
                status = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            integrity = {"ran": True, "ok": status.lower() == "ok", "status": status,
                         "elapsed_ms": round((time.perf_counter() - started_at) * 1000.0, 1)}
        errors = [item for item in checks if item["severity"] == "error"]
        warnings = [item for item in checks if item["severity"] == "warn"]
        return {
            "generated_at": _utc_now(),
            "ready": not errors,
            "overview": overview,
            "fts": fts,
            "claims": claims_report,
            "storage": storage,
            "daemon": {"status": "ok", "uptime_s": round(max(0.0, time.time() - started), 1),
                       "pid": os.getpid(), "version": __version__,
                       "watch_enabled": bool(getattr(hub, "watch_enabled", False))},
            "checks": checks,
            "warnings": [item["detail"] for item in warnings],
            "errors": [item["detail"] for item in errors],
            "integrity": integrity,
        }

    def _token_check(self) -> Tuple[bool, str]:
        hub = self.hub
        if not bool(getattr(hub, "require_auth", True)):
            return True, "本次 daemon 未启用令牌（require_auth=false）"
        path = Path(getattr(hub.paths, "token_path", ""))
        try:
            stat = path.stat()
        except OSError:
            return False, "令牌文件不可读，GUI 无法写入；请检查运行目录权限"
        if os.name != "nt" and stat.st_mode & 0o077:
            return False, "令牌文件权限过宽（应为 0600），请运行 hippomind start 重新收紧"
        return True, "令牌文件存在且仅本用户可读（0600）"

    # ------------------------------------------------------------------
    # environment / activity / pins / duplicates
    # ------------------------------------------------------------------
    def environment(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        hub = self.hub
        package_root = Path(__file__).resolve().parents[1]
        storage = self._storage()
        started = float(getattr(hub, "_started_at", time.time()))
        server = getattr(hub, "_server", None)
        host = "127.0.0.1"
        port = 0
        if server is not None:
            try:
                host = str(server.server_address[0])
                port = int(server.server_address[1])
            except (AttributeError, IndexError, TypeError, ValueError):
                port = 0
        ui_url = "http://%s:%d/ui/" % (host, port) if port else ""
        api_url = "http://%s:%d/api/v1" % (host, port) if port else ""
        try:
            usage = shutil.disk_usage(str(package_root))
            disk = {"total_bytes": int(usage.total), "used_bytes": int(usage.used),
                    "free_bytes": int(usage.free)}
        except OSError:
            disk = {}
        external = _is_external_volume(package_root)
        with self.ledger._reading() as conn:
            journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()
            events = int(conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0])
            memories = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            schema = int(conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0])
        backups = _read_backup_registry(Path(getattr(hub.paths, "state_path", "")))
        latest = backups[-1] if backups else None
        latest_info = None
        if latest:
            backup_path = Path(str(latest.get("path", "")))
            try:
                exists = backup_path.is_file()
                size = backup_path.stat().st_size if exists else int(latest.get("bytes", 0) or 0)
            except OSError:
                exists = False
                size = int(latest.get("bytes", 0) or 0)
            created = str(latest.get("created_at", ""))
            age = None
            try:
                parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
            except (TypeError, ValueError):
                age = None
            latest_info = {"path": str(backup_path), "bytes": size,
                           "created_at": created, "sha256": str(latest.get("sha256", "")),
                           "exists": exists, "age_s": round(age, 1) if age is not None else None}
        dependencies: Dict[str, Any] = {"sqlite": sqlite3.sqlite_version}
        try:
            import matplotlib
            dependencies["matplotlib"] = str(getattr(matplotlib, "__version__", "?"))
        except Exception:
            dependencies["matplotlib"] = None
        launchd = _launchd_report(package_root) if sys.platform == "darwin" else []
        checks = self._environment_checks(disk, external, backups, latest_info,
                                          launchd, dependencies, journal)
        return {
            "generated_at": _utc_now(),
            "platform": {"system": platform.system(), "release": platform.release(),
                         "machine": platform.machine(),
                         "python": platform.python_version(),
                         "python_executable": sys.executable,
                         "package_root": str(package_root), "cwd": os.getcwd()},
            "workspace": {"path": str(package_root), "external_volume": external,
                          "disk": disk},
            "daemon": {"pid": os.getpid(), "host": host, "port": port,
                       "uptime_s": round(max(0.0, time.time() - started), 1),
                       "started_at": str(getattr(hub, "started_at", "")),
                       "watch_enabled": bool(getattr(hub, "watch_enabled", False)),
                       "ui_url": ui_url, "api_url": api_url},
            "ledger": {"path": str(self.ledger.path),
                       "db_bytes": int(storage.get("db_bytes", 0) or 0),
                       "wal_bytes": int(storage.get("wal_bytes", 0) or 0),
                       "freelist": int(storage.get("freelist", 0) or 0),
                       "reclaimable_bytes": int(storage.get("reclaimable_bytes", 0) or 0),
                       "schema_version": schema, "journal_mode": journal,
                       "events": events, "memories": memories},
            "backups": {"registry_path": str(Path(getattr(hub.paths, "state_path", "")).parent / "backups.json"),
                        "count": len(backups), "latest": latest_info},
            "launchd": launchd,
            "dependencies": dependencies,
            "home": {"HIPPOMIND_HOME": os.environ.get("HIPPOMIND_HOME", ""),
                     "runtime_dir": str(Path(getattr(hub.paths, "state_path", "")).parent)},
            "checks": checks,
        }

    def _environment_checks(self, disk: Mapping[str, Any], external: bool,
                            backups: List[Any], latest: Optional[Mapping[str, Any]],
                            launchd: List[Mapping[str, Any]],
                            dependencies: Mapping[str, Any], journal: str) -> List[Dict[str, Any]]:
        checks: List[Dict[str, Any]] = []
        version = platform.python_version()
        python_ok = sys.version_info >= (3, 9)
        checks.append({"id": "python", "label": "Python", "severity": "ok" if python_ok else "error",
                       "detail": "%s · %s" % (version, sys.executable),
                       "fix": None if python_ok else {"label": "安装 Python 3.9+", "command": "python3 --version"}})
        matplotlib_ok = dependencies.get("matplotlib") is not None
        checks.append({"id": "matplotlib", "label": "绘图依赖", "severity": "ok" if matplotlib_ok else "warn",
                       "detail": ("matplotlib %s" % dependencies.get("matplotlib")) if matplotlib_ok
                                 else "未安装 matplotlib（仅旧版可视化需要）",
                       "fix": None if matplotlib_ok else
                              {"label": "安装依赖", "command": "python3 -m pip install -r requirements.txt"}})
        if external:
            checks.append({"id": "workspace", "label": "工作区卷", "severity": "warn",
                           "detail": "工作区在外置卷；macOS 会拒绝 launchd 后台任务读取该卷，登录自启不可用",
                           "fix": {"label": "启动命令", "command": "hippomind start"}})
        else:
            checks.append({"id": "workspace", "label": "工作区卷", "severity": "ok",
                           "detail": "工作区在内置卷", "fix": None})
        free = int((disk or {}).get("free_bytes", 0) or 0)
        if free and free < 2 * 1024 * 1024 * 1024:
            checks.append({"id": "disk", "label": "磁盘空间", "severity": "warn",
                           "detail": "可用空间仅 %.1f GiB" % (free / (1024 ** 3)),
                           "fix": {"label": "清理空间", "command": "hippomind maintain"}})
        else:
            checks.append({"id": "disk", "label": "磁盘空间", "severity": "ok",
                           "detail": ("可用 %.1f GiB" % (free / (1024 ** 3))) if free else "不可用",
                           "fix": None})
        if not backups or latest is None:
            checks.append({"id": "backup", "label": "备份", "severity": "warn",
                           "detail": "没有登记备份；建议定期备份",
                           "fix": {"label": "创建备份", "command": "hippomind backup ~/hippomind-backup.db"}})
        else:
            age = latest.get("age_s")
            stale = age is None or age > 30 * 86400
            exists = bool(latest.get("exists"))
            severity = "warn" if stale or not exists else "ok"
            detail = "最近备份 %s 前" % _format_age(age) if age is not None else "最近备份时间未知"
            if not exists:
                detail += "（文件不存在）"
            checks.append({"id": "backup", "label": "备份", "severity": severity, "detail": detail,
                           "fix": None if severity == "ok" else
                                  {"label": "创建备份", "command": "hippomind backup ~/hippomind-backup.db"}})
        if sys.platform == "darwin":
            problems: List[str] = []
            for item in launchd:
                problems.extend(item.get("problems", []))
            if problems:
                checks.append({"id": "launchd", "label": "登录自启", "severity": "warn",
                               "detail": "；".join(problems[:3]),
                               "fix": {"label": "重装登录项", "command": "hippomind login-install"}})
            elif external:
                checks.append({"id": "launchd", "label": "登录自启", "severity": "warn",
                               "detail": "登录项存在，但外置卷受 TCC 限制；请用 hippomind start 手动启动",
                               "fix": {"label": "启动命令", "command": "hippomind start"}})
            elif launchd:
                checks.append({"id": "launchd", "label": "登录自启", "severity": "ok",
                               "detail": "登录项路径有效", "fix": None})
            else:
                checks.append({"id": "launchd", "label": "登录自启", "severity": "ok",
                               "detail": "未安装登录项（按需运行 login-install）", "fix": None})
        journal_ok = str(journal).upper() == "WAL"
        checks.append({"id": "journal", "label": "日志模式", "severity": "ok" if journal_ok else "warn",
                       "detail": journal or "未知", "fix": None if journal_ok else
                              {"label": "检查账本", "command": "hippomind checkup"}})
        return checks

    def events(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        limit = _clamp_int(params, "limit", 20, 1, 200)
        event_type = _text_param(params, "type", "", 64)
        agent = _text_param(params, "agent", "", 128)
        since = _text_param(params, "since", "", 40)
        excluded = ("raw_chat", "tool_output", "system_message", "developer_message",
                    "environment", "command_output", "approval")
        clauses = ["event_type NOT IN (" + ",".join("?" for _ in excluded) + ")"]
        args: List[Any] = list(excluded)
        if event_type:
            clauses.append("event_type=?")
            args.append(event_type)
        if agent:
            clauses.append("source_agent=?")
            args.append(agent)
        if since:
            clauses.append("occurred_at>=?")
            args.append(since)
        clause = " AND ".join(clauses)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT event_id,event_type,occurred_at,ingested_at,source_agent,"
                "session_id,task_id,project,role,scope,content,metadata_json "
                "FROM ledger_events WHERE " + clause +
                " ORDER BY occurred_at DESC LIMIT ?", tuple(args) + (limit,)).fetchall()
            count_rows = conn.execute(
                "SELECT event_type, COUNT(*) AS count FROM ledger_events WHERE " +
                clause + " GROUP BY event_type ORDER BY count DESC", tuple(args)).fetchall()
        items = []
        for row in rows:
            items.append({"event_id": str(row["event_id"]),
                          "event_type": str(row["event_type"]),
                          "occurred_at": str(row["occurred_at"]),
                          "ingested_at": str(row["ingested_at"]),
                          "source_agent": str(row["source_agent"]),
                          "session_id": str(row["session_id"]),
                          "task_id": str(row["task_id"]),
                          "project": str(row["project"]),
                          "role": str(row["role"]),
                          "scope": str(row["scope"]),
                          "content_preview": _preview(row["content"], 240),
                          "metadata": _json_value(row["metadata_json"], {})})
        counts = {str(row["event_type"]): int(row["count"]) for row in count_rows}
        return {"limit": limit, "items": items, "counts": counts,
                "total": sum(counts.values())}

    def pins(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        limit = _clamp_int(params, "limit", 100, 1, 500)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT " + _MEMORY_COLUMNS + " FROM memories WHERE pinned=1 AND scope=? "
                "ORDER BY updated_at DESC LIMIT ?", (scope, limit)).fetchall()
            total = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE pinned=1 AND scope=?", (scope,)).fetchone()[0])
        claims = self._has_claims_for([row["memory_id"] for row in rows])
        return {"scope": scope, "total": total,
                "items": [_public_memory(row, has_claims=str(row["memory_id"]) in claims)
                          for row in rows]}

    def duplicates(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        limit = _clamp_int(params, "limit", 20, 1, 100)
        sample = _clamp_int(params, "sample", 4000, 200, 20000)
        raw_similarity = _text_param(params, "min_similarity", "0.85", 16)
        try:
            similarity = max(0.5, min(1.0, float(raw_similarity)))
        except (TypeError, ValueError):
            similarity = 0.85
        return self.ledger.duplicate_candidates(
            limit=limit, sample=sample, min_similarity=similarity)

    # ------------------------------------------------------------------
    # writes (audited ledger operations only)
    # ------------------------------------------------------------------
    def pin(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        memory_id = str(body.get("memory_id", "") or "").strip()[:80]
        if not memory_id:
            raise GuiApiError(400, "memory_id 不能为空")
        pinned = bool(body.get("pinned", True))
        try:
            result = self.ledger.pin_memory(memory_id, pinned=pinned, source="local-admin")
        except LedgerError as exc:
            raise GuiApiError(400, str(exc))
        return {"memory_id": str(result.get("memory_id", memory_id)),
                "pinned": bool(result.get("pinned", pinned)),
                "updated_at": str(result.get("updated_at", ""))}

    def feedback(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        memory_id = str(body.get("memory_id", "") or "").strip()[:80]
        if not memory_id:
            raise GuiApiError(400, "memory_id 不能为空")
        try:
            signal = max(-1.0, min(1.0, float(body.get("signal", 0.0))))
        except (TypeError, ValueError):
            raise GuiApiError(400, "signal 必须是 -1 到 1 之间的数字")
        note = str(body.get("note", "") or "")[:400]
        try:
            updated = self.ledger.feedback(memory_id, signal, note, "local-admin")
        except LedgerError as exc:
            raise GuiApiError(400, str(exc))
        return {"memory_id": memory_id, "signal": signal, "updated": bool(updated)}

    # ------------------------------------------------------------------
    # one-time UI bootstrap tokens
    # ------------------------------------------------------------------
    def _prune_ui_nonces(self) -> None:
        now = time.time()
        for nonce in [key for key, expiry in self._ui_nonces.items() if expiry < now]:
            self._ui_nonces.pop(nonce, None)

    def mint_ui_token(self) -> Dict[str, Any]:
        self._prune_ui_nonces()
        if len(self._ui_nonces) >= MAX_PENDING_UI_NONCES:
            raise GuiApiError(429, "待用引导令牌过多，请稍后再试")
        nonce = secrets.token_urlsafe(24)
        expiry = time.time() + UI_NONCE_TTL_SECONDS
        self._ui_nonces[nonce] = expiry
        return {
            "nonce": nonce,
            "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat(timespec="seconds"),
            "ttl_seconds": UI_NONCE_TTL_SECONDS,
            "url": "/ui/#/pulse?boot=" + nonce,
        }


def _is_external_volume(path: Path) -> bool:
    try:
        return os.stat(str(path)).st_dev != os.stat(os.sep).st_dev
    except OSError:
        return False


def _read_backup_registry(state_path: Path) -> List[Dict[str, Any]]:
    registry = Path(str(state_path)).parent / "backups.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return [item for item in data if isinstance(item, Mapping)] if isinstance(data, list) else []


def _launchd_report(package_root: Path) -> List[Dict[str, Any]]:
    root = Path.home() / "Library" / "LaunchAgents"
    labels = (("com.hippomind.daemon", "com.hippomind.daemon.plist"),
              ("com.hippomind.gui", "com.hippomind.gui.plist"))
    report: List[Dict[str, Any]] = []
    for label, filename in labels:
        path = root / filename
        item: Dict[str, Any] = {"label": label, "path": str(path), "exists": path.is_file(),
                                "problems": []}
        if not item["exists"]:
            report.append(item)
            continue
        try:
            with path.open("rb") as handle:
                config = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            item["problems"].append("plist 不可读: %s" % type(exc).__name__)
            report.append(item)
            continue
        if not isinstance(config, Mapping):
            item["problems"].append("plist 不是字典")
            report.append(item)
            continue
        environment = config.get("EnvironmentVariables")
        environment = environment if isinstance(environment, Mapping) else {}
        pythonpath = str(environment.get("PYTHONPATH", "") or "")
        if pythonpath and not Path(pythonpath).is_dir():
            item["problems"].append("PYTHONPATH 指向不存在的目录: %s" % pythonpath)
        working = str(config.get("WorkingDirectory", "") or "")
        if working and not Path(working).is_dir():
            item["problems"].append("WorkingDirectory 指向不存在的目录: %s" % working)
        program = config.get("ProgramArguments")
        if isinstance(program, (list, tuple)) and program:
            executable = str(program[0])
            if executable and not Path(executable).exists():
                item["problems"].append("解释器不存在: %s" % executable)
        if pythonpath and Path(pythonpath).resolve() != package_root.resolve():
            item["problems"].append("PYTHONPATH 与当前包根不一致: %s" % pythonpath)
        report.append(item)
    return report


def _format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "未知"
    total = int(max(0, seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return "%s 天 %s 小时" % (days, hours)
    if hours:
        return "%s 小时 %s 分" % (hours, minutes)
    return "%s 分钟" % minutes


def _format_bytes(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    if amount <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024.0
        index += 1
    digits = 0 if index == 0 or amount >= 100 else 1
    return ("%.*f %s" % (digits, amount, units[index]))


def _format_uptime(seconds: float) -> str:
    total = int(max(0.0, seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return "%s 天 %s 小时" % (days, hours)
    if hours:
        return "%s 小时 %s 分" % (hours, minutes)
    return "%s 分" % max(1, minutes)
