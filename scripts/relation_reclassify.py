#!/usr/bin/env python3
"""Reclassify legacy related_to edges into the typed relation vocabulary.

Why this exists
---------------
memory_relations held **2,023 edges and exactly one type** on the production
ledger (2026-10-25): related_to.  A single undifferentiated type makes the
associative-expansion step blind -- "two unrelated progress notes from the same
task" and "a fact that a later fact replaced" score the same, so
causal_ancestors / entity_graph cannot say *why* a memory is worth expanding.

The fix has two halves.  The write side (distill.py) now types new edges at
creation time.  This script is the other half: a **one-off, explainable, opt-in**
reclassification of edges that already exist.

Hard rules (each one is a test in tests/test_relation_types.py)
------------------------------------------------------------------
1. **Only related_to is touched.**  caused_by / follows / derived_from / anything
   else written by ingest or the adapter keeps its type: those edges already
   carry a decision.
2. **No evidence -> no change.**  An edge with no signal stays related_to.
   Guessing a type would be worse than the honest undifferentiated edge.
3. **Dry run by default.**  --apply is required to write, and --apply first
   takes a consistent SQLite backup via the backup API (the ledger runs in WAL
   mode, so copying the main file alone silently loses recent commits).
4. **Direction is never rewritten.**  The reclassified edge keeps its
   parent/child order; only relation_type and the derived relation_id change.
   Symmetric duplicates are *not* collapsed -- deleting an edge is data loss,
   and entity_graph already merges both directions.
5. **Every change is auditable.**  Each rewritten edge appends a
   relation_reclassify event to ledger_events recording the old type, the new
   type and the reason, so the decision can be reviewed after the fact.
6. **Measure before applying.**  Rewriting hundreds of edges changes the
   ORDER produced by causal_ancestors / entity_graph, and that order is
   consumed by the GUI and by the Context Pack.  Take a before/after
   associative-expansion comparison on a ledger copy first; --apply prints
   this reminder at runtime but deliberately does not block.

Signal priority (strongest evidence first)
-----------------------------------------
supersedes    a lifecycle fact (memories.superseded_by), not an inference
same_subject  the two memories share >=1 active claim subject
same_task     same non-empty task_id and no shared subject
derived_from  the child evidence/metadata explicitly points at the parent
related_to    nothing above applies -> keep the edge exactly as it is

same_subject outranks derived_from deliberately: on the production ledger only
8 of 2,023 edges carry a memory:<parent> evidence pointer, while 585 share a
claim subject, so the structural signal is the one that actually covers this
corpus.  Use --priority to inspect the alternative ordering.

Usage:
    python3 scripts/relation_reclassify.py                 # dry run (default)
    python3 scripts/relation_reclassify.py --json
    python3 scripts/relation_reclassify.py --apply         # backup + write
    python3 scripts/relation_reclassify.py --apply --backup-dir /path/backups

Exit codes: 0 success, 1 write/backup failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger.graph import (  # noqa: E402
    RELATION_DERIVED_FROM,
    RELATION_RELATED_TO,
    RELATION_SAME_SUBJECT,
    RELATION_SAME_TASK,
    RELATION_SUPERSEDES,
    classify_relation_type,
    relation_id_for,
    relation_weight,
)

DEFAULT_LEDGER = os.path.expanduser(
    "~/Library/Application Support/HippoMind/hippomind.sqlite")

#: Classification priority.  The default order is the documented contract above;
#: --priority exists so an operator can measure the alternative on their own
#: corpus instead of arguing about it.
DEFAULT_PRIORITY = (RELATION_SUPERSEDES, RELATION_SAME_SUBJECT,
                    RELATION_SAME_TASK, RELATION_DERIVED_FROM)
PRIORITIES = {
    "default": DEFAULT_PRIORITY,
    "evidence-first": (RELATION_SUPERSEDES, RELATION_DERIVED_FROM,
                       RELATION_SAME_SUBJECT, RELATION_SAME_TASK),
}

#: SQLite default variable ceiling is 999; batch every IN (...) query.
_CHUNK = 400


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open the ledger read-only.

    Deliberately **not** SQLiteLedger: its constructor bootstraps the schema
    (PRAGMA journal_mode=WAL + CREATE TABLE IF NOT EXISTS), which is a write.
    A dry run against the production ledger must not write anything.
    """
    uri = "file:%s?mode=ro" % str(path)
    conn = sqlite3.connect(uri, uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def _open_write(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _batched(values: Sequence[str], size: int = _CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def load_edge_rows(conn: sqlite3.Connection, *, limit: int = 0) -> List[sqlite3.Row]:
    """Every relation row, in rowid order (= deterministic plan order)."""
    sql = ("SELECT rowid AS rid, relation_id, parent_memory_id, child_memory_id, "
           "relation_type, confidence FROM memory_relations ORDER BY rowid")
    if limit and limit > 0:
        sql += " LIMIT %d" % int(limit)
    return conn.execute(sql).fetchall()


def load_memory_facts(conn: sqlite3.Connection,
                      memory_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Only the columns the classifier needs, only for memories on an edge."""
    facts: Dict[str, Dict[str, Any]] = {}
    for batch in _batched(list(dict.fromkeys(memory_ids))):
        marks = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT memory_id, task_id, superseded_by, evidence_json, metadata_json "
            "FROM memories WHERE memory_id IN (" + marks + ")",
            tuple(batch)).fetchall()
        for row in rows:
            facts[str(row["memory_id"])] = {
                "task_id": str(row["task_id"] or ""),
                "superseded_by": str(row["superseded_by"] or ""),
                "evidence": str(row["evidence_json"] or ""),
                "metadata": str(row["metadata_json"] or ""),
            }
    return facts


def load_claim_subjects(conn: sqlite3.Connection,
                        memory_ids: Sequence[str]) -> Dict[str, set]:
    """memory_id -> lower-cased active/conflict claim subjects.

    conflict is included because a contested claim still proves the two memories
    are talking about the same entity; excluding it would make the strongest
    structural signal disappear exactly where it matters most.
    """
    subjects: Dict[str, set] = {}
    for batch in _batched(list(dict.fromkeys(memory_ids))):
        marks = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT memory_id, subject FROM memory_claims "
            "WHERE memory_id IN (" + marks + ") AND state IN ('active','conflict')",
            tuple(batch)).fetchall()
        for row in rows:
            subject = str(row["subject"] or "").strip().lower()
            if subject:
                subjects.setdefault(str(row["memory_id"]), set()).add(subject)
    return subjects


def edge_signals(parent_id: str, child_id: str, *,
                 facts: Dict[str, Dict[str, Any]],
                 subjects: Dict[str, set]) -> Dict[str, Any]:
    """Evidence for one edge, in the form the classifier consumes."""
    parent = facts.get(parent_id, {})
    child = facts.get(child_id, {})
    parent_superseded_by = str(parent.get("superseded_by") or "")
    child_superseded_by = str(child.get("superseded_by") or "")
    shared = len(subjects.get(parent_id, set()) & subjects.get(child_id, set()))
    same_task = (bool(parent.get("task_id"))
                 and parent.get("task_id") == child.get("task_id"))
    # Derivation is only provable from the child side: the child names the parent
    # it was built from.  The reverse direction would be a different claim.
    child_evidence = str(child.get("evidence") or "")
    child_metadata = str(child.get("metadata") or "")
    derived = (("memory:" + parent_id) in child_evidence
               or ("consolidated_from" in child_metadata
                   and parent_id in child_metadata))
    return {
        "supersedes": (child_superseded_by == parent_id
                       or parent_superseded_by == child_id),
        "supersedes_direction": (
            "parent->child" if child_superseded_by == parent_id else
            "child->parent" if parent_superseded_by == child_id else ""),
        "derived": derived,
        "shared_subjects": shared,
        "same_task": same_task,
    }


def classify(edge_type: str, signals: Dict[str, Any], *,
             priority: Sequence[str] = DEFAULT_PRIORITY) -> Tuple[str, str]:
    """Pick the target type for one edge plus a human-readable reason.

    Returns (edge_type, ...) -- i.e. "no change" -- whenever the edge is already
    typed or no signal fires.  The caller never special-cases "keep": that is
    just another classification outcome.
    """
    if edge_type != RELATION_RELATED_TO:
        return edge_type, "已是显式类型，不参与重分类"
    for candidate in priority:
        if candidate == RELATION_SUPERSEDES and signals.get("supersedes"):
            _, reason = classify_relation_type(supersedes=True)
            return RELATION_SUPERSEDES, reason + "（" + str(
                signals.get("supersedes_direction", "")) + "）"
        if candidate == RELATION_DERIVED_FROM and signals.get("derived"):
            _, reason = classify_relation_type(derived=True)
            return RELATION_DERIVED_FROM, reason
        if (candidate == RELATION_SAME_SUBJECT
                and int(signals.get("shared_subjects") or 0) > 0):
            _, reason = classify_relation_type(
                shared_subjects=int(signals.get("shared_subjects") or 0))
            return RELATION_SAME_SUBJECT, reason
        if candidate == RELATION_SAME_TASK and signals.get("same_task"):
            _, reason = classify_relation_type(same_task=True)
            return RELATION_SAME_TASK, reason
    return RELATION_RELATED_TO, "无证据，保持 related_to"


def build_plan(conn: sqlite3.Connection, *,
               priority: Sequence[str] = DEFAULT_PRIORITY,
               limit: int = 0) -> Dict[str, Any]:
    """Read-only: what would change, with the evidence behind each decision."""
    edges = load_edge_rows(conn, limit=limit)
    memory_ids: List[str] = []
    for row in edges:
        memory_ids.append(str(row["parent_memory_id"]))
        memory_ids.append(str(row["child_memory_id"]))
    facts = load_memory_facts(conn, memory_ids)
    subjects = load_claim_subjects(conn, memory_ids)
    changes: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    untouched = 0
    counts: Dict[str, int] = {}
    for row in edges:
        parent_id = str(row["parent_memory_id"])
        child_id = str(row["child_memory_id"])
        old_type = str(row["relation_type"] or "")
        signals = edge_signals(parent_id, child_id, facts=facts, subjects=subjects)
        new_type, reason = classify(old_type, signals, priority=priority)
        if old_type != RELATION_RELATED_TO:
            untouched += 1
            continue
        record = {
            "relation_id": str(row["relation_id"]),
            "parent_memory_id": parent_id,
            "child_memory_id": child_id,
            "old_type": old_type,
            "new_type": new_type,
            "reason": reason,
            "confidence": round(float(row["confidence"] or 0.5), 3),
            "signals": {"supersedes": bool(signals["supersedes"]),
                        "derived": bool(signals["derived"]),
                        "shared_subjects": int(signals["shared_subjects"]),
                        "same_task": bool(signals["same_task"])},
        }
        counts[new_type] = counts.get(new_type, 0) + 1
        if new_type == old_type:
            kept.append(record)
        else:
            record["new_relation_id"] = relation_id_for(parent_id, child_id, new_type)
            record["weight_before"] = relation_weight(old_type)
            record["weight_after"] = relation_weight(new_type)
            changes.append(record)
    return {
        "edges": len(edges),
        "already_typed": untouched,
        "related_to": len(edges) - untouched,
        "changes": changes,
        "kept": kept,
        "by_target": counts,
    }


def _backup(conn: sqlite3.Connection, path: Path, backup_dir: Path) -> Path:
    """Consistent snapshot through SQLite online backup API (WAL-safe)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / (path.stem + "-pre-reclassify-" + stamp + ".sqlite")
    if target.exists():
        raise SystemExit("备份目标已存在，拒绝覆盖: %s" % target)
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def _audit_event(conn: sqlite3.Connection, change: Dict[str, Any], now: str) -> str:
    """Append one immutable ledger event describing the rewrite."""
    payload = {"relation_id": change["relation_id"],
               "new_relation_id": change["new_relation_id"],
               "parent_memory_id": change["parent_memory_id"],
               "child_memory_id": change["child_memory_id"],
               "old_type": change["old_type"],
               "new_type": change["new_type"],
               "reason": change["reason"],
               "signals": change["signals"]}
    digest = hashlib.sha256(
        ("relation_reclassify:" + json.dumps(payload, sort_keys=True,
                                             ensure_ascii=False)).encode("utf-8")
    ).hexdigest()
    event_id = "evt-" + digest[:24]
    conn.execute("""
        INSERT OR IGNORE INTO ledger_events(
            event_id,event_type,occurred_at,ingested_at,source_agent,session_id,
            task_id,project,role,scope,evidence_json,content,content_hash,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (event_id, "relation_reclassify", now, now, "relation-reclassify", "", "", "", "",
          "shared",
          json.dumps(["memory:" + change["parent_memory_id"],
                      "memory:" + change["child_memory_id"]]),
          "",
          "sha256:" + digest,
          json.dumps(payload, ensure_ascii=False, sort_keys=True)))
    return event_id


def apply_plan(conn: sqlite3.Connection,
               changes: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Rewrite each planned edge inside ONE transaction.

    Insert-then-delete (never delete-then-insert): if anything fails mid-way the
    rollback leaves the original related_to edge in place, so the worst case is
    "nothing happened" rather than "an edge vanished".
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    created = 0
    removed = 0
    audited = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for change in changes:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO memory_relations(
                    relation_id,parent_memory_id,child_memory_id,relation_type,
                    confidence,created_at
                ) VALUES(?,?,?,?,?,?)
            """, (change["new_relation_id"], change["parent_memory_id"],
                  change["child_memory_id"], change["new_type"],
                  float(change["confidence"]), now))
            created += 1 if cursor.rowcount else 0
            gone = conn.execute(
                "DELETE FROM memory_relations WHERE relation_id=? AND relation_type=?",
                (change["relation_id"], change["old_type"]))
            removed += 1 if gone.rowcount else 0
            _audit_event(conn, change, now)
            audited += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"inserted": created, "deleted": removed, "audited": audited}


def render_report(plan: Dict[str, Any], *, samples: int = 8) -> str:
    lines: List[str] = []
    lines.append("关系重分类报告（dry-run，未修改账本）")
    lines.append("  关系总数              %d" % plan["edges"])
    lines.append("  已是显式类型（跳过）  %d" % plan["already_typed"])
    lines.append("  related_to（待判）    %d" % plan["related_to"])
    lines.append("  将改写                %d" % len(plan["changes"]))
    lines.append("  无证据保持原样        %d" % len(plan["kept"]))
    if plan["by_target"]:
        lines.append("  目标类型分布：")
        for name in sorted(plan["by_target"]):
            lines.append("    %-14s %d" % (name, plan["by_target"][name]))
    if plan["changes"]:
        lines.append("")
        lines.append("  样例（最多 %d 条）：" % samples)
        for change in plan["changes"][:samples]:
            lines.append("    %s -> %s  %s" % (
                change["old_type"], change["new_type"], change["reason"]))
            lines.append("      %s .. %s  signals=%s" % (
                change["parent_memory_id"][-8:], change["child_memory_id"][-8:],
                json.dumps(change["signals"], ensure_ascii=False, sort_keys=True)))
    if plan["kept"]:
        lines.append("")
        lines.append("  保持 related_to 的样例（最多 %d 条，无证据不动）：" % samples)
        for record in plan["kept"][:samples]:
            lines.append("      %s .. %s  signals=%s" % (
                record["parent_memory_id"][-8:], record["child_memory_id"][-8:],
                json.dumps(record["signals"], ensure_ascii=False, sort_keys=True)))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="把存量 related_to 关系按证据重分类（默认 dry-run）")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--apply", action="store_true",
                        help="写入账本（会先用 SQLite backup API 备份）")
    parser.add_argument("--backup-dir", default=str(ROOT / "backups"),
                        help="--apply 时的备份目录（默认 <repo>/backups）")
    parser.add_argument("--priority", choices=sorted(PRIORITIES), default="default",
                        help="分类优先级；默认 same_subject > same_task > derived_from")
    parser.add_argument("--limit", type=int, default=0,
                        help="只处理前 N 条关系（调试用，默认全部）")
    parser.add_argument("--samples", type=int, default=8, help="报告里打印的样例数")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    path = Path(args.ledger).expanduser()
    if not path.is_file():
        print("error: ledger not found: %s" % path, file=sys.stderr)
        return 2
    priority = PRIORITIES[args.priority]
    read_conn = _open_readonly(path)
    try:
        plan = build_plan(read_conn, priority=priority, limit=args.limit)
    finally:
        read_conn.close()
    payload: Dict[str, Any] = {
        "ledger": str(path),
        "dry_run": not args.apply,
        "priority": list(priority),
        "edges": plan["edges"],
        "already_typed": plan["already_typed"],
        "related_to": plan["related_to"],
        "planned_changes": len(plan["changes"]),
        "kept_as_related_to": len(plan["kept"]),
        "by_target": plan["by_target"],
        "changes": plan["changes"],
        "kept": plan["kept"],
    }
    if not args.apply:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(render_report(plan, samples=args.samples))
            print("")
            print("   DRY-RUN：未修改账本。加 --apply 才会写入（会先自动备份）。")
        return 0
    if not plan["changes"]:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("没有需要重分类的关系（账本已收敛）。")
        return 0
    # 运行时提示（不是拦阻）：重分类会改变 causal_ancestors / entity_graph
    # 的排序，而这两个接口正被 GUI 和 Context Pack 消费。建议先在账本副本上
    # 跑一次改前/改后的联想扩展对比，再对生产库执行。
    print("注意：--apply 会改变因果/图谱扩展的排序。建议先在账本副本上",
          file=sys.stderr)
    print("      跑一次改前/改后的联想扩展对比再执行。", file=sys.stderr)
    write_conn = _open_write(path)
    try:
        backup_path = _backup(write_conn, path, Path(args.backup_dir).expanduser())
        payload["backup"] = str(backup_path)
        outcome = apply_plan(write_conn, plan["changes"])
        payload["applied"] = outcome
    finally:
        write_conn.close()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_report(plan, samples=args.samples))
        print("")
        print("   已备份 -> %s" % payload["backup"])
        print("   已写入：新增 %d 条、删除 %d 条、审计事件 %d 条" % (
            outcome["inserted"], outcome["deleted"], outcome["audited"]))
        print("   回滚：把备份文件复制回 %s 即可（或用 hippomind restore）" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
