"""checkup --format junit CI 集成（迭代 85）。"""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger


class CheckupJunitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-junit-test-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger_path = Path(self.tmp.name) / "c.sqlite"
        led = SQLiteLedger(self.ledger_path)
        for i in range(3):
            led.ingest({"content": f"CI 体检 {i}", "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{i}"]})

    def _run(self):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "checkup",
             "--ledger", str(self.ledger_path), "--format", "junit"],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_healthy_library_all_pass(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        root = ET.fromstring(r.stdout)
        self.assertEqual(root.tag, "testsuites")
        self.assertEqual(root.get("verdict"), "ok")
        self.assertEqual(root.get("failures"), "0")
        cases = root.findall(".//testcase")
        self.assertEqual(len(cases), 5)
        statuses = {c.get("status") for c in cases}
        self.assertEqual(statuses, {"ok", "empty"})

    def test_broken_fts_reports_failures_and_exit_1(self):
        raw = sqlite3.connect(self.ledger_path)
        raw.execute("DROP TABLE memory_fts")
        raw.commit()
        raw.close()
        r = self._run()
        self.assertEqual(r.returncode, 1)
        root = ET.fromstring(r.stdout)
        self.assertEqual(root.get("verdict"), "fail")
        self.assertGreaterEqual(int(root.get("failures")), 1)
        fails = [c for c in root.findall(".//testcase")
                 if c.get("status") == "fail"]
        self.assertTrue(fails)
        self.assertIsNotNone(fails[0].find("failure"))


if __name__ == "__main__":
    unittest.main()
