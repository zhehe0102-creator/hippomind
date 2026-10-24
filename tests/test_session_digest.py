"""Session digest: whole-session summary instead of per-message distillation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger
from hippomind.session_digest import SessionDigester


class SessionDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-digest-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _seed_session(self, session_id="s-1", count=6):
        for index in range(count):
            content = ("用户要求实现功能 %d" % index)
            if index == 2:
                content = "决定采用 SQLite WAL，因为并发写更稳定"
            if index == count - 1:
                content = "已完成验收，测试通过"
            self.ledger.ingest({
                "content": content, "kind": "raw_chat", "source_agent": "codex",
                "scope": "agent:codex", "raw_chat": True, "session_id": session_id,
                "project": "atlas", "task_id": "task-" + session_id,
                "evidence": ["codex:%s:%d" % (session_id, index)],
                "timestamp": "2026-10-23T10:%02d:00Z" % index,
            })

    def test_digest_contains_decision_and_tail(self):
        self._seed_session()
        digester = SessionDigester(self.ledger, extractor=None)
        sessions = digester.pending_sessions(limit=5)
        self.assertEqual(1, len(sessions))
        messages = digester._messages("s-1")
        digest = digester.build_digest(messages)
        self.assertIn("开场请求", digest)
        self.assertIn("SQLite WAL", digest)
        self.assertIn("收尾", digest)
        self.assertLessEqual(len(digest), 6_000)

    def test_run_creates_one_memory_per_session_and_is_idempotent(self):
        self._seed_session()
        digester = SessionDigester(self.ledger, extractor=None)
        first = digester.run(limit=5)
        self.assertEqual(1, first["digested"])
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT content, kind, source_agent FROM memories "
                "WHERE source_agent='hippomind-digest'").fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("semantic", rows[0]["kind"])
        self.assertIn("会话摘要", rows[0]["content"])
        second = digester.run(limit=5)
        self.assertEqual(0, second["sessions"])
        self.assertEqual(1, digester.status()["digested"])

    def test_small_sessions_are_skipped_by_threshold(self):
        self._seed_session(count=2)
        digester = SessionDigester(self.ledger, extractor=None)
        self.assertEqual(0, len(digester.pending_sessions(limit=5, min_messages=4)))
        self.assertEqual(1, len(digester.pending_sessions(limit=5, min_messages=2)))

    def test_dry_run_writes_nothing(self):
        self._seed_session()
        digester = SessionDigester(self.ledger, extractor=None)
        result = digester.run(limit=5, dry_run=True)
        self.assertEqual(1, result["digested"])
        with self.ledger._reading() as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_agent='hippomind-digest'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
