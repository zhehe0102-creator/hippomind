"""记忆导出/导入测试：跨账本迁移、幂等重导入、raw 隔离。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hippomind.ledger import SQLiteLedger


def _seed(ledger: SQLiteLedger) -> None:
    ledger.ingest({
        "kind": "fact", "scope": "shared", "source_agent": "tester",
        "content": "生产数据库端口 6432，已完成迁移",
        "evidence": ["evidence:port"], "confidence": 0.9,
        "subject": "db.prod", "predicate": "config", "slot_key": "port",
        "value": {"port": "6432"},
    })
    ledger.ingest({
        "kind": "progress", "scope": "shared", "source_agent": "tester",
        "content": "完成部署流水线优化", "evidence": ["evidence:deploy"],
        "context": {"progress": ["流水线提速 40%"], "next_steps": ["灰度验证"]},
        "tags": ["deploy"],
    })
    ledger.ingest({
        "kind": "raw_chat", "raw_chat": True, "role": "user",
        "scope": "shared", "source_agent": "tester",
        "content": "原始聊天证据不应默认导出", "evidence": ["evidence:raw"],
    })


class ExportImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = SQLiteLedger(Path(self.tmp.name) / "a.sqlite")
        _seed(self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_preserves_recall_and_claims(self):
        rows = self.source.export_rows(scope="shared")
        # raw chat 默认不导出
        self.assertEqual(len(rows), 2)
        target = SQLiteLedger(Path(self.tmp.name) / "b.sqlite")
        result = target.ingest_many(rows)
        self.assertEqual(sum(1 for r in result if r.inserted), 2)

        hits = target.search("生产数据库端口", scope="shared", limit=8)
        joined = " ".join(h["content"] for h in hits)
        self.assertIn("6432", joined)
        # claim 槽位随导出重建：新值可被检索、旧槽位结构保留
        claims = target.list_claims(scope="shared")
        self.assertTrue(any(c["subject"] == "db.prod" for c in claims))

        pack_rows = target.search("部署流水线优化", scope="shared", limit=8)
        self.assertTrue(any("流水线" in h["content"] for h in pack_rows))

    def test_reimport_is_idempotent(self):
        rows = self.source.export_rows(scope="shared")
        target = SQLiteLedger(Path(self.tmp.name) / "b.sqlite")
        first = target.ingest_many(rows)
        second = target.ingest_many(rows)
        self.assertEqual(sum(1 for r in first if r.inserted), 2)
        self.assertEqual(sum(1 for r in second if r.inserted), 0)

    def test_raw_chat_export_requires_explicit_flag(self):
        rows = self.source.export_rows(scope="shared", include_raw_chat=True)
        self.assertEqual(len(rows), 3)
        self.assertTrue(any(r["kind"] == "raw_chat" for r in rows))
        # 导入端 raw 行保持 raw_chat 标记（不可信隔离不因迁移丢失）
        target = SQLiteLedger(Path(self.tmp.name) / "c.sqlite")
        target.ingest_many([r for r in rows if r["kind"] == "raw_chat"])
        stats = target.stats()
        self.assertGreaterEqual(stats["raw_chat"], 1)

    def test_limit_bounds_export(self):
        rows = self.source.export_rows(scope="shared", limit=1)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
