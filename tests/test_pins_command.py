"""hippomind pins 列表命令（迭代 52）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PinsCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-pins-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        led = SQLiteLedger(self.tmp / "p.sqlite")
        self.m1 = led.ingest({"content": "置顶测试一", "kind": "fact",
                              "source_agent": "t", "evidence": ["t:1"]}).memory_id
        self.m2 = led.ingest({"content": "置顶测试二", "kind": "goal",
                              "source_agent": "t", "evidence": ["t:2"]}).memory_id
        led.pin_memory(self.m1, True)
        led.pin_memory(self.m2, True)

    def test_json_format_lists_all_pinned(self):
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "pins", "--format", "json",
             "--ledger", str(self.tmp / "p.sqlite")],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["total"], 2)
        self.assertEqual({x["memory_id"] for x in out["pinned"]},
                         {self.m1, self.m2})

    def test_text_format_shows_content_and_count(self):
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "pins",
             "--ledger", str(self.tmp / "p.sqlite")],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertIn("置顶测试一", r.stdout)
        self.assertIn("共 2 条", r.stderr)


if __name__ == "__main__":
    unittest.main()
