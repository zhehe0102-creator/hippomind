"""empty_pack 的 trace 不得进入 A/B 臂对比。

为什么这条测试必须存在：A/B 实验问的是「投递这份 pack 有没有用」。
当检索返回 0 条时，**两个臂都没有东西可投递**——对照组不是「被扣留」，
而是「本来就没有」。把这类 trace 算进对照臂，等于把「检索没命中」记成
「注入无效」，会让注入效果被系统性低估。

生产实测：16 条被 A/B 分流的 trace 里 13 条是 empty_pack。不排除的话，
对照臂会被这类噪声完全主导，实验跑再久也得不出结论。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.injection_quality import _arm_split, _aggregate


def _row(*, delivered: bool, cited: bool, items: int) -> dict:
    return {"at": "2026-10-25T00:00:00+00:00", "agent": "claude",
            "source": "claude-hook", "items": items, "memory_ids": 3,
            "replies": 2, "replies_raw": 2, "overlap": 0.5, "used": True,
            "signals": 5, "citation_rate": 0.4 if cited else 0.0, "cited": cited,
            "paraphrase": 0.6, "corrected": False, "control_rate": None,
            "control_pairs": 0, "lift": None,
            "arm": "treatment" if delivered else "control",
            "delivered": delivered, "evaluable": True, "skip_reason": ""}


class EmptyPackExclusionTests(unittest.TestCase):
    def test_empty_pack_traces_are_excluded_from_both_arms(self):
        rows = [
            _row(delivered=True, cited=True, items=5),    # informative treatment
            _row(delivered=False, cited=False, items=0),  # empty_pack control
            _row(delivered=True, cited=False, items=0),   # empty_pack treatment
            _row(delivered=False, cited=True, items=4),   # informative control
        ]
        split = _arm_split(rows)
        self.assertEqual(1, split["treatment_n"])
        self.assertEqual(1, split["control_n"])
        self.assertEqual(2, split["excluded_empty_pack"])

    def test_empty_pack_does_not_drag_the_control_rate_down(self):
        """这是本修正要防的具体失真。"""
        # 1 条真对照（被引用）+ 4 条空包对照（不可能被引用）
        rows = ([_row(delivered=True, cited=True, items=5)] * 3 +
                [_row(delivered=False, cited=True, items=5)] +
                [_row(delivered=False, cited=False, items=0)] * 4)
        split = _arm_split(rows)
        self.assertEqual(1, split["control_n"], "空包不应算进对照臂")
        self.assertAlmostEqual(1.0, split["control_citation_rate"],
                               msg="真对照臂的引用率不应被空包稀释")

    def test_all_empty_pack_yields_no_conclusion(self):
        rows = [_row(delivered=True, cited=False, items=0),
                _row(delivered=False, cited=False, items=0)]
        split = _arm_split(rows)
        self.assertEqual(0, split["treatment_n"])
        self.assertEqual(0, split["control_n"])
        self.assertFalse(split["matched"])
        self.assertIsNone(split["difference"])
        self.assertEqual(2, split["excluded_empty_pack"])

    def test_aggregate_reports_the_exclusion(self):
        rows = ([_row(delivered=True, cited=True, items=5)] * 2 +
                [_row(delivered=False, cited=False, items=5)] * 2 +
                [_row(delivered=True, cited=False, items=0)])
        aggregate = _aggregate(rows)
        self.assertEqual(1, aggregate["arms"]["excluded_empty_pack"])
        self.assertTrue(aggregate["arms"]["matched"])

    def test_zero_and_missing_items_are_treated_alike(self):
        row = _row(delivered=True, cited=True, items=5)
        row["items"] = None
        split = _arm_split([row])
        self.assertEqual(0, split["treatment_n"])
        self.assertEqual(1, split["excluded_empty_pack"])


if __name__ == "__main__":
    unittest.main()
