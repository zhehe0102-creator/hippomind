"""daemon /metrics Prometheus 端点（迭代 67）。"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths


class MetricsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-metrics-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        self.service = MemoryHubService(paths, watch=False)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/ingest",
            data=json.dumps({"records": [
                {"content": f"指标测试 {i}", "kind": "fact",
                 "source_agent": "t", "evidence": [f"t:{i}"]} for i in range(3)]}).encode(),
            headers={"Content-Type": "application/json",
                     "X-HippoMind-Token": self.service.token})
        urllib.request.urlopen(req, timeout=10).read()

    def test_prometheus_format_and_counts(self):
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/metrics", timeout=5)
        self.assertIn("text/plain; version=0.0.4", resp.headers["Content-Type"])
        body = resp.read().decode()
        for name in ("hippomind_memories_total", "hippomind_events_total",
                     "hippomind_claims_active", "hippomind_wal_bytes",
                     "hippomind_uptime_seconds"):
            self.assertIn(f"# TYPE {name}", body)
        val = [l for l in body.splitlines()
               if l.startswith("hippomind_memories_total ")][0]
        self.assertGreaterEqual(int(float(val.split()[-1])), 3)

    def test_uptime_non_negative(self):
        body = urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/metrics", timeout=5).read().decode()
        up = [l for l in body.splitlines()
              if l.startswith("hippomind_uptime_seconds ")][0]
        self.assertGreaterEqual(float(up.split()[-1]), 0.0)


if __name__ == "__main__":
    unittest.main()
