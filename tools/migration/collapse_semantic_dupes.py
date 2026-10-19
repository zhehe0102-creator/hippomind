#!/usr/bin/env python3
"""Collapse the consolidation-storm duplicates that survived the main compaction.

The main compaction only collapsed *raw* rows. The other half of the bloat is
``kind='semantic'``: 56,010 rows holding just 142 distinct texts, because
``consolidate()`` used to re-ingest an identical summary on every idle pass (one
text repeated 5,423 times). Those rows are pure noise in the 8-item Context Pack
budget and they are not referenced by any claim or relation.

This tool is deliberately narrow and auditable:

1. Group ``kind='semantic'`` rows by ``(content, scope, task_id, project)`` —
   the same summary in a different task is a different record.
2. Keep the **earliest** row of each group (stable, reproducible), preferring an
   ``active`` row over a ``cold`` one so recall keeps working.
3. Write every removed row to a verified gzip archive (fsync + re-read) before
   deleting anything.
4. Delete children first (fts/relations/claims/feedback), then memories, then the
   orphaned ledger events.

Fail-closed: if the archive cannot be written or verified, nothing is deleted.

Usage:
    python3 tools/migration/collapse_semantic_dupes.py --dry-run
    python3 tools/migration/collapse_semantic_dupes.py --apply
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import time

DEFAULT_DB = os.path.expanduser("~/Library/Application Support/HippoMind/hippomind.sqlite")
GROUP_KEY = "content, scope, task_id, project"


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def plan(conn: sqlite3.Connection) -> dict:
    """Return the survivor/duplicate split without touching anything."""
    rows = conn.execute("""
        SELECT memory_id, content, scope, task_id, project, lifecycle, created_at
        FROM memories WHERE kind='semantic'
        ORDER BY content, scope, task_id, project,
                 CASE lifecycle WHEN 'active' THEN 0 ELSE 1 END,
                 created_at ASC, memory_id ASC
    """).fetchall()
    survivors, duplicates = {}, []
    for row in rows:
        key = (row["content"], row["scope"], row["task_id"], row["project"])
        if key in survivors:
            duplicates.append((row, survivors[key]["memory_id"]))
        else:
            survivors[key] = row
    return {
        "groups": len(survivors),
        "duplicates": len(duplicates),
        "survivors": list(survivors.values()),
        "duplicate_rows": duplicates,
    }


def verify_archive(path: str, expected_ids: set) -> tuple:
    found = set()
    lines = 0
    with gzip.open(path, "rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            lines += 1
            found.add(str(json.loads(line).get("memory_id", "")))
    return lines, expected_ids - found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="折叠巩固自噬产生的重复 semantic 行")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--archive-dir", default="")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只演练）")
    parser.add_argument("--dry-run", action="store_true", help="只演练，不写入（默认行为）")
    parser.add_argument("--batch-size", type=int, default=2000)
    args = parser.parse_args(argv)

    conn = _ro(args.db)
    try:
        report = plan(conn)
    finally:
        conn.close()

    summary = {
        "database": args.db,
        "dry_run": not args.apply,
        "groups": report["groups"],
        "duplicates": report["duplicates"],
        "kept": len(report["survivors"]),
    }
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if not report["duplicates"]:
        summary["removed"] = 0
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    root = (args.archive_dir or os.path.join(os.path.dirname(args.db),
                                             os.path.basename(args.db) + ".raw-archive"))
    os.makedirs(root, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    digest = hashlib.sha256(args.db.encode()).hexdigest()[:12]
    archive_path = os.path.join(root, f"semantic-dedupe-{stamp}-{digest}.jsonl.gz")

    ids, events = [], []
    try:
        with gzip.open(archive_path, "wb") as handle:
            for row, survivor_id in report["duplicate_rows"]:
                payload = {
                    "reason": "semantic_duplicate",
                    "memory_id": row["memory_id"],
                    "survivor_memory_id": survivor_id,
                    "content": row["content"],
                    "scope": row["scope"],
                    "task_id": row["task_id"],
                    "project": row["project"],
                    "lifecycle": row["lifecycle"],
                    "created_at": row["created_at"],
                    "collapsed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                handle.write((json.dumps(payload, ensure_ascii=False,
                                         separators=(",", ":")) + "\n").encode("utf-8"))
                ids.append(str(row["memory_id"]))
        os.chmod(archive_path, 0o600)
        with open(archive_path, "rb") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        print(json.dumps({"error": f"archive failed: {exc}", "removed": 0},
                         ensure_ascii=False))
        return 1

    lines, missing = verify_archive(archive_path, set(ids))
    if missing:
        print(json.dumps({"error": f"archive missing {len(missing)} rows; refusing to delete",
                          "removed": 0}, ensure_ascii=False))
        return 1

    write = sqlite3.connect(args.db, isolation_level=None)
    write.row_factory = sqlite3.Row
    write.execute("PRAGMA busy_timeout=30000")
    removed = 0
    try:
        write.execute("BEGIN IMMEDIATE")
        for start in range(0, len(ids), args.batch_size):
            chunk = ids[start:start + args.batch_size]
            placeholders = ",".join("?" for _ in chunk)
            events.extend(row[0] for row in write.execute(
                f"SELECT origin_event_id FROM memories WHERE memory_id IN ({placeholders})",
                chunk).fetchall())
            write.execute(f"DELETE FROM memory_fts WHERE memory_id IN ({placeholders})", chunk)
            write.execute(f"DELETE FROM memory_relations WHERE child_memory_id IN ({placeholders})", chunk)
            write.execute(f"DELETE FROM memory_relations WHERE parent_memory_id IN ({placeholders})", chunk)
            write.execute(f"DELETE FROM memory_claims WHERE memory_id IN ({placeholders})", chunk)
            write.execute(f"DELETE FROM memory_feedback WHERE memory_id IN ({placeholders})", chunk)
            removed += write.execute(
                f"DELETE FROM memories WHERE memory_id IN ({placeholders})", chunk).rowcount
        for start in range(0, len(events), args.batch_size):
            chunk = events[start:start + args.batch_size]
            placeholders = ",".join("?" for _ in chunk)
            write.execute(f"DELETE FROM ledger_events WHERE event_id IN ({placeholders})", chunk)
        write.execute("COMMIT")
    except sqlite3.Error as exc:
        write.execute("ROLLBACK")
        print(json.dumps({"error": f"delete failed: {exc}", "removed": 0},
                         ensure_ascii=False))
        return 1
    finally:
        write.close()

    summary.update({
        "removed": removed,
        "archive_path": archive_path,
        "archive_bytes": os.path.getsize(archive_path),
        "archive_lines": lines,
        "events_removed": len(events),
    })
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
