"""fts_sync_report：线性耗时 + 正确的 unindexed 语义（回归测试）。

2026-10-19 线上压缩时发现两个真缺陷，本测试锁定修复后的行为：

1. **二次复杂度**：``memory_fts.memory_id`` 是 ``UNINDEXED``，旧的
   ``NOT EXISTS`` 相关子查询对每条投影重扫整个 FTS 内容表
   （O(memories x fts_rows)）。1.53M 行库上实测 >14 分钟未收敛。
   现在先物化 FTS id 再 anti-join，是线性量级。
2. **unindexed 语义错误**：旧谓词统计 ``lifecycle IN ('active','cold')``，
   但 maintain 是**故意**把 cold 行移出 FTS 的，所以任何有冷数据的库都会
   永久 ``warn``。只有 ``active`` + ``accepted`` 才应该被索引。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FtsSyncReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "fts.sqlite")
        self.ledger.ingest_many([
            {"content": f"FTS 同步测试记忆 {i}", "kind": "fact",
             "source_agent": "t", "evidence": [f"t:{i}"]}
            for i in range(12)])

    def tearDown(self):
        self.ledger.close()

    # -- helpers ---------------------------------------------------------
    def _set_lifecycle(self, memory_id: str, lifecycle: str) -> None:
        """Move a row out of the hot path the way maintain() does."""
        conn = self.ledger._connect(write=True)
        try:
            conn.execute("UPDATE memories SET lifecycle=? WHERE memory_id=?",
                         (lifecycle, memory_id))
            if lifecycle != "active":
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
            conn.commit()
        finally:
            conn.close()

    def _ids(self) -> list:
        return [row["memory_id"] for row in
                self.ledger.search("FTS 同步测试记忆", limit=50)]

    # -- 1) cold rows must not raise a permanent warn ---------------------
    def test_cold_rows_are_not_counted_as_unindexed(self):
        ids = self._ids()
        self.assertGreaterEqual(len(ids), 6)
        for memory_id in ids[:5]:
            self._set_lifecycle(memory_id, "cold")

        report = self.ledger.fts_sync_report()
        self.assertEqual(report["unindexed"], 0,
                         "cold 行被故意移出 FTS，不应算作漏索引")
        self.assertEqual(report["status"], "ok")
        self.assertIsNone(report["hint"])
        # maintain() removes the cold row's index entry too, so nothing is
        # stale; the point is that a cold row never makes the check fail.
        self.assertEqual(report["stale"], 0)

    # -- 2) a genuinely missing active row must be reported ---------------
    def test_missing_active_row_is_detected(self):
        conn = self.ledger._connect(write=True)
        try:
            victim = conn.execute(
                "SELECT memory_id FROM memory_fts LIMIT 1").fetchone()[0]
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (victim,))
            conn.commit()
        finally:
            conn.close()

        report = self.ledger.fts_sync_report()
        self.assertEqual(report["unindexed"], 1)
        self.assertEqual(report["status"], "warn")
        self.assertTrue(report["hint"])

    def test_quarantined_rows_do_not_count_as_unindexed(self):
        conn = self.ledger._connect(write=True)
        try:
            victim = conn.execute(
                "SELECT memory_id FROM memory_fts LIMIT 1").fetchone()[0]
            conn.execute(
                "UPDATE memories SET quality_state='quarantine' WHERE memory_id=?",
                (victim,))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (victim,))
            conn.commit()
        finally:
            conn.close()

        report = self.ledger.fts_sync_report()
        self.assertEqual(report["unindexed"], 0,
                         "quarantine 行不应进 FTS，也不算漏索引")
        self.assertEqual(report["status"], "ok")

    def test_projection_count_matches_indexed_rows_when_in_sync(self):
        report = self.ledger.fts_sync_report()
        self.assertEqual(report["unindexed"], 0)
        self.assertEqual(report["projections"], report["fts_rows"],
                         "同步状态下投影数应等于 FTS 行数")
        self.assertEqual(report["status"], "ok")

    def test_orphaned_fts_rows_are_reported(self):
        conn = self.ledger._connect(write=True)
        try:
            conn.execute(
                "INSERT INTO memory_fts(memory_id,content) VALUES(?,?)",
                ("mem-ghost", "孤儿索引行"))
            conn.commit()
        finally:
            conn.close()

        report = self.ledger.fts_sync_report()
        self.assertEqual(report["orphan"], 1)
        # An orphan entry is index garbage, not a missing projection.
        self.assertEqual(report["unindexed"], 0)

    # -- 3) linear-time guard -------------------------------------------
    def test_scales_linearly_on_large_ledger(self):
        """50k 行规模下必须远低于旧实现的分钟级耗时。

        旧的二次实现对此规模约需 2.5e9 次比较；线性实现是毫秒到百毫秒级。
        阈值取 2s：足够宽以吸收慢 CI 机器，又足以在复杂度回退时失败。
        """
        import sqlite3
        db = Path(self.tmp.name) / "bulk.sqlite"
        if db.exists():
            db.unlink()
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY, origin_event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, content TEXT NOT NULL, source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '[]',
                tags_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                last_access_at TEXT NOT NULL DEFAULT '', last_weighted_at TEXT NOT NULL,
                valid_until TEXT NOT NULL DEFAULT '', superseded_by TEXT NOT NULL DEFAULT '',
                archived_at TEXT NOT NULL DEFAULT '', lifecycle TEXT NOT NULL DEFAULT 'active',
                raw_chat INTEGER NOT NULL DEFAULT 0, weight REAL NOT NULL DEFAULT 0.5,
                importance INTEGER NOT NULL DEFAULT 5, touch_count INTEGER NOT NULL DEFAULT 0,
                utility REAL NOT NULL DEFAULT 0.0, confidence REAL NOT NULL DEFAULT 0.5,
                success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
                quality_state TEXT NOT NULL DEFAULT 'accepted',
                valid_from TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE ledger_events (
                event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
                source_agent TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]', content TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE, metadata_json TEXT NOT NULL DEFAULT '{}',
                asserted_at TEXT NOT NULL DEFAULT ''
            );
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                memory_id UNINDEXED, content, task_id, project, source_agent, kind,
                tokenize='trigram');
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version','8');
        """)
        total = 50_000
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO memories(memory_id,origin_event_id,kind,content,source_agent,"
            "scope,created_at,updated_at,last_weighted_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [(f"mem_{i:06d}", f"evt_{i:06d}", "fact", f"批量记忆内容 {i}",
              "t", "shared", "2026-01-01T00:00:00+00:00",
              "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
             for i in range(total)])
        conn.executemany(
            "INSERT INTO memory_fts(memory_id,content) VALUES(?,?)",
            [(f"mem_{i:06d}", f"批量记忆内容 {i}") for i in range(total)])
        conn.commit()
        conn.close()

        from hippomind.ledger import SQLiteLedger
        ledger = SQLiteLedger(db)
        self.addCleanup(ledger.close)
        started = time.monotonic()
        report = ledger.fts_sync_report()
        elapsed = time.monotonic() - started

        self.assertEqual(report["unindexed"], 0)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["projections"], total)
        self.assertLess(elapsed, 2.0,
                        f"fts_sync_report 在 {total} 行上耗时 {elapsed:.2f}s，"
                        "疑似回退为二次复杂度")


if __name__ == "__main__":
    unittest.main()
