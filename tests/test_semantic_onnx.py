"""semantic_onnx 的回归测试：可选依赖降级、分批、缓存、指纹。

锁定四件事：

1. **无 onnxruntime 时优雅降级**：load_onnx_embedder() 返回 None，绝不抛异常。
   这是检索路径的硬契约——可选组件缺失不能让 search() 失败。
2. **必须分批 encode**：一次性把几千条交给 tokenizers 会吃掉 41GB RSS
   （实测事故）。测试用假编码器记录每次调用看到的批大小。
3. **空文本 → 零向量**：模型对空串会输出非零但无语义的池化向量，
   直接进矩阵会污染 top-k，因此必须显式置零。
4. **指纹稳定**：mtime 变化（磁盘间复制）不得让 13MB 索引白重建。

全部测试离线、不依赖模型文件、不依赖网络：假编码器注入。
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from hippomind import semantic_onnx as S


class FakeEncoder:
    """确定性的假编码器：无需 onnxruntime / 模型文件。

    把文本映射到固定维度向量（字符哈希），并记录每次 encode 的批大小，
    这样"是否分批"可以被断言，而不是靠人读代码。
    """

    def __init__(self, dim: int = 8, model_id: str = "fake-model"):
        self.dim = dim
        self.model_id = model_id
        self.max_length = 128
        self.batches = []

    def encode(self, texts):
        self.batches.append(len(texts))
        rows = []
        for text in texts:
            vector = np.zeros(self.dim, dtype=np.float32)
            for index, char in enumerate(str(text)):
                vector[index % self.dim] += (ord(char) % 17) + 1.0
            rows.append(vector)
        return np.asarray(rows, dtype=np.float32) if rows else np.zeros(
            (0, self.dim), dtype=np.float32)


def make_embedding(**kwargs):
    encoder = kwargs.pop("encoder", None) or FakeEncoder()
    return S.OnnxEmbedding(encoder, **kwargs), encoder


class OptionalDependencyTests(unittest.TestCase):
    """可选依赖缺失时必须降级，不能抛异常。"""

    def setUp(self):
        S.reset_runtime_cache()
        self.addCleanup(S.reset_runtime_cache)

    def test_missing_dependency_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            embedder = S.load_onnx_embedder("bgezh", directory=tmp,
                                            vendor=Path(tmp) / "no-vendor")
            self.assertIsNone(embedder)

    def test_available_is_false_when_dependency_missing(self):
        # 必须打桩 _import_runtime，不能只传一个不存在的 vendor 目录：
        # _import_runtime 在 vendor 缺失时**仍会尝试从 sys.path 导入**，而全量
        # 测试跑到这里时 sys.path 可能已含真实 vendor 目录（生产集成代码会加
        # 它），于是 import 成功、断言失败。断言不能依赖执行顺序或环境。
        with mock.patch.object(S, "_import_runtime",
                               side_effect=S.OnnxUnavailable("onnxruntime/tokenizers 不可用（test）")):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertFalse(S.available(Path(tmp) / "no-vendor"))

    def test_dependency_error_is_descriptive(self):
        with mock.patch.object(S, "_import_runtime",
                               side_effect=S.OnnxUnavailable("onnxruntime/tokenizers 不可用（test）")):
            with tempfile.TemporaryDirectory() as tmp:
                message = S.dependency_error(Path(tmp) / "no-vendor")
                self.assertIn("不可用", message)

    def test_runtime_cache_caches_failure(self):
        # 直接打桩 _import_runtime：真实 import 会受 sys.modules 影响（同一进程
        # 里前一个测试成功导入过 onnxruntime 后，后续 "import onnxruntime" 会
        # 从缓存命中而不再失败），那样测试就依赖执行顺序，不是可复现的断言。
        calls = []

        def boom(vendor):
            calls.append(vendor)
            raise S.OnnxUnavailable("stub unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp) / "no-vendor"
            with mock.patch.object(S, "_import_runtime", boom):
                with self.assertRaises(S.OnnxUnavailable) as first:
                    S.runtime(vendor)
                with self.assertRaises(S.OnnxUnavailable) as second:
                    S.runtime(vendor)
        self.assertIs(first.exception, second.exception,
                      "失败结果必须缓存，避免每次检索重试 sys.path 探测")
        self.assertEqual(1, len(calls), "第二次不得重新探测")

    def test_describe_never_raises_and_reports_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = S.describe("bgezh", directory=tmp)
            self.assertEqual("bgezh", info["model"])
            self.assertFalse(info["model_files"])
            self.assertIn("onnx_path", info)

    def test_model_available_checks_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(S.model_available("bgezh", directory=tmp))
            target = Path(tmp) / "bgezh"
            target.mkdir()
            (target / S.MODEL_FILENAME).write_bytes(b"x")
            self.assertFalse(S.model_available("bgezh", directory=tmp))
            (target / S.TOKENIZER_FILENAME).write_bytes(b"{}")
            self.assertTrue(S.model_available("bgezh", directory=tmp))

    def test_encoder_rejects_missing_model_without_touching_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(S.OnnxUnavailable):
                S.BertOnnxEncoder(Path(tmp) / "nope")


class PathDerivationTests(unittest.TestCase):
    """路径必须从 HIPPOMIND_HOME 推导，并支持环境变量覆盖。"""

    def test_hub_home_matches_ledger_default(self):
        from hippomind.ledger._helpers import default_hub_home
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HIPPOMIND_HOME", None)
            self.assertEqual(str(default_hub_home()), str(S.hub_home()))

    def test_env_override_for_vendor_models_and_model(self):
        env = {"HIPPOMIND_HOME": "/tmp/hm-home",
               "HIPPOMIND_VENDOR_DIR": "/tmp/hm-vendor",
               "HIPPOMIND_MODELS_DIR": "/tmp/hm-models",
               "HIPPOMIND_ONNX_MODEL": "custom"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(Path("/tmp/hm-home"), S.hub_home())
            self.assertEqual(Path("/tmp/hm-vendor"), S.vendor_dir())
            self.assertEqual(Path("/tmp/hm-models"), S.models_dir())
            self.assertEqual("custom", S.default_model())
            self.assertEqual(Path("/tmp/hm-models/custom"), S.model_path())

    def test_default_model_is_bgezh(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HIPPOMIND_ONNX_MODEL", None)
            self.assertEqual("bgezh", S.default_model())


class EmbeddingContractTests(unittest.TestCase):
    """EmbeddingAdapter 协议：归一化、确定性、空文本、cosine。"""

    def test_vectors_are_normalised_and_deterministic(self):
        embedder, _encoder = make_embedding()
        first = embedder.embed("生产环境数据库端口")
        second = embedder.embed("生产环境数据库端口")
        self.assertEqual(first, second)
        self.assertEqual(8, len(first))
        self.assertAlmostEqual(1.0, math.sqrt(sum(v * v for v in first)), places=5)

    def test_empty_text_yields_zero_vector(self):
        embedder, _encoder = make_embedding()
        for text in ("", "   ", "\t\n"):
            vector = embedder.embed(text)
            self.assertEqual(8, len(vector))
            self.assertTrue(all(value == 0.0 for value in vector), text)

    def test_cosine_guards_degenerate_input(self):
        embedder, _encoder = make_embedding()
        self.assertEqual(0.0, embedder.cosine([], [1.0]))
        self.assertEqual(0.0, embedder.cosine([1.0, 2.0], [1.0]))
        self.assertEqual(0.0, embedder.cosine([0.0] * 8, [0.0] * 8))
        self.assertAlmostEqual(1.0, embedder.cosine(embedder.embed("abc"),
                                                    embedder.embed("abc")), places=5)

    def test_embed_many_returns_python_lists(self):
        embedder, _encoder = make_embedding()
        rows = embedder.embed_many(["数据库", "", "缓存"])
        self.assertEqual(3, len(rows))
        self.assertIsInstance(rows[0], list)
        self.assertTrue(all(value == 0.0 for value in rows[1]))

    def test_encode_matrix_shape_and_zero_rows(self):
        embedder, _encoder = make_embedding()
        matrix = embedder.encode_matrix(["数据库", "", "缓存"])
        self.assertEqual((3, 8), matrix.shape)
        self.assertTrue(bool((matrix[1] == 0.0).all()))
        norms = np.linalg.norm(matrix, axis=1)
        self.assertAlmostEqual(0.0, float(norms[1]), places=6)
        self.assertAlmostEqual(1.0, float(norms[0]), places=5)

    def test_nan_from_encoder_is_cleaned(self):
        class NaNEncoder(FakeEncoder):
            def encode(self, texts):
                raw = super().encode(texts)
                raw[0, 0] = np.nan
                return raw

        embedder = S.OnnxEmbedding(NaNEncoder())
        matrix = embedder.encode_matrix(["数据库"])
        self.assertTrue(bool(np.isfinite(matrix).all()))

    def test_mismatched_encoder_shape_is_an_error(self):
        class WrongEncoder(FakeEncoder):
            def encode(self, texts):
                return np.zeros((len(texts), self.dim + 1), dtype=np.float32)

        embedder = S.OnnxEmbedding(WrongEncoder())
        with self.assertRaises(S.OnnxUnavailable):
            embedder.encode_matrix(["数据库"])


class BatchingTests(unittest.TestCase):
    """必须分批：一次性 encode 6000+ 条会吃 41GB 内存。"""

    def test_large_input_is_split_into_batches(self):
        embedder, encoder = make_embedding(batch_size=4)
        texts = ["文本-%d" % index for index in range(10)]
        matrix = embedder.encode_matrix(texts)
        self.assertEqual((10, 8), matrix.shape)
        self.assertEqual([4, 4, 2], encoder.batches)
        self.assertEqual(10, embedder.encode_items)
        self.assertEqual(3, embedder.encode_calls)

    def test_default_batch_size_is_64(self):
        embedder, encoder = make_embedding()
        self.assertEqual(64, embedder.batch_size)
        embedder.encode_matrix(["t%d" % index for index in range(130)])
        self.assertEqual([64, 64, 2], encoder.batches)

    def test_batch_size_one_is_allowed(self):
        embedder, encoder = make_embedding(batch_size=1)
        embedder.encode_matrix(["a", "b", "c"])
        self.assertEqual([1, 1, 1], encoder.batches)

    def test_empty_input_does_not_call_encoder(self):
        embedder, encoder = make_embedding()
        matrix = embedder.encode_matrix([])
        self.assertEqual((0, 8), matrix.shape)
        self.assertEqual([], encoder.batches)

    def test_duplicate_texts_are_encoded_once_per_batch(self):
        embedder, encoder = make_embedding(batch_size=64)
        matrix = embedder.encode_matrix(["同一条", "同一条", "另一条"])
        self.assertEqual([2], encoder.batches)
        self.assertTrue(bool((matrix[0] == matrix[1]).all()))


class CacheTests(unittest.TestCase):
    """LRU：缓存命中不得重复编码；容量有界。"""

    def test_repeated_embed_hits_cache(self):
        embedder, encoder = make_embedding()
        embedder.embed("数据库端口")
        calls = embedder.encode_calls
        embedder.embed("数据库端口")
        self.assertEqual(calls, embedder.encode_calls)

    def test_cache_is_bounded(self):
        embedder, _encoder = make_embedding(cache_size=2)
        for index in range(6):
            embedder.embed("文本-%d" % index)
        self.assertLessEqual(len(embedder._cache), 2)

    def test_zero_cache_disables_caching(self):
        embedder, _encoder = make_embedding(cache_size=0)
        embedder.embed("数据库端口")
        first = embedder.encode_calls
        embedder.embed("数据库端口")
        self.assertEqual(first + 1, embedder.encode_calls)

    def test_embed_many_caches(self):
        embedder, _encoder = make_embedding()
        texts = ["甲", "乙", "丙"]
        embedder.embed_many(texts)
        calls = embedder.encode_calls
        embedder.embed_many(texts)
        self.assertEqual(calls, embedder.encode_calls)


class FingerprintTests(unittest.TestCase):
    """指纹：模型/维度变了必须变，文件 mtime 变了必须不变。"""

    def test_fingerprint_is_stable_across_calls(self):
        embedder, _encoder = make_embedding()
        self.assertEqual(embedder.fingerprint(), embedder.fingerprint())

    def test_fingerprint_changes_with_model_id(self):
        first, _ = make_embedding()
        second, _ = make_embedding(encoder=FakeEncoder(model_id="other"))
        self.assertNotEqual(first.fingerprint(), second.fingerprint())

    def test_fingerprint_changes_with_dim(self):
        first, _ = make_embedding()
        second, _ = make_embedding(encoder=FakeEncoder(dim=16))
        self.assertNotEqual(first.fingerprint(), second.fingerprint())

    def test_fingerprint_changes_with_name(self):
        first, _ = make_embedding(name="bgezh")
        second, _ = make_embedding(name="minilm")
        self.assertNotEqual(first.fingerprint(), second.fingerprint())

    def test_model_id_ignores_mtime_but_tracks_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "bgezh"
            directory.mkdir()
            onnx = directory / S.MODEL_FILENAME
            tokenizer = directory / S.TOKENIZER_FILENAME
            onnx.write_bytes(b"onnx-bytes")
            tokenizer.write_bytes(b"tok")
            (directory / S.CONFIG_FILENAME).write_text("{}", encoding="utf-8")
            first = S._model_id(directory, onnx, tokenizer, 512, 256)
            # 模拟"复制到另一块盘"：只改 mtime，内容不变。
            os.utime(onnx, (0, 0))
            os.utime(tokenizer, (0, 0))
            second = S._model_id(directory, onnx, tokenizer, 512, 256)
            self.assertEqual(first, second)
            # 换内容（大小变化）必须换指纹。
            onnx.write_bytes(b"onnx-bytes-longer")
            third = S._model_id(directory, onnx, tokenizer, 512, 256)
            self.assertNotEqual(first, third)

    def test_params_report_effective_values(self):
        embedder, _encoder = make_embedding(batch_size=32, cache_size=16)
        params = embedder.params()
        self.assertEqual(8, params["dim"])
        self.assertEqual(32, params["batch_size"])
        self.assertEqual(16, params["cache_size"])
        self.assertEqual("mean", params["pooling"])
        self.assertEqual(embedder.fingerprint(), params["fingerprint"])


class AdapterProtocolTests(unittest.TestCase):
    """必须能被现有调用方当作 EmbeddingAdapter 使用。"""

    def test_satisfies_adapter_protocol(self):
        embedder, _encoder = make_embedding()
        self.assertTrue(callable(embedder.embed))
        self.assertTrue(callable(embedder.cosine))
        self.assertIsInstance(embedder.embed("x"), list)

    def test_works_with_hybrid_ranker(self):
        from hippomind.embeddings import HybridRanker
        embedder, _encoder = make_embedding()
        rows = [
            {"memory_id": "a", "content": "数据库端口 5432", "score": 0.9,
             "score_parts": {"fts": 0.9, "coverage": 0.9, "topic": 0.5,
                             "phrase": True, "time": 0.9}},
            {"memory_id": "b", "content": "爵士乐即兴演奏", "score": 0.1,
             "score_parts": {"fts": 0.0, "coverage": 0.0, "topic": 0.0,
                             "time": 0.1}},
        ]
        ranked = HybridRanker(embedder).rank("数据库端口", rows)
        self.assertEqual(sorted(row["memory_id"] for row in ranked), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
