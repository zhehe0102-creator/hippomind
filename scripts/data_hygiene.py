#!/usr/bin/env python3
"""Data hygiene: find and retire memories that should never have been stored.

The production ledger accumulated two independent classes of garbage, and they
need different remedies because they damage retrieval in different ways.

Class 1 -- noise (self-referential shells)
    The distiller's own prompt captured as conversation, structurally empty
    extraction results ({"claims":[]}), and bare fragments ("Run:").  These are
    never useful, but on this ledger only a minority are even visible to the
    evaluation: eval_memory.py searches with include_raw_chat=False, and most of
    the prompt pollution is raw_chat=1.  Cleaning them is hygiene, not a fix for
    the duplicate-rate gate.

Class 2 -- duplicates (the real cause of the gate failure)
    The same normalised content stored many times over.  On 2026-10-24 one
    sentence had 141 copies, all sharing one evidence id but 141 different
    metadata blobs: the ledger identity key includes mutable metadata, so every
    re-observation became a new row.  Because a top-k list then contains the
    same text several times, duplicate_rate rises even though recall is fine.
    Retiring the redundant copies is what actually moves the metric.

Why degrade instead of DELETE
    Every row here is evidence-linked.  A physical DELETE would break the
    append-only audit trail and make the decision unrecoverable.  Degrading
    quality_state to 'quarantine' removes the row from retrieval (search.py
    filters quality_state='accepted') while keeping it queryable for audit, and
    -- unlike 'cold', which search still returns by default -- it actually stops
    the duplication.  Every changed memory_id is written to a rollback file
    before the write, so the operation is reversible with one UPDATE.

Usage
    python3 scripts/data_hygiene.py                    # dry-run, default
    python3 scripts/data_hygiene.py --apply
    python3 scripts/data_hygiene.py --category duplicates --apply
    python3 scripts/data_hygiene.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.distill import classify_unusable  # noqa: E402
from hippomind.textutil import content_fingerprint  # noqa: E402

#: The state a retired row is moved to.  Chosen over 'cold' because
#: search.py filters quality_state='accepted' but still returns cold rows by
#: default (include_cold=True), so degrading to cold changes nothing measurable.
DEGRADED_STATE = "quarantine"

#: Row classes this tool understands.  Each maps to one detection predicate.
CATEGORIES = ("noise", "duplicates")


def default_ledger_path() -> Path:
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "hippomind.sqlite"


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


class HygieneScanner:
    """Read-only scanner plus an explicit, auditable apply step."""

    def __init__(self, path: os.PathLike):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError("账本不存在: %s" % self.path)
        self._conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
        except sqlite3.DatabaseError:
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "HygieneScanner":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- live population ---------------------------------------------------

    def live_rows(self) -> List[Dict[str, Any]]:
        """The working population: accepted AND active.

        Restricting to lifecycle='active' is not an optimisation, it is the
        difference between hygiene and data loss.  253,522 of the 260,971
        accepted rows are *cold* -- evidence retired by retention.  They are
        legitimately repetitive (the same archive text re-appears across
        sessions) and they are not in the retrieval pool that matters, so a
        scanner that included them classified 162k archived rows as
        "duplicates" and would have mass-quarantined the archive on --apply.

        The first dry-run of this script did exactly that; the absurd count
        (163,622 targets) is what caught it.  Keep this predicate aligned with
        the one DistillEngine._candidates and scripts/distill_status.py use.
        """
        rows = self._conn.execute(
            "SELECT memory_id, content, kind, source_agent, project, task_id, "
            "session_id, created_at, lifecycle, raw_chat FROM memories "
            "WHERE quality_state='accepted' AND lifecycle='active'").fetchall()
        return [dict(row) for row in rows]

    # -- detection ---------------------------------------------------------

    def find_noise(self, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rows the content gate would refuse today.

        Reuses hippomind.distill.classify_unusable so the scanner and the ingest
        gate can never disagree about what "noise" means -- the exact drift that
        let 476 such rows accumulate while a filter "existed".
        """
        found: List[Dict[str, Any]] = []
        for row in rows:
            reason = classify_unusable(str(row.get("content") or ""))
            if reason:
                found.append(dict(row, reason=reason, category="noise"))
        return found

    def find_duplicates(self, rows: Sequence[Dict[str, Any]],
                        *, exclude: Optional[set] = None) -> List[Dict[str, Any]]:
        """Redundant copies of content that is already represented.

        Keeps the *newest* row per normalised content and retires the rest.  The
        newest is kept because recency is a ranking signal: retiring it would
        make the surviving copy look stale and drop out of top-k for reasons
        unrelated to the cleanup.

        Rows already classified as noise are excluded, so a row is never counted
        twice and the two categories stay independently auditable.
        """
        exclude = exclude or set()
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            if str(row.get("memory_id")) in exclude:
                continue
            content = str(row.get("content") or "")
            if not content.strip():
                continue
            groups.setdefault(content_fingerprint(content), []).append(row)
        found: List[Dict[str, Any]] = []
        for _fingerprint, members in groups.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            keeper = members[0]
            for redundant in members[1:]:
                found.append(dict(redundant, category="duplicates",
                                  reason="duplicate",
                                  duplicate_of=str(keeper.get("memory_id"))))
        return found

    def scan(self, *, categories: Sequence[str] = CATEGORIES) -> Dict[str, Any]:
        rows = self.live_rows()
        noise = self.find_noise(rows) if "noise" in categories else []
        seen = {str(item["memory_id"]) for item in noise}
        duplicates = (self.find_duplicates(rows, exclude=seen)
                      if "duplicates" in categories else [])
        targets = noise + duplicates
        by_reason: Dict[str, int] = {}
        for item in targets:
            by_reason[str(item["reason"])] = by_reason.get(str(item["reason"]), 0) + 1
        return {
            "ledger": str(self.path),
            "live_rows": len(rows),
            "distinct_contents": len({content_fingerprint(str(r.get("content") or ""))
                                      for r in rows}),
            "categories": list(categories),
            "noise": len(noise),
            "duplicates": len(duplicates),
            "targets": len(targets),
            "by_reason": by_reason,
            "target_rows": targets,
            "degraded_state": DEGRADED_STATE,
        }


def write_rollback(path: os.PathLike, targets: Sequence[Dict[str, Any]],
                   *, ledger: os.PathLike) -> Path:
    """Persist the exact rollback list BEFORE anything is modified.

    Written first on purpose: if the process dies mid-apply, the list on disk
    still describes everything that could have been touched.  The format is a
    plain JSON array plus ready-to-run SQL, because the person restoring this
    may not have this script.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ledger": str(ledger),
        "degraded_state": DEGRADED_STATE,
        "count": len(targets),
        "rows": [{"memory_id": str(item["memory_id"]),
                  "category": str(item.get("category", "")),
                  "reason": str(item.get("reason", "")),
                  "duplicate_of": str(item.get("duplicate_of", "")),
                  "source_agent": str(item.get("source_agent", "")),
                  "created_at": str(item.get("created_at", "")),
                  "preview": _norm(str(item.get("content") or ""))[:120]}
                 for item in targets],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + chr(10),
                      encoding="utf-8")
    sql_path = target.with_suffix(".rollback.sql")
    lines = ["-- Restore every row degraded by scripts/data_hygiene.py",
             "-- Run against: %s" % ledger,
             "BEGIN;"]
    for item in payload["rows"]:
        lines.append("UPDATE memories SET quality_state='accepted', "
                     "lifecycle='active' WHERE memory_id='%s';" % item["memory_id"])
    lines.append("COMMIT;")
    sql_path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    return target


def apply_degradation(ledger_path: os.PathLike, targets: Sequence[Dict[str, Any]]) -> int:
    """Move each target out of retrieval.  Never deletes anything."""
    if not targets:
        return 0
    conn = sqlite3.connect(str(ledger_path), timeout=20.0)
    try:
        conn.execute("PRAGMA busy_timeout=20000")
        now = datetime.now(timezone.utc).isoformat()
        changed = 0
        for item in targets:
            cursor = conn.execute(
                "UPDATE memories SET quality_state=?, updated_at=? "
                "WHERE memory_id=? AND quality_state!='quarantine'",
                (DEGRADED_STATE, now, str(item["memory_id"])))
            changed += cursor.rowcount
        conn.commit()
        return changed
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt(value: Optional[int]) -> str:
    return "n/a" if value is None else "{:,}".format(int(value))


def render(result: Dict[str, Any], *, applied: Optional[int] = None,
           rollback_path: Optional[Path] = None, dry_run: bool = True) -> str:
    lines: List[str] = []
    add = lines.append
    add("HippoMind 数据卫生 · 账本 %s" % result["ledger"])
    add("=" * 78)
    add("")
    add("1. 现状")
    add("   可召回行（quality_state=accepted）  %s" % _fmt(result["live_rows"]))
    add("   其中不同内容                        %s" % _fmt(result["distinct_contents"]))
    dup_ratio = (0.0 if not result["live_rows"] else
                 1.0 - result["distinct_contents"] / result["live_rows"])
    add("   内容重复率                          %.1f%%" % (dup_ratio * 100))
    add("")
    add("2. 检出")
    add("   噪音空壳（noise）                   %s" % _fmt(result["noise"]))
    add("   重复冗余（duplicates）              %s" % _fmt(result["duplicates"]))
    add("   合计待处理                          %s" % _fmt(result["targets"]))
    if result["by_reason"]:
        add("")
        add("   按原因细分：")
        for reason, count in sorted(result["by_reason"].items(),
                                    key=lambda kv: -kv[1]):
            add("     %-16s %s" % (reason, _fmt(count)))
    add("")
    add("3. 处置方式")
    add("   降级到 quality_state=%s（不物理删除）" % result["degraded_state"])
    add("   为什么不是 cold：search.py 的 include_cold 默认 True，")
    add("   cold 行照样被召回，降级到 cold 对重复率没有影响（已实测）。")
    add("")
    samples = result["target_rows"][:8]
    if samples:
        add("4. 样例（最多 8 条）")
        for item in samples:
            preview = _norm(str(item.get("content") or ""))[:70]
            add("   [%-10s] %-10s %s" % (item.get("reason", ""),
                                          str(item.get("source_agent") or "?")[:10],
                                          preview))
        add("")
    add("5. 结果")
    if dry_run:
        add("   DRY-RUN：未修改账本。加 --apply 才会写入。")
    else:
        add("   已降级行数                          %s" % _fmt(applied or 0))
        if rollback_path:
            add("   回滚清单                            %s" % rollback_path)
            add("   回滚 SQL                            %s"
                % rollback_path.with_suffix(".rollback.sql"))
    return chr(10).join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="清理无意义记忆（默认 dry-run，不修改账本）")
    parser.add_argument("--ledger", default=os.path.expanduser(str(default_ledger_path())),
                        help="账本路径，默认取 HIPPOMIND_HOME")
    parser.add_argument("--apply", action="store_true",
                        help="真正写入（默认只报告）")
    parser.add_argument("--category", action="append", choices=CATEGORIES,
                        help="只处理某一类，可重复；默认两类都处理")
    parser.add_argument("--rollback-file", default="",
                        help="回滚清单路径（默认写到 runtime 目录）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--limit-samples", type=int, default=8,
                        help="报告里最多打印多少条样例")
    args = parser.parse_args(argv)

    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("账本不存在: %s" % ledger_path, file=sys.stderr)
        return 2

    categories = tuple(args.category) if args.category else CATEGORIES
    try:
        with HygieneScanner(ledger_path) as scanner:
            result = scanner.scan(categories=categories)
    except sqlite3.Error as exc:
        print("读取账本失败: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2

    applied = 0
    rollback_path: Optional[Path] = None
    if args.apply and result["target_rows"]:
        rollback = args.rollback_file or str(
            ledger_path.parent / ("data-hygiene-rollback-%s.json"
                                  % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
        rollback_path = write_rollback(rollback, result["target_rows"],
                                       ledger=ledger_path)
        applied = apply_degradation(ledger_path, result["target_rows"])

    if args.json:
        payload = {key: value for key, value in result.items()
                   if key != "target_rows"}
        payload["applied"] = applied
        payload["dry_run"] = not args.apply
        payload["rollback_file"] = str(rollback_path) if rollback_path else ""
        payload["samples"] = [{"memory_id": str(item["memory_id"]),
                               "category": str(item.get("category", "")),
                               "reason": str(item.get("reason", "")),
                               "preview": _norm(str(item.get("content") or ""))[:120]}
                              for item in result["target_rows"][:args.limit_samples]]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(render(result, applied=applied, rollback_path=rollback_path,
                 dry_run=not args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
