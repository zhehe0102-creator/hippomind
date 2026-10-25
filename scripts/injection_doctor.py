#!/usr/bin/env python3
"""注入面自动检测与修复的运维入口。

用法：
    python3 scripts/injection_doctor.py                 # 只审计，报告每个注入面
    python3 scripts/injection_doctor.py --fix --dry-run # 预览将要做哪些修复
    python3 scripts/injection_doctor.py --fix --apply   # 真正落盘（先备份）

为什么需要这个脚本：一次真实事故里，7 个 agent 的接入全都"看起来装好了"
（配置文件里有 hippomind 字样），但没有一个能真正读到生产账本——
它们指向一个 /var/folders/.../tmpXXXX/ 的临时测试库（0 条记忆）。
状态命令只查"文件里有没有这条配置"，于是全部报告"已安装"。
本脚本的判据是"这条路径现在能不能读到生产账本"。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.agent_install import (SUPPORTED_AGENTS, agent_config_path,  # noqa: E402
                                     agent_mcp_path, agent_rule_path)
from hippomind.injection_audit import audit, summarize  # noqa: E402
from hippomind.injection_repair import (apply_repairs, repair_codex_hook,  # noqa: E402
                                        repair_mcp_server)

DEFAULT_LEDGER = os.path.expanduser(
    "~/Library/Application Support/HippoMind/hippomind.sqlite")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="注入面自动检测与修复")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--home", default="")
    parser.add_argument("--agent", action="append", default=[],
                        help="只处理指定 agent，可重复；默认全部")
    parser.add_argument("--fix", action="store_true", help="生成修复方案")
    parser.add_argument("--apply", action="store_true", help="真正写盘（需配合 --fix）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else Path.home()
    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("error: 生产账本不存在: %s" % ledger_path, file=sys.stderr)
        return 2
    agents = tuple(args.agent) or SUPPORTED_AGENTS

    findings = audit(agents, ledger_path=ledger_path, home=home,
                     rule_path_of=lambda a: agent_rule_path(a, home=home),
                     mcp_path_of=lambda a: agent_mcp_path(a, home=home))
    summary = summarize(findings)

    repairs = []
    if args.fix:
        python = sys.executable
        config_root = ledger_path.parent / "agents"
        for agent in agents:
            mcp_path = agent_mcp_path(agent, home=home)
            if mcp_path is None:
                continue
            repairs.append(repair_mcp_server(
                mcp_path, ledger_path=ledger_path,
                agent_config=agent_config_path(config_root.parent, agent),
                package_root=ROOT, python=python))
        if "codex" in agents:
            repairs.append(repair_codex_hook(
                home / ".codex" / "hooks.json", package_root=ROOT,
                agent_config=agent_config_path(config_root.parent, "codex"),
                python=python, ledger_path=ledger_path))
        # 规则文件单独处理：它要求"就地重写托管段"，不能整份覆盖
        # （用户在同一文件里还写了自己的内容）。
        from hippomind.rule_repair import repair_rule_file
        runtime_root = ledger_path.parent / "runtime"
        rule_repairs = []
        for agent in agents:
            rule_path = agent_rule_path(agent, home=home)
            rule_repairs.append(repair_rule_file(
                rule_path, agent=agent, runtime_root=runtime_root,
                agent_config=agent_config_path(config_root.parent, agent),
                python=python))
        applied_rules = []
        for record in rule_repairs:
            entry = {"path": record["path"], "changed": record.get("changed", False),
                     "reason": record.get("reason", "")}
            if not record.get("changed"):
                entry["applied"] = False
                applied_rules.append(entry)
                continue
            if not args.apply:
                entry["would_change"] = True
                entry["applied"] = False
                applied_rules.append(entry)
                continue
            try:
                from hippomind.injection_repair import _atomic_text_public, _backup
                entry["backup"] = str(_backup(Path(record["path"])) or "")
                _atomic_text_public(Path(record["path"]), record["text"])
                entry["applied"] = True
            except OSError as exc:
                entry["applied"] = False
                entry["reason"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            applied_rules.append(entry)
        applied = apply_repairs(repairs, dry_run=not args.apply)
        summary["repairs"] = applied_rules + applied

    if args.json:
        print(json.dumps({"summary": summary,
                          "findings": [f.as_dict() for f in findings]},
                         ensure_ascii=False, indent=2))
        return 0

    print("生产账本: %s" % ledger_path)
    print("注入面: %d 个，健康 %d 个" % (summary["total"], summary["healthy"]))
    print("状态分布: %s" % json.dumps(summary["counts"], ensure_ascii=False))
    print("")
    for finding in findings:
        mark = "OK  " if finding.status == "healthy" else "BAD "
        print("  %s %-12s %-6s %-14s %s"
              % (mark, finding.agent, finding.surface, finding.status,
                 finding.detail[:60]))
    if args.fix:
        print("")
        print("=== 修复方案%s ===" % ("" if args.apply else "（dry-run，未写盘）"))
        for record in summary["repairs"]:
            if record["applied"]:
                state = "APPLIED"
            elif record.get("would_change"):
                state = "WOULD"
            else:
                state = "SKIP"
            note = ("backup=" + record["backup"]) if record.get("backup") else ""
            if not note and record.get("reason"):
                note = record["reason"][:70]
            print("  %-7s %-58s %s" % (state, record["path"], note))
    # --apply 之后必须**重新审计**再判定退出码：summary 是修复前的快照，
    # 用它判退出码会让"刚刚修好了"也返回 1（安装脚本会误报失败）。
    if args.fix and args.apply:
        findings = audit(agents, ledger_path=ledger_path, home=home,
                         rule_path_of=lambda a: agent_rule_path(a, home=home),
                         mcp_path_of=lambda a: agent_mcp_path(a, home=home))
        summary = summarize(findings)
        if not args.json:
            print("")
            print("修复后: 注入面 %d 个，健康 %d 个"
                  % (summary["total"], summary["healthy"]))
            for finding in findings:
                if finding.status != "healthy":
                    print("  BAD %-12s %-6s %-14s %s"
                          % (finding.agent, finding.surface, finding.status,
                             finding.detail[:60]))
    return 0 if summary["healthy"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
