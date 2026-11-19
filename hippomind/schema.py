"""Ledger schema definition and forward-only migrations.

Extracted from ``ledger.py`` (4,195 lines, one 3,662-line class) so that the
storage engine's *schema* is reviewable on its own and the migration chain can be
diffed without scrolling past query logic.

Contract
--------
* **Forward only.** A ledger whose recorded version is newer than
  :data:`SCHEMA_VERSION` is refused rather than written to, so an older release
  can never silently corrupt a newer database.
* **Contiguous.** Every version 1..N must have a ``_migrate_vN`` hook;
  ``tests/test_schema_contract.py`` fails the build if one is missing.
* **Idempotent.** All DDL uses ``IF NOT EXISTS`` and all data backfills are
  guarded, so re-running a migration is a no-op.

``SQLiteLedger`` keeps thin forwarding attributes (``_migrate_v3 = staticmethod(...)``)
so existing callers and tests continue to work unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from .textutil import MAX_TEXT_CHARS
from .sanitize import MAX_ARCHIVE_MESSAGE_CHARS  # noqa: F401  (re-exported for callers)


SCHEMA_VERSION = 9

#: Raw-evidence kinds.  Defined here because the schema's FTS and lifecycle
#: columns are shaped around them.
RAW_KINDS = frozenset({"raw_chat", "tool_output", "system_message"})

#: **The** definition of "this memory belongs in ``memory_fts``".
#:
#: Every FTS writer, auditor and pruner must derive its membership test from
#: this one string.  Before it existed there were four incompatible definitions:
#:
#: * the write path indexed ``quality_state='accepted'`` (regardless of lifecycle);
#: * ``fts_sync_report`` counted ``accepted`` + ``active``;
#: * ``health_overview.fts.expected`` and :func:`_rebuild_fts` used
#:   "not (raw and non-active)" — i.e. they expected quarantined, conflicted,
#:   superseded, expired and cold rows to stay in the hot index;
#: * ``maintain`` pruned only ``lifecycle!='active'``, so quarantined-but-active
#:   rows were never removed.
#:
#: Measured consequence on the 136,459-row live ledger (2026-10-20): 5,571 FTS
#: rows, of which only 392 could ever be returned by a recall query — 5,179
#: (93%) permanent index garbage, while ``checkup --repair`` re-added it and
#: ``maintain`` reported a healthy ledger as drifted.
#:
#: The single rule: a row is indexed **iff** it is accepted quality *and* on the
#: active lifecycle.  Cold/audit rows leave the hot index but stay retrievable
#: through the bounded LIKE fallback in ``search(include_cold=True)``.
FTS_INDEXED_PREDICATE = "quality_state='accepted' AND lifecycle='active'"


def fts_indexed_predicate(table: str = "") -> str:
    """Return :data:`FTS_INDEXED_PREDICATE`, optionally table-qualified.

    ``fts_indexed_predicate("m")`` yields
    ``m.quality_state='accepted' AND m.lifecycle='active'`` so a query that
    joins ``memories`` can use the same single definition without copying it.
    """
    if not table:
        return FTS_INDEXED_PREDICATE
    prefix = str(table) + "."
    return (prefix + "quality_state='accepted' AND "
            + prefix + "lifecycle='active'")


def _fts_tokenizer() -> str:
    """trigram needs SQLite >= 3.34; older builds fall back to unicode61.

    ``trigram`` gives MATCH substring semantics for CJK and Latin alike, which is
    what lets the FTS channel carry Chinese recall instead of depending on the
    bounded LIKE rescue path.
    """
    parts = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:3])
    return "trigram" if parts >= (3, 34, 0) else "unicode61"


def _fts_text(content: str, metadata: Mapping[str, Any]) -> str:
    """Flatten content plus structured context fields into one indexable string."""
    parts = [content]
    context = metadata.get("context") if isinstance(metadata, Mapping) else None
    if isinstance(context, Mapping):
        for values in context.values():
            if isinstance(values, (list, tuple)):
                parts.extend(str(item) for item in values if item)
            elif values:
                parts.append(str(values))
    return "\n".join(part for part in parts if part)[:MAX_TEXT_CHARS]


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ledger_events (
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
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_events_source ON ledger_events(source_agent, session_id);
        CREATE INDEX IF NOT EXISTS idx_events_task ON ledger_events(task_id, project);
        CREATE INDEX IF NOT EXISTS idx_events_time ON ledger_events(occurred_at);

        CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY,
            origin_event_id TEXT NOT NULL UNIQUE REFERENCES ledger_events(event_id),
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source_agent TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_access_at TEXT NOT NULL DEFAULT '',
            last_weighted_at TEXT NOT NULL,
            valid_until TEXT NOT NULL DEFAULT '',
            superseded_by TEXT NOT NULL DEFAULT '',
            archived_at TEXT NOT NULL DEFAULT '',
            lifecycle TEXT NOT NULL DEFAULT 'active',
            raw_chat INTEGER NOT NULL DEFAULT 0,
            weight REAL NOT NULL DEFAULT 0.5,
            importance INTEGER NOT NULL DEFAULT 5,
            touch_count INTEGER NOT NULL DEFAULT 0,
            utility REAL NOT NULL DEFAULT 0.0,
            confidence REAL NOT NULL DEFAULT 0.5,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, lifecycle);
        CREATE INDEX IF NOT EXISTS idx_memories_task ON memories(task_id, project, updated_at);
        CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at);
        -- 结构化行（raw_chat=0）只占库容个位数百分比；部分索引让
        -- 「最近结构化记忆」兜底查询从 77 万行扫描降到千级行游走。
        CREATE INDEX IF NOT EXISTS idx_memories_structured_recent
            ON memories(updated_at) WHERE raw_chat=0;
        CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source_agent, session_id);
        CREATE INDEX IF NOT EXISTS idx_memories_state ON memories(raw_chat, lifecycle, weight);


        CREATE TABLE IF NOT EXISTS memory_feedback (
            feedback_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL REFERENCES memories(memory_id),
            signal REAL NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            source_agent TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_memory ON memory_feedback(memory_id, created_at);

        CREATE TABLE IF NOT EXISTS source_cursors (
            source_id TEXT PRIMARY KEY,
            cursor TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            last_success_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
    """)

    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
        "memory_id UNINDEXED, content, task_id, project, source_agent, kind, "
        "tokenize='" + _fts_tokenizer() + "')")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    # Token hashes live with the local ledger so a daemon restart preserves
    # policy. The raw bearer secret is returned exactly once to the local
    # installer and is never stored in SQLite.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_credentials (
            token_id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            agent_id TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            permissions_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_agent_credentials_agent
            ON agent_credentials(agent_id, revoked_at);
    """)


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Backfill FTS with structured context fields for cross-agent recall."""
    _rebuild_fts(conn)


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """手动置顶列：操作员可星标关键记忆，在相关候选中获得有界加权。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "pinned" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """检索性能索引（迭代 27，69 万行库剖析驱动）。

    - idx_memories_updated：raw 审计/兜底窗口的 updated_at 序游走，
      消除 TEMP B-TREE 全量排序；
    - idx_memories_structured_recent：raw_chat=0 部分索引——结构化行
      只占库容个位数百分比，「最近结构化记忆」兜底从 77 万行扫描
      （实测 7-9s）降到千级行游走。
    均为 IF NOT EXISTS，对已含任一索引的库零成本。
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_updated "
                 "ON memories(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_structured_recent "
                 "ON memories(updated_at) WHERE raw_chat=0")
    # 新索引若无统计信息，规划器仍会选 idx_memories_scope 全索引扫
    # （69 万行实测 0.8-6s）；ANALYZE 后改选 raw_chat 部分索引（34ms）。
    conn.execute("ANALYZE")


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """图谱边原生双时态窗口（Graphiti 对齐）：为 memory_relations 补齐 valid_from/valid_until。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memory_relations)").fetchall()}
    if "valid_from" not in columns:
        conn.execute("ALTER TABLE memory_relations ADD COLUMN valid_from TEXT NOT NULL DEFAULT ''")
    if "valid_until" not in columns:
        conn.execute("ALTER TABLE memory_relations ADD COLUMN valid_until TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_window "
                 "ON memory_relations(valid_from, valid_until)")


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Add bi-temporal claims and bounded causal edges without rewriting v3 data."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(ledger_events)").fetchall()}
    if "asserted_at" not in columns:
        conn.execute("ALTER TABLE ledger_events ADD COLUMN asserted_at TEXT NOT NULL DEFAULT ''")
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for name, definition in (
        ("valid_from", "TEXT NOT NULL DEFAULT ''"),
        ("asserted_at", "TEXT NOT NULL DEFAULT ''"),
        ("agent_trust", "REAL NOT NULL DEFAULT 0.5"),
        ("quality_score", "REAL NOT NULL DEFAULT 1.0"),
        ("quality_state", "TEXT NOT NULL DEFAULT 'accepted'"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
    conn.execute("UPDATE ledger_events SET asserted_at=ingested_at WHERE asserted_at=''")
    conn.execute("""
        UPDATE memories
        SET valid_from=CASE WHEN valid_from='' THEN created_at ELSE valid_from END,
            asserted_at=CASE WHEN asserted_at='' THEN updated_at ELSE asserted_at END
        WHERE valid_from='' OR asserted_at=''
    """)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_trust (
            agent_id TEXT PRIMARY KEY,
            baseline REAL NOT NULL DEFAULT 0.5,
            updated_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'local-admin'
        );
        CREATE TABLE IF NOT EXISTS memory_claims (
            claim_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL REFERENCES memories(memory_id),
            scope TEXT NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            value_hash TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_until TEXT NOT NULL DEFAULT '',
            asserted_at TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            source_trust REAL NOT NULL DEFAULT 0.5,
            state TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            UNIQUE(memory_id,subject,predicate,slot_key)
        );
        CREATE INDEX IF NOT EXISTS idx_claim_slot
            ON memory_claims(scope,subject,predicate,slot_key,state,valid_from);
        CREATE INDEX IF NOT EXISTS idx_claim_memory ON memory_claims(memory_id,state);
        CREATE TABLE IF NOT EXISTS memory_relations (
            relation_id TEXT PRIMARY KEY,
            parent_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
            child_memory_id TEXT NOT NULL REFERENCES memories(memory_id),
            relation_type TEXT NOT NULL DEFAULT 'caused_by',
            confidence REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            valid_from TEXT NOT NULL DEFAULT '',
            valid_until TEXT NOT NULL DEFAULT '',
            UNIQUE(parent_memory_id,child_memory_id,relation_type),
            CHECK(parent_memory_id != child_memory_id)
        );
        CREATE INDEX IF NOT EXISTS idx_relations_parent ON memory_relations(parent_memory_id);
        CREATE INDEX IF NOT EXISTS idx_relations_child ON memory_relations(child_memory_id);
        CREATE INDEX IF NOT EXISTS idx_relations_window ON memory_relations(valid_from, valid_until);
    """)


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Quarantine legacy raw-chat derivatives from normal recall.

    They remain auditable in the append-only ledger, but a heuristic
    extraction is not verified task state and must not occupy FTS/Context
    Pack candidate capacity.
    """
    conn.execute("""
        UPDATE memories
        SET quality_state='quarantine', quality_score=MIN(quality_score, 0.15)
        WHERE metadata_json LIKE '%\"derived_from_raw_chat\":true%'
    """)


def _fts_indexed_where(conn: sqlite3.Connection, table: str = "") -> str:
    """Return the indexed-set predicate for this connection's schema age.

    ``quality_state`` is only added by ``_migrate_v4``, but ``_migrate_v3`` has
    to rebuild the (then empty) index *before* that column exists.  At v3 every
    surviving row predates the quality dimension and v4 defaults it to
    ``accepted``, so "all active rows" and the full predicate coincide; from v4
    on the single :data:`FTS_INDEXED_PREDICATE` rule applies.  This keeps the
    migration chain contiguous without weakening the v4+ invariant.
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)")}
    if "quality_state" not in columns:
        prefix = (str(table) + ".") if table else ""
        return prefix + "lifecycle='active'"
    return fts_indexed_predicate(table)


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    """Recreate memory_fts from exactly the indexed set (shared by migrations).

    Callers must have already (re)created the virtual table with the desired
    tokenizer.  The backfill is restricted to :data:`FTS_INDEXED_PREDICATE`:
    cold, expired, superseded, conflicted and quarantined rows keep their audit
    LIKE path but stay out of the hot index, which is what normal maintenance
    converges to.  A rebuild must never re-add a row that maintenance would
    immediately prune — that made ``checkup --repair`` grow stale rows.
    """
    rows = conn.execute("""
        SELECT memory_id,content,task_id,project,source_agent,kind,metadata_json
        FROM memories WHERE """ + _fts_indexed_where(conn) + """
    """).fetchall()
    conn.execute("DELETE FROM memory_fts")
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        metadata = metadata if isinstance(metadata, Mapping) else {}
        conn.execute("""
            INSERT INTO memory_fts(memory_id,content,task_id,project,source_agent,kind)
            VALUES(?,?,?,?,?,?)
        """, (row["memory_id"], _fts_text(str(row["content"]), metadata), row["task_id"],
              row["project"], row["source_agent"], row["kind"]))


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """Switch FTS to the trigram tokenizer for CJK substring recall.

    unicode61 stores a contiguous CJK run as one token, so Chinese
    queries could only be rescued by the bounded LIKE scan — measured at
    584 ms on a 50k-row ledger while FTS returned zero rows. trigram
    gives MATCH substring semantics for CJK and latin alike (SQLite
    >= 3.34), letting the FTS channel carry the load and the LIKE path
    stay a sparse-result rescue.
    """
    conn.execute("DROP TABLE IF EXISTS memory_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "memory_id UNINDEXED, content, task_id, project, source_agent, kind, "
        "tokenize='" + _fts_tokenizer() + "')")
    _rebuild_fts(conn)
