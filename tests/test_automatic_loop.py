"""Automatic memory loop: end-to-end invariants on a temp ledger."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
from hippomind.distill import DistillEngine
from hippomind.ledger import SQLiteLedger
from hippomind.transcripts import TranscriptScanner


class AutomaticLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-loop-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _session(self, content="项目使用 SQLite，主题纸本。决定采用 WAL 模式。"):
        self.ledger.ingest({
            "content": content, "kind": "task", "source_agent": "codex",
            "scope": "shared", "project": "atlas", "task_id": "task-loop",
            "evidence": ["codex:loop"], "timestamp": "2026-10-23T10:00:00Z",
            "metadata": {"context": {"goal": ["项目使用 SQLite"],
                                     "key_decisions": ["决定采用 WAL 模式"]}},
        })

    def _write_transcript(self, sessions):
        sessions.mkdir()
        records = [
            {"type": "session_meta",
             "payload": {"session_id": "s-loop", "cwd": "/work/atlas"}},
            {"type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text",
                                      "text": "把默认主题设为纸本，数据库用 SQLite"}]},
             "timestamp": "2026-10-23T10:00:00Z"},
        ]
        body = chr(10).join(json.dumps(item, ensure_ascii=False) for item in records)
        (sessions / "rollout.jsonl").write_text(body + chr(10), encoding="utf-8")

    def test_capture_then_distill_then_retrieve(self):
        sessions = self.root / "codex"
        self._write_transcript(sessions)
        scanner = TranscriptScanner(self.ledger, roots=[sessions])
        self.assertGreaterEqual(scanner.sync(max_messages=10)["inserted"], 1)
        self._session()
        engine = DistillEngine(self.ledger, extractor=None)
        result = engine.run(limit=10)
        self.assertGreaterEqual(result["distilled"], 1)
        builder = ContextPackBuilder(self.ledger)
        pack = builder.build("默认主题 纸本", ContextPackOptions(max_items=4))
        self.assertGreaterEqual(len(pack.get("items", [])), 1)
        self.assertIn("纸本", pack.get("text") or "")

    def test_relevance_gate_rejects_unrelated_query(self):
        self._session()
        builder = ContextPackBuilder(self.ledger)
        pack = builder.build("今天天气怎么样", ContextPackOptions(max_items=4, min_score=0.2))
        self.assertEqual([], pack.get("items", []))

    def test_relevance_gate_keeps_relevant_query(self):
        self._session()
        builder = ContextPackBuilder(self.ledger)
        pack = builder.build("SQLite 数据库", ContextPackOptions(max_items=4, min_score=0.2))
        self.assertGreaterEqual(len(pack.get("items", [])), 1)


if __name__ == "__main__":
    unittest.main()
