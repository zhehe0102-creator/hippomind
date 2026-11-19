"""Integrity, FTS sync, claims consistency, statistics, health overview.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict
from ..schema import fts_indexed_predicate
from ._helpers import (
    DEFAULT_RAW_CAPACITY,
    DEFAULT_RAW_RETENTION_DAYS,
    RAW_ARCHIVE_DIR_NAME,
    SCHEMA_VERSION,
)

class HealthMixin:
    """Integrity, FTS sync, claims consistency, statistics, health overview."""

    def integrity_check(self) -> Dict[str, Any]:
        with self._reading() as conn:
            status = conn.execute("PRAGMA integrity_check").fetchone()[0]
        wal = Path(str(self.path) + "-wal")
        return {"status": str(status), "wal_bytes": wal.stat().st_size if wal.exists() else 0}

    def fts_sync_report(self) -> Dict[str, Any]:
        """Report projections that *should* be in FTS but are not.

        Two defects made the previous implementation unusable as a health gate
        on a real ledger (both found during the 2026-10-19 production compaction
        and fixed here):

        1. **Quadratic scan.** ``memory_fts.memory_id`` is declared
           ``UNINDEXED``, so the correlated ``NOT EXISTS`` subquery re-scanned
           the whole FTS content table once per projection:
           O(memories x fts_rows).  On the 1.53M-row ledger that is ~8.7e9
           comparisons and measured >14 minutes of CPU without converging.
           The FTS ids are now materialised once into a temp table and
           anti-joined against it, which is linear in the FTS row count.

        2. **Wrong ``unindexed`` semantics.** The predicate counted
           ``lifecycle IN ('active','cold')``, but ``maintain`` deliberately
           removes cold rows from FTS ("Cold projections remain auditable in
           SQLite, but must leave the hot FTS index").  Every ledger holding
           cold rows therefore reported ``warn`` forever, which trains
           operators to ignore the check.

        Membership now comes from :data:`hippomind.schema.FTS_INDEXED_PREDICATE`
        — the same single definition used by the write path, ``_rebuild_fts``,
        ``health_overview`` and ``maintain``'s prune.  Keeping a private copy
        here is what let the audit call a 93%-garbage ledger healthy.

        ``status`` is ``ok`` when nothing that should be indexed is missing.
        Rows in FTS that should not be there (cold/quarantined leftovers) are
        reported separately as ``stale``: they waste index space but do not
        make recall incorrect, so they do not by themselves fail the check.
        """
        indexed = fts_indexed_predicate()
        with self._reading() as conn:
            # 纯只读扫描：不在 temp 库建表，保证 SQLiteLedger(read_only=True) 的
            # PRAGMA query_only=ON 连接也能正常诊断，同时保持 O(N) 线性时间。
            fts_id_list = [str(r[0]) for r in conn.execute("SELECT memory_id FROM memory_fts")]
            expected_ids = {str(r[0]) for r in conn.execute(
                "SELECT memory_id FROM memories WHERE " + indexed)}
            fts_rows = len(fts_id_list)
            fts_set = set(fts_id_list)
            duplicate_rows = fts_rows - len(fts_set)
            expected = len(expected_ids)
            missing = len(expected_ids - fts_set)
            extra = sorted(fts_set - expected_ids)
            stale = 0
            if extra:
                for start in range(0, len(extra), 400):
                    chunk = extra[start:start + 400]
                    marks = ",".join("?" for _ in chunk)
                    stale += int(conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE memory_id IN (" + marks + ")",
                        tuple(chunk),
                    ).fetchone()[0])
            orphan = len(extra) - stale
        status = "ok" if missing == 0 and duplicate_rows == 0 else "warn"
        if missing:
            hint = "运行 hippomind maintain 可重建 FTS"
        elif duplicate_rows:
            hint = "FTS 存在重复索引行，运行 hippomind maintain 可清理"
        elif stale or orphan:
            hint = "存在陈旧/孤儿 FTS 索引行，运行 hippomind maintain 可清理"
        else:
            hint = None
        return {"status": status,
                # ``projections`` keeps its historical name (it is the count of
                # rows that ought to be indexed) but now uses the correct
                # predicate, so it is comparable with ``fts_rows``.
                "projections": expected,
                "fts_rows": fts_rows,
                "unindexed": missing,
                "stale": stale,
                "orphan": orphan,
                # Duplicate index rows: capacity waste and a sign of an
                # interrupted writer, never a missing projection.
                "duplicate_rows": duplicate_rows,
                "hint": hint}

    def claims_consistency_report(self) -> Dict[str, Any]:
        """活跃 claim 指向的记忆必须存在且满足 accepted+active。"""
        indexed_m = fts_indexed_predicate("m")
        with self._reading() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_claims c LEFT JOIN memories m "
                "ON m.memory_id=c.memory_id WHERE c.state='active' "
                "AND (m.memory_id IS NULL OR NOT (" + indexed_m + "))"
            ).fetchone()[0]
            total = conn.execute(
                "SELECT COUNT(*) FROM memory_claims WHERE state='active'").fetchone()[0]
        status = "ok" if row == 0 else "warn"
        return {"status": status, "active_claims": int(total), "dangling": int(row)}

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
            # 审计口径与 _rebuild_fts / maintain 共用同一个不变量常量：
            # FTS 恰好是 {accepted AND active}。此前这里用「除 raw 且非 active
            # 外全部入索引」，与 fts_sync_report 的 accepted+active 互相矛盾，
            # 真实库上 5,571 行索引里 5,179 行不可召回却被判为「无漂移」。
            expected_indexed = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE " + fts_indexed_predicate()
            ).fetchone()[0]
            try:
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                db_bytes = int(page_size) * int(page_count)
            except Exception:
                db_bytes = 0
        # FTS 体积审计：对照应索引集合检测漂移；占比供容量规划。
        drift = int(fts_rows) != int(expected_indexed)
        if not drift:
            note = "ok"
        elif int(fts_rows) < int(expected_indexed):
            # 行数偏少 = 应索引的行缺失，只有结构性重建能补回。
            note = "FTS 行数与应索引集合不一致，建议 checkup --repair"
        else:
            # 行数偏多 = 不可召回的陈旧索引行。``checkup --repair`` 只在
            # ``unindexed>0`` 时触发（它负责补缺失，不负责清理），所以这里
            # 必须指向真正会收敛索引的命令；否则提示会让人跑一个空操作。
            note = "FTS 存在不可召回的陈旧索引行，建议运行 hippomind maintain"
        return {"memories": int(memories), "quarantined": int(quarantined),
                "pinned": int(pinned), "events": int(events),
                "fts": {"rows": int(fts_rows),
                        "expected": int(expected_indexed),
                        "drift": drift,
                        "db_bytes": db_bytes,
                        "note": note},
                "schema_version": SCHEMA_VERSION}

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
            for pattern in ("raw-*.jsonl.gz", "purged-*.jsonl.gz", "dedupe-*.jsonl.gz"):
                for archive in archive_root.glob(pattern):
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
