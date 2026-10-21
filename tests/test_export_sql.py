"""export --format sql 可回放审计脚本（迭代 84）。"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger

_TARGET_SCHEMA = """
CREATE TABLE ledger_events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
 occurred_at TEXT NOT NULL, ingested_at TEXT NOT NULL, source_agent TEXT NOT NULL,
 session_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
 project TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL,
 evidence_json TEXT NOT NULL DEFAULT '[]', content TEXT NOT NULL,
 content_hash TEXT NOT NULL UNIQUE, metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE memories (memory_id TEXT PRIMARY KEY,
 origin_event_id TEXT NOT NULL UNIQUE REFERENCES ledger_events(event_id),
 kind TEXT NOT NULL, content TEXT NOT NULL, source_agent TEXT NOT NULL,
 session_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
 project TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL,
 evidence_json TEXT NOT NULL DEFAULT '[]', tags_json TEXT NOT NULL DEFAULT '[]',
 metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, last_access_at TEXT NOT NULL DEFAULT '',
 last_weighted_at TEXT NOT NULL, valid_until TEXT NOT NULL DEFAULT '',
 superseded_by TEXT NOT NULL DEFAULT '', archived_at TEXT NOT NULL DEFAULT '',
 lifecycle TEXT NOT NULL DEFAULT 'active', raw_chat INTEGER NOT NULL DEFAULT 0,
 weight REAL NOT NULL DEFAULT 0.5, importance INTEGER NOT NULL DEFAULT 5,
 touch_count INTEGER NOT NULL DEFAULT 0, utility REAL NOT NULL DEFAULT 0.0,
 confidence REAL NOT NULL DEFAULT 0.5, success_count INTEGER NOT NULL DEFAULT 0,
 failure_count INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
 -- v4 quality dimension: the dump's FTS rebuild filters on it, and the real
 -- target is documented as a *same-schema-version* empty database.  The
 -- reduced fixture predates this column and silently diverged from the
 -- schema the exporter targets.
 valid_from TEXT NOT NULL DEFAULT '', asserted_at TEXT NOT NULL DEFAULT '',
 agent_trust REAL NOT NULL DEFAULT 0.5,
 quality_score REAL NOT NULL DEFAULT 1.0,
 quality_state TEXT NOT NULL DEFAULT 'accepted');
CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, content);
"""


class ExportSqlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-sql-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src = SQLiteLedger(root / "s.sqlite")
        for i in range(5):
            self.src.ingest({"content": f"SQL 导出 {i}", "kind": "fact",
                             "source_agent": "t", "evidence": [f"t:{i}"]})
        self.dump_path = root / "dump.sql"

    def test_replay_into_empty_schema_and_fts_rebuilt(self):
        dump = self.src.export_sql()
        self.dump_path.write_text(dump)
        raw = sqlite3.connect(Path(self.tmp.name) / "t.sqlite")
        raw.executescript(_TARGET_SCHEMA)
        raw.executescript(self.dump_path.read_text())
        n_mem = raw.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        n_fts = raw.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        n_evt = raw.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
        raw.close()
        self.assertEqual((n_mem, n_fts, n_evt), (5, 5, 5))

    def test_replay_is_idempotent(self):
        self.dump_path.write_text(self.src.export_sql())
        raw = sqlite3.connect(Path(self.tmp.name) / "t.sqlite")
        raw.executescript(_TARGET_SCHEMA)
        raw.executescript(self.dump_path.read_text())
        raw.executescript(self.dump_path.read_text())  # 二次回放
        n = raw.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        raw.close()
        self.assertEqual(n, 5)

    def test_cli_sql_format_with_encryption(self):
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "export",
             "--ledger", str(Path(self.tmp.name) / "s.sqlite"),
             "--format", "sql", str(self.dump_path)],
            capture_output=True, text=True, cwd=str(ROOT))
        out = json.loads(r.stdout)
        self.assertEqual(out["exported"], 4 + 1)
        self.assertEqual(out["format"], "sql")
        enc_path = Path(self.tmp.name) / "enc.sql"
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "export",
             "--ledger", str(Path(self.tmp.name) / "s.sqlite"),
             "--format", "sql", "--encrypt", "--passphrase", "pw123",
             str(enc_path)],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertTrue(json.loads(r.stdout)["encrypted"])
        self.assertFalse(enc_path.read_bytes().startswith(b"--"))


if __name__ == "__main__":
    unittest.main()
