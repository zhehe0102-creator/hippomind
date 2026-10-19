"""restore --sha256 校验和守卫（迭代 35）回归测试。"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RestoreChecksumGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-restore-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.SQLiteLedger = SQLiteLedger
        led = SQLiteLedger(self.tmp / "live.sqlite")
        led.ingest({"content": "恢复测试记忆", "kind": "fact",
                    "source_agent": "t", "evidence": ["t:r"]})
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "backup",
             "--ledger", str(self.tmp / "live.sqlite"), str(self.tmp / "good.sqlite")],
            capture_output=True, text=True, cwd=str(ROOT))
        self.good_sha = json.loads(r.stdout)["sha256"]

    def _restore(self, backup: Path, sha: str):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "restore",
             "--ledger", str(self.tmp / "target.sqlite"),
             "--sha256", sha, str(backup)],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_tampered_backup_rejected_on_mismatch(self):
        data = bytearray((self.tmp / "good.sqlite").read_bytes())
        data[100:110] = b"XXXXXXXXXX"
        bad = self.tmp / "bad.sqlite"
        bad.write_bytes(bytes(data))
        r = self._restore(bad, self.good_sha)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不匹配", r.stdout + r.stderr)

    def test_matching_checksum_restores_and_is_searchable(self):
        target = self.tmp / "target.sqlite"
        r = self._restore(self.tmp / "good.sqlite", self.good_sha)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["sha256"], self.good_sha)
        restored = self.SQLiteLedger(target)
        hits = restored.search("恢复测试", limit=5)
        self.assertTrue(any("恢复测试记忆" in h["content"] for h in hits))


if __name__ == "__main__":
    unittest.main()
