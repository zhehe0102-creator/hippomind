"""ingest 幂等契约：重复 canonical 记录返回已有 memory_id（迭代 72）。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger


class IngestIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-dup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "d.sqlite")
        self.record = {"content": "同一时刻同一内容", "kind": "fact",
                       "source_agent": "t", "evidence": ["t:1"],
                       "occurred_at": "2026-08-22T08:00:00+00:00"}

    def test_duplicate_returns_existing_memory_id(self):
        r1 = self.led.ingest(self.record)
        r2 = self.led.ingest(self.record)
        self.assertTrue(r1.inserted)
        self.assertFalse(r2.inserted, "相同 canonical 记录不应二次插入")
        self.assertEqual(r1.memory_id, r2.memory_id)
        self.assertEqual(r1.event_id, r2.event_id)

    def test_new_observation_time_creates_new_memory(self):
        r1 = self.led.ingest(self.record)
        later = dict(self.record, occurred_at="2026-08-22T09:00:00+00:00")
        r2 = self.led.ingest(later)
        self.assertTrue(r2.inserted)
        self.assertNotEqual(r1.memory_id, r2.memory_id,
                            "append-only 设计：新观测时刻是新事件")

    def test_batch_ingest_same_contract(self):
        rows = [dict(self.record) for _ in range(3)]
        results = self.led.ingest_many(rows)
        inserted_flags = [r.inserted for r in results]
        self.assertEqual(inserted_flags, [True, False, False])
        ids = {r.memory_id for r in results}
        self.assertEqual(len(ids), 1)


if __name__ == "__main__":
    unittest.main()
