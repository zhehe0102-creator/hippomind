"""注入效果：匹配对照组（随机分组）的判定逻辑。

这段逻辑的产出会被写进周报，所以测试断言的都不是"函数能跑"，而是三条会被
人拿去做决定的性质：

1. **单臂时不给结论**。只有注入臂有样本时必须明确说"没有对照"，而不是用
   观察性的 lift 冒充实验结果。
2. **两臂都达标才比较**，且比较的是同一个指标（citation rate）。
3. **对照组样本不足时仍然给数**（treatment_n/control_n 都写出来），让"还差
   多少"可算，而不是一句 insufficient。

背景：claude hook 的对照组是**匹配**对照——两臂都检索、都记录"本会注入什么"，
只有投递与否不同。所以两臂的 citation rate 可以直接相减，前提是样本量够。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.injection_quality import _aggregate, _arm_split, _verdict


def _row(*, delivered: bool, cited: bool, overlap: float = 0.5,
         items: int = 3) -> dict:
    """一条可用于臂对比的 trace。

    items 默认 3 是刻意的：_arm_split 会排除 empty_pack（items==0）的 trace，
    因为那时两臂都没有东西可投递——把「检索没命中」算成「注入无效」会系统性
    低估注入效果。测试要构造「本来有东西可投递」的场景，所以对照组也用 3。
    """
    return {"at": "2026-10-25T00:00:00+00:00", "agent": "claude",
            "source": "claude-hook", "items": items,
            "memory_ids": 3, "replies": 2, "replies_raw": 2,
            "overlap": overlap, "used": True, "signals": 5,
            "citation_rate": 0.4 if cited else 0.0, "cited": cited,
            "paraphrase": 0.6, "corrected": False,
            "control_rate": None, "control_pairs": 0, "lift": None,
            "arm": "treatment" if delivered else "control",
            "delivered": delivered, "evaluable": True, "skip_reason": ""}


class ArmSplitTests(unittest.TestCase):
    def test_empty_input_is_none_not_zero(self):
        split = _arm_split([])
        self.assertIsNone(split["treatment_citation_rate"])
        self.assertIsNone(split["control_citation_rate"])
        self.assertIsNone(split["difference"])
        self.assertFalse(split["matched"])

    def test_single_arm_is_not_matched(self):
        split = _arm_split([_row(delivered=True, cited=True)] * 3)
        self.assertEqual(3, split["treatment_n"])
        self.assertEqual(0, split["control_n"])
        self.assertFalse(split["matched"])
        self.assertIsNone(split["difference"], "只有单臂时不得算差值")

    def test_difference_is_computed_on_the_same_metric(self):
        rows = ([_row(delivered=True, cited=True)] * 3 +
                [_row(delivered=True, cited=False)] +
                [_row(delivered=False, cited=False)] * 4)
        split = _arm_split(rows)
        self.assertTrue(split["matched"])
        self.assertEqual(4, split["treatment_n"])
        self.assertEqual(4, split["control_n"])
        self.assertAlmostEqual(0.75, split["treatment_citation_rate"])
        self.assertAlmostEqual(0.0, split["control_citation_rate"])
        self.assertAlmostEqual(0.75, split["difference"])
        # Wilson 区间必须跟着样本量走，否则读报告的人会以为 4 个样本就能定论。
        self.assertIsNotNone(split["treatment_ci"])
        self.assertIsNotNone(split["control_ci"])

    def test_undelivered_rows_count_as_control(self):
        """delivered=False 就是对照组，与 arm 字符串无关。"""
        row = _row(delivered=False, cited=True)
        row["arm"] = ""  # 老 trace 没有 arm 字段
        split = _arm_split([row])
        self.assertEqual(1, split["control_n"])
        self.assertEqual(0, split["treatment_n"])

    def test_aggregate_carries_the_arms(self):
        rows = ([_row(delivered=True, cited=True)] * 3 +
                [_row(delivered=False, cited=False)] * 3)
        aggregate = _aggregate(rows)
        self.assertEqual(6, aggregate["sampled"])
        self.assertTrue(aggregate["arms"]["matched"])


class ArmVerdictTests(unittest.TestCase):
    def _aggregate_for(self, treatment, control):
        rows = ([_row(delivered=True, cited=True)] * treatment +
                [_row(delivered=False, cited=False)] * control)
        return _aggregate(rows)

    def test_small_arms_do_not_produce_a_conclusion(self):
        aggregate = self._aggregate_for(3, 3)
        verdict = _verdict(aggregate["sampled"], aggregate, min_samples=30,
                           control_mode="matched_not_injected")
        self.assertFalse(verdict["conclusive"])
        self.assertIn(verdict["code"], {"insufficient_sample"})

    def test_both_arms_at_threshold_produces_a_conclusion(self):
        aggregate = self._aggregate_for(30, 30)
        verdict = _verdict(aggregate["sampled"], aggregate, min_samples=30,
                           control_mode="matched_not_injected")
        self.assertTrue(verdict["conclusive"])
        self.assertEqual("arm_positive", verdict["code"])
        self.assertIn("随机分组对照", verdict["headline"])
        # 即便是随机分组，也不能说成因果证明。
        self.assertTrue(any("单账户" in caveat for caveat in verdict["caveats"]))

    def test_negative_difference_is_reported_as_no_lift(self):
        rows = ([_row(delivered=True, cited=False)] * 30 +
                [_row(delivered=False, cited=True)] * 30)
        aggregate = _aggregate(rows)
        verdict = _verdict(aggregate["sampled"], aggregate, min_samples=30,
                           control_mode="matched_not_injected")
        self.assertTrue(verdict["conclusive"])
        self.assertEqual("arm_no_lift", verdict["code"])

    def test_single_arm_still_gets_no_control_conclusion(self):
        aggregate = self._aggregate_for(30, 0)
        verdict = _verdict(aggregate["sampled"], aggregate, min_samples=30,
                           control_mode="none")
        self.assertFalse(verdict["conclusive"])
        self.assertEqual("no_control", verdict["code"])


if __name__ == "__main__":
    unittest.main()
