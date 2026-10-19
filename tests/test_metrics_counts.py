"""/metrics 计数扩充：claims/conflicts/pinned（前景轮八）。"""
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths


class MetricsCountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-metrics-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        self.service = MemoryHubService(self.paths, watch=False)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def _claim(self, value):
        return {"subject": "service-a", "predicate": "status",
                "slot_key": "state", "value": {"v": value},
                "confidence": 0.8}

    def test_conflict_and_pinned_counters(self):
        led = self.service.ledger
        r = led.ingest({"content": "指标探针 service-a v1", "kind": "fact",
                        "source_agent": "t", "evidence": ["t:1"],
                        "claims": [self._claim("v1")]})
        led.pin_memory(r.memory_id)
        # 同主语同槽位不同值 → 双双进入 conflict（系统既定语义）
        led.ingest({"content": "冲突值 v2", "kind": "fact",
                    "source_agent": "t", "evidence": ["t:2"],
                    "claims": [self._claim("v2")]})
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/metrics", timeout=10) as resp:
            body = resp.read().decode()
        lines = {l.split()[0]: l.split()[1] for l in body.splitlines()
                 if l and not l.startswith("#")}
        self.assertEqual(lines["hippomind_claims_active"], "0")
        self.assertEqual(lines["hippomind_claims_conflict"], "2")
        self.assertEqual(lines["hippomind_memories_pinned"], "1")

    def test_active_claim_counter_single(self):
        led = self.service.ledger
        led.ingest({"content": "唯一值 v9", "kind": "fact",
                    "source_agent": "t", "evidence": ["t:9"],
                    "claims": [self._claim("v9")]})
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/metrics", timeout=10) as resp:
            body = resp.read().decode()
        lines = {l.split()[0]: l.split()[1] for l in body.splitlines()
                 if l and not l.startswith("#")}
        self.assertEqual(lines["hippomind_claims_active"], "1")
        self.assertEqual(lines["hippomind_claims_conflict"], "0")


if __name__ == "__main__":
    unittest.main()
