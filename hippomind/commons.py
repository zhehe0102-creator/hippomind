"""全电脑 Agent 记忆共通层。

原始聊天继续由 Agent-Chat-Archive 管理；本模块以只读方式实时检索其
SQLite 索引，并用独立的共享语义账本交换经过提炼、带证据的记忆。
全量聊天不复制进每个 Agent 的活跃海马体，避免容量失控和来源丢失。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .textutil import token_sim

MAX_QUERY_CHARS = 2_000
MAX_SHARED_CONTENT_CHARS = 12_000
MAX_SHARED_REFS = 64
MAX_SHARED_TAGS = 32
MAX_SHARED_METADATA_CHARS = 32_000
#: 对账分桶名（顺序即报告顺序）。
RECONCILE_BUCKETS: Sequence[str] = (
    "missing_in_ledger", "missing_in_commons", "stale", "window_mismatch")
DEFAULT_RECONCILE_SAMPLES = 10
_SOURCE_REF_RE = re.compile(
    r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9._:-]{1,240}$", re.IGNORECASE)


DEFAULT_ARCHIVES = (
    Path.home() / "Library/Application Support/Agent-Chat-Archive",
    Path("/Volumes/移动硬盘/Agent-Chat-Archive"),
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(*parts: object) -> str:
    raw = "\x1f".join(str(p or "") for p in parts).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def _short_text(value, limit: int) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()[:limit]
    except Exception:
        return ""


def _finite_float(value, default: float = 0.0, lower: float = -1.0,
                  upper: float = 1.0) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(lower, min(upper, number))


def _bounded_int(value, default: int = 0, lower: int = 0,
                 upper: int = 1_000_000_000) -> int:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(lower, min(upper, number))


def _clean_strings(values, maximum: int, limit: int) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    try:
        raw = list(values)
    except TypeError:
        return []
    return sorted({_short_text(item, limit) for item in raw if _short_text(item, limit)})[:maximum]


def _normalise_refs(values) -> List[str]:
    refs = _clean_strings(values, MAX_SHARED_REFS, 256)
    invalid = [ref for ref in refs if not _SOURCE_REF_RE.fullmatch(ref)]
    if invalid:
        raise ValueError("source_ref 必须是 '<来源>:<稳定标识>'，且不能包含空白字符")
    return refs


def _safe_metadata(value) -> Dict:
    if not isinstance(value, dict):
        return {}
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str,
                             allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return {"_invalid_metadata": True}
    if len(encoded) > MAX_SHARED_METADATA_CHARS:
        return {"_truncated_metadata": True}
    try:
        decoded = json.loads(encoded)
    except (TypeError, ValueError):
        return {"_invalid_metadata": True}
    return decoded if isinstance(decoded, dict) else {}


def _normalise_valid_until(value) -> str:
    text = _short_text(value, 64)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("valid_until 必须是 ISO-8601 时间，例如 2026-12-31T00:00:00Z")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _json_list(value) -> List[str]:
    if isinstance(value, list):
        return _clean_strings(value, MAX_SHARED_REFS, 256)
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return _clean_strings(parsed, MAX_SHARED_REFS, 256) if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _terms(query: str, maximum: int = 10) -> List[str]:
    """生成适合 LIKE 候选召回的少量多语言词/bigram。"""
    query = _short_text(query, MAX_QUERY_CHARS)
    out: List[str] = []
    out.extend(re.findall(r"[a-z0-9_]{2,}", query.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        if len(run) <= 2:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    unique = []
    for term in out:
        if term not in unique:
            unique.append(term)
    return unique[:maximum]


def _parse_time(value: str) -> float:
    if not value:
        return 0.0
    try:
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _is_noise(content: str) -> bool:
    """过滤收集器中的运行时样板；原始记录仍完整保留在归档。"""
    text = content.lstrip().lower()
    prefixes = (
        "the following is the codex agent history",
        "<environment_context>",
        "<app-context>",
        "<permissions instructions>",
        "<turn_aborted>",
        "# agents.md instructions",
        "[思考]",
    )
    return not text or any(text.startswith(prefix) for prefix in prefixes)


@dataclass
class CommonsMessage:
    id: str
    agent: str
    session_id: str
    event_id: str
    ts: str
    role: str
    content: str
    model: str = ""
    cwd: str = ""
    title: str = ""
    source_file: str = ""
    tags: List[str] = field(default_factory=list)
    source: str = "agent-chat-archive"
    scope: str = "shared"
    valence: float = 0.0
    arousal: float = 0.3
    importance: int = 5
    weight: float = 0.5
    confidence: float = 0.65
    utility: float = 0.0

    @property
    def situation(self) -> str:
        return self.content

    @property
    def action(self) -> str:
        return ""

    @property
    def outcome(self) -> str:
        return ""


@dataclass
class SharedMemory:
    id: str
    content: str
    kind: str
    source_agent: str
    source_session: str
    source_refs: List[str]
    tags: List[str]
    scope: str
    valence: float
    confidence: float
    utility: float
    importance: int
    created_at: str
    updated_at: str
    valid_until: str = ""
    superseded_by: str = ""
    success_count: int = 0
    failure_count: int = 0
    metadata: Dict = field(default_factory=dict)
    source: str = "hippomind-commons"
    weight: float = 0.7
    arousal: float = 0.3

    @property
    def situation(self) -> str:
        return self.content

    @property
    def action(self) -> str:
        return ""

    @property
    def outcome(self) -> str:
        return ""

    @property
    def agent(self) -> str:
        return self.source_agent

    @property
    def role(self) -> str:
        return self.kind


@dataclass
class CommonsHit:
    score: float
    memory: object
    origin: str


class MemoryCommons:
    """连接全机原始聊天和跨 Agent 共享语义记忆。"""

    def __init__(self, archive: Optional[str] = None):
        self.archive_root = self._resolve_archive(archive)
        self.chat_db = self.archive_root / "index/index.sqlite"
        self.shared_db = self.archive_root / "index/hippomind-commons.sqlite"
        # 最近一次对账摘要（仅内存；stats() 暴露，不引入任何额外 IO）。
        self._last_reconcile: Optional[Dict[str, object]] = None
        if not self.chat_db.exists():
            raise FileNotFoundError(f"Agent 聊天索引不存在: {self.chat_db}")

    @staticmethod
    def _resolve_archive(explicit: Optional[str]) -> Path:
        candidates: List[Path] = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        for name in ("HIPPOMIND_COMMONS", "AGENT_ARCHIVE"):
            if os.environ.get(name):
                candidates.append(Path(os.environ[name]).expanduser())
        candidates.extend(DEFAULT_ARCHIVES)
        for candidate in candidates:
            if candidate.name == "index.sqlite":
                candidate = candidate.parent.parent
            if (candidate / "index/index.sqlite").exists():
                return candidate
        attempted = ", ".join(str(p) for p in candidates)
        raise FileNotFoundError(f"未找到 Agent-Chat-Archive，已检查: {attempted}")

    @contextmanager
    def _chat_connection(self):
        conn = sqlite3.connect(f"file:{self.chat_db}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
        except sqlite3.DatabaseError:
            pass
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _shared_connection(self, write: bool = False):
        if not write and not self.shared_db.exists():
            raise FileNotFoundError(self.shared_db)
        if write:
            conn = sqlite3.connect(self.shared_db, timeout=10.0)
            conn.execute("PRAGMA busy_timeout=10000")
        else:
            conn = sqlite3.connect(f"file:{self.shared_db}?mode=ro", uri=True, timeout=5.0)
            conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
        except sqlite3.DatabaseError:
            pass
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _shared_journal_mode(self) -> str:
        return "DELETE" if str(self.shared_db).startswith("/Volumes/") else "WAL"

    def initialize(self) -> Path:
        """创建跨 Agent 共享语义账本；不修改原始聊天索引。"""
        self.shared_db.parent.mkdir(parents=True, exist_ok=True)
        with self._shared_connection(write=True) as conn:
            # macOS 外置盘经常是 exFAT，不支持 SQLite WAL 所需的共享内存锁。
            # 主归档使用 WAL；/Volumes 下的便携镜像使用兼容的回滚日志。
            journal = self._shared_journal_mode()
            conn.execute(f"PRAGMA journal_mode={journal}")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shared_memories (
                    memory_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_agent TEXT NOT NULL,
                    source_session TEXT,
                    source_refs TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    valence REAL NOT NULL,
                    confidence REAL NOT NULL,
                    utility REAL NOT NULL,
                    importance INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    valid_until TEXT,
                    superseded_by TEXT,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shared_scope ON shared_memories(scope)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shared_updated ON shared_memories(updated_at)")
            conn.commit()
        try:
            os.chmod(self.shared_db, 0o600)
        except OSError:
            pass
        return self.shared_db

    def stats(self) -> Dict:
        with self._chat_connection() as conn:
            rows = conn.execute(
                "SELECT agent, COUNT(*) messages, COUNT(DISTINCT session_id) sessions "
                "FROM messages GROUP BY agent ORDER BY agent"
            ).fetchall()
        agents = {r["agent"]: {"messages": r["messages"], "sessions": r["sessions"]}
                  for r in rows}
        shared = 0
        if self.shared_db.exists():
            with self._shared_connection() as conn:
                shared = conn.execute("SELECT COUNT(*) FROM shared_memories").fetchone()[0]
        last_reconcile = None
        if isinstance(self._last_reconcile, dict):
            last_reconcile = dict(self._last_reconcile)
            if isinstance(last_reconcile.get("counts"), dict):
                last_reconcile["counts"] = dict(last_reconcile["counts"])
        return {
            "archive": str(self.archive_root),
            "agents": agents,
            "messages": sum(v["messages"] for v in agents.values()),
            "sessions": sum(v["sessions"] for v in agents.values()),
            "shared_memories": shared,
            # 仅内存摘要：本进程最近一次 compare/repair 的计数（进程重启即清空）。
            "last_reconcile": last_reconcile,
        }

    def search_messages(self, query: str, limit: int = 10,
                        agents: Optional[Sequence[str]] = None,
                        roles: Optional[Sequence[str]] = ("user", "assistant"),
                        session_id: Optional[str] = None,
                        candidate_limit: int = 800) -> List[CommonsHit]:
        query = _short_text(query, MAX_QUERY_CHARS)
        limit = _bounded_int(limit, 10, 0, 100)
        terms = _terms(query)
        if not terms or limit <= 0:
            return []
        agents = _clean_strings(agents, 64, 128) if agents else []
        roles = _clean_strings(roles, 16, 64) if roles else []
        session_id = _short_text(session_id, 256) if session_id else ""
        candidate_limit = max(
            limit, _bounded_int(candidate_limit, 800, 10, 5_000))
        clauses = ["(" + " OR ".join("LOWER(content) LIKE ?" for _ in terms) + ")"]
        params: List[object] = [f"%{term}%" for term in terms]
        if agents:
            clauses.append("agent IN (" + ",".join("?" for _ in agents) + ")")
            params.extend(agents)
        if roles:
            clauses.append("role IN (" + ",".join("?" for _ in roles) + ")")
            params.extend(roles)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        params.append(candidate_limit)
        sql = (
            "SELECT agent,session_id,event_id,ts,role,content,model,cwd,title,source_file "
            "FROM messages WHERE " + " AND ".join(clauses) + " ORDER BY ts DESC LIMIT ?"
        )
        with self._chat_connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        now = datetime.now(timezone.utc).timestamp()
        seen = set()
        hits = []
        query_lower = query.lower().strip()
        for row in rows:
            content = _short_text(row["content"], 24_000)
            if _is_noise(content):
                continue
            # 收集器可能保留同一消息的两个事件表示；检索时按会话/角色/正文折叠。
            duplicate_key = _digest(row["agent"], row["session_id"], row["role"], content.strip())
            if duplicate_key in seen:
                continue
            seen.add(duplicate_key)
            lowered = content.lower()
            coverage = sum(1 for term in terms if term in lowered) / len(terms)
            exact = 1.0 if query_lower and query_lower in lowered else 0.0
            similarity = token_sim(query, content[:6000])
            age_days = max(0.0, now - _parse_time(row["ts"])) / 86400.0
            recency = 1.0 / (1.0 + age_days / 120.0)
            role_quality = 1.0 if row["role"] in ("user", "assistant") else 0.55
            score = min(1.0, 0.53 * coverage + 0.22 * exact
                        + 0.18 * similarity + 0.04 * recency + 0.03 * role_quality)
            event_id = row["event_id"] or ""
            stable_id = "chat:" + _digest(row["agent"], row["session_id"],
                                           event_id or duplicate_key)
            agent = _short_text(row["agent"], 128) or "unknown"
            role = _short_text(row["role"], 64) or "system"
            memory = CommonsMessage(
                id=stable_id, agent=agent,
                session_id=_short_text(row["session_id"], 256) or "unknown",
                event_id=_short_text(event_id, 256),
                ts=_short_text(row["ts"], 64), role=role, content=content,
                model=_short_text(row["model"], 128), cwd=_short_text(row["cwd"], 512),
                title=_short_text(row["title"], 512), source_file=_short_text(row["source_file"], 512),
                tags=["shared-chat", f"agent:{agent}", f"role:{role}"],
            )
            hits.append(CommonsHit(score=score, memory=memory, origin="chat"))
        hits.sort(key=lambda hit: (hit.score, _parse_time(hit.memory.ts)), reverse=True)
        return hits[:limit]

    @staticmethod
    def _shared_from_row(row: sqlite3.Row) -> SharedMemory:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        return SharedMemory(
            id=_short_text(row["memory_id"], 320),
            content=_short_text(row["content"], MAX_SHARED_CONTENT_CHARS),
            kind=_short_text(row["kind"], 64) or "semantic",
            source_agent=_short_text(row["source_agent"], 128) or "unknown",
            source_session=_short_text(row["source_session"], 256),
            source_refs=_json_list(row["source_refs"]),
            tags=_clean_strings(_json_list(row["tags"]), MAX_SHARED_TAGS, 96),
            scope=_short_text(row["scope"], 128) or "shared",
            valence=_finite_float(row["valence"], 0.0, -1.0, 1.0),
            confidence=_finite_float(row["confidence"], 0.6, 0.0, 1.0),
            utility=_finite_float(row["utility"], 0.0, -1.0, 1.0),
            importance=_bounded_int(row["importance"], 5, 1, 10),
            created_at=_short_text(row["created_at"], 64),
            updated_at=_short_text(row["updated_at"], 64),
            valid_until=_short_text(row["valid_until"], 64),
            superseded_by=_short_text(row["superseded_by"], 320),
            success_count=_bounded_int(row["success_count"], 0),
            failure_count=_bounded_int(row["failure_count"], 0),
            metadata=_safe_metadata(metadata),
        )

    def search_shared(self, query: str, limit: int = 10,
                      scope: str = "shared") -> List[CommonsHit]:
        query = _short_text(query, MAX_QUERY_CHARS)
        limit = _bounded_int(limit, 10, 0, 100)
        scope = _short_text(scope, 128) or "shared"
        if not self.shared_db.exists() or limit <= 0:
            return []
        terms = _terms(query)
        if not terms:
            return []
        where = " OR ".join("LOWER(content) LIKE ?" for _ in terms)
        params: List[object] = [f"%{term}%" for term in terms]
        params.extend([scope, _utcnow(), max(100, limit * 20)])
        sql = (
            "SELECT * FROM shared_memories WHERE (" + where + ") AND scope=? "
            "AND (superseded_by IS NULL OR superseded_by='') "
            "AND (valid_until IS NULL OR valid_until='' OR valid_until>?) "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        with self._shared_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        hits = []
        for row in rows:
            memory = self._shared_from_row(row)
            lowered = memory.content.lower()
            coverage = sum(1 for term in terms if term in lowered) / len(terms)
            evidence = min(1.0, len(memory.source_refs) / 3.0)
            utility = (memory.utility + 1.0) / 2.0
            score = min(1.0, 0.52 * coverage + 0.20 * token_sim(query, memory.content)
                        + 0.13 * memory.confidence + 0.07 * evidence
                        + 0.05 * utility + 0.03 * memory.importance / 10.0)
            hits.append(CommonsHit(score=score, memory=memory, origin="shared"))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    def recent_shared(self, limit: int = 20,
                      scope: str = "shared") -> List[CommonsHit]:
        """返回最近更新的有效共享规则，用于显式、有界的 Agent 同步。"""
        limit = _bounded_int(limit, 20, 0, 100)
        scope = _short_text(scope, 128) or "shared"
        if not self.shared_db.exists() or limit <= 0:
            return []
        sql = (
            "SELECT * FROM shared_memories WHERE scope=? "
            "AND (superseded_by IS NULL OR superseded_by='') "
            "AND (valid_until IS NULL OR valid_until='' OR valid_until>?) "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        with self._shared_connection() as conn:
            rows = conn.execute(sql, (scope, _utcnow(), min(100, limit))).fetchall()
        hits = []
        for row in rows:
            memory = self._shared_from_row(row)
            evidence = min(1.0, len(memory.source_refs) / 3.0)
            utility = (memory.utility + 1.0) / 2.0
            score = min(1.0, 0.62 * memory.confidence + 0.16 * evidence
                        + 0.12 * utility + 0.10 * memory.importance / 10.0)
            hits.append(CommonsHit(score=score, memory=memory, origin="shared"))
        return hits

    def recall(self, query: str, limit: int = 10,
               agents: Optional[Sequence[str]] = None,
               roles: Optional[Sequence[str]] = ("user", "assistant"),
               scope: str = "shared", include_chats: bool = True) -> List[CommonsHit]:
        """合并原始聊天证据与已提炼的共享记忆。"""
        shared = self.search_shared(query, limit=limit, scope=scope)
        chats = (self.search_messages(query, limit=limit, agents=agents, roles=roles)
                 if include_chats else [])
        # 共享规则经过证据化提炼；给轻微优先级，但不覆盖相关性。
        for hit in shared:
            hit.score = min(1.0, hit.score + 0.06)
        return sorted(shared + chats, key=lambda hit: hit.score, reverse=True)[:limit]

    def publish(self, content: str, source_agent: str, kind: str = "semantic",
                source_session: str = "", source_refs: Optional[Iterable[str]] = None,
                tags: Optional[Iterable[str]] = None, scope: str = "shared",
                valence: float = 0.0, confidence: float = 0.6,
                utility: float = 0.0, importance: int = 5,
                valid_until: str = "", metadata: Optional[Dict] = None,
                memory_id: Optional[str] = None) -> str:
        """发布可供所有 Agent 使用的提炼记忆；必须保留来源引用。"""
        raw_content = _short_text(content, MAX_SHARED_CONTENT_CHARS + 1)
        if len(raw_content) > MAX_SHARED_CONTENT_CHARS:
            raise ValueError(f"共享记忆内容不能超过 {MAX_SHARED_CONTENT_CHARS} 个字符")
        content = raw_content
        if not content:
            raise ValueError("共享记忆内容不能为空")
        source_agent = _short_text(source_agent, 128) or "unknown"
        source_session = _short_text(source_session, 256)
        kind = _short_text(kind, 64).lower() or "semantic"
        scope = _short_text(scope, 128) or "shared"
        refs = _normalise_refs(source_refs)
        tag_list = _clean_strings(tags, MAX_SHARED_TAGS, 96)
        if not refs:
            raise ValueError("共享记忆必须至少有一个 source_ref")
        valence = _finite_float(valence, 0.0, -1.0, 1.0)
        confidence = _finite_float(confidence, 0.6, 0.0, 1.0)
        utility = _finite_float(utility, 0.0, -1.0, 1.0)
        importance = _bounded_int(importance, 5, 1, 10)
        valid_until = _normalise_valid_until(valid_until)
        metadata = _safe_metadata(metadata)
        self.initialize()
        mid = _short_text(memory_id, 320) if memory_id else (
            "shared:" + _digest(source_agent, source_session, kind, content))
        if not mid:
            raise ValueError("memory_id 不能为空")
        now = _utcnow()
        with self._shared_connection(write=True) as conn:
            # Serialize read-modify-write so simultaneous Agents do not lose
            # provenance or feedback when publishing the same rule.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM shared_memories WHERE memory_id=?", (mid,)
            ).fetchone()
            if existing is not None:
                refs = sorted(set(refs) | set(_json_list(existing["source_refs"])))
                tag_list = sorted(set(tag_list) | set(_json_list(existing["tags"])))
                try:
                    old_metadata = json.loads(existing["metadata"] or "{}")
                except (TypeError, ValueError):
                    old_metadata = {}
                old_metadata.update(metadata)
                old_metadata = _safe_metadata(old_metadata)
                blended_utility = (0.8 * _finite_float(existing["utility"], 0.0, -1.0, 1.0)
                                   + 0.2 * utility)
                conn.execute("""
                    UPDATE shared_memories SET content=?,kind=?,source_agent=?,
                    source_session=?,source_refs=?,tags=?,scope=?,valence=?,confidence=?,
                    utility=?,importance=?,updated_at=?,valid_until=?,metadata=?
                    WHERE memory_id=?
                """, (
                    content, kind, source_agent, source_session,
                    json.dumps(refs, ensure_ascii=False),
                    json.dumps(tag_list, ensure_ascii=False), scope,
                    valence,
                    max(_finite_float(existing["confidence"], 0.6, 0.0, 1.0),
                        confidence),
                    blended_utility,
                    max(_bounded_int(existing["importance"], 5, 1, 10), importance),
                    now, valid_until or existing["valid_until"],
                    json.dumps(old_metadata, ensure_ascii=False, allow_nan=False), mid,
                ))
            else:
                conn.execute("""
                    INSERT INTO shared_memories
                    (memory_id,content,kind,source_agent,source_session,source_refs,tags,scope,
                     valence,confidence,utility,importance,created_at,updated_at,valid_until,
                     superseded_by,success_count,failure_count,metadata)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    mid, content, kind, source_agent, source_session,
                    json.dumps(refs, ensure_ascii=False),
                    json.dumps(tag_list, ensure_ascii=False), scope,
                    valence, confidence, utility, importance, now, now, valid_until,
                    "", 0, 0, json.dumps(metadata, ensure_ascii=False, allow_nan=False),
                ))
            conn.commit()
        return mid

    def feedback(self, memory_id: str, delta: float) -> bool:
        if not self.shared_db.exists():
            return False
        memory_id = _short_text(memory_id, 320)
        if not memory_id:
            return False
        signal = _finite_float(delta, 0.0, -1.0, 1.0)
        with self._shared_connection(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT utility,success_count,failure_count FROM shared_memories "
                "WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if row is None:
                return False
            utility = 0.8 * _finite_float(row["utility"], 0.0, -1.0, 1.0) + 0.2 * signal
            success = int(row["success_count"]) + (1 if signal > 0 else 0)
            failure = int(row["failure_count"]) + (1 if signal < 0 else 0)
            conn.execute(
                "UPDATE shared_memories SET utility=?,success_count=?,failure_count=?,"
                "updated_at=? WHERE memory_id=?",
                (utility, success, failure, _utcnow(), memory_id),
            )
            conn.commit()
        return True

    def supersede(self, old_id: str, new_id: str) -> bool:
        old_id = _short_text(old_id, 320)
        new_id = _short_text(new_id, 320)
        if not self.shared_db.exists() or old_id == new_id:
            return False
        with self._shared_connection(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM shared_memories WHERE memory_id=?", (new_id,)).fetchone() is None:
                return False
            cur = conn.execute(
                "UPDATE shared_memories SET superseded_by=?,updated_at=? WHERE memory_id=?",
                (new_id, _utcnow(), old_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def compare_shared_with_ledger(self, ledger, *, scope: str = "",
                                   sample_limit: int = DEFAULT_RECONCILE_SAMPLES
                                   ) -> Dict[str, object]:
        """对账并把紧凑摘要记到内存，供 stats()/看板读取（不落盘）。"""
        report = compare_shared_with_ledger(
            ledger, commons_db=self.shared_db, scope=scope,
            sample_limit=sample_limit)
        self._last_reconcile = _reconcile_summary(report, mode="compare")
        return report

    def repair_shared_with_ledger(self, ledger, *, scope: str = "",
                                  sample_limit: int = DEFAULT_RECONCILE_SAMPLES,
                                  dry_run: bool = False) -> Dict[str, object]:
        """把 ledger 的窗口字段同步到 commons，并把摘要记到内存。"""
        report = repair_shared_with_ledger(
            ledger, commons_db=self.shared_db, scope=scope,
            sample_limit=sample_limit, dry_run=dry_run)
        self._last_reconcile = _reconcile_summary(report, mode="repair")
        return report

    def promote_to(self, mind, hit: CommonsHit, importance: int = 5) -> int:
        """把一次真正有用的共通召回提升到当前 Agent 的活跃海马体。"""
        memory = hit.memory
        for item in list(mind.episodic.values()) + list(mind.archive.values()):
            if item.metadata.get("commons_id") == memory.id:
                if item.id in mind.archive:
                    mind.restore(item.id)
                return item.id
        return mind.store(
            situation=memory.situation[:8000], action="reference",
            outcome=f"来自 {getattr(memory, 'agent', 'unknown')} 的共通记忆",
            valence=float(getattr(memory, "valence", 0.0)),
            arousal=float(getattr(memory, "arousal", 0.3)),
            importance=importance,
            tags=list(getattr(memory, "tags", [])) + ["promoted"],
            source=getattr(memory, "source", "memory-commons"), scope="shared",
            metadata={"commons_id": memory.id, "commons_origin": hit.origin,
                      "source_session": getattr(memory, "session_id", "")},
        )

    def publish_from_mind(self, mind, source_agent: str,
                          source_session: str = "", min_confidence: float = 0.6,
                          min_support: int = 3, scope: str = "shared") -> List[str]:
        """发布达到证据门槛的语义规则，供其他 Agent 召回。"""
        published = []
        for memory in mind.semantic.values():
            if memory.confidence < min_confidence or memory.support_count < min_support:
                continue
            refs = []
            for episode_id in memory.source_episodes:
                episode = mind._get(episode_id)
                refs.append((episode.metadata.get("commons_id") if episode else None)
                            or f"hippomind:{episode_id}")
            if not refs:
                # 共享账本契约：发布必须携带可追溯 source_ref。导入或损坏的
                # 语义记忆可能没有来源情景；跳过而不是让整批发布中途失败。
                continue
            published.append(self.publish(
                content=memory.rule, source_agent=source_agent,
                source_session=source_session, source_refs=refs,
                tags=list(memory.tags) + ["consolidated"], scope=scope,
                valence=memory.valence, confidence=memory.confidence,
                utility=memory.utility, importance=memory.importance,
                metadata={"hippomind_id": memory.id,
                          "support_count": memory.support_count},
                memory_id="shared:" + _digest(source_agent, source_session, memory.rule),
            ))
        return published


# ---------------------------------------------------------------------------
# 双存储对账：commons shared_memories <-> ledger memories
# ---------------------------------------------------------------------------
# 设计契约：
#   * 两侧以 memory_id 为唯一连接键（双存储同步必须保留该 id，否则无法对账）；
#   * 只读默认：compare 只用 mode=ro/query_only 连接，不写任何文件；WAL 库在无
#     sidecar 时用 immutable 快照打开，连 -shm 都不会创建；
#   * 分桶互斥：missing_in_ledger / missing_in_commons 先按集合差判定；两侧都在
#     时 **window_mismatch 优先于 stale** —— 窗口漂移是双存储分叉的强证据且是
#     唯一可自动修复的桶，若与 stale 重叠它必须先出现，否则 --repair 前后计数
#     无法收敛；
#   * repair 只同步 superseded_by/valid_until（不删行、不改 content/updated_at），
#     一次 BEGIN IMMEDIATE 事务完成，计划为空时根本不打开写连接 -> 重复执行幂等。

def default_commons_db_path(archive: Optional[str] = None) -> Path:
    """commons 库默认位置：显式 archive > 环境变量 > 已知归档根。"""
    if archive:
        return Path(archive).expanduser() / "index" / "hippomind-commons.sqlite"
    for name in ("HIPPOMIND_COMMONS", "AGENT_ARCHIVE"):
        configured = os.environ.get(name, "").strip()
        if configured:
            return Path(configured).expanduser() / "index" / "hippomind-commons.sqlite"
    for candidate in DEFAULT_ARCHIVES:
        if (candidate / "index").is_dir():
            return candidate / "index" / "hippomind-commons.sqlite"
    return DEFAULT_ARCHIVES[0] / "index" / "hippomind-commons.sqlite"


def resolve_commons_db(commons_db=None, archive=None) -> Path:
    """显式 commons 库优先；否则按 archive/环境变量/默认归档根解析。"""
    if commons_db:
        return Path(commons_db).expanduser()
    return default_commons_db_path(archive)


def _configure_readonly(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.DatabaseError:
        pass
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
    except sqlite3.DatabaseError:
        pass


def _open_readonly(path: Path) -> sqlite3.Connection:
    """只读打开；WAL 且无 sidecar 时用 immutable 快照，避免创建 -shm。"""
    file_path = Path(path)
    last_error: Optional[sqlite3.Error] = None
    try:
        conn = sqlite3.connect(file_path.absolute().as_uri() + "?mode=ro",
                               uri=True, timeout=5.0)
        try:
            _configure_readonly(conn)
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        except Exception:
            conn.close()
            raise
        return conn
    except sqlite3.OperationalError as exc:
        last_error = exc
    try:
        with open(file_path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        raise last_error or sqlite3.OperationalError("unreadable database")
    wal_mode = len(header) >= 20 and (header[18] == 2 or header[19] == 2)
    sidecars = [Path(str(file_path) + suffix) for suffix in ("-wal", "-shm")]
    if not wal_mode or any(item.exists() for item in sidecars):
        raise last_error or sqlite3.OperationalError("unreadable database")
    conn = sqlite3.connect(file_path.absolute().as_uri() + "?mode=ro&immutable=1",
                           uri=True, timeout=5.0)
    _configure_readonly(conn)
    conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    return conn


@contextmanager
def _ledger_reader(ledger):
    """账本读取入口：普通/只读 SQLiteLedger 用 _reading()，路径用只读连接。"""
    reader = getattr(ledger, "_reading", None)
    if callable(reader):
        with reader() as conn:
            yield conn
        return
    raw_path = getattr(ledger, "path", None) or ledger
    conn = _open_readonly(Path(raw_path))
    try:
        yield conn
    finally:
        conn.close()


def _store_rows(path: Path, scope: str, table: str) -> Dict[str, object]:
    """读取一个 SQLite 表里参与对账的列（不存在时返回结构化错误，不抛）。"""
    file_path = Path(path)
    result: Dict[str, object] = {"exists": file_path.is_file(), "table": False,
                                 "rows": {}, "error": "", "path": str(file_path)}
    if not result["exists"]:
        result["error"] = "%s 不存在" % file_path
        return result
    try:
        conn = _open_readonly(file_path)
    except (sqlite3.Error, OSError) as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        return result
    try:
        names = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in names:
            result["error"] = "%s 表不存在" % table
            return result
        result["table"] = True
        sql = ("SELECT memory_id,scope,updated_at,superseded_by,valid_until "
               "FROM " + table)
        params: Sequence[object] = ()
        scope = _short_text(scope, 128)
        if scope:
            sql += " WHERE scope=?"
            params = (scope,)
        rows: Dict[str, Dict[str, str]] = {}
        for row in conn.execute(sql, params):
            memory_id = _short_text(row["memory_id"], 320)
            if not memory_id:
                continue
            rows[memory_id] = {
                "scope": _short_text(row["scope"], 128),
                "updated_at": _short_text(row["updated_at"], 64),
                "superseded_by": _short_text(row["superseded_by"], 320),
                "valid_until": _short_text(row["valid_until"], 64),
            }
        result["rows"] = rows
        return result
    except sqlite3.Error as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        return result
    finally:
        conn.close()


def _ledger_rows(ledger, scope: str) -> Dict[str, object]:
    """读取 ledger.memories 的对账列；实例与路径两种传入都支持。"""
    raw_path = getattr(ledger, "path", None)
    if raw_path is None and isinstance(ledger, (str, os.PathLike)):
        raw_path = ledger
    result: Dict[str, object] = {"exists": True, "rows": {}, "error": "",
                                 "path": str(raw_path or "")}
    if raw_path is not None and not Path(raw_path).is_file():
        result["exists"] = False
        result["error"] = "账本不存在: %s" % raw_path
        return result
    try:
        with _ledger_reader(ledger) as conn:
            names = {str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "memories" not in names:
                result["error"] = "memories 表不存在"
                return result
            sql = ("SELECT memory_id,scope,updated_at,superseded_by,valid_until "
                   "FROM memories WHERE raw_chat=0")
            params: Sequence[object] = ()
            scope = _short_text(scope, 128)
            if scope:
                sql += " AND scope=?"
                params = (scope,)
            rows: Dict[str, Dict[str, str]] = {}
            for row in conn.execute(sql, params):
                memory_id = _short_text(row["memory_id"], 320)
                if not memory_id:
                    continue
                rows[memory_id] = {
                    "scope": _short_text(row["scope"], 128),
                    "updated_at": _short_text(row["updated_at"], 64),
                    "superseded_by": _short_text(row["superseded_by"], 320),
                    "valid_until": _short_text(row["valid_until"], 64),
                }
            result["rows"] = rows
        return result
    except FileNotFoundError as exc:
        result["exists"] = False
        result["error"] = str(exc)
        return result
    except (sqlite3.Error, OSError) as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        return result


def _empty_counts() -> Dict[str, int]:
    counts: Dict[str, int] = {bucket: 0 for bucket in RECONCILE_BUCKETS}
    counts["in_sync"] = 0
    return counts


def compare_shared_with_ledger(ledger, *, commons_db=None, archive=None,
                               scope: str = "",
                               sample_limit: int = DEFAULT_RECONCILE_SAMPLES
                               ) -> Dict[str, object]:
    """按 memory_id 比对 commons shared_memories 与 ledger memories。

    分桶（互斥，窗口优先）：

    * ``missing_in_ledger``  -- commons 有、ledger 没有；
    * ``missing_in_commons`` -- ledger 有、commons 没有；
    * ``window_mismatch``    -- 两侧都在但 superseded_by/valid_until 不一致
      （唯一可由 repair 自动收敛的桶，因此优先于 stale 归类）；
    * ``stale``              -- 两侧都在、窗口一致但 updated_at 不一致（内容/反馈
      漂移，不是窗口漂移，repair 不碰）。

    ``scope`` 为空串时比较两侧的全部行；显式 scope 时两侧都按该 scope 过滤。
    返回计数与每桶最多 ``sample_limit`` 条样例，任何一侧缺失/损坏都返回结构化
    报告而不是抛异常（只读审计不能因为对端不在就崩）。
    """
    commons_path = resolve_commons_db(commons_db, archive)
    limit = _bounded_int(sample_limit, DEFAULT_RECONCILE_SAMPLES, 0, 100)
    scope_text = _short_text(scope, 128)
    commons = _store_rows(commons_path, scope_text, "shared_memories")
    ledger_state = _ledger_rows(ledger, scope_text)
    commons_rows: Dict[str, Dict[str, str]] = commons["rows"]  # type: ignore[assignment]
    ledger_map: Dict[str, Dict[str, str]] = ledger_state["rows"]  # type: ignore[assignment]
    counts = _empty_counts()
    samples: Dict[str, List[Dict[str, object]]] = {
        bucket: [] for bucket in RECONCILE_BUCKETS}

    def add_sample(bucket: str, item: Dict[str, object]) -> None:
        if len(samples[bucket]) < limit:
            samples[bucket].append(item)

    for memory_id in sorted(commons_rows):
        stored = commons_rows[memory_id]
        current = ledger_map.get(memory_id)
        if current is None:
            counts["missing_in_ledger"] += 1
            add_sample("missing_in_ledger", {
                "memory_id": memory_id,
                "commons_updated_at": stored["updated_at"],
                "commons_superseded_by": stored["superseded_by"],
                "commons_valid_until": stored["valid_until"],
            })
            continue
        if (stored["superseded_by"] != current["superseded_by"]
                or stored["valid_until"] != current["valid_until"]):
            counts["window_mismatch"] += 1
            add_sample("window_mismatch", {
                "memory_id": memory_id,
                "commons_updated_at": stored["updated_at"],
                "ledger_updated_at": current["updated_at"],
                "commons_superseded_by": stored["superseded_by"],
                "ledger_superseded_by": current["superseded_by"],
                "commons_valid_until": stored["valid_until"],
                "ledger_valid_until": current["valid_until"],
            })
        elif stored["updated_at"] != current["updated_at"]:
            counts["stale"] += 1
            add_sample("stale", {
                "memory_id": memory_id,
                "commons_updated_at": stored["updated_at"],
                "ledger_updated_at": current["updated_at"],
            })
        else:
            counts["in_sync"] += 1
    for memory_id in sorted(ledger_map):
        if memory_id in commons_rows:
            continue
        counts["missing_in_commons"] += 1
        current = ledger_map[memory_id]
        add_sample("missing_in_commons", {
            "memory_id": memory_id,
            "ledger_updated_at": current["updated_at"],
            "ledger_superseded_by": current["superseded_by"],
            "ledger_valid_until": current["valid_until"],
        })
    counts["drift_total"] = (counts["missing_in_ledger"]
                             + counts["missing_in_commons"]
                             + counts["stale"] + counts["window_mismatch"])
    ok = (counts["drift_total"] == 0 and bool(commons["exists"])
          and bool(ledger_state["exists"]) and not commons["error"]
          and not ledger_state["error"])
    return {
        "ok": bool(ok),
        "checked_at": _utcnow(),
        "scope": scope_text,
        "commons_db": str(commons_path),
        "commons_exists": bool(commons["exists"]),
        "commons_table": bool(commons["table"]),
        "commons_error": str(commons["error"] or ""),
        "ledger": str(ledger_state["path"] or ""),
        "ledger_exists": bool(ledger_state["exists"]),
        "ledger_error": str(ledger_state["error"] or ""),
        "commons_rows": len(commons_rows),
        "ledger_rows": len(ledger_map),
        "counts": counts,
        "samples": samples,
        "sample_limit": limit,
        "repairable": counts["window_mismatch"],
    }


def _window_repairs(commons_state, ledger_state, limit: int) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    for memory_id in sorted(commons_state["rows"]):
        current = ledger_state["rows"].get(memory_id)
        if current is None:
            continue
        stored = commons_state["rows"][memory_id]
        if (stored["superseded_by"] == current["superseded_by"]
                and stored["valid_until"] == current["valid_until"]):
            continue
        items.append({
            "memory_id": memory_id,
            "old_superseded_by": stored["superseded_by"],
            "new_superseded_by": current["superseded_by"],
            "old_valid_until": stored["valid_until"],
            "new_valid_until": current["valid_until"],
        })
    return items[:limit] if limit else items


def repair_shared_with_ledger(ledger, *, commons_db=None, archive=None,
                              scope: str = "",
                              sample_limit: int = DEFAULT_RECONCILE_SAMPLES,
                              dry_run: bool = False) -> Dict[str, object]:
    """把 ledger 的 superseded_by/valid_until 同步到 commons（默认 dry-run）。

    安全边界：只 UPDATE 这两列，不删行、不改 content/updated_at/feedback；计划为
    空时根本不打开写连接，所以重复执行幂等，默认模式不产生任何写入。返回
    ``before``/``after`` 两份完整对账报告、``planned``/``repaired`` 影响面与
    ``window_resolved`` 收敛判定。
    """
    commons_path = resolve_commons_db(commons_db, archive)
    limit = _bounded_int(sample_limit, DEFAULT_RECONCILE_SAMPLES, 0, 100)
    scope_text = _short_text(scope, 128)
    before = compare_shared_with_ledger(
        ledger, commons_db=commons_path, scope=scope_text, sample_limit=limit)
    error = ""
    repairs: List[Dict[str, object]] = []
    plan: List[Dict[str, str]] = []
    if not before["commons_exists"]:
        error = "commons 库不存在，未做任何写入"
    elif not before["commons_table"]:
        error = "commons 缺少 shared_memories 表，未做任何写入"
    elif not before["ledger_exists"]:
        error = "账本不存在，未做任何写入"
    else:
        commons_state = _store_rows(commons_path, scope_text, "shared_memories")
        ledger_state = _ledger_rows(ledger, scope_text)
        for memory_id in sorted(commons_state["rows"]):
            current = ledger_state["rows"].get(memory_id)
            if current is None:
                continue
            stored = commons_state["rows"][memory_id]
            if (stored["superseded_by"] == current["superseded_by"]
                    and stored["valid_until"] == current["valid_until"]):
                continue
            plan.append({"memory_id": memory_id,
                         "superseded_by": current["superseded_by"],
                         "valid_until": current["valid_until"]})
        repairs = _window_repairs(commons_state, ledger_state, limit)
    repaired = 0
    if plan and not dry_run:
        try:
            conn = sqlite3.connect(str(commons_path), timeout=10.0)
            try:
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("BEGIN IMMEDIATE")
                for item in plan:
                    conn.execute(
                        "UPDATE shared_memories SET superseded_by=?, valid_until=? "
                        "WHERE memory_id=?",
                        (item["superseded_by"], item["valid_until"],
                         item["memory_id"]))
                    repaired += 1
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            error = "%s: %s" % (type(exc).__name__, exc)
            repaired = 0
    after = compare_shared_with_ledger(
        ledger, commons_db=commons_path, scope=scope_text, sample_limit=limit)
    window_resolved = after["counts"]["window_mismatch"] == 0
    return {
        "mode": "repair",
        "dry_run": bool(dry_run),
        "checked_at": _utcnow(),
        "commons_db": str(commons_path),
        "ledger": str(before["ledger"] or ""),
        "before": before,
        "planned": len(plan),
        "repaired": repaired,
        "repairs": repairs,
        "after": after,
        "error": error,
        "ok": bool(after["ok"]),
        "window_resolved": bool(window_resolved),
        "changed": bool(repaired),
    }


def _reconcile_summary(report: Dict[str, object],
                       mode: str = "compare") -> Dict[str, object]:
    """内存里的最近一次对账摘要（stats 用，不写文件、不做额外 IO）。"""
    base = report.get("after") if mode == "repair" else report
    base = base if isinstance(base, dict) else report
    counts = base.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    window = counts.get("window_mismatch", 0)
    return {
        "mode": mode,
        "checked_at": str(base.get("checked_at") or report.get("checked_at") or ""),
        "ok": bool(base.get("ok")),
        "window_resolved": bool(report.get("window_resolved", window == 0)),
        "repaired": int(report.get("repaired", 0) or 0),
        "commons_db": str(base.get("commons_db") or report.get("commons_db") or ""),
        "ledger": str(base.get("ledger") or report.get("ledger") or ""),
        "counts": dict(counts),
    }



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hippomind 全电脑 Agent 记忆共通层")
    parser.add_argument("--archive", help="Agent-Chat-Archive 根目录")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化共享语义账本")
    sub.add_parser("stats", help="显示全机 Agent 记忆统计")
    search = sub.add_parser("search", help="跨 Agent 搜索原始聊天与共享记忆")
    search.add_argument("query")
    search.add_argument("--agent", action="append", dest="agents")
    search.add_argument("--role", action="append", dest="roles")
    search.add_argument("--limit", type=int, default=10)
    publish = sub.add_parser("publish", help="发布一条带证据的共享语义记忆")
    publish.add_argument("content")
    publish.add_argument("--agent", required=True)
    publish.add_argument("--session", default="")
    publish.add_argument("--source-ref", action="append", required=True)
    publish.add_argument("--tag", action="append", default=[])
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    commons = MemoryCommons(args.archive)
    if args.command == "init":
        print(f"共享语义账本: {commons.initialize()}")
    elif args.command == "stats":
        info = commons.stats()
        print(f"归档: {info['archive']}")
        for agent, values in info["agents"].items():
            print(f"{agent:<14} {values['messages']:>8} 消息  {values['sessions']:>5} 会话")
        print(f"合计: {info['messages']} 消息 / {info['sessions']} 会话 / "
              f"{info['shared_memories']} 条共享语义记忆")
    elif args.command == "search":
        roles = args.roles if args.roles else ("user", "assistant")
        for hit in commons.recall(args.query, args.limit, args.agents, roles):
            memory = hit.memory
            text = memory.situation.replace("\n", " ")[:180]
            print(f"[{hit.origin}:{getattr(memory, 'agent', '?')}] "
                  f"{hit.score:.3f} {memory.id[:20]}  {text}")
    elif args.command == "publish":
        mid = commons.publish(args.content, source_agent=args.agent,
                              source_session=args.session,
                              source_refs=args.source_ref, tags=args.tag)
        print(f"已发布: {mid}")


if __name__ == "__main__":
    main()
