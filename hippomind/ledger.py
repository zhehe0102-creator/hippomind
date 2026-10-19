"""SQLite/WAL event ledger for the long-running HippoMind Memory Hub.

The original Grid World hippocampus keeps its compact JSON snapshot for the
interactive experiment.  This module is deliberately separate: it is an
append-only, cross-agent evidence ledger whose mutable memory rows are only a
projection of immutable events.  Lifecycle maintenance can archive or
supersede a memory projection, but it never silently removes raw evidence.
"""

from __future__ import annotations

import hashlib
import gzip
import copy
import json
import math
import time as _time_mod
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple
from collections import deque

from .textutil import (content_fingerprint, informative_ratio, is_noise_text,
                       normalise_for_dedup, token_sim)
from .sanitize import MAX_ARCHIVE_MESSAGE_CHARS, sanitize_chat_content
from . import schema as _schema


# 中英技术词映射：跨语言查询改写用（扩展通道），命中即补对侧词。
CROSS_LANGUAGE_TERMS = {
    "数据库": ["database", "db"],
    "部署": ["deploy", "deployment"],
    "缓存": ["cache"],
    "监控": ["monitor", "monitoring"],
    "日志": ["log", "logging"],
    "网络": ["network"],
    "服务器": ["server"],
    "配置": ["config", "configuration"],
    "测试": ["test", "testing"],
    "版本": ["version", "release"],
    "备份": ["backup"],
    "恢复": ["recovery", "restore"],
    "安全": ["security"],
    "密码": ["password"],
    "用户": ["user"],
    "接口": ["api", "interface"],
    "容器": ["container", "docker"],
    "前端": ["frontend"],
    "后端": ["backend"],
    "性能": ["performance"],
    "内存": ["memory"],
    "磁盘": ["disk"],
    "证书": ["certificate", "tls"],
    "域名": ["domain"],
    "告警": ["alert", "alerting"],
    "权限": ["permission", "auth"],
    "登录": ["login"],
    "回滚": ["rollback"],
    "扩容": ["scale", "scaling"],
    "灰度": ["canary"],
    "消息队列": ["kafka", "queue"],
    "负载均衡": ["balancer"],
}

#: FTS tokenizer and schema version are owned by :mod:`hippomind.schema`, which
#: is the single source of truth for the database layout.
_fts_tokenizer = _schema._fts_tokenizer
SCHEMA_VERSION = _schema.SCHEMA_VERSION
MAX_CONTENT_CHARS = 2_000_000
MAX_METADATA_CHARS = 64_000
MAX_EVIDENCE = 128
MAX_TAGS = 64
# 检索候选池天花板：offset 深分页需要 limit+offset 个候选，池不足会
# 静默返回空页（2026-08-22 cursor 分页评估发现并修复）。穷举遍历仍建议
# 用 created_at keyset 游标，search 是相关性排序不是全表枚举。
_FTS_POOL_CEILING = 20_000
_LIKE_POOL_CEILING = 8_000
# LIKE 兜底扫描的行窗口上限：只在新度最高的 N 行内做子串救援，
# 保证最坏情况成本有界（69 万行全表扫描实测 11s，10 万行窗口 <400ms）。
_LIKE_SCAN_WINDOW = 100_000
# raw 审计 LIKE 通道的行窗口上限（raw 行可占库容 99%+）。
_RAW_LIKE_SCAN_WINDOW = 20_000
MAX_QUERY_CHARS = 4_000
MAX_EXPORT_ROWS = 250_000
RAW_KINDS = {"raw_chat", "tool_output", "system_message"}
PORTABLE_EXCLUDED_EVENT_KINDS = RAW_KINDS | {"developer_message", "environment", "command_output", "approval"}
DEFAULT_RAW_RETENTION_DAYS = 30
DEFAULT_RAW_CAPACITY = 5_000
DEFAULT_RAW_ARCHIVE_MAX_BYTES = 256 * 1024 * 1024
RAW_ARCHIVE_DIR_NAME = "raw-archive"
# 自动缩容阈值：删除大量行后页会进 freelist，而 SQLite 不会自动归还文件系统。
# 实测 4.48GB 库删除后 98.8% 的页是空的却不缩容。维护周期内一旦超过任一阈值
# 就做一次有界 VACUUM；阈值定得较高，避免小库每次维护都白跑一遍。
DEFAULT_AUTO_VACUUM_MIN_BYTES = 64 * 1024 * 1024
DEFAULT_AUTO_VACUUM_FREELIST_RATIO = 0.15
LOW_SIGNAL_QUERY_TERMS = frozenset({
    "agent", "agents", "app", "apps", "auto", "chat", "context", "current", "hub", "memory",
    "project", "projects", "task", "tasks", "workspace", "workspaces",
})
LOW_SIGNAL_CJK_QUERY_TERMS = frozenset({
    "任务", "修复", "验收", "完成", "继续", "检查", "测试", "实现",
    "问题", "功能", "方案", "当前", "工作", "记忆", "上下文", "相关",
})
CONTEXT_FIELDS = (
    "goal", "progress", "key_decisions", "related_files", "blockers",
    "next_steps", "preferences",
)
PROCEDURAL_FIELDS = (
    "trigger_pattern", "preconditions", "execution_pipeline",
    "verification_assertion", "calibration_score",
)
STRUCTURED_KINDS = frozenset({
    "task", "goal", "progress", "decision", "blocker", "next_step", "result",
    "semantic", "procedural", "fact", "file", "preference",
})
DEFAULT_AGENT_TRUST = 0.5
QUALITY_ACCEPT_THRESHOLD = 0.35


class LedgerError(ValueError):
    """Raised for malformed records or an unavailable ledger operation."""


@dataclass(frozen=True)
class IngestResult:
    event_id: str
    memory_id: str
    inserted: bool
    content_hash: str


@dataclass(frozen=True)
class AgentCredential:
    """A non-secret view of a scoped local agent credential."""

    token_id: str
    agent_id: str
    scopes: Tuple[str, ...]
    permissions: Tuple[str, ...]
    created_at: str


AGENT_PERMISSIONS = frozenset({
    "read", "ingest", "feedback", "stats", "shared_ingest", "shared_feedback",
    "sync",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_hub_home() -> Path:
    configured = os.environ.get("HIPPOMIND_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "HippoMind"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "").strip()
        return (Path(appdata).expanduser() if appdata else home / "AppData" / "Roaming") / "HippoMind"
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    return (Path(state_home).expanduser() if state_home else home / ".local" / "state") / "hippomind"


def default_ledger_path() -> Path:
    return default_hub_home() / "hippomind.sqlite"


def _hash(*parts: object) -> str:
    value = "\x1f".join(str(part if part is not None else "") for part in parts)
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _text(value: object, limit: int, field: str = "文本") -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if len(value) > limit:
        raise LedgerError(f"{field} 不能超过 {limit} 个字符")
    return value


def _short(value: object, limit: int = 256) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _json(value: object, field: str, maximum: int = MAX_METADATA_CHARS) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False, default=str)
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"{field} 必须可序列化为 JSON") from exc
    if len(encoded) > maximum:
        raise LedgerError(f"{field} 不能超过 {maximum} 个字符")
    return encoded


def _mapping(value: object, field: str = "metadata") -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LedgerError(f"{field} 必须是对象")
    try:
        copied = json.loads(_json(dict(value), field))
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"{field} 无法规范化") from exc
    return copied if isinstance(copied, dict) else {}


def _sanitize_untrusted_metadata(value: Any) -> Any:
    """Recursively scrub strings in raw metadata without changing its shape."""
    if isinstance(value, str):
        return sanitize_chat_content(value, limit=MAX_ARCHIVE_MESSAGE_CHARS)[0]
    if isinstance(value, list):
        return [_sanitize_untrusted_metadata(item) for item in value[:MAX_EVIDENCE]]
    if isinstance(value, Mapping):
        return {str(key)[:128]: _sanitize_untrusted_metadata(item)
                for key, item in list(value.items())[:MAX_TAGS]}
    return value


def _sim_tokens(text: str) -> set:
    """与 hippocampus.token_sim 内嵌 tokens 同语义的多语言分词
    （词元 + CJK 单字 + 相邻 bigram），供评分循环复用查询侧结果。"""
    parts = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", str(text or "").lower())
    out = set(parts)
    cjk = [p for p in parts if len(p) == 1 and "\u3400" <= p <= "\u9fff"]
    out.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return out


def _context_search_text(metadata: Mapping[str, Any]) -> str:
    """Return bounded, already-sanitized structured fields for FTS/ranking."""
    context = metadata.get("context", {}) if isinstance(metadata, Mapping) else {}
    if not isinstance(context, Mapping):
        return ""
    values: List[str] = []
    for field in CONTEXT_FIELDS:
        entries = context.get(field, [])
        if isinstance(entries, str):
            entries = [entries]
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries[:12]:
            text = str(entry).strip()
            if text:
                values.append(text[:4_000])
    return "\n".join(values)[:32_000]


def _fts_text(content: str, metadata: Mapping[str, Any]) -> str:
    context = _context_search_text(metadata)
    return (content + ("\n" + context if context else ""))[:MAX_CONTENT_CHARS]


def _strings(value: object, limit: int, item_limit: int, field: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    try:
        values = list(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise LedgerError(f"{field} 必须是字符串列表") from exc
    out: List[str] = []
    for item in values:
        text = _short(item, item_limit)
        if text and text not in out:
            out.append(text)
    return out[:limit]


def _timestamp(value: object) -> str:
    text = _short(value, 80)
    if not text:
        return utc_now()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError) as exc:
            raise LedgerError("timestamp 必须是 ISO-8601 或 Unix 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _clamp_float(value: object, default: float, lower: float, upper: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        numeric = default
    if not math.isfinite(numeric):
        numeric = default
    return max(lower, min(upper, numeric))


def _clamp_int(value: object, default: int, lower: int, upper: int) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError, OverflowError):
        numeric = default
    return max(lower, min(upper, numeric))


def _safe_kind(value: object) -> str:
    kind = _short(value, 64).lower().replace(" ", "_") or "fact"
    return "".join(ch for ch in kind if ch.isalnum() or ch in {"_", "-"})[:64] or "fact"


def _normalise_context(record: Mapping[str, Any], metadata: Dict[str, Any],
                       kind: str, content: str) -> Dict[str, List[str]]:
    incoming = record.get("context", {})
    if incoming is None:
        incoming = {}
    if not isinstance(incoming, Mapping):
        raise LedgerError("context 必须是对象")
    result: Dict[str, List[str]] = {}
    aliases = {
        "decision": "key_decisions", "decisions": "key_decisions",
        "files": "related_files", "file": "related_files",
        "blocker": "blockers", "next_step": "next_steps",
        "preference": "preferences",
    }
    combined: Dict[str, object] = dict(incoming)
    for key in CONTEXT_FIELDS:
        if key in record:
            combined[key] = record[key]
    for old, new in aliases.items():
        if old in record and new not in combined:
            combined[new] = record[old]
        if old in incoming and new not in combined:
            combined[new] = incoming[old]
    kind_field = {
        "task": "goal", "goal": "goal", "progress": "progress",
        "decision": "key_decisions", "file": "related_files",
        "blocker": "blockers", "next_step": "next_steps",
        "preference": "preferences", "result": "progress",
        "semantic": "key_decisions",
    }.get(kind)
    if kind_field and content and kind_field not in combined:
        combined[kind_field] = content
    for key in CONTEXT_FIELDS:
        if key not in combined:
            continue
        value = combined[key]
        result[key] = _strings(value, 12, 4_000, f"context.{key}")
    if result:
        metadata["context"] = result
    return result


def _normalise_procedural(record: Mapping[str, Any], metadata: Dict[str, Any], kind: str) -> None:
    """Validate a skill card without ever treating its commands as executable."""
    incoming = record.get("procedural", metadata.get("procedural", {}))
    if kind != "procedural" and not incoming:
        return
    if not isinstance(incoming, Mapping):
        raise LedgerError("procedural 必须是对象")

    def values(field: str, limit: int = 16) -> List[str]:
        value = incoming.get(field, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise LedgerError(f"procedural.{field} 必须是字符串列表")
        return _strings(value, limit, 4_000, f"procedural.{field}")

    trigger = values("trigger_pattern")
    preconditions = values("preconditions")
    assertions = values("verification_assertion")
    pipeline_value = incoming.get("execution_pipeline", [])
    if not isinstance(pipeline_value, (list, tuple)):
        raise LedgerError("procedural.execution_pipeline 必须是步骤列表")
    pipeline: List[Dict[str, Any]] = []
    for index, step in enumerate(pipeline_value[:32]):
        if isinstance(step, str):
            pipeline.append({"step": index + 1, "action": _short(step, 4_000),
                             "mode": "declarative"})
        elif isinstance(step, Mapping):
            action = _short(step.get("action", step.get("step", "")), 4_000)
            if not action:
                raise LedgerError("procedural.execution_pipeline 每步必须有 action")
            pipeline.append({"step": index + 1, "action": action,
                             "mode": "declarative",
                             "verify": _short(step.get("verify", ""), 2_000)})
        else:
            raise LedgerError("procedural.execution_pipeline 步骤必须是字符串或对象")
    calibration = incoming.get("calibration_score", {})
    if isinstance(calibration, (int, float)):
        calibration = {"rate": _clamp_float(calibration, 0.0, 0.0, 1.0)}
    if not isinstance(calibration, Mapping):
        raise LedgerError("procedural.calibration_score 必须是对象或 0..1 数字")
    calibration = {
        "attempts": _clamp_int(calibration.get("attempts", 0), 0, 0, 1_000_000),
        "successes": _clamp_int(calibration.get("successes", 0), 0, 0, 1_000_000),
        "rate": _clamp_float(calibration.get("rate", 0.0), 0.0, 0.0, 1.0),
    }
    if calibration["attempts"] and calibration["successes"] > calibration["attempts"]:
        raise LedgerError("procedural.calibration_score.successes 不能超过 attempts")
    if kind == "procedural" and (not trigger or not preconditions or not pipeline or not assertions):
        raise LedgerError("procedural 必须包含 trigger_pattern、preconditions、execution_pipeline 和 verification_assertion")
    metadata["procedural"] = {
        "trigger_pattern": trigger,
        "preconditions": preconditions,
        "execution_pipeline": pipeline,
        "verification_assertion": assertions,
        "calibration_score": calibration,
        "execution_policy": "declarative-only",
    }


def _quality_score(kind: str, content: str, evidence: Sequence[str], metadata: Mapping[str, Any], raw_chat: bool) -> float:
    if raw_chat:
        # Raw chat is evidence, not knowledge.  Scoring it 1.0 unconditionally let
        # harness banners, `/command` echoes and one-word acknowledgements occupy
        # the same retrieval band as a real lesson.  Such rows stay in the ledger
        # for audit, but they must not be treated as accepted working memory.
        #
        # The classifier is deliberately conservative (see textutil.is_noise_text):
        # it only rejects structurally provable noise, so a short but substantive
        # answer is still admitted.
        if is_noise_text(content):
            return 0.15
        return round(0.55 + 0.45 * min(1.0, informative_ratio(content) / 0.55), 4)
    if bool(metadata.get("derived_from_raw_chat")) or bool(metadata.get("untrusted_source")):
        # A marker heuristic is auditable provenance, not independently
        # verified state. It stays queryable through audit APIs but never
        # enters the accepted Context Pack projection by default.
        return 0.15
    score = 0.0
    score += min(0.35, len(content) / 160.0)
    score += 0.25 if evidence else 0.0
    context = metadata.get("context", {}) if isinstance(metadata, Mapping) else {}
    if isinstance(context, Mapping) and any(context.get(field) for field in CONTEXT_FIELDS):
        score += 0.25
    if kind in STRUCTURED_KINDS:
        score += 0.15
    if kind == "procedural" and metadata.get("procedural"):
        score = max(score, 0.8)
    return round(max(0.0, min(1.0, score)), 4)


def _normalise_claims(record: Mapping[str, Any], metadata: Dict[str, Any], *,
                      valid_from: str, asserted_at: str, confidence: float) -> List[Dict[str, Any]]:
    incoming: Any = record.get("claims", metadata.get("claims", []))
    if not incoming and all(key in record for key in ("subject", "predicate", "slot_key", "value")):
        incoming = [{key: record.get(key) for key in ("subject", "predicate", "slot_key", "value")}]
    if incoming is None:
        return []
    if isinstance(incoming, Mapping):
        incoming = [incoming]
    if not isinstance(incoming, (list, tuple)):
        raise LedgerError("claims 必须是对象列表")
    claims: List[Dict[str, Any]] = []
    for claim in incoming[:32]:
        if not isinstance(claim, Mapping):
            raise LedgerError("claims 每项必须是对象")
        subject = _short(claim.get("subject", ""), 256)
        predicate = _short(claim.get("predicate", ""), 256)
        slot_key = _short(claim.get("slot_key", claim.get("slot", "")), 256)
        if not subject or not predicate or not slot_key or "value" not in claim:
            raise LedgerError("claim 必须包含 subject、predicate、slot_key 和 value")
        value = _sanitize_untrusted_metadata(claim.get("value"))
        value_json = _json(value, "claim.value", 8_000)
        claim_from = _timestamp(claim.get("valid_from", valid_from))
        claim_until = _short(claim.get("valid_until", ""), 80)
        if claim_until:
            claim_until = _timestamp(claim_until)
        claim_confidence = _clamp_float(claim.get("confidence", confidence), confidence, 0.0, 1.0)
        claims.append({
            "subject": subject, "predicate": predicate, "slot_key": slot_key,
            "value_json": value_json, "value_hash": _hash(subject, predicate, slot_key, value_json),
            "valid_from": claim_from, "valid_until": claim_until,
            "asserted_at": asserted_at, "confidence": claim_confidence,
        })
    if claims:
        metadata["claims"] = [
            {"subject": item["subject"], "predicate": item["predicate"],
             "slot_key": item["slot_key"], "value": json.loads(item["value_json"]),
             "valid_from": item["valid_from"], "valid_until": item["valid_until"],
             "confidence": item["confidence"]}
            for item in claims
        ]
    return claims


def _normalise_causal_parents(record: Mapping[str, Any], metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    incoming: Any = record.get("causal_parents", record.get("parent_memory_ids", metadata.get("causal_parents", [])))
    if incoming is None:
        return []
    if isinstance(incoming, (str, bytes)):
        incoming = [incoming]
    if not isinstance(incoming, (list, tuple)):
        raise LedgerError("causal_parents 必须是 memory_id 列表或对象列表")
    result: List[Dict[str, Any]] = []
    for parent in incoming[:32]:
        if isinstance(parent, Mapping):
            memory_id = _short(parent.get("memory_id", parent.get("parent_memory_id", "")), 80)
            relation_type = _short(parent.get("relation_type", "caused_by"), 64) or "caused_by"
            confidence = _clamp_float(parent.get("confidence", 0.5), 0.5, 0.0, 1.0)
        else:
            memory_id = _short(parent, 80)
            relation_type = "caused_by"
            confidence = 0.5
        if memory_id:
            result.append({"memory_id": memory_id, "relation_type": relation_type, "confidence": confidence})
    if result:
        metadata["causal_parents"] = result
    return result


class SQLiteLedger:
    """Transactional projection over a durable append-only evidence ledger."""


    # ------------------------------------------------------------------
    # Schema (see hippomind/schema.py)
    # ------------------------------------------------------------------
    # The migration chain used to live here, in a 3,662-line class.  It now
    # lives in a dedicated module so the schema can be reviewed and diffed on
    # its own.  These aliases keep the historical ``SQLiteLedger._migrate_vN``
    # call sites (including tests) working unchanged.
    _migrate_v1 = staticmethod(_schema._migrate_v1)
    _migrate_v2 = staticmethod(_schema._migrate_v2)
    _migrate_v3 = staticmethod(_schema._migrate_v3)
    _migrate_v4 = staticmethod(_schema._migrate_v4)
    _migrate_v5 = staticmethod(_schema._migrate_v5)
    _migrate_v6 = staticmethod(_schema._migrate_v6)
    _migrate_v7 = staticmethod(_schema._migrate_v7)
    _migrate_v8 = staticmethod(_schema._migrate_v8)
    _rebuild_fts = staticmethod(_schema._rebuild_fts)

    def __init__(self, path: Optional[os.PathLike] = None):
        self.path = Path(path or default_ledger_path()).expanduser()
        if self.path.exists() and (not self.path.is_file() or self.path.is_symlink()):
            raise LedgerError(f"账本路径必须是普通文件（不能是目录或符号链接）: {self.path}")
        if not self.path.name or self.path.name in {".", ".."}:
            raise LedgerError("账本路径无效")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local_lock = threading.RLock()
        self._tls = threading.local()
        self._generation = 0
        # search 结果 LRU：键含 generation（任何写事务自增即整体失效）。
        # TTL 只是防御外部进程写者的兜底；深拷贝隔离调用方突变。
        self._search_cache: Dict[Tuple[Any, ...], Tuple[float, List[Dict[str, Any]]]] = {}
        self._search_cache_ttl = 5.0
        # 慢查询哨兵（对齐 mem0 PERFORMANCE_SLOW_QUERY_THRESHOLD 语义）：
        # 最近 N 次 search 延迟环 + 超 2s 计数，经 stats() 暴露 p95。
        self._search_durations: "deque[float]" = deque(maxlen=256)
        self._slow_search_threshold = 2.0
        self._slow_search_count = 0
        self._search_lock = threading.Lock()
        self._bootstrap()

    @property
    def generation(self) -> int:
        """Monotonic in-process write generation for short-lived query caches."""
        return self._generation

    # ------------------------------------------------------------------
    # Connection and migrations
    # ------------------------------------------------------------------

    def _connect(self, write: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
        except sqlite3.DatabaseError:
            pass
        # 长跑 daemon 的读路径调优：更大的页缓存减少重复磁盘读，mmap 让
        # OS 管理热页，临时表驻留内存。均为连接级参数，不影响持久性语义。
        try:
            conn.execute("PRAGMA cache_size=-16000")     # 16 MiB 页缓存
            conn.execute("PRAGMA mmap_size=134217728")   # 128 MiB 内存映射
            conn.execute("PRAGMA temp_store=MEMORY")
        except sqlite3.DatabaseError:
            pass
        if write:
            conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _reader(self) -> sqlite3.Connection:
        """Thread-local cached read connection.

        cProfile on the recall path showed ~15% of wall time spent in
        connect+PRAGMA+close churn (one fresh connection per query). A
        per-thread cached reader removes that overhead. Safety notes:
        - autocommit mode means every statement gets a fresh WAL snapshot,
          so long-lived readers never see stale data after other writers
          commit;
        - ``threading.local`` guarantees the connection is only ever used
          by its creating thread (sqlite3 check_same_thread stays True);
        - write paths still use dedicated short-lived connections via
          ``_transaction``/``_bootstrap``.
        """
        conn = getattr(self._tls, "reader", None)
        if conn is None:
            conn = self._connect()
            self._tls.reader = conn
        return conn

    @staticmethod
    def _copy_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """检索行的定向拷贝：比 copy.deepcopy 快数倍（无递归/memo 开销），
        同时保证调用方后续修改不会污染查询缓存（前景轮九性能优化）。"""
        out: List[Dict[str, Any]] = []
        for row in rows:
            cloned = dict(row)
            for key in ("evidence", "tags"):
                value = cloned.get(key)
                if isinstance(value, list):
                    cloned[key] = list(value)
            value = cloned.get("metadata")
            if isinstance(value, dict):
                cloned["metadata"] = dict(value)
            out.append(cloned)
        return out

    def _search_cached(self, key: Tuple[Any, ...]) -> Optional[List[Dict[str, Any]]]:
        hit = self._search_cache.get(key)
        if hit is None:
            return None
        stamp, rows = hit
        if time.monotonic() - stamp > self._search_cache_ttl:
            self._search_cache.pop(key, None)
            return None
        return self._copy_rows(rows)

    def _search_store(self, key: Tuple[Any, ...], rows: List[Dict[str, Any]]) -> None:
        self._search_cache[key] = (time.monotonic(), self._copy_rows(rows))
        if len(self._search_cache) > 128:
            oldest = min(self._search_cache.items(), key=lambda kv: kv[1][0])[0]
            self._search_cache.pop(oldest, None)

    def close(self) -> None:
        """Close this thread's cached read connection (idempotent)."""
        conn = getattr(self._tls, "reader", None)
        if conn is not None:
            self._tls.reader = None
            try:
                conn.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """Read-only connection for every read path.

        Historically this helper opened and closed a fresh connection per
        call to guarantee deterministic cleanup even when daemon error
        handlers keep exception frames (and tracebacks) alive. The cached
        thread-local reader keeps that determinism — the connection is not
        tied to any with-block lifetime — while eliminating per-query
        connect/close cost. Use :meth:`close` to release it explicitly.
        """
        yield self._reader()

    def maintenance_checkpoint(self) -> Dict[str, Any]:
        """Bounded WAL housekeeping for a long-running daemon.

        PASSIVE auto-checkpoints never shrink the WAL file and can be starved
        indefinitely by overlapping readers; a TRUNCATE checkpoint at a quiet
        batch boundary is the only mode that resets the sidecar to 0 bytes.
        ``PRAGMA optimize`` refreshes query-planner statistics on the same
        low-frequency schedule. Both tolerate busy readers: TRUNCATE simply
        reports busy=1 and the next cycle retries.
        """
        summary = {"wal_truncated": False, "wal_busy": False, "wal_pages": 0,
                   "checkpointed_pages": 0, "optimize": "skipped"}
        try:
            with self._reading() as conn:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row is not None:
                    busy, log_pages, checkpointed = int(row[0]), int(row[1]), int(row[2])
                    summary.update({"wal_busy": bool(busy), "wal_pages": log_pages,
                                    "checkpointed_pages": checkpointed,
                                    "wal_truncated": not busy})
                try:
                    conn.execute("PRAGMA analysis_limit=400")
                    conn.execute("PRAGMA optimize")
                    summary["optimize"] = "ok"
                except sqlite3.DatabaseError:
                    summary["optimize"] = "error"
        except sqlite3.Error as exc:
            summary["optimize"] = f"error: {type(exc).__name__}"
        return summary

    def _bootstrap(self) -> None:
        with self._local_lock:
            conn = self._connect(write=True)
            try:
                # WAL enables readers and independent agent writers to coexist.
                journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(journal).lower() != "wal":
                    raise LedgerError(f"SQLite 未能启用 WAL（当前: {journal}）")
                conn.execute("CREATE TABLE IF NOT EXISTS schema_meta "
                             "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                version = int(row[0]) if row else 0
                if version > SCHEMA_VERSION:
                    raise LedgerError("账本 schema 比当前 HippoMind 更新，拒绝降级写入")
                if version < 1:
                    self._migrate_v1(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(1),))
                    version = 1
                if version < 2:
                    self._migrate_v2(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(2),))
                    version = 2
                if version < 3:
                    self._migrate_v3(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(3),))
                    version = 3
                if version < 4:
                    self._migrate_v4(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(4),))
                    version = 4
                if version < 5:
                    self._migrate_v5(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(5),))
                    version = 5
                if version < 6:
                    self._migrate_v6(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(6),))
                    version = 6
                if version < 7:
                    self._migrate_v7(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(7),))
                    version = 7
                if version < 8:
                    self._migrate_v8(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(8),))
                    version = 8
                if version != SCHEMA_VERSION:
                    raise LedgerError(f"不支持的账本 schema 版本: {version}")
                conn.commit()
            finally:
                conn.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass










    def _transaction(self, callback, *, bump: bool = True):
        last_error: Optional[Exception] = None
        for attempt in range(7):
            with self._local_lock:
                conn = self._connect(write=True)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    value = callback(conn)
                    conn.commit()
                    if bump:
                        self._generation += 1
                    return value
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    last_error = exc
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            time.sleep(0.015 * (2 ** attempt))
        raise LedgerError("账本忙，稍后重试") from last_error

    # ------------------------------------------------------------------
    # Ingest and lifecycle operations
    # ------------------------------------------------------------------

    def _prepare_ingest(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        """Validate one external record before a single-record or batch write."""
        if not isinstance(record, Mapping):
            raise LedgerError("ingest 记录必须是对象")
        content = _text(record.get("content", ""), MAX_CONTENT_CHARS, "content")
        if not content:
            raise LedgerError("content 不能为空")
        kind = _safe_kind(record.get("kind", record.get("event_type", "fact")))
        source_agent = _short(record.get("source_agent", record.get("agent", "unknown")), 128) or "unknown"
        session_id = _short(record.get("session_id", record.get("session", "")), 256)
        task_id = _short(record.get("task_id", record.get("task", "")), 256)
        project = _short(record.get("project", ""), 512)
        role = _short(record.get("role", ""), 64)
        scope = _short(record.get("scope", "shared"), 128) or "shared"
        external_id = _short(record.get("event_id", record.get("external_id", "")), 512)
        timestamp_value = record.get("timestamp", record.get("occurred_at", ""))
        # Archive tables occasionally omit timestamps.  A stable external id is
        # still enough for idempotent replay, so do not hash a fresh ``now`` on
        # every watcher pass in that case.
        occurred_at = (_timestamp(timestamp_value) if timestamp_value else
                       ("1970-01-01T00:00:00+00:00" if external_id else utc_now()))
        evidence = _strings(record.get("evidence", record.get("source_refs", [])),
                            MAX_EVIDENCE, 512, "evidence")
        if not evidence:
            raise LedgerError("每条记忆记录必须至少有一条 evidence/source_ref")
        tags = _strings(record.get("tags", []), MAX_TAGS, 128, "tags")
        raw_chat = bool(record.get("raw_chat", kind in RAW_KINDS))
        if kind in {"tool_output", "system_message"}:
            raw_chat = True
        metadata = _mapping(record.get("metadata", {}))
        # Every inbound path, including raw evidence, crosses the same secret
        # boundary before it reaches either the event ledger or FTS. Raw rows
        # retain their audit role, but no longer act as a delayed token store.
        content, redactions, truncated = sanitize_chat_content(
            content, limit=MAX_ARCHIVE_MESSAGE_CHARS if raw_chat else MAX_CONTENT_CHARS)
        if raw_chat and (redactions or truncated):
            metadata["raw_sanitized"] = {"redactions": redactions, "truncated": bool(truncated)}
        _normalise_context(record, metadata, kind, content)
        _normalise_procedural(record, metadata, kind)
        # Watcher sources already sanitize raw content; this second boundary
        # protects metadata/evidence supplied by legacy importers and direct
        # local callers from becoming a secret side channel. Raw *content* is
        # left for the explicit historical redaction command so old evidence
        # can be audited and repaired deterministically.
        metadata = _sanitize_untrusted_metadata(metadata)
        evidence = [sanitize_chat_content(ref, limit=512)[0] for ref in evidence]
        fts_content = _fts_text(content, metadata)
        now = utc_now()
        # An *explicit* observation time is meaningful identity: the append-only
        # contract says "same record at a new observation time is a new event".
        # A time derived from ``utc_now()`` is capture drift and must not be
        # hashed, or every re-report becomes a new row (the 1.47M-row storm).
        explicit_valid_from = bool(str(record.get("valid_from", "") or "").strip())
        explicit_observed_at = bool(str(timestamp_value or "").strip())
        valid_from = _timestamp(record.get("valid_from", occurred_at))
        valid_until = _short(record.get("valid_until", ""), 80)
        if valid_until:
            valid_until = _timestamp(valid_until)
        importance = _clamp_int(record.get("importance", 5), 5, 1, 10)
        weight = _clamp_float(record.get("weight", 0.5), 0.5, 0.02, 1.0)
        confidence = _clamp_float(record.get("confidence", 0.5), 0.5, 0.0, 1.0)
        utility = _clamp_float(record.get("utility", 0.0), 0.0, -1.0, 1.0)
        claims = _normalise_claims(record, metadata, valid_from=valid_from,
                                   asserted_at=now, confidence=confidence)
        causal_parents = _normalise_causal_parents(record, metadata)
        quality_score = _quality_score(kind, content, evidence, metadata, raw_chat)
        quality_state = "accepted" if quality_score >= QUALITY_ACCEPT_THRESHOLD else "quarantine"
        # ------------------------------------------------------------------
        # Stable identity key
        # ------------------------------------------------------------------
        # The previous canonical form hashed ``occurred_at``, ``valid_from`` and
        # the whole metadata blob.  For raw chat that meant the *same* message
        # re-captured on a later watcher pass produced a different hash every
        # time, so de-duplication never fired: the live ledger held 1,475,584 raw
        # rows whose content_hash were all distinct, while a single session's
        # 70,508 rows contained only 3,573 unique messages.
        #
        # Identity is now split by record class:
        #
        #   * Raw evidence (chat / tool output / system message) is identified by
        #     *what it says*, not when it was captured:
        #         source_agent | session_id | role | normalised content
        #
        #   * Structured records (fact/decision/progress/...) keep a semantic key
        #     that still includes the observation time, because the same sentence
        #     asserted at two different times is legitimately two observations
        #     (bi-temporal history).  Only volatile capture metadata is excluded.
        #
        # ``content_hash`` remains the dedup key; ``canonical`` is retained as
        # the human-auditable description of what was ingested.
        if raw_chat:
            identity = {
                "class": "raw",
                "source_agent": source_agent,
                "session_id": session_id,
                "role": role,
                "content": normalise_for_dedup(content),
            }
        else:
            # ``role`` and ``evidence`` are deliberately absent: an agent that
            # re-reports the same decision with a fresh evidence id is repeating
            # itself, not making a new observation.  Keeping ``valid_from``
            # preserves genuine bitemporal history (same sentence, new time).
            identity = {
                "class": "structured",
                "kind": kind,
                "source_agent": source_agent,
                "session_id": session_id,
                "external_id": external_id,
                "task_id": task_id,
                "project": project,
                "scope": scope,
                # Only an explicitly supplied observation time identifies a
                # distinct bitemporal observation. When it is derived from "now"
                # it is capture drift: hashing it made every re-report a new row.
                "valid_from": (valid_from if (explicit_valid_from or explicit_observed_at) else ""),
                "valid_until": valid_until,
                "content": normalise_for_dedup(content),
            }
        content_hash = _hash(_json(identity, "ingest identity", MAX_CONTENT_CHARS + 8_000))
        event_id = "evt_" + content_hash[:32]
        memory_id = "mem_" + content_hash[:32]
        evidence_json = _json(evidence, "evidence")
        tags_json = _json(tags, "tags")
        metadata_json = _json(metadata, "metadata")

        return {
            "event_id": event_id, "memory_id": memory_id, "kind": kind,
            "occurred_at": occurred_at,
            # 缺时间戳的归档行用固定哨兵保证 content_hash 幂等；可变投影
            # （created_at 驱动保留期、冷置与 recency）必须用真实时间。
            "projection_created_at": (occurred_at if timestamp_value else now),
            "now": now, "source_agent": source_agent,
            "session_id": session_id, "task_id": task_id, "project": project,
            "role": role, "scope": scope, "evidence_json": evidence_json,
            "tags_json": tags_json, "content": content, "content_hash": content_hash,
            "metadata_json": metadata_json, "fts_content": fts_content, "valid_until": valid_until,
            "raw_chat": int(raw_chat), "weight": weight, "importance": importance,
            "utility": utility, "confidence": confidence, "valid_from": valid_from,
            "asserted_at": now, "claims": claims, "causal_parents": causal_parents,
            "quality_score": quality_score, "quality_state": quality_state,
        }

    @staticmethod
    def _agent_trust_for(conn: sqlite3.Connection, source_agent: str) -> float:
        row = conn.execute("SELECT baseline FROM agent_trust WHERE agent_id=?", (source_agent,)).fetchone()
        if row is None:
            return DEFAULT_AGENT_TRUST
        return _clamp_float(row[0], DEFAULT_AGENT_TRUST, 0.0, 1.0)

    @staticmethod
    def _relation_would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
        if parent_id == child_id:
            return True
        row = conn.execute("""
            WITH RECURSIVE ancestors(memory_id) AS (
                SELECT child_memory_id FROM memory_relations WHERE parent_memory_id=?
                UNION
                SELECT r.child_memory_id FROM memory_relations r JOIN ancestors a
                  ON r.parent_memory_id=a.memory_id
            )
            SELECT 1 FROM ancestors WHERE memory_id=? LIMIT 1
        """, (child_id, parent_id)).fetchone()
        return row is not None

    @staticmethod
    def _insert_claims(conn: sqlite3.Connection, prepared: Mapping[str, Any], source_trust: float) -> None:
        for claim in prepared.get("claims", []):
            incoming_score = float(claim["confidence"]) * source_trust
            existing = conn.execute("""
                SELECT c.*,m.lifecycle AS memory_lifecycle,m.quality_state AS memory_quality
                FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id
                WHERE c.scope=? AND c.subject=? AND c.predicate=? AND c.slot_key=?
                  AND c.state='active' AND m.memory_id!=?
                  AND (c.valid_until='' OR c.valid_until>?)
                  AND (?='' OR c.valid_from<?)
                  AND m.lifecycle='active' AND m.quality_state='accepted'
            """, (prepared["scope"], claim["subject"], claim["predicate"], claim["slot_key"],
                  prepared["memory_id"], claim["valid_from"], claim["valid_until"], claim["valid_from"])).fetchall()
            state = "active"
            for old in existing:
                if old["value_hash"] == claim["value_hash"]:
                    continue
                old_score = float(old["confidence"]) * float(old["source_trust"])
                if incoming_score > old_score + 0.05:
                    conn.execute("UPDATE memory_claims SET state='superseded' WHERE claim_id=?", (old["claim_id"],))
                    conn.execute("""UPDATE memories SET lifecycle='superseded',superseded_by=?,quality_state='superseded',updated_at=?
                                    WHERE memory_id=? AND lifecycle='active'""",
                                 (prepared["memory_id"], prepared["asserted_at"], old["memory_id"]))
                    conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (old["memory_id"],))
                elif old_score > incoming_score + 0.05:
                    # 既有高置信事实胜出：保持旧 claim/memory 为 accepted，
                    # 只把低分新断言记为 conflict，避免把获胜方也踢出召回。
                    state = "conflict"
                else:
                    # 分数接近、无法裁决：双方同时下线等待更强证据。
                    state = "conflict"
                    conn.execute("UPDATE memory_claims SET state='conflict' WHERE claim_id=?", (old["claim_id"],))
                    conn.execute("UPDATE memories SET quality_state='conflict',updated_at=? WHERE memory_id=?",
                                 (prepared["asserted_at"], old["memory_id"]))
            claim_id = "cl_" + _hash(prepared["memory_id"], claim["subject"], claim["predicate"], claim["slot_key"])[:32]
            conn.execute("""
                INSERT INTO memory_claims(
                    claim_id,memory_id,scope,subject,predicate,slot_key,value_json,value_hash,
                    valid_from,valid_until,asserted_at,confidence,source_trust,state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (claim_id, prepared["memory_id"], prepared["scope"], claim["subject"], claim["predicate"],
                  claim["slot_key"], claim["value_json"], claim["value_hash"], claim["valid_from"],
                  claim["valid_until"], claim["asserted_at"], claim["confidence"], source_trust, state,
                  prepared["asserted_at"]))
            if state != "active":
                conn.execute("UPDATE memories SET quality_state='conflict',updated_at=? WHERE memory_id=?",
                             (prepared["asserted_at"], prepared["memory_id"]))

    @classmethod
    def _insert_relations(cls, conn: sqlite3.Connection, prepared: Mapping[str, Any]) -> None:
        for parent in prepared.get("causal_parents", []):
            parent_id = str(parent.get("memory_id", ""))
            if not parent_id or conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (parent_id,)).fetchone() is None:
                continue
            if cls._relation_would_cycle(conn, parent_id, prepared["memory_id"]):
                raise LedgerError("causal relation 不能形成环路")
            relation_id = "rel_" + _hash(parent_id, prepared["memory_id"], parent.get("relation_type", "caused_by"))[:32]
            conn.execute("""
                INSERT OR IGNORE INTO memory_relations(
                    relation_id,parent_memory_id,child_memory_id,relation_type,confidence,created_at
                ) VALUES(?,?,?,?,?,?)
            """, (relation_id, parent_id, prepared["memory_id"], parent.get("relation_type", "caused_by"),
                  _clamp_float(parent.get("confidence", 0.5), 0.5, 0.0, 1.0), prepared["asserted_at"]))

    @classmethod
    def _insert_prepared(cls, conn: sqlite3.Connection, prepared: Mapping[str, Any]) -> IngestResult:
        existing = conn.execute(
            "SELECT event_id FROM ledger_events WHERE content_hash=?", (prepared["content_hash"],)
        ).fetchone()
        if existing is not None:
            row = conn.execute(
                "SELECT memory_id FROM memories WHERE origin_event_id=?", (existing["event_id"],)
            ).fetchone()
            return IngestResult(existing["event_id"], row["memory_id"] if row else prepared["memory_id"],
                                False, prepared["content_hash"])
        source_trust = cls._agent_trust_for(conn, str(prepared["source_agent"]))
        effective_confidence = _clamp_float(
            0.7 * float(prepared["confidence"]) + 0.3 * source_trust, 0.5, 0.0, 1.0)
        conn.execute("""
            INSERT INTO ledger_events(
                event_id,event_type,occurred_at,ingested_at,source_agent,session_id,
                task_id,project,role,scope,evidence_json,content,content_hash,metadata_json,asserted_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (prepared["event_id"], prepared["kind"], prepared["occurred_at"], prepared["now"],
              prepared["source_agent"], prepared["session_id"], prepared["task_id"], prepared["project"],
              prepared["role"], prepared["scope"], prepared["evidence_json"], prepared["content"],
              prepared["content_hash"], prepared["metadata_json"], prepared["asserted_at"]))
        conn.execute("""
            INSERT INTO memories(
                memory_id,origin_event_id,kind,content,source_agent,session_id,task_id,
                project,role,scope,evidence_json,tags_json,metadata_json,created_at,updated_at,
                last_weighted_at,valid_from,valid_until,asserted_at,raw_chat,weight,importance,utility,
                confidence,agent_trust,quality_score,quality_state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (prepared["memory_id"], prepared["event_id"], prepared["kind"], prepared["content"],
              prepared["source_agent"], prepared["session_id"], prepared["task_id"], prepared["project"],
              prepared["role"], prepared["scope"], prepared["evidence_json"], prepared["tags_json"],
              prepared["metadata_json"], prepared["projection_created_at"], prepared["now"], prepared["now"],
              prepared["valid_from"], prepared["valid_until"], prepared["asserted_at"], prepared["raw_chat"],
              prepared["weight"], prepared["importance"], prepared["utility"], effective_confidence,
              source_trust, prepared["quality_score"], prepared["quality_state"]))
        cls._insert_claims(conn, prepared, source_trust)
        cls._insert_relations(conn, prepared)
        if prepared["quality_state"] == "accepted":
            conn.execute("INSERT INTO memory_fts(memory_id,content,task_id,project,source_agent,kind) "
                         "VALUES(?,?,?,?,?,?)",
                         (prepared["memory_id"], prepared["fts_content"], prepared["task_id"], prepared["project"],
                          prepared["source_agent"], prepared["kind"]))
        return IngestResult(prepared["event_id"], prepared["memory_id"], True, prepared["content_hash"])

    def ingest(self, record: Mapping[str, Any]) -> IngestResult:
        """Append one immutable source event and create its mutable projection."""
        prepared = self._prepare_ingest(record)
        return self._transaction(lambda conn: self._insert_prepared(conn, prepared))

    def ingest_many(self, records: Iterable[Mapping[str, Any]]) -> List[IngestResult]:
        """Validate and commit a bounded ingestion batch in one SQLite transaction."""
        prepared = [self._prepare_ingest(record) for record in records]
        if not prepared:
            return []
        if len(prepared) > 1_000:
            raise LedgerError("单批最多写入 1000 条记录")
        return self._transaction(lambda conn: [self._insert_prepared(conn, record) for record in prepared])

    def feedback(self, memory_id: str, signal: float, note: str = "",
                 source_agent: str = "", feedback_id: str = "") -> bool:
        memory_id = _short(memory_id, 80)
        if not memory_id:
            raise LedgerError("memory_id 不能为空")
        signal = _clamp_float(signal, 0.0, -1.0, 1.0)
        note = _text(note, 4_000, "反馈说明")
        source_agent = _short(source_agent, 128)
        now = utc_now()
        base = feedback_id or _hash(memory_id, signal, note, source_agent, now)
        feedback_id = "fb_" + _short(base, 128).replace(" ", "_")
        content_hash = _hash("feedback", memory_id, feedback_id, signal, note, source_agent)

        def write(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                raise LedgerError(f"未找到记忆: {memory_id}")
            if conn.execute("SELECT 1 FROM memory_feedback WHERE content_hash=?", (content_hash,)).fetchone():
                return False
            utility = max(-1.0, min(1.0, 0.8 * float(row["utility"]) + 0.2 * signal))
            weight = max(0.02, min(1.0, float(row["weight"]) + 0.16 * signal))
            confidence = max(0.0, min(1.0, float(row["confidence"]) + 0.04 * abs(signal)))
            conn.execute("""
                INSERT INTO memory_feedback(feedback_id,memory_id,signal,note,created_at,source_agent,content_hash)
                VALUES(?,?,?,?,?,?,?)
            """, (feedback_id, memory_id, signal, note, now, source_agent, content_hash))
            conn.execute("""
                UPDATE memories SET utility=?,weight=?,confidence=?,updated_at=?,
                success_count=success_count+?,failure_count=failure_count+? WHERE memory_id=?
            """, (utility, weight, confidence, now, int(signal > 0), int(signal < 0), memory_id))
            event_hash = _hash("feedback-event", content_hash)
            conn.execute("""
                INSERT OR IGNORE INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,task_id,
                    project,role,scope,evidence_json,content,content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ("evt_" + event_hash[:32], "feedback", now, now, source_agent, "", "", "",
                  "feedback", row["scope"], _json(["memory:" + memory_id], "evidence"), note,
                  event_hash, _json({"memory_id": memory_id, "signal": signal}, "metadata")))
            return True

        return self._transaction(write)

    def touch(self, memory_ids: Iterable[str]) -> int:
        ids = sorted({_short(mid, 80) for mid in memory_ids if _short(mid, 80)})[:128]
        if not ids:
            return 0
        now = utc_now()

        def write(conn: sqlite3.Connection) -> int:
            placeholders = ",".join("?" for _ in ids)
            result = conn.execute(
                # Touch is a retrieval signal, not fresh source evidence. Do
                # not reset updated_at/last_weighted_at merely because an old
                # record was recalled; otherwise hot history never decays.
                f"UPDATE memories SET touch_count=touch_count+1,last_access_at=? "
                f"WHERE memory_id IN ({placeholders})", (now, *ids)
            )
            return result.rowcount

        return self._transaction(write, bump=False)

    def supersede(self, old_memory_id: str, new_memory_id: str,
                  source_agent: str = "") -> bool:
        old_memory_id = _short(old_memory_id, 80)
        new_memory_id = _short(new_memory_id, 80)
        if not old_memory_id or not new_memory_id or old_memory_id == new_memory_id:
            raise LedgerError("替代关系需要两个不同的 memory_id")
        now = utc_now()

        def write(conn: sqlite3.Connection) -> bool:
            old = conn.execute("SELECT * FROM memories WHERE memory_id=?", (old_memory_id,)).fetchone()
            new = conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (new_memory_id,)).fetchone()
            if old is None or new is None:
                raise LedgerError("替代关系引用了不存在的记忆")
            if old["superseded_by"] == new_memory_id:
                return False
            conn.execute("UPDATE memories SET superseded_by=?,lifecycle='superseded',updated_at=? "
                         "WHERE memory_id=?", (new_memory_id, now, old_memory_id))
            conn.execute("UPDATE memory_claims SET state='superseded' WHERE memory_id=? AND state='active'",
                         (old_memory_id,))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (old_memory_id,))
            digest = _hash("supersede", old_memory_id, new_memory_id)
            conn.execute("""
                INSERT OR IGNORE INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,task_id,
                    project,role,scope,evidence_json,content,content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ("evt_" + digest[:32], "supersede", now, now, _short(source_agent, 128), "",
                  old["task_id"], old["project"], "system", old["scope"],
                  _json(["memory:" + old_memory_id, "memory:" + new_memory_id], "evidence"),
                  "", digest, _json({"old_memory_id": old_memory_id, "new_memory_id": new_memory_id}, "metadata")))
            return True

        return self._transaction(write)

    def list_facts(self, *, scope: str = "shared", subject: str = "",
                   limit: int = 200) -> List[Dict[str, Any]]:
        """事实投影：活跃 claim 的结构化视图（subject/predicate/slot/value）。"""
        scope = _short(scope, 128) or "shared"
        subject = _short(subject, 256)
        limit = _clamp_int(limit, 200, 1, 2_000)
        clauses = ["c.scope=?", "c.state='active'",
                   "m.lifecycle='active'", "m.quality_state='accepted'"]
        params: List[object] = [scope]
        if subject:
            clauses.append("c.subject=?")
            params.append(subject)
        with self._reading() as conn:
            rows = conn.execute(f"""
                SELECT c.claim_id,c.subject,c.predicate,c.slot_key,c.value_json,
                       c.confidence,c.source_trust,c.asserted_at,
                       m.memory_id,m.content,m.source_agent
                FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id
                WHERE {' AND '.join(clauses)}
                ORDER BY c.asserted_at DESC LIMIT ?
            """, (*params, limit)).fetchall()
        facts: List[Dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["value_json"])
            except (TypeError, ValueError):
                value = None
            facts.append({
                "claim_id": row["claim_id"], "subject": row["subject"],
                "predicate": row["predicate"], "slot_key": row["slot_key"],
                "value": value, "confidence": float(row["confidence"]),
                "source_trust": float(row["source_trust"]),
                "asserted_at": row["asserted_at"],
                "memory_id": row["memory_id"], "content": row["content"],
                "source_agent": row["source_agent"],
            })
        return facts

    def list_skills(self, *, scope: str = "shared",
                    limit: int = 100) -> List[Dict[str, Any]]:
        """技能投影：procedural 记忆的结构化视图（触发/前置/校验/步骤）。"""
        scope = _short(scope, 128) or "shared"
        limit = _clamp_int(limit, 100, 1, 1_000)
        skills: List[Dict[str, Any]] = []
        with self._reading() as conn:
            rows = conn.execute("""
                SELECT memory_id,content,metadata_json,confidence,weight,
                       source_agent,created_at
                FROM memories
                WHERE kind='procedural' AND scope=? AND lifecycle='active'
                  AND quality_state='accepted'
                ORDER BY updated_at DESC LIMIT ?
            """, (scope, limit)).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            procedural = (metadata.get("procedural")
                          if isinstance(metadata, Mapping) else {})
            procedural = procedural if isinstance(procedural, Mapping) else {}
            skills.append({
                "memory_id": row["memory_id"], "content": row["content"],
                "trigger_pattern": list(procedural.get("trigger_pattern", [])),
                "preconditions": list(procedural.get("preconditions", [])),
                "verification_assertion": list(procedural.get("verification_assertion", [])),
                "execution_pipeline": list(procedural.get("execution_pipeline", [])),
                "confidence": float(row["confidence"]),
                "weight": float(row["weight"]),
                "source_agent": row["source_agent"],
                "created_at": row["created_at"],
            })
        return skills

    def forget(self, *, scope: str = "", before_time: str = "",
               memory_ids: Optional[Sequence[str]] = None,
               source_agent: str = "local-admin",
               purge: bool = False,
               import_batch: str = "",
               dry_run: bool = False) -> Dict[str, Any]:
        """遗忘请求：移除记忆投影（软遗忘）或连同事件内容一并清除（purge）。

        软遗忘：删除 memories 及其 claims/relations/feedback/FTS 行；
        ``ledger_events`` 保留（append-only 审计），并追加一条不含内容的
        forget 审计事件。``purge=True`` 同时删除对应 ledger_events 行——
        真正的内容擦除，需显式调用。至少提供 scope/memory_ids/import_batch
        之一。``import_batch`` 按导入批次回滚（迭代 56 的批次戳）。
        ``dry_run=True`` 只预览将被遗忘的记忆清单与影响面，不执行删除、
        不写审计事件——危险操作前的标准确认步骤。
        """
        scope = _short(scope, 128)
        before_iso = _timestamp(before_time) if before_time else ""
        ids = [_short(value, 80) for value in (memory_ids or []) if _short(value, 80)]
        batch = _short(import_batch, 160)
        if not scope and not ids and not batch:
            raise LedgerError("遗忘请求需要 --scope/--memory-id/--batch 之一")
        if batch:
            with self._reading() as conn:
                rows = conn.execute(
                    "SELECT memory_id FROM memories WHERE metadata_json LIKE ?",
                    (f'%{{"import_batch": "{batch}"%',)).fetchone()
                # LIKE 只能做前缀匹配兜底；精确匹配走 json_extract。
                rows = conn.execute(
                    "SELECT memory_id FROM memories WHERE "
                    "json_extract(metadata_json, '$.import_batch') = ?",
                    (batch,)).fetchall()
            ids = sorted({*(ids or []), *(r["memory_id"] for r in rows)})
            if not ids:
                return {"forgotten": 0, "batch": batch,
                        "note": "该批次没有可回滚的记忆"}
        now = utc_now()

        if dry_run:
            # 预览走只读连接：不进事务、不 bump generation、不写审计。
            d_clauses: List[str] = []
            d_params: List[object] = []
            if scope:
                d_clauses.append("scope=?")
                d_params.append(scope)
            if before_iso:
                d_clauses.append("created_at<=?")
                d_params.append(before_iso)
            if ids:
                d_clauses.append("memory_id IN (" +
                                 ",".join("?" for _ in ids) + ")")
                d_params.extend(ids)
            with self._reading() as conn:
                rows = conn.execute(
                    "SELECT memory_id,created_at,scope FROM memories WHERE " +
                    (" AND ".join(d_clauses) if d_clauses else "1=1"),
                    d_params).fetchall()
            created = sorted(row["created_at"] for row in rows
                             if row["created_at"])
            scopes: Dict[str, int] = {}
            for row in rows:
                scopes[row["scope"] or "shared"] = \
                    scopes.get(row["scope"] or "shared", 0) + 1
            return {"dry_run": True, "would_forget": len(rows), "purged": 0,
                    "time_from": created[0] if created else "",
                    "time_to": created[-1] if created else "",
                    "scopes": scopes,
                    "preview": [{"memory_id": row["memory_id"],
                                 "created_at": row["created_at"],
                                 "scope": row["scope"]} for row in rows[:50]],
                    "note": ("预览仅前 50 条；去掉 --dry-run 执行"
                             if len(rows) > 50 else "")}

        def write(conn: sqlite3.Connection) -> Dict[str, Any]:
            clauses: List[str] = []
            params: List[object] = []
            if scope:
                clauses.append("scope=?")
                params.append(scope)
            if before_iso:
                clauses.append("created_at<=?")
                params.append(before_iso)
            if ids:
                clauses.append("memory_id IN (" + ",".join("?" for _ in ids) + ")")
                params.extend(ids)
            targets = conn.execute(
                "SELECT memory_id,origin_event_id,created_at,scope FROM memories WHERE " +
                " AND ".join(clauses), params).fetchall()
            if not targets:
                return {"forgotten": 0, "purged": int(purge)}
            # 影响面摘要（迭代 81）：时间窗与 scope 分布，供审计报表。
            created = sorted(row["created_at"] for row in targets if row["created_at"])
            scope_counts: Dict[str, int] = {}
            for row in targets:
                scope_counts[row["scope"] or "shared"] = \
                    scope_counts.get(row["scope"] or "shared", 0) + 1
            impact = {"time_from": created[0] if created else "",
                      "time_to": created[-1] if created else "",
                      "scopes": scope_counts}
            target_ids = [row["memory_id"] for row in targets]
            event_ids = [row["origin_event_id"] for row in targets]
            id_placeholders = ",".join("?" for _ in target_ids)
            # 子表先行（foreign_keys=ON，引用必须先清）
            conn.execute(f"DELETE FROM memory_claims WHERE memory_id IN ({id_placeholders})",
                         target_ids)
            conn.execute(f"DELETE FROM memory_relations WHERE parent_memory_id IN ({id_placeholders}) "
                         f"OR child_memory_id IN ({id_placeholders})", (*target_ids, *target_ids))
            conn.execute(f"DELETE FROM memory_feedback WHERE memory_id IN ({id_placeholders})",
                         target_ids)
            conn.execute(f"DELETE FROM memory_fts WHERE memory_id IN ({id_placeholders})",
                         target_ids)
            conn.execute(f"DELETE FROM memories WHERE memory_id IN ({id_placeholders})",
                         target_ids)
            purged_events = 0
            if purge and event_ids:
                evt_placeholders = ",".join("?" for _ in event_ids)
                cur = conn.execute(
                    f"DELETE FROM ledger_events WHERE event_id IN ({evt_placeholders})",
                    event_ids)
                purged_events = cur.rowcount
            digest = _hash("forget", scope, before_iso, len(target_ids), purge, now)
            result_payload = {"forgotten": len(target_ids),
                              "purged": int(purged_events) if purge else int(purge)}
            conn.execute("""
                INSERT OR IGNORE INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,
                    task_id,project,role,scope,evidence_json,content,content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ("evt_" + digest[:32], "forget", now, now,
                  _short(source_agent, 128), "", "", "", "system", scope or "shared",
                  _json([], "evidence"), "", digest,
                  _json({"count": len(target_ids), "scope": scope,
                         "before": before_iso, "purge": bool(purge),
                         "purged_events": purged_events}, "metadata")))
            return {"forgotten": len(target_ids), "purged": int(purge),
                    "purged_events": purged_events, "impact": impact}

        return self._transaction(write)

    def resolve_claim(self, claim_id: str, winner_memory_id: str,
                      *, source_agent: str = "local-admin") -> Dict[str, Any]:
        """人工裁决 Slot-Value 冲突：指定胜者记忆，其余让位（谱系保留）。

        ``claim_id`` 只用于定位冲突组（同 scope/subject/predicate/slot_key）；
        被激活的是**胜者记忆**在该组内的全部 claim，而非传入的这条 claim。
        组查询不限状态，因此可以对历史裁决再次翻案。
        败者：组内 claim 全部转 superseded；当其不再持有任何 active claim 时，
        memory 置 superseded 并指向胜者，同时移出 FTS。
        """
        claim_id = _short(claim_id, 80)
        winner_memory_id = _short(winner_memory_id, 80)
        if not claim_id or not winner_memory_id:
            raise LedgerError("裁决需要 claim_id 与 winner_memory_id")
        now = utc_now()

        def write(conn: sqlite3.Connection) -> Dict[str, Any]:
            claim = conn.execute("SELECT * FROM memory_claims WHERE claim_id=?",
                                 (claim_id,)).fetchone()
            if claim is None:
                raise LedgerError(f"未找到 claim: {claim_id}")
            winner = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                                  (winner_memory_id,)).fetchone()
            if winner is None:
                raise LedgerError("未找到胜者记忆")
            if winner["scope"] != claim["scope"]:
                raise LedgerError("胜者与 claim 的 scope 不一致")
            slot_params = (claim["scope"], claim["subject"],
                           claim["predicate"], claim["slot_key"])
            slot_c = "c.scope=? AND c.subject=? AND c.predicate=? AND c.slot_key=?"
            slot_m = "scope=? AND subject=? AND predicate=? AND slot_key=?"
            group_rows = conn.execute(f"""
                SELECT DISTINCT c.memory_id FROM memory_claims c
                WHERE {slot_c}
            """, slot_params).fetchall()
            group_ids = {row["memory_id"] for row in group_rows}
            if winner_memory_id not in group_ids:
                raise LedgerError("胜者不在该冲突组内")
            loser_ids = sorted(group_ids - {winner_memory_id})

            # 胜者：组内全部 claim 激活、memory 恢复 accepted（缺失时补回 FTS）
            conn.execute(f"""
                UPDATE memory_claims SET state='active'
                WHERE memory_id=? AND state!='active' AND {slot_m}
            """, (winner_memory_id, *slot_params))
            conn.execute("UPDATE memories SET quality_state='accepted',updated_at=? "
                         "WHERE memory_id=?", (now, winner_memory_id))
            has_fts = conn.execute("SELECT 1 FROM memory_fts WHERE memory_id=?",
                                   (winner_memory_id,)).fetchone()
            if has_fts is None:
                fts_text = _fts_text(winner["content"],
                                     json.loads(winner["metadata_json"] or "{}"))
                conn.execute(
                    "INSERT INTO memory_fts(memory_id,content,task_id,project,"
                    "source_agent,kind) VALUES(?,?,?,?,?,?)",
                    (winner_memory_id, fts_text, winner["task_id"],
                     winner["project"], winner["source_agent"], winner["kind"]))
            # 败者：仅让位该槽位的 claim；不再持有任何 active 时记忆整体下线
            for mid in loser_ids:
                conn.execute(f"""
                    UPDATE memory_claims SET state='superseded'
                    WHERE memory_id=? AND state!='superseded' AND {slot_m}
                """, (mid, *slot_params))
                active_left = conn.execute(f"""
                    SELECT count(*) FROM memory_claims c
                    WHERE c.memory_id=? AND c.state='active' AND {slot_c}
                """, (mid, *slot_params)).fetchone()[0]
                if int(active_left) == 0:
                    conn.execute(
                        "UPDATE memories SET quality_state='superseded',"
                        "lifecycle='superseded',superseded_by=?,updated_at=? "
                        "WHERE memory_id=?", (winner_memory_id, now, mid))
                    conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (mid,))
            digest = _hash("resolve", claim_id, winner_memory_id)
            conn.execute("""
                INSERT OR IGNORE INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,
                    task_id,project,role,scope,evidence_json,content,content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ("evt_" + digest[:32], "resolve", now, now,
                  _short(source_agent, 128), "", winner["task_id"],
                  winner["project"], "system", winner["scope"],
                  _json(["claim:" + claim_id], "evidence"), "", digest,
                  _json({"claim_id": claim_id, "winner": winner_memory_id,
                         "losers": loser_ids}, "metadata")))
            return {"resolved": claim_id, "winner": winner_memory_id,
                    "losers": loser_ids}

        return self._transaction(write)

    def export_rows(self, *, scope: str = "shared", include_raw_chat: bool = False,
                    limit: int = 0, since: str = "") -> List[Dict[str, Any]]:
        """导出可再导入的记忆记录（ingest 兼容格式，content_hash 幂等）。

        默认只导 accepted 且非 raw 的行；raw chat 是不可信审计证据，
        需要显式 ``include_raw_chat=True`` 才随包导出。
        ``since`` 只导该 ISO 时刻之后创建的记忆（增量导出）。
        """
        scope = _short(scope, 128) or "shared"
        limit = _clamp_int(limit, 0, 0, 1_000_000)
        clauses = ["m.scope=?", "m.quality_state='accepted'",
                   "m.lifecycle IN ('active','cold')",
                   "(m.valid_until='' OR m.valid_until>?)"]
        params: List[object] = [scope, utc_now()]
        since_iso = _timestamp(since) if since else ""
        if since_iso:
            clauses.append("m.created_at>?")
            params.append(since_iso)
        if not include_raw_chat:
            clauses.append("m.raw_chat=0")
        sql = ("SELECT m.*, e.occurred_at FROM memories m "
               "JOIN ledger_events e ON e.event_id=m.origin_event_id "
               "WHERE " + " AND ".join(clauses) +
               " ORDER BY m.created_at ASC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows: List[Dict[str, Any]] = []
        with self._reading() as conn:
            for row in conn.execute(sql, params):
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                try:
                    evidence = json.loads(row["evidence_json"] or "[]")
                except (TypeError, ValueError):
                    evidence = []
                try:
                    tags = json.loads(row["tags_json"] or "[]")
                except (TypeError, ValueError):
                    tags = []
                record: Dict[str, Any] = {
                    "kind": row["kind"], "content": row["content"],
                    "source_agent": row["source_agent"],
                    "session_id": row["session_id"], "task_id": row["task_id"],
                    "project": row["project"], "role": row["role"],
                    "scope": row["scope"], "evidence": evidence,
                    "timestamp": row["occurred_at"],
                    "importance": int(row["importance"]),
                    "confidence": float(row["confidence"]),
                }
                if tags:
                    record["tags"] = tags
                context = metadata.get("context") if isinstance(metadata, Mapping) else None
                if isinstance(context, Mapping) and context:
                    record["context"] = context
                claims = metadata.get("claims") if isinstance(metadata, Mapping) else None
                if isinstance(claims, list) and claims:
                    first = claims[0]
                    if isinstance(first, Mapping):
                        for key in ("subject", "predicate", "slot_key"):
                            if first.get(key):
                                record[key] = str(first[key])[:256]
                        if "value" in first:
                            record["value"] = first["value"]
                        if first.get("confidence") is not None:
                            record["confidence"] = max(record["confidence"],
                                                       float(first["confidence"]))
                if row["valid_from"]:
                    record["valid_from"] = row["valid_from"]
                if row["valid_until"]:
                    record["valid_until"] = row["valid_until"]
                rows.append(record)
        return rows

    def maintain(self, active_limit: int = 2_000, cold_after_days: int = 90,
                 decay_per_day: float = 0.008, raw_active_limit: int = DEFAULT_RAW_CAPACITY,
                 raw_after_days: int = DEFAULT_RAW_RETENTION_DAYS, *,
                 raw_ttl_days: Optional[int] = None,
                 purge_raw: bool = False,
                 raw_capacity: Optional[int] = None,
                 raw_archive_dir: Optional[os.PathLike] = None,
                 raw_archive_max_bytes: int = DEFAULT_RAW_ARCHIVE_MAX_BYTES,
                 archive_raw: bool = True,
                 auto_vacuum_min_bytes: int = DEFAULT_AUTO_VACUUM_MIN_BYTES,
                 events_keep: int = 100_000) -> Dict[str, Any]:
        """Decay projections, expire facts and cold-archive low-value rows.

        It intentionally only changes ``memories``.  The corresponding entries
        in ``ledger_events`` remain durable audit evidence.
        """
        active_limit = _clamp_int(active_limit, 2_000, 32, 100_000)
        cold_after_days = _clamp_int(cold_after_days, 90, 1, 36500)
        decay_per_day = _clamp_float(decay_per_day, 0.008, 0.0, 1.0)
        raw_active_limit = _clamp_int(raw_active_limit, DEFAULT_RAW_CAPACITY, 100, 1_000_000)
        raw_after_days = _clamp_int(raw_after_days, DEFAULT_RAW_RETENTION_DAYS, 1, 36500)
        # ``raw_ttl_days`` is the *deletion* horizon and is intentionally separate
        # from ``raw_after_days`` (the point at which a raw row stops being hot).
        # Before this existed, "retention" only demoted rows to cold and the file
        # grew without bound: 1.47M raw messages, 4.48GB, on a 2-month-old ledger.
        #
        # ``purge_raw`` defaults to False: demoting to ``cold`` keeps a row
        # auditable through ``search(include_cold=True)``, which is the documented
        # storage contract and what existing callers assert.  Long-running
        # deployments that must bound file growth (the daemon) opt in explicitly.
        raw_ttl_days = _clamp_int(
            raw_ttl_days if raw_ttl_days is not None else max(raw_after_days, 90),
            max(raw_after_days, 90), 1, 36500)
        if raw_capacity is not None:
            # ``raw_capacity`` is the public policy name; retain the older
            # ``raw_active_limit`` argument for callers of the v0.7 API.
            raw_active_limit = _clamp_int(raw_capacity, raw_active_limit, 1, 1_000_000)
        raw_archive_max_bytes = _clamp_int(raw_archive_max_bytes,
                                           DEFAULT_RAW_ARCHIVE_MAX_BYTES, 1 << 20,
                                           2 * 1024 * 1024 * 1024)
        now = utc_now()
        now_epoch = _epoch(now)

        def write(conn: sqlite3.Connection) -> Dict[str, int]:
            # Upgrade cleanup: older releases kept already-cold projections in
            # FTS. Remove them idempotently on every maintenance pass so a
            # long-running ledger converges without requiring a full rebuild.
            legacy_cold_fts = int(conn.execute("""
                SELECT count(*) FROM memory_fts f JOIN memories m ON m.memory_id=f.memory_id
                WHERE m.lifecycle!='active'
            """).fetchone()[0])
            conn.execute("""
                DELETE FROM memory_fts WHERE memory_id IN (
                    SELECT memory_id FROM memories WHERE lifecycle!='active'
                )
            """)
            # 审计事件压缩归档：主表超 events_keep 时把最旧行移入侧表。
            # ledger_events 仍是审计事实来源——侧表同构保留全部列，可查证。
            events_archived = 0
            keep = _clamp_int(events_keep, 100_000, 100, 10_000_000)
            # 以「无投影锚定的孤儿审计行」为治理对象：有投影的事件被外键
            # 钉住属永久居民，不计入阈值（否则永远超限、幂等性破坏）。
            orphan_events = int(conn.execute(
                "SELECT COUNT(*) FROM ledger_events WHERE event_id NOT IN "
                "(SELECT origin_event_id FROM memories)").fetchone()[0])
            total_events = orphan_events
            if orphan_events > keep:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ledger_events_archive (
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        ingested_at TEXT NOT NULL,
                        source_agent TEXT NOT NULL,
                        session_id TEXT NOT NULL DEFAULT '',
                        task_id TEXT NOT NULL DEFAULT '',
                        project TEXT NOT NULL DEFAULT '',
                        role TEXT NOT NULL DEFAULT '',
                        scope TEXT NOT NULL,
                        evidence_json TEXT NOT NULL DEFAULT '[]',
                        content TEXT NOT NULL,
                        content_hash TEXT NOT NULL UNIQUE,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        asserted_at TEXT NOT NULL DEFAULT ''
                    )
                """)
                # 只归档无投影锚定的纯审计事件（feedback/pin/unpin/forget 等）：
                # 有记忆投影的事件被 memories.origin_event_id 外键钉住，必须
                # 留在主表以维持引用完整性。
                overflow = min(total_events - keep, 50_000)
                conn.execute(f"""
                    INSERT OR IGNORE INTO ledger_events_archive
                    SELECT * FROM ledger_events
                    WHERE event_id IN (
                        SELECT event_id FROM ledger_events
                        WHERE event_id NOT IN (
                            SELECT origin_event_id FROM memories
                        )
                        ORDER BY occurred_at ASC LIMIT {int(overflow)}
                    )
                """)
                events_archived = conn.execute("SELECT changes()").fetchone()[0]
                if events_archived:
                    conn.execute("""
                        DELETE FROM ledger_events WHERE event_id NOT IN (
                            SELECT origin_event_id FROM memories
                        ) AND event_id IN (
                            SELECT event_id FROM ledger_events_archive
                        )
                    """)
            protected_parents = {
                str(row[0]) for row in conn.execute("""
                    SELECT DISTINCT r.parent_memory_id
                    FROM memory_relations r JOIN memories child ON child.memory_id=r.child_memory_id
                    WHERE child.lifecycle='active'
                """).fetchall()
            }
            # 增量维护：只取「距上次加权超过重加权阈值」「有效期即将到来/
            # 已到」或「源年龄已越过冷置保留线」的行。刚维护过的行其状态
            # 条件以天为单位变化，一小时内不可能翻转；这样每个 tick 的扫描
            # 量与活跃库大小解耦，长跑不卡顿。
            reweight_cutoff = datetime.fromtimestamp(
                now_epoch - 3_600.0, tz=timezone.utc).isoformat()
            expiry_horizon = datetime.fromtimestamp(
                now_epoch + 3_600.0, tz=timezone.utc).isoformat()
            age_horizon = datetime.fromtimestamp(
                now_epoch - min(cold_after_days, raw_after_days) * 86_400.0,
                tz=timezone.utc).isoformat()
            rows = conn.execute("""
                SELECT memory_id,weight,last_weighted_at,valid_until,created_at,raw_chat
                FROM memories
                WHERE lifecycle='active'
                  AND (last_weighted_at<=? OR (valid_until!='' AND valid_until<=?)
                       OR created_at<=?)
                ORDER BY last_weighted_at ASC
                LIMIT 8000
            """, (reweight_cutoff, expiry_horizon, age_horizon)).fetchall()
            decayed = expired = archived = raw_archived = 0
            for row in rows:
                lifecycle = "active"
                weight = float(row["weight"])
                last = _epoch(row["last_weighted_at"]) or now_epoch
                age_days = max(0.0, now_epoch - last) / 86400.0
                # Watcher ticks are normally seconds/minutes apart. Rewriting
                # every row for a numerically invisible decay wastes IO and
                # defeats the intended event-driven idle profile.
                reweight = age_days >= (1.0 / 24.0)
                next_weight = (max(0.02, min(1.0, weight * math.exp(-decay_per_day * age_days)))
                               if reweight else weight)
                valid_until = _epoch(row["valid_until"])
                created = _epoch(row["created_at"])
                if valid_until and valid_until <= now_epoch:
                    lifecycle = "expired"
                    expired += 1
                elif row["raw_chat"]:
                    # Chat transcripts are immutable evidence, not permanent
                    # hot working memory. Keep them auditable in the ledger,
                    # but move old projections out of the active/FTS path.
                    if created and now_epoch - created > raw_after_days * 86400:
                        lifecycle = "cold"
                        raw_archived += 1
                elif row["memory_id"] in protected_parents:
                    # A live child keeps its causal decision/progress parents
                    # available even when their standalone activation decays.
                    lifecycle = "active"
                elif next_weight < 0.12 or (created and now_epoch - created > cold_after_days * 86400):
                    lifecycle = "cold"
                    archived += 1
                if (reweight and abs(next_weight - weight) > 1e-9) or lifecycle != "active":
                    conn.execute("UPDATE memories SET weight=?,last_weighted_at=?,lifecycle=?,"
                                 "archived_at=CASE WHEN ?='cold' THEN ? ELSE archived_at END,updated_at=? "
                                 "WHERE memory_id=?",
                                 (next_weight, now, lifecycle, lifecycle, now, now, row["memory_id"]))
                    if lifecycle == "cold":
                        # Cold projections remain auditable in SQLite, but must
                        # leave the hot FTS index. Search keeps an explicit,
                        # bounded LIKE fallback for cold/audit requests, so
                        # removing the index row cannot hide evidence while it
                        # prevents long-running maintenance from growing FTS
                        # without bound.
                        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
                    decayed += 1
            active_rows = conn.execute("""
                SELECT memory_id FROM memories WHERE lifecycle='active' AND raw_chat=0
                    AND quality_state='accepted'
                ORDER BY (weight * (0.5 + importance / 20.0) + utility * 0.15) ASC, created_at ASC
            """).fetchall()
            eligible_rows = [row for row in active_rows if str(row["memory_id"]) not in protected_parents]
            overflow = max(0, len(eligible_rows) - active_limit)
            for row in eligible_rows[:overflow]:
                conn.execute("UPDATE memories SET lifecycle='cold',archived_at=?,updated_at=? WHERE memory_id=?",
                             (now, now, row["memory_id"]))
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
            archived += overflow
            raw_rows = conn.execute("""
                SELECT memory_id FROM memories WHERE lifecycle='active' AND raw_chat=1
                ORDER BY created_at ASC
            """).fetchall()
            raw_overflow = max(0, len(raw_rows) - raw_active_limit)
            for row in raw_rows[:raw_overflow]:
                conn.execute("UPDATE memories SET lifecycle='cold',archived_at=?,updated_at=? WHERE memory_id=?",
                             (now, now, row["memory_id"]))
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
            raw_archived += raw_overflow
            return {"decayed": decayed, "expired": expired, "archived": archived,
                    "raw_archived": raw_archived, "active_limit": active_limit,
                    "raw_active_limit": raw_active_limit,
                    "fts_pruned": legacy_cold_fts,
                    "events_archived": events_archived}

        result = self._transaction(write)
        # Compression is deliberately outside the SQLite transaction. A slow
        # filesystem or a denied TCC path must not hold the WAL write lock;
        # evidence remains searchable in the ledger and the next maintenance
        # cycle retries the archive operation. Background callers can defer
        # this more expensive filesystem pass while retaining normal decay.
        if archive_raw:
            try:
                result.update(self.archive_raw_evidence(
                    before_days=raw_after_days, archive_dir=raw_archive_dir,
                    max_bytes=raw_archive_max_bytes,
                ))
            except (OSError, sqlite3.Error, LedgerError) as exc:
                result.update({"raw_compressed": 0, "raw_archive_bytes": 0,
                               "raw_archive_error": str(exc)[:300]})
        else:
            result.update({"raw_compressed": 0, "raw_archive_deferred": True})
        # Second pass: physically drop raw evidence past the deletion horizon.
        # Runs outside the write transaction (file IO must not hold the WAL lock)
        # and is fail-closed: nothing is deleted unless the archive verified.
        if purge_raw:
            try:
                result["raw_purged"] = self.purge_expired_raw(
                    before_days=raw_ttl_days,
                    archive_dir=raw_archive_dir,
                    max_bytes=raw_archive_max_bytes,
                )
            except (OSError, sqlite3.Error, LedgerError) as exc:
                result["raw_purged"] = {"purged": 0, "error": str(exc)[:300]}
        else:
            result["raw_purged"] = {"purged": 0, "deferred": True}
        # Reclaim file space when a large deletion just happened.  Skipping this
        # left 98.8% of a 754MB file as empty pages in the earlier audit: SQLite
        # moves freed pages to the freelist but never returns them to the OS
        # unless VACUUM runs, so a ledger could delete 1.4M rows and stay 4.48GB.
        result["storage"] = self._storage_report()
        result["auto_vacuum"] = self._maybe_auto_vacuum(
            result["storage"],
            min_reclaimable_bytes=_clamp_int(
                auto_vacuum_min_bytes, DEFAULT_AUTO_VACUUM_MIN_BYTES, 0, 64 * 1024 * 1024 * 1024),
            # Only the tier that can actually free pages is allowed to reclaim
            # them.  A full VACUUM is a whole-file rewrite; running it on a
            # maintenance tick that deleted nothing rewrites a multi-GB file for
            # no reason and starves concurrent readers.  ``purge_raw`` is the
            # delete-capable pass, so it owns the shrink.
            allow=bool(purge_raw))
        if result["auto_vacuum"].get("vacuumed"):
            # Re-measure: the point of the pass was to change these numbers.
            result["storage"] = self._storage_report()
        result["raw_retention_days"] = raw_after_days
        result["raw_ttl_days"] = raw_ttl_days
        result["raw_capacity"] = raw_active_limit
        return result

    def _storage_report(self) -> Dict[str, int]:
        """Page/freelist accounting used to decide whether shrinking is worth it."""
        conn = self._connect()
        try:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        return {"page_size": page_size, "page_count": page_count,
                "freelist": freelist, "auto_vacuum": auto_vacuum,
                "db_bytes": page_size * page_count,
                "reclaimable_bytes": page_size * freelist}

    def _maybe_auto_vacuum(self, storage: Mapping[str, Any], *,
                           min_reclaimable_bytes: int = DEFAULT_AUTO_VACUUM_MIN_BYTES,
                           freelist_ratio: float = DEFAULT_AUTO_VACUUM_FREELIST_RATIO,
                           allow: bool = True,
                           dry_run: bool = False) -> Dict[str, Any]:
        """Shrink the database file when deleted pages have piled up.

        A retention policy that deletes rows but never returns the pages is only
        half a policy: the ledger keeps its 4.48GB footprint while holding a
        fraction of the data.  This runs at most one shrink per maintenance pass,
        and only past a threshold, so small ledgers do not pay a full table copy
        on every tick.

        Two mechanisms, picked by schema state:

        * ``auto_vacuum=FULL/INCREMENTAL`` databases can return pages with a
          bounded ``PRAGMA incremental_vacuum`` — no full rewrite, safe to run
          often.  Turning ``auto_vacuum`` ON for an existing database requires a
          full VACUUM anyway, so we never flip it implicitly.
        * Otherwise a plain ``VACUUM``, which is a full rewrite.  It cannot run
          inside a transaction, so it uses its own connection and any failure is
          reported rather than raised: a locked or read-only database must not
          break maintenance.

        Returns a metrics record only — safe to log, no content.
        """
        summary: Dict[str, Any] = {
            "vacuumed": False, "reason": "", "reclaimed_bytes": 0,
            "before_bytes": int(storage.get("db_bytes", 0) or 0),
            "after_bytes": int(storage.get("db_bytes", 0) or 0),
            "mode": "none", "dry_run": bool(dry_run),
        }
        if not allow:
            summary["reason"] = "no delete pass this cycle"
            return summary
        if not storage:
            summary["reason"] = "storage unavailable"
            return summary
        reclaimable = int(storage.get("reclaimable_bytes", 0) or 0)
        page_count = int(storage.get("page_count", 0) or 0)
        freelist = int(storage.get("freelist", 0) or 0)
        ratio = (freelist / page_count) if page_count else 0.0
        # Either absolute waste or a high fraction justifies the pass.  A 4.48GB
        # ledger with 10% free space is worth shrinking; a 2MB ledger with the
        # same fraction is not.
        if reclaimable < min_reclaimable_bytes and ratio < freelist_ratio:
            summary["reason"] = "below threshold"
            return summary
        if dry_run:
            summary["reason"] = "dry run"
            return summary
        default_mode = 1 if int(storage.get("auto_vacuum", 0) or 0) else 0
        conn = self._connect(write=True)
        try:
            if default_mode:
                # Bounded: one page per step, capped so a huge freelist cannot
                # stall the maintenance tick indefinitely.
                steps = min(freelist, 100_000)
                conn.execute(f"PRAGMA incremental_vacuum({int(steps)})")
                summary["mode"] = "incremental"
            else:
                conn.execute("VACUUM")
                summary["mode"] = "full"
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            summary["vacuumed"] = True
        except sqlite3.Error as exc:
            # Locked/read-only/busy: report and let the next cycle retry.
            summary["reason"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            return summary
        finally:
            conn.close()
        after = self._storage_report()
        if after:
            summary["after_bytes"] = int(after.get("db_bytes", 0) or 0)
            summary["reclaimed_bytes"] = max(0, summary["before_bytes"] - summary["after_bytes"])
        summary["reason"] = "threshold exceeded"
        return summary

    def scan_raw_secrets(self, *, batch_size: int = 250) -> Dict[str, int]:
        """Scan raw evidence without returning or logging its contents.

        The result is intentionally aggregate-only so a maintenance report
        cannot become a new secret exfiltration channel.
        """
        batch_size = _clamp_int(batch_size, 250, 10, 1_000)
        scanned = affected = redactions = truncated = 0
        after = ""
        while True:
            with self._reading() as conn:
                rows = conn.execute(
                    "SELECT memory_id,content FROM memories WHERE raw_chat=1 AND memory_id>? "
                    "ORDER BY memory_id ASC LIMIT ?", (after, batch_size)
                ).fetchall()
            if not rows:
                break
            after = str(rows[-1]["memory_id"])
            for row in rows:
                _cleaned, count, was_truncated = sanitize_chat_content(
                    row["content"], limit=MAX_ARCHIVE_MESSAGE_CHARS)
                scanned += 1
                affected += int(bool(count or was_truncated))
                redactions += int(count)
                truncated += int(was_truncated)
        return {"scanned": scanned, "affected": affected,
                "redactions": redactions, "truncated": truncated}

    def purge_expired_raw(self, *, before_days: int = DEFAULT_RAW_RETENTION_DAYS,
                          archive_dir: Optional[os.PathLike] = None,
                          max_bytes: int = DEFAULT_RAW_ARCHIVE_MAX_BYTES,
                          batch_size: int = 2_000,
                          dry_run: bool = False) -> Dict[str, Any]:
        """Physically drop raw evidence past its retention window.

        This is the tier that was missing.  ``maintain`` moved old raw rows to
        ``lifecycle='cold'`` and ``archive_raw_evidence`` wrote a compressed copy,
        but **nothing ever deleted a row**, so a 2-month-old ledger held 1.47M raw
        messages and 4.48GB of file.  A retention policy that never deletes is a
        rota that never rotates.

        Contract (strictly fail-closed):
        1. Select rows whose ``created_at`` is older than ``before_days``.
        2. Write them to a compressed archive file and ``fsync`` it.
        3. Re-read that exact file and confirm every selected ``memory_id`` is
           present.
        4. Only then delete the rows, in bounded batches, in one transaction.
        Any failure in 2 or 3 leaves the database untouched and is reported in
        ``error``; deletion is attempted on the next maintenance pass.

        Returns counts only — never content — so it is safe to log.
        """
        before_days = _clamp_int(before_days, DEFAULT_RAW_RETENTION_DAYS, 1, 36_500)
        max_bytes = _clamp_int(max_bytes, DEFAULT_RAW_ARCHIVE_MAX_BYTES, 1 << 20,
                               2 * 1024 * 1024 * 1024)
        batch_size = _clamp_int(batch_size, 2_000, 50, 20_000)
        summary: Dict[str, Any] = {
            "purge_candidates": 0, "purged": 0, "events_purged": 0,
            "archive_path": "", "dry_run": bool(dry_run),
            "retention_days": before_days,
        }
        cutoff_epoch = time.time() - before_days * 86_400
        cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).isoformat()

        with self._connect() as conn:
            summary["purge_candidates"] = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE raw_chat=1 AND created_at<=?",
                (cutoff_iso,),
            ).fetchone()[0])
        if dry_run or not summary["purge_candidates"]:
            return summary

        root = self._raw_archive_root(archive_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = root / f"purged-{stamp}-{_hash(str(self.path), str(cutoff_iso))[:12]}.jsonl.gz"

        # ---- 1) stream the rows we are about to delete into the archive ----
        selected: List[str] = []
        selected_events: List[str] = []
        try:
            with gzip.open(archive_path, "wb") as handle:
                after = ""
                while True:
                    with self._connect() as conn:
                        batch = conn.execute("""
                            SELECT m.memory_id,m.origin_event_id,m.content,m.source_agent,
                                   m.session_id,m.task_id,m.project,m.role,m.scope,
                                   m.evidence_json,m.created_at,m.metadata_json,
                                   e.occurred_at,e.content_hash
                            FROM memories AS m
                            JOIN ledger_events AS e ON e.event_id=m.origin_event_id
                            WHERE m.raw_chat=1 AND m.created_at<=? AND m.memory_id>?
                            ORDER BY m.memory_id ASC LIMIT ?
                        """, (cutoff_iso, after, batch_size)).fetchall()
                    if not batch:
                        break
                    after = str(batch[-1]["memory_id"])
                    for row in batch:
                        payload = {
                            "memory_id": row["memory_id"],
                            "event_id": row["origin_event_id"],
                            "content": row["content"],
                            "content_sha256": row["content_hash"],
                            "source_agent": row["source_agent"],
                            "session_id": row["session_id"],
                            "task_id": row["task_id"],
                            "project": row["project"],
                            "role": row["role"],
                            "scope": row["scope"],
                            "evidence": row["evidence_json"],
                            "created_at": row["created_at"],
                            "occurred_at": row["occurred_at"],
                            "purged_at": utc_now(),
                        }
                        handle.write((json.dumps(payload, ensure_ascii=False,
                                                 separators=(",", ":")) + "\n").encode("utf-8"))
                        selected.append(str(row["memory_id"]))
                        selected_events.append(str(row["origin_event_id"]))
            try:
                os.chmod(archive_path, 0o600)
            except OSError:
                pass
            # Force the archive to disk before any row is removed.
            with open(archive_path, "rb") as handle:
                os.fsync(handle.fileno())
        except (OSError, sqlite3.Error) as exc:
            summary["error"] = f"archive failed: {type(exc).__name__}: {exc}"[:300]
            summary["purged"] = 0
            try:
                archive_path.unlink()
            except OSError:
                pass
            return summary

        # ---- 2) verify the archive really contains every selected id -------
        try:
            found = set()
            with gzip.open(archive_path, "rb") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    found.add(str(json.loads(line).get("memory_id", "")))
            missing = set(selected) - found
        except (OSError, ValueError) as exc:
            summary["error"] = f"archive verify failed: {type(exc).__name__}: {exc}"[:300]
            return summary
        if missing:
            summary["error"] = f"archive missing {len(missing)} rows; refusing to delete"
            return summary

        # ---- 3) delete only after the archive is proven complete -----------
        def remove(conn: sqlite3.Connection) -> Dict[str, int]:
            removed_events = 0
            for start in range(0, len(selected), batch_size):
                chunk = selected[start:start + batch_size]
                events = selected_events[start:start + batch_size]
                ph = ",".join("?" for _ in chunk)
                eph = ",".join("?" for _ in events)
                # Order matters: child rows first, projection, then the event that
                # pins the projection through memories.origin_event_id.
                conn.execute(f"DELETE FROM memory_fts WHERE memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memory_relations WHERE child_memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memory_relations WHERE parent_memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memory_claims WHERE memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memory_feedback WHERE memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memories WHERE memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM ledger_events WHERE event_id IN ({eph})", events)
                removed_events += len(events)
            return {"events": removed_events}

        try:
            self._transaction(remove)
        except (sqlite3.Error, LedgerError) as exc:
            summary["error"] = f"delete failed after archive: {type(exc).__name__}: {exc}"[:300]
            return summary

        summary["purged"] = len(selected)
        summary["events_purged"] = len(selected_events)
        summary["archive_path"] = str(archive_path)
        removed, remaining = self._prune_raw_archives(root, max_bytes)
        summary["archives_pruned"] = removed
        summary["archive_bytes"] = remaining
        return summary

    def _raw_archive_root(self, archive_dir: Optional[os.PathLike] = None) -> Path:
        root = (Path(archive_dir).expanduser() if archive_dir else
                self.path.parent / (self.path.stem + "." + RAW_ARCHIVE_DIR_NAME))
        if root.exists() and (not root.is_dir() or root.is_symlink()):
            raise LedgerError(f"原始证据归档目录无效: {root}")
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        return root

    @staticmethod
    def _prune_raw_archives(root: Path, max_bytes: int) -> Tuple[int, int]:
        files = []
        total = 0
        for path in root.glob("raw-*.jsonl.gz"):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append((path.stat().st_mtime_ns, path, size))
            total += size
        removed = 0
        for _mtime, path, size in sorted(files):
            if total <= max_bytes:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        return removed, max(0, total)

    def archive_raw_evidence(self, *, before_days: int = DEFAULT_RAW_RETENTION_DAYS,
                             archive_dir: Optional[os.PathLike] = None,
                             max_bytes: int = DEFAULT_RAW_ARCHIVE_MAX_BYTES,
                             batch_size: int = 250) -> Dict[str, int]:
        """Write bounded, redacted raw evidence to compressed JSONL.

        Rows stay in SQLite as the audit projection; the compressed copy is a
        recoverable cold tier and is marked in metadata to make repeated daemon
        maintenance idempotent. Archive files are capped oldest-first.
        """
        before_days = _clamp_int(before_days, DEFAULT_RAW_RETENTION_DAYS, 1, 36500)
        max_bytes = _clamp_int(max_bytes, DEFAULT_RAW_ARCHIVE_MAX_BYTES, 1 << 20,
                               2 * 1024 * 1024 * 1024)
        batch_size = _clamp_int(batch_size, 250, 10, 1_000)
        root = self._raw_archive_root(archive_dir)
        cutoff = time.time() - before_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        now = utc_now()
        archive_path: Optional[Path] = None
        # 会话级已归档集合：同一维护调用内跨批次累积，避免每个批次都重读
        # 整个 gzip 归档文件（文件最大可达数 MB，批次多时是平方级浪费）。
        archived_ids_seen: set = set()
        archive_membership_loaded = False
        written = redactions = truncated = scanned = 0
        after = ""
        while True:
            with self._reading() as conn:
                rows = conn.execute("""
                    SELECT m.memory_id,m.origin_event_id,m.content,m.lifecycle,m.source_agent,m.session_id,
                           m.task_id,m.project,m.role,m.scope,m.evidence_json,m.created_at,
                           m.metadata_json,e.occurred_at
                    FROM memories AS m JOIN ledger_events AS e ON e.event_id=m.origin_event_id
                    WHERE m.raw_chat=1 AND m.memory_id>? AND m.lifecycle IN ('active','cold')
                      AND (m.lifecycle='cold' OR m.created_at<=?)
                      AND m.metadata_json NOT LIKE '%raw_archive_path%'
                    ORDER BY m.memory_id ASC LIMIT ?
                """, (after, cutoff_iso, batch_size)).fetchall()
            if not rows:
                break
            after = str(rows[-1]["memory_id"])
            eligible = []
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                if isinstance(metadata, Mapping) and metadata.get("raw_archive_path"):
                    continue
                source_epoch = _epoch(row["created_at"] or row["occurred_at"])
                if source_epoch and source_epoch > cutoff and row["lifecycle"] == "active":
                    continue
                cleaned, count, was_truncated = sanitize_chat_content(
                    row["content"], limit=MAX_ARCHIVE_MESSAGE_CHARS)
                scanned += 1
                redactions += int(count)
                truncated += int(was_truncated)
                try:
                    evidence = json.loads(row["evidence_json"] or "[]")
                except (TypeError, ValueError):
                    evidence = []
                eligible.append((row, cleaned, count, was_truncated, evidence))
            if not eligible:
                continue
            if archive_path is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                archive_path = root / f"raw-{stamp}-{_hash(str(self.path), now)[:12]}.jsonl.gz"

            def mark_rows(rows: List[tuple]) -> None:
                def mark(conn: sqlite3.Connection) -> None:
                    for row, _cleaned, count, was_truncated, _evidence in rows:
                        try:
                            metadata = json.loads(row["metadata_json"] or "{}")
                        except (TypeError, ValueError):
                            metadata = {}
                        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
                        metadata.update({"raw_archive_path": str(archive_path), "raw_archived_at": now,
                                         "raw_archive_redactions": int(count),
                                         "raw_archive_truncated": bool(was_truncated)})
                        conn.execute("UPDATE memories SET metadata_json=?,updated_at=? WHERE memory_id=?",
                                     (_json(metadata, "metadata"), now, row["memory_id"]))
                self._transaction(mark)

            # 崩溃窗口幂等：gzip 追加与标记事务是两个介质步骤，中间断电会让
            # 同一批行因未标记而被再次选中。写入前先读回归档文件里已有的
            # memory_id 并跳过（残帧容忍），重复维护不再产生重复归档行。
            if not archive_membership_loaded:
                archive_membership_loaded = True
                if archive_path.exists() and archive_path.stat().st_size > 0:
                    try:
                        with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
                            for line in handle:
                                try:
                                    payload = json.loads(line)
                                except (TypeError, ValueError):
                                    continue
                                if isinstance(payload, Mapping):
                                    archived_ids_seen.add(str(payload.get("memory_id", "")))
                    except (OSError, EOFError):
                        archived_ids_seen = set()
            pending = [item for item in eligible
                       if str(item[0]["memory_id"]) not in archived_ids_seen]
            if not pending:
                # 全部已在崩溃前的批次落盘：只补标记即可。
                mark_rows(eligible)
                continue
            eligible = pending
            with gzip.open(archive_path, "ab") as handle:
                for row, cleaned, count, was_truncated, evidence in eligible:
                    payload = {
                        "memory_id": row["memory_id"], "event_id": row["origin_event_id"],
                        "content": cleaned, "source_agent": row["source_agent"],
                        "session_id": row["session_id"], "task_id": row["task_id"],
                        "project": row["project"], "role": row["role"], "scope": row["scope"],
                        "evidence": evidence if isinstance(evidence, list) else [],
                        "occurred_at": row["occurred_at"], "archived_at": now,
                        "redactions": int(count), "truncated": bool(was_truncated),
                    }
                    handle.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                    written += 1
            archived_ids_seen.update(str(row["memory_id"]) for row, *_ in eligible)
            try:
                os.chmod(archive_path, 0o600)
            except OSError:
                pass
            mark_rows(eligible)
        removed, remaining_bytes = self._prune_raw_archives(root, max_bytes)
        return {"raw_compressed": written, "raw_redactions": redactions,
                "raw_truncated": truncated, "raw_scanned": scanned,
                "raw_archive_bytes": remaining_bytes, "raw_archives_pruned": removed}

    def redact_raw_evidence(self, *, limit: int = MAX_ARCHIVE_MESSAGE_CHARS,
                             batch_size: int = 250) -> Dict[str, int]:
        """Redact and bound already-ingested raw evidence in both projections.

        Raw chat is untrusted audit material.  A security remediation is the
        one deliberate exception to the append-only content rule: it keeps
        event IDs, hashes, timestamps and evidence references stable while
        replacing exposed secrets in both ``ledger_events`` and ``memories``.
        Run this only while the daemon is stopped so every FTS row can be
        updated consistently without fighting an active watcher.
        """
        limit = _clamp_int(limit, MAX_ARCHIVE_MESSAGE_CHARS, 200, MAX_CONTENT_CHARS)
        batch_size = _clamp_int(batch_size, 250, 10, 1_000)
        summary = {"scanned": 0, "changed": 0, "redacted": 0, "truncated": 0,
                   "fts_reindexed": 0}
        after = ""
        now = utc_now()

        def updated_metadata(encoded: str, *, redactions: int, truncated: bool) -> str:
            try:
                value = json.loads(encoded or "{}")
            except (TypeError, ValueError):
                value = {}
            metadata = dict(value) if isinstance(value, Mapping) else {}
            metadata["sanitization"] = {
                "redacted_at": now,
                "redactions": int(redactions),
                "truncated": bool(truncated),
            }
            return _json(metadata, "metadata")

        while True:
            with self._reading() as conn:
                rows = conn.execute("""
                    SELECT m.memory_id,m.origin_event_id,m.content AS memory_content,
                           m.metadata_json AS memory_metadata,m.lifecycle,m.task_id,m.project,
                           m.source_agent,m.kind,e.content AS event_content,
                           e.metadata_json AS event_metadata
                    FROM memories AS m
                    JOIN ledger_events AS e ON e.event_id=m.origin_event_id
                    WHERE m.raw_chat=1 AND m.memory_id>?
                    ORDER BY m.memory_id ASC LIMIT ?
                """, (after, batch_size)).fetchall()
            if not rows:
                break
            after = str(rows[-1]["memory_id"])

            def write(conn: sqlite3.Connection) -> Dict[str, int]:
                result = {"scanned": len(rows), "changed": 0, "redacted": 0,
                          "truncated": 0, "fts_reindexed": 0}
                for row in rows:
                    memory_text, memory_redactions, memory_truncated = sanitize_chat_content(
                        row["memory_content"], limit=limit)
                    event_text, event_redactions, event_truncated = sanitize_chat_content(
                        row["event_content"], limit=limit)
                    redactions = (memory_redactions if row["memory_content"] == row["event_content"]
                                  else memory_redactions + event_redactions)
                    truncated = bool(memory_truncated or event_truncated)
                    changed = (memory_text != row["memory_content"] or event_text != row["event_content"])
                    if not changed:
                        continue
                    memory_metadata = updated_metadata(
                        row["memory_metadata"], redactions=redactions, truncated=truncated)
                    event_metadata = updated_metadata(
                        row["event_metadata"], redactions=redactions, truncated=truncated)
                    conn.execute("UPDATE ledger_events SET content=?,metadata_json=? WHERE event_id=?",
                                 (event_text, event_metadata, row["origin_event_id"]))
                    conn.execute("UPDATE memories SET content=?,metadata_json=?,updated_at=? WHERE memory_id=?",
                                 (memory_text, memory_metadata, now, row["memory_id"]))
                    # FTS is a separate copy of the content. Delete first so
                    # a secret cannot survive in an old full-text segment.
                    conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
                    if row["lifecycle"] == "active":
                        conn.execute("""
                            INSERT INTO memory_fts(memory_id,content,task_id,project,source_agent,kind)
                            VALUES(?,?,?,?,?,?)
                        """, (row["memory_id"], memory_text, row["task_id"], row["project"],
                              row["source_agent"], row["kind"]))
                    result["changed"] += 1
                    result["redacted"] += redactions
                    result["truncated"] += int(truncated)
                    result["fts_reindexed"] += 1
                return result

            batch = self._transaction(write)
            for key, value in batch.items():
                summary[key] += int(value)
        return summary

    def consolidate(self, task_id: str = "", limit: int = 64) -> List[IngestResult]:
        """Create small evidence-backed task summaries from structured records only."""
        task_id = _short(task_id, 256)
        limit = _clamp_int(limit, 64, 1, 512)
        clauses = ["raw_chat=0", "kind!='semantic'", "lifecycle IN ('active','cold')",
                   "quality_state='accepted'", "evidence_json!='[]'"]
        params: List[object] = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        sql = ("SELECT * FROM memories WHERE " + " AND ".join(clauses) +
               " ORDER BY updated_at DESC LIMIT ?")
        params.append(limit)
        with self._reading() as conn:
            rows = conn.execute(sql, params).fetchall()
        by_task: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            key = row["task_id"] or ("project:" + row["project"] if row["project"] else "")
            if key:
                by_task.setdefault(key, []).append(row)
        results: List[IngestResult] = []
        for key, group in by_task.items():
            if len(group) < 2:
                continue
            context: Dict[str, List[str]] = {}
            evidence: List[str] = []
            for row in group:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                for field, values in (metadata.get("context", {}) if isinstance(metadata, dict) else {}).items():
                    if field in CONTEXT_FIELDS:
                        context.setdefault(field, []).extend(_strings(values, 12, 4_000, field))
                evidence.extend(_strings(json.loads(row["evidence_json"]), MAX_EVIDENCE, 512, "evidence"))
            if not context:
                continue
            goal = (context.get("goal") or [f"任务 {key}"])[0]
            summary = f"任务 {key} 的已证据化摘要：{goal}"
            sources = sorted({str(row["memory_id"]) for row in group})
            # ------------------------------------------------------------------
            # Idempotent consolidation
            # ------------------------------------------------------------------
            # ``consolidate`` used to re-ingest a freshly built summary whose
            # metadata embedded the source ids *in row order*.  A stable content
            # key therefore still differed on almost every idle pass, so the
            # ledger accumulated 56,010 ``semantic`` rows holding just 142 distinct
            # texts (one repeated 5,423 times).  The projection is now keyed by the
            # source set, written once, and refreshed only when that set changes.
            fingerprint = _hash("consolidate", key, group[0]["scope"], *sources)[:32]
            marker = f"consolidation:{fingerprint}"
            with self._connect() as conn:
                already = conn.execute(
                    "SELECT memory_id FROM memories WHERE kind='semantic' "
                    "AND lifecycle IN ('active','cold') AND evidence_json LIKE ? "
                    "AND quality_state='accepted' LIMIT 1",
                    (f"%{marker}%",),
                ).fetchone()
            if already is not None:
                continue
            result = self.ingest({
                "kind": "semantic", "content": summary, "source_agent": "hippomindd",
                "task_id": key if not key.startswith("project:") else "",
                "project": group[0]["project"], "scope": group[0]["scope"],
                "evidence": [marker] + sources[:MAX_EVIDENCE - 1],
                "context": context,
                "confidence": min(0.95, 0.5 + 0.08 * len(group)), "importance": 7,
                "valid_from": group[0]["created_at"] if "created_at" in group[0].keys() else "",
                "metadata": {
                    "consolidated_from": sources,
                    "consolidation_fingerprint": fingerprint,
                },
            })
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Query projection used by Context Pack and API clients
    # ------------------------------------------------------------------

    @staticmethod
    def _fts_terms(query: str) -> List[str]:
        import re
        raw_terms = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]{2,}", query.lower())
        terms = [term for term in raw_terms
                 if term not in LOW_SIGNAL_QUERY_TERMS and term not in LOW_SIGNAL_CJK_QUERY_TERMS]
        # A bare product/task name such as ``Memory Hub`` can consist entirely
        # of otherwise low-signal words. Retain it only when no stronger term
        # exists; downstream coverage still requires the complete phrase.
        if not terms and len(raw_terms) >= 2:
            terms = raw_terms
        # trigram tokenizer（v6 起）需要 >=3 字的词项；对长 CJK 段补充滑动窗口，
        # 让部分短语也能经 FTS 子串匹配命中（整段词项保留为最严格通道）。
        windowed: List[str] = []
        for term in terms:
            windowed.append(term)
            if len(term) > 6 and re.fullmatch(r"[\u3400-\u9fff]+", term):
                for i in range(len(term) - 4 + 1):
                    window = term[i:i + 5]
                    if window not in windowed:
                        windowed.append(window)
        seen: List[str] = []
        for term in windowed:
            if term not in seen:
                seen.append(term)
        return seen[:12]

    @staticmethod
    def _expansion_terms(query: str) -> List[str]:
        """Strongest entity words for the sparse-candidate expansion pass."""
        import re

        query = str(query or "").lower()
        words = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]{2,}", query)
        unique: List[str] = []
        for word in sorted(set(words), key=len, reverse=True):
            if word not in unique:
                unique.append(word)
        return unique[:6]

    @staticmethod
    def _bigram_terms(query: str) -> List[str]:
        """查询 CJK 段的二元碎片：最后回退通道专用（仅共享短词也能触达）。"""
        import re

        text = str(query or "").lower()
        terms: List[str] = []
        for segment in re.findall(r"[\u3400-\u9fff]{2,}", text):
            if segment in LOW_SIGNAL_CJK_QUERY_TERMS:
                continue
            for index in range(len(segment) - 1):
                gram = segment[index:index + 2]
                if gram not in terms and gram not in LOW_SIGNAL_CJK_QUERY_TERMS:
                    terms.append(gram)
        return terms[:12]

    @staticmethod
    def _cross_language_terms(query: str) -> List[str]:
        """中英技术词映射：查询含映射表词时给出对侧翻译词。"""
        lowered = str(query or "").lower()
        out: List[str] = []
        for cn, english in CROSS_LANGUAGE_TERMS.items():
            if cn in lowered:
                out.extend(english)
                continue
            for en in english:
                if en in lowered:
                    out.append(cn)
                    break
        seen: List[str] = []
        for term in out:
            if term.lower() not in seen:
                seen.append(term.lower())
        return seen[:6]

    @staticmethod
    def _like_terms(query: str) -> List[str]:
        """Return bounded phrase fragments for the non-FTS CJK candidate path."""
        import re

        query = str(query or "").lower()
        terms: List[str] = re.findall(r"[a-z0-9_]{2,}", query)
        for segment in re.findall(r"[\u3400-\u9fff]{2,}", query):
            if segment in LOW_SIGNAL_CJK_QUERY_TERMS:
                continue
            if len(segment) < 4:
                terms.append(segment)
                continue
            # 三字碎片已足够判别（如「端口是」「负责人」），四字起会漏掉
            # 词序不同的自然问句；容量仍受 [:64] 上限约束。
            for size in range(min(8, len(segment)), 2, -1):
                terms.extend(segment[index:index + size] for index in range(len(segment) - size + 1))
        seen: List[str] = []
        for term in terms:
            if term and term not in seen:
                seen.append(term)
        # LIKE 回退是全库扫描：每个词项都是一行上的 OR 谓词。长窗口判别力
        # 最高（误报最少），裁剪到前 8 项可把扫描成本近乎减半而不损召回——
        # 实测 50k 行救援路径 ~160ms，词项数是其主要成本项。
        return seen[:8]

    @staticmethod
    def _query_coverage(query: str, corpus: str) -> Tuple[float, bool]:
        """Measure meaningful textual overlap independently of FTS rank.

        SQLite builds differ in their CJK token boundaries. A broad query can
        still find a record that shares only a common short fragment. BM25 is
        useful for candidate generation but cannot by itself prove that a
        record covers the current task.
        """
        prepared = SQLiteLedger._prepare_query_coverage(query)
        return SQLiteLedger._apply_query_coverage(prepared, corpus)

    @staticmethod
    def _prepare_query_coverage(query: str) -> Dict[str, Any]:
        """查询侧分析只做一次（多行评分循环外提升，前景轮九性能优化）。"""
        import re

        query = str(query or "").lower()
        raw_ascii_terms = [
            term for term in re.findall(r"[a-z0-9_]{2,}", query)
            if not term.startswith("__hm_")
        ]
        ascii_terms = [
            term for term in raw_ascii_terms
            if term not in LOW_SIGNAL_QUERY_TERMS and not term.startswith("__hm_")
        ]
        if not ascii_terms and len(raw_ascii_terms) >= 2:
            ascii_terms = raw_ascii_terms
        cjk_segments = [segment for segment in re.findall(r"[\u3400-\u9fff]{2,}", query)
                        if segment not in LOW_SIGNAL_CJK_QUERY_TERMS]
        cjk_bigrams = [segment[index:index + 2] for segment in cjk_segments
                       for index in range(len(segment) - 1)]
        terms = list(dict.fromkeys(ascii_terms + cjk_bigrams))[:64]
        phrase_terms = list(ascii_terms)
        # A useful identifier can consist solely of otherwise common words:
        # "Memory Hub" must remain a reliable phrase even though either word
        # alone is too broad to select an unrelated task.
        for size in range(min(4, len(raw_ascii_terms)), 1, -1):
            phrase_terms.extend(
                " ".join(raw_ascii_terms[index:index + size])
                for index in range(len(raw_ascii_terms) - size + 1)
            )
        for segment in cjk_segments:
            # A full sentence usually does not occur verbatim in a memory.
            # Four-or-more-character fragments preserve useful CJK retrieval
            # such as "自动上下文" without accepting a lone "上下文" overlap.
            for size in range(min(8, len(segment)), 3, -1):
                phrase_terms.extend(segment[index:index + size] for index in range(len(segment) - size + 1))
        return {"terms": terms, "phrase_terms": phrase_terms}

    @staticmethod
    def _apply_query_coverage(prepared: Dict[str, Any], corpus: str) -> Tuple[float, bool]:
        """对单条候选应用已准备的查询词表（corpus 侧逐行执行）。"""
        import re

        terms = prepared["terms"]
        phrase_terms = prepared["phrase_terms"]
        if not terms and not phrase_terms:
            return (0.0, False)
        corpus = str(corpus or "").lower()
        matched = sum(1 for term in terms if term in corpus)
        phrase_corpus = re.sub(r"[-_]+", " ", corpus)
        phrase_hit = any(term in phrase_corpus for term in phrase_terms)
        return (matched / len(terms) if terms else 0.0, phrase_hit)

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        for key in ("evidence_json", "tags_json", "metadata_json"):
            try:
                data[key[:-5]] = json.loads(data.pop(key) or ("{}" if key == "metadata_json" else "[]"))
            except (TypeError, ValueError):
                data[key[:-5]] = {} if key == "metadata_json" else []
        return data

    def _record_search_latency(self, seconds: float) -> None:
        """慢查询哨兵采样：线程安全地记录延迟并累计超阈值次数。"""
        with self._search_lock:
            self._search_durations.append(seconds)
            if seconds >= self._slow_search_threshold:
                self._slow_search_count += 1

    def search(self, query: str, *, scope: str = "shared", task_id: str = "",
               project: str = "", source_agents: Optional[Sequence[str]] = None,
               roles: Optional[Sequence[str]] = None, from_time: str = "", to_time: str = "",
               include_raw_chat: bool = False, require_evidence: bool = True,
               include_cold: bool = True, limit: int = 80, offset: int = 0,
               embedder=None, as_of: str = "") -> List[Dict[str, Any]]:
        started = _time_mod.perf_counter()
        try:
            return self._search_impl(
                query, scope=scope, task_id=task_id, project=project,
                source_agents=source_agents, roles=roles, from_time=from_time,
                to_time=to_time, include_raw_chat=include_raw_chat,
                require_evidence=require_evidence, include_cold=include_cold,
                limit=limit, offset=offset, embedder=embedder, as_of=as_of)
        finally:
            self._record_search_latency(_time_mod.perf_counter() - started)

    def _search_impl(self, query: str, *, scope: str = "shared", task_id: str = "",
                     project: str = "", source_agents: Optional[Sequence[str]] = None,
                     roles: Optional[Sequence[str]] = None, from_time: str = "", to_time: str = "",
                     include_raw_chat: bool = False, require_evidence: bool = True,
                     include_cold: bool = True, limit: int = 80, offset: int = 0,
                     embedder=None, as_of: str = "") -> List[Dict[str, Any]]:
        """``embedder``：可选向量通道（EmbeddingAdapter）。仅做重排融合，
        不参与候选生成；候选数超过 600 时自动跳过以约束成本。
        ``as_of``：双时态历史切片（对齐 zep/Graphiti point-in-time 语义）——
        给定 ISO 时间点时，valid_from/valid_until 窗口按该时刻求值，
        可回答"当时系统认为什么为真"；默认空 = 现在。"""
        query = _text(query, MAX_QUERY_CHARS, "query")
        scope = _short(scope, 128) or "shared"
        task_id = _short(task_id, 256)
        project = _short(project, 512)
        source_agents = _strings(source_agents, 64, 128, "source_agents")
        roles = _strings(roles, 16, 64, "roles")
        from_time = _timestamp(from_time) if from_time else ""
        as_of = _timestamp(as_of) if as_of else ""
        to_time = _timestamp(to_time) if to_time else ""
        limit = _clamp_int(limit, 80, 1, 1_000)
        offset = _clamp_int(offset, 0, 0, 100_000)
        clauses = ["m.scope=?"]
        params: List[object] = [scope]
        if task_id:
            clauses.append("m.task_id=?")
            params.append(task_id)
        if project:
            clauses.append("m.project=?")
            params.append(project)
        if source_agents:
            clauses.append("m.source_agent IN (" + ",".join("?" for _ in source_agents) + ")")
            params.extend(source_agents)
        if roles:
            clauses.append("m.role IN (" + ",".join("?" for _ in roles) + ")")
            params.extend(roles)
        if from_time:
            clauses.append("m.created_at>=?")
            params.append(from_time)
        if to_time:
            clauses.append("m.created_at<=?")
            params.append(to_time)
        if not include_raw_chat:
            clauses.append("m.raw_chat=0")
        if require_evidence:
            clauses.append("m.evidence_json!='[]'")
        # Quarantined, conflicting and superseded claims are audit-visible but
        # must not silently enter a normal Context Pack.
        clauses.append("m.quality_state='accepted'")
        if include_cold:
            clauses.append("m.lifecycle IN ('active','cold')")
        else:
            clauses.append("m.lifecycle='active'")
        if as_of:
            # point-in-time 语义：维护任务按"现在"把过期事实标为 expired，
            # 但其窗口可能包含 as_of 时刻——历史切片必须仍可见
            # （superseded 属冲突语义，保持排除）。
            clauses.append("m.lifecycle IN ('active','cold','expired')")
        # Expiry is a recall constraint, not only a maintenance concern.  A
        # daemon can be queried before its next maintenance tick, so a fact
        # with an elapsed validity window must never re-enter a Context Pack.
        # as_of 给定时，窗口按历史时刻求值（point-in-time 查询）。
        now_iso = as_of or utc_now()
        clauses.append("(m.valid_until='' OR m.valid_until>?)")
        params.append(now_iso)
        # 双时态窗口的另一侧：提前登记的定时事实在生效前不得进入 Context Pack。
        clauses.append("(m.valid_from='' OR m.valid_from<=?)")
        params.append(now_iso)
        cache_key = (self.generation, query, scope, task_id, project,
                     tuple(source_agents), tuple(roles), from_time, to_time,
                     include_raw_chat, require_evidence, include_cold,
                     limit, offset, embedder is not None, as_of)
        cached = self._search_cached(cache_key) if query else None
        if cached is not None:
            return cached
        terms = self._fts_terms(query)
        fts_scores: Dict[str, float] = {}
        # RRF 融合：记录每条候选在各召回通道中的名次，双通道同时命中的
        # 记忆获得有界加分（比单通道 max 归一化更稳，见混合检索通行做法）。
        fts_order: Dict[str, int] = {}
        like_order: Dict[str, int] = {}
        candidate_rows: List[sqlite3.Row] = []
        if terms:
            expression = " OR ".join('"' + term.replace('"', '') + '"' for term in terms)
            try:
                with self._reading() as conn:
                    # Apply every structured filter before FTS candidate
                    # limiting. Otherwise a noisy raw transcript, unrelated
                    # scope, or another project can consume the whole FTS
                    # candidate budget and hide the only allowed memory.
                    fts_where = ["memory_fts MATCH ?", *clauses]
                    fts_params: List[object] = [expression, *params]
                    fts_rows = conn.execute(
                        "SELECT m.*,bm25(memory_fts) AS _fts_rank FROM memory_fts "
                        "JOIN memories m ON m.memory_id=memory_fts.memory_id WHERE " +
                        " AND ".join(fts_where) +
                        " ORDER BY bm25(memory_fts) ASC LIMIT ?",
                        (*fts_params,
                         min(_FTS_POOL_CEILING, max(limit + offset, limit * 20))),
                    ).fetchall()
                for position, row in enumerate(fts_rows):
                    fts_scores[row["memory_id"]] = max(0.0, -float(row["_fts_rank"]))
                    fts_order[row["memory_id"]] = position
                candidate_rows.extend(fts_rows)
            except sqlite3.DatabaseError:
                # FTS is an optimisation, not a reason to return unrelated history.
                fts_scores = {}
                fts_order = {}
                candidate_rows = []
        # CJK token boundaries vary by SQLite build. Add a bounded phrase-LIKE
        # candidate path even when FTS returned something, then let coverage
        # and MMR reject weak overlaps. This avoids a few noisy FTS candidates
        # hiding the exact phrase the user actually asked to continue.
        like_terms = self._like_terms(query)
        # LIKE 回退是 O(全库) 扫描（50k 行实测 584ms）。FTS 切 trigram 后中文
        # 查询已能经子串匹配命中；仅当 FTS 候选稀疏（低于召回预算）时才值得
        # 付出扫描成本做救援——富余时直接跳过。
        like_gate = max(limit * 2, 48)
        if query and like_terms and len(candidate_rows) < like_gate:
            # trigram FTS 本身支持子串 MATCH：≥3 字符的救援词走索引短语
            # 查询，语义等价于 LIKE '%term%'，但免去 O(全库) 表扫描
            # （69 万行实测 11s → 索引路径 <10ms）。关键推论：若短语
            # MATCH 已返回空，同样的 ≥3 字符词做表扫描也不可能命中
            # （trigram 覆盖全部内容且大小写不敏感）——此时仅当存在
            # <3 字符短词且候选完全为空时，才做有界窗口扫描兜底。
            phrase_terms = [term for term in like_terms if len(term) >= 3]
            short_terms = [term for term in like_terms if len(term) < 3]
            like_rows: List[sqlite3.Row] = []
            if phrase_terms:
                phrase_expr = " OR ".join(
                    '"' + term.replace('"', '') + '"' for term in phrase_terms)
                try:
                    with self._reading() as conn:
                        phrase_where = ["memory_fts MATCH ?", *clauses]
                        like_rows = conn.execute(
                            "SELECT m.*,bm25(memory_fts) AS _fts_rank FROM memory_fts "
                            "JOIN memories m ON m.memory_id=memory_fts.memory_id "
                            "WHERE " + " AND ".join(phrase_where) +
                            # bm25 序而非 updated_at：后者会诱导规划器沿
                            # updated_at 索引全表游走（77 万行 ≈1s）来喂排序；
                            # bm25 序留在 FTS 空间内，救援名次本就按相关性融合。
                            " ORDER BY bm25(memory_fts) ASC LIMIT ?",
                            (phrase_expr, *params,
                             min(_LIKE_POOL_CEILING,
                                 max(limit + offset, limit * 20, 200))),
                        ).fetchall()
                except sqlite3.DatabaseError:
                    like_rows = []
            if not like_rows and short_terms and not candidate_rows:
                like_clauses = [*clauses,
                                "(" + " OR ".join("LOWER(m.content) LIKE ?"
                                                  for _ in short_terms) + ")"]
                like_params: List[object] = [*params,
                                             *("%" + term + "%" for term in short_terms)]
                # 有界窗口：只在新度最高的行里救援，最坏成本有上限。
                with self._reading() as conn:
                    like_rows = conn.execute(
                        "SELECT * FROM ("
                        "SELECT m.* FROM memories m WHERE " + " AND ".join(like_clauses[:-1]) +
                        " ORDER BY m.updated_at DESC LIMIT ?"
                        ") AS m WHERE " + like_clauses[-1],
                        (*params,
                         min(_LIKE_SCAN_WINDOW,
                             max(limit + offset, limit * 20, 200)),
                         *like_params[len(params):]),
                    ).fetchall()
            present = {row["memory_id"] for row in candidate_rows}
            for position, row in enumerate(like_rows):
                if row["memory_id"] not in present:
                    like_order[row["memory_id"]] = position
                    candidate_rows.append(row)
        # Archived raw chat is deliberately removed from FTS during maintenance.
        # Explicit raw audit must still be complete, so add a scoped LIKE path
        # without changing the default raw_chat=false safety boundary. Low
        # signal queries can yield no FTS terms; skip instead of emitting an
        # invalid empty OR group (the generic LIKE fallback below still runs).
        if query and include_raw_chat and terms:
            # raw 归档可能占库容 99%+（本机实测 77 万行）。LIKE 审计通道
            # 用新度有界窗口（默认最新 5 万行 raw 行），否则一次显式审计
            # 查询要付出 6-11s 全表扫描；小库（<窗口）行为完全不变。
            raw_like = [*clauses, "m.raw_chat=1"]
            raw_like_group = "(" + " OR ".join("LOWER(m.content) LIKE ?" for _ in terms) + ")"
            raw_params: List[object] = [*params, *("%" + term.lower() + "%" for term in terms)]
            with self._reading() as conn:
                raw_rows = conn.execute(
                    "SELECT * FROM ("
                    "SELECT m.* FROM memories m WHERE " + " AND ".join(raw_like) +
                    " ORDER BY m.updated_at DESC LIMIT ?"
                    ") AS m WHERE " + raw_like_group,
                    (*params,
                     min(_RAW_LIKE_SCAN_WINDOW, max(limit + offset, limit * 20, 400)),
                     *raw_params[len(params):]),
                ).fetchall()
            present = {row["memory_id"] for row in candidate_rows}
            candidate_rows.extend(row for row in raw_rows if row["memory_id"] not in present)
        # 实体扩展二次检索（multi-pass entity-aware retrieval）：主查询候选
        # 稀疏时，用查询中未参与 FTS 的最强实体词补一轮受限 OR 检索；结果
        # 仍要经过同一 coverage/可靠性门槛，不会引入弱相关噪声。
        if query and len(candidate_rows) < max(limit * 2, 24):
            expansion = [term for term in self._expansion_terms(query) if term not in terms][:3]
            # 跨语言改写：中文查询补英文技术词（反之亦然）——混合语言团队的
            # 归档常用另一种语言书写，扩展通道让两侧都能触达。
            existing_lower = {term.lower() for term in terms}
            cross = [term for term in self._cross_language_terms(query)
                     if term.lower() not in existing_lower][:3]
            expansion = (expansion + cross)[:4]
            if expansion:
                exp_expression = " OR ".join('"' + term.replace('"', '') + '"' for term in expansion)
                try:
                    with self._reading() as conn:
                        exp_where = ["memory_fts MATCH ?", *clauses]
                        exp_rows = conn.execute(
                            "SELECT m.*,bm25(memory_fts) AS _fts_rank FROM memory_fts "
                            "JOIN memories m ON m.memory_id=memory_fts.memory_id WHERE " +
                            " AND ".join(exp_where) +
                            " ORDER BY bm25(memory_fts) ASC LIMIT ?",
                            (*[exp_expression, *params], min(400, max(limit + offset, limit * 8))),
                        ).fetchall()
                    present = {row["memory_id"] for row in candidate_rows}
                    candidate_rows.extend(row for row in exp_rows if row["memory_id"] not in present)
                except sqlite3.DatabaseError:
                    pass
        if query and not candidate_rows:
            # 2 字词回退：主通道（FTS/LIKE≥3字/实体扩展）全部落空时，用查询
            # CJK 段的二元碎片做最后一次有界尝试——词序不同、仅共享短词的
            # 自然问句也能触达记忆。候选仍要过评分与可靠性门槛，弱匹配不会
            # 进入 Context Pack（弃权契约不变）。
            bigram_terms = self._bigram_terms(query)
            if bigram_terms:
                bg_clauses = [*clauses, "(" + " OR ".join(
                    "LOWER(m.content) LIKE ?" for _ in bigram_terms) + ")"]
                bg_params = [*params, *("%" + term + "%" for term in bigram_terms)]
                with self._reading() as conn:
                    # 有界窗口：二元词精度低、命中面大，全表扫描在 69 万行
                    # 库上实测 0.5-1s+；只在新度最高的窗口行内尝试，
                    # 小库（<窗口）行为不变。
                    candidate_rows = conn.execute(
                        "SELECT * FROM ("
                        "SELECT m.* FROM memories m WHERE " + " AND ".join(bg_clauses[:-1]) +
                        " ORDER BY m.updated_at DESC LIMIT ?"
                        ") AS m WHERE " + bg_clauses[-1],
                        (*params,
                         min(_LIKE_SCAN_WINDOW, max(limit + offset, limit * 8)),
                         *bg_params[len(params):]),
                    ).fetchall()
        if query and not candidate_rows:
            terms_like = self._like_terms(query)
            if not terms_like:
                return []
            clauses.append("(" + " OR ".join("LOWER(m.content) LIKE ?" for _ in terms_like) + ")")
            params.extend(["%" + term.lower() + "%" for term in terms_like])
        if candidate_rows:
            rows = candidate_rows
        else:
            sql = "SELECT m.* FROM memories m WHERE " + " AND ".join(clauses) + " ORDER BY m.updated_at DESC LIMIT ?"
            params.append(min(5_000, max(limit + offset, limit * 12)))
            with self._reading() as conn:
                rows = conn.execute(sql, params).fetchall()
        now = time.time()
        max_fts = max(fts_scores.values(), default=0.0)
        # 查询侧常量工作提到行循环外：coverage 词表与查询 token 只算一次
        # （此前每行重复正则/切分，剖析显示占检索耗时显著份额）。
        qprep = self._prepare_query_coverage(query) if query else None
        qtokens = _sim_tokens(query) if query else None
        # 可选向量通道：查询向量只算一次；候选集过大时跳过（成本约束），
        # 小权重融合进总分，不替换关键词通道。
        query_vec = embedder.embed(query) if (embedder is not None and query) else None
        use_vector = query_vec is not None and len(rows) <= 600
        results: List[Dict[str, Any]] = []
        for row in rows:
            item = self._row_dict(row)
            context_text = _context_search_text(item.get("metadata", {}))
            corpus = " ".join([item["content"], context_text, item["task_id"],
                                item["project"], item["kind"]])
            if qprep is not None:
                ctokens = _sim_tokens(corpus)
                union = len(qtokens | ctokens)
                topic = (len(qtokens & ctokens) / union) if union else 0.0
                coverage, phrase_hit = self._apply_query_coverage(qprep, corpus)
            else:
                topic = 0.7
                coverage, phrase_hit = (1.0, True)
            fts = fts_scores.get(item["memory_id"], 0.0)
            # Rank selects candidates; coverage prevents a single shared CJK
            # character from being promoted to a full-strength FTS hit.
            fts = (fts / max_fts if max_fts else 0.0) * coverage
            # RRF 双通道融合加分：FTS 与 LIKE 各按名次贡献 1/(60+rank)，
            # 归一化后作为小权重信号——两路独立命中的证据更可信。
            rrf = 0.0
            position = fts_order.get(item["memory_id"])
            if position is not None:
                rrf += 1.0 / (60.0 + position)
            position = like_order.get(item["memory_id"])
            if position is not None:
                rrf += 1.0 / (60.0 + position)
            rrf_norm = min(1.0, rrf / (2.0 / 61.0))
            vec_sim = 0.0
            if use_vector:
                corpus_vec = embedder.embed(corpus)
                vec_sim = max(0.0, float(embedder.cosine(query_vec, corpus_vec)))
            # ``updated_at`` changes when feedback or maintenance touches the
            # mutable projection. Source freshness must track the immutable
            # event timestamp instead, otherwise old hot memories outrank a
            # newer project update merely because they were maintained often.
            source_epoch = _epoch(item.get("created_at", "")) or _epoch(item["updated_at"])
            age_days = max(0.0, now - source_epoch) / 86400.0
            recency = 1.0 / (1.0 + age_days / 60.0)
            access_epoch = _epoch(item.get("last_access_at", "")) or source_epoch
            access_age_days = max(0.0, now - access_epoch) / 86400.0
            touch = (min(1.0, math.log1p(int(item["touch_count"])) / math.log(11.0))
                     / (1.0 + access_age_days / 14.0))
            utility = (float(item["utility"]) + 1.0) / 2.0
            task_scope = 1.0 if (task_id and item["task_id"] == task_id) else (
                1.0 if project and item["project"] == project else 0.0)
            structured = 1.0 if (not item.get("raw_chat") and item.get("kind") in {
                "task", "goal", "progress", "decision", "blocker", "next_step", "result", "semantic",
                "procedural",
            }) else 0.0
            trust = _clamp_float(item.get("agent_trust", DEFAULT_AGENT_TRUST), DEFAULT_AGENT_TRUST, 0.0, 1.0)
            quality = _clamp_float(item.get("quality_score", 1.0), 1.0, 0.0, 1.0)
            # Keyword/FTS selects evidence first; task scope, immutable source
            # freshness, confidence, importance and actual rendered touches
            # then settle ties. Raw chat is filtered by default before this
            # path, so it cannot crowd out structured continuation state.
            pinned = 1 if int(item.get("pinned", 0) or 0) else 0
            score = (0.47 * max(topic, fts) + 0.14 * recency + 0.025 * touch +
                     0.04 * float(item["weight"]) + 0.09 * float(item["confidence"]) +
                     0.06 * utility + 0.04 * int(item["importance"]) / 10.0 +
                     0.07 * task_scope + 0.05 * structured + 0.02 * trust + 0.02 * quality +
                     0.05 * rrf_norm + 0.04 * vec_sim + 0.06 * pinned)
            item["score"] = min(1.0, score)
            item["score_parts"] = {"topic": round(topic, 4), "fts": round(fts, 4),
                                   "coverage": round(coverage, 4), "phrase": phrase_hit,
                                   "rrf": round(rrf_norm, 4),
                                   "time": round(recency, 4), "touch": round(touch, 4),
                                   "weight": round(float(item["weight"]), 4),
                                   "confidence": round(float(item["confidence"]), 4),
                                   "trust": round(trust, 4), "quality": round(quality, 4),
                                   "utility": round(utility, 4),
                                   "task_scope": round(task_scope, 4),
                                   "structured": round(structured, 4),
                                   "pinned": pinned}
            if use_vector:
                item["score_parts"]["vec"] = round(vec_sim, 4)
            results.append(item)
        results.sort(key=lambda item: (item["score"], item.get("created_at", ""), item["updated_at"]), reverse=True)
        page = results[offset:offset + limit]
        if query:
            self._search_store(cache_key, page)
        return page

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        with self._reading() as conn:
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (_short(memory_id, 80),)).fetchone()
        return self._row_dict(row) if row is not None else None

    def pin_memory(self, memory_id: str, pinned: bool = True,
                   *, source: str = "local-admin") -> Dict[str, Any]:
        """手动置顶/取消置顶。

        置顶只在「已被查询词命中为候选」的记忆之间提升排名（有界加分），
        不把无关记忆强行塞进结果——覆盖度与可靠性门槛不变。
        """
        mid = _short(memory_id, 80)
        if not mid:
            raise LedgerError("memory_id 不能为空")
        now = utc_now()

        operator = _short(source, 128) or "local-admin"
        event_type = "pin" if pinned else "unpin"

        def write(conn: sqlite3.Connection) -> Dict[str, Any]:
            cur = conn.execute("UPDATE memories SET pinned=?, updated_at=? WHERE memory_id=?",
                               (1 if pinned else 0, now, mid))
            if cur.rowcount == 0:
                raise LedgerError(f"记忆不存在: {mid}")
            # 审计事件不含记忆内容（与 forget 同一隐私取向），仅记录操作本身。
            event_id = "evt-" + hashlib.sha256(
                f"{event_type}:{mid}:{now}".encode()).hexdigest()[:24]
            conn.execute("""
                INSERT INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,
                    task_id,project,role,scope,evidence_json,content,content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (event_id, event_type, now, now, operator, "", "", "", "",
                   "shared", json.dumps([f"{event_type}:{mid}"]), "",
                   "sha256:" + hashlib.sha256(
                       f"{event_type}:{mid}:{now}:{operator}".encode()).hexdigest(),
                   "{}"))
            return {"memory_id": mid, "pinned": bool(pinned), "updated_at": now,
                    "event_id": event_id}

        return self._transaction(write)

    def list_pinned(self, *, scope: str = "shared", limit: int = 100) -> List[Dict[str, Any]]:
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT memory_id,content,kind,scope,pinned,updated_at FROM memories "
                "WHERE pinned=1 AND scope=? ORDER BY updated_at DESC LIMIT ?",
                (_short(scope, 128) or "shared", _clamp_int(limit, 100, 1, 1000)),
            ).fetchall()
        return [dict(r) for r in rows]

    def repair_fts(self) -> Dict[str, Any]:
        """一键重建 FTS 投影（checkup --repair 的后端）。

        采用 DROP+CREATE 结构性重建：外部工具直接改动 FTS 表可能留下
        残缺的内部结构（integrity_check 报 fts5 corruption），仅 DELETE+重插
        不保证清除。DROP 后按当前分词器原样建表并回填全部投影。
        """
        with self._local_lock:
            conn = self._connect(write=True)
            try:
                before = conn.execute(
                    "SELECT COUNT(*) FROM memory_fts").fetchone()[0]
                conn.execute("DROP TABLE IF EXISTS memory_fts")
                conn.execute(
                    "CREATE VIRTUAL TABLE memory_fts USING fts5("
                    "memory_id UNINDEXED, content, task_id, project, "
                    "source_agent, kind, tokenize='" + _fts_tokenizer() + "')")
                self._rebuild_fts(conn)
                after = conn.execute(
                    "SELECT COUNT(*) FROM memory_fts").fetchone()[0]
                self._generation += 1
                return {"rebuilt": True, "fts_rows_before": int(before),
                        "fts_rows_after": int(after)}
            finally:
                conn.close()

    def integrity_check(self) -> Dict[str, Any]:
        with self._reading() as conn:
            status = conn.execute("PRAGMA integrity_check").fetchone()[0]
        wal = Path(str(self.path) + "-wal")
        return {"status": str(status), "wal_bytes": wal.stat().st_size if wal.exists() else 0}

    def fts_sync_report(self) -> Dict[str, Any]:
        """投影与 FTS 行数差：>0 表示有记忆未被索引（可经 maintain 重建）。"""
        with self._reading() as conn:
            proj = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE quality_state='accepted' "
                "AND lifecycle IN ('active','cold') AND NOT (raw_chat=1 AND lifecycle!='active')"
            ).fetchone()[0]
            fts_rows = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
            missing = conn.execute(
                "SELECT COUNT(*) FROM memories m WHERE m.quality_state='accepted' "
                "AND m.lifecycle IN ('active','cold') AND NOT EXISTS "
                "(SELECT 1 FROM memory_fts f WHERE f.memory_id=m.memory_id)"
            ).fetchone()[0]
        status = "ok" if missing == 0 else "warn"
        return {"status": status, "projections": int(proj), "fts_rows": int(fts_rows),
                "unindexed": int(missing),
                "hint": None if missing == 0 else "运行 hippomind maintain 可重建 FTS"}

    def claims_consistency_report(self) -> Dict[str, Any]:
        """活跃 claim 指向的记忆必须存在且为 accepted。"""
        with self._reading() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_claims c LEFT JOIN memories m "
                "ON m.memory_id=c.memory_id WHERE c.state='active' "
                "AND (m.memory_id IS NULL OR m.quality_state!='accepted')"
            ).fetchone()[0]
            total = conn.execute(
                "SELECT COUNT(*) FROM memory_claims WHERE state='active'").fetchone()[0]
        status = "ok" if row == 0 else "warn"
        return {"status": status, "active_claims": int(total), "dangling": int(row)}

    def export_sql(self, *, scope: str = "shared", include_raw_chat: bool = False,
                   limit: int = 0, since: str = "") -> str:
        """导出为可回放 SQL（审计友好）：ledger_events + memories + FTS 重建。

        与 JSONL 导出同源同过滤（export_rows 的 SQL 形态）。回放目标必须是
        同 schema 版本的空库（sqlite3 target.db < dump.sql）；INSERT OR
        IGNORE 保证重复回放幂等。claims/relations/feedback 不随包——它们是
        派生数据，正式迁移请用 backup/restore 或 import。
        """
        rows = self.export_rows(scope=scope, include_raw_chat=include_raw_chat,
                                limit=limit, since=since)
        # 取原始行（含未解析的 json 字段与事件列）
        scope_val = _short(scope, 128) or "shared"
        since_iso = _timestamp(since) if since else ""
        clauses = ["m.scope=?", "m.quality_state='accepted'",
                   "m.lifecycle IN ('active','cold')",
                   "(m.valid_until='' OR m.valid_until>?)"]
        params: List[object] = [scope_val, utc_now()]
        if since_iso:
            clauses.append("m.created_at>?")
            params.append(since_iso)
        if not include_raw_chat:
            clauses.append("m.raw_chat=0")
        sql = ("SELECT m.*, e.event_id AS _event_id, e.event_type AS _event_type, "
               "e.occurred_at AS _occurred_at, e.ingested_at AS _ingested_at, "
               "e.source_agent AS _e_source_agent, e.session_id AS _e_session_id, "
               "e.task_id AS _e_task_id, e.project AS _e_project, e.role AS _e_role, "
               "e.evidence_json AS _e_evidence_json, e.content AS _e_content, "
               "e.content_hash AS _e_content_hash, e.metadata_json AS _e_metadata_json "
               "FROM memories m JOIN ledger_events e ON e.event_id=m.origin_event_id "
               "WHERE " + " AND ".join(clauses) + " ORDER BY m.created_at ASC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        out: List[str] = [
            "-- HippoMind 审计导出（可回放）",
            f"-- generated: {utc_now()}",
            f"-- scope: {scope_val} | rows: {len(rows)} | "
            f"schema_version: {SCHEMA_VERSION}",
            "-- 回放: sqlite3 target.db < this_file.sql （须为同版本空库）",
            "BEGIN TRANSACTION;",
        ]

        def q(value) -> str:
            if value is None:
                return "NULL"
            return "'" + str(value).replace("'", "''") + "'"

        seen_events: set = set()
        with self._reading() as conn:
            for row in conn.execute(sql, params):
                eid = row["_event_id"]
                if eid not in seen_events:
                    seen_events.add(eid)
                    out.append(
                        "INSERT OR IGNORE INTO ledger_events(event_id,event_type,"
                        "occurred_at,ingested_at,source_agent,session_id,task_id,"
                        "project,role,scope,evidence_json,content,content_hash,"
                        "metadata_json) VALUES("
                        + ",".join(q(row[k]) for k in (
                            "_event_id", "_event_type", "_occurred_at",
                            "_ingested_at", "_e_source_agent", "_e_session_id",
                            "_e_task_id", "_e_project", "_e_role", "scope",
                            "_e_evidence_json", "_e_content", "_e_content_hash",
                            "_e_metadata_json")) + ");")
                out.append(
                    "INSERT OR IGNORE INTO memories(memory_id,origin_event_id,kind,"
                    "content,source_agent,session_id,task_id,project,role,scope,"
                    "evidence_json,tags_json,metadata_json,created_at,updated_at,"
                    "last_access_at,last_weighted_at,valid_until,superseded_by,"
                    "archived_at,lifecycle,raw_chat,weight,importance,touch_count,"
                    "utility,confidence,success_count,failure_count,pinned) VALUES("
                    + ",".join(q(row[k]) for k in (
                        "memory_id", "origin_event_id", "kind", "content",
                        "source_agent", "session_id", "task_id", "project",
                        "role", "scope", "evidence_json", "tags_json",
                        "metadata_json", "created_at", "updated_at",
                        "last_access_at", "last_weighted_at", "valid_until",
                        "superseded_by", "archived_at", "lifecycle", "raw_chat",
                        "weight", "importance", "touch_count", "utility",
                        "confidence", "success_count", "failure_count",
                        "pinned")) + ");")
        out.append("INSERT INTO memory_fts(memory_id, content) "
                   "SELECT memory_id, content FROM memories;")
        out.append("COMMIT;")
        return "\n".join(out) + "\n"

    def vacuum(self) -> Dict[str, Any]:
        """VACUUM 整理库文件，返回前后体积（调用方负责先备份）。"""
        path = Path(self.path)
        before = path.stat().st_size if path.exists() else 0
        conn = self._connect(write=True)
        try:
            # 先截断 WAL，否则空闲页仍在 WAL 里，主文件不会缩小。
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        after = path.stat().st_size if path.exists() else 0
        return {"before_bytes": before, "after_bytes": after,
                "reclaimed_bytes": max(0, before - after)}

    def entity_graph(self, subject: str, *, scope: str = "shared",
                     hops: int = 1, limit: int = 200) -> Dict[str, Any]:
        """实体图谱：claim 主语为节点，memory_relations 因果/关联为边。

        从指定主语出发做有界 BFS（1–2 跳）。节点带活跃 claim 数与记忆数；
        边保留关系类型与置信度。匹配大小写不敏感；生产级别名合并留待
        后续（当前精确规范化匹配）。
        """
        subject = _short(subject, 256)
        if not subject:
            raise LedgerError("entity_graph 需要 subject")
        hops = _clamp_int(hops, 1, 1, 2)
        limit = _clamp_int(limit, 200, 10, 2_000)
        key = subject.strip().lower()
        with self._reading() as conn:
            # 图谱可见 claim：active + conflict（冲突实体必须可见，不能假装
            # 不存在）；superseded/retired 仍排除。节点带 conflict 计数。
            subj_rows = conn.execute(
                "SELECT c.memory_id, c.subject, c.state FROM memory_claims c "
                "WHERE c.scope=? AND c.state IN ('active','conflict')",
                (_short(scope, 128) or "shared",)).fetchall()
        mem_subjects: Dict[str, set] = {}
        display: Dict[str, str] = {}
        conflicts: Dict[str, int] = {}
        for row in subj_rows:
            s = str(row["subject"] or "")
            if not s:
                continue
            k = s.strip().lower()
            mem_subjects.setdefault(row["memory_id"], set()).add(k)
            display.setdefault(k, s)
            if str(row["state"]) == "conflict":
                conflicts[k] = conflicts.get(k, 0) + 1
        if key not in display:
            return {"subject": subject, "found": False, "hops": hops,
                    "nodes": [], "edges": []}
        # 关系边（仅两端都有活跃 claim 的记忆对）
        with self._reading() as conn:
            rel_rows = conn.execute(
                "SELECT r.parent_memory_id,r.child_memory_id,"
                "r.relation_type,r.confidence "
                "FROM memory_relations r LIMIT 20000").fetchall()
        adj: Dict[str, Dict[str, Dict[str, float]]] = {}
        edge_records: Dict[tuple, Dict[str, Any]] = {}
        for row in rel_rows:
            ps = mem_subjects.get(str(row["parent_memory_id"]), set())
            cs = mem_subjects.get(str(row["child_memory_id"]), set())
            if not ps or not cs:
                continue
            for a in ps:
                for b in cs:
                    if a == b:
                        continue
                    ek = (a, b)
                    rec = edge_records.get(ek)
                    if rec is None or float(row["confidence"]) > rec["confidence"]:
                        edge_records[ek] = {
                            "from": display[a], "to": display[b],
                            "relation_type": str(row["relation_type"]),
                            "confidence": round(float(row["confidence"]), 3)}
                    adj.setdefault(a, {}).setdefault(b, 0.0)
                    adj[a][b] = max(adj[a].get(b, 0.0), float(row["confidence"]))
        # BFS
        visited = {key}
        frontier = [key]
        node_claims: Dict[str, int] = {}
        node_memories: Dict[str, set] = {}
        for mid, ks in mem_subjects.items():
            for k in ks:
                node_claims[k] = node_claims.get(k, 0) + 1
                node_memories.setdefault(k, set()).add(mid)
        collected_edges: List[Dict[str, Any]] = []
        seen_edges: set = set()
        for _ in range(hops):
            next_frontier: List[str] = []
            for node in frontier:
                for nb in sorted(adj.get(node, {}), key=lambda x: -adj[node][x]):
                    ek = (node, nb)
                    if ek not in seen_edges:
                        seen_edges.add(ek)
                        collected_edges.append(edge_records[ek])
                    if nb not in visited and len(visited) < limit:
                        visited.add(nb)
                        next_frontier.append(nb)
            frontier = next_frontier
            if not frontier:
                break
        nodes = [{"subject": display[k],
                  "claims": node_claims.get(k, 0),
                  "conflicts": conflicts.get(k, 0),
                  "memories": len(node_memories.get(k, set())),
                  "hop": 0 if k == key else None}
                 for k in sorted(visited)]
        return {"subject": display[key], "found": True, "hops": hops,
                "nodes": nodes, "edges": collected_edges[:limit]}

    def recent_subjects(self, *, scope: str = "shared",
                        limit: int = 400) -> List[Dict[str, Any]]:
        """最近活跃的 claim 主语聚合（GUI 实体图层索引用，只读有界）。"""
        limit = _clamp_int(limit, 400, 1, 2000)
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT subject, COUNT(*) AS claims, "
                "SUM(CASE WHEN state='conflict' THEN 1 ELSE 0 END) AS conflicts "
                "FROM memory_claims WHERE scope=? AND state IN ('active','conflict') "
                "GROUP BY subject ORDER BY MAX(rowid) DESC LIMIT ?",
                (_short(scope, 128) or "shared", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_conflicts(self, *, scope: str = "shared",
                       limit: int = 100) -> Dict[str, Any]:
        """冲突与取代谱系：全部非 active 状态的 claim（人工裁决入口）。"""
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT c.claim_id,c.subject,c.predicate,c.slot_key,c.state,"
                "c.confidence,c.memory_id,m.content,m.quality_state "
                "FROM memory_claims c LEFT JOIN memories m ON m.memory_id=c.memory_id "
                "WHERE c.scope=? AND c.state IN ('conflict','superseded') "
                "ORDER BY c.rowid DESC LIMIT ?",
                (_short(scope, 128) or "shared", _clamp_int(limit, 100, 1, 1000)),
            ).fetchall()
        items = [dict(r) for r in rows]
        return {"scope": scope, "total": len(items), "items": items}

    def health_overview(self) -> Dict[str, Any]:
        with self._reading() as conn:
            memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            quarantined = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE quality_state='quarantine'").fetchone()[0]
            pinned = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE pinned=1").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
            fts_rows = conn.execute(
                "SELECT COUNT(*) FROM memory_fts").fetchone()[0]
            # 审计口径必须与 _rebuild_fts 的不变量一致：除「raw 且非 active」
            # 外全部入索引。用全表做分母会在 raw 归档占多数的真实库上永远误报。
            expected_indexed = conn.execute(
                "SELECT COUNT(*) FROM memories "
                "WHERE NOT (raw_chat=1 AND lifecycle!='active')").fetchone()[0]
            try:
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                db_bytes = int(page_size) * int(page_count)
            except Exception:
                db_bytes = 0
        # FTS 体积审计：对照应索引集合检测漂移；占比供容量规划。
        drift = int(fts_rows) != int(expected_indexed)
        return {"memories": int(memories), "quarantined": int(quarantined),
                "pinned": int(pinned), "events": int(events),
                "fts": {"rows": int(fts_rows),
                        "expected": int(expected_indexed),
                        "drift": drift,
                        "db_bytes": db_bytes,
                        "note": ("FTS 行数与应索引集合不一致，建议 checkup --repair"
                                 if drift else "ok")},
                "schema_version": SCHEMA_VERSION}

    @staticmethod
    def _dup_fingerprint(content: str) -> str:
        """归一化：仅去空白与标点；保留数字——「端口5432」≠「端口6432」。"""
        import re as _re
        normalized = _re.sub(r"[\s\W]+", "", str(content or "").lower(), flags=_re.UNICODE)
        return normalized

    def duplicate_candidates(self, *, limit: int = 50, sample: int = 4000,
                             min_similarity: float = 0.85) -> Dict[str, Any]:
        """近似重复记忆对报告（只读，供人工合并决策）。

        指纹分桶（归一化内容前 24 字符）后桶内做字符 Jaccard 相似度，
        避免全库两两比较。证据不同导致 content_hash 不同、得以共存的
        重复陈述会被标出。
        """
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT memory_id,content,kind,scope,created_at FROM memories "
                "WHERE quality_state='accepted' AND lifecycle IN ('active','cold') "
                "ORDER BY updated_at DESC LIMIT ?",
                (_clamp_int(sample, 4000, 100, 50_000),),
            ).fetchall()
        buckets: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            fp = self._dup_fingerprint(row["content"])
            if len(fp) < 8:
                continue
            buckets.setdefault(fp[:24], []).append(row)
        pairs: List[Dict[str, Any]] = []

        def jaccard(a: str, b: str) -> float:
            sa = {a[i:i + 3] for i in range(max(1, len(a) - 2))}
            sb = {b[i:i + 3] for i in range(max(1, len(b) - 2))}
            union = sa | sb
            return len(sa & sb) / len(union) if union else 0.0

        for group in buckets.values():
            if len(group) < 2 or len(pairs) >= limit:
                continue
            if len(group) > 200:
                # 病态大桶（如模板化日志）只抽样比较，避免 O(n²) 爆炸。
                group = group[:200]
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if len(pairs) >= limit:
                        break
                    sim = jaccard(self._dup_fingerprint(group[i]["content"]),
                                  self._dup_fingerprint(group[j]["content"]))
                    if sim >= min_similarity:
                        pairs.append({
                            "a": group[i]["memory_id"], "b": group[j]["memory_id"],
                            "similarity": round(sim, 4),
                            "preview": str(group[i]["content"])[:60],
                        })
        return {"pairs": pairs, "scanned": len(rows), "suggestions": len(pairs)}

    def collapse_duplicate_rows(self, *, archive_dir: Optional[os.PathLike] = None,
                                batch_size: int = 5_000,
                                dry_run: bool = False) -> Dict[str, Any]:
        """Collapse rows whose normalised content is byte-identical.

        The capture storm stored the same message repeatedly; the live ledger held
        1,404,190 redundant raw rows across 39,345 duplicate groups.  Retrieval
        only has an 8-item budget, so duplicates burn it.

        Safety rules:
          * Only *raw evidence* is collapsed.  Structured records keep bitemporal
            versions even when the text repeats.
          * The survivor is the earliest row (stable, reproducible ordering).
          * Every removed row is first written to the same verified gzip archive
            used by :meth:`purge_expired_raw`, so nothing becomes unrecoverable.
          * ``dry_run`` reports the exact counts and touches nothing.

        Returns counts only.
        """
        batch_size = _clamp_int(batch_size, 5_000, 100, 50_000)
        summary: Dict[str, Any] = {"duplicate_groups": 0, "collapsed": 0,
                                   "archive_path": "", "dry_run": bool(dry_run)}
        self._transaction(lambda conn: conn.execute("""
            CREATE TABLE IF NOT EXISTS _dedupe_groups AS
            SELECT MAX(memory_id) AS survivor_id, content, source_agent, session_id,
                   role, COUNT(*) AS n
            FROM memories WHERE raw_chat=1 AND content!=''
            GROUP BY content, source_agent, session_id, role HAVING COUNT(*)>1
        """))
        try:
            with self._connect() as conn:
                summary["duplicate_groups"] = int(conn.execute(
                    "SELECT COUNT(*) FROM _dedupe_groups").fetchone()[0])
            if dry_run or not summary["duplicate_groups"]:
                return summary

            root = self._raw_archive_root(archive_dir)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive_path = root / f"dedupe-{stamp}-{_hash(str(self.path))[:12]}.jsonl.gz"
            removed: List[str] = []
            removed_events: List[str] = []
            with gzip.open(archive_path, "wb") as handle:
                after = ""
                while True:
                    with self._connect() as conn:
                        batch = conn.execute("""
                            SELECT m.memory_id,m.origin_event_id,m.content,m.source_agent,
                                   m.session_id,m.task_id,m.project,m.role,m.scope,
                                   m.created_at,e.occurred_at,e.content_hash
                            FROM memories AS m
                            JOIN ledger_events AS e ON e.event_id=m.origin_event_id
                            JOIN _dedupe_groups g
                              ON g.content=m.content
                             AND g.source_agent=m.source_agent
                             AND g.session_id=m.session_id
                             AND g.role=m.role
                            WHERE m.raw_chat=1 AND m.memory_id!=g.survivor_id
                              AND m.memory_id>?
                            ORDER BY m.memory_id ASC LIMIT ?
                        """, (after, batch_size)).fetchall()
                    if not batch:
                        break
                    after = str(batch[-1]["memory_id"])
                    for row in batch:
                        handle.write((json.dumps({
                            "reason": "duplicate",
                            "memory_id": row["memory_id"],
                            "event_id": row["origin_event_id"],
                            "content": row["content"],
                            "content_sha256": row["content_hash"],
                            "source_agent": row["source_agent"],
                            "session_id": row["session_id"],
                            "created_at": row["created_at"],
                            "collapsed_at": utc_now(),
                        }, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                        removed.append(str(row["memory_id"]))
                        removed_events.append(str(row["origin_event_id"]))
            try:
                os.chmod(archive_path, 0o600)
            except OSError:
                pass
            with open(archive_path, "rb") as handle:
                os.fsync(handle.fileno())

            # Verify before deleting (same fail-closed rule as purge).
            found = set()
            with gzip.open(archive_path, "rb") as handle:
                for line in handle:
                    if line.strip():
                        found.add(str(json.loads(line).get("memory_id", "")))
            if set(removed) - found:
                summary["error"] = "archive incomplete; refusing to delete"
                return summary

            def remove(conn: sqlite3.Connection) -> None:
                for start in range(0, len(removed), batch_size):
                    chunk = removed[start:start + batch_size]
                    events = removed_events[start:start + batch_size]
                    ph = ",".join("?" for _ in chunk)
                    eph = ",".join("?" for _ in events)
                    conn.execute(f"DELETE FROM memory_fts WHERE memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM memory_relations WHERE child_memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM memory_relations WHERE parent_memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM memory_claims WHERE memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM memories WHERE memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM ledger_events WHERE event_id IN ({eph})", events)

            self._transaction(remove)
            summary["collapsed"] = len(removed)
            summary["archive_path"] = str(archive_path)
            return summary
        finally:
            self._transaction(lambda conn: conn.execute("DROP TABLE IF EXISTS _dedupe_groups"))

    def set_agent_trust(self, agent_id: str, baseline: float, *, source: str = "local-admin") -> Dict[str, Any]:
        """Set an operator-controlled trust prior; agents cannot self-promote."""
        agent_id = _short(agent_id, 128).lower()
        if not agent_id:
            raise LedgerError("agent_id 不能为空")
        baseline = _clamp_float(baseline, DEFAULT_AGENT_TRUST, 0.0, 1.0)
        source = _short(source, 128) or "local-admin"
        now = utc_now()

        def write(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute("""INSERT INTO agent_trust(agent_id,baseline,updated_at,source) VALUES(?,?,?,?)
                            ON CONFLICT(agent_id) DO UPDATE SET baseline=excluded.baseline,
                            updated_at=excluded.updated_at,source=excluded.source""",
                         (agent_id, baseline, now, source))
            return {"agent_id": agent_id, "baseline": baseline, "updated_at": now, "source": source}

        return self._transaction(write)

    def get_agent_trust(self, agent_id: str) -> float:
        agent_id = _short(agent_id, 128).lower()
        with self._reading() as conn:
            return self._agent_trust_for(conn, agent_id)

    def list_claims(self, *, memory_id: str = "", scope: str = "shared",
                    state: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        clauses = ["scope=?"]
        params: List[object] = [_short(scope, 128) or "shared"]
        if memory_id:
            clauses.append("memory_id=?")
            params.append(_short(memory_id, 80))
        if state:
            clauses.append("state=?")
            params.append(_short(state, 32))
        limit = _clamp_int(limit, 200, 1, 2_000)
        with self._reading() as conn:
            rows = conn.execute("SELECT * FROM memory_claims WHERE " + " AND ".join(clauses) +
                                " ORDER BY asserted_at DESC LIMIT ?", (*params, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["value"] = json.loads(item.pop("value_json") or "null")
            except (TypeError, ValueError):
                item["value"] = None
                item.pop("value_json", None)
            result.append(item)
        return result

    def causal_ancestors(self, memory_ids: Sequence[str], *, max_depth: int = 2,
                         limit: int = 32, scope: str = "", require_evidence: bool = False,
                         include_raw_chat: bool = True) -> List[Dict[str, Any]]:
        """Return a bounded, visibility-filtered parent closure.

        Causal provenance is useful only when it observes the same visibility
        and evidence contract as the Context Pack that requested it.
        """
        ids = [_short(value, 80) for value in memory_ids if _short(value, 80)]
        if not ids:
            return []
        max_depth = _clamp_int(max_depth, 2, 1, 4)
        limit = _clamp_int(limit, 32, 1, 128)
        scope = _short(scope, 128)
        placeholders = ",".join("?" for _ in ids)
        filters = ["m.lifecycle IN ('active','cold')", "m.quality_state='accepted'",
                   "(m.valid_until='' OR m.valid_until>?)",
                   "(m.valid_from='' OR m.valid_from<=?)"]
        params: List[object] = [*ids, max_depth, utc_now(), utc_now()]
        if scope:
            filters.append("m.scope=?")
            params.append(scope)
        if require_evidence:
            filters.append("m.evidence_json!='[]'")
        if not include_raw_chat:
            filters.append("m.raw_chat=0")
        params.append(limit)
        with self._reading() as conn:
            rows = conn.execute(f"""
                WITH RECURSIVE ancestors(memory_id,depth) AS (
                    SELECT parent_memory_id,1 FROM memory_relations
                    WHERE child_memory_id IN ({placeholders})
                    UNION
                    SELECT r.parent_memory_id,a.depth+1 FROM memory_relations r JOIN ancestors a
                      ON r.child_memory_id=a.memory_id WHERE a.depth<?
                ), unique_ancestors AS (
                    SELECT memory_id,MIN(depth) AS causal_depth FROM ancestors GROUP BY memory_id
                )
                SELECT m.*,a.causal_depth
                FROM unique_ancestors a CROSS JOIN memories m
                WHERE m.memory_id=a.memory_id
                  AND {' AND '.join(filters)}
                ORDER BY a.causal_depth ASC, m.updated_at DESC LIMIT ?
            """, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = self._row_dict(row)
            item["causal_depth"] = int(row["causal_depth"] or 1)
            item["score"] = max(0.0, 0.45 - 0.08 * (item["causal_depth"] - 1))
            item["score_parts"] = {"causal": item["score"]}
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # Watcher cursors, reporting, backup and compatibility JSON
    # ------------------------------------------------------------------

    def list_cursors(self, *, prefix: str = "",
                     limit: int = 10_000) -> List[Dict[str, Any]]:
        """枚举游标行（source_id 前缀过滤），供维护性清理使用。"""
        limit = _clamp_int(limit, 10_000, 1, 100_000)
        with self._reading() as conn:
            if prefix:
                rows = conn.execute(
                    "SELECT source_id,metadata_json FROM source_cursors "
                    "WHERE source_id LIKE ? ORDER BY source_id LIMIT ?",
                    (prefix + "%", limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT source_id,metadata_json FROM source_cursors "
                    "ORDER BY source_id LIMIT ?", (limit,)).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            result.append({"source_id": row["source_id"],
                           "metadata": metadata if isinstance(metadata, dict) else {}})
        return result

    def delete_cursor(self, source_id: str) -> bool:
        """删除一条游标行（源文件已消失时的维护清理）。"""
        source_id = _short(source_id, 256)
        if not source_id:
            return False

        def write(conn: sqlite3.Connection) -> bool:
            cur = conn.execute("DELETE FROM source_cursors WHERE source_id=?",
                               (source_id,))
            return cur.rowcount > 0

        return self._transaction(write)

    def get_cursor(self, source_id: str) -> Dict[str, Any]:
        source_id = _short(source_id, 256)
        with self._reading() as conn:
            row = conn.execute("SELECT * FROM source_cursors WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            return {"source_id": source_id, "cursor": "", "updated_at": "", "last_success_at": "",
                    "last_error": "", "metadata": {}}
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        except (TypeError, ValueError):
            data["metadata"] = {}
        return data

    def set_cursor(self, source_id: str, cursor: str, *, error: str = "",
                   metadata: Optional[Mapping[str, Any]] = None) -> None:
        source_id = _short(source_id, 256)
        if not source_id:
            raise LedgerError("source_id 不能为空")
        cursor = _short(cursor, 4_000)
        error = _short(error, 2_000)
        now = utc_now()
        metadata_json = _json(_mapping(metadata or {}), "cursor metadata")

        def write(conn: sqlite3.Connection) -> None:
            conn.execute("""
                INSERT INTO source_cursors(source_id,cursor,updated_at,last_success_at,last_error,metadata_json)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at,
                last_success_at=CASE WHEN excluded.last_error='' THEN excluded.updated_at ELSE source_cursors.last_success_at END,
                last_error=excluded.last_error,metadata_json=excluded.metadata_json
            """, (source_id, cursor, now, now if not error else "", error, metadata_json))

        self._transaction(write)

    # ------------------------------------------------------------------
    # Scoped local agent credentials
    # ------------------------------------------------------------------

    @staticmethod
    def _credential_values(agent_id: object, scopes: Optional[Sequence[str]],
                           permissions: Optional[Sequence[str]]) -> Tuple[str, List[str], List[str]]:
        agent = _short(agent_id, 128)
        if not agent:
            raise LedgerError("agent_id 不能为空")
        cleaned_scopes = _strings(scopes or (), 64, 128, "scopes")
        if not cleaned_scopes:
            cleaned_scopes = ["shared", "agent:" + agent]
        cleaned_permissions = _strings(permissions or (), 16, 64, "permissions")
        if not cleaned_permissions:
            cleaned_permissions = ["read", "ingest", "feedback", "stats"]
        invalid = [item for item in cleaned_permissions if item not in AGENT_PERMISSIONS]
        if invalid:
            raise LedgerError("不支持的 agent permission: " + ", ".join(invalid))
        return agent, cleaned_scopes, cleaned_permissions

    def issue_agent_credential(self, agent_id: str, *, scopes: Optional[Sequence[str]] = None,
                               permissions: Optional[Sequence[str]] = None,
                               token: str = "") -> Tuple[AgentCredential, str]:
        """Create a scoped bearer token and return its plaintext only once."""
        agent, cleaned_scopes, cleaned_permissions = self._credential_values(agent_id, scopes, permissions)
        raw_token = _text(token, 512, "agent token") if token else secrets.token_urlsafe(32)
        if len(raw_token) < 24:
            raise LedgerError("agent token 至少需要 24 个字符")
        token_hash = _hash("agent-token", raw_token)
        token_id = "agt_" + _hash(agent, token_hash)[:24]
        now = utc_now()
        credential = AgentCredential(token_id, agent, tuple(cleaned_scopes), tuple(cleaned_permissions), now)

        def write(conn: sqlite3.Connection) -> None:
            existing = conn.execute("SELECT token_id FROM agent_credentials WHERE token_hash=?", (token_hash,)).fetchone()
            if existing is not None:
                raise LedgerError("agent token 已存在；请生成新 token")
            conn.execute("""
                INSERT INTO agent_credentials(token_id,token_hash,agent_id,scopes_json,permissions_json,created_at,revoked_at)
                VALUES(?,?,?,?,?,?, '')
            """, (token_id, token_hash, agent, _json(cleaned_scopes, "scopes"),
                  _json(cleaned_permissions, "permissions"), now))

        self._transaction(write)
        return credential, raw_token

    def resolve_agent_credential(self, token: str) -> Optional[AgentCredential]:
        token = _text(token, 512, "agent token")
        if not token:
            return None
        token_hash = _hash("agent-token", token)
        with self._reading() as conn:
            row = conn.execute("""
                SELECT token_id,agent_id,scopes_json,permissions_json,created_at
                FROM agent_credentials WHERE token_hash=? AND revoked_at=''
            """, (token_hash,)).fetchone()
        if row is None:
            return None
        try:
            scopes = _strings(json.loads(row["scopes_json"]), 64, 128, "scopes")
            permissions = _strings(json.loads(row["permissions_json"]), 16, 64, "permissions")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not scopes or any(item not in AGENT_PERMISSIONS for item in permissions):
            return None
        return AgentCredential(str(row["token_id"]), str(row["agent_id"]), tuple(scopes),
                               tuple(permissions), str(row["created_at"]))

    def revoke_agent_credential(self, token_id: str) -> bool:
        token_id = _short(token_id, 80)
        if not token_id:
            raise LedgerError("token_id 不能为空")
        now = utc_now()

        def write(conn: sqlite3.Connection) -> bool:
            result = conn.execute("UPDATE agent_credentials SET revoked_at=? WHERE token_id=? AND revoked_at=''",
                                  (now, token_id))
            return bool(result.rowcount)

        return self._transaction(write)

    def stats(self) -> Dict[str, Any]:
        with self._reading() as conn:
            event_count = conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
            rows = conn.execute("SELECT lifecycle,COUNT(*) count FROM memories GROUP BY lifecycle").fetchall()
            kinds = conn.execute("SELECT kind,COUNT(*) count FROM memories GROUP BY kind").fetchall()
            raw = conn.execute("SELECT COUNT(*) FROM memories WHERE raw_chat=1").fetchone()[0]
            agents = conn.execute("SELECT source_agent,COUNT(*) count FROM memories "
                                  "GROUP BY source_agent ORDER BY count DESC LIMIT 100").fetchall()
            cursor_count = conn.execute("SELECT COUNT(*) FROM source_cursors").fetchone()[0]
            credential_count = conn.execute("SELECT COUNT(*) FROM agent_credentials WHERE revoked_at='' ").fetchone()[0]
            claim_count = conn.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0]
            active_claim_count = conn.execute("SELECT COUNT(*) FROM memory_claims WHERE state='active'").fetchone()[0]
            conflict_claim_count = conn.execute(
                "SELECT COUNT(*) FROM memory_claims WHERE state='conflict'").fetchone()[0]
            pinned_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE pinned=1 AND lifecycle!='superseded'").fetchone()[0]
            relation_count = conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]
            trust_agent_count = conn.execute("SELECT COUNT(*) FROM agent_trust").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            version = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        with self._search_lock:
            durations = tuple(self._search_durations)
        lifecycle = {row["lifecycle"]: row["count"] for row in rows}
        archive_root = self.path.parent / (self.path.stem + "." + RAW_ARCHIVE_DIR_NAME)
        archive_files = 0
        archive_bytes = 0
        if archive_root.is_dir() and not archive_root.is_symlink():
            for archive in archive_root.glob("raw-*.jsonl.gz"):
                try:
                    archive_files += 1
                    archive_bytes += archive.stat().st_size
                except OSError:
                    continue
        return {
            "path": str(self.path), "schema_version": int(version), "journal_mode": str(journal).upper(),
            "events": event_count, "memories": sum(lifecycle.values()), "raw_chat": raw,
            "active": lifecycle.get("active", 0), "cold": lifecycle.get("cold", 0),
            "archived": lifecycle.get("cold", 0), "superseded": lifecycle.get("superseded", 0),
            "expired": lifecycle.get("expired", 0), "cursors": cursor_count,
            "agent_credentials": credential_count,
            "claims": claim_count,
            "active_claims": active_claim_count,
            "conflict_claims": conflict_claim_count,
            "pinned": pinned_count,
            "causal_relations": relation_count,
            "trusted_agents": trust_agent_count,
            "search_p95_ms": round(sorted(durations)[
                max(0, math.ceil(len(durations) * 0.95) - 1)] * 1000.0, 1)
                if durations else 0.0,
            "slow_search_count": self._slow_search_count,
            "slow_search_threshold_s": self._slow_search_threshold,
            "raw_policy": {"retention_days": DEFAULT_RAW_RETENTION_DAYS,
                           "capacity": DEFAULT_RAW_CAPACITY,
                           "archive_files": archive_files,
                           "archive_bytes": archive_bytes},
            "agents": {row["source_agent"]: row["count"] for row in agents},
            "kinds": {row["kind"]: row["count"] for row in kinds},
        }

    def export_json(self, path: os.PathLike, max_rows: int = MAX_EXPORT_ROWS, *, include_raw_chat: bool = False) -> Path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        max_rows = _clamp_int(max_rows, MAX_EXPORT_ROWS, 1, MAX_EXPORT_ROWS)
        with self._reading() as conn:
            if include_raw_chat:
                events = [dict(row) for row in conn.execute("SELECT * FROM ledger_events ORDER BY occurred_at LIMIT ?", (max_rows,))]
            else:
                placeholders = ",".join("?" for _ in PORTABLE_EXCLUDED_EVENT_KINDS)
                events = [dict(row) for row in conn.execute(
                    f"SELECT * FROM ledger_events WHERE event_type NOT IN ({placeholders}) ORDER BY occurred_at LIMIT ?",
                    (*sorted(PORTABLE_EXCLUDED_EVENT_KINDS), max_rows))]
        payload = {"hippomind_memory_hub_export": 2, "schema_version": SCHEMA_VERSION,
                   "exported_at": utc_now(), "portable": True,
                   "raw_chat_included": bool(include_raw_chat), "events": events}
        fd, temp_name = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return target

    def import_json(self, path: os.PathLike, source_agent: str = "legacy-json") -> Dict[str, int]:
        source = Path(path).expanduser()
        if not source.exists():
            raise LedgerError(f"JSON 文件不存在: {source}")
        if source.stat().st_size > 128 * 1024 * 1024:
            raise LedgerError("JSON 导入文件超过 128 MiB 安全上限")
        with source.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, Mapping):
            raise LedgerError("JSON 根节点必须是对象")
        imported = duplicate = 0
        if data.get("hippomind_memory_hub_export") in {1, 2}:
            for event in data.get("events", []):
                if not isinstance(event, Mapping) or event.get("event_type") in {"feedback", "supersede"}:
                    continue
                result = self.ingest({
                    "kind": event.get("event_type", "fact"), "content": event.get("content", ""),
                    "source_agent": event.get("source_agent", source_agent),
                    "session_id": event.get("session_id", ""), "task_id": event.get("task_id", ""),
                    "project": event.get("project", ""), "role": event.get("role", ""),
                    "scope": event.get("scope", "shared"), "timestamp": event.get("occurred_at", ""),
                    "evidence": json.loads(event.get("evidence_json", "[]") or "[]"),
                    "metadata": json.loads(event.get("metadata_json", "{}") or "{}"),
                })
                imported += int(result.inserted)
                duplicate += int(not result.inserted)
            return {"imported": imported, "duplicate": duplicate}
        hippo = data.get("hippo_data", data)
        if not isinstance(hippo, Mapping):
            raise LedgerError("未识别的 HippoMind JSON 格式")
        for field, kind, content_key in (("episodic", "episode", "situation"),
                                         ("semantic", "semantic", "rule"),
                                         ("archive", "archive", "situation")):
            values = hippo.get(field, [])
            if isinstance(values, Mapping):
                values = list(values.values())
            if not isinstance(values, list):
                continue
            for entry in values:
                if not isinstance(entry, Mapping):
                    continue
                content = _short(entry.get(content_key, ""), MAX_CONTENT_CHARS)
                if not content:
                    continue
                legacy_id = _short(entry.get("id", ""), 128)
                metadata = {"legacy_json": str(source), "legacy_kind": field,
                            "legacy_record": dict(entry)}
                result = self.ingest({
                    "kind": kind, "content": content, "source_agent": source_agent,
                    "session_id": str(source), "event_id": f"{source.name}:{field}:{legacy_id}",
                    "scope": _short(entry.get("scope", "agent"), 128) or "agent",
                    "timestamp": entry.get("created", ""), "tags": entry.get("tags", []),
                    "weight": entry.get("weight", 0.5), "importance": entry.get("importance", 5),
                    "confidence": entry.get("confidence", 0.5), "utility": entry.get("utility", 0.0),
                    "evidence": ["legacy:" + _hash(str(source), field, legacy_id)[:32]],
                    "metadata": metadata,
                })
                imported += int(result.inserted)
                duplicate += int(not result.inserted)
        return {"imported": imported, "duplicate": duplicate}

    def backup_to(self, path: os.PathLike) -> Path:
        target = Path(path).expanduser()
        if target.exists() and (not target.is_file() or target.is_symlink()):
            raise LedgerError(f"备份目标必须是普通文件: {target}")
        try:
            if target.resolve() == self.path.resolve():
                raise LedgerError("备份目标不能覆盖生产账本")
        except OSError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="." + target.name + ".",
                                         suffix=".tmp", dir=str(target.parent))
        os.close(fd)
        temp = Path(temp_name)
        source_conn = None
        dest_conn = None
        try:
            source_conn = self._connect()
            dest_conn = sqlite3.connect(str(temp))
            source_conn.backup(dest_conn)
            dest_conn.commit()
            integrity = dest_conn.execute("PRAGMA integrity_check").fetchone()[0]
            if str(integrity).lower() != "ok":
                raise LedgerError("SQLite 备份完整性检查失败")
            dest_conn.close()
            dest_conn = None
            source_conn.close()
            source_conn = None
            os.replace(temp, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            return target
        finally:
            if dest_conn is not None:
                dest_conn.close()
            if source_conn is not None:
                source_conn.close()
            # SQLite can create sidecars for the disposable destination even
            # though the final backup is a single atomically replaced file.
            # Only remove names derived from this invocation's unique temp.
            for candidate in (temp, Path(str(temp) + "-wal"), Path(str(temp) + "-shm")):
                try:
                    candidate.unlink()
                except OSError:
                    pass

    def recovery_check(self) -> Dict[str, Any]:
        """Run a disposable backup/open/restore validation drill.

        No live projection is replaced. The returned counts are safe to record
        in health telemetry and prove that WAL pages can be recovered.
        """
        fd, name = tempfile.mkstemp(prefix=".hippomind-recovery-", suffix=".sqlite",
                                    dir=str(self.path.parent))
        os.close(fd)
        temporary = Path(name)
        try:
            self.backup_to(temporary)
            with sqlite3.connect(str(temporary)) as conn:
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower()
                version = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
                events = int(conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0])
            if integrity != "ok" or int(version) > SCHEMA_VERSION:
                raise LedgerError("恢复演练未通过 schema/integrity 校验")
            return {"ok": True, "integrity": integrity, "schema_version": int(version),
                    "events": events, "backup": str(temporary)}
        finally:
            for candidate in (temporary, Path(str(temporary) + "-wal"),
                              Path(str(temporary) + "-shm")):
                try:
                    candidate.unlink()
                except OSError:
                    pass

    def restore_from(self, backup_path: os.PathLike) -> None:
        """Restore a validated SQLite backup into this ledger's projection.

        Callers should stop the daemon first.  Validation happens before the
        destination is touched; SQLite's backup API then performs the copy.
        """
        source_path = Path(backup_path).expanduser()
        if not source_path.exists():
            raise LedgerError(f"备份不存在: {source_path}")
        # A normal absolute-path connection is more portable across the
        # macOS SQLite builds than a hand-built ``file:`` URI. query_only keeps
        # the validated backup source read-only for this operation.
        source = sqlite3.connect(str(source_path))
        source.execute("PRAGMA query_only=ON")
        try:
            row = source.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is None or int(row[0]) > SCHEMA_VERSION:
                raise LedgerError("备份不是兼容的 HippoMind 账本")
            required_tables = {"ledger_events", "memories", "memory_fts", "source_cursors"}
            present = {str(item[0]) for item in source.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}
            if not required_tables.issubset(present):
                raise LedgerError("备份缺少 HippoMind 必要表，拒绝恢复")
            integrity = str(source.execute("PRAGMA integrity_check").fetchone()[0]).lower()
            if integrity != "ok":
                raise LedgerError("备份完整性检查失败，拒绝恢复")
            dest = sqlite3.connect(str(self.path))
            try:
                source.backup(dest)
                dest.commit()
            finally:
                dest.close()
        finally:
            source.close()
        self._bootstrap()
