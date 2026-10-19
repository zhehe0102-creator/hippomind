"""前景轮九审查修复回归：413 排空 + 监听队列容量。"""
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


class ReviewFixRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-review-fix-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        self.service = MemoryHubService(self.paths, watch=False)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def test_oversized_body_client_receives_413_not_reset(self):
        """超长请求体：排空后回 413，客户端必须能读到错误响应而非连接重置。"""
        body = json.dumps({"records": [{"content": "x" * 1_000_000,
                                        "kind": "fact", "source_agent": "t",
                                        "evidence": ["e"]}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/ingest", data=body,
            headers={"Content-Type": "application/json",
                     "X-HippoMind-Token": self.service.token},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=15)
            self.fail("超长请求不应成功")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 413)
            self.assertIn(b"budget", e.read())
        except Exception as e:  # 连接重置等——修复前会走到这里
            self.fail(f"客户端应收到 413 而非 {type(e).__name__}: {e}")
        # 服务在 413 后仍健康
        with urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{self.port}/health",
                headers={"X-HippoMind-Token": self.service.token}),
                timeout=10) as r:
            self.assertEqual(r.status, 200)

    def test_listen_backlog_sized_for_concurrency(self):
        """backlog 契约：默认值 5 在多 agent keep-alive 下会溢出 reset。"""
        self.assertGreaterEqual(MemoryHubHTTPServer.request_queue_size, 128)


if __name__ == "__main__":
    unittest.main()
