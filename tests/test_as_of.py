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


class ContextPackTaskExpansionAsOfTests(unittest.TestCase):
    """WP-C：任务状态补充检索也必须透传 as_of。

    历史包回答的是「as_of 当时系统认为什么为真」。任务状态展开是主检索之外
    的第二条查询（ledger.search("", task_id=...)）；漏传 as_of 时它按「现在」
    取数，会把 as_of 之后才登记的进度/决定塞回历史包——主检索是对的，双时态
    却在展开通路上悄悄失效。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-asof-expand-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        now = time.time()
        self.as_of = _fmt(now - 2 * 86400)
        self.ledger.ingest({
            "kind": "task", "scope": "shared", "source_agent": "tester",
            "content": "任务 t-100 登录页重构 阶段一已完成",
            "evidence": ["e:asof-past"], "task_id": "t-100", "project": "atlas",
            "valid_from": _fmt(now - 3 * 86400),
            "timestamp": _fmt(now - 3 * 86400),
            "event_id": "asof-expand-past",
            "metadata": {"context": {"progress": ["阶段一：登录页重构已完成"]}},
        })
        # as_of 之后才登记的进度：历史切片里必须不存在，当前包里才应可见。
        self.ledger.ingest({
            "kind": "progress", "scope": "shared", "source_agent": "tester",
            "content": "任务 t-100 进度 阶段二密码重置改造已完成",
            "evidence": ["e:asof-new"], "task_id": "t-100", "project": "atlas",
            "valid_from": _fmt(now - 86400),
            "timestamp": _fmt(now - 86400),
            "event_id": "asof-expand-new",
            "metadata": {"context": {"progress": ["阶段二：密码重置改造已完成"]}},
        })

    def test_historical_pack_excludes_progress_added_after_as_of(self):
        builder = ContextPackBuilder(self.ledger)
        current = builder.build("登录页重构", ContextPackOptions(max_items=4))
        self.assertIn("密码重置", str(current.get("text", "")),
                      "夹具前提不成立：当前包应当渲染阶段二进度")

        historical = builder.build(
            "登录页重构", ContextPackOptions(max_items=4, as_of=self.as_of))
        text = str(historical.get("text", ""))
        self.assertIn("阶段一", text, "历史包应当包含 as_of 当时的任务状态")
        self.assertNotIn("密码重置", text,
                         "历史包包含了 as_of 之后才登记的进度（任务展开漏传 as_of）")

        # 历史切片必须排除 as_of 之后才生效的记忆本身（主检索路径）。
        hits = self.ledger.search("登录页重构", scope="shared", limit=10,
                                  as_of=self.as_of)
        self.assertNotIn("密码重置", " ".join(hit["content"] for hit in hits))


class ContextPackCausalAncestorAsOfTests(unittest.TestCase):
    """WP-H：因果祖先展开也必须遵守 as_of（历史包不得泄漏 as_of 之后才生效的祖先）。

    context_pack 的任务卡展开会调用 causal_ancestors 补因果证据；WP-H 之前
    该接口没有 as_of，只能按「现在」过滤祖先窗口，于是历史包会带上 as_of
    之后才出现的祖先（未来泄漏），同时看不见当时有效、现在 superseded 的
    祖先（历史欠收）。这里端到端锁定 context_pack 的透传。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-asof-causal-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        now = time.time()
        self.as_of = _fmt(now - 5 * 86400)
        past_from = _fmt(now - 10 * 86400)
        recent_from = _fmt(now - 3600)
        old = self.ledger.ingest({
            "kind": "blocker", "scope": "shared", "source_agent": "tester",
            "content": "祖先阻塞甲 证书审批未完成", "evidence": ["e:anc-old"],
            "valid_from": past_from, "timestamp": past_from,
            "event_id": "asof-causal-old",
        })
        new = self.ledger.ingest({
            "kind": "blocker", "scope": "shared", "source_agent": "tester",
            "content": "祖先阻塞乙 新出现的监控告警", "evidence": ["e:anc-new"],
            "valid_from": recent_from, "timestamp": recent_from,
            "event_id": "asof-causal-new",
        })
        self.ledger.ingest({
            "kind": "task", "scope": "shared", "source_agent": "tester",
            "content": "任务 t-200 部署交接 阶段一", "evidence": ["e:seed"],
            "task_id": "t-200", "valid_from": past_from, "timestamp": past_from,
            "event_id": "asof-causal-seed",
            "metadata": {"context": {"progress": ["阶段一：交接准备完成"]}},
            "causal_parents": [
                {"memory_id": old.memory_id, "relation_type": "caused_by"},
                {"memory_id": new.memory_id, "relation_type": "caused_by"},
            ],
        })

    def test_historical_pack_excludes_ancestor_that_appears_later(self):
        builder = ContextPackBuilder(self.ledger)
        current = builder.build("部署交接", ContextPackOptions(max_items=4))
        cur_text = str(current.get("text", ""))
        self.assertIn("证书审批", cur_text,
                      "夹具前提不成立：当前包应渲染历史祖先")
        self.assertIn("监控告警", cur_text,
                      "夹具前提不成立：当前包应渲染新近祖先")

        historical = builder.build(
            "部署交接", ContextPackOptions(max_items=4, as_of=self.as_of))
        hist_text = str(historical.get("text", ""))
        self.assertIn("证书审批", hist_text,
                      "as_of 当时已生效的祖先必须出现在历史包")
        self.assertNotIn("监控告警", hist_text,
                         "历史包泄漏了 as_of 之后才生效的因果祖先")


class FollowupAnchorAsOfTests(unittest.TestCase):
    """WP-M：as_of 历史切片不得用当前会话做 follow-up 锚定。

    锚点来自「现在」的会话消息；把它注入点时间查询会把 as_of 之后才发生的
    上下文带进历史包。实现里 as_of 非空时跳过锚定（debug 不出现
    query_context），这里端到端锁定该边界。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-asof-anchor-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.ledger.ingest({
            "kind": "task", "scope": "shared", "source_agent": "tester",
            "content": "GUI recall 视图优化任务", "evidence": ["e:asof-anchor"],
            "task_id": "gui-recall",
            "metadata": {"context": {"goal": ["优化 GUI 的 recall 视图"]}},
        })
        self.ledger.ingest({
            "kind": "raw_chat", "raw_chat": True, "role": "user",
            "scope": "shared", "source_agent": "claude", "session_id": "s-asof",
            "content": "优化 GUI 的 recall 视图", "evidence": ["e:raw-anchor"],
        })
        self.as_of = _fmt(time.time() - 2 * 86400)

    def test_as_of_disables_followup_anchoring(self):
        builder = ContextPackBuilder(self.ledger)
        anchored = builder.build("继续", ContextPackOptions(max_items=4, min_score=0,
                                                            debug=True))
        self.assertIn("GUI", str(anchored.get("text", "")))
        self.assertTrue(anchored["debug"]["query_context"]["applied"])

        historical = builder.build(
            "继续", ContextPackOptions(max_items=4, min_score=0, debug=True,
                                      as_of=self.as_of))
        historical_off = builder.build(
            "继续", ContextPackOptions(max_items=4, min_score=0, debug=True,
                                      as_of=self.as_of, query_context=False))
        self.assertEqual(historical, historical_off,
                         "as_of 历史包在锚定开关开/关下必须完全一致")
        self.assertNotIn("query_context", historical.get("debug", {}))


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
