"""ledger_events 审计归档（迭代 54）：孤儿事件移侧表、幂等、投影无损。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EventsArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.SQLiteLedger = SQLiteLedger
        self.led = SQLiteLedger(Path(self.tmp.name) / "e.sqlite")
        rows = [{"content": f"事件归档测试 {i}", "kind": "fact",
                 "source_agent": "t", "evidence": [f"t:{i}"]}
                for i in range(60)]
        self.led.ingest_many(rows)
        mids = [h["memory_id"] for h in
                self.led.search("事件归档测试", limit=60)]
        for round_i in range(160):  # 纯审计事件（无投影锚定）
            self.led.pin_memory(mids[round_i % len(mids)],
                                pinned=(round_i % 2 == 0))

    def test_archive_moves_orphans_only_and_is_idempotent(self):
        result = self.led.maintain(events_keep=100)
        self.assertEqual(result["events_archived"], 60)
        with self.led._reading() as conn:
            main = conn.execute(
                "SELECT COUNT(*) FROM ledger_events").fetchone()[0]
            side = conn.execute(
                "SELECT COUNT(*) FROM ledger_events_archive").fetchone()[0]
            anchored = conn.execute(
                "SELECT COUNT(*) FROM ledger_events WHERE event_id IN "
                "(SELECT origin_event_id FROM memories)").fetchone()[0]
        self.assertEqual(main, 160)          # 60 锚定 + 100 保留孤儿
        self.assertEqual(side, 60)
        self.assertEqual(anchored, 60)       # 锚定行全部留在主表
        # 幂等：再跑一次不再归档
        again = self.led.maintain(events_keep=100)
        self.assertEqual(again["events_archived"], 0)

    def test_projections_unaffected_after_archive(self):
        self.led.maintain(events_keep=100)
        hits = self.led.search("事件归档测试", limit=200)
        self.assertEqual(len(hits), 60)

    def test_archived_rows_remain_queryable_for_audit(self):
        self.led.maintain(events_keep=100)
        with self.led._reading() as conn:
            side_types = dict(
                (r[0], r[1]) for r in conn.execute(
                    "SELECT event_type, COUNT(*) FROM ledger_events_archive "
                    "GROUP BY event_type").fetchall())
        self.assertGreater(side_types.get("pin", 0) +
                           side_types.get("unpin", 0), 0,
                           "侧表必须保留可查证的审计行")


if __name__ == "__main__":
    unittest.main()
