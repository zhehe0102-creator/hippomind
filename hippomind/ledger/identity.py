"""Record validation, stable identity keys and event/projection insertion.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import json
import sqlite3
from ..textutil import normalise_for_dedup
from ..sanitize import MAX_ARCHIVE_MESSAGE_CHARS, sanitize_chat_content
from typing import Any, Dict, Iterable, List, Mapping
# 双时态窗口的闭合规则只有一处实现（见 lifecycle._supersede_window_end 的
# docstring）：散在多个写入点里，任何一处漏掉都会静默产生重叠窗口。
from .lifecycle import _supersede_window_end
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

#: Identity key format version.
#:
#: v1 hashed ``valid_from`` whenever the record carried *any* timestamp, so a
#: collector that re-observes the same message with a refreshed timestamp minted
#: a brand-new identity every pass.  Measured on the production ledger
#: (2026-10-24): one Gemini conversation summary existed 141 times -- identical
#: ``content`` and identical ``evidence_json``, 141 distinct ``valid_from``
#: values, 141 distinct ``content_hash``.  Re-deriving each row identity with the
#: v1 formula reproduced all 141/141 stored ids, and excluding ``valid_from``
#: collapsed them to exactly 1.
#:
#: v2 only lets an *explicitly supplied* ``valid_from`` participate, so genuine
#: bitemporal history (a caller asserting the same sentence as valid from two
#: different dates) is still two observations, while capture-time drift is not.
#:
#: v3 keeps both rules and adds the one discriminator v2 could not express: an
#: explicitly supplied source event id (event_id / external_id) enters raw-chat
#: identity.  Raw capture used to be keyed by (agent, session, role, content)
#: alone, which is right for append-only transcripts but wrong for two distinct
#: events carrying the same text ("ok" answered twice), and wrong for a bubble
#: Cursor streams in place: the draft and the finished answer were two
#: identities, so both stayed recallable.
#:
#: With an event id, distinct source events keep two rows (the append-only
#: contract) and *the same* event revised in place is superseded by its newest
#: revision inside the insert transaction.  With no event id the raw key is
#: byte-for-byte the v2 key, i.e. "no id keeps the v2 dedup rule".
#:
#: Structured rows already carried external_id in their key since v2; v3 leaves
#: that key untouched and only bumps the version marker (itself hashed).  Both
#: older rules remain as compatibility probes -- see
#: IdentityMixin._find_existing_event -- so upgrading a live ledger does not
#: re-ingest rows written by the previous release.
IDENTITY_VERSION = 3

#: Version stamped into every prepared record (and persisted in metadata) so a
#: future format change can tell which rows were minted under which rule.
IDENTITY_VERSION_KEY = "identity_version"

#: The immediately preceding identity rule.  Not a stored marker: it names the
#: digest recomputed for the compatibility probe that recognises rows written by
#: the version being upgraded from.
IDENTITY_VERSION_V2 = 2

#: Records written before v2 carry no version marker; they were all v1.
IDENTITY_VERSION_LEGACY = 1


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
        # v3: the stable source event id is projection data too, so a later
        # revision of the same event can be located and superseded.  Written
        # *after* sanitisation so the stored value is byte-identical to the
        # one that entered the identity key above (a redaction pass must
        # never make the projection disagree with the digest).
        if external_id:
            metadata["external_id"] = external_id
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
        # Stable identity key (see IDENTITY_VERSION for the full history)
        # ------------------------------------------------------------------
        # Two independent bugs have been fixed here, in order:
        #
        # (1) The original canonical form hashed ``occurred_at``, ``valid_from``
        #     *and the whole metadata blob*.  For raw chat that meant the same
        #     message re-captured on a later watcher pass produced a different
        #     hash every time: the live ledger held 1,475,584 raw rows whose
        #     content_hash were all distinct while a single session's 70,508
        #     rows contained only 3,573 unique messages.  Raw identity is now
        #     *what it says*, not when it was captured.
        #
        # (2) Structured records still hashed ``valid_from`` whenever the record
        #     carried any timestamp -- including a timestamp the *collector*
        #     derived from a file mtime.  A collector that refreshes its source
        #     file on every pass therefore minted a new identity on every pass.
        #     Measured on the production ledger (2026-10-24): one Gemini
        #     conversation summary stored 141 times, identical content and
        #     evidence, 141 distinct ``valid_from``.  The collector
        #     (``gemini_capture``) passes ``timestamp=source_modified_time``,
        #     never ``valid_from``, so ``explicit_valid_from`` was always False
        #     and only ``explicit_observed_at`` kept the drifting value in the
        #     hash.  v2 drops that path: a *derived* observation time is capture
        #     drift, not a distinct assertion.
        #
        # Note the metadata blob is NOT part of either key -- ``step_count`` and
        # ``source_modified_time`` varied across all 141 rows but the row
        # identities were fully explained by ``valid_from`` alone.  Metadata is
        # projection data, so a later enrichment pass cannot fork identity.
        #
        # Backward compatibility: v2 mints the new id, but ``_insert_prepared``
        # also probes the v1 id so rows written before this change are still
        # recognised as duplicates instead of being re-ingested wholesale.
        if raw_chat:
            identity = {
                "class": "raw",
                "source_agent": source_agent,
                "session_id": session_id,
                "role": role,
                "content": normalise_for_dedup(content),
            }
            # v3: an explicit source event id is part of raw identity.
            # Without one this dict is byte-for-byte the v2 form, so capture
            # paths that never had stable ids keep the v2 dedup rule.
            if external_id:
                identity["external_id"] = external_id
        else:
            # ``role`` and ``evidence`` are deliberately absent: an agent that
            # re-reports the same decision with a fresh evidence id is repeating
            # itself, not making a new observation.
            #
            # When does a *derived* observation time still belong in the key?
            #
            # A record that names a stable source object (``task_id`` for a
            # conversation summary, ``session_id`` for a transcript) is a
            # *statement about that object*.  Re-reading the object -- even with
            # a refreshed mtime -- says the same thing, so the time must not
            # fork identity.  That is the 141-duplicate case: gemini_capture
            # always sets ``task_id=conversation_id``.
            #
            # A record with no such anchor is a *standalone assertion*; the
            # observation time is then the only thing that distinguishes one
            # assertion from the next, and the append-only contract keeps both
            # (``tests/test_ingest_idempotency`` pins this).
            has_stable_anchor = bool(task_id or session_id or external_id)
            keep_derived_valid_from = (not explicit_valid_from
                                       and not has_stable_anchor
                                       and explicit_observed_at)
            identity = {
                "class": "structured",
                "kind": kind,
                "source_agent": source_agent,
                "session_id": session_id,
                "external_id": external_id,
                "task_id": task_id,
                "project": project,
                "scope": scope,
                "valid_from": (valid_from if (explicit_valid_from
                                                or keep_derived_valid_from) else ""),
                "valid_until": valid_until,
                "content": normalise_for_dedup(content),
            }
        # The immediately preceding rule (v2) applied to *this* record.  It is
        # probed after the current digest so the first v3 pass over an existing
        # ledger recognises rows written by the release being upgraded instead
        # of re-ingesting them under a new hash (the 1.47M-row duplicate storm).
        if raw_chat and external_id:
            v2_identity = {key: value for key, value in identity.items()
                           if key != "external_id"}
        else:
            v2_identity = dict(identity)
        identity_payload = dict(identity, **{IDENTITY_VERSION_KEY: IDENTITY_VERSION})
        content_hash = _hash(_json(identity_payload, "ingest identity", MAX_CONTENT_CHARS + 8_000))
        v2_payload = dict(v2_identity, **{IDENTITY_VERSION_KEY: IDENTITY_VERSION_V2})
        compat_hash = _hash(_json(v2_payload, "ingest identity", MAX_CONTENT_CHARS + 8_000))
        # v1 key for the same record, used only to recognise pre-v2 rows.  The
        # payload is rebuilt exactly as _legacy_identity_hash reconstructs it:
        # v1 never hashed external_id at all (the reconstruction pins it to the
        # empty string), so neither may the compatibility probe.
        legacy_payload = dict(v2_identity)
        if not raw_chat:
            legacy_payload["external_id"] = ""
        if not raw_chat and explicit_observed_at and not explicit_valid_from:
            # v1 kept the derived observation time unconditionally; that is what
            # the historical rows were minted with, so the compatibility probe
            # must reproduce it exactly.
            legacy_payload["valid_from"] = valid_from
        legacy_hash = _hash(_json(legacy_payload, "ingest identity", MAX_CONTENT_CHARS + 8_000))
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
            # Pre-v2 identity for the same record: ``_insert_prepared`` probes it
            # so a row written before this change is recognised as a duplicate
            # instead of being re-ingested under its new id.
            "legacy_hash": legacy_hash,
            # The v2 digest for this same record: _find_existing_event probes
            # it so rows written by the previous release are duplicates, not
            # fresh events.
            "compat_hash": compat_hash,
            "identity_version": IDENTITY_VERSION,
            "identity_external_id": external_id,
            # Fields the backward-compatibility probe needs.  Kept on the prepared
            # record so _insert_prepared never has to re-derive them from the raw
            # input (which would drift from what identity actually used).
            "identity_content": normalise_for_dedup(content),
            "identity_explicit_valid_from": explicit_valid_from,
            "metadata_json": metadata_json, "fts_content": fts_content, "valid_until": valid_until,
            "raw_chat": int(raw_chat), "weight": weight, "importance": importance,
            "utility": utility, "confidence": confidence, "valid_from": valid_from,
            "asserted_at": now, "claims": claims, "causal_parents": causal_parents,
            "quality_score": quality_score, "quality_state": quality_state,
        }

    @staticmethod
    def _agent_trust_for(conn: sqlite3.Connection, source_agent: str) -> float:
        agent_key = _short(source_agent, 128).lower()
        row = conn.execute("SELECT baseline FROM agent_trust WHERE agent_id=?", (agent_key,)).fetchone()
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
    def _insert_claims(conn: sqlite3.Connection, prepared: Mapping[str, Any], source_trust: float) -> bool:
        """Insert claims for a newly ingested memory; return True if memory stays accepted."""
        claims = list(prepared.get("claims", []))
        if not claims:
            return str(prepared.get("quality_state") or "") == "accepted"
        if str(prepared.get("quality_state") or "") != "accepted":
            # Quarantine / non-accepted memories never activate claims or race live slots.
            for claim in claims:
                claim_id = "cl_" + _hash(
                    prepared["memory_id"], claim["subject"], claim["predicate"], claim["slot_key"]
                )[:32]
                conn.execute("""
                    INSERT INTO memory_claims(
                        claim_id,memory_id,scope,subject,predicate,slot_key,value_json,value_hash,
                        valid_from,valid_until,asserted_at,confidence,source_trust,state,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (claim_id, prepared["memory_id"], prepared["scope"], claim["subject"], claim["predicate"],
                      claim["slot_key"], claim["value_json"], claim["value_hash"], claim["valid_from"],
                      claim["valid_until"], claim["asserted_at"], claim["confidence"], source_trust,
                      "superseded", prepared["asserted_at"]))
            return False

        memory_conflicted = False
        for claim in claims:
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
                  prepared["memory_id"], claim["valid_from"], claim["valid_until"], claim["valid_until"])).fetchall()
            state = "active"
            for old in existing:
                if old["value_hash"] == claim["value_hash"]:
                    continue
                old_score = float(old["confidence"]) * float(old["source_trust"])
                if incoming_score > old_score + 0.05:
                    # 双时态 + 单槽位作用域：高分断言顶掉旧事实时，仅退位当前槽位的旧
                    # claim，并在胜者 valid_from 闭合其窗口；只有当旧记忆不再持有任何
                    # active/conflict claim 时才将整条旧记忆下线，消除爆炸半径。
                    window_end = _supersede_window_end(
                        conn, str(old["valid_from"] or ""),
                        prepared["memory_id"], prepared["asserted_at"])
                    conn.execute("UPDATE memory_claims SET state='superseded',"
                                 "valid_until=CASE WHEN valid_until='' THEN ? "
                                 "ELSE valid_until END "
                                 "WHERE claim_id=? AND state='active'",
                                 (window_end, old["claim_id"]))
                    remaining = int(conn.execute(
                        "SELECT COUNT(*) FROM memory_claims WHERE memory_id=? AND state IN ('active','conflict')",
                        (old["memory_id"],)).fetchone()[0])
                    if remaining == 0:
                        conn.execute("""UPDATE memories SET lifecycle='superseded',superseded_by=?,quality_state='superseded',
                                        valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END,updated_at=?
                                        WHERE memory_id=? AND lifecycle='active'""",
                                     (prepared["memory_id"], window_end,
                                      prepared["asserted_at"], old["memory_id"]))
                        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (old["memory_id"],))
                elif old_score > incoming_score + 0.05:
                    # 既有高置信事实胜出：保持旧 claim/memory 为 accepted，
                    # 只把低分新断言记为 conflict，避免把获胜方也踢出召回。
                    state = "conflict"
                else:
                    # 分数接近、无法裁决：双方同时下线并从 FTS 移除，等待更强证据。
                    state = "conflict"
                    conn.execute("UPDATE memory_claims SET state='conflict' "
                                 "WHERE memory_id=? AND state='active'", (old["memory_id"],))
                    conn.execute("UPDATE memories SET quality_state='conflict',updated_at=? WHERE memory_id=?",
                                 (prepared["asserted_at"], old["memory_id"]))
                    conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (old["memory_id"],))
            if state != "active":
                memory_conflicted = True
            claim_id = "cl_" + _hash(prepared["memory_id"], claim["subject"], claim["predicate"], claim["slot_key"])[:32]
            conn.execute("""
                INSERT INTO memory_claims(
                    claim_id,memory_id,scope,subject,predicate,slot_key,value_json,value_hash,
                    valid_from,valid_until,asserted_at,confidence,source_trust,state,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (claim_id, prepared["memory_id"], prepared["scope"], claim["subject"], claim["predicate"],
                  claim["slot_key"], claim["value_json"], claim["value_hash"], claim["valid_from"],
                  claim["valid_until"], claim["asserted_at"], claim["confidence"], source_trust,
                  "conflict" if memory_conflicted else state,
                  prepared["asserted_at"]))
        if memory_conflicted:
            conn.execute("UPDATE memory_claims SET state='conflict' WHERE memory_id=? AND state='active'",
                         (prepared["memory_id"],))
            conn.execute("UPDATE memories SET quality_state='conflict',updated_at=? WHERE memory_id=?",
                         (prepared["asserted_at"], prepared["memory_id"]))
            return False
        return True

    @classmethod
    def _insert_relations(cls, conn: sqlite3.Connection, prepared: Mapping[str, Any]) -> None:
        default_from = str(prepared.get("valid_from") or "")
        for parent in prepared.get("causal_parents", []):
            parent_id = str(parent.get("memory_id", ""))
            if not parent_id or conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (parent_id,)).fetchone() is None:
                continue
            if cls._relation_would_cycle(conn, parent_id, prepared["memory_id"]):
                raise LedgerError("causal relation 不能形成环路")
            relation_id = "rel_" + _hash(parent_id, prepared["memory_id"], parent.get("relation_type", "caused_by"))[:32]
            rel_from = _timestamp(parent["valid_from"]) if parent.get("valid_from") else default_from
            rel_until = _timestamp(parent["valid_until"]) if parent.get("valid_until") else ""
            conn.execute("""
                INSERT OR IGNORE INTO memory_relations(
                    relation_id,parent_memory_id,child_memory_id,relation_type,confidence,created_at,valid_from,valid_until
                ) VALUES(?,?,?,?,?,?,?,?)
            """, (relation_id, parent_id, prepared["memory_id"], parent.get("relation_type", "caused_by"),
                  _clamp_float(parent.get("confidence", 0.5), 0.5, 0.0, 1.0), prepared["asserted_at"],
                  rel_from, rel_until))

    @staticmethod
    def _legacy_identity_hash(row: Any) -> str:
        """Recompute the **v1** identity hash for an already-stored row.

        Used to recognise rows written before :data:`IDENTITY_VERSION` 2 without
        a schema change: a v2 row can never reproduce its own v1 hash (v2 adds
        ``identity_version`` to the payload), so an exact match proves the row
        was minted under the old rule.
        """
        # The two v1 branches are structurally different and must be rebuilt
        # exactly: ``_json`` sorts keys, but an extra or missing field changes the
        # digest.  Structured rows never hashed ``role`` (v1 deliberately dropped
        # it so a re-reported decision was not a new observation); raw rows never
        # hashed ``kind``/``task_id``/``project``/``scope``/``valid_*``.
        try:
            raw = int(row["raw_chat"] or 0)
            if raw:
                payload = {
                    "class": "raw",
                    "source_agent": row["source_agent"],
                    "session_id": row["session_id"],
                    "role": row["role"],
                    "content": normalise_for_dedup(row["content"]),
                }
            else:
                payload = {
                    "class": "structured",
                    "kind": row["kind"],
                    "source_agent": row["source_agent"],
                    "session_id": row["session_id"],
                    "external_id": "",
                    "task_id": row["task_id"],
                    "project": row["project"],
                    "scope": row["scope"],
                    "valid_from": row["valid_from"],
                    "valid_until": row["valid_until"],
                    "content": normalise_for_dedup(row["content"]),
                }
        except (KeyError, IndexError, TypeError, ValueError):
            return ""
        return _hash(_json(payload, "legacy identity", MAX_CONTENT_CHARS + 8_000))

    @staticmethod
    def _find_existing_event(conn: sqlite3.Connection,
                             prepared: Mapping[str, Any]) -> Any:
        """Return the already-stored event for this record, if any.

        Four probes, cheapest first:

        1. **current identity (v3)** -- an indexed UNIQUE lookup, the steady
           state for every write after the upgrade.
        2. **previous identity (v2) recomputed for this record** -- v3 only
           *adds* the external id to the raw key (the structured key is
           unchanged), so the v2 digest of this same record catches rows
           written by the release being upgraded from.  Without it the first
           capture pass after the upgrade would re-ingest the whole ledger
           under v3 ids (the 141-duplicate production storm).
        3. **legacy identity (v1) recomputed for this record** -- catches an
           idempotent replay of exactly the same v1 observation.
        4. **bounded semantic probe** -- the important one for backward
           compatibility.  A v1 row hashed the *derived* observation time, so a
           re-observation with a refreshed timestamp produces a v1 hash that
           matches nothing.  Probe 3 therefore cannot find it; only comparing
           normalised content within the same (task|session, agent, scope)
           bucket can.  Candidates are confirmed to be v1 rows by recomputing
           their own v1 hash, so a genuine v2/v3 row is never silently merged.

        Probe 4 is skipped when the record carries an **explicit** valid_from:
        that is a deliberate bitemporal assertion, and two different dates must
        stay two observations.
        """
        current = str(prepared.get("content_hash") or "")
        existing = conn.execute(
            "SELECT event_id, content_hash FROM ledger_events WHERE content_hash=?",
            (current,)).fetchone()
        if existing is not None:
            return existing
        compat = str(prepared.get("compat_hash") or "")
        if compat and compat != current:
            existing = conn.execute(
                "SELECT event_id, content_hash FROM ledger_events WHERE content_hash=?",
                (compat,)).fetchone()
            if existing is not None:
                return existing
        legacy = str(prepared.get("legacy_hash") or "")
        if legacy and legacy != current:
            existing = conn.execute(
                "SELECT event_id, content_hash FROM ledger_events WHERE content_hash=?",
                (legacy,)).fetchone()
            if existing is not None:
                return existing
        if bool(prepared.get("identity_explicit_valid_from")):
            return None
        return IdentityMixin._legacy_semantic_probe(conn, prepared)

    #: Candidate cap for the legacy semantic probe.  Bounded so an ingest can
    #: never degrade into a table scan; 64 covers every observed duplicate group
    #: (the largest in the production ledger is 293 rows for archive sources, but
    #: those carry a stable ``event_id`` and never reach this path).
    LEGACY_PROBE_LIMIT = 64

    @staticmethod
    def _legacy_semantic_probe(conn: sqlite3.Connection,
                               prepared: Mapping[str, Any]) -> Any:
        """Find a pre-v2 row describing the same observation (bounded, indexed).

        Narrowing uses the two existing indexes -- ``idx_memories_task``
        (task_id) or ``idx_memories_source`` (source_agent, session_id) -- so the
        probe costs a sub-millisecond index seek rather than a scan.  Content is
        then compared in its normalised form, which is the same canonicalisation
        the identity key uses.
        """
        agent = str(prepared.get("source_agent") or "")
        scope = str(prepared.get("scope") or "")
        task_id = str(prepared.get("task_id") or "")
        session_id = str(prepared.get("session_id") or "")
        if not agent:
            return None
        if task_id:
            sql = ("SELECT * FROM memories WHERE task_id=? AND source_agent=? "
                   "AND scope=? AND lifecycle!='superseded' LIMIT ?")
            params: Any = (task_id, agent, scope, IdentityMixin.LEGACY_PROBE_LIMIT)
        elif session_id:
            sql = ("SELECT * FROM memories WHERE source_agent=? AND session_id=? "
                   "AND scope=? AND lifecycle!='superseded' LIMIT ?")
            params = (agent, session_id, scope, IdentityMixin.LEGACY_PROBE_LIMIT)
        else:
            # No indexed narrowing available: refuse to scan.  Probe 3 (the
            # recomputed v1 digest) still covers the exact-replay case here.
            return None
        target = str(prepared.get("identity_content") or "")
        if not target:
            return None
        try:
            candidates = conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError:
            return None
        for row in candidates:
            if normalise_for_dedup(row["content"]) != target:
                continue
            # Only merge onto a row that really was minted under v1: recompute its
            # own v1 hash and compare with the hash actually stored on its event.
            # A v2 row can never reproduce its own v1 hash (v2 adds
            # ``identity_version`` to the payload), so this cannot merge two
            # genuinely distinct v2 observations.
            event = conn.execute(
                "SELECT event_id, content_hash FROM ledger_events WHERE event_id=?",
                (row["origin_event_id"],)).fetchone()
            if event is None:
                continue
            if IdentityMixin._legacy_identity_hash(row) != str(event["content_hash"] or ""):
                continue
            return event
        return None

    #: Cap for the external-id revision probe.  Every candidate set is bounded
    #: so a streamed bubble can never turn an ingest into a table scan.
    REVISION_PROBE_LIMIT = 256

    @staticmethod
    def _metadata_value(metadata_json: Any, key: str) -> str:
        try:
            metadata = json.loads(metadata_json or "{}")
        except (TypeError, ValueError):
            return ""
        if not isinstance(metadata, dict):
            return ""
        value = metadata.get(key)
        return value if isinstance(value, str) else ""

    @staticmethod
    def _json_string_list_has(encoded: Any, needle: str) -> bool:
        try:
            values = json.loads(encoded or "[]")
        except (TypeError, ValueError):
            return False
        if not isinstance(values, list):
            return False
        return needle in [str(value) for value in values]

    @staticmethod
    def _revision_row_matches(row: Any, external_id: str,
                              evidence: List[str], raw_chat: bool) -> bool:
        """True when this stored row is an older revision of the same event.

        A v3 row is decided by its projection-level event id only: a caller may
        reuse one evidence string across structured records, so evidence must
        never overrule an explicit id.  Rows written before v3 have no
        projection-level id, and the stable evidence string is the only bridge
        back to their event for raw streaming capture.
        """
        stored_external = IdentityMixin._metadata_value(
            row["metadata_json"], "external_id")
        if stored_external:
            return stored_external == external_id
        if not raw_chat:
            return False
        return any(IdentityMixin._json_string_list_has(
                       row["evidence_json"], reference)
                   for reference in evidence)

    @staticmethod
    def _revision_candidates(conn: sqlite3.Connection,
                             prepared: Mapping[str, Any]) -> List[Any]:
        """Rows that describe the *same source event* as ``prepared``.

        Streaming sources rewrite one bubble in place, so one stable event id
        arrives several times with growing content.  Earlier rows must not
        co-exist with the finished revision.

        The probe is bounded in two different ways, because the two shapes have
        different anchors:

        * records that name a task/session use the same indexed
          (task|session, agent, scope) bucket as the legacy semantic probe, so
          the scan is an index seek rather than a table walk;
        * records without any anchor fall back to the *newest* REVISION_PROBE_LIMIT
          rows (rowid DESC).  A live event's previous revision was inserted
          moments earlier, so it is necessarily inside that bounded window;
          an unindexed full-table LIKE scan per ingest would not be.
        """
        external_id = str(prepared.get("identity_external_id") or "")
        if not external_id:
            return []
        agent = str(prepared.get("source_agent") or "")
        scope = str(prepared.get("scope") or "")
        task_id = str(prepared.get("task_id") or "")
        session_id = str(prepared.get("session_id") or "")
        memory_id = str(prepared.get("memory_id") or "")
        raw_chat = bool(prepared.get("raw_chat"))
        try:
            parsed_evidence = json.loads(prepared.get("evidence_json") or "[]")
        except (TypeError, ValueError):
            parsed_evidence = []
        evidence: List[str] = []
        if isinstance(parsed_evidence, list):
            evidence = [str(item) for item in parsed_evidence if str(item)][:4]
        if task_id:
            bucket: Any = ("task_id=? AND source_agent=? AND scope=?",
                           (task_id, agent, scope))
        elif session_id:
            bucket = ("source_agent=? AND session_id=? AND scope=?",
                      (agent, session_id, scope))
        else:
            bucket = None
        if bucket is None:
            sql = ("SELECT * FROM memories WHERE lifecycle='active' "
                   "AND memory_id!=? ORDER BY rowid DESC LIMIT ?")
            try:
                candidates = conn.execute(
                    sql, (memory_id, IdentityMixin.REVISION_PROBE_LIMIT)).fetchall()
            except sqlite3.DatabaseError:
                return []
            return [row for row in candidates
                    if IdentityMixin._revision_row_matches(
                        row, external_id, evidence, raw_chat)]
        bucket_sql, bucket_params = bucket
        # LIKE only prefilters inside the indexed bucket; the exact check above
        # decides, so a wildcard inside an id cannot retire an unrelated row.
        metadata_pattern = '%"external_id":' + json.dumps(
            external_id, ensure_ascii=False) + '%'
        filters: List[Any] = [("metadata_json LIKE ?", (metadata_pattern,))]
        if raw_chat:
            for reference in evidence:
                filters.append(("evidence_json LIKE ?", ("%" + reference + "%",)))
        found: Dict[str, Any] = {}
        for column_filter, filter_params in filters:
            conditions = [column_filter, bucket_sql, "lifecycle='active'",
                          "memory_id!=?"]
            params: Any = (*filter_params, *bucket_params, memory_id)
            sql = ("SELECT * FROM memories WHERE " + " AND ".join(conditions) +
                   " ORDER BY created_at ASC LIMIT ?")
            try:
                rows = conn.execute(
                    sql, (*params, IdentityMixin.REVISION_PROBE_LIMIT)).fetchall()
            except sqlite3.DatabaseError:
                continue
            for row in rows:
                candidate_id = str(row["memory_id"] or "")
                if not candidate_id or candidate_id in found:
                    continue
                if IdentityMixin._revision_row_matches(
                        row, external_id, evidence, raw_chat):
                    found[candidate_id] = row
        return list(found.values())

    @classmethod
    def _supersede_external_revisions(cls, conn: sqlite3.Connection,
                                      prepared: Mapping[str, Any]) -> int:
        """Retire older in-place revisions of the same source event.

        Streaming capture rewrites one record while the answer grows.  Every
        revision carries the same explicit event id, so the older row stops
        describing the event the moment newer content arrives.  This runs
        inside the insert transaction, after the new projection exists (the
        loser window closes on the winner valid_from) and *before* claims are
        arbitrated (a retired draft must not fight the finished answer for its
        slot).  Only rows whose normalised content actually differs are
        retired: different identity with the same text (an explicitly dated
        repeat) stays two observations, exactly as v2 did.
        """
        target = str(prepared.get("identity_content") or "")
        retired = 0
        for row in cls._revision_candidates(conn, prepared):
            if normalise_for_dedup(row["content"]) == target:
                continue
            window_end = _supersede_window_end(
                conn, str(row["valid_from"] or ""),
                str(prepared["memory_id"]), str(prepared["asserted_at"]))
            # Same retirement contract as claim arbitration: every active claim
            # on the losing projection leaves the active set, otherwise health
            # reports dangling active claims forever.
            conn.execute(
                "UPDATE memory_claims SET state='superseded',"
                "valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END "
                "WHERE memory_id=? AND state='active'",
                (window_end, row["memory_id"]))
            updated = conn.execute(
                "UPDATE memories SET lifecycle='superseded',superseded_by=?,"
                "quality_state='superseded',"
                "valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END,"
                "updated_at=? WHERE memory_id=? AND lifecycle='active'",
                (prepared["memory_id"], window_end, prepared["asserted_at"],
                 row["memory_id"]))
            if not updated.rowcount:
                continue
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
            retired += 1
        return retired

    @classmethod
    def _tag_external_id(cls, conn: sqlite3.Connection, memory_id: str,
                         metadata_json: Any,
                         prepared: Mapping[str, Any]) -> None:
        """Attach a v3 event id to an already-stored projection.

        A row written by v2 has no event id in its projection metadata, so the
        first v3 revision after the upgrade could not find it to supersede.
        Tagging the surviving duplicate keeps the revision chain connected.
        Identity is untouched: metadata is projection data and is not part of
        any identity key.
        """
        external_id = str(prepared.get("identity_external_id") or "")
        if not external_id or not memory_id:
            return
        try:
            metadata = json.loads(metadata_json or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get("external_id") == external_id:
            return
        metadata["external_id"] = external_id
        try:
            encoded = _json(metadata, "metadata")
        except LedgerError:
            return
        conn.execute("UPDATE memories SET metadata_json=? WHERE memory_id=?",
                     (encoded, memory_id))

    @classmethod
    def _insert_prepared(cls, conn: sqlite3.Connection, prepared: Mapping[str, Any]) -> IngestResult:
        existing = cls._find_existing_event(conn, prepared)
        if existing is not None:
            row = conn.execute(
                "SELECT memory_id, metadata_json FROM memories WHERE origin_event_id=?",
                (existing["event_id"],)).fetchone()
            memory_id = row["memory_id"] if row else prepared["memory_id"]
            # Upgrade path: tag the surviving row so the *next* revision of
            # this same source event can find and supersede it.
            cls._tag_external_id(conn, memory_id,
                                 row["metadata_json"] if row else "", prepared)
            return IngestResult(existing["event_id"], memory_id,
                                False, existing["content_hash"])
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
        # Streaming revision chain: retire drafts of this same source event
        # before claims are arbitrated (see _supersede_external_revisions).
        cls._supersede_external_revisions(conn, prepared)
        still_accepted = cls._insert_claims(conn, prepared, source_trust)
        cls._insert_relations(conn, prepared)
        if still_accepted and prepared["quality_state"] == "accepted":
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
