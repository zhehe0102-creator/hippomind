"""export --encrypt 口令加密（迭代 62）：往返/拒绝/明文兼容。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ExportEncryptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-enc-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        src = SQLiteLedger(self.tmp / "s.sqlite")
        for i in range(5):
            src.ingest({"content": f"加密导出测试 {i}", "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{i}"]})

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", *args],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_roundtrip_with_correct_passphrase(self):
        r = self._run("export", "--ledger", str(self.tmp / "s.sqlite"),
                      "--encrypt", "--passphrase", "correct-horse",
                      str(self.tmp / "enc.dump"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["encrypted"])
        blob = (self.tmp / "enc.dump").read_bytes()
        self.assertFalse(blob.lstrip().startswith(b"{"), "密文不应是明文")

        r = self._run("import", "--ledger", str(self.tmp / "d.sqlite"),
                      "--passphrase", "correct-horse", str(self.tmp / "enc.dump"))
        out = json.loads(r.stdout)
        self.assertEqual(out["imported"], 5)
        self.assertTrue(out["decrypted"])
        from hippomind.ledger import SQLiteLedger
        hits = SQLiteLedger(self.tmp / "d.sqlite").search("加密导出测试", limit=10)
        self.assertEqual(len(hits), 5)

    def test_wrong_passphrase_rejected(self):
        self._run("export", "--ledger", str(self.tmp / "s.sqlite"),
                  "--encrypt", "--passphrase", "correct-horse",
                  str(self.tmp / "enc.dump"))
        r = self._run("import", "--ledger", str(self.tmp / "dx.sqlite"),
                      "--passphrase", "wrong", str(self.tmp / "enc.dump"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("口令错误", r.stderr)

    def test_plaintext_path_unaffected(self):
        r = self._run("export", "--ledger", str(self.tmp / "s.sqlite"),
                      str(self.tmp / "plain.jsonl"))
        out = json.loads(r.stdout)
        self.assertIsNone(out.get("encrypted"))


if __name__ == "__main__":
    unittest.main()
