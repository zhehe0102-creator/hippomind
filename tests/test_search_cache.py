"""search 结果 LRU 缓存（迭代 53）：generation 失效 + 突变隔离。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SearchCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.SQLiteLedger = SQLiteLedger
        self.led = SQLiteLedger(Path(self.tmp.name) / "sc.sqlite")
        for i in range(30):
            self.led.ingest({"content": f"缓存测试条目 {i} 数据库事实",
                             "kind": "fact", "source_agent": "t",
                             "evidence": [f"t:{i}"]})

    def test_repeat_query_consistent_and_fast_path(self):
        q = "缓存测试 数据库"
        r1 = self.led.search(q, limit=10)
        r2 = self.led.search(q, limit=10)
        self.assertEqual([x["memory_id"] for x in r1],
                         [x["memory_id"] for x in r2])
        cache = self.led._search_cache
        key_hit = any(True for _ in cache)
        self.assertTrue(key_hit, "重复查询后缓存应非空")

    def test_write_invalidates_cache_immediately(self):
        q = "缓存测试 数据库"
        self.led.search(q, limit=10)
        self.led.ingest({"content": "独角兽量子协议新写入", "kind": "fact",
                         "source_agent": "t", "evidence": ["t:new"]})
        hits = self.led.search("独角兽量子协议", limit=5)
        self.assertTrue(any("独角兽" in x["content"] for x in hits),
                        "写入后必须立即可见（generation 失效）")

    def test_caller_mutation_does_not_poison_cache(self):
        q = "缓存测试 数据库"
        r1 = self.led.search(q, limit=5)
        r1[0]["content"] = "MUTATED"
        r2 = self.led.search(q, limit=5)
        self.assertFalse(any(x["content"] == "MUTATED" for x in r2))


if __name__ == "__main__":
    unittest.main()
