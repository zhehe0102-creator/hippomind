"""estimate_tokens 校准契约（迭代 73）。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.context_pack import estimate_tokens


class TokenEstimateTests(unittest.TestCase):
    def test_english_never_undercounts(self):
        """英文估算不得低于 chars/4 下界。"""
        text = "The memory system stores facts with evidence links and retrieves them."
        self.assertGreaterEqual(estimate_tokens(text), len(text) // 4)

    def test_cjk_within_reference_band(self):
        """CJK 估算落在每字 1.0~1.5 的保守区间内。"""
        text = "记忆系统存储带证据链接的事实并按激活分数检索它们" * 4
        cjk_chars = sum(1 for c in text if "\u3400" <= c <= "\u9fff")
        est = estimate_tokens(text)
        self.assertGreaterEqual(est, int(cjk_chars * 1.0))
        self.assertLessEqual(est, int(cjk_chars * 1.5))

    def test_monotonic_in_length(self):
        short = estimate_tokens("短文本")
        long = estimate_tokens("短文本" * 50)
        self.assertGreater(long, short)

    def test_minimum_one_token(self):
        self.assertEqual(estimate_tokens(""), 1)


if __name__ == "__main__":
    unittest.main()
