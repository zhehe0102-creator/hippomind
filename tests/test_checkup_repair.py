"""checkup --repair FTS 一键重建（迭代 51）。"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CheckupRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-repair-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        led = SQLiteLedger(self.tmp / "c.sqlite")
        for i in range(10):
            led.ingest({"content": f"修复测试记忆 {i}", "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{i}"]})
        conn = sqlite3.connect(str(self.tmp / "c.sqlite"))
        mid = conn.execute("SELECT memory_id FROM memory_fts LIMIT 1").fetchone()[0]
        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (mid,))
        conn.commit()
        conn.close()

    def test_repair_rebuilds_and_restores_recall(self):
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "checkup", "--repair",
             "--ledger", str(self.tmp / "c.sqlite")],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["repair"]["triggered"])
        self.assertEqual(out["checks"]["fts_sync"]["status"], "ok")

        from hippomind.ledger import SQLiteLedger
        hits = SQLiteLedger(self.tmp / "c.sqlite").search("修复测试", limit=20)
        self.assertEqual(len(hits), 10)

    def test_noop_when_already_synced(self):
        # 第一次 --repair 重建；紧接着第二次应报告无需重建
        r1 = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "checkup", "--repair",
             "--ledger", str(self.tmp / "c.sqlite")],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertTrue(json.loads(r1.stdout)["repair"]["triggered"])
        r2 = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "checkup", "--repair",
             "--ledger", str(self.tmp / "c.sqlite")],
            capture_output=True, text=True, cwd=str(ROOT))
        out2 = json.loads(r2.stdout)
        self.assertFalse(out2["repair"]["triggered"])
        self.assertIn("无需重建", out2["repair"]["reason"])
        self.assertEqual(out2["checks"]["fts_sync"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
