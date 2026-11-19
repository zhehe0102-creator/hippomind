"""Session digest freshness: new messages requeue and supersede the old digest."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger
from hippomind.session_digest import (
    CURSOR_PREFIX,
    MAX_MESSAGES_PER_SESSION,
    SessionDigester,
)


class SessionDigestFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-digest-fresh-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _seed(self, index, content=None):
        self.ledger.ingest({
            "content": content or ("用户要求实现功能 %d" % index),
            "kind": "raw_chat", "source_agent": "codex",
            "scope": "agent:codex", "raw_chat": True,
            "session_id": "session-A", "project": "project-A",
            "task_id": "task-A",
            "evidence": ["codex:session-A:%d" % index],
            "timestamp": "2026-11-13T10:%02d:00Z" % index,
        })

    def _cursor(self):
        row = self.ledger.get_cursor(CURSOR_PREFIX + "session-A")
        return json.loads(row["cursor"])

    def _digest_rows(self):
        with self.ledger._reading() as conn:
            return {str(row["memory_id"]): dict(row) for row in conn.execute(
                "SELECT memory_id, lifecycle, superseded_by FROM memories "
                "WHERE source_agent='hippomind-digest'")}

    def test_new_message_requeues_and_supersedes_previous_digest(self):
        for index in range(4):
            content = None
            if index == 2:
                content = "决定采用 SQLite WAL，因为并发写更稳定"
            elif index == 3:
                content = "已完成验收，测试通过"
            self._seed(index, content)
        digester = SessionDigester(self.ledger, extractor=None)
        first = digester.run(limit=5)
        self.assertEqual(1, first["digested"])
        self.assertEqual(0, first["superseded"])
        self.assertEqual(0, len(digester.pending_sessions(limit=5)))
        cursor = self._cursor()
        self.assertEqual(4, cursor["messages"])
        self.assertTrue(cursor["last_at"])
        old_last_at = cursor["last_at"]
        old_id = cursor["memory_id"]
        self.assertTrue(old_id)

        self._seed(4, "决定下一步补齐采集链路的新消息")
        pending = digester.pending_sessions(limit=5)
        self.assertEqual(1, len(pending), "会话新增消息后必须重新进入队列")
        self.assertEqual(5, pending[0]["n"])

        second = digester.run(limit=5)
        self.assertEqual(1, second["digested"])
        self.assertEqual(1, second["superseded"])
        cursor = self._cursor()
        self.assertEqual(5, cursor["messages"])
        self.assertGreater(cursor["last_at"], old_last_at)
        new_id = cursor["memory_id"]
        self.assertNotEqual(old_id, new_id)

        rows = self._digest_rows()
        self.assertEqual("superseded", rows[old_id]["lifecycle"])
        self.assertEqual(new_id, rows[old_id]["superseded_by"])
        self.assertEqual("active", rows[new_id]["lifecycle"])
        self.assertEqual(0, len(digester.pending_sessions(limit=5)))

    def test_long_session_digests_the_tail_and_stops_requeueing(self):
        rows = []
        for index in range(MAX_MESSAGES_PER_SESSION + 5):
            content = ("历史消息 %d" % index if index < MAX_MESSAGES_PER_SESSION + 4
                       else "最新收尾：决定上线")
            rows.append({
                "content": content, "kind": "raw_chat", "source_agent": "codex",
                "scope": "agent:codex", "raw_chat": True,
                "session_id": "long-session", "project": "project-L",
                "evidence": ["codex:long-session:%d" % index],
                "timestamp": "2026-11-13T%02d:%02d:%02dZ"
                             % (10 + index // 3600, (index // 60) % 60, index % 60),
            })
        self.ledger.ingest_many(rows)
        digester = SessionDigester(self.ledger, extractor=None)
        messages = digester._messages("long-session")
        self.assertEqual(MAX_MESSAGES_PER_SESSION, len(messages))
        self.assertIn("最新收尾", messages[-1]["content"],
                      "窗口必须取尾部，否则新消息永远进不了摘要")
        self.assertEqual(1, digester.run(limit=5)["digested"])
        self.assertEqual(0, len(digester.pending_sessions(limit=5)),
                         "超过窗口长度的会话不得在每轮都被重新排队")

    def test_dry_run_does_not_advance_the_freshness_cursor(self):
        for index in range(4):
            self._seed(index)
        digester = SessionDigester(self.ledger, extractor=None)
        result = digester.run(limit=5, dry_run=True)
        self.assertEqual(1, result["digested"])
        self.assertEqual({}, digester._parse_cursor(
            self.ledger.get_cursor(CURSOR_PREFIX + "session-A")["cursor"]))
        self.assertEqual(1, len(digester.pending_sessions(limit=5)))


if __name__ == "__main__":
    unittest.main()
