"""Distillation inherits the source scope instead of promoting it to shared."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.distill import DistillEngine
from hippomind.ledger import SQLiteLedger


class DistillScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-distill-scope-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _distilled(self):
        with self.ledger._reading() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT memory_id, scope, metadata_json FROM memories "
                "WHERE source_agent='hippomind-distill'").fetchall()]

    def test_private_raw_scope_is_inherited_and_flagged(self):
        raw = self.ledger.ingest({
            "content": "决定采用 SQLite WAL 作为默认存储，并保留双写日志。",
            "kind": "raw_chat", "source_agent": "claude", "scope": "agent:claude",
            "raw_chat": True, "session_id": "session-A", "project": "project-A",
            "task_id": "task-A", "evidence": ["claude:session-A:1"],
            "timestamp": "2026-11-13T10:00:00Z",
        })
        engine = DistillEngine(self.ledger, extractor=None)
        summary = engine.run(limit=10)
        self.assertGreaterEqual(summary["distilled"], 1)
        rows = self._distilled()
        self.assertEqual(1, len(rows))
        self.assertEqual("agent:claude", rows[0]["scope"], "提炼不得把私有 raw 提升为 shared")
        self.assertNotEqual("shared", rows[0]["scope"])
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertIs(True, metadata["derived_from_raw_chat"])
        self.assertIs(True, metadata["untrusted_source"])
        self.assertEqual("agent:claude", metadata["source_scope"])
        self.assertIn(raw.memory_id, metadata["source_memory_ids"])

    def test_unknown_source_scope_falls_back_to_shared(self):
        self.ledger.ingest({
            "content": "任务：把 GUI 主题默认设为纸本",
            "kind": "task", "source_agent": "codex", "scope": "shared",
            "project": "atlas", "task_id": "task-1", "evidence": ["codex:1"],
            "timestamp": "2026-11-13T10:01:00Z",
            "metadata": {"context": {"goal": ["把 GUI 主题默认设为纸本"]}},
        })
        engine = DistillEngine(self.ledger, extractor=None)
        engine.run(limit=10)
        rows = self._distilled()
        self.assertEqual(1, len(rows))
        self.assertEqual("shared", rows[0]["scope"])
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertIs(False, metadata["derived_from_raw_chat"])
        self.assertIs(False, metadata["untrusted_source"])
        self.assertEqual("shared", metadata["source_scope"])


if __name__ == "__main__":
    unittest.main()
