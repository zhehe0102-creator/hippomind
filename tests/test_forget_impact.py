"""forget 影响面摘要（迭代 81）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger


class ForgetImpactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-impact-test-")
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "i.sqlite")
        for i in range(4):
            self.led.ingest({"content": f"影响面 {i}", "kind": "fact",
                             "source_agent": "t", "evidence": [f"t:{i}"],
                             "scope": "shared"})
        self.led.ingest({"content": "另一 scope", "kind": "fact",
                         "source_agent": "t", "evidence": ["t:x"],
                         "scope": "private"})

    def test_impact_summary_shape(self):
        result = self.led.forget(scope="shared")
        impact = result["impact"]
        self.assertEqual(impact["scopes"], {"shared": 4})
        self.assertTrue(impact["time_from"])
        self.assertLessEqual(impact["time_from"], impact["time_to"])

    def test_scope_isolation_in_impact(self):
        self.led.forget(scope="shared")
        remaining = self.led.search("另一 scope", scope="private", limit=5)
        self.assertEqual(len(remaining), 1)

    def test_cli_forwards_impact(self):
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "forget",
             "--ledger", str(Path(self.tmp.name) / "i.sqlite"),
             "--scope", "private"],
            capture_output=True, text=True, cwd=str(ROOT))
        out = json.loads(r.stdout)
        self.assertEqual(out["impact"]["scopes"], {"private": 1})


if __name__ == "__main__":
    unittest.main()
