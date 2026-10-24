"""语义通道改造的回归测试（2026-10-23）。

锁定三件事，防止后续重构悄悄把实测收益改回去：

1. **确定性与归一化**：同文本同向量、L2 范数为 1、空文本为零向量。
2. **特征族权重可观测**：``params()`` 报告实际生效的权重，测试断言默认值；
   改动默认权重必须同时改这里和 docs/SEMANTIC.md，避免"文档说 A 代码是 B"。
3. **本地向量文件**：加载成功时使用文件向量；文件缺失/损坏/维度不符时
   **确定性回退**到字符 n-gram，且永不抛异常（检索路径不能因外部文件失败）。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import unittest

from hippomind.embeddings import (HashingNGramEmbedding, HybridRanker,
                                  HybridWeights, LocalVectorEmbedding,
                                  load_embedder)


class WeightedGramTests(unittest.TestCase):
    """特征族权重：CJK 二元组加权 + 拉丁词边界 gram。"""

    def test_default_weights_are_the_measured_ones(self):
        params = HashingNGramEmbedding().params()
        self.assertEqual(params["dim"], 256)
        self.assertEqual(params["cjk_unigram_weight"], 0.6)
        self.assertEqual(params["cjk_bigram_weight"], 1.8)
        self.assertEqual(params["latin_gram_weight"], 1.0)
        self.assertEqual(params["latin_word_weight"], 0.2)
        self.assertTrue(params["sublinear_tf"])

    def test_cjk_bigram_outweighs_unigram(self):
        embedder = HashingNGramEmbedding()
        weighted = embedder._weighted_grams("数据库")
        unigrams = [w for g, w in weighted if len(g) == 1]
        bigrams = [w for g, w in weighted if len(g) == 2]
        self.assertTrue(unigrams and bigrams)
        self.assertLess(max(unigrams), min(bigrams))

    def test_latin_word_boundary_grams_are_emitted(self):
        embedder = HashingNGramEmbedding()
        grams = set(embedder._grams("postgres port"))
        self.assertIn("^po", grams)
        self.assertIn("rt$", grams)
        self.assertIn("postgres", grams)

    def test_sublinear_tf_dampens_repetition(self):
        embedder = HashingNGramEmbedding()
        once = embedder.embed("部署")
        many = embedder.embed("部署" * 20)
        self.assertAlmostEqual(sum(v * v for v in many), 1.0, places=6)
        self.assertAlmostEqual(sum(v * v for v in once), 1.0, places=6)
        self.assertNotEqual(once, many)

    def test_zero_weight_family_is_dropped(self):
        embedder = HashingNGramEmbedding(cjk_unigram_weight=0.0)
        grams = embedder._grams("数据库")
        cjk_unigrams = [g for g in grams
                        if len(g) == 1 and "\u3400" <= g <= "\u9fff"]
        self.assertEqual(cjk_unigrams, [])

    def test_weight_collision_between_word_and_gram_is_preserved(self):
        # "32g" 同时是整词（权重 0.2）与 ^word$ 三元组（权重 1.0）。
        # 曾因按 gram 合并计数把两个族权重压成一个，导致向量静默改变。
        embedder = HashingNGramEmbedding()
        weighted = embedder._weighted_grams("32g")
        weights = {round(w, 4) for g, w in weighted if g == "32g"}
        self.assertEqual(weights, {0.2, 1.0})


class DeterminismTests(unittest.TestCase):
    def test_same_text_same_vector_and_normalised(self):
        embedder = HashingNGramEmbedding()
        a = embedder.embed("部署流水线优化经验")
        b = embedder.embed("部署流水线优化经验")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 256)
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in a)), 1.0, places=6)

    def test_empty_text_is_zero_vector(self):
        embedder = HashingNGramEmbedding()
        vector = embedder.embed("")
        self.assertEqual(len(vector), 256)
        self.assertTrue(all(value == 0.0 for value in vector))

    def test_cosine_self_is_one_and_unrelated_is_lower(self):
        embedder = HashingNGramEmbedding()
        a = embedder.embed("数据库连接池调优指南")
        b = embedder.embed("数据库连接池参数说明")
        c = embedder.embed("古埃及象形文字的破译历史")
        self.assertAlmostEqual(embedder.cosine(a, a), 1.0, places=6)
        self.assertGreater(embedder.cosine(a, b), embedder.cosine(a, c))

    def test_cosine_guards_degenerate_input(self):
        embedder = HashingNGramEmbedding()
        self.assertEqual(embedder.cosine([], [1.0]), 0.0)
        self.assertEqual(embedder.cosine([1.0, 2.0], [1.0]), 0.0)

    def test_semantic_neighbour_beats_lexical_stranger(self):
        """同一主题改写应比无关文本更接近（这是"有语义"的最低要求）。"""
        embedder = HashingNGramEmbedding()
        query = embedder.embed("生产环境数据库端口")
        near = embedder.embed("生产环境 PostgreSQL 数据库端口是 5432")
        far = embedder.embed("爵士乐即兴演奏和弦进行")
        self.assertGreater(embedder.cosine(query, near),
                           embedder.cosine(query, far))


class LocalVectorFileTests(unittest.TestCase):
    """本地 JSON 向量文件：命中即用，异常即确定性回退。"""

    def _write(self, payload) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        with handle:
            json.dump(payload, handle, ensure_ascii=False)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_loads_vectors_and_normalises(self):
        path = self._write({"dim": 4, "vectors": {"数据库": [1, 0, 0, 0]}})
        loaded = LocalVectorEmbedding.load(path)
        self.assertIsNotNone(loaded)
        self.assertIn("数据库", loaded.vectors)
        norm = math.sqrt(sum(v * v for v in loaded.vectors["数据库"]))
        self.assertAlmostEqual(norm, 1.0, places=6)
        vector = loaded.embed("数据库")
        self.assertEqual(len(vector), 4)
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in vector)), 1.0, places=6)

    def test_missing_file_returns_none(self):
        self.assertIsNone(LocalVectorEmbedding.load("/nonexistent/vectors.json"))

    def test_corrupt_file_returns_none(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        with handle:
            handle.write("{not json")
        self.addCleanup(os.unlink, handle.name)
        self.assertIsNone(LocalVectorEmbedding.load(handle.name))

    def test_wrong_dimension_entries_are_skipped(self):
        path = self._write({"dim": 3, "vectors": {"ok": [1, 0, 0],
                                                  "bad": [1, 0]}})
        loaded = LocalVectorEmbedding.load(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(sorted(loaded.vectors), ["ok"])

    def test_uncovered_text_falls_back_to_ngram(self):
        path = self._write({"dim": 4, "vectors": {"数据库": [1, 0, 0, 0]}})
        loaded = LocalVectorEmbedding.load(path)
        fallback = HashingNGramEmbedding(4)
        self.assertEqual(loaded.embed("完全未收录的文本"),
                         fallback.embed("完全未收录的文本"))

    def test_load_embedder_falls_back_when_file_missing(self):
        embedder = load_embedder("/nonexistent/vectors.json")
        self.assertIsInstance(embedder, HashingNGramEmbedding)

    def test_load_embedder_prefers_valid_file(self):
        path = self._write({"dim": 4, "vectors": {"数据库": [1, 0, 0, 0]}})
        self.assertIsInstance(load_embedder(path), LocalVectorEmbedding)

    def test_env_var_is_honoured(self):
        path = self._write({"dim": 4, "vectors": {"数据库": [1, 0, 0, 0]}})
        previous = os.environ.get("HIPPOMIND_SEMANTIC_VECTORS")
        os.environ["HIPPOMIND_SEMANTIC_VECTORS"] = path
        try:
            self.assertIsInstance(load_embedder(), LocalVectorEmbedding)
        finally:
            if previous is None:
                os.environ.pop("HIPPOMIND_SEMANTIC_VECTORS", None)
            else:
                os.environ["HIPPOMIND_SEMANTIC_VECTORS"] = previous


class HybridContractTests(unittest.TestCase):
    """HybridRanker 的对外契约不变：只改顺序、不增删候选、权重可解释。"""

    def _rows(self):
        return [
            {"memory_id": "a", "content": "数据库端口 5432", "score": 0.9,
             "score_parts": {"fts": 0.9, "coverage": 0.9, "topic": 0.5,
                             "phrase": True, "time": 0.9}},
            {"memory_id": "b", "content": "缓存部署 cache-02", "score": 0.5,
             "score_parts": {"fts": 0.2, "coverage": 0.2, "topic": 0.1,
                             "time": 0.5}},
            {"memory_id": "c", "content": "无关文本", "score": 0.1,
             "score_parts": {"fts": 0.0, "coverage": 0.0, "topic": 0.0,
                             "time": 0.1}},
        ]

    def test_rank_preserves_the_candidate_set(self):
        rows = self._rows()
        ranked = HybridRanker(HashingNGramEmbedding()).rank("数据库端口", rows)
        self.assertEqual(sorted(r["memory_id"] for r in ranked),
                         sorted(r["memory_id"] for r in rows))

    def test_disabled_ranker_returns_input_order(self):
        rows = self._rows()
        self.assertEqual(HybridRanker(None).rank("数据库端口", rows), rows)

    def test_ranking_is_deterministic(self):
        rows = self._rows()
        ranker = HybridRanker(HashingNGramEmbedding())
        self.assertEqual(ranker.rank("数据库端口", rows),
                         ranker.rank("数据库端口", rows))

    def test_weights_normalise_to_one(self):
        weights = HybridWeights(lexical=2.0, vector=1.0, recency=1.0).normalised()
        self.assertAlmostEqual(weights.lexical + weights.vector + weights.recency,
                               1.0, places=6)
        self.assertEqual(HybridWeights(0.0, 0.0, 0.0).normalised().as_dict(),
                         {"lexical": 1.0, "vector": 0.0, "recency": 0.0})

    def test_embedding_adapter_protocol_is_satisfied(self):
        embedder = HashingNGramEmbedding()
        self.assertTrue(callable(embedder.embed))
        self.assertTrue(callable(embedder.cosine))
        self.assertIsInstance(embedder.embed("x"), list)


if __name__ == "__main__":
    unittest.main()
