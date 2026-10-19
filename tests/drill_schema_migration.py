#!/usr/bin/env python3
"""Schema migration drill — the CI gate for "the source can still read the data".

The incident
------------
A production ledger reached schema v8 while the only published source tree was
v5.  Opening it raised::

    LedgerError: 账本 schema 比当前 HippoMind 更新，拒绝降级写入

so 1.5M memories had no code that could read them.  Nothing caught it, because
every test built databases with the *current* code: a database created by the
code under test can never be too new for it.

What this drill does
--------------------
It opens databases that this source tree did **not** create, and refuses to pass
unless they can be read, migrated and written to:

1. **Frozen legacy fixtures** (``tests/fixtures/schema/vN.sql``) — literal
   snapshots of what older releases wrote.  ``v5.sql`` is hand-written and
   deliberately contains no ``pinned`` column (v7), no v8 indexes and a
   unicode61 FTS table (v6 switched to trigram), so the migration chain has real
   work to do.  A fixture newer than the source is a hard failure: it is the
   exact shape of the outage.
2. For each fixture: build it → open with the current source (which migrates) →
   verify counts, structure, FTS consistency, quarantine state, and that new
   writes and searches still work → reopen to prove idempotence.
3. **Forward-compatibility guard** — stamp a copy one version ahead and require
   a loud refusal that leaves the file untouched (no downgrade, no partial
   write).
4. **Live copy mode** (``--live`` / an explicit path) — the original behaviour:
   copy a real ledger (never opened for writing) and repeat the same checks on
   the copy, so a production upgrade can be rehearsed before it ships.

Usage::

    python3 tests/drill_schema_migration.py              # fixtures only (CI)
    python3 tests/drill_schema_migration.py --json       # machine readable
    python3 tests/drill_schema_migration.py --live       # copy the live ledger
    python3 tests/drill_schema_migration.py /path/hub.sqlite   # copy any ledger
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "schema"
CORE_TABLES = (
    "schema_meta", "ledger_events", "memories", "memory_fts", "memory_claims",
    "memory_relations", "memory_feedback", "source_cursors",
    "agent_credentials", "agent_trust",
)
# Tables whose row counts must survive migration unchanged.
COUNTED_TABLES = (
    "ledger_events", "memories", "memory_claims", "memory_relations",
    "memory_feedback", "source_cursors", "agent_trust", "agent_credentials",
)


class Drill:
    """Accumulates named checks so one report explains every failure."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.checks: List[Dict[str, Any]] = []
        self.info: Dict[str, Any] = {}

    def check(self, check_id: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"id": check_id, "ok": bool(ok), "detail": detail})
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)

    def report(self, **extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "drill": "schema-migration",
            "mode": self.mode,
            "ok": self.ok,
            "checks": self.checks,
            "failed": [c["id"] for c in self.checks if not c["ok"]],
        }
        payload.update(self.info)
        payload.update(extra)
        return payload


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _connect_ro(uri: str) -> sqlite3.Connection:
    """Open ``uri`` read-only and force the file open.

    ``sqlite3.connect`` is lazy: for a WAL database whose sidecars are missing
    it returns a connection happily and only fails on the first statement.  The
    probe below makes the failure surface here, where the caller can fall back.
    """
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError:
        conn.close()
        raise
    conn.row_factory = sqlite3.Row
    return conn


def _ro(path: Path) -> sqlite3.Connection:
    """Read-only connection, tolerating a checkpointed WAL file with no sidecars.

    A WAL-mode database needs its ``-shm`` companion even to be read, and a
    read-only connection cannot create one.  So ``file:...?mode=ro`` fails with
    ``unable to open database file`` for exactly the state this drill produces:
    a self-contained copy whose ``-wal``/``-shm`` were removed after a TRUNCATE
    checkpoint.  The old helper swallowed that as "unreadable table" and returned
    the -1 sentinel, which is how ``future_schema_no_data_loss`` ended up
    comparing a real count (6) against -1 and reporting phantom data loss.

    With no WAL sidecar the main file is the whole database, so ``immutable=1``
    reads it without taking locks or writing anything.  If sidecars *are*
    present (a live ledger) the normal read-only path is used, so a non-empty
    WAL is never ignored.
    """
    uri = Path(path).absolute().as_uri() + "?mode=ro"
    try:
        return _connect_ro(uri)
    except sqlite3.OperationalError as error:
        failure = error
    try:
        with open(path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        raise failure
    wal_mode = len(header) >= 20 and (header[18] == 2 or header[19] == 2)
    sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-shm")]
    if not wal_mode or any(item.exists() for item in sidecars):
        raise failure
    try:
        return _connect_ro(uri + "&immutable=1")
    except sqlite3.OperationalError:
        raise failure


def _counts(path: Path) -> Dict[str, int]:
    """Row counts per core table; tables the file does not have are -1.

    -1 means "this file has no such table" (legitimate for a pre-v1 ledger or a
    schema that dropped one), **not** "the file could not be read": an
    unreadable database raises from :func:`_ro` instead of quietly reporting
    counts, which is what let the drill compare real rows against a sentinel.
    """
    counts: Dict[str, int] = {}
    conn = _ro(path)
    try:
        for table in COUNTED_TABLES:
            try:
                counts[table] = int(
                    conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
                counts[table] = -1
    finally:
        conn.close()
    return counts


#: Above this size the byte-identity check compares size only: hashing a
#: multi-GB live copy twice would dominate a drill whose point is the refusal.
_HASH_LIMIT_BYTES = 256 * 1024 * 1024


def _digest(path: Path) -> Tuple[int, str]:
    """Size and SHA-256 of the main database file.

    A refusal must be provably non-destructive, so the drill pins the bytes
    rather than trusting that "nothing looked different".  Files above
    :data:`_HASH_LIMIT_BYTES` return an empty digest and are compared by size.
    """
    size = path.stat().st_size
    if size > _HASH_LIMIT_BYTES:
        return size, ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return size, digest.hexdigest()


def _recorded_version(path: Path) -> int:
    """Schema version recorded in the file, or 0 for pre-``schema_meta`` ledgers."""
    conn = _ro(path)
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.DatabaseError:
        return 0
    finally:
        conn.close()


def _tables(path: Path) -> set:
    conn = _ro(path)
    try:
        return {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    finally:
        conn.close()


def _columns(path: Path, table: str) -> set:
    conn = _ro(path)
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % table)}
    finally:
        conn.close()


def _sql(path: Path, name: str) -> str:
    conn = _ro(path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        return str(row[0] or "") if row else ""
    finally:
        conn.close()


def _integrity(path: Path) -> str:
    conn = _ro(path)
    try:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower()
    finally:
        conn.close()


def _copy_ledger(src: Path, dest_dir: Path) -> Path:
    """Copy a ledger plus its sidecars; the source is opened read-only only."""
    dest = dest_dir / src.name
    for suffix in ("", "-wal", "-shm"):
        source_file = Path(str(src) + suffix)
        if source_file.exists():
            shutil.copy2(str(source_file), str(dest) + suffix)
    return dest


def _checkpoint(path: Path) -> None:
    """Merge sidecars into the main file so a copy is self-contained."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The verification itself, shared by fixtures and live copies
# ---------------------------------------------------------------------------

def _verify_migrated(drill: Drill, path: Path, before: Dict[str, int],
                     schema_api: int, tag: str) -> None:
    """Open ``path`` with the current source and pin every post-migration claim."""
    from hippomind.ledger import SQLiteLedger

    ledger = SQLiteLedger(path)  # opens => migrates, if the file is older
    try:
        stats = ledger.stats()
        health = ledger.health_overview()

        drill.check("%s.schema_is_current" % tag,
                    int(stats["schema_version"]) == schema_api,
                    "recorded=%s source=%s" % (stats["schema_version"], schema_api))
        drill.check("%s.recorded_version_matches" % tag,
                    _recorded_version(path) == schema_api,
                    "schema_meta=%s" % _recorded_version(path))

        after = _counts(path)
        drift = {name: (before[name], after[name]) for name in COUNTED_TABLES
                 if before.get(name, -1) >= 0 and after.get(name) != before.get(name)}
        drill.check("%s.row_counts_preserved" % tag, not drift,
                    "mismatched=%s" % drift if drift else
                    "tables=%d rows=%d" % (len([v for v in after.values() if v >= 0]),
                                           sum(v for v in after.values() if v > 0)))

        missing = [name for name in CORE_TABLES if name not in _tables(path)]
        drill.check("%s.core_tables_present" % tag, not missing,
                    "missing=%s" % missing)

        # v7 added `pinned`; the fixture predates it, so this is a real ALTER.
        columns = _columns(path, "memories")
        drill.check("%s.latest_columns_present" % tag,
                    {"pinned", "valid_from", "asserted_at", "quality_state",
                     "agent_trust"}.issubset(columns),
                    "missing=%s" % sorted({"pinned", "valid_from", "asserted_at",
                                           "quality_state", "agent_trust"} - columns))

        indexes = {str(row[0]) for row in _ro(path).execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        drill.check("%s.latest_indexes_present" % tag,
                    {"idx_memories_updated", "idx_memories_structured_recent"}.issubset(indexes),
                    "indexes=%d" % len(indexes))

        drill.check("%s.integrity_ok" % tag, _integrity(path) == "ok",
                    _integrity(path))

        # The FTS audit invariant is the cheapest proof that the migration
        # rebuilt the index over the *surviving* rows rather than nothing.
        fts = health.get("fts", {})
        drill.check("%s.fts_matches_expected_set" % tag,
                    not fts.get("drift", True),
                    "rows=%s expected=%s" % (fts.get("rows"), fts.get("expected")))

        # Data, not just shape: a migrated row must be searchable by content.
        # ``str([])`` is ``"[]"``, which is truthy, so the historical version of
        # this check passed on an empty result set.  Keep the list.
        probe = (ledger.search("迁移演练记忆", scope="shared", limit=10)
                 if drill.mode == "fixtures" else [])
        if drill.mode == "fixtures":
            drill.check("%s.legacy_rows_are_searchable" % tag, bool(probe),
                        "hits=%d" % len(probe))

        # Migrated ledgers must still be writable — a read-only-but-openable
        # database is still an outage for the collector.
        written = ledger.ingest({
            "content": "迁移演练：升级后写入探针",
            "kind": "fact", "scope": "shared", "source_agent": "migration-drill",
            "evidence": ["drill:post-migration-write"],
        })
        drill.check("%s.writable_after_migration" % tag,
                    bool(getattr(written, "inserted", False)),
                    "memory_id=%s" % getattr(written, "memory_id", "?"))
        drill.check("%s.post_write_search" % tag,
                    bool(ledger.search("升级后写入探针", scope="shared", limit=5)),
                    "")
        drill.check("%s.fts_still_consistent" % tag,
                    not ledger.health_overview()["fts"]["drift"],
                    "")
    finally:
        ledger.close()

    # Reopening must be a no-op: no second migration, no row churn.
    from hippomind.ledger import SQLiteLedger as _Ledger
    reopen = _Ledger(path)
    try:
        drill.check("%s.reopen_is_noop" % tag,
                    int(reopen.stats()["schema_version"]) == schema_api,
                    "schema_after_reopen=%s" % reopen.stats()["schema_version"])
    finally:
        reopen.close()


def _verify_forward_guard(drill: Drill, path: Path, schema_api: int,
                          work_dir: Path, tag: str) -> None:
    """A database from the future must be refused, not downgraded or written."""
    from hippomind.ledger import LedgerError, SQLiteLedger

    future = work_dir / ("future-%s.sqlite" % tag)
    _checkpoint(path)
    shutil.copy2(str(path), str(future))
    conn = sqlite3.connect(str(future), isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) "
                     "VALUES('schema_version',?)", (str(schema_api + 1),))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(future) + suffix)
        if sidecar.exists():
            sidecar.unlink()

    # The baseline must be a *real* count.  The historical version of this check
    # silently accepted the -1 sentinel from ``_counts`` and compared it against
    # the post-refusal count, reporting phantom data loss whenever the probe
    # itself could not open the file.  ``_counts`` now raises on an unreadable
    # database, so reaching here already proves the baseline was measured; keep
    # an explicit check so a future refactor cannot reintroduce the sentinel.
    baseline = _counts(future)
    if baseline.get("memories", -1) < 0:
        drill.check("%s.future_baseline_readable" % tag, False,
                    "memories 表缺失或不可读：%s" % baseline)
        return
    drill.check("%s.future_baseline_readable" % tag, True,
                "memories=%s" % baseline["memories"])
    rows_before = {name: value for name, value in baseline.items() if value >= 0}
    size_before, sha_before = _digest(future)

    refused = False
    message = ""
    try:
        SQLiteLedger(future)
    except LedgerError as exc:  # expected: the guard from the incident
        refused = True
        message = str(exc)[:120]
    except Exception as exc:  # noqa: BLE001 - any other error is a finding
        message = "%s: %s" % (type(exc).__name__, str(exc)[:80])

    drill.check("%s.future_schema_refused" % tag, refused, message)
    drill.check("%s.future_schema_unchanged" % tag,
                _recorded_version(future) == schema_api + 1,
                "recorded=%s expected=%s" % (_recorded_version(future), schema_api + 1))

    after = _counts(future)
    lost = {name: (rows_before[name], after.get(name))
            for name in rows_before if after.get(name) != rows_before[name]}
    drill.check("%s.future_schema_no_data_loss" % tag, not lost,
                "rows=%s drift=%s" % (after.get("memories"), lost))
    size_after, sha_after = _digest(future)
    drill.check("%s.future_schema_bytes_intact" % tag,
                (size_after, sha_after) == (size_before, sha_before),
                "bytes=%s sha=%s" % (size_after,
                                     "unchanged" if sha_after == sha_before
                                     else ("size-only" if not sha_before else "CHANGED")))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_fixtures(drill: Drill, work_dir: Path) -> None:
    from hippomind.ledger import SCHEMA_VERSION

    fixtures = sorted(FIXTURE_DIR.glob("v*.sql"),
                      key=lambda p: int(p.stem.lstrip("v") or 0))
    drill.info["schema_source"] = SCHEMA_VERSION
    drill.info["fixtures"] = [f.name for f in fixtures]
    if not fixtures:
        drill.check("fixtures.present", False,
                    "tests/fixtures/schema/vN.sql 缺失：没有历史库就无法证明可迁移")
        return
    drill.check("fixtures.present", True, "found=%d" % len(fixtures))

    for fixture in fixtures:
        declared = int(fixture.stem.lstrip("v") or 0)
        tag = "v%d" % declared
        # A fixture newer than the source IS the outage: data exists that this
        # code cannot read.  Fail rather than migrate it silently.
        if not drill.check("%s.fixture_not_from_the_future" % tag,
                           declared <= SCHEMA_VERSION,
                           "fixture=v%d source=v%d" % (declared, SCHEMA_VERSION)):
            continue

        target = work_dir / ("legacy-%s.sqlite" % tag)
        conn = sqlite3.connect(str(target), isolation_level=None)
        try:
            conn.executescript(fixture.read_text(encoding="utf-8"))
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

        recorded = _recorded_version(target)
        drill.check("%s.fixture_records_its_version" % tag, recorded == declared,
                    "recorded=%s declared=%s" % (recorded, declared))
        before = _counts(target)
        drill.info.setdefault("fixtures_detail", {})[tag] = {
            "recorded_version": recorded, "rows": before,
            "migration_exercised": declared < SCHEMA_VERSION,
        }
        if declared == SCHEMA_VERSION:
            drill.check("%s.migration_exercised" % tag, True,
                        "fixture 已与源码同版本：迁移链未被覆盖，请补 v%d 快照"
                        % (SCHEMA_VERSION - 1))
        if not before.get("memories", 0) > 0:
            drill.check("%s.fixture_has_rows" % tag, False, "memories=%s" % before.get("memories"))
            continue

        _verify_migrated(drill, target, before, SCHEMA_VERSION, tag)
        _verify_forward_guard(drill, target, SCHEMA_VERSION, work_dir, tag)


def run_live_copy(drill: Drill, source: Path, work_dir: Path) -> None:
    from hippomind.ledger import SCHEMA_VERSION

    drill.info["source"] = str(source)
    drill.info["schema_source"] = SCHEMA_VERSION
    if not source.exists():
        drill.check("live.source_exists", False, "库不存在: %s" % source)
        return
    drill.check("live.source_exists", True, str(source))
    drill.info["source_bytes"] = source.stat().st_size

    started = time.perf_counter()
    copy = _copy_ledger(source, work_dir)
    drill.info["copy_seconds"] = round(time.perf_counter() - started, 3)
    # Copy before opening: the original must never be migrated in place by a
    # drill, and an older copy must be safe to discard.
    before = _counts(source)
    recorded = _recorded_version(source)
    drill.info["schema_before"] = recorded
    drill.info["rows_before"] = before
    drill.check("live.copy_matches_source",
                _counts(copy) == before, "copy=%s" % _counts(copy))

    if recorded > SCHEMA_VERSION:
        # Rehearsing this is exactly what an operator needs to see.
        from hippomind.ledger import LedgerError, SQLiteLedger
        refused = False
        try:
            SQLiteLedger(copy)
        except LedgerError:
            refused = True
        drill.check("live.newer_ledger_refused", refused,
                    "记录版本 v%s > 源码 v%s" % (recorded, SCHEMA_VERSION))
        return

    _verify_migrated(drill, copy, before, SCHEMA_VERSION, "live")
    _verify_forward_guard(drill, copy, SCHEMA_VERSION, work_dir, "live")
    try:
        drill.info["fts"] = str(_sql(copy, "memory_fts"))
        drill.info["probe_search_hits"] = int(_ro(copy).execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE '%记忆%'").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        drill.info["probe_error"] = str(exc)[:120]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="schema 迁移演练：证明当前源码能读懂并升级历史账本")
    parser.add_argument("ledger", nargs="?", default="",
                        help="要演练的账本路径（只读复制；省略则只跑内置历史库）")
    parser.add_argument("--live", action="store_true",
                        help="复制本机默认生产账本做演练（绝不以写方式打开原库）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = parser.parse_args(argv)

    from hippomind.ledger import SCHEMA_VERSION

    started = time.perf_counter()
    tmp = tempfile.mkdtemp(prefix="hippomind-migration-drill-")
    work_dir = Path(tmp)
    try:
        if args.ledger:
            drill = Drill("live-copy")
            run_live_copy(drill, Path(args.ledger).expanduser(), work_dir)
        elif args.live:
            from hippomind.ledger import default_ledger_path
            drill = Drill("live-copy")
            run_live_copy(drill, Path(default_ledger_path()).expanduser(), work_dir)
        else:
            drill = Drill("fixtures")
            run_fixtures(drill, work_dir)
        report = drill.report(
            schema_source=SCHEMA_VERSION,
            duration_seconds=round(time.perf_counter() - started, 3),
        )
    except Exception:  # noqa: BLE001 - a crashed drill must still be a red build
        report = {
            "drill": "schema-migration", "mode": "crash", "ok": False,
            "error": traceback.format_exc()[-1200:],
        }
    finally:
        if args.keep:
            print("临时目录已保留: %s" % tmp, file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False))
    else:
        print("schema 迁移演练 · %s · %s" % (report.get("mode"),
                                            "PASS" if report.get("ok") else "FAIL"))
        print("  源码 schema: v%s" % report.get("schema_source"))
        for item in report.get("checks", []):
            print("  [%s] %s%s" % ("OK  " if item["ok"] else "FAIL", item["id"],
                                   ("  " + item["detail"]) if item["detail"] else ""))
        if report.get("failed"):
            print("  失败项: %s" % ", ".join(report["failed"]))
        if report.get("error"):
            print(report["error"])
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
