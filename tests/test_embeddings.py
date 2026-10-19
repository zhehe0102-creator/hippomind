"""可选 embedding 适配器通道测试：确定性/归一化/搜索融合开关。"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from hippomind.embeddings import HashingNGramEmbedding
from hippomind.ledger import SQLiteLedger


class HashingNGramEmbeddingTests(unittest.TestCase):
    def test_deterministic_and_normalized(self):
        embedder = HashingNGramEmbedding()
        a1 = embedder.embed("部署流水线优化经验")
        a2 = embedder.embed("部署流水线优化经验")
        self.assertEqual(a1, a2)
        self.assertEqual(len(a1), 256)
        norm = math.sqrt(sum(v * v for v in a1))
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_cosine_self_is_one_and_others_lower(self):
        embedder = HashingNGramEmbedding()
        a = embedder.embed("数据库连接池调优指南")
        b = embedder.embed("数据库连接池参数说明")
        c = embedder.embed("古埃及象形文字的破译历史")
        self.assertAlmostEqual(embedder.cosine(a, a), 1.0, places=6)
        self.assertGreater(embedder.cosine(a, b), embedder.cosine(a, c))

    def test_empty_text_is_zero_vector(self):
        embedder = HashingNGramEmbedding()
        vec = embedder.embed("")
        self.assertEqual(len(vec), 256)
        self.assertTrue(all(v == 0.0 for v in vec))


class VectorChannelSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "生产环境 PostgreSQL 数据库端口是 5432",
            "evidence": ["evidence:port"],
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_vec_channel_absent_by_default(self):
        hits = self.ledger.search("生产环境的数据库端口", scope="shared", limit=5)
        self.assertTrue(hits)
        self.assertNotIn("vec", hits[0]["score_parts"])

    def test_vec_channel_present_when_enabled(self):
        hits = self.ledger.search("生产环境的数据库端口", scope="shared", limit=5,
                                  embedder=HashingNGramEmbedding())
        self.assertTrue(hits)
        self.assertIn("vec", hits[0]["score_parts"])
        self.assertGreaterEqual(float(hits[0]["score_parts"]["vec"]), 0.0)


if __name__ == "__main__":
    unittest.main()
