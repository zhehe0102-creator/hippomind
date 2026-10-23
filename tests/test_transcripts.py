"""Native transcript capture (Claude/Codex JSONL) — incremental and bounded."""

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


class TranscriptCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-transcripts-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        claude_dir = self.root / "claude" / "project"
        codex_dir = self.root / "codex" / "2026"
        claude_dir.mkdir(parents=True)
        codex_dir.mkdir(parents=True)
        claude_lines = [
            {"type": "user", "message": {"role": "user", "content": "请记住：项目使用 SQLite"},
             "timestamp": "2026-10-23T10:00:00Z", "sessionId": "c-1", "cwd": "/work/atlas"},
            {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "决定采用 WAL 模式。"}]},
             "timestamp": "2026-10-23T10:01:00Z", "sessionId": "c-1", "cwd": "/work/atlas"},
            {"type": "attachment", "attachment": {"type": "sandbox"}},
        ]
        (claude_dir / "session.jsonl").write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in claude_lines) + "\n",
            encoding="utf-8")
        codex_lines = [
            {"type": "session_meta", "payload": {"session_id": "x-1", "cwd": "/work/codex"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "继续实现登录页"}]},
             "timestamp": "2026-10-23T11:00:00Z"},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "已完成登录页验收。"}]},
             "timestamp": "2026-10-23T11:01:00Z"},
            {"type": "event_msg", "payload": {"type": "task_started"}},
        ]
        (codex_dir / "rollout.jsonl").write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in codex_lines) + "\n",
            encoding="utf-8")
        self.scanner = TranscriptScanner(self.ledger, roots=[self.root / "claude", self.root / "codex"])

    def test_captures_both_transcript_schemas(self):
        summary = self.scanner.sync(max_messages=20)
        self.assertGreaterEqual(summary["messages"], 4)
        self.assertGreaterEqual(summary["inserted"], 4)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT source_agent,session_id,role,content,scope,raw_chat FROM memories "
                "WHERE kind='raw_chat' ORDER BY source_agent,role").fetchall()
        self.assertEqual(4, len(rows))
        self.assertTrue(all(row["raw_chat"] == 1 for row in rows))
        self.assertEqual({"claude", "codex"}, {row["source_agent"] for row in rows})
        self.assertIn("c-1", {row["session_id"] for row in rows})
        self.assertIn("x-1", {row["session_id"] for row in rows})
        self.assertTrue(all(row["scope"].startswith("agent:") for row in rows))

    def test_second_sync_is_incremental(self):
        first = self.scanner.sync(max_messages=20)
        second = self.scanner.sync(max_messages=20)
        self.assertGreater(first["messages"], 0)
        self.assertEqual(0, second["messages"])
        self.assertEqual(0, second["inserted"])

    def test_malformed_line_is_skipped(self):
        path = self.root / "claude" / "project" / "bad.jsonl"
        path.write_text("{not json}\n" + json.dumps(
            {"type": "user", "message": {"role": "user", "content": "有效内容"},
             "sessionId": "c-2"}, ensure_ascii=False) + "\n", encoding="utf-8")
        summary = self.scanner.sync(max_messages=20)
        self.assertGreaterEqual(summary["messages"], 1)


if __name__ == "__main__":
    unittest.main()
