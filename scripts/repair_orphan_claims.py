#!/usr/bin/env python3
"""One-shot convergence for orphan claims: demoted, but never taken over.

The 2026-10-25 ledger held 1,576 claims whose memory was lifecycle=active but
whose claim was superseded.  Precise diagnosis split them into three groups,
and only one of them is a defect:

    1,535  memory.quality_state = quarantine
           The memory is NOT recallable (the recall predicate is
           accepted AND active), so its claim SHOULD be superseded.  This is
           correct behaviour, not a bug -- it is the same shape as the 1,535
           dangling claims fixed in round 12.  Never touched here.

       32  memory accepted+active, but another memory holds an active claim
           in the same slot.  Reactivating would put two active claims in one
           slot, which conflict detection immediately turns into a conflict
           and BOTH sides drop out of recall.  Net loss.  Reported, not
           changed.

        9  memory accepted+active, no winner (superseded_by empty), no rival
           in the slot, window still open.  These violate the invariant and
           are the only safe group to restore.

Why the script exists at all
    repair_claim_states() now converges these automatically (the reverse
    direction of the invariant), so the mechanism is permanent.  But the
    SCHEDULE is not: repair_claim_states() is NOT called from maintain(),
    the hourly maintenance tick.  Its only callers are two lines in
    hippomind/distill.py (DistillEngine.run and resolve_conflicts), so it
    runs only when a distill cycle happens -- which depends on whether there
    is new evidence to distil, not on the clock.

    Measured on 2026-10-25: the production ledger still held all 9 orphans
    with zero distill events recorded, i.e. nothing had healed them since the
    invariant landed.  So "the invariant heals it" is true of the mechanism
    and false of the timing.  This script is the deterministic convergence:
    run it, or wait for an indeterminate distill cycle.

    中文对照：「不变式会自愈」在**机制层面为真，时序层面为假**。
    repair_claim_states() 不在 maintain() 里，只在 distill 的两个点被调用，
    而 distill 跑不跑取决于「有没有新证据要提炼」，不取决于时钟。实测
    2026-10-25 生产库：distill 事件数 0，9 条孤儿一条没少——反向不变式落地
    之后自愈从未发生过。这类「机制正确但不会触发」的问题比「机制错误」更
    难发现，因为它看起来一切正常。所以本脚本是确定性的收敛手段：要么现在
    跑它，要么等一个时机不确定的 distill 周期。

    It calls the same predicate as repair_claim_states so the two can never
    disagree about what "orphan" means.

Safety
    * Dry-run by default; --apply is required to write.
    * --apply takes a SQLite online-backup snapshot before the first write.
    * Only the 9-orphan shape is touched; the other two groups are counted
      and reported so an operator can see they were considered.
    * Idempotent: after the reverse invariant runs, a second pass finds
      nothing.

Usage
    python3 scripts/repair_orphan_claims.py                 # dry-run
    python3 scripts/repair_orphan_claims.py --json
    python3 scripts/repair_orphan_claims.py --apply
    python3 scripts/repair_orphan_claims.py --apply --backup-dir /some/dir
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

#: 真孤儿：可召回记忆、无胜者、窗口未闭合、同 slot 无竞争者。
#: 与 lifecycle.repair_claim_states() 的反向收敛判据逐字一致——两处若漂移，
#: 脚本会修一批 repair 不修的行（或反过来），对账时永远解释不清。
ORPHAN_PREDICATE = """
    c.state = 'superseded'
    AND c.valid_until = ''
    AND m.superseded_by = ''
    AND m.quality_state = 'accepted'
    AND m.lifecycle = 'active'
    AND NOT EXISTS (
        SELECT 1 FROM memory_claims o
        WHERE o.claim_id != c.claim_id
          AND o.scope = c.scope AND o.subject = c.subject
          AND o.predicate = c.predicate AND o.slot_key = c.slot_key
          AND o.state IN ('active','conflict'))
"""

#: 同 slot 有活竞争者：报告为「已知冲突」，不改变状态。
RIVAL_PREDICATE = """
    c.state = 'superseded'
    AND c.valid_until = ''
    AND m.superseded_by = ''
    AND m.quality_state = 'accepted'
    AND m.lifecycle = 'active'
    AND EXISTS (
        SELECT 1 FROM memory_claims o
        WHERE o.claim_id != c.claim_id
          AND o.scope = c.scope AND o.subject = c.subject
          AND o.predicate = c.predicate AND o.slot_key = c.slot_key
          AND o.state IN ('active','conflict'))
"""

#: 不可召回记忆上的 claim：判据正确，不是缺陷。
QUARANTINED_PREDICATE = """
    c.state = 'superseded'
    AND NOT (m.quality_state = 'accepted' AND m.lifecycle = 'active')
"""


def default_ledger_path() -> Path:
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "hippomind.sqlite"


def _select(conn: sqlite3.Connection, predicate: str, limit: int = 0) -> List[Dict[str, Any]]:
    sql = ("SELECT c.claim_id, c.memory_id, c.scope, c.subject, c.predicate, "
           "c.slot_key, c.valid_from, m.content "
           "FROM memory_claims c JOIN memories m ON m.memory_id = c.memory_id "
           "WHERE " + predicate + " ORDER BY c.claim_id")
    if limit:
        sql += " LIMIT %d" % int(limit)
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql).fetchall()]


def scan(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    return {
        "orphans": _select(conn, ORPHAN_PREDICATE),
        "rivals": _select(conn, RIVAL_PREDICATE),
        "quarantined": _select(conn, QUARANTINED_PREDICATE),
    }


def summarise(groups: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    return {
        "orphans": len(groups["orphans"]),
        "rivals": len(groups["rivals"]),
        "quarantined": len(groups["quarantined"]),
        "orphan_memories": len({r["memory_id"] for r in groups["orphans"]}),
    }


def open_readonly(path: Path) -> sqlite3.Connection:
    """Read-only connection; falls back when a WAL database lacks its -shm.

    mode=ro cannot open a WAL database whose shared-memory index is absent,
    because SQLite has to create the -shm to read the WAL.  The fallback uses
    query_only, which still refuses every write at the SQLite layer.
    """
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM memory_claims LIMIT 1").fetchone()
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error:
        pass
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("SELECT 1 FROM memory_claims LIMIT 1").fetchone()
    return conn


def take_backup(path: Path, backup_dir: Optional[Path]) -> Path:
    target_dir = Path(backup_dir) if backup_dir else path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / ("orphan-claims-backup-%s.sqlite" % stamp)
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


def apply_orphans(path: Path, orphans: Sequence[Dict[str, Any]]) -> int:
    """Reactivate exactly the given orphan claims (and nothing else)."""
    if not orphans:
        return 0
    conn = sqlite3.connect(str(path), timeout=30.0)
    updated = 0
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        for row in orphans:
            # 重复一遍完整判据：扫描和写入之间 daemon 可能已经改过状态，
            # 用 UPDATE ... WHERE <predicate> 保证不会覆盖更新的决定。
            cursor = conn.execute("""
                UPDATE memory_claims SET state='active'
                WHERE claim_id = ? AND state = 'superseded'
                  AND valid_until = ''
                  AND memory_id IN (SELECT memory_id FROM memories m
                                    WHERE m.superseded_by = ''
                                      AND m.quality_state = 'accepted'
                                      AND m.lifecycle = 'active')
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_claims o
                      WHERE o.claim_id != memory_claims.claim_id
                        AND o.scope = memory_claims.scope
                        AND o.subject = memory_claims.subject
                        AND o.predicate = memory_claims.predicate
                        AND o.slot_key = memory_claims.slot_key
                        AND o.state IN ('active','conflict'))""", (row["claim_id"],))
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


def render(summary: Dict[str, Any], *, ledger: Path,
           applied: Optional[int], backup: Optional[Path], dry_run: bool) -> str:
    lines: List[str] = []
    add = lines.append
    add("HippoMind 孤儿 claim 收敛 · 账本 %s" % ledger)
    add("=" * 78)
    add("")
    add("1. 三类分诊（判据来自 repair_claim_states 的反向不变式）")
    add("")
    add("   [可恢复] 可召回 + 无胜者 + 窗口未闭合 + 无竞争者  %s"
        % _fmt(summary["orphans"]))
    add("            涉及记忆                                 %s"
        % _fmt(summary["orphan_memories"]))
    add("")
    add("   [不动]   同 slot 有活跃竞争者（恢复会造真冲突）     %s"
        % _fmt(summary["rivals"]))
    add("   [不动]   记忆不可召回（判据正确，非缺陷）           %s"
        % _fmt(summary["quarantined"]))
    add("")
    add("2. 为什么只有第一类可恢复")
    add("   同 slot 有竞争者时恢复 = 同一 slot 两条 active claim，")
    add("   冲突检测会立刻把两条都打成 conflict，双方一起掉出召回。")
    add("   记忆不可召回（quarantine/cold/superseded）时恢复 = 悬空 claim，")
    add("   正是 round 12 修掉的那 1,535 条事故的形状。")
    add("")
    add("3. 结果")
    if dry_run:
        add("   DRY-RUN：未修改账本。加 --apply 才会写入。")
    else:
        add("   已恢复 active                          %s" % _fmt(applied or 0))
        if backup:
            add("   备份（SQLite backup API）               %s" % backup)
    return chr(10).join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="收敛孤儿 claim（可召回记忆上无胜者的 superseded claim）；默认 dry-run")
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
            groups = scan(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print("读取账本失败: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2

    summary = summarise(groups)
    applied: Optional[int] = None
    backup: Optional[Path] = None
    if args.apply and groups["orphans"]:
        backup = take_backup(ledger_path,
                             Path(args.backup_dir) if args.backup_dir else None)
        applied = apply_orphans(ledger_path, groups["orphans"])

    if args.json:
        payload = dict(summary)
        payload.update({
            "ledger": str(ledger_path),
            "dry_run": not args.apply,
            "backup": str(backup) if backup else "",
            "applied": applied or 0,
            "orphan_samples": [{"claim_id": r["claim_id"],
                                "memory_id": r["memory_id"],
                                "subject": r["subject"],
                                "predicate": r["predicate"],
                                "slot_key": r["slot_key"],
                                "preview": (r.get("content") or "")[:80]}
                               for r in groups["orphans"][:20]],
        })
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(render(summary, ledger=ledger_path, applied=applied,
                 backup=backup, dry_run=not args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
