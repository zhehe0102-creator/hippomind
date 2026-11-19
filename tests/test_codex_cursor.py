"""Codex cursor persistence: session/project/absolute line survive batching."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger
from hippomind.transcripts import TranscriptScanner


class CodexCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-codex-cursor-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.sessions = self.root / "codex" / "2026" / "11"
        self.sessions.mkdir(parents=True)
        self.path = self.sessions / "rollout-2026-11-13.jsonl"
        self._write([
            {"type": "session_meta",
             "payload": {"session_id": "session-A", "cwd": "/work/project-A"}},
            self._response("user", "实现采集链路游标", "2026-11-13T10:00:00Z"),
            self._response("assistant", "决定把游标扩展为 JSON", "2026-11-13T10:01:00Z"),
            self._response("user", "继续验证绝对行号", "2026-11-13T10:02:00Z"),
        ])
        self.scanner = TranscriptScanner(self.ledger, roots=[self.sessions])

    @staticmethod
    def _response(role, text, timestamp):
        content_type = "input_text" if role == "user" else "output_text"
        return {"type": "response_item", "timestamp": timestamp,
                "payload": {"type": "message", "role": role,
                            "content": [{"type": content_type, "text": text}]}}

    def _write(self, lines):
        self.path.write_text(
            "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
            encoding="utf-8")

    def _cursor(self):
        row = self.ledger.get_cursor(self.scanner._cursor_id("codex", self.path))
        return json.loads(row["cursor"])

    def _rows(self):
        with self.ledger._reading() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT session_id, project, evidence_json, metadata_json "
                "FROM memories WHERE kind='raw_chat' ORDER BY created_at").fetchall()]

    def test_three_batches_keep_session_project_and_absolute_lines(self):
        batches = [self.scanner.sync(max_messages=5, max_per_file=1) for _ in range(3)]
        self.assertEqual([1, 1, 1], [batch["messages"] for batch in batches])
        rows = self._rows()
        self.assertEqual(3, len(rows))
        self.assertEqual({"session-A"}, {row["session_id"] for row in rows})
        self.assertEqual({"project-A"}, {row["project"] for row in rows})
        lines = [json.loads(row["metadata_json"])["line"] for row in rows]
        self.assertEqual([2, 3, 4], lines)
        evidence = [json.loads(row["evidence_json"])[0] for row in rows]
        self.assertEqual(3, len(set(evidence)), "三条消息不得重复")
        for item, line in zip(evidence, lines):
            self.assertTrue(item.endswith(":%d" % line), item)
        cursor = self._cursor()
        self.assertEqual("session-A", cursor["session_id"])
        self.assertEqual("/work/project-A", cursor["cwd"])
        self.assertEqual(4, cursor["line"])
        fourth = self.scanner.sync(max_messages=5, max_per_file=1)
        self.assertEqual(0, fourth["messages"])
        self.assertEqual(0, fourth["inserted"])
        self.assertEqual(3, len(self._rows()))

    def test_truncated_file_resets_state_and_restarts_line_numbers(self):
        self.scanner.sync(max_messages=5, max_per_file=1)
        self.scanner.sync(max_messages=5, max_per_file=1)
        # Rewrite the rollout shorter than the persisted offset.  The scanner
        # must discard byte offset, absolute line, session and cwd, then restart
        # at byte 0 / line 0 so stale line numbers never reach evidence.
        self._write([
            {"type": "session_meta",
             "payload": {"session_id": "session-B", "cwd": "/work/project-B"}},
            self._response("user", "截断之后重新采集的新内容", "2026-11-13T11:00:00Z"),
        ])
        summary = self.scanner.sync(max_messages=5, max_per_file=1)
        self.assertGreaterEqual(summary["resets"], 1)
        self.assertEqual(1, summary["messages"])
        with self.ledger._reading() as conn:
            row = conn.execute(
                "SELECT session_id, project, metadata_json FROM memories "
                "WHERE content LIKE '%截断之后%'").fetchone()
        self.assertEqual("session-B", row["session_id"])
        self.assertEqual("project-B", row["project"])
        self.assertEqual(2, json.loads(row["metadata_json"])["line"])
        self.assertEqual(1, self._cursor()["truncated_resets"])


if __name__ == "__main__":
    unittest.main()
