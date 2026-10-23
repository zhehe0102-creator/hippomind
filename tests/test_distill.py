"""Automatic distillation: evidence -> claims, idempotent and conflict-aware."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.distill import DistillEngine
from hippomind.ledger import SQLiteLedger


class DistillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-distill-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _seed(self):
        self.ledger.ingest({
            "content": "任务：把 GUI 主题默认设为纸本",
            "kind": "task", "source_agent": "codex", "scope": "shared",
            "project": "atlas", "task_id": "task-1",
            "evidence": ["codex:1"], "timestamp": "2026-10-23T10:00:00Z",
            "metadata": {"context": {"goal": ["把 GUI 主题默认设为纸本"],
                                     "key_decisions": ["决定使用纸本作为默认主题"]}},
        })
        self.ledger.ingest({
            "content": "决定采用 SQLite + WAL；数据库使用 SQLite。",
            "kind": "raw_chat", "source_agent": "claude", "scope": "agent:claude",
            "raw_chat": True, "session_id": "s-1", "project": "atlas",
            "evidence": ["claude:1"], "timestamp": "2026-10-23T10:01:00Z",
        })

    def test_heuristic_distillation_creates_claims_and_is_idempotent(self):
        self._seed()
        engine = DistillEngine(self.ledger, extractor=None)
        first = engine.run(limit=10)
        self.assertGreaterEqual(first["distilled"], 1)
        self.assertGreaterEqual(first["claims"], 2)
        with self.ledger._reading() as conn:
            distilled = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_agent='hippomind-distill'").fetchone()[0]
            claims = conn.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0]
        self.assertGreaterEqual(distilled, 1)
        self.assertGreaterEqual(claims, 2)
        second = engine.run(limit=10)
        self.assertEqual(0, second["candidates"])

    def test_dry_run_does_not_write(self):
        self._seed()
        engine = DistillEngine(self.ledger, extractor=None)
        result = engine.run(limit=10, dry_run=True)
        self.assertGreaterEqual(result["claims"], 1)
        with self.ledger._reading() as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_agent='hippomind-distill'").fetchone()[0])

    def test_conflict_resolution_is_bounded(self):
        self._seed()
        engine = DistillEngine(self.ledger, extractor=None)
        resolved = engine.resolve_conflicts(limit=10)
        self.assertIsInstance(resolved, int)
        self.assertGreaterEqual(resolved, 0)


if __name__ == "__main__":
    unittest.main()
