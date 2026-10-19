"""daemon --read-only 只读模式（前景轮六，LAN 镜像使能器）。"""
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


class ReadOnlyModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-ro-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        self.service = MemoryHubService(self.paths, watch=False, read_only=True)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        # 预置数据（绕过只读服务直写账本）
        self.service.ledger.ingest({"content": "只读模式探针", "kind": "fact",
                                    "source_agent": "t", "evidence": ["t:1"]})

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "X-HippoMind-Token": self.service.token},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def _get(self, path):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{self.port}{path}",
                    headers={"X-HippoMind-Token": self.service.token}),
                    timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_reads_allowed_writes_rejected(self):
        self.assertEqual(self._get("/health"), 200)
        self.assertEqual(self._get("/stats"), 200)
        self.assertIn(self._post("/ingest", {"records": [
            {"content": "x", "kind": "fact", "source_agent": "t",
             "evidence": ["x"]}]}), (401, 403))
        self.assertIn(self._post("/sync", {}), (401, 403))
        self.assertIn(self._post("/admin/reload", {}), (401, 403))

    def test_no_data_written_in_read_only(self):
        before = self.service.ledger.stats()["memories"]
        self._post("/ingest", {"records": [
            {"content": "y", "kind": "fact", "source_agent": "t",
             "evidence": ["y"]}]})
        after = self.service.ledger.stats()["memories"]
        self.assertEqual(before, after)

    def test_admin_token_downgraded_policy(self):
        policy = self.service.access_policy(self.service.token)
        self.assertIsNotNone(policy)
        self.assertFalse(policy.admin, "只读模式下管理员必须被降级")


if __name__ == "__main__":
    unittest.main()
