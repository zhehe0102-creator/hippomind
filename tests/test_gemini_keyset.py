"""Gemini capture uses a compound (last_modified_time, conversation_id) keyset."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.gemini_capture import GeminiSummaryScanner
from hippomind.ledger import SQLiteLedger

SAME_TS = "2026-11-13T10:00:00Z"


class GeminiKeysetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-gemini-keyset-")
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
        rows = [
            ("c-1", "第三页工具链", "同秒写入的第一条", 3, SAME_TS, "", "IDLE", "p-1", "gemini"),
            ("c-2", "第四页工具链", "同秒写入的第二条", 4, SAME_TS, "", "IDLE", "p-2", "gemini"),
            ("c-3", "第五页工具链", "同秒写入的第三条", 5, SAME_TS, "", "IDLE", "p-3", "gemini"),
        ]
        conn.executemany("INSERT INTO conversation_summaries VALUES(?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        self.scanner = GeminiSummaryScanner(self.ledger, databases=[self.db])

    def _memory_count(self):
        with self.ledger._reading() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_agent='gemini'").fetchone()[0])

    def _cursor(self):
        row = self.ledger.get_cursor(self.scanner._cursor_id(self.db))
        return json.loads(row["cursor"])

    def test_same_timestamp_rows_are_all_captured_across_pages(self):
        first = self.scanner.sync(max_rows=2)
        self.assertEqual(2, first["rows"])
        self.assertEqual(2, first["inserted"])
        self.assertEqual(2, self._memory_count())
        second = self.scanner.sync(max_rows=2)
        self.assertEqual(1, second["rows"], "同时间戳的第三条不能被 keyset 跳过")
        self.assertEqual(1, second["inserted"])
        self.assertEqual(3, self._memory_count())
        third = self.scanner.sync(max_rows=2)
        self.assertEqual(0, third["rows"], "重复 sync 不得再取到任何行")
        self.assertEqual(0, third["inserted"])
        self.assertEqual(3, self._memory_count())
        cursor = self._cursor()
        self.assertEqual(SAME_TS, cursor["last_modified_time"])
        self.assertEqual("c-3", cursor["conversation_id"])

    def test_legacy_plain_timestamp_cursor_is_still_understood(self):
        self.scanner.sync(max_rows=10)
        self.assertEqual(3, self._memory_count())
        cursor_id = self.scanner._cursor_id(self.db)
        # Old releases stored the bare timestamp string.  Reading it must keep
        # the same-timestamp siblings reachable; the ledger dedupes the replay.
        self.ledger.set_cursor(cursor_id, SAME_TS)
        replay = self.scanner.sync(max_rows=10)
        self.assertEqual(3, replay["rows"])
        self.assertEqual(0, replay["inserted"])
        self.assertEqual(3, replay["duplicate"])
        self.assertEqual(3, self._memory_count())
        final = self.scanner.sync(max_rows=10)
        self.assertEqual(0, final["rows"])


if __name__ == "__main__":
    unittest.main()
