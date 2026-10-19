"""hippomind conflicts 冲突谱系查询（迭代 66）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConflictsCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-conf-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.SQLiteLedger = SQLiteLedger
        led = SQLiteLedger(self.tmp / "c.sqlite")
        for value, conf, tag in (("5432", 0.5, 1), ("6432", 0.52, 2)):
            led.ingest({"content": f"数据库端口是 {value}", "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{tag}"],
                        "claims": [{"subject": "db.prod", "predicate": "config",
                                    "slot_key": "port", "value": {"port": value},
                                    "confidence": conf}]})

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "conflicts",
             "--ledger", str(self.tmp / "c.sqlite"), *args],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_json_lists_conflicting_claims(self):
        r = self._run("--format", "json")
        out = json.loads(r.stdout)
        self.assertGreaterEqual(out["total"], 2)
        states = {i["state"] for i in out["items"]}
        self.assertIn("conflict", states)
        subjects = {i["subject"] for i in out["items"]}
        self.assertIn("db.prod", subjects)

    def test_text_mode_and_empty_scope(self):
        r = self._run()
        self.assertIn("conflict", r.stdout)
        self.assertIn("共", r.stderr)
        # 干净 scope 无冲突
        led = self.SQLiteLedger(self.tmp / "c.sqlite")
        report = led.list_conflicts(scope="empty-scope")
        self.assertEqual(report["total"], 0)


if __name__ == "__main__":
    unittest.main()
