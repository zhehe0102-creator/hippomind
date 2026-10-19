"""hippomind checkup 账本深度体检（迭代 34）回归测试。"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(ledger: Path) -> dict:
    r = subprocess.run(
        [sys.executable, "-m", "hippomind.cli", "checkup", "--ledger", str(ledger)],
        capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode in (0, 1), r.stderr
    return json.loads(r.stdout)


class CheckupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-checkup-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.SQLiteLedger = SQLiteLedger
        led = SQLiteLedger(self.tmp / "c.sqlite")
        for i in range(10):
            led.ingest({"content": f"体检测试记忆 {i}", "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{i}"]})

    def test_healthy_ledger_reports_ok(self):
        out = _run(self.tmp / "c.sqlite")
        self.assertEqual(out["checkup"], "ok")
        checks = out["checks"]
        self.assertEqual(checks["integrity"]["status"], "ok")
        self.assertEqual(checks["fts_sync"]["status"], "ok")
        self.assertEqual(checks["claims"]["status"], "ok")
        self.assertGreater(checks["overview"]["memories"], 0)

    def test_fts_desync_detected_as_warn_with_hint(self):
        conn = sqlite3.connect(str(self.tmp / "c.sqlite"))
        mid = conn.execute("SELECT memory_id FROM memory_fts LIMIT 1").fetchone()[0]
        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (mid,))
        conn.commit()
        conn.close()
        out = _run(self.tmp / "c.sqlite")
        fts = out["checks"]["fts_sync"]
        self.assertEqual(fts["status"], "warn")
        self.assertEqual(fts["unindexed"], 1)
        self.assertTrue(fts.get("hint"))

    def test_dangling_active_claim_detected(self):
        conn = sqlite3.connect(str(self.tmp / "c.sqlite"))
        conn.execute(
            "INSERT INTO memory_claims(claim_id,memory_id,scope,subject,predicate,"
            "slot_key,value_json,value_hash,valid_from,asserted_at,state,created_at) "
            "VALUES('claim-ghost','mem-missing','shared','x','y','z','{}','',"
            "'','2026-01-01T00:00:00+00:00','active',"
            "'2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        out = _run(self.tmp / "c.sqlite")
        claims = out["checks"]["claims"]
        self.assertEqual(claims["status"], "warn")
        self.assertGreaterEqual(claims["dangling"], 1)


if __name__ == "__main__":
    unittest.main()
