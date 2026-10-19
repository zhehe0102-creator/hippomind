"""深分页契约：offset 超过候选池不再返回空页（迭代 79 修复）。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger


class DeepPaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hippo-deep-test-")
        cls.led = SQLiteLedger(Path(cls.tmp.name) / "p.sqlite")
        rows = [{"content": f"分页压测记录 {i}", "kind": "fact",
                 "source_agent": "t", "evidence": [f"t:{i}"]}
                for i in range(2_000)]
        for i in range(0, 2_000, 1000):
            cls.led.ingest_many(rows[i:i + 1000])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_deep_offset_returns_rows_not_empty(self):
        """修复前 offset>=1600 因 FTS 池上限 1500 返回空页。"""
        hits = self.led.search("分页压测记录", scope="shared", limit=80,
                               offset=1600)
        self.assertEqual(len(hits), 80)

    def test_full_walk_no_loss_no_dupe(self):
        seen = []
        offset = 0
        while True:
            hits = self.led.search("分页压测记录", scope="shared", limit=80,
                                   offset=offset)
            if not hits:
                break
            seen.extend(h["memory_id"] for h in hits)
            offset += 80
        self.assertEqual(len(seen), 2_000)
        self.assertEqual(len(set(seen)), 2_000, "翻页不得重复")

    def test_first_page_cost_unchanged(self):
        import time
        t = time.perf_counter()
        self.led.search("分页压测记录", scope="shared", limit=80, offset=0)
        ms = (time.perf_counter() - t) * 1000
        self.assertLess(ms, 200, f"首页成本异常: {ms:.1f}ms")


if __name__ == "__main__":
    unittest.main()
