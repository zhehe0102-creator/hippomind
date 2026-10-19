"""备份登记 + maintain --prune-backups 轮转（前景轮六）。"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger


class BackupPruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-prune-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)],
                                               check=False))
        self.ledger_path = self.tmp / "d.sqlite"
        led = SQLiteLedger(self.ledger_path)
        led.ingest({"content": "轮转测试", "kind": "fact",
                    "source_agent": "t", "evidence": ["t:1"]})
        self.rt = self.tmp / "rt"
        self.paths = []
        for i in range(3):
            b = self.tmp / f"b{i}.sqlite"
            r = self._run("backup", str(b))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.paths.append(b)
            time.sleep(0.05)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", *args,
             "--ledger", str(self.ledger_path),
             "--runtime-dir", str(self.rt)],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_backup_registers_entry(self):
        reg = json.loads((self.rt / "backups.json").read_text())
        self.assertEqual(len(reg), 3)
        for entry in reg:
            self.assertIn("sha256", entry)
            self.assertIn("created_at", entry)
            self.assertTrue(Path(entry["path"]).exists())

    def test_prune_keeps_newest_and_removes_verified_old(self):
        r = self._run("maintain", "--prune-backups", "1")
        out = json.loads(r.stdout)["maintenance"]["prune_backups"]
        self.assertEqual(out["kept"], 1)
        self.assertEqual(out["removed"], 2)
        self.assertFalse(self.paths[0].exists())
        self.assertFalse(self.paths[1].exists())
        self.assertTrue(self.paths[2].exists())
        reg = json.loads((self.rt / "backups.json").read_text())
        self.assertEqual(len(reg), 1)

    def test_tampered_file_skipped_not_deleted(self):
        self.paths[0].write_bytes(self.paths[0].read_bytes() + b"tamper")
        r = self._run("maintain", "--prune-backups", "1")
        out = json.loads(r.stdout)["maintenance"]["prune_backups"]
        # b2 保留、b1 删除、b0 篡改跳过（保留在册）
        self.assertEqual(out["removed"], 1)
        self.assertEqual(out["skipped"], 1)
        self.assertTrue(self.paths[0].exists(), "sha 不吻合的文件不得删除")
        self.assertFalse(self.paths[1].exists())


if __name__ == "__main__":
    unittest.main()
