"""Read-only analysis of a live HippoMind ledger before any compaction.

This tool NEVER writes to the database.  It opens the ledger with ``mode=ro`` and
prints a JSON report describing exactly what a compaction would do: how much of
the file is raw chat, how much content is duplicated, how large the JSON sidecars
are, and how much space each cleanup step is expected to recover.

The goal is to make the destructive step reviewable *before* it runs, and to
produce an auditable artefact that can be stored next to the backup.

Usage:
    python3 tools/migration/dry_run_report.py [--db PATH] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List

DEFAULT_DB = os.path.expanduser("~/Library/Application Support/HippoMind/hippomind.sqlite")


def _ro(path: str) -> sqlite3.Connection:
    """Open read-only so an accidental write cannot corrupt a live ledger."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _scalar(conn: sqlite3.Connection, sql: str, params=()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def analyse(path: str) -> Dict[str, Any]:
    conn = _ro(path)
    try:
        report: Dict[str, Any] = {"database": path, "read_only": True}

        if not os.path.exists(path):
            raise SystemExit(f"数据库不存在: {path}")
        report["file_bytes"] = os.path.getsize(path)
        for suffix in ("-wal", "-shm"):
            sibling = path + suffix
            report[f"file_bytes{suffix.replace('-', '_')}"] = (
                os.path.getsize(sibling) if os.path.exists(sibling) else 0
            )

        report["schema_version"] = _scalar(
            conn, "SELECT value FROM schema_meta WHERE key='schema_version'"
        )
        report["journal_mode"] = _scalar(conn, "PRAGMA journal_mode")
        report["page_size"] = _scalar(conn, "PRAGMA page_size")
        report["page_count"] = _scalar(conn, "PRAGMA page_count")
        report["freelist_count"] = _scalar(conn, "PRAGMA freelist_count")

        # ---- composition -------------------------------------------------
        rows = conn.execute(
            """SELECT lifecycle, raw_chat, COUNT(*) AS n,
                      COALESCE(SUM(LENGTH(content)),0) AS content_chars
               FROM memories GROUP BY lifecycle, raw_chat"""
        ).fetchall()
        report["composition"] = [
            {
                "lifecycle": r["lifecycle"],
                "raw_chat": bool(r["raw_chat"]),
                "rows": r["n"],
                "content_chars": r["content_chars"],
            }
            for r in rows
        ]
        report["total_memories"] = _scalar(conn, "SELECT COUNT(*) FROM memories")
        report["total_events"] = _scalar(conn, "SELECT COUNT(*) FROM ledger_events")

        # ---- where the bytes actually live -------------------------------
        report["bytes_by_column"] = {
            "content": _scalar(conn, "SELECT SUM(LENGTH(content)) FROM memories") or 0,
            "evidence_json": _scalar(conn, "SELECT SUM(LENGTH(evidence_json)) FROM memories") or 0,
            "metadata_json": _scalar(conn, "SELECT SUM(LENGTH(metadata_json)) FROM memories") or 0,
            "tags_json": _scalar(conn, "SELECT SUM(LENGTH(tags_json)) FROM memories") or 0,
        }
        report["bytes_by_column_events"] = {
            "content": _scalar(conn, "SELECT SUM(LENGTH(content)) FROM ledger_events") or 0,
            "evidence_json": _scalar(conn, "SELECT SUM(LENGTH(evidence_json)) FROM ledger_events") or 0,
            "metadata_json": _scalar(conn, "SELECT SUM(LENGTH(metadata_json)) FROM ledger_events") or 0,
        }

        # ---- duplication --------------------------------------------------
        # Exact duplicate (normalised) user/assistant text inside a session.  The
        # capture storm makes this the single biggest recoverable win.
        dup = conn.execute(
            """SELECT COALESCE(SUM(c - 1), 0) AS redundant_rows,
                      COUNT(*) AS duplicate_groups
               FROM (
                   SELECT content, COUNT(*) AS c
                   FROM memories
                   WHERE raw_chat = 1
                   GROUP BY content
                   HAVING c > 1
               )"""
        ).fetchone()
        report["raw_duplication"] = {
            "redundant_rows": dup["redundant_rows"],
            "duplicate_groups": dup["duplicate_groups"],
        }

        sem = conn.execute(
            """SELECT COUNT(*) AS rows, COUNT(DISTINCT content) AS distinct_content
               FROM memories WHERE kind = 'semantic'"""
        ).fetchone()
        report["semantic_bloat"] = {
            "rows": sem["rows"],
            "distinct_content": sem["distinct_content"],
            "redundant_rows": max(0, sem["rows"] - sem["distinct_content"]),
        }

        # ---- raw retention -------------------------------------------------
        report["raw_age"] = {
            "oldest": _scalar(conn, "SELECT MIN(created_at) FROM memories WHERE raw_chat=1"),
            "newest": _scalar(conn, "SELECT MAX(created_at) FROM memories WHERE raw_chat=1"),
            "rows_older_than_90d": _scalar(
                conn,
                "SELECT COUNT(*) FROM memories WHERE raw_chat=1 "
                "AND created_at < datetime('now','-90 day')",
            ),
        }

        # ---- unused-but-advertised structures -----------------------------
        report["unused_structures"] = {
            table: _scalar(conn, f"SELECT COUNT(*) FROM {table}")
            for table in ("memory_claims", "memory_relations", "memory_feedback")
        }
        report["fts_rows"] = _scalar(conn, "SELECT COUNT(*) FROM memory_fts") or 0
        report["fts_cold_leak"] = _scalar(
            conn,
            """SELECT COUNT(*) FROM memory_fts f
               JOIN memories m ON m.memory_id = f.memory_id
               WHERE m.lifecycle != 'active'""",
        )

        # ---- candidate cleanup actions ------------------------------------
        actions: List[Dict[str, Any]] = []
        redundant = report["raw_duplication"]["redundant_rows"]
        if redundant:
            actions.append({
                "action": "collapse_raw_duplicates",
                "rows_removed": redundant,
                "rationale": "同一会话内规范化后完全相同的原始消息只保留最早一条",
            })
        if report["semantic_bloat"]["redundant_rows"]:
            actions.append({
                "action": "collapse_semantic_duplicates",
                "rows_removed": report["semantic_bloat"]["redundant_rows"],
                "rationale": "巩固自噬产生的同内容 semantic 摘要合并为一条（保留谱系）",
            })
        if report["fts_cold_leak"]:
            actions.append({
                "action": "purge_cold_from_fts",
                "rows_removed": report["fts_cold_leak"],
                "rationale": "冷归档记忆不应占用热索引",
            })
        actions.append({
            "action": "vacuum",
            "rows_removed": 0,
            "rationale": "回收删除后的空洞页；预计回收 freelist_count 页",
        })
        report["planned_actions"] = actions
        report["estimated_reclaim_bytes"] = (
            report["freelist_count"] * report["page_size"]
        )
        return report
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读分析 HippoMind 账本，输出压缩预案")
    parser.add_argument("--db", default=DEFAULT_DB, help="账本路径")
    parser.add_argument("--json", dest="out", help="把报告写到指定 JSON 文件")
    args = parser.parse_args(argv)

    report = analyse(args.db)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"[wrote] {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
