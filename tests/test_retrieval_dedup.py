"""Retrieval-side de-duplication: the 8-item budget must not be burned on repeats.

The live ledger stored 70,508 raw rows for a single session that contained only
3,573 distinct messages, because every capture wrapped the same user sentence in
freshly timestamped harness blocks.  These tests pin the content-key behaviour
that stops such repeats from occupying Context Pack slots.
"""

from __future__ import annotations

import unittest

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions, _normalised_text_key
from hippomind.textutil import (content_fingerprint, informative_ratio, is_noise_text,
                                normalise_for_dedup, strip_wrappers)


def _wrap(payload: str, stamp: str) -> str:
    return (f"<USER_REQUEST>{payload}</USER_REQUEST>\n"
            f"<ADDITIONAL_METADATA>current time is: {stamp}</ADDITIONAL_METADATA>")


class WrapperNormalisationTests(unittest.TestCase):
    def test_wrapper_content_is_preserved(self):
        """<USER_REQUEST> is a container: its text must survive."""
        self.assertEqual(normalise_for_dedup(_wrap("检查服务状态", "2026-09-05T07:50:55-07:00")),
                         "检查服务状态")

    def test_volatile_blocks_are_dropped_entirely(self):
        self.assertEqual(normalise_for_dedup(_wrap("部署失败", "2026-01-01T00:00:00Z")),
                         "部署失败")

    def test_same_request_at_different_times_has_one_fingerprint(self):
        first = content_fingerprint(_wrap("检查服务状态", "2026-09-05T07:50:55-07:00"))
        second = content_fingerprint(_wrap("检查服务状态", "2026-09-06T09:11:02-07:00"))
        self.assertEqual(first, second)

    def test_different_requests_stay_distinct(self):
        self.assertNotEqual(content_fingerprint(_wrap("检查服务状态", "2026-09-05T07:50:55-07:00")),
                            content_fingerprint(_wrap("检查日志状态", "2026-09-05T07:50:55-07:00")))

    def test_plain_text_is_unchanged_by_normalisation(self):
        self.assertEqual(normalise_for_dedup("部署失败是因为 daemon 未重启"),
                         "部署失败是因为 daemon 未重启")

    def test_strip_wrappers_keeps_short_payloads(self):
        self.assertEqual(strip_wrappers("<USER_REQUEST>好</USER_REQUEST>").strip(), "好")


class ContentKeyTests(unittest.TestCase):
    def test_context_key_collapses_wrapper_repeats(self):
        a = _wrap("检查服务状态", "2026-09-05T07:50:55-07:00")
        b = _wrap("检查服务状态", "2026-09-07T11:00:00-07:00")
        self.assertEqual(_normalised_text_key(a), _normalised_text_key(b))

    def test_context_key_keeps_distinct_questions(self):
        self.assertNotEqual(_normalised_text_key(_wrap("检查服务状态", "2026-09-05T07:50:55-07:00")),
                            _normalised_text_key(_wrap("检查日志状态", "2026-09-05T07:50:55-07:00")))

    def test_context_key_unchanged_for_plain_text(self):
        """No ranking drift: ordinary text keeps its previous key."""
        self.assertEqual(_normalised_text_key("部署失败"), "部署失败")
        self.assertEqual(_normalised_text_key("部署 失败"), "部署失败")


class NoiseClassifierTests(unittest.TestCase):
    def test_harness_banners_are_noise(self):
        for banner in ("# AGENTS.md instructions for /Users/zps",
                       "<environment_context>",
                       "The current local time is: 2026-09-05",
                       "[exit 0]"):
            self.assertTrue(is_noise_text(banner), banner)

    def test_acknowledgements_are_noise(self):
        for filler in ("ok", "好的", "继续", "/compact"):
            self.assertTrue(is_noise_text(filler), filler)

    def test_real_lessons_are_kept(self):
        for lesson in ("部署失败是因为 daemon 没有重启，先 stop 再 start",
                       "用 Redis 因为要持久化",
                       "把默认 token 预算改成 6000"):
            self.assertFalse(is_noise_text(lesson), lesson)

    def test_informative_ratio_separates_prose_from_noise(self):
        self.assertGreater(informative_ratio("部署失败是因为 daemon 未重启"), 0.6)
        self.assertLess(informative_ratio("--- ... ==="), 0.3)


class PackLevelDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        import os
        import tempfile

        from hippomind.ledger import SQLiteLedger

        self.home = tempfile.mkdtemp()
        self.ledger = SQLiteLedger(os.path.join(self.home, "ledger.sqlite"))
        self.builder = ContextPackBuilder(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()

    def test_repeated_wrapper_captures_yield_one_item(self):
        for index in range(6):
            self.ledger.ingest({
                "kind": "raw_chat", "raw_chat": True, "role": "user",
                "source_agent": "pi", "session_id": "s1", "evidence": [f"a:{index}"],
                "content": _wrap("检查 daemon API 的并发限制", f"2026-09-0{index + 1}T07:50:55-07:00"),
            })
        pack = self.builder.build("并发限制", ContextPackOptions(raw_chat=True, debug=True))
        matching = [item for item in pack["items"]
                    if "并发限制" in str(item.get("content", "")) or
                    "并发限制" in str(item.get("text", ""))]
        self.assertLessEqual(len(matching), 1, "同一请求的重复抓取不应占用多个名额")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 内容级折叠（content fold）与混合检索通道的回归锁定
# ---------------------------------------------------------------------------
# 背景（线上实测形态，不是估算）：1,475,584 条 raw 行里同一会话 70,508 行
# 只有 3,573 条唯一 content（≈95% 冗余）；56,010 条 kind='semantic' 只对应
# 142 条不同正文，最热一条重复 5,423 次。折叠必须在 MMR 之前，且不能把
# "同一结论的不同时间版本"（双时态）误折叠掉。


class FoldKeyTests(unittest.TestCase):
    """折叠键语义：只折叠"同一断言被重复采集"，保留双时态版本。"""

    def test_volatile_wrapper_repeats_share_one_key(self):
        from hippomind.context_pack import _fold_key
        a = {"content": _wrap("检查服务状态", "2026-09-01T07:50:55-07:00")}
        b = {"content": _wrap("检查服务状态", "2026-09-01T18:11:02-07:00")}
        self.assertEqual(_fold_key(a), _fold_key(b))

    def test_distinct_content_keeps_distinct_key(self):
        from hippomind.context_pack import _fold_key
        self.assertNotEqual(_fold_key({"content": "检查服务状态"}),
                            _fold_key({"content": "检查日志状态"}))

    def test_dual_temporal_valid_from_survives(self):
        """同一结论在不同生效窗口下是历史，不得折叠。"""
        from hippomind.context_pack import _fold_key
        old = {"content": "部署端口 8000", "valid_from": "2026-01-01T00:00:00+00:00"}
        new = {"content": "部署端口 8000", "valid_from": "2026-02-01T00:00:00+00:00"}
        self.assertNotEqual(_fold_key(old), _fold_key(new))

    def test_confidence_change_survives(self):
        """置信度被显著上调是不同的证据强度版本。"""
        from hippomind.context_pack import _fold_key
        weak = {"content": "支付接口 /v2/pay", "confidence": 0.55}
        strong = {"content": "支付接口 /v2/pay", "confidence": 0.90}
        self.assertNotEqual(_fold_key(weak), _fold_key(strong))

    def test_superseded_rows_never_fold_into_successor(self):
        """取代关系是结构，不是重复。"""
        from hippomind.context_pack import _fold_key
        live = {"content": "缓存方案 Redis 集群"}
        old = {"content": "缓存方案 Redis 集群", "superseded_by": "mem_successor"}
        self.assertNotEqual(_fold_key(live), _fold_key(old))

    def test_same_day_recaptures_fold(self):
        """同一天内的重复采集折叠（采集时间戳不应伪装成历史版本）。"""
        from hippomind.context_pack import _fold_key
        first = {"content": "部署端口 8000", "valid_from": "2026-01-01T01:00:00+00:00"}
        second = {"content": "部署端口 8000", "valid_from": "2026-01-01T23:00:00+00:00"}
        self.assertEqual(_fold_key(first), _fold_key(second))


class ContextFoldTests(unittest.TestCase):
    """端到端：折叠在 MMR 之前生效，把预算还给真正不同的内容。"""

    TOPICS = ["数据库端口", "缓存容量", "Kafka分区", "网关限流", "日志保留",
              "备份策略", "证书轮换", "CDN回源", "监控告警", "对象存储"]

    def setUp(self) -> None:
        import os
        import tempfile

        from hippomind.ledger import SQLiteLedger

        self.home = tempfile.mkdtemp()
        self.ledger = SQLiteLedger(os.path.join(self.home, "fold.sqlite"))
        # 10 个不同事实 × 30 份重复副本 = 300 行，模拟线上冗余密度。
        # 副本数必须足够高才会暴露"候选池被重复占满"：12 份时固定深度 96
        # 恰好还能覆盖 8 个主题，30 份时只能覆盖 3 个左右（实测 4 条不同）。
        for index, topic in enumerate(self.TOPICS):
            for copy in range(30):
                self.ledger.ingest({
                    "kind": "fact", "scope": "shared",
                    "source_agent": f"agent-{copy % 3}",
                    "session_id": f"sess-{copy}", "task_id": f"task-{index}",
                    "content": f"{topic} 的配置值是 V{index}，属于服务 S{index}",
                    "evidence": [f"e:{index}:{copy}"],
                    "timestamp": f"2026-08-01T{copy % 24:02d}:{copy % 60:02d}:00+00:00",
                })
        self.builder = ContextPackBuilder(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()

    def test_fold_reports_collapse_counts_in_debug(self):
        pack = self.builder.build("配置值 服务",
                                  ContextPackOptions(max_items=8, debug=True))
        fold = pack["debug"]["fold"]
        self.assertTrue(fold["applied"])
        self.assertGreater(fold["folded"], 0, "重复候选必须被折叠")
        self.assertLess(fold["after"], fold["before"])
        self.assertGreater(fold["fold_ratio"], 0.5, "此形态下冗余密度应很高")
        # 折叠记录必须进入 filtered 轨迹，便于线上定位。
        self.assertTrue(any("折叠" in str(entry.get("reason", ""))
                            for entry in pack["debug"]["filtered"]))

    def test_fold_lets_more_distinct_topics_fill_the_budget(self):
        """折叠的真正收益：8 条预算里装到更多**不同**结论。"""
        import re

        folded = self.builder.build("配置值 服务",
                                    ContextPackOptions(max_items=8, debug=True))
        unfolded = self.builder.build("配置值 服务",
                                      ContextPackOptions(max_items=8, debug=True,
                                                         fold_duplicates=False))
        def distinct(pack):
            return len(set(re.findall(r"V(\d+)", pack["text"])))
        self.assertGreater(distinct(folded), distinct(unfolded),
                           "折叠后应装下更多不同结论")
        self.assertGreaterEqual(distinct(folded), 8, "8 条预算应被真正不同的内容填满")

    def test_fold_is_idempotent(self):
        first = self.builder.build("配置值 服务", ContextPackOptions(max_items=8, debug=True))
        second = self.builder.build("配置值 服务", ContextPackOptions(max_items=8, debug=True))
        self.assertEqual(first["text"], second["text"])

    def test_fold_disabled_restores_previous_behaviour(self):
        pack = self.builder.build("配置值 服务",
                                  ContextPackOptions(max_items=8, debug=True,
                                                     fold_duplicates=False))
        self.assertFalse(pack["debug"]["fold"]["applied"])
        self.assertEqual(pack["debug"]["fold"]["folded"], 0)
