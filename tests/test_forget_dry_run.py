"""forget --dry-run 预览契约（前景轮）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger


class ForgetDryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-dry-test-")
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "d.sqlite")
        for i in range(7):
            self.led.ingest({"content": f"干跑测试 {i}", "kind": "fact",
                             "source_agent": "t", "evidence": [f"t:{i}"]})

    def test_dry_run_deletes_nothing_and_bumps_nothing(self):
        g0 = self.led.generation
        r = self.led.forget(scope="shared", dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["would_forget"], 7)
        self.assertEqual(self.led.generation, g0, "预览不得推进 generation")
        self.assertEqual(len(self.led.search("干跑测试", scope="shared",
                                             limit=10)), 7)

    def test_preview_shape_and_scopes(self):
        r = self.led.forget(scope="shared", dry_run=True)
        self.assertEqual(r["scopes"], {"shared": 7})
        self.assertTrue(r["time_from"])
        self.assertLessEqual(r["time_from"], r["time_to"])
        self.assertEqual(len(r["preview"]), 7)
        self.assertIn("memory_id", r["preview"][0])

    def test_real_forge_after_preview(self):
        self.led.forget(scope="shared", dry_run=True)
        r = self.led.forget(scope="shared")
        self.assertEqual(r["forgotten"], 7)
        self.assertFalse(r.get("dry_run"))
        self.assertFalse(self.led.search("干跑测试", scope="shared", limit=10))

    def test_cli_flag_passthrough(self):
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "forget",
             "--ledger", str(Path(self.tmp.name) / "d.sqlite"),
             "--scope", "shared", "--dry-run"],
            capture_output=True, text=True, cwd=str(ROOT))
        out = json.loads(r.stdout)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["would_forget"], 7)


if __name__ == "__main__":
    unittest.main()
