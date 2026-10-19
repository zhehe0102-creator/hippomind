"""迭代 28：as_of 双时态历史切片 + 慢查询哨兵契约。

对齐 zep/Graphiti point-in-time 语义：valid_from/valid_until 窗口按给定
时刻求值，可回答"当时系统认为什么为真"；lifecycle='expired'（按现在标记）
的行只要窗口包含 as_of 就必须可见。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
from hippomind.ledger import SQLiteLedger


def _fmt(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


class AsOfSliceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.now = time.time()
        led = self.ledger
        led.ingest({"kind": "fact", "scope": "shared", "source_agent": "t",
                    "content": "旧版部署端口是 8000", "evidence": ["e1"],
                    "valid_from": _fmt(self.now - 3 * 86400),
                    "valid_until": _fmt(self.now - 86400)})
        led.ingest({"kind": "fact", "scope": "shared", "source_agent": "t",
                    "content": "新版部署端口是 9000", "evidence": ["e2"],
                    "valid_from": _fmt(self.now + 86400)})
        led.ingest({"kind": "fact", "scope": "shared", "source_agent": "t",
                    "content": "部署使用 systemd 托管", "evidence": ["e3"]})

    def tearDown(self):
        self.tmp.cleanup()

    def _contents(self, rows):
        return sorted(row["content"] for row in rows)

    def test_default_query_hides_expired_and_future(self):
        contents = self._contents(self.ledger.search("部署端口", limit=10))
        self.assertTrue(any("systemd" in c for c in contents))
        self.assertFalse(any("8000" in c for c in contents), "过期事实不得出现在默认查询")
        self.assertFalse(any("9000" in c for c in contents), "未生效事实不得出现在默认查询")

    def test_as_of_past_recovers_expired_fact(self):
        as_of = _fmt(self.now - 3 * 86400 + 3600)
        contents = self._contents(self.ledger.search("部署端口", limit=10, as_of=as_of))
        self.assertTrue(any("8000" in c for c in contents),
                        "as_of 过去必须能查到当时有效、现已过期的事实")
        self.assertFalse(any("9000" in c for c in contents),
                         "as_of 过去不得提前泄露未生效事实")

    def test_as_of_future_previews_scheduled_fact(self):
        as_of = _fmt(self.now + 3 * 86400)
        contents = self._contents(self.ledger.search("部署端口", limit=10, as_of=as_of))
        self.assertTrue(any("9000" in c for c in contents),
                        "as_of 未来必须能查到定时事实")
        self.assertFalse(any("8000" in c for c in contents))

    def test_as_of_cache_key_separation(self):
        """同一查询不同 as_of 必须各自命中独立缓存项。"""
        past = _fmt(self.now - 3 * 86400 + 3600)
        first = self._contents(self.ledger.search("部署端口", limit=10, as_of=past))
        second = self._contents(self.ledger.search("部署端口", limit=10, as_of=past))
        self.assertEqual(first, second)
        now_view = self._contents(self.ledger.search("部署端口", limit=10))
        self.assertNotEqual(first, now_view)

    def test_context_pack_as_of_passthrough(self):
        builder = ContextPackBuilder(self.ledger)
        past = _fmt(self.now - 3 * 86400 + 3600)
        pack_past = builder.build("部署端口", ContextPackOptions(max_items=8, as_of=past))
        self.assertIn("8000", str(pack_past.get("text", "")))
        pack_now = builder.build("部署端口", ContextPackOptions(max_items=8))
        self.assertNotIn("8000", str(pack_now.get("text", "")))


class SlowSearchSentinelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_stats_expose_latency_fields(self):
        self.ledger.search("部署", limit=5)
        stats = self.ledger.stats()
        self.assertIn("search_p95_ms", stats)
        self.assertIn("slow_search_count", stats)
        self.assertIn("slow_search_threshold_s", stats)
        self.assertEqual(stats["slow_search_count"], 0)

    def test_slow_search_counter_increments(self):
        self.ledger._record_search_latency(2.5)
        self.ledger._record_search_latency(0.01)
        self.assertEqual(self.ledger.stats()["slow_search_count"], 1)
        self.ledger._record_search_latency(3.0)
        self.assertEqual(self.ledger.stats()["slow_search_count"], 2)

    def test_p95_reflects_recorded_durations(self):
        for value in (0.1, 0.2, 0.3, 0.4, 0.5):
            self.ledger._record_search_latency(value)
        # 5 个样本 p95 = 上取整第 5 个 = 0.5s = 500ms
        self.assertGreaterEqual(self.ledger.stats()["search_p95_ms"], 500.0)


if __name__ == "__main__":
    unittest.main()
