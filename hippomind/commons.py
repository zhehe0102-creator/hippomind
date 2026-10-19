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

    def _chat_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.chat_db}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        try:
            conn.execute("PRAGMA trusted_schema=OFF")
        except sqlite3.DatabaseError:
            pass
        return conn

    def _shared_connection(self, write: bool = False) -> sqlite3.Connection:
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
        return conn

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
        return {
            "archive": str(self.archive_root),
            "agents": agents,
            "messages": sum(v["messages"] for v in agents.values()),
            "sessions": sum(v["sessions"] for v in agents.values()),
            "shared_memories": shared,
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
            success = int(row["success_count"]) + (1 if signal >= 0 else 0)
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
