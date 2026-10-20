"""Feedback, decay, retention, consolidation, pinning, forgetting, vacuum.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from ._helpers import (
    CONTEXT_FIELDS,
    DEFAULT_AUTO_VACUUM_FREELIST_RATIO,
    DEFAULT_AUTO_VACUUM_MIN_BYTES,
    DEFAULT_RAW_ARCHIVE_MAX_BYTES,
    DEFAULT_RAW_CAPACITY,
    DEFAULT_RAW_RETENTION_DAYS,
    IngestResult,
    LedgerError,
    MAX_EVIDENCE,
    _clamp_float,
    _clamp_int,
    _epoch,
    _fts_text,
    _fts_tokenizer,
    _hash,
    _json,
    _short,
    _strings,
    _text,
    _timestamp,
    utc_now,
)

class LifecycleMixin:
    """Feedback, decay, retention, consolidation, pinning, forgetting, vacuum."""

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
