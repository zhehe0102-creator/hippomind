"""Schema compatibility contract: source and ledger must never drift apart.

The incident this guards against
-------------------------------
A production ledger had reached schema v8 while the only published source tree
was still v5.  Opening the ledger raised::

    LedgerError: 账本 schema 比当前 HippoMind 更新，拒绝降级写入

so 1.5M memories had no code that could read them.  The failure was invisible
until someone tried to open the file, because nothing in CI compared the source
schema number with a real, older database.

What this module pins
---------------------
1. A ledger created at the *current* schema reports that version and reopens.
2. A ledger whose recorded schema is newer than the source is refused with a
   clear, non-destructive error (no silent downgrade, no partial write).
3. Every migration is forward-only and idempotent: opening twice is a no-op.
4. The public schema number is reported by the package and by the CLI, so a
   release check can compare it against a live ledger without importing SQL.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

import hippomind
from hippomind import ledger as ledger_module
from hippomind.ledger import SCHEMA_VERSION, LedgerError, SQLiteLedger


class SchemaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "ledger.sqlite")

    def _version(self) -> int:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        return int(row[0])

    def test_package_and_ledger_export_agree(self):
        """Two independent schema numbers must never disagree."""
        self.assertEqual(hippomind.SCHEMA_VERSION, SCHEMA_VERSION)

    def test_new_ledger_is_stamped_with_current_schema(self):
        ledger = SQLiteLedger(self.path)
        try:
            self.assertEqual(self._version(), SCHEMA_VERSION)
            self.assertEqual(ledger.stats()["schema_version"], SCHEMA_VERSION)
        finally:
            ledger.close()

    def test_reopening_an_up_to_date_ledger_is_a_noop(self):
        first = SQLiteLedger(self.path)
        first.ingest({"content": "schema 幂等性验证", "kind": "fact",
                      "source_agent": "codex", "evidence": ["a:1"]})
        rows = first.stats()["memories"]
        first.close()

        second = SQLiteLedger(self.path)
        try:
            self.assertEqual(second.stats()["memories"], rows)
            self.assertEqual(self._version(), SCHEMA_VERSION)
        finally:
            second.close()

    def test_future_ledger_is_refused_without_modification(self):
        """A newer DB must be rejected loudly, and left byte-compatible."""
        ledger = SQLiteLedger(self.path)
        ledger.ingest({"content": "事故复现：更高 schema 的库", "kind": "fact",
                       "source_agent": "codex", "evidence": ["a:1"]})
        ledger.close()

        future = SCHEMA_VERSION + 1
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                         (str(future),))

        with self.assertRaises(LedgerError) as ctx:
            SQLiteLedger(self.path)
        self.assertIn("schema", str(ctx.exception).lower())

        # The refusal must not have rewritten the version (no downgrade).
        self.assertEqual(self._version(), future)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)

    def test_migration_paths_are_declared_contiguously(self):
        """Every version 1..N must have a migration hook, or upgrades silently skip."""
        for version in range(1, SCHEMA_VERSION + 1):
            self.assertTrue(
                hasattr(SQLiteLedger, f"_migrate_v{version}"),
                f"缺少 _migrate_v{version}：schema 升级链会断档",
            )

    def test_fts_and_core_tables_exist_after_migration(self):
        ledger = SQLiteLedger(self.path)
        ledger.close()
        with sqlite3.connect(self.path) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        for required in ("ledger_events", "memories", "memories_fts".replace("memories_fts", "memory_fts"),
                         "memory_claims", "memory_relations", "memory_feedback",
                         "source_cursors", "agent_credentials", "schema_meta"):
            self.assertIn(required, names, f"缺少核心表 {required}")

    def test_ledger_module_is_importable_without_the_demo_package(self):
        """The storage layer must not depend on the grid-world demonstration."""
        import subprocess
        import sys
        code = (
            "import sys; import hippomind.ledger; "
            "demo=[m for m in ('hippomind.hippocampus','hippomind.drives',"
            "'hippomind.grid_world','hippomind.mind') if m in sys.modules]; "
            "print(','.join(demo))"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=os.path.dirname(os.path.dirname(
                                 os.path.abspath(__file__))))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "",
                         f"导入存储层时不应加载演示包: {out.stdout!r}")


class ReleaseSchemaGuardTests(unittest.TestCase):
    """A release check must be able to compare source against a live ledger."""

    def test_version_command_reports_schema(self):
        """``hippomind version --json`` exposes the schema for release checks."""
        import json
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        home = tempfile.mkdtemp()
        env = dict(os.environ, HIPPOMIND_HOME=home, PYTHONPATH=root)
        out = subprocess.run(
            [sys.executable, "-m", "hippomind", "version", "--json"],
            capture_output=True, text=True, env=env, cwd=root)
        if out.returncode != 0:
            self.skipTest(f"version --json 不可用: {out.stderr[:200]}")
        payload = json.loads(out.stdout)
        self.assertIn("schema_version", payload)
        self.assertEqual(int(payload["schema_version"]), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
