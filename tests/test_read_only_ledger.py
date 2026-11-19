"""SQLiteLedger(read_only=True) 契约（WP-B：只读镜像/巡检副本）。

覆盖：
- 文件必须存在；只读打开不 bootstrap/迁移/chmod，主库 size/mtime 不变；
- 连接用 mode=ro + PRAGMA query_only，_connect(write=True)/_transaction/写 API 拒绝；
- maintenance_checkpoint 降级为空操作；
- schema 版本不匹配（旧/新/非 SQLite 文件）在打开时显式失败且不迁移；
- close() 回收所有线程的 reader，close() 后可重连。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import LedgerError, SCHEMA_VERSION, SQLiteLedger


def _seed(path: Path) -> str:
    ledger = SQLiteLedger(path)
    try:
        result = ledger.ingest({"content": "只读账本探针", "kind": "fact",
                                "source_agent": "t", "evidence": ["t:1"]})
    finally:
        ledger.close()
    return result.memory_id


def _file_state(path: Path):
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


def _set_schema_version(path: Path, version: int) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'",
                     (str(version),))
        conn.commit()


class ReadOnlyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-ro-ledger-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "hub.sqlite"
        self.memory_id = _seed(self.path)

    def open_ro(self) -> SQLiteLedger:
        ledger = SQLiteLedger(self.path, read_only=True)
        self.addCleanup(ledger.close)
        return ledger

    # -- construction --------------------------------------------------
    def test_missing_file_is_rejected(self):
        missing = Path(self.tmp.name) / "missing.sqlite"
        with self.assertRaises(LedgerError):
            SQLiteLedger(missing, read_only=True)
        self.assertFalse(missing.exists(), "只读构造绝不能创建账本文件")

    def test_open_does_not_touch_the_file(self):
        before = _file_state(self.path)
        ledger = self.open_ro()
        self.assertTrue(ledger.read_only)
        ledger.stats()
        ledger.close()
        self.assertEqual(before, _file_state(self.path),
                         "只读打开/查询不得改动主库 size/mtime")

    def test_stale_schema_is_rejected_without_migrating(self):
        _set_schema_version(self.path, 7)
        before = _file_state(self.path)
        with self.assertRaises(LedgerError) as ctx:
            SQLiteLedger(self.path, read_only=True)
        self.assertIn("schema", str(ctx.exception))
        self.assertEqual(before, _file_state(self.path))
        with sqlite3.connect(str(self.path)) as conn:
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual("7", str(version), "只读打开绝不能执行迁移")

    def test_future_schema_is_rejected(self):
        _set_schema_version(self.path, SCHEMA_VERSION + 1)
        with self.assertRaises(LedgerError):
            SQLiteLedger(self.path, read_only=True)

    def test_non_database_file_is_rejected(self):
        junk = Path(self.tmp.name) / "junk.sqlite"
        junk.write_bytes(b"this is definitely not a sqlite database\n" * 64)
        before = junk.stat().st_size
        with self.assertRaises(LedgerError):
            SQLiteLedger(junk, read_only=True)
        self.assertEqual(before, junk.stat().st_size)

    # -- reads ---------------------------------------------------------
    def test_reads_work(self):
        ledger = self.open_ro()
        stats = ledger.stats()
        self.assertEqual(1, stats["memories"])
        self.assertEqual("WAL", stats["journal_mode"])
        memory = ledger.get_memory(self.memory_id)
        self.assertIsNotNone(memory)
        self.assertEqual("只读账本探针", memory["content"])
        hits = ledger.search("探针", scope="shared")
        self.assertEqual(1, len(hits))
        self.assertEqual(self.memory_id, hits[0]["memory_id"])

    # -- write refusals -------------------------------------------------
    def test_connections_are_read_only(self):
        ledger = self.open_ro()
        with ledger._reading() as conn:
            self.assertEqual(1, int(conn.execute("PRAGMA query_only").fetchone()[0]))
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("INSERT INTO schema_meta(key,value) VALUES('probe','1')")
        conn = ledger._connect()
        try:
            self.assertEqual(1, int(conn.execute("PRAGMA query_only").fetchone()[0]))
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("CREATE TABLE should_not_exist(x)")
        finally:
            conn.close()

    def test_write_paths_are_rejected_and_change_nothing(self):
        ledger = self.open_ro()
        before = _file_state(self.path)
        attempts = {
            "connect-write": lambda: ledger._connect(write=True),
            "transaction": lambda: ledger._transaction(lambda conn: None),
            "ingest": lambda: ledger.ingest({
                "content": "只读写入尝试", "kind": "fact",
                "source_agent": "t", "evidence": ["t:2"]}),
            "feedback": lambda: ledger.feedback(self.memory_id, 1.0, "note", "t"),
            "maintain": lambda: ledger.maintain(archive_raw=True, purge_raw=True),
        }
        for label, attempt in attempts.items():
            with self.subTest(attempt=label):
                with self.assertRaises(LedgerError) as ctx:
                    attempt()
                self.assertIn("只读", str(ctx.exception))
        self.assertEqual(before, _file_state(self.path),
                         "被拒绝的写尝试不得改动账本文件")

    def test_maintenance_checkpoint_is_a_noop(self):
        ledger = self.open_ro()
        before = _file_state(self.path)
        summary = ledger.maintenance_checkpoint()
        self.assertIs(summary.get("read_only"), True)
        self.assertFalse(summary.get("wal_truncated"))
        self.assertIsNotNone(summary.get("optimize"))
        self.assertEqual(before, _file_state(self.path))

    # -- close() -------------------------------------------------------
    def test_close_reclaims_every_thread_reader_and_reconnects(self):
        ledger = self.open_ro()
        readers = []

        def worker() -> None:
            ledger.stats()
            readers.append(ledger._tls.reader)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(3, len(readers))
        self.assertGreaterEqual(len(ledger._readers), 3)

        ledger.close()
        for index, conn in enumerate(readers):
            with self.subTest(reader=index):
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")
        self.assertEqual({}, ledger._readers)

        # close() 不是终态：下次读取会自动重连，同一实例可继续服务。
        self.assertEqual(1, ledger.stats()["memories"])
        ledger.close()
        ledger.close()  # 幂等

    def test_close_from_another_thread_does_not_raise(self):
        ledger = self.open_ro()
        captured = []

        def worker() -> None:
            ledger.stats()
            captured.append(ledger._tls.reader)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        ledger.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            captured[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
