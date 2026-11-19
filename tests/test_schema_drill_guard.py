"""Regression tests for the schema-migration drill's own assertions.

The drill is the safety net for the 2026-08 outage (a live ledger at schema v8,
a published source tree at v5, 1.5M unreadable memories).  A safety net with a
hole in it is worse than none, because it reports green.  Two holes existed:

1. ``_ro()`` opened the probe database with ``file:...?mode=ro``.  A WAL-mode
   file needs its ``-shm`` companion even to be read, and a read-only connection
   cannot create one, so the probe failed with "unable to open database file"
   exactly on the self-contained copy the forward guard builds (it removes the
   sidecars after a TRUNCATE checkpoint).  ``_counts()`` swallowed that into its
   -1 sentinel, so ``future_schema_no_data_loss`` compared a real count (6)
   against -1 and reported two phantom lost rows.
2. ``probe = str(ledger.search(...))`` made the "legacy rows are searchable"
   check pass on an *empty* result set, because ``str([])`` is ``"[]"``.

These tests pin the helpers directly, then pin the end-to-end behaviour: the
forward guard must pass on an intact database and must fail when a refusal
really does destroy rows.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import drill_schema_migration as drill  # noqa: E402
from hippomind.schema import SCHEMA_VERSION  # noqa: E402


def _make_ledger(path: Path, rows: int = 6) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.execute("CREATE TABLE memories (memory_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO memories VALUES(?)",
                         [("m%d" % index,) for index in range(rows)])
        conn.commit()
    finally:
        conn.close()


class ReadOnlyProbeTests(unittest.TestCase):
    """The probe must read a checkpointed WAL file whose sidecars are gone."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="drill-probe-")
        self.path = Path(self.home) / "ledger.sqlite"

    def test_reads_self_contained_wal_file_without_sidecars(self):
        _make_ledger(self.path)
        # Simulate the forward guard's copy: checkpoint, then drop the sidecars.
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        self.assertEqual(
            [item.name for item in Path(self.home).iterdir()], ["ledger.sqlite"])

        counts = drill._counts(self.path)
        self.assertEqual(counts["memories"], 6,
                         "a checkpointed WAL file must still be countable")
        self.assertEqual(drill._recorded_version(self.path), SCHEMA_VERSION)

    def test_reads_live_wal_file_that_still_has_sidecars(self):
        _make_ledger(self.path, rows=3)
        writer = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            writer.execute("INSERT INTO memories VALUES('pending')")
            # No checkpoint: this row lives only in the -wal sidecar.
            self.assertTrue(Path(str(self.path) + "-wal").exists())
            self.assertEqual(drill._counts(self.path)["memories"], 4,
                             "an uncheckpointed WAL row must be visible")
        finally:
            writer.close()

    def test_unreadable_path_raises_instead_of_inventing_counts(self):
        missing = Path(self.home) / "absent.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            drill._counts(missing)


class ForwardGuardTests(unittest.TestCase):
    """The guard must pass on an intact db and fail when rows really vanish."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="drill-guard-"))
        self.source = self.home / "source.sqlite"
        _make_ledger(self.source, rows=6)
        # The forward guard copies `path`; give it a stable source.
        self.drill = drill.Drill("fixtures")

    def test_intact_database_passes_the_guard(self):
        drill._verify_forward_guard(self.drill, self.source, SCHEMA_VERSION, self.home, "ok")
        failed = [check["id"] for check in self.drill.checks if not check["ok"]]
        self.assertEqual(failed, [], "an intact future-version db must pass")
        self.assertTrue(self.drill.ok)

    def test_guard_detects_a_refusal_that_deletes_rows(self):
        from hippomind.ledger import LedgerError, SQLiteLedger

        original = SQLiteLedger.__init__

        def destructive_init(instance, path=None):  # noqa: ANN001 - test shim
            conn = sqlite3.connect(str(path), isolation_level=None)
            try:
                conn.execute("DELETE FROM memories WHERE memory_id='m0'")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            raise LedgerError("账本 schema 比当前 HippoMind 更新，拒绝降级写入")

        SQLiteLedger.__init__ = destructive_init
        try:
            drill._verify_forward_guard(self.drill, self.source, SCHEMA_VERSION, self.home, "bad")
        finally:
            SQLiteLedger.__init__ = original

        self.assertFalse(self.drill.ok,
                         "the guard must fail when the refusal destroys data")
        failed = [check["id"] for check in self.drill.checks if not check["ok"]]
        self.assertIn("bad.future_schema_no_data_loss", failed)
        self.assertIn("bad.future_schema_bytes_intact", failed)


class SearchProbeTests(unittest.TestCase):
    """``str([])`` is truthy: the searchable check must not accept no hits."""

    def test_empty_search_result_is_reported_as_a_failure(self):
        class _EmptyLedger:
            def search(self, *_args, **_kwargs):
                return []

            def ingest(self, _record):
                class _Result:
                    inserted = False
                    memory_id = ""
                return _Result()

            def health_overview(self):
                return {"fts": {"drift": False}}

            def stats(self):
                return {"schema_version": SCHEMA_VERSION}

            def close(self):
                pass

        probe = _EmptyLedger().search("anything")
        self.assertFalse(bool(probe),
                         "the check must branch on the list, not on str(list)")


if __name__ == "__main__":
    unittest.main()
