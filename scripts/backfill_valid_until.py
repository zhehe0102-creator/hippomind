#!/usr/bin/env python3
"""Backfill claim windows: give already-superseded claims a valid_until.

The health review measured 0 of 4,771 claims with a non-empty valid_until.
The write side now closes the window when a claim loses (lifecycle.py), but
that only helps claims superseded *from now on*.  Every claim that already
lost its slot still has an open-ended window, which means as_of cannot
answer "was this true in March?" for any historical fact.

The rule
--------
    state='superseded' AND valid_until=''
        -> valid_until = the winner's valid_from   (via superseded_by)
        -> fallback: the loser's own asserted_at

Why the winner's valid_from and not "now"
    Writing now would give the loser the window [loser.valid_from, now).
    The winner's window starts at winner.valid_from, which is *earlier* than
    now, so the two overlap and as_of returns both facts at once -- a
    different flavour of the same lie (instead of "always empty" it becomes
    "always too many").  Anchoring on the winner's start makes the windows
    adjacent: loser=[from, winner_from), winner=[winner_from, inf).

Why asserted_at as the fallback
    superseded_by is empty for claims retired by the convergence path
    (repair_claim_states / _converge_claim_memory) rather than by a ruling --
    those have no winner at all.  asserted_at is when the system recorded the
    assertion, so it is the last moment the claim was definitely live.  It is
    an approximation and the report says so.

Safety
    * Dry-run by default; --apply is required to write anything.
    * --apply takes a consistent snapshot through SQLite's online backup API
      *before* the first write (the same convention as
      scripts/data_hygiene.py and "hippomind backup"; cp misses pages still
      sitting in the WAL).
    * Only rows that are BOTH superseded AND still open are touched, so the
      script is idempotent: a second run finds nothing to do.
    * valid_until is never overwritten once set.

Usage
    python3 scripts/backfill_valid_until.py                  # dry-run
    python3 scripts/backfill_valid_until.py --json
    python3 scripts/backfill_valid_until.py --apply
    python3 scripts/backfill_valid_until.py --apply --backup-dir /some/dir
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Winner's valid_from, else the loser's own asserted_at.
SELECT_CANDIDATES = """
    SELECT c.claim_id, c.memory_id, c.valid_from, c.asserted_at,
           c.valid_until, m.superseded_by,
           w.valid_from AS winner_valid_from
    FROM memory_claims c
    JOIN memories m ON m.memory_id = c.memory_id
    LEFT JOIN memories w ON w.memory_id = m.superseded_by
    WHERE c.state = 'superseded' AND c.valid_until = ''
    ORDER BY c.claim_id
"""


def default_ledger_path() -> Path:
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "hippomind.sqlite"


def resolve_window(row: Dict[str, Any]) -> Tuple[str, str]:
    """(valid_until, basis) for one superseded claim.

    **Only a real winner can date a claim.**  The asserted_at fallback was
    measured on the production ledger and removed: all 1,715 winnerless rows
    have valid_from == asserted_at to within a second, so the fallback
    produced a window of [X, X) -- zero width.  A zero-width window is not a
    slightly-wrong approximation, it is a stronger and falser claim than the
    one it replaces:

        valid_until = ''            -> "still true, we have no history"
        valid_until = X (=from)     -> "provably never true"

    Those rows were not superseded by a ruling at all; they were squeezed out
    by capacity/age and their superseded_by is empty.  Writing asserted_at
    would dress an archive decision up as a replacement.

    basis values:
        'winner'     -- the winner's valid_from, strictly after the loser's
        'no_winner'  -- superseded_by empty or the winner row is gone
        'inverted'   -- the winner starts at/before the loser (clock skew,
                        out-of-order import); a window would be zero/negative
        'bad_time'   -- a timestamp is missing, so no width can be checked
    """
    winner_from = str(row.get("winner_valid_from") or "")
    loser_from = str(row.get("valid_from") or "")
    if not winner_from:
        return "", "no_winner"
    if not loser_from:
        # No start means no width to check.  Refusing is the safe side: the
        # as_of gate needs a non-degenerate [from, until) pair to mean anything.
        return "", "bad_time"
    if winner_from <= loser_from:
        return "", "inverted"
    return winner_from, "winner"


def scan(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(SELECT_CANDIDATES).fetchall()]
    for row in rows:
        window_end, basis = resolve_window(row)
        row["proposed_valid_until"] = window_end
        row["basis"] = basis
    return rows


def summarise(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    actionable = [row for row in rows if row["proposed_valid_until"]]
    skipped: Dict[str, int] = {}
    for row in rows:
        if not row["proposed_valid_until"]:
            skipped[row["basis"]] = skipped.get(row["basis"], 0) + 1
    memories = {row["memory_id"] for row in actionable}
    return {
        "candidates": len(rows),
        "actionable": len(actionable),
        "unresolvable": len(rows) - len(actionable),
        "memories": len(memories),
        "skipped_by_reason": skipped,
    }


def open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=10000")
    except sqlite3.DatabaseError:
        pass
    return conn


def take_backup(path: Path, backup_dir: Optional[Path]) -> Path:
    """Consistent snapshot via the SQLite online backup API.

    cp is not enough: pages committed but not yet checkpointed live in the
    -wal sidecar, so a plain file copy can silently lose the most recent
    writes (the project measured a 20-row gap on a 754MB ledger).
    """
    target_dir = Path(backup_dir) if backup_dir else path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / ("valid-until-backup-%s.sqlite" % stamp)
    if target.exists():
        raise SystemExit("备份目标已存在，拒绝覆盖: %s" % target)
    source = sqlite3.connect(str(path), timeout=30.0)
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
            dest.commit()
            integrity = str(dest.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        finally:
            dest.close()
    finally:
        source.close()
    if integrity != "ok":
        raise SystemExit("备份完整性检查失败: %s" % integrity)
    return target


def apply_windows(path: Path, rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Write the proposed claim windows.  Only superseded+open rows are touched."""
    actionable = [row for row in rows if row["proposed_valid_until"]]
    if not actionable:
        return {"claims_updated": 0, "memories_updated": 0}
    # Belt and braces: resolve_window already refuses non-positive widths, but
    # this is the last gate before the write.  A zero-width window is a wrong
    # answer that looks precise, so refuse the whole run instead of writing it.
    bad = [row for row in actionable
           if not (str(row["proposed_valid_until"]) > str(row["valid_from"] or ""))]
    if bad:
        raise SystemExit(
            "拒绝写入：%d 条候选会产生零宽/倒置窗口（valid_until <= valid_from），"
            "例如 claim_id=%s" % (len(bad), bad[0]["claim_id"]))
    conn = sqlite3.connect(str(path), timeout=30.0)
    updated = 0
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        for row in actionable:
            # The WHERE clause repeats the candidate predicate: if the daemon
            # superseded (or re-opened) the row between the scan and this
            # write, the update must not clobber the newer decision.
            cursor = conn.execute(
                "UPDATE memory_claims SET valid_until=? "
                "WHERE claim_id=? AND state='superseded' AND valid_until=''",
                (row["proposed_valid_until"], row["claim_id"]))
            updated += cursor.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"claims_updated": updated, "memories_updated": 0}


def apply_memory_windows(path: Path, rows: Sequence[Dict[str, Any]]) -> int:
    """Mirror the claim windows onto the memory rows.

    search() filters memories.valid_until, so a claim-only backfill would
    leave the as_of gate exactly as broken as before.  Only memories that are
    themselves superseded and still open are touched, and the window is the
    earliest value its claims got (the memory stopped being true when its
    first claim did).
    """
    windows: Dict[str, str] = {}
    for row in rows:
        window = row["proposed_valid_until"]
        if not window:
            continue
        current = windows.get(row["memory_id"])
        if current is None or window < current:
            windows[row["memory_id"]] = window
    if not windows:
        return 0
    conn = sqlite3.connect(str(path), timeout=30.0)
    updated = 0
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        for memory_id, window in windows.items():
            cursor = conn.execute(
                "UPDATE memories SET valid_until=? "
                "WHERE memory_id=? AND lifecycle='superseded' AND valid_until=''",
                (window, memory_id))
            updated += cursor.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return updated


def _fmt(value: Any) -> str:
    return "{:,}".format(int(value))


def render(summary: Dict[str, Any], *, ledger: Path, applied: Optional[Dict[str, int]],
           backup: Optional[Path], dry_run: bool) -> str:
    lines: List[str] = []
    add = lines.append
    add("HippoMind valid_until 历史回填 · 账本 %s" % ledger)
    add("=" * 78)
    add("")
    add("1. 候选")
    add("   state='superseded' 且 valid_until=''   %s" % _fmt(summary["candidates"]))
    add("   可回填                                %s" % _fmt(summary["actionable"]))
    add("   无法定日期（跳过）                    %s" % _fmt(summary["unresolvable"]))
    add("   涉及记忆                              %s" % _fmt(summary["memories"]))
    add("")
    add("2. 跳过（保留空 valid_until，语义「仍然有效」）")
    skip_labels = {
        "no_winner": "superseded_by 为空或胜者行已不存在",
        "inverted": "胜者 valid_from 不晚于败者（时间倒置，窗口会为零/负）",
        "bad_time": "时间戳缺失，无法校验窗口宽度",
    }
    if not summary["skipped_by_reason"]:
        add("   （无）")
    for basis, count in sorted(summary["skipped_by_reason"].items(),
                               key=lambda kv: -kv[1]):
        add("   %-46s %s" % (skip_labels.get(basis, basis), _fmt(count)))
    add("")
    add("3. 规则")
    add("   valid_until = 胜者 valid_from（严格晚于败者 valid_from）。")
    add("   **只回填有胜者的 claim**：没有胜者说明这条不是被裁决取代的，")
    add("   而是被容量/年龄挤出（cold 归档）。用它的 asserted_at 冒充失效")
    add("   时间会造出零宽窗口 [X, X) ——「可证明从未为真」，比留空")
    add("   （「仍然有效，只是没有历史」）更强也更假。")
    add("   为什么不写 now：败者窗口会变成 [from, now)，与胜者的")
    add("   [winner_from, inf) 重叠，as_of 会同时返回两条事实。")
    add("")
    add("4. 结果")
    if dry_run:
        add("   DRY-RUN：未修改账本。加 --apply 才会写入。")
    else:
        add("   已更新 claim                          %s"
            % _fmt((applied or {}).get("claims_updated", 0)))
        add("   已更新 memory                         %s"
            % _fmt((applied or {}).get("memories_updated", 0)))
        if backup:
            add("   备份（SQLite backup API）             %s" % backup)
    return chr(10).join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="为已 superseded 的 claim 回填 valid_until（默认 dry-run）")
    parser.add_argument("--ledger", default=str(default_ledger_path()),
                        help="账本路径，默认取 HIPPOMIND_HOME")
    parser.add_argument("--apply", action="store_true",
                        help="真正写入（默认只报告）；写入前自动做一致性备份")
    parser.add_argument("--backup-dir", default="",
                        help="备份目录（默认放在账本同目录）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("账本不存在: %s" % ledger_path, file=sys.stderr)
        return 2

    try:
        conn = open_readonly(ledger_path)
        try:
            rows = scan(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print("读取账本失败: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2

    summary = summarise(rows)
    applied: Optional[Dict[str, int]] = None
    backup: Optional[Path] = None
    if args.apply and summary["actionable"]:
        # Backup first, unconditionally: a partial write is recoverable only
        # if the snapshot predates it.
        backup = take_backup(ledger_path,
                             Path(args.backup_dir) if args.backup_dir else None)
        applied = apply_windows(ledger_path, rows)
        applied["memories_updated"] = apply_memory_windows(ledger_path, rows)

    if args.json:
        payload = dict(summary)
        payload.update({
            "ledger": str(ledger_path),
            "dry_run": not args.apply,
            "backup": str(backup) if backup else "",
            "applied": applied or {"claims_updated": 0, "memories_updated": 0},
            "samples": [{"claim_id": row["claim_id"],
                         "memory_id": row["memory_id"],
                         "valid_from": row["valid_from"],
                         "valid_until": row["proposed_valid_until"],
                         "basis": row["basis"]}
                        for row in rows[:10]],
        })
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(render(summary, ledger=ledger_path, applied=applied,
                 backup=backup, dry_run=not args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
