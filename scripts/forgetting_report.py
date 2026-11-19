#!/usr/bin/env python3
"""Why was this memory demoted or archived?  An audit, not a dashboard.

The health review (docs/HEALTH_REVIEW.md §5) measured the symptom and named the
cause: every active memory had utility < 1, 255k rows were archived as cold, and
nothing recorded why any individual row moved.  A count cannot answer "why",
so this script prints, for the recent window:

  1. 证据       how many decay events exist, over what window, and how many of
                them could not be parsed (a shrinking denominator is a lie);
  2. 原因       per reason (expired / raw_retention / weight_floor / age_limit /
                capacity_overflow) with the rule that produced it;
  3. 分布       by agent and by kind -- whose memories are being forgotten;
  4. 明细       the N memories whose weight actually dropped, with the before
                and after weight and the age that drove it;
  5. 归档       where the archived rows went (cold / expired / superseded);
  6. 可恢复性   whether those rows can still be recalled.  include_cold is on
                by default in SQLiteLedger.search, so a cold row leaves the hot
                FTS index but stays reachable through the LIKE rescue path.
                Rows that are NOT reachable are named with the exact clause
                that blocks them (quality gate, scope, expiry, raw_chat);
  7. 覆盖缺口   how much of the current cold population this window explains;
  8. 结论       what may and may not be claimed.

The honest failure mode this is built around: when the write side has not
landed yet, a report that prints "0 条" reads like "nothing was forgotten"
while the database holds a quarter of a million cold rows.  Section 7 makes
that difference visible, and the verdict says 「无法解释」 instead of zero.

The ledger is opened read-only (mode=ro + PRAGMA query_only): measuring
production must never mutate it.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.forgetting import (  # noqa: E402
    DEFAULT_DAYS,
    DEFAULT_LIMIT,
    ReadOnlyLedger,
    default_ledger_path,
    forgetting_report,
    render,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="遗忘审计：哪些记忆被降权/归档、依据是什么、还能不能召回"
                    "（只读，不会修改账本）")
    parser.add_argument("--ledger", default=os.path.expanduser(str(default_ledger_path())),
                        help="账本路径，默认取 HIPPOMIND_HOME")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="回看天数（默认 %d）" % DEFAULT_DAYS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="「降权最狠」列出多少条（默认 %d）" % DEFAULT_LIMIT)
    parser.add_argument("--scope", default="shared",
                        help="可恢复性判定用的查询 scope（默认 shared，与 CLI 默认一致）")
    parser.add_argument("--include-raw-chat", action="store_true",
                        help="可恢复性判定时把原始会话也算作可召回（raw_chat 默认不参与召回）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("账本不存在: %s" % ledger_path, file=sys.stderr)
        return 2
    try:
        with ReadOnlyLedger(ledger_path) as ledger:
            result = forgetting_report(
                ledger, days=max(1, args.days), limit=max(1, args.limit),
                scope=args.scope, include_raw_chat=bool(args.include_raw_chat))
    except sqlite3.Error as exc:
        print("读取账本失败: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
