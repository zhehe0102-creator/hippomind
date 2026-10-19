"""search 缓存失效精度：写操作即时失效（迭代 82 评估）。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger


class CacheInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-cacheinv-test-")
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "c.sqlite")
        res = self.led.ingest({"content": "缓存失效探测目标", "kind": "fact",
                               "source_agent": "t", "evidence": ["t:1"]})
        self.mid = res.memory_id

    def _prime_cache(self):
        self.led.search("缓存失效探测", scope="shared", limit=5)

    def test_pin_invalidates_immediately(self):
        self._prime_cache()
        g = self.led.generation
        self.led.pin_memory(self.mid, pinned=True)
        self.assertNotEqual(self.led.generation, g, "pin 必须推进 generation")

    def test_feedback_invalidates_immediately(self):
        self._prime_cache()
        g = self.led.generation
        self.led.feedback(memory_id=self.mid, signal=1.0, source_agent="t")
        self.assertNotEqual(self.led.generation, g, "feedback 必须推进 generation")
        hits = self.led.search("缓存失效探测", scope="shared", limit=5)
        self.assertEqual(hits[0]["memory_id"], self.mid,
                         "feedback 后立即查询不得命中旧缓存")

    def test_ingest_invalidates_and_new_memory_visible(self):
        self._prime_cache()
        self.led.ingest({"content": "缓存失效探测新记忆", "kind": "fact",
                         "source_agent": "t", "evidence": ["t:2"]})
        hits = self.led.search("缓存失效探测", scope="shared", limit=5)
        contents = {h["content"] for h in hits}
        self.assertIn("缓存失效探测新记忆", contents, "新写入必须立即可见")


if __name__ == "__main__":
    unittest.main()
