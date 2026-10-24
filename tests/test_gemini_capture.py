"""Gemini/Antigravity conversation summary capture."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.gemini_capture import GeminiSummaryScanner
from hippomind.ledger import SQLiteLedger


class GeminiCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-gemini-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.db = self.root / "conversation_summaries.db"
        conn = sqlite3.connect(self.db)
        conn.execute("""CREATE TABLE conversation_summaries(
            conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT,
            step_count INTEGER, last_modified_time TEXT, workspace_uris TEXT,
            status TEXT, project_id TEXT, agent_name TEXT)""")
        conn.execute("INSERT INTO conversation_summaries VALUES(?,?,?,?,?,?,?,?,?)",
                     ("c-1", "AegisEye 传感器", "请编写传感器层", 8,
                      "2026-10-23 10:00:00.09725+00:00", "", "IDLE", "aegis-eye", "gemini"))
        conn.execute("INSERT INTO conversation_summaries VALUES(?,?,?,?,?,?,?,?,?)",
                     ("c-2", "RelayDesk", "修复模型路由", 3,
                      "2026-10-23T11:00:00Z", "", "IDLE", "relaydesk", "gemini"))
        conn.commit()
        conn.close()
        self.scanner = GeminiSummaryScanner(self.ledger, databases=[self.db])

    def test_capture_and_incremental(self):
        first = self.scanner.sync(max_rows=10)
        self.assertEqual(first["rows"], 2)
        self.assertEqual(first["inserted"], 2)
        with self.ledger._reading() as conn:
            rows = conn.execute("SELECT source_agent,kind,task_id,content FROM memories").fetchall()
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row["source_agent"] == "gemini" for row in rows))
        self.assertTrue(all(row["kind"] == "task" for row in rows))
        second = self.scanner.sync(max_rows=10)
        self.assertEqual(0, second["rows"])

    def test_new_row_is_captured(self):
        self.scanner.sync(max_rows=10)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO conversation_summaries VALUES(?,?,?,?,?,?,?,?,?)",
                     ("c-3", "新任务", "继续验收", 1, "2026-10-23T12:00:00Z", "", "IDLE", "x", "gemini"))
        conn.commit()
        conn.close()
        result = self.scanner.sync(max_rows=10)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["inserted"], 1)


if __name__ == "__main__":
    unittest.main()
