"""vector_index 的回归测试：构建、增量、指纹失效、RRF 融合、降级。

锁定五件事：

1. **构建与检索**：矩阵乘 + argpartition 的 top-N 必须与暴力排序一致。
2. **增量**：新增/删除/内容变化只动受影响的行，不重算整表。
3. **指纹失效**：换模型 / 换维度 / 文件损坏都必须自动重建（返回 None）。
4. **降级**：numpy 缺失 / embedder 不可用 / 空账本一律返回 None，不抛异常。
5. **RRF 融合正确性**：ledger.search(vector_index=...) 的默认行为零变化，
   开启后只在候选生成/名次上生效，且不引入越权召回（过滤必须重放）。

6. **claim 级索引**（task-18，2026-10-24）：构建/持久化/指纹失效/增量、
   记忆级去重、claim 失效移除、粒度回退顺序。claim 级是**并列**的新能力，
   上面 1-5 锁定的 memory 级行为一个字都不能变——本文件的后半部分就是
   这条约束的回归测试。

全部离线：假 embedder 注入，不依赖 onnxruntime / 模型文件 / 网络。
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

import numpy as np

# 门禁逻辑在 scripts/ 下（不是包），按脚本自身的方式导入。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eval_semantic  # noqa: E402

# 隔离：本模块任何代码路径都不得读写**真实**的 $HIPPOMIND_HOME。
# 曾经踩过：一个测试调用 load_granular_index() 没传 path，于是默认路径解析到
# 生产账本的 vector_index.npz，被 FakeEmbedder（16 维）覆盖。测试进程必须
# 从一开始就指向临时目录，而不是依赖每个测试自觉传 path。
_HERMETIC_HOME = tempfile.mkdtemp(prefix="hippo-vi-home-")
os.environ["HIPPOMIND_HOME"] = _HERMETIC_HOME

from hippomind.ledger import SQLiteLedger
from hippomind.vector_index import (
    CLAIM_FORMAT_VERSION, CLAIM_INDEX_FILENAME, CLAIM_OVERSCAN, DEFAULT_GRANULARITY,
    ENV_GRANULARITY, FORMAT_VERSION, GRANULARITY_CLAIM, GRANULARITY_MEMORY,
    ClaimVectorIndex, VectorIndex, claim_text, configured_granularity, content_hash,
    corpus_text, default_claim_index_path, default_index_path, fingerprint_of,
    load_claim_vector_index, load_granular_index, load_vector_index)


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


def _claim(subject, predicate, value, slot_key=None):
    """构造一条 claim（账本要求 subject/predicate/slot_key/value 齐全）。"""
    return {"subject": subject, "predicate": predicate,
            "slot_key": slot_key or ("%s.%s" % (subject, predicate)),
            "value": value}


def _ingest_claims(ledger, content, claims, **extra):
    """写入一条带 claims 的记忆，返回 (memory_id, [claim_id, ...])。"""
    record = {"content": content, "kind": "fact", "source_agent": "vi-tester",
              "scope": "shared", "evidence": ["vi:" + content], "claims": claims}
    record.update(extra)
    memory_id = ledger.ingest(record).memory_id
    with ledger._reading() as conn:
        rows = conn.execute(
            "SELECT claim_id FROM memory_claims WHERE memory_id=? ORDER BY claim_id",
            (memory_id,)).fetchall()
    return memory_id, [str(row["claim_id"]) for row in rows]


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



# ---------------------------------------------------------------------------
# claim 级索引（task-18）
# ---------------------------------------------------------------------------
# 背景（实测见 docs/SEMANTIC.md「claim 级索引」）：真正的瓶颈是索引粒度。
# 生产提炼记忆把 5-10 个互不相关的事实塞进一条 350-550 字符的正文，整体
# embedding 被稀释成"什么都像一点"的中心点。按 claim 建索引后两套查询集都
# 大幅提升（semantic 0.226 -> 0.415，keyword 0.652 -> 0.826）。
#
# 这些测试锁定的契约：
#   1. 只索引 state='active' 且记忆 accepted+active 的 claim（与 FTS 同集合）；
#   2. 对外返回**记忆级**去重结果（同一记忆多条 claim 只留最好的名次）；
#   3. 持久化独立文件 + 独立 format_version + 指纹失效重建；
#   4. 增量：只 encode 新增/内容变化的 claim，失效 claim 移除；
#   5. 降级：numpy/embedder/账本缺失一律返回 None；
#   6. memory 级行为零变化（上面的测试类就是那条约束的回归）。


class ClaimTextTests(unittest.TestCase):
    """claim 文本 = subject + " " + predicate + " " + value（value 先解 JSON）。"""

    def test_joins_three_parts(self):
        self.assertEqual("RelayDesk 运行方式 LaunchAgent 启动",
                         claim_text("RelayDesk", "运行方式", '"LaunchAgent 启动"'))

    def test_decodes_json_string_value(self):
        # 账本里 value 是 JSON 文本；直接 embed 字面量会把引号当语义信号。
        self.assertEqual("端口 5432", claim_text("端口", "是", '"5432"').replace("是 ", ""))

    def test_flattens_json_object_value(self):
        text = claim_text("配置", "包含", '{"b": 2, "a": "中文"}')
        self.assertIn("中文", text)
        self.assertNotIn("\\u", text, "中文必须以原字面量出现，不能是 JSON 转义")

    def test_invalid_json_falls_back_to_raw_text(self):
        self.assertEqual("a b {not json", claim_text("a", "b", "{not json"))

    def test_none_value_is_omitted(self):
        self.assertEqual("a b", claim_text("a", "b", None))

    def test_blank_parts_are_skipped(self):
        self.assertEqual("subject predicate",
                         claim_text("subject", "predicate", "   "))


class ClaimBuildTests(LedgerCase):
    def test_build_indexes_active_claims_of_accepted_memories(self):
        first, first_claims = _ingest_claims(self.ledger, "记忆一", [
            _claim("PostgreSQL", "端口", 5432),
            _claim("PostgreSQL", "部署", "db-01"),
        ])
        second, second_claims = _ingest_claims(self.ledger, "记忆二", [
            _claim("Redis", "端口", 6379),
        ])
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        self.assertIsNotNone(index)
        self.assertEqual(3, len(index), "矩阵行是 claim，不是记忆")
        self.assertEqual(sorted(first_claims + second_claims),
                         sorted(index.claim_ids))
        self.assertEqual(2, len(set(index.memory_ids)), "两条记忆")
        self.assertEqual(16, index.dim)

    def test_build_skips_memories_outside_fts_predicate(self):
        keep, _ = _ingest_claims(self.ledger, "可召回", [_claim("A", "b", 1)])
        drop, _ = _ingest_claims(self.ledger, "隔离", [_claim("B", "c", 2)])
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET quality_state=? WHERE memory_id=?",
                         ("quarantine", drop))
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        self.assertEqual([keep], list(dict.fromkeys(index.memory_ids)))

    def test_build_skips_non_active_claims(self):
        memory_id, claim_ids = _ingest_claims(self.ledger, "记忆", [
            _claim("A", "b", 1), _claim("C", "d", 2)])
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memory_claims SET state=? WHERE claim_id=?",
                         ("superseded", claim_ids[0]))
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        self.assertEqual([claim_ids[1]], index.claim_ids)

    def test_build_returns_none_without_claims(self):
        _ingest(self.ledger, "没有 claim 的记忆")
        self.assertIsNone(ClaimVectorIndex.build(self.ledger, self.embedder))

    def test_build_returns_none_without_embedder(self):
        _ingest_claims(self.ledger, "记忆", [_claim("A", "b", 1)])
        self.assertIsNone(ClaimVectorIndex.build(self.ledger, None))

    def test_rows_are_normalised_and_finite(self):
        for index in range(4):
            _ingest_claims(self.ledger, "记忆 %d" % index,
                           [_claim("S%d" % index, "p", index)])
        built = ClaimVectorIndex.build(self.ledger, self.embedder)
        self.assertTrue(bool(np.isfinite(built.matrix).all()))
        for norm in np.linalg.norm(built.matrix, axis=1):
            self.assertAlmostEqual(1.0, float(norm), places=5)

    def test_stats_reports_claims_and_memories(self):
        _ingest_claims(self.ledger, "记忆", [_claim("A", "b", 1), _claim("C", "d", 2)])
        built = ClaimVectorIndex.build(self.ledger, self.embedder)
        stats = built.stats()
        self.assertEqual(2, stats["claims"])
        self.assertEqual(1, stats["memories"])
        self.assertEqual(2, stats["docs"])
        self.assertEqual(16, stats["dim"])
        self.assertEqual(2 * 16 * 4, stats["bytes"])
        self.assertEqual("claim", stats["granularity"])


class ClaimSearchTests(LedgerCase):
    """检索必须返回**记忆级**去重结果，同一记忆只保留最好的名次。"""

    def setUp(self):
        super().setUp()
        # 一条"什么都塞进去"的记忆：3 条互不相关的 claim（这正是生产形态）。
        self.dense, self.dense_claims = _ingest_claims(
            self.ledger, "纸本；SQLite；mac-mini；6000；schema v4", [
                _claim("纸本", "用途", "纸质笔记本"),
                _claim("SQLite", "用途", "本地账本存储"),
                _claim("mac-mini", "角色", "常驻主机"),
            ])
        self.other, _ = _ingest_claims(self.ledger, "Redis 端口", [
            _claim("Redis", "端口", 6379)])
        self.index = ClaimVectorIndex.build(self.ledger, self.embedder)

    def test_search_deduplicates_to_memory_level(self):
        results = self.index.search_text("纸本 SQLite mac-mini", topn=10)
        memory_ids = [memory_id for memory_id, _score, _claim in results]
        self.assertEqual(len(memory_ids), len(set(memory_ids)),
                         "同一记忆的多条 claim 只能出现一次")
        self.assertIn(self.dense, memory_ids)

    def test_search_returns_best_claim_for_the_memory(self):
        results = self.index.search_text("纸本 SQLite mac-mini", topn=10)
        for memory_id, _score, claim_id in results:
            if memory_id == self.dense:
                # 保留的是**最好**的那条 claim（名次最靠前），不是随便一条。
                best = max(
                    ((cid, float(np.dot(self.index.matrix[
                        self.index.claim_ids.index(cid)],
                        np.asarray(self.embedder.embed("纸本 SQLite mac-mini"),
                                   dtype=np.float32))))
                     for cid in self.dense_claims),
                    key=lambda pair: pair[1])
                self.assertEqual(best[0], claim_id)
                break
        else:  # pragma: no cover - 前提不成立说明索引本身坏了
            self.fail("目标记忆必须出现在结果里")

    def test_search_topn_counts_memories_not_claims(self):
        results = self.index.search_text("纸本 SQLite mac-mini", topn=1)
        self.assertEqual(1, len(results), "topn 的语义是记忆数")

    def test_search_scores_are_descending(self):
        scores = [score for _m, score, _c in self.index.search_text("端口", topn=10)]
        self.assertEqual(sorted(scores, reverse=True), scores)

    def test_search_is_deterministic(self):
        first = self.index.search_text("端口", topn=10)
        second = self.index.search_text("端口", topn=10)
        self.assertEqual(first, second)

    def test_empty_and_blank_query_return_nothing(self):
        self.assertEqual([], self.index.search_text(""))
        self.assertEqual([], self.index.search_text("   "))

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

        index = ClaimVectorIndex(self.embedder, self.index.claim_ids,
                                 self.index.memory_ids, self.index.matrix)
        index.embedder = Boom()
        self.assertEqual([], index.search_text("端口"))

    def test_zero_rows_are_masked_out(self):
        matrix = self.index.matrix.copy()
        matrix[0] = 0.0
        index = ClaimVectorIndex(self.embedder, self.index.claim_ids,
                                 self.index.memory_ids, matrix)
        self.assertEqual(len(self.index) - 1, int(index._valid.sum()))
        results = index.search_text("端口", topn=10)
        for _memory_id, score, _claim in results:
            self.assertTrue(math.isfinite(score))
        dropped = self.index.claim_ids[0]
        self.assertNotIn(dropped, [claim for _m, _s, claim in results])

    def test_overscan_collapses_dense_memories(self):
        """一个记忆独占很多 claim 时，topn 仍应尽量凑满不同的记忆。"""
        # 8 条 claim 全在同一记忆上：只取 topn 行会折叠出 1 条记忆。
        _ingest_claims(self.ledger, "稠密记忆", [
            _claim("稠密主题", "项 %d" % index, "值 %d" % index) for index in range(8)])
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        results = index.search_text("稠密主题", topn=3)
        self.assertGreater(len(results), 1,
                           "超额取数 %d 倍后应折叠出多于 1 条记忆" % CLAIM_OVERSCAN)


class ClaimPersistenceTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.memory_id, self.claim_ids = _ingest_claims(self.ledger, "记忆", [
            _claim("PostgreSQL", "端口", 5432), _claim("Redis", "端口", 6379)])
        self.index = ClaimVectorIndex.build(self.ledger, self.embedder)

    def test_save_and_load_roundtrip(self):
        path = self.root / "claims.npz"
        self.assertIsNotNone(self.index.save(path))
        self.assertTrue(path.is_file())
        loaded = ClaimVectorIndex.load(self.embedder, path)
        self.assertIsNotNone(loaded)
        self.assertEqual(self.index.claim_ids, loaded.claim_ids)
        self.assertEqual(self.index.memory_ids, loaded.memory_ids)
        self.assertTrue(bool(np.array_equal(self.index.matrix, loaded.matrix)))
        self.assertEqual(self.index.fingerprint, loaded.fingerprint)

    def test_save_leaves_no_temporary_file(self):
        self.index.save(self.root / "claims.npz")
        self.assertEqual([], [p.name for p in self.root.iterdir()
                              if p.name.endswith(".tmp")])

    def test_load_missing_file_returns_none(self):
        self.assertIsNone(ClaimVectorIndex.load(self.embedder, self.root / "nope.npz"))

    def test_load_corrupt_file_returns_none(self):
        path = self.root / "corrupt.npz"
        path.write_bytes(b"not an npz at all")
        self.assertIsNone(ClaimVectorIndex.load(self.embedder, path))

    def test_fingerprint_mismatch_forces_rebuild(self):
        path = self.root / "claims.npz"
        self.index.save(path)
        self.assertIsNone(ClaimVectorIndex.load(FakeEmbedder(name="other"), path))
        self.assertIsNone(ClaimVectorIndex.load(FakeEmbedder(dim=32), path))

    def test_claim_and_memory_indexes_use_separate_files(self):
        self.assertEqual("vector_index.npz", default_index_path().name)
        self.assertEqual(CLAIM_INDEX_FILENAME, default_claim_index_path().name)
        self.assertNotEqual(default_index_path(), default_claim_index_path())

    def test_format_versions_are_independent(self):
        self.assertEqual(1, CLAIM_FORMAT_VERSION)
        self.assertEqual(1, FORMAT_VERSION)

    def test_load_or_build_uses_existing_file(self):
        path = self.root / "claims.npz"
        first = ClaimVectorIndex.load_or_build(self.ledger, self.embedder, path=path)
        self.assertIsNotNone(first)
        self.embedder.encoded.clear()
        second = ClaimVectorIndex.load_or_build(self.ledger, self.embedder, path=path)
        self.assertEqual(first.claim_ids, second.claim_ids)
        self.assertEqual([], self.embedder.encoded, "命中磁盘索引不应重新 encode")

    def test_default_claim_path_uses_hub_home(self):
        previous = os.environ.get("HIPPOMIND_HOME")
        os.environ["HIPPOMIND_HOME"] = str(self.root)
        try:
            self.assertEqual(self.root / CLAIM_INDEX_FILENAME,
                             default_claim_index_path())
        finally:
            if previous is None:
                os.environ.pop("HIPPOMIND_HOME", None)
            else:
                os.environ["HIPPOMIND_HOME"] = previous


class ClaimIncrementalTests(LedgerCase):
    def test_sync_adds_only_new_claims(self):
        _ingest_claims(self.ledger, "已有", [_claim("A", "b", 1)])
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        self.embedder.encoded.clear()
        _ingest_claims(self.ledger, "新增", [_claim("C", "d", 2)])
        outcome = index.sync(self.ledger)
        self.assertEqual(1, outcome["added"])
        self.assertEqual(0, outcome["removed"])
        self.assertEqual(2, outcome["docs"])
        self.assertEqual(1, len(self.embedder.encoded), "只 encode 新增那条")

    def test_sync_removes_superseded_claims(self):
        memory_id, claim_ids = _ingest_claims(self.ledger, "记忆", [
            _claim("A", "b", 1), _claim("C", "d", 2)])
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memory_claims SET state=? WHERE claim_id=?",
                         ("superseded", claim_ids[0]))
        outcome = index.sync(self.ledger)
        self.assertEqual(1, outcome["removed"])
        self.assertEqual([claim_ids[1]], index.claim_ids)

    def test_sync_removes_claims_of_deactivated_memories(self):
        keep, keep_claims = _ingest_claims(self.ledger, "保留", [_claim("A", "b", 1)])
        drop, drop_claims = _ingest_claims(self.ledger, "下线", [_claim("C", "d", 2)])
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET lifecycle=? WHERE memory_id=?",
                         ("cold", drop))
        outcome = index.sync(self.ledger)
        self.assertEqual(1, outcome["removed"])
        self.assertEqual(keep_claims, index.claim_ids)
        self.assertEqual([keep], index.memory_ids)

    def test_sync_reencodes_changed_value(self):
        memory_id, claim_ids = _ingest_claims(self.ledger, "记忆", [_claim("A", "b", "旧值")])
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        before = index.matrix[0].copy()
        # claim_id 是 (memory_id, subject, predicate, slot_key) 的哈希：value 变了
        # id 不变，所以"内容哈希"这一层是发现变化的唯一途径。
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memory_claims SET value_json=? WHERE claim_id=?",
                         ('"完全不同的新值 爵士乐和弦"', claim_ids[0]))
        self.embedder.encoded.clear()
        outcome = index.sync(self.ledger)
        self.assertEqual(1, outcome["updated"])
        self.assertEqual(1, len(self.embedder.encoded))
        self.assertFalse(bool((index.matrix[0] == before).all()))

    def test_sync_is_idempotent(self):
        _ingest_claims(self.ledger, "稳定", [_claim("A", "b", 1)])
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        self.embedder.encoded.clear()
        outcome = index.sync(self.ledger)
        self.assertEqual({"added": 0, "removed": 0, "updated": 0, "docs": 1,
                          "memories": 1}, outcome)
        self.assertEqual([], self.embedder.encoded)

    def test_add_many_skips_blank_text(self):
        index = ClaimVectorIndex(self.embedder, [], [],
                                 np.zeros((0, 16), dtype=np.float32))
        self.assertEqual(0, index.add_many([("c1", "m1", "   "), ("c2", "m2", "")]))
        self.assertEqual(0, len(index))

    def test_remove_unknown_claim_is_noop(self):
        _ingest_claims(self.ledger, "记忆", [_claim("A", "b", 1)])
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        self.assertEqual(0, index.remove(["cl_nope"]))
        self.assertEqual(1, len(index))


class ClaimDegradationTests(LedgerCase):
    def test_load_claim_vector_index_returns_none_without_embedder(self):
        _ingest_claims(self.ledger, "记忆", [_claim("A", "b", 1)])
        self.assertIsNone(load_claim_vector_index(self.ledger, None))

    def test_load_claim_vector_index_returns_none_for_empty_ledger(self):
        self.assertIsNone(load_claim_vector_index(self.ledger, self.embedder))

    def test_load_claim_vector_index_survives_embedder_explosion(self):
        class Boom(FakeEmbedder):
            def encode_matrix(self, texts):
                raise RuntimeError("encoder down")

        _ingest_claims(self.ledger, "记忆", [_claim("A", "b", 1)])
        self.assertIsNone(load_claim_vector_index(self.ledger, Boom()))

    def test_index_rejects_row_count_mismatch(self):
        with self.assertRaises(ValueError):
            ClaimVectorIndex(self.embedder, ["c1"], [], np.zeros((1, 16), dtype=np.float32))

    def test_index_rejects_memory_id_count_mismatch(self):
        with self.assertRaises(ValueError):
            ClaimVectorIndex(self.embedder, ["c1"], ["m1", "m2"],
                             np.zeros((1, 16), dtype=np.float32))

    def test_index_rejects_mismatched_embedder_dim(self):
        with self.assertRaises(ValueError):
            ClaimVectorIndex(FakeEmbedder(dim=8), ["c1"], ["m1"],
                             np.zeros((1, 16), dtype=np.float32))


class GranularityTests(LedgerCase):
    """粒度选择：环境变量、回退顺序、默认值。"""

    def setUp(self):
        super().setUp()
        self.memory_id, self.claim_ids = _ingest_claims(self.ledger, "记忆", [
            _claim("PostgreSQL", "端口", 5432)])

    def tearDown(self):
        os.environ.pop(ENV_GRANULARITY, None)
        super().tearDown()

    def test_default_granularity_is_claim(self):
        os.environ.pop(ENV_GRANULARITY, None)
        self.assertEqual(GRANULARITY_CLAIM, DEFAULT_GRANULARITY)
        self.assertEqual(GRANULARITY_CLAIM, configured_granularity())

    def test_env_switches_granularity(self):
        os.environ[ENV_GRANULARITY] = "memory"
        self.assertEqual(GRANULARITY_MEMORY, configured_granularity())
        os.environ[ENV_GRANULARITY] = "CLAIM"
        self.assertEqual(GRANULARITY_CLAIM, configured_granularity())

    def test_invalid_env_falls_back_silently(self):
        os.environ[ENV_GRANULARITY] = "nonsense"
        self.assertEqual(DEFAULT_GRANULARITY, configured_granularity())

    def test_load_granular_index_returns_claim_index_by_default(self):
        index = load_granular_index(self.ledger, self.embedder,
                                    path=self.root / "claims.npz")
        self.assertIsInstance(index, ClaimVectorIndex)
        self.assertEqual("claim", index.granularity)

    def test_load_granular_index_honours_memory_choice(self):
        index = load_granular_index(self.ledger, self.embedder,
                                    granularity="memory")
        self.assertIsInstance(index, VectorIndex)
        self.assertNotIsInstance(index, ClaimVectorIndex)

    def test_claim_index_falls_back_to_memory_when_empty(self):
        """账本没有 claim 时不能让"没有向量通道"成为结论——退回 memory 级。"""
        empty = SQLiteLedger(self.root / "empty.sqlite")
        self.addCleanup(empty.close)
        _ingest(empty, "没有 claim 的记忆")
        index = load_granular_index(empty, self.embedder)
        self.assertIsInstance(index, VectorIndex)
        self.assertNotIsInstance(index, ClaimVectorIndex)

    def test_load_granular_index_returns_none_without_embedder(self):
        self.assertIsNone(load_granular_index(self.ledger, None))

    def test_build_false_never_encodes(self):
        """build=False 是"只读盘"契约：请求路径靠它避免 56-86s 的构建。"""
        self.embedder.encoded.clear()
        path = self.root / "claims.npz"
        self.assertIsNone(load_granular_index(self.ledger, self.embedder,
                                              path=path, build=False))
        self.assertEqual([], self.embedder.encoded,
                         "build=False 不得触发任何 encode")
        self.assertFalse(path.exists(), "build=False 不得落盘")

    def test_build_false_loads_existing_file(self):
        path = self.root / "claims.npz"
        built = load_granular_index(self.ledger, self.embedder, path=path)
        self.assertIsNotNone(built)
        self.embedder.encoded.clear()
        loaded = load_granular_index(self.ledger, self.embedder, path=path,
                                     build=False)
        self.assertIsNotNone(loaded)
        self.assertEqual(built.claim_ids, loaded.claim_ids)
        self.assertEqual([], self.embedder.encoded)

    def test_default_paths_stay_inside_hub_home(self):
        """默认路径必须落在 $HIPPOMIND_HOME 内（测试进程已把它指向临时目录）。

        这条是回归测试：曾经有一个测试调用 load_granular_index() 时不传 path，
        默认路径解析到**生产**账本的索引文件并被测试数据覆盖。断言"默认路径
        不越出 hub_home"比"每个测试自觉传 path"更可靠。
        """
        home = Path(os.environ["HIPPOMIND_HOME"]).resolve()
        self.assertTrue(str(default_index_path().resolve()).startswith(str(home)))
        self.assertTrue(str(default_claim_index_path().resolve()).startswith(str(home)))
        self.assertNotIn("Application Support", str(default_claim_index_path()))


class ClaimSearchIntegrationTests(LedgerCase):
    """ledger.search(vector_index=ClaimVectorIndex) 的融合契约。"""

    def setUp(self):
        super().setUp()
        self.dense, self.dense_claims = _ingest_claims(
            self.ledger, "纸本；SQLite；mac-mini；6000；schema v4", [
                _claim("纸本", "用途", "纸质笔记本"),
                _claim("SQLite", "用途", "本地账本存储"),
                _claim("mac-mini", "角色", "常驻主机"),
            ])
        self.redis, self.redis_claims = _ingest_claims(self.ledger, "缓存端口", [
            _claim("Redis", "端口", 6379)])
        self.index = ClaimVectorIndex.build(self.ledger, self.embedder)

    def test_claim_index_participates_in_candidate_generation(self):
        hits = self.ledger.search("纸本 SQLite mac-mini", scope="shared", limit=5,
                                  vector_index=self.index)
        self.assertTrue(hits)
        ranked = [hit for hit in hits if "vec_rank" in hit["score_parts"]]
        self.assertTrue(ranked, "claim 索引必须能生成候选")

    def test_score_parts_records_the_matched_claim(self):
        hits = self.ledger.search("纸本 SQLite mac-mini", scope="shared", limit=5,
                                  vector_index=self.index)
        recorded = [hit for hit in hits if "vec_claim" in hit["score_parts"]]
        self.assertTrue(recorded, "claim 级命中必须记下是哪条 claim")
        known = set(self.dense_claims) | set(self.redis_claims)
        for hit in recorded:
            # 记下的必须是**该记忆自己的** claim，不能串到别的记忆上。
            self.assertIn(hit["score_parts"]["vec_claim"], known)
            owner = (self.dense if hit["memory_id"] == self.dense else self.redis)
            owned = (self.dense_claims if owner == self.dense else self.redis_claims)
            self.assertIn(hit["score_parts"]["vec_claim"], owned)

    def test_claim_index_respects_scope_filter(self):
        _ingest_claims(self.ledger, "私有纸本记忆", [_claim("纸本", "私有", 1)],
                       scope="private")
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        hits = self.ledger.search("纸本", scope="shared", limit=10,
                                  vector_index=index)
        for hit in hits:
            self.assertEqual("shared", hit["scope"], "claim 索引不得绕过 scope 过滤")

    def test_claim_index_respects_evidence_gate(self):
        stripped, _ = _ingest_claims(self.ledger, "无证据纸本记忆",
                                     [_claim("纸本", "无证据", 1)])
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET evidence_json=? WHERE memory_id=?",
                         ("[]", stripped))
        index = ClaimVectorIndex.build(self.ledger, self.embedder)
        hits = self.ledger.search("纸本", scope="shared", limit=10,
                                  vector_index=index, require_evidence=True)
        for hit in hits:
            self.assertTrue(hit["evidence"], "claim 索引不得绕过证据门槛")

    def test_memory_index_has_no_vec_claim_key(self):
        """memory 级索引没有 claim 概念，不得凭空多出 vec_claim。"""
        memory_index = VectorIndex.build(self.ledger, self.embedder)
        hits = self.ledger.search("纸本 SQLite mac-mini", scope="shared", limit=5,
                                  vector_index=memory_index)
        for hit in hits:
            self.assertNotIn("vec_claim", hit["score_parts"])

    def test_default_search_still_has_no_vector_traces(self):
        hits = self.ledger.search("纸本 SQLite mac-mini", scope="shared", limit=5)
        for hit in hits:
            self.assertNotIn("vec_rank", hit["score_parts"])
            self.assertNotIn("vec_claim", hit["score_parts"])


class SemanticGateTests(unittest.TestCase):
    """`scripts/eval_semantic.py` 的**相对**门禁契约。

    第一版门禁把 `keyword recall >= 0.95` 当硬阈值，结果被账本漂移判成失败
    （HEAD 代码 + 关闭向量同样 FAIL，实测 0.935）。相对门禁的核心断言是：
    **同一次运行、同一份账本**上，pipeline 模式不得低于 lexical 基线。
    这些测试用构造出来的 report 验证门禁的判定逻辑本身，不碰账本。
    """

    @staticmethod
    def _entry(recall: float) -> Dict[str, Any]:
        return {"recall": recall, "mrr": 0.5, "p50_ms": 1.0, "p95_ms": 2.0,
                "misses": []}

    @classmethod
    def _report(cls, keyword: Dict[str, float], semantic: Dict[str, float],
                available: bool = True) -> Dict[str, Any]:
        sets = {}
        for name, modes in (("keyword", keyword), ("semantic", semantic)):
            sets[name] = {mode: cls._entry(value) for mode, value in modes.items()}
        return {"vector": {"available": available}, "sets": sets}

    @classmethod
    def _healthy(cls, **overrides) -> Dict[str, Any]:
        keyword = {"lexical": 0.935, "claim": 0.935, "memory": 0.935,
                   "fused": 0.935, "claim_raw": 0.826, "memory_raw": 0.674,
                   "fused_raw": 0.783}
        semantic = {"lexical": 0.067, "claim": 0.733, "memory": 0.533,
                    "fused": 0.667, "claim_raw": 0.467, "memory_raw": 0.200,
                    "fused_raw": 0.333}
        keyword.update(overrides.get("keyword", {}))
        semantic.update(overrides.get("semantic", {}))
        return cls._report(keyword, semantic, overrides.get("available", True))

    def test_healthy_report_passes(self):
        result = eval_semantic.gate(self._healthy())
        self.assertTrue(result["passed"], result["failures"])

    def test_low_absolute_lexical_still_passes(self):
        """账本漂移让 lexical 掉到 0.80：只要各模式不劣于它，门禁就该 PASS。

        这是本次修正的核心：绝对数字度量的是"账本长什么样"，不是"代码有没有
        退化"。把它当硬阈值会让门禁随账本漂移随机变红。
        """
        report = self._report(
            {"lexical": 0.80, "claim": 0.80, "memory": 0.80, "fused": 0.80,
             "claim_raw": 0.826, "memory_raw": 0.674, "fused_raw": 0.783},
            {"lexical": 0.067, "claim": 0.733, "memory": 0.533, "fused": 0.667,
             "claim_raw": 0.467, "memory_raw": 0.200, "fused_raw": 0.333})
        self.assertTrue(eval_semantic.gate(report)["passed"])

    def test_pipeline_below_lexical_fails(self):
        result = eval_semantic.gate(self._healthy(keyword={"claim": 0.90}))
        self.assertFalse(result["passed"])
        self.assertTrue(any("keyword/claim" in item for item in result["failures"]))

    def test_small_noise_within_tolerance_passes(self):
        result = eval_semantic.gate(self._healthy(keyword={"claim": 0.931}))
        self.assertTrue(result["passed"], result["failures"])

    def test_claim_raw_collapse_fails(self):
        """claim 索引退化成 memory 级（0.826 -> 0.674）必须被抓住。"""
        result = eval_semantic.gate(self._healthy(
            keyword={"claim_raw": 0.674}, semantic={"claim_raw": 0.200}))
        self.assertFalse(result["passed"])
        self.assertTrue(any("claim_raw" in item for item in result["failures"]))

    def test_semantic_without_gain_fails(self):
        """向量通道完全没起作用（gain == 0）必须失败，不能被 0.0 阈值放过。"""
        result = eval_semantic.gate(self._healthy(
            semantic={"claim": 0.067, "memory": 0.067, "fused": 0.067}))
        self.assertFalse(result["passed"])
        self.assertTrue(any("无提升" in item for item in result["failures"]))

    def test_vector_unavailable_fails(self):
        result = eval_semantic.gate(self._report({}, {}, available=False))
        self.assertFalse(result["passed"])

    def test_gate_reports_baselines_for_audit(self):
        result = eval_semantic.gate(self._healthy())
        self.assertEqual(0.935, result["baselines"]["keyword"])
        self.assertEqual(0.067, result["baselines"]["semantic"])


if __name__ == "__main__":
    unittest.main()
