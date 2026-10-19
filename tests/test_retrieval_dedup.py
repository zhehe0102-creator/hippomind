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
