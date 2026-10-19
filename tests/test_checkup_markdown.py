"""checkup --format md markdown 报告（迭代 78）。"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CheckupMarkdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-md-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        led = SQLiteLedger(self.tmp / "c.sqlite")
        for i in range(3):
            led.ingest({"content": f"体检 {i}", "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{i}"]})

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "checkup",
             "--ledger", str(self.tmp / "c.sqlite"), *args],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_md_report_structure(self):
        r = self._run("--format", "md")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertTrue(out.startswith("# HippoMind Checkup：**ok**"))
        for section in ("## SQLite 完整性", "## FTS 同步", "## 游标健康",
                        "## Claim 一致性", "## 概览"):
            self.assertIn(section, out)
        self.assertEqual(out.count("```json"), 5)

    def test_json_default_unchanged(self):
        import json
        r = self._run()
        payload = json.loads(r.stdout)
        self.assertEqual(payload["checkup"], "ok")
        self.assertIn("checks", payload)


if __name__ == "__main__":
    unittest.main()
