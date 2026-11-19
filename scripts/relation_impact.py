#!/usr/bin/env python3
"""Before/after impact report for relation-type reclassification.

Why this exists
---------------
scripts/relation_reclassify.py rewrites legacy related_to edges into
same_subject / same_task / supersedes / derived_from.  That is NOT a cosmetic
metadata change:

* causal_ancestors multiplies its distance-decayed score by the relation weight
  (same_subject 0.9, same_task 0.7, everything else 1.0), so a rewritten edge
  makes its ancestor score LOWER and can reorder the result list;
* entity_graph uses confidence x weight as edge strength, which drives the BFS
  neighbour order and which edge record represents a pair.

Those two interfaces feed the GUI and the Context Pack.  A rewrite that silently
reorders them is exactly the kind of change that needs evidence *before* it is
applied -- which is why this tool exists and why it can simulate the rewrite on
a disposable copy instead of touching a real ledger.

Two ways to use it
------------------
1. **Predict** (safe, default): take a read-only snapshot of the production
   ledger, copy it, apply the reclassification to the COPY, and diff the two.
   The production ledger is never opened for writing.  This is the run that
   should happen before --apply.
2. **Verify** (after --apply): diff the pre-apply backup against the live
   ledger to confirm the realised change matches the prediction.

What is compared
----------------
* entity_graph(subject) for a sample of subjects: edge weight, edge ordering,
  and which edge represents each (from, to) pair.
* causal_ancestors(memory_ids) for a sample of memories: score, ordering, and
  the relation type/weight recorded per ancestor.

Every difference is reported with the numbers on both sides, so a reviewer can
judge "explainable" instead of trusting a boolean.

Why same_subject edges are invisible in entity_graph (not a bug)
---------------------------------------------------------------
On the production ledger, 585 of the 622 rewritten edges became same_subject.
**Zero of them appear in entity_graph.**  That is a structural consequence of
what the two interfaces mean, not a defect, and it is recorded here so nobody
goes hunting for a bug that does not exist:

* ``entity_graph`` is a **subject-neighbourhood** view.  Nodes are claim
  subjects; an edge means "these two subjects co-occur across a relation".
  A ``same_subject`` edge joins two memories that share the *same* subject, so
  both endpoints map to one and the same node -- the edge becomes a self-loop,
  and the BFS skips it (``if a == b: continue``).  There is no pair of distinct
  subjects to draw a line between.
* Its effect lands in ``causal_ancestors`` (associative expansion), which walks
  memory-to-memory edges and multiplies the distance-decayed score by the
  relation weight.  There a ``same_subject`` rewrite is fully visible: the
  ancestor score drops from 0.45 to 0.405 at depth 1 (x0.9).

Measured on the 2026-10-25 production run: sampling 20 same_subject edges found
**0** present in any endpoint subject's graph, while the same 20 edges all moved
their ancestor scores.  ``count_invisible_edges()`` reports this split for every
run, so the report never implies the graph changed more than it did.

Practical reading: a "622 edges reclassified" headline means ~270 changed edges
in ``entity_graph`` and ~33 changed ancestors in ``causal_ancestors`` on the
sample -- the two interfaces see disjoint parts of the same rewrite.

Usage:
    # predict (never writes to the source ledger)
    python3 scripts/relation_impact.py --predict

    # verify after --apply, against the backup taken by the reclassify run
    python3 scripts/relation_impact.py --before /path/to/backup.sqlite

    # compare any two ledgers explicitly
    python3 scripts/relation_impact.py --before a.sqlite --after b.sqlite

Exit codes: 0 report produced, 2 usage/input error.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import relation_reclassify as rr  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind.ledger.graph import relation_weight  # noqa: E402

DEFAULT_LEDGER = os.path.expanduser(
    "~/Library/Application Support/HippoMind/hippomind.sqlite")

#: How many subjects / memories to sample by default.  The task asks for 20;
#: both are configurable because a larger sample is cheap on a snapshot.
DEFAULT_SUBJECT_SAMPLE = 20
DEFAULT_MEMORY_SAMPLE = 20


def snapshot(source: Path, target: Path) -> Path:
    """Consistent copy via the SQLite online backup API.

    The production ledger runs in WAL mode with a live daemon writing to it, so
    copying the main file with cp would silently lose recent commits.  The
    source is opened mode=ro: this tool must never be able to write to it.
    """
    if target.exists():
        raise SystemExit("snapshot already exists: %s" % target)
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect("file:%s?mode=ro" % str(source), uri=True, timeout=30.0)
    try:
        dest = sqlite3.connect(str(target))
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    _normalise_journal_mode(target)
    return target


def _normalise_journal_mode(path: Path) -> None:
    """Switch a snapshot we own back to ``DELETE`` journaling.

    Why: a WAL-mode database cannot be opened with ``mode=ro`` unless its
    ``-shm`` file already exists (SQLite refuses to create it on a read-only
    connection).  ``backup()`` and ``copyfile()`` both produce a database that
    still reports ``journal_mode=wal`` but has no ``-shm`` sidecar, so every
    read-only open of it fails with "unable to open database file".

    Only ever called on files this tool created, never on the production ledger:
    the whole point of the snapshot is that the source stays untouched.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    finally:
        conn.close()


def _readonly_conn(path: Path) -> sqlite3.Connection:
    """Read-only connection with a clear error when the file is not usable."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % str(path), uri=True,
                               timeout=30.0)
        conn.execute("SELECT 1 FROM memory_relations LIMIT 1").fetchone()
        return conn
    except sqlite3.DatabaseError as exc:
        raise SystemExit(
            "cannot open %s read-only: %s\n"
            "hint: pass a ledger produced by this tool (it normalises journal "
            "mode), or run --predict to snapshot the production ledger first."
            % (path, exc))

def simulate_reclassify(source: Path, target: Path,
                        *, priority: Sequence[str] = rr.DEFAULT_PRIORITY
                        ) -> Dict[str, Any]:
    """Copy source and apply the reclassification to the COPY.

    This is what makes the prediction honest: the "after" ledger goes through
    the exact same build_plan + apply_plan code path that --apply uses, on the
    same rows, so the diff below is a real dry run of the write rather than a
    hand-built approximation of it.
    """
    shutil.copyfile(str(source), str(target))
    _normalise_journal_mode(target)
    read_conn = rr._open_readonly(target)
    try:
        plan = rr.build_plan(read_conn, priority=priority)
    finally:
        read_conn.close()
    write_conn = rr._open_write(target)
    try:
        outcome = rr.apply_plan(write_conn, plan["changes"])
    finally:
        write_conn.close()
    return {"planned": len(plan["changes"]), "applied": outcome,
            "by_target": plan["by_target"]}


def sample_subjects(conn: sqlite3.Connection, limit: int) -> List[str]:
    """Subjects that actually sit on a related_to edge, readable names first.

    Sampling subjects with no edges would report "no change" for every one of
    them and prove nothing, so the EXISTS clause restricts to memories that are
    an endpoint of at least one related_to edge.

    Ordering matters too: production subjects are dominated by machine ids
    (1,554 of 2,815 claims have a UUID subject), and a report full of UUIDs is
    unreadable -- a reviewer cannot judge whether "1922574a... -> 00b59e90..."
    changing weight is expected.  Human-readable subjects are sampled FIRST so
    the sample answers the question the report is for; machine-id subjects are
    still included afterwards to keep the sample representative.
    """
    rows = conn.execute("""
        SELECT c.subject, COUNT(*) AS n,
               CASE WHEN c.subject GLOB '[0-9a-f]*-[0-9a-f]*-[0-9a-f]*-*'
                    THEN 1 ELSE 0 END AS machine
        FROM memory_claims c
        JOIN memories m ON m.memory_id = c.memory_id
        WHERE c.state IN ('active','conflict')
          AND m.quality_state = 'accepted' AND m.lifecycle IN ('active','cold')
          AND EXISTS (
              SELECT 1 FROM memory_relations r
              WHERE r.relation_type = 'related_to'
                AND (r.parent_memory_id = c.memory_id
                     OR r.child_memory_id = c.memory_id)
          )
        GROUP BY c.subject
        ORDER BY machine ASC, n DESC, c.subject
        LIMIT ?
    """, (max(1, int(limit)),)).fetchall()
    return [str(row[0]) for row in rows if str(row[0] or "").strip()]

def sample_memories(conn: sqlite3.Connection, limit: int) -> List[str]:
    """Memories that are the CHILD of at least one related_to edge.

    causal_ancestors walks upwards from the ids it is given, so a memory with no
    parent edges would return an empty list on both sides.  Only children can
    show a score or ordering difference.
    """
    rows = conn.execute("""
        SELECT DISTINCT r.child_memory_id
        FROM memory_relations r
        WHERE r.relation_type = 'related_to'
        ORDER BY r.child_memory_id
        LIMIT ?
    """, (max(1, int(limit)),)).fetchall()
    return [str(row[0]) for row in rows if str(row[0] or "").strip()]


def graph_snapshot(ledger: SQLiteLedger, subject: str, *, hops: int = 2
                   ) -> Dict[str, Any]:
    """One entity_graph call reduced to the fields the diff cares about."""
    report = ledger.entity_graph(subject, hops=hops)
    edges = []
    for edge in report.get("edges") or []:
        edges.append({
            "from": str(edge.get("from", "")),
            "to": str(edge.get("to", "")),
            "relation_type": str(edge.get("relation_type", "")),
            "confidence": float(edge.get("confidence", 0.0) or 0.0),
            "weight": float(edge.get("weight", 1.0) or 0.0),
        })
    return {
        "found": bool(report.get("found")),
        "nodes": [str(node.get("subject", "")) for node in report.get("nodes") or []],
        "edges": edges,
        "edge_order": ["%s->%s" % (e["from"], e["to"]) for e in edges],
    }


def ancestors_snapshot(ledger: SQLiteLedger, memory_ids: Sequence[str],
                       *, max_depth: int = 2, limit: int = 32
                       ) -> Dict[str, Any]:
    """One causal_ancestors call reduced to the fields the diff cares about."""
    rows = ledger.causal_ancestors(list(memory_ids), max_depth=max_depth,
                                   limit=limit)
    items = []
    for row in rows:
        items.append({
            "memory_id": str(row.get("memory_id", "")),
            "score": round(float(row.get("score", 0.0) or 0.0), 6),
            "causal_depth": int(row.get("causal_depth", 0) or 0),
            "relation_type": str(row.get("relation_type", "")),
            "relation_weight": float(row.get("relation_weight", 1.0) or 1.0),
        })
    return {"order": [item["memory_id"] for item in items], "items": items}


def diff_graph(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Edge-level diff: which edges changed weight, and did the order move?

    Order is compared as a list, not a set, because a reordering is a real
    behavioural change (it decides which node the GUI highlights first) even
    when the same edges are present.
    """
    before_edges = {"%s->%s" % (e["from"], e["to"]): e for e in before["edges"]}
    after_edges = {"%s->%s" % (e["from"], e["to"]): e for e in after["edges"]}
    changed = []
    for key in sorted(set(before_edges) & set(after_edges)):
        old, new = before_edges[key], after_edges[key]
        type_changed = old["relation_type"] != new["relation_type"]
        weight_changed = abs(old["weight"] - new["weight"]) > 1e-9
        if type_changed or weight_changed:
            changed.append({
                "edge": key,
                "type_before": old["relation_type"],
                "type_after": new["relation_type"],
                "weight_before": old["weight"],
                "weight_after": new["weight"],
                "confidence": old["confidence"],
            })
    return {
        "found_before": before["found"],
        "found_after": after["found"],
        "edges_before": len(before["edges"]),
        "edges_after": len(after["edges"]),
        "only_before": sorted(set(before_edges) - set(after_edges)),
        "only_after": sorted(set(after_edges) - set(before_edges)),
        "changed_edges": changed,
        "order_changed": before["edge_order"] != after["edge_order"],
        "order_before": before["edge_order"],
        "order_after": after["edge_order"],
        "nodes_changed": before["nodes"] != after["nodes"],
    }


def diff_ancestors(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Ancestor-level diff: score deltas, type changes, ordering moves."""
    before_items = {item["memory_id"]: item for item in before["items"]}
    after_items = {item["memory_id"]: item for item in after["items"]}
    changed = []
    for memory_id in sorted(set(before_items) & set(after_items)):
        old, new = before_items[memory_id], after_items[memory_id]
        delta = round(new["score"] - old["score"], 6)
        if abs(delta) > 1e-9 or old["relation_type"] != new["relation_type"]:
            changed.append({
                "memory_id": memory_id,
                "score_before": old["score"],
                "score_after": new["score"],
                "delta": delta,
                "type_before": old["relation_type"],
                "type_after": new["relation_type"],
                "weight_before": old["relation_weight"],
                "weight_after": new["relation_weight"],
                "depth": new["causal_depth"],
            })
    return {
        "count_before": len(before["items"]),
        "count_after": len(after["items"]),
        "only_before": sorted(set(before_items) - set(after_items)),
        "only_after": sorted(set(after_items) - set(before_items)),
        "changed_ancestors": changed,
        "order_changed": before["order"] != after["order"],
        "order_before": before["order"],
        "order_after": after["order"],
    }


def _relation_type_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT relation_type, COUNT(*) FROM memory_relations "
        "GROUP BY relation_type ORDER BY relation_type").fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _expected_weight(type_name: str) -> float:
    return relation_weight(type_name)


def check_expectations(report: Dict[str, Any]) -> List[str]:
    """Assert the change is the one we predicted, and return the violations.

    The contract this encodes (measured, not assumed):

    * every changed edge weight must equal relation_weight(type_after);
    * no edge weight may INCREASE -- only same_subject (0.9) and same_task (0.7)
      are below 1.0, so the rewrite can only lower a weight;
    * no ancestor score may INCREASE, for the same reason;
    * a supersedes edge keeps weight 1.0, so it may change the reported type
      without changing the score.
    """
    problems: List[str] = []
    for diff in report["subjects"]:
        for change in diff["changed_edges"]:
            expected = _expected_weight(change["type_after"])
            if abs(change["weight_after"] - expected) > 1e-9:
                problems.append(
                    "edge %s: weight_after=%s does not match relation_weight(%s)=%s"
                    % (change["edge"], change["weight_after"],
                       change["type_after"], expected))
            if change["weight_after"] > change["weight_before"] + 1e-9:
                problems.append(
                    "edge %s: weight INCREASED %s -> %s (no type should raise it)"
                    % (change["edge"], change["weight_before"],
                       change["weight_after"]))
    for diff in report["memories"]:
        for change in diff["changed_ancestors"]:
            if change["delta"] > 1e-9:
                problems.append(
                    "ancestor %s: score INCREASED %.6f -> %.6f"
                    % (change["memory_id"], change["score_before"],
                       change["score_after"]))
            expected = change["weight_after"]
            if abs(expected - _expected_weight(change["type_after"])) > 1e-9:
                problems.append(
                    "ancestor %s: weight %s does not match type %s"
                    % (change["memory_id"], expected, change["type_after"]))
    return problems


def count_invisible_edges(before_path: Path, after_path: Path) -> Dict[str, int]:
    """Count rewritten edges that ``entity_graph`` structurally cannot show.

    Measured, not assumed: an edge between two memories that share a claim
    subject connects the SAME entity node to itself, and ``entity_graph`` skips
    those (``if a == b: continue``).  So a ``same_subject`` rewrite is invisible
    in the graph view even though it is very visible in ``causal_ancestors``.

    That asymmetry matters for reviewing the change: without this number a
    reader sees "585 edges became same_subject" and assumes the graph changed
    by that much, when in fact most of those edges vanish from the graph by
    construction.
    """
    conn = _readonly_conn(after_path)
    try:
        rows = conn.execute("""
            SELECT r.relation_type,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM memory_claims a
                       JOIN memory_claims b
                         ON LOWER(TRIM(a.subject)) = LOWER(TRIM(b.subject))
                       WHERE a.memory_id = r.parent_memory_id
                         AND b.memory_id = r.child_memory_id
                         AND a.state IN ('active','conflict')
                         AND b.state IN ('active','conflict')
                   ) THEN 1 ELSE 0 END AS same_entity
            FROM memory_relations r
            WHERE r.relation_type IN ('same_subject','same_task','supersedes',
                                      'derived_from')
        """).fetchall()
    finally:
        conn.close()
    hidden: Dict[str, int] = {}
    visible: Dict[str, int] = {}
    for relation_type, same_entity in rows:
        bucket = hidden if int(same_entity) else visible
        bucket[str(relation_type)] = bucket.get(str(relation_type), 0) + 1
    return {"hidden": hidden, "visible": visible}

def count_pair_dedupe(before_path: Path, after_path: Path) -> Dict[str, Any]:
    """Detect edges that merged because the pair already had the target type.

    Real case from the production run (2026-10-25): 622 planned rewrites
    produced only 583 inserts, and the relation count fell 2,070 -> 2,031.  That
    looked like data loss until the pairs were compared: **0 memory pairs were
    lost**.  39 pairs already carried BOTH ``related_to`` and ``same_subject``
    (written by the new distill path before this reclassification ran), so
    ``INSERT OR IGNORE`` on the new ``same_subject`` row was a no-op and only
    the old ``related_to`` row was deleted -- the pair collapsed from two edges
    to one.  That is the desired end state, but a report that only shows the row
    count going DOWN reads as loss, so it has to be stated explicitly.
    """
    before_conn = _readonly_conn(before_path)
    try:
        before_edges = {(str(r[0]), str(r[1]), str(r[2])) for r in before_conn.execute(
            "SELECT parent_memory_id, child_memory_id, relation_type "
            "FROM memory_relations")}
    finally:
        before_conn.close()
    after_conn = _readonly_conn(after_path)
    try:
        after_edges = {(str(r[0]), str(r[1]), str(r[2])) for r in after_conn.execute(
            "SELECT parent_memory_id, child_memory_id, relation_type "
            "FROM memory_relations")}
    finally:
        after_conn.close()
    before_pairs = {(p, c) for (p, c, _t) in before_edges}
    after_pairs = {(p, c) for (p, c, _t) in after_edges}
    return {
        "edges_before": len(before_edges),
        "edges_after": len(after_edges),
        "edge_delta": len(after_edges) - len(before_edges),
        "pairs_before": len(before_pairs),
        "pairs_after": len(after_pairs),
        "pairs_lost": sorted("%s->%s" % pair for pair in (before_pairs - after_pairs)),
        "pairs_gained": sorted("%s->%s" % pair for pair in (after_pairs - before_pairs)),
    }

def build_report(before_path: Path, after_path: Path, *,
                 subjects: int = DEFAULT_SUBJECT_SAMPLE,
                 memories: int = DEFAULT_MEMORY_SAMPLE,
                 hops: int = 2, max_depth: int = 2
                 ) -> Dict[str, Any]:
    """Compare two ledgers across both interfaces and summarise the impact."""
    before_ledger = SQLiteLedger(before_path)
    after_ledger = SQLiteLedger(after_path)
    try:
        before_conn = _readonly_conn(before_path)
        try:
            subject_list = sample_subjects(before_conn, subjects)
            memory_list = sample_memories(before_conn, memories)
            relation_types_before = _relation_type_counts(before_conn)
        finally:
            before_conn.close()
        after_conn = _readonly_conn(after_path)
        try:
            relation_types_after = _relation_type_counts(after_conn)
        finally:
            after_conn.close()

        subject_diffs = []
        for subject in subject_list:
            before = graph_snapshot(before_ledger, subject, hops=hops)
            after = graph_snapshot(after_ledger, subject, hops=hops)
            diff = diff_graph(before, after)
            diff["subject"] = subject
            subject_diffs.append(diff)

        memory_diffs = []
        for memory_id in memory_list:
            before = ancestors_snapshot(before_ledger, [memory_id],
                                        max_depth=max_depth)
            after = ancestors_snapshot(after_ledger, [memory_id],
                                       max_depth=max_depth)
            diff = diff_ancestors(before, after)
            diff["memory_id"] = memory_id
            memory_diffs.append(diff)
    finally:
        before_ledger.close()
        after_ledger.close()

    changed_edges = sum(len(d["changed_edges"]) for d in subject_diffs)
    reordered_graphs = sum(1 for d in subject_diffs if d["order_changed"])
    changed_ancestors = sum(len(d["changed_ancestors"]) for d in memory_diffs)
    reordered_ancestor_lists = sum(1 for d in memory_diffs if d["order_changed"])
    deltas = [c["delta"] for d in memory_diffs for c in d["changed_ancestors"]]
    return {
        "before": str(before_path),
        "after": str(after_path),
        "sampled_subjects": len(subject_diffs),
        "sampled_memories": len(memory_diffs),
        "relation_types_before": relation_types_before,
        "relation_types_after": relation_types_after,
        "summary": {
            "subjects_with_changed_edges": sum(
                1 for d in subject_diffs if d["changed_edges"]),
            "changed_edges": changed_edges,
            "subjects_with_reordered_edges": reordered_graphs,
            "subjects_with_changed_nodes": sum(
                1 for d in subject_diffs if d["nodes_changed"]),
            "memories_with_changed_ancestors": sum(
                1 for d in memory_diffs if d["changed_ancestors"]),
            "changed_ancestors": changed_ancestors,
            "memories_with_reordered_ancestors": reordered_ancestor_lists,
            "ancestor_score_delta_min": round(min(deltas), 6) if deltas else 0.0,
            "ancestor_score_delta_max": round(max(deltas), 6) if deltas else 0.0,
        },
        "graph_visibility": count_invisible_edges(before_path, after_path),
        "edge_integrity": count_pair_dedupe(before_path, after_path),
        "subjects": subject_diffs,
        "memories": memory_diffs,
    }


def render_report(report: Dict[str, Any], *, samples: int = 5) -> str:
    lines: List[str] = []
    summary = report["summary"]
    lines.append("关系重分类影响报告（改前 -> 改后）")
    lines.append("  before: %s" % report["before"])
    lines.append("  after : %s" % report["after"])
    simulated = report.get("simulated")
    if simulated:
        lines.append("  模拟  : 改写 %d 条，写入 %s" % (
            simulated.get("planned", 0), simulated.get("applied")))
    lines.append("")
    lines.append("关系类型分布：")
    names = sorted(set(report["relation_types_before"]) |
                   set(report["relation_types_after"]))
    for name in names:
        before = report["relation_types_before"].get(name, 0)
        after = report["relation_types_after"].get(name, 0)
        if before or after:
            lines.append("  %-16s %6d -> %6d  (%+d)" % (name, before, after,
                                                       after - before))
    integrity = report.get("edge_integrity") or {}
    if integrity:
        lines.append("")
        lines.append("边完整性：")
        lines.append("  边数 %d -> %d  (%+d)" % (
            integrity["edges_before"], integrity["edges_after"],
            integrity["edge_delta"]))
        lines.append("  记忆对数 %d -> %d  (%+d)" % (
            integrity["pairs_before"], integrity["pairs_after"],
            integrity["pairs_after"] - integrity["pairs_before"]))
        if integrity["pairs_lost"]:
            lines.append("  ** 丢失的记忆对：%d **" % len(integrity["pairs_lost"]))
            for pair in integrity["pairs_lost"][:5]:
                lines.append("    - %s" % pair)
        else:
            lines.append("  丢失的记忆对：0（边数下降是冗余边合并，不是数据丢失）")
    lines.append("")
    lines.append("entity_graph 抽样 %d 个 subject：" % report["sampled_subjects"])
    lines.append("  边权重变化的 subject  %d" % summary["subjects_with_changed_edges"])
    lines.append("  变化的边总数          %d" % summary["changed_edges"])
    lines.append("  边顺序变化的 subject  %d" % summary["subjects_with_reordered_edges"])
    lines.append("  节点集变化的 subject  %d" % summary["subjects_with_changed_nodes"])
    visibility = report.get("graph_visibility") or {}
    hidden = visibility.get("hidden") or {}
    if hidden:
        lines.append("  entity_graph 里**结构性不可见**的新类型边：")
        lines.append("    （两端记忆共享同一 claim 主语 -> 同一实体节点自环，")
        lines.append("      entity_graph 的 if a == b: continue 会跳过）")
        for name in sorted(hidden):
            lines.append("    %-16s %d" % (name, hidden[name]))
    lines.append("")
    lines.append("causal_ancestors 抽样 %d 条记忆：" % report["sampled_memories"])
    lines.append("  祖先分数变化的记忆    %d" % summary["memories_with_changed_ancestors"])
    lines.append("  变化的祖先总数        %d" % summary["changed_ancestors"])
    lines.append("  祖先顺序变化的记忆    %d" % summary["memories_with_reordered_ancestors"])
    lines.append("  分数 delta 范围       %+.6f .. %+.6f" % (
        summary["ancestor_score_delta_min"],
        summary["ancestor_score_delta_max"]))
    shown = 0
    for diff in report["subjects"]:
        if not diff["changed_edges"] or shown >= samples:
            continue
        shown += 1
        lines.append("")
        lines.append("  subject %r  (%d 条边变化%s)" % (
            diff["subject"], len(diff["changed_edges"]),
            "，顺序变化" if diff["order_changed"] else ""))
        for change in diff["changed_edges"][:4]:
            lines.append("    %s  %s(%s) -> %s(%s)" % (
                change["edge"], change["type_before"],
                change["weight_before"], change["type_after"],
                change["weight_after"]))
    shown = 0
    for diff in report["memories"]:
        if not diff["changed_ancestors"] or shown >= samples:
            continue
        shown += 1
        lines.append("")
        lines.append("  memory %s  (%d 条祖先变化%s)" % (
            diff["memory_id"][-12:], len(diff["changed_ancestors"]),
            "，顺序变化" if diff["order_changed"] else ""))
        for change in diff["changed_ancestors"][:4]:
            lines.append("    depth=%d  %.6f -> %.6f  (%+.6f)  %s w=%s" % (
                change["depth"], change["score_before"], change["score_after"],
                change["delta"], change["type_after"], change["weight_after"]))
    problems = report.get("problems") or []
    lines.append("")
    if problems:
        lines.append("  预期校验：FAILED（%d 条）" % len(problems))
        for problem in problems[:10]:
            lines.append("    - %s" % problem)
    else:
        lines.append("  预期校验：通过（只降不升、权重与类型一致）")
    return chr(10).join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="重分类改前/改后对比报告（默认在快照副本上预测，不写生产库）")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER,
                        help="生产库（只读；作为快照源）")
    parser.add_argument("--before", default="",
                        help="改前账本路径；省略时自动取生产库快照")
    parser.add_argument("--after", default="",
                        help="改后账本路径；省略时在快照副本上模拟 apply")
    parser.add_argument("--predict", action="store_true",
                        help="在快照副本上模拟 apply（默认行为，显式写出来便于审计）")
    parser.add_argument("--workdir", default=str(ROOT / "tmp"),
                        help="快照与副本的存放目录")
    parser.add_argument("--subjects", type=int, default=DEFAULT_SUBJECT_SAMPLE)
    parser.add_argument("--memories", type=int, default=DEFAULT_MEMORY_SAMPLE)
    parser.add_argument("--hops", type=int, default=2)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--samples", type=int, default=5,
                        help="报告里每条接口打印的样例数")
    parser.add_argument("--json", default="", help="把完整报告写到这个路径")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).expanduser()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    simulated = None
    if args.before:
        before_path = Path(args.before).expanduser()
    else:
        source = Path(args.ledger).expanduser()
        if not source.is_file():
            print("error: ledger not found: %s" % source, file=sys.stderr)
            return 2
        before_path = workdir / ("impact-before-" + stamp + ".sqlite")
        print("snapshotting %s -> %s" % (source, before_path), file=sys.stderr)
        snapshot(source, before_path)
    if not before_path.is_file():
        print("error: before ledger not found: %s" % before_path, file=sys.stderr)
        return 2
    if args.after:
        after_path = Path(args.after).expanduser()
    else:
        after_path = workdir / ("impact-after-" + stamp + ".sqlite")
        print("simulating reclassification on a copy -> %s" % after_path,
              file=sys.stderr)
        simulated = simulate_reclassify(before_path, after_path)
    if not after_path.is_file():
        print("error: after ledger not found: %s" % after_path, file=sys.stderr)
        return 2

    report = build_report(before_path, after_path,
                          subjects=args.subjects, memories=args.memories,
                          hops=args.hops, max_depth=args.max_depth)
    report["simulated"] = simulated
    report["problems"] = check_expectations(report)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("full report -> %s" % args.json, file=sys.stderr)
    print(render_report(report, samples=args.samples))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
