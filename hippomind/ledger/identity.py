"""Record validation, stable identity keys and event/projection insertion.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import sqlite3
from ..textutil import normalise_for_dedup
from ..sanitize import MAX_ARCHIVE_MESSAGE_CHARS, sanitize_chat_content
from typing import Any, Dict, Iterable, List, Mapping
from ._helpers import (
    DEFAULT_AGENT_TRUST,
    IngestResult,
    LedgerError,
    MAX_CONTENT_CHARS,
    MAX_EVIDENCE,
    MAX_TAGS,
    QUALITY_ACCEPT_THRESHOLD,
    RAW_KINDS,
    _clamp_float,
    _clamp_int,
    _fts_text,
    _hash,
    _json,
    _mapping,
    _normalise_causal_parents,
    _normalise_claims,
    _normalise_context,
    _normalise_procedural,
    _quality_score,
    _safe_kind,
    _sanitize_untrusted_metadata,
    _short,
    _strings,
    _text,
    _timestamp,
    utc_now,
)

class IdentityMixin:
    """Record validation, stable identity keys and event/projection insertion."""

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
                    # The whole losing memory leaves recall, so every active
                    # claim on it must leave the active set as well; otherwise
                    # health reports dangling active claims forever.
                    conn.execute("UPDATE memory_claims SET state='superseded' "
                                 "WHERE memory_id=? AND state='active'", (old["memory_id"],))
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
                    conn.execute("UPDATE memory_claims SET state='conflict' "
                                 "WHERE memory_id=? AND state='active'", (old["memory_id"],))
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
