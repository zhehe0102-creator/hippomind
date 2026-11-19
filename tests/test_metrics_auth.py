"""daemon /metrics 鉴权（WP-B：审计复现第 13 条）。

约定：
- /health 仍免鉴权（launcher 用它探测存活与账本归属）；
- /metrics 与 /stats 一样要求本地凭据：无 token / 错误 token -> 401；
- 正确 token -> 200 + Prometheus 文本，只读镜像上也能抓到真实规模
  （只读模式降级的是数据面，不是观测面的规模指标）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths
from hippomind.ledger import SQLiteLedger

METRIC_NAMES = ("hippomind_memories_total", "hippomind_events_total",
                "hippomind_claims_active", "hippomind_claims_conflict",
                "hippomind_wal_bytes", "hippomind_uptime_seconds")


class MetricsAuthMixin:
    """Shared server fixture; concrete classes pick writable vs read-only."""

    read_only = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-metrics-auth-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        # Seed through a writable handle; a read-only service then serves it.
        writer = SQLiteLedger(self.paths.ledger_path)
        try:
            writer.ingest({"content": "指标鉴权探针", "kind": "fact",
                           "source_agent": "t", "evidence": ["t:1"]})
        finally:
            writer.close()
        self.service = MemoryHubService(self.paths, watch=False,
                                        read_only=self.read_only)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.service.ledger.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _get(self, path, token=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}")
        if token:
            request.add_header("X-HippoMind-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def _metric_lines(self, body):
        lines = {}
        for line in body.decode().splitlines():
            if line and not line.startswith("#"):
                parts = line.split()
                if len(parts) == 2:
                    lines[parts[0]] = parts[1]
        return lines

    def test_metrics_without_token_is_unauthorized(self):
        status, headers, body = self._get("/metrics")
        self.assertEqual(401, status)
        self.assertIn("authorization", body.decode().lower())

    def test_metrics_with_wrong_token_is_unauthorized(self):
        self.assertEqual(401, self._get("/metrics", token="not-the-token")[0])

    def test_metrics_with_token_is_prometheus(self):
        status, headers, body = self._get("/metrics", token=self.service.token)
        self.assertEqual(200, status)
        self.assertIn("text/plain; version=0.0.4", headers["Content-Type"])
        text = body.decode()
        for name in METRIC_NAMES:
            self.assertIn(f"# TYPE {name}", text)
        values = self._metric_lines(body)
        self.assertGreaterEqual(int(float(values["hippomind_memories_total"])), 1)

    def test_health_is_still_anonymous(self):
        status, headers, body = self._get("/health")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual("ok", payload["status"])
        self.assertEqual(self.read_only, payload["read_only"])


class MetricsAuthTests(MetricsAuthMixin, unittest.TestCase):
    read_only = False


class ReadOnlyMetricsAuthTests(MetricsAuthMixin, unittest.TestCase):
    read_only = True

    def test_read_only_mirror_serves_authenticated_metrics(self):
        # 只读模式不能因为数据面降级就把 Prometheus 抓取也变成 401。
        self.assertEqual(401, self._get("/metrics")[0])
        status, _, body = self._get("/metrics", token=self.service.token)
        self.assertEqual(200, status)
        values = self._metric_lines(body)
        self.assertGreaterEqual(int(float(values["hippomind_memories_total"])), 1)

    def test_read_only_service_uses_read_only_ledger(self):
        self.assertTrue(self.service.read_only)
        self.assertTrue(self.service.ledger.read_only)


if __name__ == "__main__":
    unittest.main()
