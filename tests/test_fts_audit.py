"""FTS 体积审计进 health_overview（迭代 71）。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger


class FtsAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-ftsaud-test-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger_path = Path(self.tmp.name) / "a.sqlite"
        self.led = SQLiteLedger(self.ledger_path)
        for i in range(5):
            self.led.ingest({"content": f"审计测试 {i}", "kind": "fact",
                             "source_agent": "t", "evidence": [f"t:{i}"]})

    def test_healthy_library_reports_ok(self):
        o = self.led.health_overview()
        self.assertEqual(o["fts"]["rows"], 5)
        self.assertFalse(o["fts"]["drift"])
        self.assertEqual(o["fts"]["note"], "ok")
        self.assertGreater(o["fts"]["db_bytes"], 0)

    def test_raw_archived_rows_excluded_from_denominator(self):
        """raw 归档按设计不入索引——不得触发漂移误报（生产库 689k 行实测教训）。"""
        raw = sqlite3.connect(self.ledger_path)
        raw.execute(
            "INSERT INTO memories(memory_id,origin_event_id,kind,content,"
            "source_agent,scope,created_at,updated_at,last_weighted_at,"
            "quality_state,lifecycle,raw_chat) VALUES("
            "'mem_raw1','evt_raw1','message','raw chat body','watcher',"
            "'shared','2026-08-22T00:00:00+00:00','2026-08-22T00:00:00+00:00',"
            "'2026-08-22T00:00:00+00:00','quarantine','archived',1)")
        raw.commit()
        raw.close()
        o = SQLiteLedger(self.ledger_path).health_overview()
        self.assertFalse(o["fts"]["drift"], o["fts"])
        self.assertEqual(o["fts"]["expected"], 5)
        self.assertEqual(o["memories"], 6)

    def test_drift_detected_and_repair_hinted(self):
        raw = sqlite3.connect(self.ledger_path)
        raw.execute("DELETE FROM memory_fts "
                    "WHERE rowid IN (SELECT rowid FROM memory_fts LIMIT 1)")
        raw.commit()
        raw.close()
        o = SQLiteLedger(self.ledger_path).health_overview()
        self.assertTrue(o["fts"]["drift"])
        self.assertIn("repair", o["fts"]["note"])


if __name__ == "__main__":
    unittest.main()
