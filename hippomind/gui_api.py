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
import math
import os
import platform
import plistlib
import secrets
import shutil
import sqlite3
import sys
import threading
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
from .forgetting import (
    DEFAULT_DAYS as FORGETTING_DEFAULT_DAYS,
    DEFAULT_LIMIT as FORGETTING_DEFAULT_LIMIT,
    ReadOnlyLedger as ForgettingReader,
    forgetting_report,
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

#: 遗忘审计报告在进程内的存活时间（秒）。
#:
#: 为什么要缓存：forgetting_report() 每次都要重读整个窗口，其中「全部时间 decay
#: 事件数」在生产账本上是一次无索引全表扫描（实测：页缓存热 ~50ms，冷启动
#: 0.8-2.1s；ledger_events 268k 行、账本 924MB）。控制台每次打开视图都付一次
#: 这个成本，首屏就卡住了。
#:
#: 为什么还要 TTL 而不是纯按 generation 失效：ledger.generation 只对**本进程**
#: 的写事务自增。运维在终端里跑一次 maintain 是另一个进程写的，generation 不动，
#: 缓存会把维护前的报告一直端出去。30 秒是「审计报告」这个新鲜度诉求的上限：
#: 它描述的是按天计的衰减，不是秒级指标。
#: 2026-10-25 从 30s 提到 300s。原值定得太短，理由本身没错（运维在终端跑
#: maintain 是另一个进程写的，generation 不动，缓存会把维护前的报告一直端出去），
#: 但它把「跨进程维护」的新鲜度诉求套在了「按天计的衰减报告」上：
#:
#:   * 这份报告描述的是天级的 utility 衰减与归档，不是秒级指标；
#:   * 实测全量重算 0.65s（冷页）/ 0.065s（热页），TTL=30s 意味着
#:     每 30 秒就有一个倒霉请求要付一次全表扫描；
#:   * maintain 的 tick 是小时级，5 分钟的新鲜度已经比它细。
#:
#: 需要立刻看到最新状态时，加 ?refresh=1 绕过缓存（见 forgetting()）。
FORGETTING_CACHE_TTL_SECONDS = 300.0

#: 首屏汇总（/api/v1/overview）与体检（/api/v1/health）的缓存存活时间。
#:
#: 为什么要缓存：这两个端点是 Pulse / Health 视图的**首屏**依赖，而它们各自
#: 都要跑一遍全库聚合。2026-10-25 生产账本（26.8 万记忆 / 27.9 万事件 / 881 MiB）
#: 实测：ledger.stats() 3.2s、integrity_check() 3.6s、health_overview() 0.7s、
#: fts_sync_report() 0.5s。Pulse 视图同时调 overview + health，首屏超过 15 秒——
#: 截图 QA 里表现为「骨架屏还在，页面一片空白」，而报告却显示 PASS。
#:
#: 15 秒是「账本规模 + 无索引 COUNT」的物理成本，不是可以靠优化查询消掉的；
#: 而这些数字描述的是**按小时变化的账本规模**，不是秒级指标。缓存 60 秒让
#: 第二次之后的访问是毫秒级，同时保持「一分钟内的变化可见」。
#: 需要立刻看到最新值时加 ?refresh=1。
SNAPSHOT_CACHE_TTL_SECONDS = 60.0

#: 报告缓存的条目上限。键里含 days/limit/scope/include_raw_chat，正常浏览只会
#: 用到个位数条目；超过就整体清空（代数一变旧键永远不可能再命中，留着是泄漏）。
FORGETTING_CACHE_MAX = 8

#: GUI 允许回看的天数上限。与 CLI 的 --days 不同，这里要挡掉「一次点开 3650 天」
#: 这种把事件预算打满的请求；1 年是衰减审计还能问出答案的实际边界。
FORGETTING_MAX_DAYS = 365

#: A/B 进度端点的 tail-read 上界（字节）。
#:
#: 注：injection_trace.recent()/summary() 已改为尾部有界读取；GUI 侧仍自行
#: append-only 且没有轮转，所以直接用它等于把文件大小变成每次请求的成本。
#: 2 MiB 够放几万条 trace（一条约 300-400 字节），而「距 30 条还差多少」这个
#: 问题不需要全量历史：数到 30 就够下结论了，再多也只是同一个答案。
INJECTION_AB_TAIL_BYTES = 2 * 1024 * 1024

#: 进度端点允许回看多少条 trace（tail 读之后的上界）。
INJECTION_AB_MAX_TRACES = 50_000

#: 单飞锁的上限。键里含写代数，而 daemon 每次写事务都会让它变一次，所以这个
#: dict 会随写入缓慢增长。超过就整体清空：**等待者持有的是 Lock 对象本身**，
#: 清空 dict 不会打断它们，最多让一个后来的调用者新建一把锁、多扫一遍窗口——
#: 代价是一次 60ms 的重复读，而留着不清理是确定的泄漏。
FORGETTING_LOCK_MAX = 32

#: Endpoint catalogue served by /api/v1/meta and rendered by the reference view.
ENDPOINTS: Tuple[Dict[str, Any], ...] = (
    {"method": "GET", "path": "/health", "summary": "存活与账本指纹（无需令牌）", "auth": "none"},
    {"method": "GET", "path": "/stats", "summary": "账本/归档/来源/缓存统计", "auth": "token"},
    {"method": "POST", "path": "/recall", "summary": "关键词召回（FTS + LIKE 双通道）", "auth": "token"},
    {"method": "GET", "path": "/context-pack",
     "summary": "组装最小、带证据的上下文包（query string）", "auth": "token"},
    {"method": "POST", "path": "/context-pack", "summary": "组装最小、带证据的上下文包", "auth": "token"},
    {"method": "POST", "path": "/feedback", "summary": "以外部反馈调整工作记忆权重", "auth": "token"},
    {"method": "GET", "path": "/graph", "summary": "实体图谱邻域", "auth": "token"},
    {"method": "GET", "path": "/conflicts", "summary": "冲突/被取代 claim 谱系", "auth": "token"},
    {"method": "GET", "path": "/metrics", "summary": "Prometheus 文本指标（需令牌）", "auth": "token"},
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
    {"method": "GET", "path": "/api/v1/forgetting", "summary": "遗忘审计：为什么被降权、还能否召回", "auth": "admin"},
    {"method": "GET", "path": "/api/v1/injection-ab",
     "summary": "注入 A/B 实验进度：两臂样本与还差多少", "auth": "admin"},
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

    def __init__(self, hub: Any, *, prefetch: bool = False):
        self.hub = hub
        self.ledger = hub.ledger
        self._ui_nonces: Dict[str, float] = {}
        # 遗忘审计报告的进程内缓存：值 = (monotonic, 报告)，键见 _forgetting_key()。
        # 用锁而不是「读多写少就无所谓」：ThreadingHTTPServer 下两个标签页同时
        # 打开视图就会并发写这个 dict。
        self._forgetting_cache: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}
        # 首屏快照缓存：overview 与 health 各一个槽位。键含 generation，
        # 所以本进程写一次就会失效——跨进程写入靠 TTL 兜底（与 forgetting 同理）。
        self._snapshot_cache: Dict[str, Tuple[float, int, Dict[str, Any]]] = {}
        self._snapshot_lock = threading.Lock()
        self._forgetting_lock = threading.Lock()
        # 单飞：每个键一把锁，先到者扫描、后到者等它发布结果再直接命中缓存。
        # 没有它，预取与真实请求撞在一起时会把 279 MiB 扫两遍。
        self._forgetting_build_locks: Dict[Tuple[Any, ...], threading.Lock] = {}
        #: 预热线程句柄。默认**不启动**：__init__ 由 MemoryHubService 调用，
        #: 而构造 service 不代表进程会长跑（测试建完就删目录）。这条线程持有
        #: 只读连接，会创建 hub.sqlite-shm；在删除目录之后才创建就会让 rmtree
        #: 撞上 ENOTEMPTY。长跑进程（daemon main）显式调用
        #: start_forgetting_prefetch()，并在 shutdown 前 close() 等它结束。
        self._prefetch_thread: Optional[threading.Thread] = None
        if prefetch:
            self.start_forgetting_prefetch()

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
        if route == "forgetting":
            return self.forgetting(params)
        if route == "injection-ab":
            return self.injection_ab(params)
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
            for pattern in ("raw-*.jsonl.gz", "purged-*.jsonl.gz", "dedupe-*.jsonl.gz"):
                for archive in root.glob(pattern):
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

    def _snapshot(self, slot, refresh, compute):
        """Cache one whole-ledger aggregate. slot is overview/health."""
        generation = int(getattr(self.ledger, "generation", 0) or 0)
        now = time.monotonic()
        with self._snapshot_lock:
            cached = self._snapshot_cache.get(slot)
            if (not refresh and cached is not None
                    and cached[1] == generation
                    and (now - cached[0]) <= SNAPSHOT_CACHE_TTL_SECONDS):
                return cached[2]
        value = compute()
        with self._snapshot_lock:
            self._snapshot_cache[slot] = (time.monotonic(), generation, value)
        return value

    def overview(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        refresh = _truthy_param(params, "refresh")
        return self._snapshot("overview", refresh,
                              lambda: self._overview_uncached(params))

    def _overview_uncached(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
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
        # The daemon has a 512 KiB response budget.  Long values used to push
        # a 500-row page over it and the whole view failed with 413; trim the
        # values here so a bounded page always fits.
        for item in items:
            value = item.get("value")
            if isinstance(value, str) and len(value) > 240:
                item["value"] = value[:240] + "…"
            content = item.get("content")
            if isinstance(content, str) and len(content) > 400:
                item["content"] = content[:400] + "…"
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
        refresh = _truthy_param(params, "refresh")
        # integrity=1 runs a whole-ledger PRAGMA check (3.6s on production).
        # It is only reached when the user explicitly asks for a deep check, so
        # caching a possibly-stale integrity verdict would be pointless.
        if _truthy_param(params, "integrity"):
            return self._health_uncached(params)
        return self._snapshot("health", refresh,
                              lambda: self._health_uncached(params))

    def _health_uncached(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
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
        capture = getattr(hub, "health", {}).get("capture") if isinstance(getattr(hub, "health", {}), Mapping) else {}
        capture = capture if isinstance(capture, Mapping) else {}
        capture_error = str(capture.get("error", "") or "")
        checks.append({"id": "capture", "label": "自动采集",
                       "severity": "warn" if capture_error else "ok",
                       "detail": capture_error or ("%s 个文件 · %s 条消息 · %s 条新增" % (
                           capture.get("files", 0), capture.get("messages", 0),
                           capture.get("inserted", 0))) if capture else "等待首次采集",
                       "fix": ({"label": "手动补采", "command": "hippomind sync-transcripts"}
                               if capture_error else None)})
        distill = getattr(hub, "health", {}).get("distill") if isinstance(getattr(hub, "health", {}), Mapping) else {}
        distill = distill if isinstance(distill, Mapping) else {}
        distill_error = str(distill.get("error", "") or "")
        checks.append({"id": "distill", "label": "自动提炼",
                       "severity": "warn" if distill_error else "ok",
                       "detail": distill_error or ("%s 个候选 · %s 条 claims · %s 条提炼" % (
                           distill.get("candidates", 0), distill.get("claims", 0),
                           distill.get("distilled", 0))) if distill else "等待首次提炼",
                       "fix": ({"label": "手动提炼", "command": "hippomind distill --limit 16"}
                               if distill_error else None)})
        try:
            from .injection_trace import summary as injection_summary
            injection = injection_summary()
        except Exception:
            injection = {"count": 0, "injected": 0, "last_at": ""}
        checks.append({"id": "injection", "label": "自动注入",
                       "severity": "ok",
                       "detail": ("最近 %s 次注入 · %s 次实际注入 · 最近 %s" % (
                           injection.get("count", 0), injection.get("injected", 0),
                           injection.get("last_at", "") or "尚未记录")),
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
    # forgetting audit
    # ------------------------------------------------------------------
    def forgetting(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        """Why did a memory lose weight, and can it still be recalled?

        Everything the numbers mean lives in hippomind/forgetting.py; this is
        only the transport.  Two things it must not do:

        * write.  The report opens its own mode=ro handle (ReadOnlyLedger), so
          measuring production can never mutate it -- not even by accident.
        * answer slowly.  See FORGETTING_CACHE_TTL_SECONDS for the measured
          cost, and why the memo is keyed on (ledger path, generation) + TTL.
        """
        days = _clamp_int(params, "days", FORGETTING_DEFAULT_DAYS, 1,
                          FORGETTING_MAX_DAYS)
        limit = _clamp_int(params, "limit", FORGETTING_DEFAULT_LIMIT, 1,
                           MAX_LIST_LIMIT)
        scope = _text_param(params, "scope", "shared", 128) or "shared"
        include_raw_chat = _truthy_param(params, "include_raw_chat")
        # ?refresh=1 绕过缓存。TTL 从 30s 提到 300s 之后，这条是「我现在就要
        # 看最新状态」的唯一入口——比如刚在终端跑完 maintain，不想等 5 分钟。
        # 没有它，长 TTL 就变成「缓存说了算」，而这份报告的职责是解释现状。
        if _truthy_param(params, "refresh"):
            self._forgetting_cache.pop(
                self._forgetting_key(days, limit, scope, include_raw_chat), None)
        report, hit, age_s, elapsed_ms = self._forgetting_result(
            days=days, limit=limit, scope=scope,
            include_raw_chat=include_raw_chat)
        return _with_cache_note(report, hit=hit, age_s=age_s,
                                elapsed_ms=elapsed_ms)

    def _forgetting_key(self, days: int, limit: int, scope: str,
                        include_raw_chat: bool) -> Tuple[Any, ...]:
        """(账本路径, 写代数, 窗口参数) —— 任一变化都必须重新测量。

        路径而不是 id(ledger)：对象回收后地址会复用，新账本会命中旧账本的报告
        （entity_index 的缓存注释里记着同一次踩坑）。
        """
        return (str(getattr(self.ledger, "path", "")),
                int(getattr(self.ledger, "generation", 0) or 0),
                days, limit, scope, include_raw_chat)

    def _forgetting_result(self, *, days: int, limit: int, scope: str,
                           include_raw_chat: bool
                           ) -> Tuple[Dict[str, Any], bool, float, float]:
        """缓存命中、等别人算完、或自己算——三者必居其一。

        Returns (report, hit, age_s, elapsed_ms).

        The per-key lock is what keeps a prefetch and a real request from
        scanning the same 279 MiB concurrently: the loser blocks, then finds
        the winner's report already published and returns a cache hit.
        """
        key = self._forgetting_key(days, limit, scope, include_raw_chat)
        now = time.monotonic()
        with self._forgetting_lock:
            cached = self._forgetting_cache.get(key)
            if (cached is not None
                    and (now - cached[0]) <= FORGETTING_CACHE_TTL_SECONDS):
                return cached[1], True, now - cached[0], 0.0
            if len(self._forgetting_build_locks) >= FORGETTING_LOCK_MAX:
                self._forgetting_build_locks.clear()
            key_lock = self._forgetting_build_locks.get(key)
            if key_lock is None:
                key_lock = threading.Lock()
                self._forgetting_build_locks[key] = key_lock
        with key_lock:
            now = time.monotonic()
            with self._forgetting_lock:
                cached = self._forgetting_cache.get(key)
            if (cached is not None
                    and (now - cached[0]) <= FORGETTING_CACHE_TTL_SECONDS):
                return cached[1], True, now - cached[0], 0.0
            started = time.monotonic()
            try:
                with ForgettingReader(self.ledger.path) as reader:
                    report = forgetting_report(
                        reader, days=days, limit=limit, scope=scope,
                        include_raw_chat=include_raw_chat)
            except FileNotFoundError as exc:
                raise GuiApiError(503, "账本不存在，无法做遗忘审计: %s" % exc)
            except sqlite3.Error as exc:
                raise GuiApiError(503, "读取账本失败: %s: %s"
                                 % (type(exc).__name__, exc))
            elapsed_ms = (time.monotonic() - started) * 1000.0
            with self._forgetting_lock:
                if len(self._forgetting_cache) >= FORGETTING_CACHE_MAX:
                    self._forgetting_cache.clear()
                self._forgetting_cache[key] = (time.monotonic(), report)
            return report, False, 0.0, elapsed_ms

    def start_forgetting_prefetch(self) -> Optional[threading.Thread]:
        """在后台把默认窗口算好，别让第一次打开视图的人等这次全表扫描。

        NOT called from __init__ on purpose.  Constructing a GuiApi is not a
        statement about how long the process lives, and this thread opens the
        ledger read-only -- which creates hub.sqlite-shm.  A caller that
        builds a service and then removes its directory (every test in
        tests/test_mcp_register.py) gets a -shm recreated after the unlink
        and rmtree fails with ENOTEMPTY.  The daemon's main() is the only
        caller that knows it will run for weeks, so it starts this and
        shutdown() joins it.

        Idempotent and best effort: a second call is a no-op, and a failure
        can never stop the daemon from serving.  The scan it hides is the
        unindexed all-time decay COUNT, measured at 0.7-4.6 s with a cold
        page cache on the 881 MiB production ledger.
        """
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            return self._prefetch_thread
        def warm() -> None:
            try:
                self._forgetting_result(
                    days=FORGETTING_DEFAULT_DAYS,
                    limit=FORGETTING_DEFAULT_LIMIT,
                    scope="shared", include_raw_chat=False)
            except Exception as exc:  # noqa: BLE001 - 预取失败不能影响 daemon
                sys.stderr.write("[hippomindd] forgetting prefetch skipped: %s: %s\n"
                                 % (type(exc).__name__, exc))

        thread = threading.Thread(target=warm, name="forgetting-prefetch",
                                  daemon=True)
        self._prefetch_thread = thread
        thread.start()
        return thread

    def close(self) -> None:
        """Wait for the prefetch, so its read-only handle cannot outlive us.

        A reader that is still open when the ledger directory is removed
        recreates hub.sqlite-shm after the unlink, and the removal then fails
        with ENOTEMPTY.  Observed in tests/test_mcp_register.py, which tears
        its temporary directory down immediately after constructing a
        service.  Joining here means "this GuiApi is done" is a real
        statement rather than a hope.

        Bounded: the scan is one COUNT over ledger_events (60 ms warm,
        0.7-2.7 s cold), so a 30 s ceiling cannot turn a shutdown into a hang.
        """
        thread = self._prefetch_thread
        self._prefetch_thread = None
        if thread is None or not thread.is_alive():
            return
        if thread is threading.current_thread():
            return
        thread.join(timeout=30.0)


    # ------------------------------------------------------------------
    # injection A/B experiment progress
    # ------------------------------------------------------------------
    def injection_ab(self, params: Mapping[str, List[str]]) -> Dict[str, Any]:
        """How far along is the injection A/B experiment, and what is missing?

        Why this is not just injection_quality.report(): measured on the
        production ledger, evaluate() costs 3.4 s (background+controls) or
        1.2 s (both off), and _diagnostics alone is 4.7 s of SQL over
        raw_chat.  None of that is needed to answer "how many samples do we
        have".  The progress numbers come from explain() plus a bounded tail
        read of the trace file, which together measured 0.74 ms.

        The deeper metrics stay behind ?metrics=1 so the view can offer them as
        an explicit action instead of paying for them on every paint.

        Two different denominators, and conflating them is how this kind of
        dashboard starts lying:
          * min_samples (30) is the floor for ANY conclusion;
          * a matched arm comparison additionally needs
            min(treatment_n, control_n) >= min_samples, so the CONTROL arm is
            the binding constraint at the default 20% rate.
        The trace counts below are an UPPER BOUND on evaluable samples, not a
        substitute for them: a trace is only evaluable when its window has a
        readable assistant reply.  evaluable is null unless the caller asked
        for metrics -- it is never guessed from the trace count.
        """
        from . import injection_ab as ab
        from .injection_quality import MIN_SAMPLES

        limit = _clamp_int(params, "limit", 500, 1, INJECTION_AB_MAX_TRACES)
        config = ab.explain()

        counts = self._injection_trace_counts()
        treatment_n = counts["treatment"]
        control_n = counts["control"]

        # The arm comparison needs BOTH arms at the floor; at the default rate
        # the control arm is always the one that is short.
        treatment_short = max(0, MIN_SAMPLES - treatment_n)
        control_short = max(0, MIN_SAMPLES - control_n)
        binding = "control" if control_short >= treatment_short else "treatment"

        payload: Dict[str, Any] = {
            "config": config,
            "min_samples": MIN_SAMPLES,
            "traces": {
                "read": counts["read"],
                "malformed": counts["malformed"],
                "truncated": counts["truncated"],
                "treatment": treatment_n,
                "control": control_n,
                "sampled_upper_bound": treatment_n + control_n,
                "last_at": counts["last_at"],
                "agents": counts["agents"],
                "path": counts["path"],
            },
            "progress": {
                "treatment": {"have": treatment_n, "need": MIN_SAMPLES,
                              "shortfall": treatment_short,
                              "ratio": _ratio(treatment_n, MIN_SAMPLES)},
                "control": {"have": control_n, "need": MIN_SAMPLES,
                            "shortfall": control_short,
                            "ratio": _ratio(control_n, MIN_SAMPLES)},
                "binding_arm": binding,
                "binding_shortfall": max(treatment_short, control_short),
                "prompts_needed": config.get("prompts_needed_for_min_samples"),
                "expected_control_per_100_prompts":
                    config.get("expected_control_per_100_prompts"),
            },
            "arms": None,
            "evaluable": None,
            "verdict": None,
            "note": (
                "trace 计数是「可评估样本」的上界，不是它本身：一条 trace 只有在其"
                "时间窗内存在可读的 assistant 回复时才可评估。要得到真实的可评估"
                "样本数与两臂指标，用 ?metrics=1（需要数秒）。"),
        }

        if _truthy_param(params, "metrics"):
            started = time.monotonic()
            try:
                from .injection_quality import ReadOnlyLedger as QualityReader
                from .injection_quality import evaluate as quality_evaluate
                with QualityReader(self.ledger.path) as reader:
                    report = quality_evaluate(
                        reader, self._injection_traces(limit),
                        embedder=None, background=False, controls=False)
                payload["arms"] = report.get("arms")
                payload["evaluable"] = report.get("sampled")
                payload["verdict"] = report.get("verdict")
                payload["sample"] = report.get("sample")
                payload["metrics"] = report.get("metrics")
                payload["metrics_elapsed_ms"] = round(
                    (time.monotonic() - started) * 1000.0, 1)
            except sqlite3.Error as exc:
                payload["metrics_error"] = (
                    "%s: %s" % (type(exc).__name__, exc))[:300]
            except Exception as exc:  # noqa: BLE001 - 指标失败不能拖垮进度
                payload["metrics_error"] = (
                    "%s: %s" % (type(exc).__name__, exc))[:300]
        return payload

    def _injection_trace_path(self) -> Path:
        from .injection_trace import trace_path
        return trace_path()

    def _read_trace_tail(self) -> Optional[bytes]:
        """Bounded tail of the append-only trace file, or None when absent.

        Deliberately not injection_trace.recent(): that reads the WHOLE file
        before slicing, and this file has no rotation.
        """
        path = self._injection_trace_path()
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > INJECTION_AB_TAIL_BYTES:
                    handle.seek(size - INJECTION_AB_TAIL_BYTES)
                    handle.readline()  # drop the partial first line
                return handle.read()
        except OSError:
            return None

    def _injection_trace_counts(self) -> Dict[str, Any]:
        """Count both arms from a bounded tail of the trace file."""
        out: Dict[str, Any] = {"treatment": 0, "control": 0, "read": 0,
                               "malformed": 0, "truncated": False,
                               "last_at": "", "agents": [],
                               "path": str(self._injection_trace_path())}
        try:
            out["truncated"] = (self._injection_trace_path().stat().st_size
                                > INJECTION_AB_TAIL_BYTES)
        except OSError:
            return out
        raw = self._read_trace_tail()
        if raw is None:
            return out
        agents = set()
        for line in raw.decode("utf-8", "replace").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                entry = json.loads(text)
            except ValueError:
                out["malformed"] += 1
                continue
            if not isinstance(entry, dict):
                out["malformed"] += 1
                continue
            out["read"] += 1
            if entry.get("injected"):
                out["treatment"] += 1
            else:
                out["control"] += 1
            agent = str(entry.get("agent") or "")
            if agent:
                agents.add(agent)
            at = str(entry.get("at") or "")
            if at > out["last_at"]:
                out["last_at"] = at
        out["agents"] = sorted(agents)
        return out

    def _injection_traces(self, limit: int) -> List[Dict[str, Any]]:
        """The newest limit traces, for the opt-in metrics path."""
        raw = self._read_trace_tail()
        if raw is None:
            return []
        rows: List[Dict[str, Any]] = []
        for line in raw.decode("utf-8", "replace").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                entry = json.loads(text)
            except ValueError:
                continue
            if isinstance(entry, dict):
                rows.append(entry)
        return rows[-max(1, int(limit)):]

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


def _ratio(value: Any, total: Any) -> float:
    """value/total clamped to [0, 1]; 0.0 when the denominator is unusable.

    A progress bar that can exceed 1.0 is a bug report from the future: the
    sample count is data from another writer and must never be trusted to be
    <= need.
    """
    try:
        denominator = float(total)
        numerator = float(value)
    except (TypeError, ValueError):
        return 0.0
    if denominator <= 0 or not math.isfinite(denominator):
        return 0.0
    if not math.isfinite(numerator):
        return 0.0
    return max(0.0, min(1.0, numerator / denominator))


def _with_cache_note(report: Mapping[str, Any], *, hit: bool, age_s: float,
                     elapsed_ms: float) -> Dict[str, Any]:
    """The report verbatim, plus the one envelope field the daemon adds.

    Every key forgetting_report() returns stays at the top level and is passed
    through untouched.  The console and scripts/forgetting_report.py --json must
    read the same document: an operator cross-checking the view against the CLI
    has to get identical numbers, and a view that reshaped the report would be a
    second, drifting definition of "explained".

    cache is additive and is the only key forgetting.py does not produce.  It
    exists so the console can say how old the answer is instead of implying
    every paint re-measured the ledger.
    """
    payload = dict(report)
    payload["cache"] = {"hit": bool(hit), "age_s": round(max(0.0, age_s), 3),
                        "ttl_s": FORGETTING_CACHE_TTL_SECONDS,
                        "elapsed_ms": round(max(0.0, elapsed_ms), 1)}
    return payload


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
