"""search 深分页一致性与排序稳定性（迭代 40）。"""
import tempfile
import unittest
from pathlib import Path

from hippomind.ledger import SQLiteLedger


def _rec(text: str, marker: str) -> dict:
    return {"kind": "fact", "scope": "shared", "source_agent": "t",
            "content": text, "evidence": [f"t:{marker}"]}


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "page.sqlite")
        # 40 条同主题记忆：查询词全部命中，排序键（score/created_at/updated_at）
        # 需要稳定——ingest 顺序即 created_at 顺序。
        for i in range(40):
            self.led.ingest(_rec(f"分页测试条目 {i} 号：数据库运维事实", f"p{i}"))

    def test_offset_pages_are_disjoint_and_complete(self):
        seen = []
        page_size = 10
        for offset in range(0, 40, page_size):
            page = self.led.search("分页测试 数据库", limit=page_size, offset=offset)
            self.assertLessEqual(len(page), page_size)
            seen.extend(h["memory_id"] for h in page)
        self.assertEqual(len(seen), len(set(seen)), "分页不得重复返回同一记忆")
        # 全部命中候选都应可经分页取回（limit*20 候选预算 ≥ 40 条）
        self.assertEqual(len(seen), 40,
                         f"分页应覆盖全部 40 条命中，实际 {len(seen)}")

    def test_ordering_stable_across_repeated_queries(self):
        first = [h["memory_id"] for h in self.led.search("分页测试 数据库", limit=50)]
        second = [h["memory_id"] for h in self.led.search("分页测试 数据库", limit=50)]
        self.assertEqual(first, second, "同库同查的排序必须稳定")

    def test_offset_beyond_results_returns_empty(self):
        hits = self.led.search("分页测试 数据库", limit=10, offset=10_000)
        self.assertEqual(hits, [])

    def test_first_page_prefix_of_full_result(self):
        full = [h["memory_id"] for h in self.led.search("分页测试 数据库", limit=50)]
        head = [h["memory_id"] for h in self.led.search("分页测试 数据库", limit=5)]
        self.assertEqual(full[:5], head, "小 limit 的首页必须是全量结果前缀")


if __name__ == "__main__":
    unittest.main()
