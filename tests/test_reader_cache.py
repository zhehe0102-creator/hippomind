"""线程本地读连接缓存（迭代 27 检索延迟剖析产物）的回归测试。"""
import sqlite3
import threading
import tempfile
import unittest
from pathlib import Path

from hippomind.ledger import SQLiteLedger


def _rec(text: str, marker: str) -> dict:
    return {"kind": "fact", "scope": "shared", "source_agent": "t",
            "content": text, "evidence": [f"t:{marker}"]}


class ReaderCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "rc.sqlite")

    def test_same_thread_reuses_connection(self):
        with self.led._reading() as c1:
            pass
        with self.led._reading() as c2:
            pass
        self.assertIs(c1, c2, "同线程两次读应复用同一连接")

    def test_cached_reader_sees_cross_thread_writes(self):
        """缓存连接在 autocommit 下每语句取新 WAL 快照——不得读到旧数据。"""
        self.led.ingest(_rec("生产数据库端口是 5432", "a"))

        seen = {}

        def other_writer():
            led2 = SQLiteLedger(self.led.path)  # 独立实例模拟另一进程/线程写者
            led2.ingest(_rec("缓存主机是 cache-09", "b"))

        t = threading.Thread(target=other_writer)
        t.start()
        t.join()

        hits = self.led.search("cache-09", limit=5)
        seen["n"] = len(hits)
        self.assertGreaterEqual(seen["n"], 1, "跨写者提交后缓存读者必须立即可见")

    def test_close_is_idempotent_and_reconnects(self):
        with self.led._reading():
            pass
        self.led.close()
        self.led.close()  # 幂等
        with self.led._reading() as c:
            # 关闭后再次读取应自动重建连接
            rows = c.execute("SELECT COUNT(*) FROM memories").fetchone()
            self.assertIsNotNone(rows)

    def test_close_then_reuse_gives_fresh_connection_object(self):
        with self.led._reading() as c1:
            pass
        self.led.close()
        with self.led._reading() as c2:
            pass
        self.assertIsNot(c1, c2)



class TrigramFtsTests(unittest.TestCase):
    """迭代 29：FTS trigram 切换（v6 迁移）+ 中文滑窗词项。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tg.sqlite"

    def test_fts_terms_emit_windows_for_long_cjk(self):
        from hippomind.ledger import SQLiteLedger
        terms = SQLiteLedger._fts_terms("生产数据库端口是多少")
        self.assertIn("生产数据库端口是多少", terms)   # 整段严格通道保留
        self.assertTrue(any(3 <= len(t) <= 6 for t in terms), terms)
        self.assertLessEqual(len(terms), 12)

    def test_v5_ledger_migrates_to_v6_and_fts_still_matches(self):
        led = SQLiteLedger(self.path)
        led.ingest(_rec("kafka 集群共 24 个分区，副本因子 3", "m1"))
        # 手动降级版本号，模拟旧库升级路径
        import sqlite3 as s3
        conn = s3.connect(str(self.path))
        conn.execute("UPDATE schema_meta SET value='5' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        led2 = SQLiteLedger(self.path)
        from hippomind.ledger import SCHEMA_VERSION
        self.assertEqual(led2.stats()["schema_version"], SCHEMA_VERSION)
        hits = led2.search("kafka 分区", limit=5)
        self.assertTrue(any("kafka" in h["content"] for h in hits),
                        "迁移后中文+英文混合查询必须仍可召回")

    def test_chinese_substring_query_hits_via_fts_or_rescue(self):
        led = SQLiteLedger(self.path)
        for i in range(200):
            led.ingest(_rec(f"常规干扰记忆 {i} 号：会议、周报、代码评审", f"d{i}"))
        led.ingest(_rec("全量备份每周日执行，增量备份每日执行", "target"))
        hits = led.search("增量备份每日执行吗", limit=8)
        self.assertTrue(any("增量备份" in h["content"] for h in hits))


class ContextCacheStatsTests(unittest.TestCase):
    """迭代 30：context-pack 缓存命中率观测。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_counters_track_hit_miss_expired(self):
        import time as _time
        from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
        led = SQLiteLedger(Path(self.tmp.name) / "cc.sqlite")
        led.ingest(_rec("生产数据库端口是 5432", "cc1"))
        b = ContextPackBuilder(led)
        opts = ContextPackOptions()
        q = "生产数据库端口"
        self.assertEqual(b.cache_stats()["context_cache_hits"], 0)
        b.build(q, options=opts)   # miss
        b.build(q, options=opts)   # hit
        st = b.cache_stats()
        self.assertEqual(st["context_cache_misses"], 1)
        self.assertEqual(st["context_cache_hits"], 1)
        self.assertGreater(st["context_cache_hit_rate"], 0)
        _time.sleep(2.1)
        b.build(q, options=opts)   # expired -> rebuild
        st = b.cache_stats()
        self.assertEqual(st["context_cache_expired"], 1)



if __name__ == "__main__":
    unittest.main()
