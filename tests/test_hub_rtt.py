"""hub_client rtt 测量进 HUD（迭代 74）。"""
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths
from hippomind.hub_client import HubStatusClient


class HubRttTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-rtt-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        self.service = MemoryHubService(paths, watch=False)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.client = HubStatusClient(port=self.server.server_address[1])

    def test_stats_reports_rtt_ms(self):
        result = self.client.stats()
        self.assertIsNotNone(result)
        self.assertIn("rtt_ms", result)
        self.assertIsInstance(result["rtt_ms"], (int, float))
        self.assertGreaterEqual(result["rtt_ms"], 0.0)

    def test_offline_hub_returns_none(self):
        dead = HubStatusClient(port=1)  # 不可路由端口
        self.assertIsNone(dead.stats())


if __name__ == "__main__":
    unittest.main()
