"""Ledger-native LSA semantic space: build, persist and reuse."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger
from hippomind.semantic_space import (SemanticSpaceEmbedding, build, save)


class SemanticSpaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-lsa-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _seed(self, count=40):
        topics = [
            "部署 上线 发布 服务器 网关",
            "数据库 SQLite WAL 事务 并发",
            "界面 主题 纸本 排版 视觉",
            "记忆 检索 召回 向量 语义",
        ]
        for index in range(count):
            self.ledger.ingest({
                "content": topics[index % len(topics)] + " 记录 %d" % index,
                "kind": "semantic", "source_agent": "t", "scope": "shared",
                "evidence": ["t:%d" % index],
                "timestamp": "2026-10-23T10:00:00Z",
            })

    def test_build_creates_term_vectors(self):
        self._seed()
        model = build(self.ledger, dim=16)
        self.assertGreater(len(model["terms"]), 10)
        self.assertEqual(16, model["dim"])
        self.assertEqual(16, len(next(iter(model["vectors"].values()))))

    def test_similar_topics_are_closer_than_unrelated(self):
        self._seed()
        embedder = SemanticSpaceEmbedding(build(self.ledger, dim=32))
        deploy = embedder.embed("部署 上线 服务器")
        release = embedder.embed("发布 网关 部署")
        ui = embedder.embed("界面 主题 排版")
        self.assertGreater(embedder.cosine(deploy, release),
                           embedder.cosine(deploy, ui))

    def test_save_and_load_roundtrip(self):
        self._seed()
        model = build(self.ledger, dim=16)
        path = Path(self.tmp.name) / "semantic_space.json"
        save(model, path)
        loaded = SemanticSpaceEmbedding.load(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(16, loaded.dim)
        self.assertTrue(loaded.embed("部署 上线"))

    def test_missing_model_returns_none(self):
        self.assertIsNone(SemanticSpaceEmbedding.load(Path(self.tmp.name) / "missing.json"))


if __name__ == "__main__":
    unittest.main()
