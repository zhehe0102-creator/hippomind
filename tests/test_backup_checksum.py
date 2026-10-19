"""hippomind backup 命令校验和（迭代 32）。"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BackupChecksumTests(unittest.TestCase):
    def test_backup_outputs_matching_sha256_and_restores(self):
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger

        tmp = Path(tempfile.mkdtemp(prefix="hippo-backup-test-"))
        self.addCleanup(lambda: subprocess.run(
            ["rm", "-rf", str(tmp)], check=False))
        ledger = SQLiteLedger(tmp / "b.sqlite")
        ledger.ingest({"content": "备份校验测试记忆", "kind": "fact",
                       "source_agent": "t", "evidence": ["test:backup"]})

        target = tmp / "b.backup.sqlite"
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "backup",
             "--ledger", str(tmp / "b.sqlite"), str(target)],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["sha256"],
                         hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertGreater(out["bytes"], 0)

        restored = SQLiteLedger(target)
        hits = restored.search("备份校验", limit=5)
        self.assertTrue(any("备份校验测试记忆" in h["content"] for h in hits))


if __name__ == "__main__":
    unittest.main()
