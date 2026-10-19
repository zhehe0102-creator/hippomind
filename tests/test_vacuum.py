"""maintain --vacuum 库整理（迭代 70）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VacuumTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-vac-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.SQLiteLedger = SQLiteLedger
        self.ledger_path = self.tmp / "v.sqlite"
        led = SQLiteLedger(self.ledger_path)
        for i in range(200):
            led.ingest({"content": f"碎片数据 {i} " * 20, "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{i}"]})
        led.forget(scope="shared")

    def _maintain_vacuum(self):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "maintain",
             "--ledger", str(self.ledger_path), "--vacuum"],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_vacuum_reclaims_space_and_warns_backup(self):
        r = self._maintain_vacuum()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        v = out["maintenance"]["vacuum"]
        self.assertGreater(v["reclaimed_bytes"], 0)
        self.assertIn("建议先", r.stderr)

    def test_second_vacuum_is_noop(self):
        self._maintain_vacuum()
        r = self._maintain_vacuum()
        v = json.loads(r.stdout)["maintenance"]["vacuum"]
        self.assertEqual(v["reclaimed_bytes"], 0)

    def test_ledger_level_vacuum_contract(self):
        led = self.SQLiteLedger(self.ledger_path)
        result = led.vacuum()
        self.assertEqual(set(result), {"before_bytes", "after_bytes",
                                       "reclaimed_bytes"})
        self.assertGreaterEqual(result["after_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
