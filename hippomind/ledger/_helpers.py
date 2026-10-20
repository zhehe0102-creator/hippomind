"""SQLite/WAL event ledger for the long-running HippoMind Memory Hub.

The original Grid World hippocampus keeps its compact JSON snapshot for the
interactive experiment.  This module is deliberately separate: it is an
append-only, cross-agent evidence ledger whose mutable memory rows are only a
projection of immutable events.  Lifecycle maintenance can archive or
supersede a memory projection, but it never silently removes raw evidence.
"""

from __future__ import annotations  # noqa: F401 (mixin re-export surface)

import hashlib  # noqa: F401 (mixin re-export surface)
import gzip  # noqa: F401 (mixin re-export surface)
import copy  # noqa: F401 (mixin re-export surface)
import json  # noqa: F401 (mixin re-export surface)
import math  # noqa: F401 (mixin re-export surface)
import time as _time_mod  # noqa: F401 (mixin re-export surface)
import os  # noqa: F401 (mixin re-export surface)
import re  # noqa: F401 (mixin re-export surface)
import secrets  # noqa: F401 (mixin re-export surface)
import sqlite3  # noqa: F401 (mixin re-export surface)
import tempfile  # noqa: F401 (mixin re-export surface)
import threading  # noqa: F401 (mixin re-export surface)
import time  # noqa: F401 (mixin re-export surface)
import sys  # noqa: F401 (mixin re-export surface)
from contextlib import contextmanager  # noqa: F401 (mixin re-export surface)
from dataclasses import dataclass  # noqa: F401 (mixin re-export surface)
from datetime import datetime, timezone  # noqa: F401 (mixin re-export surface)
from pathlib import Path  # noqa: F401 (mixin re-export surface)
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple  # noqa: F401 (mixin re-export surface)
from collections import deque  # noqa: F401 (mixin re-export surface)

from ..textutil import (  # noqa: F401 (mixin re-export surface)
    content_fingerprint,
    informative_ratio,
    is_noise_text,
    normalise_for_dedup,
    token_sim,
)
from ..sanitize import MAX_ARCHIVE_MESSAGE_CHARS, sanitize_chat_content  # noqa: F401 (mixin re-export surface)
from .. import schema as _schema


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
