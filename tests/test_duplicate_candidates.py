"""maintain 近似重复对报告（迭代 36）回归测试。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DuplicateCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-dup-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.SQLiteLedger = SQLiteLedger
        led = SQLiteLedger(self.tmp / "d.sqlite")
        # 同文不同证据 → content_hash 不同 → 共存 → 应被建议合并
        led.ingest({"content": "生产数据库的连接池大小是 20 个", "kind": "fact",
                    "source_agent": "a1", "evidence": ["x:1"]})
        led.ingest({"content": "生产数据库的连接池大小是 20 个", "kind": "fact",
                    "source_agent": "a2", "evidence": ["x:2"]})
        # 数字不同的事实不得误报
        led.ingest({"content": "生产数据库端口是 5432", "kind": "fact",
                    "source_agent": "a1", "evidence": ["x:3"]})
        led.ingest({"content": "生产数据库端口是 6432", "kind": "fact",
                    "source_agent": "a1", "evidence": ["x:4"]})
        for i in range(15):
            led.ingest({"content": f"完全不同的会议记录编号 {i}", "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{i}"]})

    def test_maintain_reports_exact_duplicate_pair_only(self):
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "maintain",
             "--ledger", str(self.tmp / "d.sqlite")],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        dups = json.loads(r.stdout)["duplicate_candidates"]
        self.assertEqual(dups["suggestions"], 1, dups)
        pair = dups["pairs"][0]
        self.assertGreaterEqual(pair["similarity"], 0.85)
        self.assertIn("连接池", pair["preview"])

    def test_direct_api_returns_scanned_count(self):
        led = self.SQLiteLedger(self.tmp / "d.sqlite")
        report = led.duplicate_candidates()
        self.assertEqual(report["scanned"], 19)
        self.assertEqual(report["suggestions"], 1)


if __name__ == "__main__":
    unittest.main()
