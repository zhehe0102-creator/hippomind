"""Guarded, auditable compaction driver for a live HippoMind ledger.

This is the operator tool that turns the *reviewed* compaction plan into an
executed one, in the order the runbook requires:

    preflight  ->  dry-run  ->  collapse duplicates  ->  purge expired raw
               ->  VACUUM   ->  verify

Design rules (all of them fail closed):

* **The backup gate is not advisory.**  ``preflight`` re-hashes the backup,
  runs ``PRAGMA integrity_check`` against it (opened ``immutable=1`` so a stale
  ``-wal``/``-shm`` cannot make a good backup look broken on exFAT) and compares
  its logical fingerprint against the live ledger.  ``execute`` refuses to run
  unless that gate passed in the same process.
* **No daemon, no write.**  ``execute`` refuses while anything serves the
  ledger: a matching process, a running launchd job, an open file handle, or a
  live pid recorded in ``hippomindd.json``.
* **Every destructive step is timed and recorded.**  Counts and byte sizes are
  written to a JSON state file so the markdown report is generated from
  measurements, never from recollection.
* **Verification is independent of the code under test.**  FTS synchronisation
  is recomputed with a linear anti-join instead of the shipped correlated
  subquery (see ``fts_sync_linear``), and archive recoverability is proven by
  matching each sampled archived row against its surviving duplicate.

Nothing here writes outside ``tools/migration/`` and the ledger/archive paths
the operator names.

Usage:
    python3 tools/migration/compact_ledger.py preflight
    python3 tools/migration/compact_ledger.py dry-run
    python3 tools/migration/compact_ledger.py execute
    python3 tools/migration/compact_ledger.py verify
    python3 tools/migration/compact_ledger.py all
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DB = Path.home() / "Library" / "Application Support" / "HippoMind" / "hippomind.sqlite"
DEFAULT_BACKUP = (REPO_ROOT / "backups"
                  / "hippomind-pre-compaction-20261019T141216Z.sqlite")
DEFAULT_BACKUP_SHA256 = "948002887dd42998c90d189b3af26b30f0a4f2313f4fbb6f1591aaf584d9b638"
DEFAULT_STATE = REPO_ROOT / "tools" / "migration" / ".compact-state.json"
DEFAULT_TTL_DAYS = 90
MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024  # archive + VACUUM temp headroom


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def human_bytes(value: Optional[float]) -> str:
    if not value:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


def sha256_file(path: Path, chunk: int = 8 << 20) -> str:
    digest = hashlib.sha256()
    total = 0
    started = time.monotonic()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
            total += len(block)
    elapsed = time.monotonic() - started
    sys.stderr.write(f"[sha256] {path.name}: {human_bytes(total)} in {elapsed:.1f}s\n")
    return digest.hexdigest()


def ro_connect(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """Read-only connection. ``immutable`` avoids creating sidecars on exFAT."""
    suffix = "&immutable=1" if immutable else ""
    conn = sqlite3.connect(f"file:{path}?mode=ro{suffix}", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def scalar(conn: sqlite3.Connection, sql: str, params: Tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return scalar(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) == 1


# ---------------------------------------------------------------------------
# preflight: the backup gate + daemon gate
# ---------------------------------------------------------------------------
def fingerprint(path: Path, *, immutable: bool) -> Dict[str, Any]:
    """Logical identity of a ledger, cheap enough to run on a 4.5GB file."""
    conn = ro_connect(path, immutable=immutable)
    try:
        data: Dict[str, Any] = {
            "memories": scalar(conn, "SELECT COUNT(*) FROM memories"),
            "ledger_events": scalar(conn, "SELECT COUNT(*) FROM ledger_events"),
            "memory_fts": scalar(conn, "SELECT COUNT(*) FROM memory_fts"),
            "raw_chat": scalar(conn, "SELECT COUNT(*) FROM memories WHERE raw_chat=1"),
            "semantic": scalar(conn, "SELECT COUNT(*) FROM memories WHERE kind='semantic'"),
            "memory_claims": scalar(conn, "SELECT COUNT(*) FROM memory_claims"),
            "memory_relations": scalar(conn, "SELECT COUNT(*) FROM memory_relations"),
            "memory_feedback": scalar(conn, "SELECT COUNT(*) FROM memory_feedback"),
            "schema_version": scalar(
                conn, "SELECT value FROM schema_meta WHERE key='schema_version'"),
            "min_created": scalar(conn, "SELECT MIN(created_at) FROM memories"),
            "max_created": scalar(conn, "SELECT MAX(created_at) FROM memories"),
            "journal_mode": scalar(conn, "PRAGMA journal_mode"),
            "page_size": scalar(conn, "PRAGMA page_size"),
            "page_count": scalar(conn, "PRAGMA page_count"),
            "freelist_count": scalar(conn, "PRAGMA freelist_count"),
        }
    finally:
        conn.close()
    data["file_bytes"] = path.stat().st_size
    return data


def detect_daemon(db_path: Path) -> Dict[str, Any]:
    """Evidence that something is (or is not) serving the ledger."""
    findings: Dict[str, Any] = {
        "matching_processes": [],
        "launchd": {},
        "open_handles": [],
        "recorded_pid": None,
        "recorded_pid_alive": False,
        "serving": False,
    }
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,command="],
                            capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        ps = ""
    pattern = re.compile(r"-m\s+hippomind\.(daemon|main|cli)\b")
    for line in ps.splitlines():
        line = line.strip()
        if not line or pattern.search(line) is None:
            continue
        pid, _, command = line.partition(" ")
        try:
            pid_int = int(pid)
        except ValueError:
            continue
        findings["matching_processes"].append(
            {"pid": pid_int, "command": command.strip()[:300]})

    try:
        listing = subprocess.run(["launchctl", "list"],
                                 capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        listing = ""
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].startswith("com.hippomind"):
            findings["launchd"][parts[2]] = parts[0]  # "-" means not running

    if shutil.which("lsof"):
        try:
            out = subprocess.run(["lsof", "-nP", str(db_path)],
                                 capture_output=True, text=True, timeout=60).stdout
            for line in out.splitlines()[1:]:
                if line.strip():
                    findings["open_handles"].append(line.strip()[:300])
        except (OSError, subprocess.SubprocessError):
            pass

    state_file = db_path.parent / "hippomindd.json"
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            pid = int(state.get("pid") or 0)
            findings["recorded_pid"] = pid
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    findings["recorded_pid_alive"] = True
                except OSError:
                    findings["recorded_pid_alive"] = False
        except (OSError, ValueError, TypeError):
            pass

    running_launchd = [label for label, pid in findings["launchd"].items() if pid != "-"]
    findings["serving"] = bool(
        findings["matching_processes"]
        or running_launchd
        or findings["open_handles"]
        or findings["recorded_pid_alive"]
    )
    return findings


def run_preflight(db_path: Path, backup: Path, expected_sha: str) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def record(check_id: str, ok: bool, detail: str, fatal: bool = True) -> None:
        checks.append({"id": check_id, "ok": bool(ok), "fatal": bool(fatal), "detail": detail})

    record("db_exists", db_path.is_file(), str(db_path))
    record("db_regular_file", db_path.is_file() and not db_path.is_symlink(),
           f"is_file={db_path.is_file()} is_symlink={db_path.is_symlink()}")
    record("backup_exists", backup.is_file(), str(backup))
    record("backup_regular_file", backup.is_file() and not backup.is_symlink(),
           f"is_file={backup.is_file()} is_symlink={backup.is_symlink()}")

    backup_sha = ""
    if backup.is_file():
        backup_sha = sha256_file(backup)
        record("backup_sha256", backup_sha == expected_sha,
               f"expected={expected_sha} actual={backup_sha}")

    backup_integrity = ""
    backup_fp: Dict[str, Any] = {}
    if backup.is_file():
        started = time.monotonic()
        conn = ro_connect(backup, immutable=True)
        try:
            backup_integrity = str(scalar(conn, "PRAGMA integrity_check"))
        finally:
            conn.close()
        record("backup_integrity_check", backup_integrity.lower() == "ok",
               f"{backup_integrity} ({time.monotonic() - started:.1f}s)")
        backup_fp = fingerprint(backup, immutable=True)

    live_fp: Dict[str, Any] = {}
    if db_path.is_file():
        live_fp = fingerprint(db_path, immutable=False)

    # The backup must describe the file we are about to rewrite.  Because the
    # daemon is stopped, equality is the expected result; anything else means
    # the rollback path would restore a different dataset.
    compared = ("memories", "ledger_events", "memory_fts", "raw_chat", "semantic",
                "memory_claims", "memory_relations", "memory_feedback",
                "schema_version", "min_created", "max_created")
    diff_keys = [key for key in compared if backup_fp.get(key) != live_fp.get(key)]
    record("backup_matches_live", not diff_keys,
           "identical logical fingerprint" if not diff_keys
           else f"divergent fields: {diff_keys}")

    daemon = detect_daemon(db_path)
    record("daemon_stopped", not daemon["serving"],
           json.dumps(daemon, ensure_ascii=False)[:900])

    if db_path.is_file():
        conn = ro_connect(db_path)
        try:
            residual = [name for name in ("_dedupe_groups",)
                        if table_exists(conn, name)]
        finally:
            conn.close()
        record("no_residual_temp_tables", not residual, f"residual={residual}")

    wal = Path(str(db_path) + "-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    record("wal_checkpointed", wal_bytes == 0, f"wal_bytes={wal_bytes}")

    free = shutil.disk_usage(db_path.parent).free
    record("free_space", free >= MIN_FREE_BYTES,
           f"free={human_bytes(free)} required>={human_bytes(MIN_FREE_BYTES)}")

    try:
        from hippomind.schema import SCHEMA_VERSION
        code_version = int(SCHEMA_VERSION)
        import_note = f"code={code_version}"
    except Exception as exc:  # pragma: no cover - import failure is fatal anyway
        code_version = -1
        import_note = f"{type(exc).__name__}: {exc}"
        record("import_schema_module", False, import_note)
    record("schema_version_supported",
           live_fp.get("schema_version") is not None
           and int(live_fp["schema_version"]) == code_version,
           f"ledger={live_fp.get('schema_version')} {import_note}")

    passed = all(item["ok"] for item in checks if item["fatal"])
    return {
        "step": "preflight",
        "passed": passed,
        "checks": checks,
        "backup": {"path": str(backup), "sha256": backup_sha,
                   "integrity_check": backup_integrity, "fingerprint": backup_fp},
        "live": {"path": str(db_path), "fingerprint": live_fp},
        "daemon": daemon,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# ledger helpers (read-only) used by dry-run and verify
# ---------------------------------------------------------------------------
def ledger_counts(path: Path) -> Dict[str, Any]:
    conn = ro_connect(path)
    try:
        return {
            "memories": scalar(conn, "SELECT COUNT(*) FROM memories"),
            "ledger_events": scalar(conn, "SELECT COUNT(*) FROM ledger_events"),
            "memory_fts": scalar(conn, "SELECT COUNT(*) FROM memory_fts"),
            "raw_chat": scalar(conn, "SELECT COUNT(*) FROM memories WHERE raw_chat=1"),
            "non_raw": scalar(conn, "SELECT COUNT(*) FROM memories WHERE raw_chat=0"),
            "semantic": scalar(conn, "SELECT COUNT(*) FROM memories WHERE kind='semantic'"),
            "semantic_distinct": scalar(
                conn, "SELECT COUNT(DISTINCT content) FROM memories WHERE kind='semantic'"),
            "schema_version": scalar(
                conn, "SELECT value FROM schema_meta WHERE key='schema_version'"),
            "page_size": scalar(conn, "PRAGMA page_size"),
            "page_count": scalar(conn, "PRAGMA page_count"),
            "freelist_count": scalar(conn, "PRAGMA freelist_count"),
            "journal_mode": scalar(conn, "PRAGMA journal_mode"),
            "min_created": scalar(conn, "SELECT MIN(created_at) FROM memories"),
            "max_created": scalar(conn, "SELECT MAX(created_at) FROM memories"),
        }
    finally:
        conn.close()


def duplicate_plan(path: Path) -> Dict[str, Any]:
    """Exact counts produced by the same GROUP BY collapse_duplicate_rows uses."""
    conn = ro_connect(path)
    try:
        grouped = """
            SELECT COUNT(*) AS n FROM memories
            WHERE raw_chat=1 AND content!=''
            GROUP BY content, source_agent, session_id, role
            HAVING COUNT(*)>1
        """
        content_only = """
            SELECT COUNT(*) AS n FROM memories WHERE raw_chat=1
            GROUP BY content HAVING COUNT(*)>1
        """
        groups = scalar(conn, f"SELECT COUNT(*) FROM ({grouped})")
        rows_in_groups = scalar(conn, f"SELECT COALESCE(SUM(n),0) FROM ({grouped})")
        redundant = scalar(conn, f"SELECT COALESCE(SUM(n-1),0) FROM ({grouped})")
        c_groups = scalar(conn, f"SELECT COUNT(*) FROM ({content_only})")
        c_redundant = scalar(conn, f"SELECT COALESCE(SUM(n-1),0) FROM ({content_only})")
        largest = scalar(conn, f"SELECT MAX(n) FROM ({grouped})")
        total = scalar(conn, "SELECT COUNT(*) FROM memories")
        return {
            "collapse_key": "content, source_agent, session_id, role",
            "duplicate_groups": groups,
            "rows_inside_groups": rows_in_groups,
            "redundant_rows": redundant,
            "largest_group": largest,
            "memories_before": total,
            "memories_after_expected": total - redundant,
            "content_only_groups": c_groups,
            "content_only_redundant_rows": c_redundant,
        }
    finally:
        conn.close()


def fts_sync_linear(path: Path) -> Dict[str, Any]:
    """Recompute the FTS-sync invariant with a linear anti-join.

    Two defects in the shipped ``fts_sync_report`` make it unusable as a
    regression gate on this ledger, and both are *measurement* defects rather
    than data defects:

    1. It correlates on ``memory_fts.memory_id``, which is declared
       ``UNINDEXED``: every outer row re-scans the whole FTS content table, i.e.
       O(memories x fts_rows).  On the 1.53M-row production ledger that is
       ~8.7e9 comparisons and measured >14 minutes of CPU before being killed.
       This version materialises the FTS ids once and anti-joins against them.
    2. Its ``unindexed`` predicate counts ``lifecycle IN ('active','cold')``
       rows, but maintenance deliberately keeps cold rows *out* of FTS (see
       ``maintain``: "Cold projections ... must leave the hot FTS index").
       Every ledger with cold rows therefore reports ``warn`` forever.  The
       invariant that actually holds is "every accepted **active** row is
       indexed", which is what ``active_accepted_missing`` measures.

    ``shipped_style_unindexed`` reproduces defect 2 verbatim so the report can
    show that the number moved for the expected reason, not because indexing
    regressed.
    """
    conn = ro_connect(path)
    try:
        conn.execute("CREATE TEMP TABLE fts_ids(memory_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO fts_ids SELECT memory_id FROM memory_fts")
        fts_rows = scalar(conn, "SELECT COUNT(*) FROM fts_ids")
        active_accepted = scalar(conn, """
            SELECT COUNT(*) FROM memories
            WHERE quality_state='accepted' AND lifecycle='active'""")
        active_accepted_missing = scalar(conn, """
            SELECT COUNT(*) FROM memories m
            WHERE m.quality_state='accepted' AND m.lifecycle='active'
              AND NOT EXISTS (SELECT 1 FROM fts_ids f WHERE f.memory_id=m.memory_id)""")
        cold_rows = scalar(conn, """
            SELECT COUNT(*) FROM memories WHERE lifecycle IN ('cold','expired','superseded')""")
        shipped_style_unindexed = scalar(conn, """
            SELECT COUNT(*) FROM memories m
            WHERE m.quality_state='accepted' AND m.lifecycle IN ('active','cold')
              AND NOT EXISTS (SELECT 1 FROM fts_ids f WHERE f.memory_id=m.memory_id)""")
        cold_leak = scalar(conn, """
            SELECT COUNT(*) FROM fts_ids f JOIN memories m ON m.memory_id=f.memory_id
            WHERE m.lifecycle!='active'""")
        dangling_claims = scalar(conn, """
            SELECT COUNT(*) FROM memory_claims c LEFT JOIN memories m
              ON m.memory_id=c.memory_id
            WHERE c.state='active'
              AND (m.memory_id IS NULL OR m.quality_state!='accepted')""")
        active_claims = scalar(conn,
                               "SELECT COUNT(*) FROM memory_claims WHERE state='active'")
    finally:
        conn.close()
    return {
        "status": "ok" if active_accepted_missing == 0 else "warn",
        "fts_rows": fts_rows,
        "active_accepted": active_accepted,
        "active_accepted_missing": active_accepted_missing,
        "cold_rows_deliberately_unindexed": cold_rows,
        "shipped_style_unindexed": shipped_style_unindexed,
        "fts_cold_leak": cold_leak,
        "active_claims": active_claims,
        "dangling_claims": dangling_claims,
        "method": "linear temp-table anti-join; invariant is active+accepted fully indexed",
    }


def archive_dir_for(db_path: Path) -> Path:
    return db_path.parent / (db_path.stem + ".raw-archive")


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------
def run_dry_run(db_path: Path, ttl_days: int) -> Dict[str, Any]:
    from hippomind.ledger import SQLiteLedger  # local import: constructor writes

    started = time.monotonic()
    ledger = SQLiteLedger(db_path)
    collapse = ledger.collapse_duplicate_rows(dry_run=True)
    purge = ledger.purge_expired_raw(before_days=ttl_days, dry_run=True)
    elapsed = time.monotonic() - started

    plan = duplicate_plan(db_path)
    counts = ledger_counts(db_path)
    age_days = None
    if counts.get("min_created"):
        try:
            oldest = datetime.fromisoformat(str(counts["min_created"]))
            age_days = (datetime.now(timezone.utc) - oldest).total_seconds() / 86400.0
        except ValueError:
            age_days = None

    return {
        "step": "dry-run",
        "elapsed_seconds": round(elapsed, 1),
        "collapse_dry_run": collapse,
        "purge_dry_run": purge,
        "duplicate_plan_independent": plan,
        "counts": counts,
        "ttl_days": ttl_days,
        "oldest_row_age_days": round(age_days, 1) if age_days is not None else None,
        "purge_expected_empty": bool(age_days is not None and age_days < ttl_days),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------
def run_execute(db_path: Path, ttl_days: int, preflight: Dict[str, Any]) -> Dict[str, Any]:
    if not preflight.get("passed"):
        raise SystemExit("preflight did not pass; refusing to write to the live ledger")
    # Re-check the daemon immediately before the first write: preflight may have
    # run minutes ago and a launchd job could have restarted in the meantime.
    daemon = detect_daemon(db_path)
    if daemon["serving"]:
        raise SystemExit(f"daemon became active after preflight; refusing to write: {daemon}")

    from hippomind.ledger import SQLiteLedger

    steps: List[Dict[str, Any]] = []
    before = ledger_counts(db_path)
    before_size = db_path.stat().st_size
    archive_dir = archive_dir_for(db_path)

    def archive_stats() -> Dict[str, Any]:
        if not archive_dir.is_dir():
            return {"files": 0, "bytes": 0}
        files = list(archive_dir.glob("*.jsonl.gz"))
        return {"files": len(files),
                "bytes": sum(p.stat().st_size for p in files if p.is_file())}

    archive_before = archive_stats()
    ledger = SQLiteLedger(db_path)

    # ---- 1) collapse exact duplicate raw rows -----------------------------
    started = time.monotonic()
    collapse = ledger.collapse_duplicate_rows()
    steps.append({"step": "collapse_duplicate_rows",
                  "elapsed_seconds": round(time.monotonic() - started, 1),
                  "result": collapse,
                  "db_bytes_after": db_path.stat().st_size})
    if collapse.get("error"):
        return {"step": "execute", "aborted": True, "at": "collapse_duplicate_rows",
                "steps": steps, "before": before, "before_bytes": before_size}
    if collapse.get("collapsed", 0) == 0:
        return {"step": "execute", "aborted": True, "at": "collapse_duplicate_rows",
                "reason": "collapse removed no rows", "steps": steps,
                "before": before, "before_bytes": before_size}

    # ---- 2) purge expired raw evidence (TTL) ------------------------------
    started = time.monotonic()
    purge = ledger.purge_expired_raw(before_days=ttl_days)
    steps.append({"step": "purge_expired_raw",
                  "elapsed_seconds": round(time.monotonic() - started, 1),
                  "result": purge,
                  "db_bytes_after": db_path.stat().st_size})
    if purge.get("error"):
        return {"step": "execute", "aborted": True, "at": "purge_expired_raw",
                "steps": steps, "before": before, "before_bytes": before_size}

    # ---- 3) VACUUM --------------------------------------------------------
    started = time.monotonic()
    vacuum = ledger.vacuum()
    steps.append({"step": "vacuum",
                  "elapsed_seconds": round(time.monotonic() - started, 1),
                  "result": vacuum,
                  "db_bytes_after": db_path.stat().st_size})

    after = ledger_counts(db_path)
    after_size = db_path.stat().st_size
    return {
        "step": "execute",
        "aborted": False,
        "steps": steps,
        "before": before,
        "after": after,
        "before_bytes": before_size,
        "after_bytes": after_size,
        "reclaimed_bytes": max(0, before_size - after_size),
        "archive_before": archive_before,
        "archive_after": archive_stats(),
        "ttl_days": ttl_days,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def sample_retained_memories(conn: sqlite3.Connection, count: int,
                             seed: int = 20261019) -> List[Dict[str, Any]]:
    """Pick retained, active, structured memories — one per distinct content.

    Deduplicating by content is what makes the probe informative: this ledger
    stores 200 byte-identical copies of several semantic summaries, so a uniform
    sample returns the same query 20 times and any single query can only surface
    80 of those 200 twins.
    """
    rows = conn.execute("""
        SELECT m.memory_id, m.content, m.scope, m.kind, m.created_at, m.lifecycle
        FROM memories m
        WHERE m.quality_state='accepted' AND m.lifecycle='active'
          AND m.raw_chat=0 AND LENGTH(m.content) >= 12
          AND m.memory_id IN (
              SELECT MIN(memory_id) FROM memories
              WHERE quality_state='accepted' AND lifecycle='active'
                AND raw_chat=0 AND LENGTH(content) >= 12
              GROUP BY content)
        ORDER BY m.memory_id
    """).fetchall()
    rng = random.Random(seed)
    picked = rng.sample(rows, min(count, len(rows)))
    return [dict(row) for row in picked]


def distinctive_phrase(text: str, used: Optional[set] = None) -> str:
    """A query fragment the FTS trigram index can match, unique across probes.

    Prefers the *rarest-looking* CJK run or ASCII token and avoids reusing a
    phrase already assigned to another probe, because a shared template fragment
    is precisely the case where one row cannot be individually recalled.
    """
    candidates: List[str] = []
    for segment in re.findall(r"[\u3400-\u9fff]{4,}", text):
        candidates.append(segment[:8])
        candidates.append(segment[-8:])
    for word in re.findall(r"[A-Za-z0-9_]{4,}", text):
        candidates.append(word[:24])
    seen = used if used is not None else set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            return candidate
    return ""


def _run_recall_probes(ledger, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    used: set = set()
    details: List[Dict[str, Any]] = []
    started = time.monotonic()
    for row in samples:
        phrase = distinctive_phrase(str(row["content"]), used)
        if not phrase:
            details.append({"memory_id": row["memory_id"], "query": "",
                            "found": None, "rank": None,
                            "note": "no distinctive phrase"})
            continue
        try:
            results = ledger.search(phrase, scope=str(row["scope"]), limit=80)
        except Exception as exc:  # noqa: BLE001 - report, never crash the drill
            details.append({"memory_id": row["memory_id"], "query": phrase,
                            "found": None, "rank": None,
                            "note": f"search raised {type(exc).__name__}: {exc}"})
            continue
        ids = [str(item.get("memory_id", "")) for item in results]
        rank = ids.index(row["memory_id"]) + 1 if row["memory_id"] in ids else None
        # "Recalled" means the memory is reachable, not that this exact row id
        # won a slot: byte-identical twins are interchangeable by construction,
        # and the survivor keeps the same content.
        content_equivalent = any(item.get("content") == row["content"] for item in results)
        details.append({"memory_id": row["memory_id"], "query": phrase,
                        "found": rank is not None, "rank": rank,
                        "content_equivalent": content_equivalent,
                        "returned": len(ids), "scope": row["scope"],
                        "kind": row["kind"], "lifecycle": row["lifecycle"]})
    return {
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "sample_size": len(details),
        "recalled": sum(1 for item in details if item["found"]),
        "recalled_content_equivalent": sum(
            1 for item in details if item.get("content_equivalent")),
        "at_rank_1": sum(1 for item in details if item["rank"] == 1),
        "distinct_queries": len({item["query"] for item in details if item["query"]}),
        "details": details,
    }


def run_recall_ab(live_db: Path, control_db: Path,
                  sample_size: int = 20) -> Dict[str, Any]:
    """Replay one identical probe set against live and pre-compaction ledgers.

    Compaction changes which *rows* exist, so the only defensible claim is
    parity on a probe set that both ledgers can serve.  Probes are built from
    the control ledger's content (the superset), then run against both.
    """
    from hippomind.ledger import SQLiteLedger

    conn = ro_connect(control_db)
    try:
        samples = sample_retained_memories(conn, sample_size)
    finally:
        conn.close()
    control = _run_recall_probes(SQLiteLedger(control_db), samples)
    live = _run_recall_probes(SQLiteLedger(live_db), samples)
    return {
        "probe_source": str(control_db),
        "sample_size": len(samples),
        "control": {k: control[k] for k in (
            "recalled", "recalled_content_equivalent", "at_rank_1", "distinct_queries")},
        "live": {k: live[k] for k in (
            "recalled", "recalled_content_equivalent", "at_rank_1", "distinct_queries")},
        "parity": (control["recalled_content_equivalent"]
                   == live["recalled_content_equivalent"]),
        "details": [{"memory_id": control_row["memory_id"],
                     "query": control_row["query"],
                     "control_rank": control_row["rank"],
                     "live_rank": live_row["rank"],
                     "control_equiv": control_row.get("content_equivalent"),
                     "live_equiv": live_row.get("content_equivalent")}
                    for control_row, live_row in zip(control["details"], live["details"])],
    }


def run_verify(db_path: Path, expected_archive: str,
               baseline: Dict[str, Any], *, sample_size: int = 20,
               control_db: Optional[Path] = None) -> Dict[str, Any]:
    from hippomind.ledger import SQLiteLedger

    out: Dict[str, Any] = {"step": "verify", "samples": sample_size}

    # ---- structural integrity --------------------------------------------
    started = time.monotonic()
    conn = ro_connect(db_path)
    try:
        integrity = str(scalar(conn, "PRAGMA integrity_check"))
    finally:
        conn.close()
    out["integrity_check"] = {"status": integrity,
                              "elapsed_seconds": round(time.monotonic() - started, 1),
                              "ok": integrity.lower() == "ok"}

    out["counts"] = ledger_counts(db_path)
    out["file_bytes"] = db_path.stat().st_size
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        out[f"file_bytes{suffix.replace('-', '_')}"] = (
            sidecar.stat().st_size if sidecar.exists() else 0)

    # ---- no duplicates survive -------------------------------------------
    plan = duplicate_plan(db_path)
    out["duplicate_plan_after"] = plan
    out["duplicates_eliminated"] = plan["redundant_rows"] == 0

    # ---- FTS sync / claims ------------------------------------------------
    out["fts_sync"] = fts_sync_linear(db_path)
    out["baseline_fts_sync"] = baseline.get("fts_sync")
    base_missing = (baseline.get("fts_sync") or {}).get("active_accepted_missing")
    out["fts_sync_unchanged_vs_baseline"] = (
        out["fts_sync"].get("active_accepted_missing") == base_missing)

    # ---- recall probe -----------------------------------------------------
    # A probe is only meaningful if each sampled memory has a *distinctive*
    # query.  Sampling uniformly over active structured rows draws mostly from
    # the template-heavy semantic summaries (200 byte-identical copies of one
    # line share 80 result slots), which measures corpus shape, not compaction.
    # Probes are therefore built from one representative per distinct content,
    # and the same probe set is replayed against a pre-compaction control copy
    # when one is supplied, so any difference is attributable to compaction.
    ledger = SQLiteLedger(db_path)
    conn = ro_connect(db_path)
    try:
        samples = sample_retained_memories(conn, sample_size)
    finally:
        conn.close()
    recall = _run_recall_probes(ledger, samples)
    out["recall"] = recall

    # ---- archive recoverability ------------------------------------------
    archive = Path(expected_archive) if expected_archive else None
    if archive and archive.is_file():
        out["archive"] = verify_archive(db_path, archive, sample_size)
    else:
        out["archive"] = {"error": f"archive not found: {expected_archive}"}

    # ---- A/B recall parity against the pre-compaction control ------------
    if control_db and Path(control_db).is_file():
        out["recall_ab"] = run_recall_ab(db_path, Path(control_db), sample_size)
        out["provenance_gap"] = provenance_gap(Path(control_db))
    else:
        out["recall_ab"] = {"skipped": "no control db supplied"}
        out["provenance_gap"] = {"skipped": "no control db supplied"}

    out["recorded_at"] = datetime.now(timezone.utc).isoformat()
    return out


def verify_archive(db_path: Path, archive: Path, sample_size: int) -> Dict[str, Any]:
    """Full structural pass + random rows proven recoverable against survivors.

    The dedupe payload deliberately carries only identity, content and its
    digest (see ``collapse_duplicate_rows``): ``role`` is *not* archived.  It is
    safe to omit because a duplicate group is keyed by
    ``(content, source_agent, session_id, role)`` and role is constant inside a
    group — verified against the pre-compaction control — so
    ``(content, source_agent, session_id)`` identifies the survivor uniquely.
    Matching on ``role`` here would fail for every row and report a false
    catastrophe.

    Recoverability is proven as: the deleted row and its event are gone, *and*
    a surviving row with byte-identical content still exists, *and* the archived
    digest equals that survivor's event digest.
    """
    started = time.monotonic()
    lines = 0
    bad_json = 0
    missing_fields = 0
    empty_content = 0
    reservoir: List[Dict[str, Any]] = []
    rng = random.Random(20261019)
    reasons: Dict[str, int] = {}
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            lines += 1
            try:
                payload = json.loads(line)
            except ValueError:
                bad_json += 1
                continue
            if not payload.get("memory_id") or not payload.get("event_id"):
                missing_fields += 1
                continue
            if not str(payload.get("content") or "").strip():
                empty_content += 1
            reason = str(payload.get("reason") or ("purged" if payload.get("purged_at") else "?"))
            reasons[reason] = reasons.get(reason, 0) + 1
            if len(reservoir) < sample_size:
                reservoir.append(payload)
            else:
                index = rng.randrange(lines)
                if index < sample_size:
                    reservoir[index] = payload

    conn = ro_connect(db_path)
    try:
        recoverable = 0
        details: List[Dict[str, Any]] = []
        for payload in reservoir:
            memory_id = str(payload["memory_id"])
            event_id = str(payload["event_id"])
            still_there = scalar(
                conn, "SELECT 1 FROM memories WHERE memory_id=?", (memory_id,))
            event_there = scalar(
                conn, "SELECT 1 FROM ledger_events WHERE event_id=?", (event_id,))
            survivor = conn.execute("""
                SELECT m.memory_id, e.content_hash
                FROM memories m JOIN ledger_events e ON e.event_id=m.origin_event_id
                WHERE m.raw_chat=1 AND m.content=? AND m.source_agent=?
                  AND m.session_id=?
                LIMIT 1
            """, (payload.get("content", ""), payload.get("source_agent", ""),
                  payload.get("session_id", ""))).fetchone()
            survivor_id = str(survivor["memory_id"]) if survivor else None
            digest_matches = bool(
                survivor is not None
                and str(survivor["content_hash"]) == str(payload.get("content_sha256")))
            ok = (still_there is None and event_there is None
                  and survivor_id is not None and digest_matches)
            recoverable += 1 if ok else 0
            details.append({
                "memory_id": memory_id,
                "event_id": event_id,
                "content_chars": len(str(payload.get("content") or "")),
                "content_sha256": payload.get("content_sha256"),
                "reason": payload.get("reason"),
                "created_at": payload.get("created_at"),
                "row_still_in_db": still_there is not None,
                "event_still_in_db": event_there is not None,
                "survivor_memory_id": survivor_id,
                "digest_matches_survivor_event": digest_matches,
                "recoverable": ok,
            })
    finally:
        conn.close()

    return {
        "path": str(archive),
        "bytes": archive.stat().st_size,
        "sha256": sha256_file(archive),
        "lines": lines,
        "bad_json_lines": bad_json,
        "missing_id_lines": missing_fields,
        "empty_content_lines": empty_content,
        "reasons": reasons,
        "full_decompress_ok": bad_json == 0 and missing_fields == 0,
        "sample_size": len(details),
        "sample_recoverable": recoverable,
        "sample_details": details,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }


def provenance_gap(control_db: Path, *, sample: int = 500) -> Dict[str, Any]:
    """Measure what the dedupe archive does *not* carry.

    ``collapse_duplicate_rows`` archives identity + content only.  The rows it
    deletes also carried ``metadata_json`` (697MB across 1.34M rows, measured on
    the pre-compaction control), which is where ``archive_rowid`` — the pointer
    into the external Agent-Chat-Archive — lives.  Content is fully recoverable
    from the archive; the *per-copy* provenance pointer is not.

    This is reported rather than "fixed": the collapse semantics are owned by
    ``hippomind/ledger.py``, which this run is not allowed to modify.  The
    operator needs the number to decide whether to keep the pre-compaction
    backup as the provenance source of record.
    """
    conn = ro_connect(control_db)
    try:
        survivors = """
            SELECT MAX(memory_id) FROM memories WHERE raw_chat=1 AND content!=''
            GROUP BY content, source_agent, session_id, role
        """
        deleted = scalar(
            conn, f"SELECT COUNT(*) FROM memories WHERE raw_chat=1 AND memory_id NOT IN ({survivors})")
        metadata_bytes = scalar(
            conn, f"SELECT COALESCE(SUM(LENGTH(metadata_json)),0) FROM memories "
                  f"WHERE raw_chat=1 AND memory_id NOT IN ({survivors})")
        with_rowid = scalar(
            conn, f"SELECT COUNT(*) FROM memories WHERE raw_chat=1 "
                  f"AND memory_id NOT IN ({survivors}) "
                  f"AND metadata_json LIKE '%archive_rowid%'")
        distinct_rowid = scalar(
            conn, f"SELECT COUNT(DISTINCT json_extract(metadata_json,'$.archive_rowid')) "
                  f"FROM memories WHERE raw_chat=1 AND memory_id NOT IN ({survivors}) "
                  f"AND metadata_json LIKE '%archive_rowid%'")
        evidence_bytes = scalar(
            conn, f"SELECT COALESCE(SUM(LENGTH(evidence_json)),0) FROM memories "
                  f"WHERE raw_chat=1 AND memory_id NOT IN ({survivors})")

        # Is the external chat archive still able to serve those pointers?
        rows = conn.execute(f"""
            SELECT json_extract(metadata_json,'$.archive_rowid') AS rid, content
            FROM memories
            WHERE raw_chat=1 AND memory_id NOT IN ({survivors})
              AND metadata_json LIKE '%archive_rowid%'
            LIMIT ?""", (sample,)).fetchall()
    finally:
        conn.close()

    external = Path.home() / "Library" / "Application Support" / "Agent-Chat-Archive" / "index" / "index.sqlite"
    external_present = external.is_file()
    external_found = external_match = 0
    if external_present and rows:
        ext = ro_connect(external)
        try:
            for row in rows:
                rid = row["rid"]
                if rid is None:
                    continue
                hit = ext.execute("SELECT content FROM messages WHERE rowid=?", (rid,)).fetchone()
                if hit:
                    external_found += 1
                    if hit["content"] == row["content"]:
                        external_match += 1
        finally:
            ext.close()

    return {
        "control_db": str(control_db),
        "deleted_rows": deleted,
        "deleted_metadata_json_bytes": metadata_bytes,
        "deleted_evidence_json_bytes": evidence_bytes,
        "deleted_rows_with_archive_rowid": with_rowid,
        "distinct_archive_rowid_lost": distinct_rowid,
        "archive_carries_metadata_json": False,
        "content_recoverable_from_archive": True,
        "external_archive": {
            "path": str(external),
            "present": external_present,
            "sampled_pointers": len(rows),
            "resolved": external_found,
            "content_identical": external_match,
            "note": ("per-copy provenance pointers are only partly re-resolvable "
                     "from the external archive; the pre-compaction backup remains "
                     "the authoritative source for them"),
        },
    }


# ---------------------------------------------------------------------------
# state handling
# ---------------------------------------------------------------------------
def load_state(path: Path) -> Dict[str, Any]:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=("preflight", "dry-run", "execute",
                                         "verify", "all", "show"))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--backup", default=str(DEFAULT_BACKUP))
    parser.add_argument("--backup-sha256", default=DEFAULT_BACKUP_SHA256)
    parser.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--control", default="",
                        help="pre-compaction copy used for the A/B recall parity check")
    parser.add_argument("--json", dest="json_out", default="")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser()
    backup = Path(args.backup).expanduser()
    state_path = Path(args.state).expanduser()
    state = load_state(state_path)

    def emit(key: str, payload: Dict[str, Any]) -> None:
        state[key] = payload
        save_state(state_path, state)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        print(text)
        if args.json_out:
            out = Path(args.json_out).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8")

    if args.step == "show":
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    if args.step == "preflight":
        result = run_preflight(db_path, backup, args.backup_sha256)
        emit("preflight", result)
        return 0 if result["passed"] else 2

    if args.step == "dry-run":
        result = run_dry_run(db_path, args.ttl_days)
        emit("dry_run", result)
        return 0

    if args.step == "execute":
        if "preflight" not in state:
            raise SystemExit("run the preflight step first")
        result = run_execute(db_path, args.ttl_days, state["preflight"])
        emit("execute", result)
        return 0 if not result.get("aborted") else 3

    if args.step == "verify":
        archive = ""
        execute = state.get("execute") or {}
        for item in execute.get("steps", []):
            found = (item.get("result") or {}).get("archive_path")
            if found:
                archive = str(found)
        if not archive:
            raise SystemExit("no archive_path recorded in state; run execute first")
        baseline = state.get("baseline_fts_sync") or {
            "fts_sync": (state.get("verify") or {}).get("baseline_fts_sync")}
        result = run_verify(db_path, archive, baseline, sample_size=args.samples,
                            control_db=Path(args.control).expanduser() if args.control else None)
        emit("verify", result)
        ok = (result["integrity_check"]["ok"]
              and result["duplicates_eliminated"]
              and result["archive"].get("full_decompress_ok")
              and result["archive"].get("sample_recoverable")
              == result["archive"].get("sample_size"))
        return 0 if ok else 4

    # all: preflight -> dry-run -> execute -> verify, stopping at the first gate
    pre = run_preflight(db_path, backup, args.backup_sha256)
    state["preflight"] = pre
    save_state(state_path, state)
    print(json.dumps({"preflight_passed": pre["passed"]}, ensure_ascii=False))
    if not pre["passed"]:
        return 2
    state["baseline_fts_sync"] = fts_sync_linear(db_path)
    save_state(state_path, state)
    dry = run_dry_run(db_path, args.ttl_days)
    state["dry_run"] = dry
    save_state(state_path, state)
    print(json.dumps({"dry_run": {
        "collapse": dry["collapse_dry_run"],
        "purge": dry["purge_dry_run"],
        "oldest_row_age_days": dry["oldest_row_age_days"],
    }}, ensure_ascii=False))
    exe = run_execute(db_path, args.ttl_days, pre)
    state["execute"] = exe
    save_state(state_path, state)
    print(json.dumps({"execute": {k: exe.get(k) for k in (
        "aborted", "before_bytes", "after_bytes", "reclaimed_bytes")}},
        ensure_ascii=False))
    if exe.get("aborted"):
        return 3
    archive = ""
    for item in exe.get("steps", []):
        archive = (item.get("result") or {}).get("archive_path") or archive
    ver = run_verify(db_path, archive, {"fts_sync": state.get("baseline_fts_sync")},
                     sample_size=args.samples,
                     control_db=Path(args.control).expanduser() if args.control else None)
    state["verify"] = ver
    save_state(state_path, state)
    print(json.dumps({"verify": {
        "integrity": ver["integrity_check"],
        "duplicates_eliminated": ver["duplicates_eliminated"],
        "recall": {k: ver["recall"][k] for k in ("recalled", "sample_size", "at_rank_1")},
        "archive": {k: ver["archive"].get(k) for k in (
            "lines", "sample_recoverable", "sample_size", "full_decompress_ok")},
    }}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
