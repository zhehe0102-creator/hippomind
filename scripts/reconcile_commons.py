#!/usr/bin/env python3
"""commons <-> ledger 双存储对账（默认只读；--repair 显式开启）。

commons 的 shared_memories 与 HippoMind ledger 的 memories 是同一批共享记忆的
两个投影。同步/提炼路径一旦只更新了一侧，就会表现为四种分叉：

  missing_in_ledger  commons 有、ledger 没有；
  missing_in_commons  ledger 有、commons 没有；
  stale              两侧都在、窗口一致，但 updated_at 不一致（内容/反馈漂移）；
  window_mismatch    两侧都在，但 superseded_by / valid_until 不一致。

窗口漂移是唯一可以安全自动收敛的桶：--repair 只把 ledger 的窗口字段写回
commons，不删行、不改正文/updated_at/feedback，且一次事务完成。默认模式只用
mode=ro + query_only 连接（WAL 库无 sidecar 时用 immutable 快照），不会修改
任何一个文件；重复执行 --repair 时计划为空就不打开写连接，因此幂等。

用法::

    python3 scripts/reconcile_commons.py                       # 只读对账，JSON 为可选
    python3 scripts/reconcile_commons.py --json                # 机器可读报告
    python3 scripts/reconcile_commons.py --repair --json       # 显式修复窗口漂移
    python3 scripts/reconcile_commons.py --commons-db /tmp/copy.sqlite --ledger /tmp/hub.sqlite

退出码：0 一致/修复收敛；1 存在漂移或修复未收敛；2 账本缺失。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.commons import (  # noqa: E402
    DEFAULT_RECONCILE_SAMPLES, RECONCILE_BUCKETS,
    compare_shared_with_ledger, repair_shared_with_ledger)


def _default_ledger_path() -> Path:
    from hippomind.ledger import default_ledger_path
    return Path(default_ledger_path()).expanduser()


def _counts_line(counts: dict) -> str:
    return ("一致 %d ｜ 仅commons %d ｜ 仅ledger %d ｜ updated漂移 %d ｜ 窗口漂移 %d"
            % (int(counts.get("in_sync", 0)),
               int(counts.get("missing_in_ledger", 0)),
               int(counts.get("missing_in_commons", 0)),
               int(counts.get("stale", 0)),
               int(counts.get("window_mismatch", 0))))


def _render_compare(report: dict) -> str:
    lines = ["commons 对账（只读）",
             "  commons: %s%s" % (report["commons_db"],
                                  "" if report["commons_exists"] else "  [缺失]"),
             "  ledger : %s%s" % (report["ledger"],
                                  "" if report["ledger_exists"] else "  [缺失]"),
             "  行数: commons %d / ledger %d"
             % (report["commons_rows"], report["ledger_rows"]),
             "  " + _counts_line(report["counts"])]
    if report.get("commons_error"):
        lines.append("  commons 错误: " + str(report["commons_error"]))
    if report.get("ledger_error"):
        lines.append("  ledger 错误: " + str(report["ledger_error"]))
    samples = report.get("samples") or {}
    for bucket in RECONCILE_BUCKETS:
        items = samples.get(bucket) or []
        if not items:
            continue
        lines.append("  [%s]" % bucket)
        for item in items[:3]:
            lines.append("    " + json.dumps(item, ensure_ascii=False, default=str))
    lines.append("  结论: " + ("一致" if report["ok"] else "存在双存储分叉"))
    return "\n".join(lines)


def _render_repair(report: dict) -> str:
    before = report["before"]
    after = report["after"]
    lines = ["commons 对账修复（--repair）",
             "  commons: %s" % report["commons_db"],
             "  ledger : %s" % report["ledger"],
             "  修复前: " + _counts_line(before["counts"]),
             "  计划 %d 行 / 实际写入 %d 行%s"
             % (report["planned"], report["repaired"],
                "（dry-run）" if report["dry_run"] else ""),
             "  修复后: " + _counts_line(after["counts"]),
             "  窗口收敛: " + ("是" if report["window_resolved"] else "否")]
    if report.get("error"):
        lines.append("  错误: " + str(report["error"]))
    for item in (report.get("repairs") or [])[:5]:
        lines.append("    " + json.dumps(item, ensure_ascii=False, default=str))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="commons 与 ledger 双存储对账；默认只读，--repair 才写 commons")
    parser.add_argument("--ledger", default="",
                        help="ledger SQLite 路径（默认取 HIPPOMIND_HOME）")
    parser.add_argument("--commons-db", default="",
                        help="commons SQLite 路径（默认取 Agent-Chat-Archive/index）")
    parser.add_argument("--archive", default="",
                        help="Agent-Chat-Archive 根目录（等价于 --commons-db 的父父目录）")
    parser.add_argument("--scope", default="",
                        help="只比较该 scope（默认空串=两侧全部行）")
    parser.add_argument("--samples", type=int, default=DEFAULT_RECONCILE_SAMPLES,
                        help="每桶最多输出多少条样例（默认 %d）"
                             % DEFAULT_RECONCILE_SAMPLES)
    parser.add_argument("--repair", action="store_true",
                        help="显式开启修复：只把 ledger 的 superseded_by/valid_until"
                             "同步到 commons（不删行、不改内容）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    ledger_path = Path(args.ledger).expanduser() if args.ledger else _default_ledger_path()
    kwargs = {"ledger": ledger_path, "scope": args.scope,
              "sample_limit": max(0, int(args.samples))}
    if args.commons_db:
        kwargs["commons_db"] = Path(args.commons_db).expanduser()
    elif args.archive:
        kwargs["archive"] = args.archive

    if args.repair:
        report = repair_shared_with_ledger(**kwargs)
        payload = dict(report, mode="repair")
    else:
        report = compare_shared_with_ledger(**kwargs)
        payload = dict(report, mode="compare")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(_render_repair(report) if args.repair else _render_compare(report))

    if not report.get("ledger_exists", True):
        return 2
    if args.repair:
        return 0 if report.get("window_resolved") and not report.get("error") else 1
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
