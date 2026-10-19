"""forget --batch 按批次回滚导入（迭代 65）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ForgetBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-batchfg-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.SQLiteLedger = SQLiteLedger
        led = SQLiteLedger(self.tmp / "b.sqlite")
        led.ingest({"content": "保留原生记忆", "kind": "fact",
                    "source_agent": "t", "evidence": ["t:k"]})
        rows = [{"content": f"批量导入 {i}", "kind": "fact",
                 "source_agent": "imp", "evidence": [f"t:{i}"],
                 "metadata": {"import_batch": "imp-xyz"}} for i in range(4)]
        led.ingest_many(rows)

    def test_batch_forget_removes_only_batch_memories(self):
        led = self.SQLiteLedger(self.tmp / "b.sqlite")
        result = led.forget(import_batch="imp-xyz")
        self.assertEqual(result["forgotten"], 4)
        self.assertFalse(led.search("批量导入", limit=10))
        self.assertTrue(led.search("保留原生记忆", limit=5))

    def test_unknown_batch_reports_zero(self):
        led = self.SQLiteLedger(self.tmp / "b.sqlite")
        result = led.forget(import_batch="imp-nonexistent")
        self.assertEqual(result["forgotten"], 0)
        self.assertIn("note", result)

    def test_cli_forget_batch_flag(self):
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "forget",
             "--ledger", str(self.tmp / "b.sqlite"), "--batch", "imp-xyz"],
            capture_output=True, text=True, cwd=str(ROOT))
        out = json.loads(r.stdout)
        self.assertEqual(out["forgotten"], 4)
        led = self.SQLiteLedger(self.tmp / "b.sqlite")
        self.assertFalse(led.search("批量导入", limit=10))


if __name__ == "__main__":
    unittest.main()
