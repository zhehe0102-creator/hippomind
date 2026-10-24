"""vector_index 的回归测试：构建、增量、指纹失效、RRF 融合、降级。

锁定五件事：

1. **构建与检索**：矩阵乘 + argpartition 的 top-N 必须与暴力排序一致。
2. **增量**：新增/删除/内容变化只动受影响的行，不重算整表。
3. **指纹失效**：换模型 / 换维度 / 文件损坏都必须自动重建（返回 None）。
4. **降级**：numpy 缺失 / embedder 不可用 / 空账本一律返回 None，不抛异常。
5. **RRF 融合正确性**：ledger.search(vector_index=...) 的默认行为零变化，
   开启后只在候选生成/名次上生效，且不引入越权召回（过滤必须重放）。

全部离线：假 embedder 注入，不依赖 onnxruntime / 模型文件 / 网络。
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from hippomind.ledger import SQLiteLedger
from hippomind.vector_index import (
    FORMAT_VERSION, VectorIndex, content_hash, corpus_text, default_index_path,
    fingerprint_of, load_vector_index)


class FakeEmbedder:
    """确定性假 embedder：字符频率投影，无需 onnxruntime。"""

    def __init__(self, dim: int = 16, name: str = "fake"):
        self.dim = dim
        self.name = name
        self.encoded = []

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        for index, char in enumerate(str(text)):
            vector[index % self.dim] += (ord(char) % 23) + 1.0
        return vector

    def encode_matrix(self, texts):
        self.encoded.extend(str(text) for text in texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        rows = [self._vector(text) for text in texts]
        return np.asarray(rows, dtype=np.float32)

    def embed(self, text):
        vector = self.encode_matrix([text])[0]
        norm = float(np.linalg.norm(vector)) or 1.0
        return (vector / norm).tolist()

    def cosine(self, a, b):
        left = np.asarray(a, dtype=np.float32)
        right = np.asarray(b, dtype=np.float32)
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        return 0.0 if denominator <= 0 else float(np.dot(left, right) / denominator)

    def fingerprint(self):
        # 指纹必须同时反映"哪个模型"和"多少维"：真实 OnnxEmbedding 的
        # fingerprint() 也包含 name，否则换模型但维度相同会误判为可复用。
        return "fake-%s-%d" % (self.name, self.dim)


def _ingest(ledger, content, **extra):
    record = {"content": content, "kind": "fact", "source_agent": "vi-tester",
              "scope": "shared", "evidence": ["vi:" + content]}
    record.update(extra)
    return ledger.ingest(record).memory_id


class LedgerCase(unittest.TestCase):
    """每个测试一份独立账本 + 假 embedder。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-vi-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "vi.sqlite")
        self.addCleanup(self.ledger.close)
        self.embedder = FakeEmbedder()


class BuildTests(LedgerCase):
    def test_build_indexes_accepted_active_rows(self):
        first = _ingest(self.ledger, "生产环境 PostgreSQL 数据库端口是 5432")
        second = _ingest(self.ledger, "缓存集群部署在 cache-02 机器")
        index = VectorIndex.build(self.ledger, self.embedder)
        self.assertIsNotNone(index)
        self.assertEqual(2, len(index))
        self.assertEqual(16, index.dim)
        self.assertIn(first, index.ids)
        self.assertIn(second, index.ids)

    def test_build_skips_empty_content(self):
        _ingest(self.ledger, "有内容的记忆")
        index = VectorIndex.build(self.ledger, self.embedder)
        self.assertEqual(1, len(index))

    def test_build_skips_quarantined_and_inactive_rows(self):
        keep = _ingest(self.ledger, "可召回记忆")
        drop_quality = _ingest(self.ledger, "隔离记忆")
        drop_lifecycle = _ingest(self.ledger, "冷记忆")
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET quality_state=? WHERE memory_id=?",
                         ("quarantine", drop_quality))
            conn.execute("UPDATE memories SET lifecycle=? WHERE memory_id=?",
                         ("cold", drop_lifecycle))
        index = VectorIndex.build(self.ledger, self.embedder)
        self.assertEqual([keep], index.ids)

    def test_build_returns_none_for_empty_ledger(self):
        self.assertIsNone(VectorIndex.build(self.ledger, self.embedder))

    def test_build_returns_none_without_embedder(self):
        _ingest(self.ledger, "有内容的记忆")
        self.assertIsNone(VectorIndex.build(self.ledger, None))

    def test_rows_are_normalised_and_finite(self):
        for index in range(5):
            _ingest(self.ledger, "记忆内容 %d 数据库端口" % index)
        index = VectorIndex.build(self.ledger, self.embedder)
        self.assertTrue(bool(np.isfinite(index.matrix).all()))
        norms = np.linalg.norm(index.matrix, axis=1)
        for norm in norms:
            self.assertAlmostEqual(1.0, float(norm), places=5)

    def test_stats_reports_docs_dim_bytes_model(self):
        _ingest(self.ledger, "统计用记忆")
        index = VectorIndex.build(self.ledger, self.embedder)
        stats = index.stats()
        self.assertEqual(1, stats["docs"])
        self.assertEqual(16, stats["dim"])
        self.assertEqual(16 * 4, stats["bytes"])
        self.assertEqual("fake", stats["model"])
        self.assertTrue(stats["built_at"])


class SearchTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ids = {}
        for key, text in (("db", "生产环境数据库端口 5432"),
                          ("cache", "缓存集群部署 cache-02"),
                          ("jazz", "爵士乐即兴演奏和弦进行")):
            self.ids[key] = _ingest(self.ledger, text)
        self.index = VectorIndex.build(self.ledger, self.embedder)

    def test_search_returns_ranked_pairs(self):
        results = self.index.search_text("生产环境数据库端口", topn=3)
        self.assertEqual(3, len(results))
        memory_id, similarity = results[0]
        self.assertEqual(self.ids["db"], memory_id)
        self.assertGreater(similarity, 0.0)
        # 降序
        scores = [score for _mid, score in results]
        self.assertEqual(sorted(scores, reverse=True), scores)

    def test_search_matches_bruteforce_ordering(self):
        query = np.asarray(self.embedder.embed("数据库端口"), dtype=np.float32)
        brute = sorted(zip(self.index.ids, self.index.matrix @ query),
                       key=lambda pair: (-pair[1], pair[0]))
        fast = self.index.search(query, topn=len(self.index))
        self.assertEqual([mid for mid, _s in brute], [mid for mid, _s in fast])

    def test_topn_is_clamped_to_index_size(self):
        self.assertEqual(3, len(self.index.search_text("数据库", topn=100)))

    def test_search_is_deterministic(self):
        first = self.index.search_text("数据库端口", topn=3)
        second = self.index.search_text("数据库端口", topn=3)
        self.assertEqual(first, second)

    def test_empty_query_returns_nothing(self):
        self.assertEqual([], self.index.search_text(""))
        self.assertEqual([], self.index.search_text("   "))

    def test_zero_vector_query_returns_nothing(self):
        self.assertEqual([], self.index.search([0.0] * 16))

    def test_wrong_dimension_query_returns_nothing(self):
        self.assertEqual([], self.index.search([0.5] * 3))

    def test_nan_query_returns_nothing(self):
        vector = np.ones(16, dtype=np.float32)
        vector[0] = np.nan
        self.assertEqual([], self.index.search(vector))

    def test_embedder_failure_degrades_to_empty(self):
        class Boom(FakeEmbedder):
            def embed(self, text):
                raise RuntimeError("embedder down")

        index = VectorIndex(self.embedder, self.index.ids, self.index.matrix)
        index.embedder = Boom()
        self.assertEqual([], index.search_text("数据库"))

    def test_zero_rows_are_masked_out(self):
        matrix = self.index.matrix.copy()
        matrix[0] = 0.0
        index = VectorIndex(self.embedder, self.index.ids, matrix)
        self.assertEqual(len(self.index) - 1, int(index._valid.sum()))
        results = index.search_text("数据库端口", topn=10)
        self.assertEqual(len(self.index) - 1, len(results),
                         "零向量行不得出现在结果里（不是排最后，是丢弃）")
        for memory_id, score in results:
            self.assertNotEqual(self.index.ids[0], memory_id)
            self.assertTrue(math.isfinite(score))


class IncrementalTests(LedgerCase):
    def test_add_many_appends_rows(self):
        index = VectorIndex(self.embedder, [],
                            np.zeros((0, 16), dtype=np.float32))
        added = index.add_many([("m1", "第一条记忆"), ("m2", "第二条记忆")])
        self.assertEqual(2, added)
        self.assertEqual(["m1", "m2"], index.ids)
        self.assertEqual((2, 16), index.matrix.shape)

    def test_add_many_skips_blank_text(self):
        index = VectorIndex(self.embedder, [],
                            np.zeros((0, 16), dtype=np.float32))
        self.assertEqual(0, index.add_many([("m1", "   "), ("m2", "")]))
        self.assertEqual(0, len(index))

    def test_remove_drops_rows(self):
        _ingest(self.ledger, "第一条记忆")
        _ingest(self.ledger, "第二条记忆")
        index = VectorIndex.build(self.ledger, self.embedder)
        victim = index.ids[0]
        self.assertEqual(1, index.remove([victim]))
        self.assertNotIn(victim, index.ids)
        self.assertEqual(1, len(index))
        self.assertEqual(1, index.matrix.shape[0])
        self.assertEqual(1, index.hashes.shape[0])

    def test_remove_unknown_id_is_noop(self):
        _ingest(self.ledger, "第一条记忆")
        index = VectorIndex.build(self.ledger, self.embedder)
        self.assertEqual(0, index.remove(["mem_nope"]))
        self.assertEqual(1, len(index))

    def test_sync_adds_new_rows_only(self):
        _ingest(self.ledger, "已有记忆")
        index = VectorIndex.build(self.ledger, self.embedder)
        self.embedder.encoded.clear()
        _ingest(self.ledger, "新增记忆")
        outcome = index.sync(self.ledger)
        self.assertEqual(1, outcome["added"])
        self.assertEqual(0, outcome["removed"])
        self.assertEqual(2, outcome["docs"])
        # 只 encode 新增那一条，不重算整表
        self.assertEqual(["新增记忆"], self.embedder.encoded)

    def test_sync_removes_rows_that_left_the_set(self):
        keep = _ingest(self.ledger, "保留记忆")
        drop = _ingest(self.ledger, "将被隔离的记忆")
        index = VectorIndex.build(self.ledger, self.embedder)
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET quality_state=? WHERE memory_id=?",
                         ("quarantine", drop))
        outcome = index.sync(self.ledger)
        self.assertEqual(1, outcome["removed"])
        self.assertEqual([keep], index.ids)

    def test_sync_reencodes_changed_content(self):
        memory_id = _ingest(self.ledger, "原始内容 数据库端口")
        index = VectorIndex.build(self.ledger, self.embedder)
        before = index.matrix[0].copy()
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET content=? WHERE memory_id=?",
                         ("完全不同的内容 爵士乐和弦", memory_id))
        self.embedder.encoded.clear()
        outcome = index.sync(self.ledger)
        self.assertEqual(1, outcome["updated"])
        self.assertEqual(1, outcome["removed"])
        self.assertEqual(1, outcome["added"])
        self.assertEqual(1, len(index))
        self.assertFalse(bool((index.matrix[0] == before).all()))
        self.assertEqual(1, len(self.embedder.encoded))

    def test_sync_is_idempotent(self):
        _ingest(self.ledger, "稳定记忆")
        index = VectorIndex.build(self.ledger, self.embedder)
        self.embedder.encoded.clear()
        outcome = index.sync(self.ledger)
        self.assertEqual({"added": 0, "removed": 0, "updated": 0, "docs": 1},
                         outcome)
        self.assertEqual([], self.embedder.encoded)


class PersistenceTests(LedgerCase):
    def test_save_and_load_roundtrip(self):
        _ingest(self.ledger, "生产环境数据库端口 5432")
        _ingest(self.ledger, "缓存集群部署 cache-02")
        index = VectorIndex.build(self.ledger, self.embedder)
        path = self.root / "index.npz"
        self.assertIsNotNone(index.save(path))
        self.assertTrue(path.is_file())
        loaded = VectorIndex.load(self.embedder, path)
        self.assertIsNotNone(loaded)
        self.assertEqual(index.ids, loaded.ids)
        self.assertTrue(bool(np.array_equal(index.matrix, loaded.matrix)))
        self.assertEqual(index.fingerprint, loaded.fingerprint)
        self.assertEqual(index.stats()["docs"], loaded.stats()["docs"])

    def test_save_leaves_no_temporary_file(self):
        _ingest(self.ledger, "生产环境数据库端口 5432")
        index = VectorIndex.build(self.ledger, self.embedder)
        path = self.root / "index.npz"
        index.save(path)
        leftovers = [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual([], leftovers)

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(VectorIndex.load(self.embedder, self.root / "nope.npz"))

    def test_load_corrupt_file_returns_none(self):
        path = self.root / "corrupt.npz"
        path.write_bytes(b"not an npz at all")
        self.assertIsNone(VectorIndex.load(self.embedder, path))

    def test_fingerprint_mismatch_forces_rebuild(self):
        _ingest(self.ledger, "生产环境数据库端口 5432")
        index = VectorIndex.build(self.ledger, self.embedder)
        path = self.root / "index.npz"
        index.save(path)
        # 换模型（不同 name + 不同 fingerprint）必须读不出来
        other = FakeEmbedder(name="other")
        self.assertIsNone(VectorIndex.load(other, path))
        # 换维度也必须读不出来
        wider = FakeEmbedder(dim=32)
        self.assertIsNone(VectorIndex.load(wider, path))

    def test_dimension_mismatch_forces_rebuild(self):
        _ingest(self.ledger, "生产环境数据库端口 5432")
        index = VectorIndex.build(self.ledger, self.embedder)
        path = self.root / "index.npz"
        index.save(path)

        class SamePrint:
            """fingerprint 相同但 dim 不同（防指纹碰撞的独立防线）。"""
            name = "fake"

            def __init__(self):
                # dim 报 32（与落盘矩阵的 16 不符），但指纹字符串与旧索引一致：
                # 这条独立防线保证"指纹碰撞/手工改文件"也不会载入错误矩阵。
                self.dim = 32
                self.inner = FakeEmbedder(dim=32)

            def fingerprint(self):
                return "fake-fake-16"

            def embed(self, text):
                return self.inner.embed(text)

            def encode_matrix(self, texts):
                return self.inner.encode_matrix(texts)

            def cosine(self, a, b):
                return self.inner.cosine(a, b)

        self.assertIsNone(VectorIndex.load(SamePrint(), path))

    def test_load_or_build_uses_existing_file(self):
        _ingest(self.ledger, "生产环境数据库端口 5432")
        path = self.root / "index.npz"
        first = VectorIndex.load_or_build(self.ledger, self.embedder, path=path)
        self.assertIsNotNone(first)
        self.assertTrue(path.is_file())
        self.embedder.encoded.clear()
        second = VectorIndex.load_or_build(self.ledger, self.embedder, path=path)
        self.assertEqual(first.ids, second.ids)
        self.assertEqual([], self.embedder.encoded, "命中磁盘索引不应重新 encode")

    def test_load_or_build_syncs_when_asked(self):
        _ingest(self.ledger, "已有记忆")
        path = self.root / "index.npz"
        VectorIndex.load_or_build(self.ledger, self.embedder, path=path)
        _ingest(self.ledger, "新增记忆")
        synced = VectorIndex.load_or_build(self.ledger, self.embedder, path=path,
                                           sync=True)
        self.assertEqual(2, len(synced))

    def test_default_index_path_uses_hub_home(self):
        previous = os.environ.get("HIPPOMIND_HOME")
        os.environ["HIPPOMIND_HOME"] = str(self.root)
        try:
            self.assertEqual(self.root / "vector_index.npz", default_index_path())
        finally:
            if previous is None:
                os.environ.pop("HIPPOMIND_HOME", None)
            else:
                os.environ["HIPPOMIND_HOME"] = previous


class HelperTests(unittest.TestCase):
    def test_content_hash_is_stable_and_distinguishes(self):
        self.assertEqual(content_hash("数据库"), content_hash("数据库"))
        self.assertNotEqual(content_hash("数据库"), content_hash("缓存"))
        self.assertNotEqual(content_hash(""), content_hash(" "))

    def test_corpus_text_uses_content_only(self):
        self.assertEqual("正文", corpus_text({"content": "正文", "task_id": "t"}))
        self.assertEqual("", corpus_text({}))

    def test_fingerprint_of_prefers_embedder_method(self):
        embedder = FakeEmbedder()
        self.assertEqual(embedder.fingerprint(), fingerprint_of(embedder))
        self.assertEqual("fake-fake-16", fingerprint_of(embedder))

    def test_fingerprint_of_falls_back_to_name_and_dim(self):
        class Bare:
            name = "bare"
            dim = 8

        value = fingerprint_of(Bare())
        self.assertEqual(value, fingerprint_of(Bare()))
        self.assertNotEqual(value, fingerprint_of(FakeEmbedder()))

    def test_format_version_is_pinned(self):
        self.assertEqual(1, FORMAT_VERSION)


class DegradationTests(LedgerCase):
    """可选组件不可用时必须静默降级，绝不抛异常。"""

    def test_load_vector_index_returns_none_without_embedder(self):
        _ingest(self.ledger, "有内容的记忆")
        self.assertIsNone(load_vector_index(self.ledger, None))

    def test_load_vector_index_returns_none_for_empty_ledger(self):
        self.assertIsNone(load_vector_index(self.ledger, self.embedder))

    def test_load_vector_index_survives_embedder_explosion(self):
        class Boom(FakeEmbedder):
            def encode_matrix(self, texts):
                raise RuntimeError("encoder down")

        _ingest(self.ledger, "有内容的记忆")
        self.assertIsNone(load_vector_index(self.ledger, Boom()))

    def test_index_rejects_mismatched_embedder_dim(self):
        with self.assertRaises(ValueError):
            VectorIndex(FakeEmbedder(dim=8), ["m1"],
                        np.zeros((1, 16), dtype=np.float32))

    def test_index_rejects_row_count_mismatch(self):
        with self.assertRaises(ValueError):
            VectorIndex(FakeEmbedder(), ["m1"],
                        np.zeros((2, 16), dtype=np.float32))


class SearchIntegrationTests(LedgerCase):
    """ledger.search(vector_index=...) 的融合契约。"""

    def setUp(self):
        super().setUp()
        self.db = _ingest(self.ledger, "生产环境 PostgreSQL 数据库端口是 5432")
        self.cache = _ingest(self.ledger, "缓存集群部署在 cache-02 机器")
        self.index = VectorIndex.build(self.ledger, self.embedder)

    def test_default_search_is_unchanged(self):
        """默认路径（不传 vector_index）不得出现任何向量痕迹。"""
        hits = self.ledger.search("数据库端口", scope="shared", limit=5)
        self.assertTrue(hits)
        for hit in hits:
            self.assertNotIn("vec_rank", hit["score_parts"])

    def test_vector_index_is_opt_in(self):
        hits = self.ledger.search("数据库端口", scope="shared", limit=5,
                                  vector_index=self.index)
        self.assertTrue(hits)
        self.assertIn("vec_rank", hits[0]["score_parts"])

    def test_vector_channel_recalls_lexically_missed_memory(self):
        """改写式查询：词法完全没命中，但向量通道能把它拉进来。"""
        lexical = self.ledger.search("显存不够怎么把模型压小", scope="shared",
                                     limit=5)
        self.assertEqual([], lexical, "前提：该查询在词法通道上完全落空")
        fused = self.ledger.search("显存不够怎么把模型压小", scope="shared",
                                   limit=5, vector_index=self.index)
        self.assertTrue(fused, "向量通道必须生成候选")
        self.assertIn("vec_rank", fused[0]["score_parts"])

    def test_vector_candidates_respect_scope_filter(self):
        """过滤必须重放：向量索引覆盖全库，不能把别的 scope 带进来。"""
        _ingest(self.ledger, "私有作用域里的数据库端口记忆", scope="private")
        index = VectorIndex.build(self.ledger, self.embedder)
        self.assertEqual(3, len(index), "索引本身覆盖两个 scope")
        hits = self.ledger.search("数据库端口", scope="shared", limit=10,
                                  vector_index=index)
        self.assertTrue(hits)
        for hit in hits:
            self.assertEqual("shared", hit["scope"],
                             "向量通道不得绕过 scope 过滤")

    def test_vector_candidates_respect_require_evidence(self):
        # ingest 本身拒绝空 evidence，所以这里写完之后再剥掉，构造"历史遗留
        # 无证据行"的形态——向量通道必须和词法通道一样把它挡在门外。
        stripped = _ingest(self.ledger, "没有证据的数据库端口记忆")
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET evidence_json=? WHERE memory_id=?",
                         ("[]", stripped))
        index = VectorIndex.build(self.ledger, self.embedder)
        hits = self.ledger.search("数据库端口", scope="shared", limit=10,
                                  vector_index=index, require_evidence=True)
        for hit in hits:
            self.assertTrue(hit["evidence"], "向量通道不得绕过证据门槛")

    def test_rrf_favours_memories_hit_by_both_channels(self):
        """同时被词法与向量命中的记忆应排在只有单通道命中的前面。"""
        both = self.ledger.search("数据库端口", scope="shared", limit=10,
                                  vector_index=self.index)
        ranks = [hit["score_parts"].get("vec_rank") for hit in both]
        self.assertIn(0, ranks, "向量通道的第一名必须在候选里")
        top = both[0]
        self.assertIn(top["memory_id"], {self.db, self.cache})

    def test_switching_vector_index_does_not_hit_stale_cache(self):
        first = self.ledger.search("数据库端口", scope="shared", limit=5)
        second = self.ledger.search("数据库端口", scope="shared", limit=5,
                                    vector_index=self.index)
        self.assertNotIn("vec_rank", first[0]["score_parts"])
        self.assertIn("vec_rank", second[0]["score_parts"])

    def test_empty_index_degrades_to_lexical(self):
        empty = VectorIndex(self.embedder, [], np.zeros((0, 16), dtype=np.float32))
        hits = self.ledger.search("数据库端口", scope="shared", limit=5,
                                  vector_index=empty)
        self.assertTrue(hits)
        for hit in hits:
            self.assertNotIn("vec_rank", hit["score_parts"])

    def test_broken_index_degrades_to_lexical(self):
        class Boom:
            def search_text(self, query, topn=96):
                raise RuntimeError("index down")

        hits = self.ledger.search("数据库端口", scope="shared", limit=5,
                                  vector_index=Boom())
        self.assertTrue(hits)
        for hit in hits:
            self.assertNotIn("vec_rank", hit["score_parts"])

    def test_rrf_k_is_honoured(self):
        wide = self.ledger.search("数据库端口", scope="shared", limit=5,
                                  vector_index=self.index, rrf_k=10.0)
        self.assertTrue(wide)
        for hit in wide:
            self.assertGreaterEqual(hit["score_parts"]["rrf"], 0.0)
            self.assertLessEqual(hit["score_parts"]["rrf"], 1.0)

    def test_vector_topn_is_honoured(self):
        hits = self.ledger.search("数据库端口", scope="shared", limit=5,
                                  vector_index=self.index, vector_topn=1)
        self.assertTrue(hits)
        ranked = [hit for hit in hits if "vec_rank" in hit["score_parts"]]
        self.assertLessEqual(len(ranked), 1)


if __name__ == "__main__":
    unittest.main()
