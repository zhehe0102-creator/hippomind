"""metrics.jsonl 趋势日志（迭代 68）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.daemon import MemoryHubService, RuntimePaths


class MetricsTrendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-trend-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        self.service = MemoryHubService(self.paths, watch=False)
        self.service.ledger.ingest({"content": "趋势测试", "kind": "fact",
                                    "source_agent": "t", "evidence": ["t:1"]})

    def test_appends_hourly_line(self):
        self.service._last_metrics_log -= 7200  # 强制到期
        entry = self.service.append_metrics_trend()
        self.assertIsNotNone(entry)
        self.assertEqual(entry["memories"], 1)
        log = self.paths.state_path.parent / "metrics.jsonl"
        data = json.loads(log.read_text().strip().splitlines()[0])
        self.assertEqual(data["memories"], 1)
        self.assertIn("ts", data)
        self.assertIn("uptime_s", data)

    def test_no_duplicate_within_window(self):
        self.service._last_metrics_log -= 7200
        self.service.append_metrics_trend()
        self.assertIsNone(self.service.append_metrics_trend())
        log = self.paths.state_path.parent / "metrics.jsonl"
        self.assertEqual(len(log.read_text().strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
