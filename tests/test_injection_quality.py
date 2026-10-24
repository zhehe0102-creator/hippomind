"""Injection reuse proxy: traces with ids are scored against later replies."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.injection_quality import evaluate
from hippomind.ledger import SQLiteLedger


class InjectionQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-injq-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

    def _memory(self, content):
        return self.ledger.ingest({
            "content": content, "kind": "semantic", "source_agent": "codex",
            "scope": "shared", "evidence": ["codex:1"],
            "timestamp": self.now.isoformat(),
        }).memory_id

    def _reply(self, content, minutes=5):
        self.ledger.ingest({
            "content": content, "kind": "raw_chat", "source_agent": "codex",
            "scope": "agent:codex", "raw_chat": True, "role": "assistant",
            "session_id": "s1", "evidence": ["codex:r"],
            "timestamp": (self.now + timedelta(minutes=minutes)).isoformat(),
        })

    def test_overlap_marks_reuse(self):
        memory_id = self._memory("默认主题使用纸本，数据库使用 SQLite WAL")
        self._reply("我会把默认主题设为纸本，并用 SQLite WAL 存储")
        result = evaluate(self.ledger, [{
            "at": self.now.isoformat(), "agent": "codex", "memory_ids": [memory_id],
        }])
        self.assertEqual(1, result["traced_with_ids"])
        self.assertEqual(1, result["sampled"])
        self.assertEqual(1, result["reused"])
        self.assertGreaterEqual(result["reuse_rate"], 0.5)

    def test_unrelated_reply_is_not_reuse(self):
        memory_id = self._memory("默认主题使用纸本，数据库使用 SQLite WAL")
        self._reply("今天先休息一下，明天再看")
        result = evaluate(self.ledger, [{
            "at": self.now.isoformat(), "agent": "codex", "memory_ids": [memory_id],
        }])
        self.assertEqual(0, result["reused"])
        self.assertEqual(0.0, result["reuse_rate"])

    def test_trace_without_ids_is_ignored(self):
        result = evaluate(self.ledger, [{"at": self.now.isoformat(), "agent": "codex"}])
        self.assertEqual(0, result["traced_with_ids"])
        self.assertIsNone(result["reuse_rate"])


if __name__ == "__main__":
    unittest.main()
