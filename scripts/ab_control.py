#!/usr/bin/env python3
"""Turn the injection A/B experiment on or off.

Why a script instead of an environment variable
-----------------------------------------------
The switch originally lived only in the environment, which was the wrong shape
for the thing it controls.  The hook that needs it is spawned by the agent
runtime (Claude Code), not by a login shell - so an export in .zshrc never
reaches it, and the experiment stays silently off no matter what the operator
did.  A file next to the ledger is read by every process regardless of how it
was launched, and changing it is a deliberate act with a visible diff.

Usage
-----
    python3 scripts/ab_control.py            # show current state
    python3 scripts/ab_control.py on         # enable at the default rate
    python3 scripts/ab_control.py on 0.1     # enable at 10% control
    python3 scripts/ab_control.py off        # inject everything again

The experiment withholds memory on a fraction of prompts.  That is a real cost
paid by the user, so this script always prints the arithmetic: how many prompts
it takes before the sample supports any conclusion at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import injection_ab  # noqa: E402


def _describe() -> dict:
    state = injection_ab.explain()
    return {
        "enabled": state["enabled"],
        "rate": state["rate"],
        "config": str(injection_ab.config_path()),
        "config_exists": injection_ab.config_path().is_file(),
        "min_samples": state["min_samples_for_conclusion"],
        "prompts_for_min_samples": state["prompts_needed_for_min_samples"],
        "env_override": bool(str(__import__("os").environ.get(
            injection_ab.ENV_SWITCH, "") or "").strip()),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="开关注入 A/B 实验")
    parser.add_argument("action", nargs="?", choices=("on", "off", "status"),
                        default="status")
    parser.add_argument("rate", nargs="?", type=float, default=None,
                        help="对照比例（0-0.5），默认 0.20")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.action in ("on", "off"):
        injection_ab.write_config(enabled=(args.action == "on"), rate=args.rate)

    state = _describe()
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    print("注入 A/B 实验")
    print("=" * 60)
    print("  状态          %s" % ("已开启" if state["enabled"] else "已关闭（全部注入）"))
    print("  对照比例      %.2f" % state["rate"])
    print("  配置文件      %s%s" % (state["config"],
                                    "" if state["config_exists"] else "（不存在）"))
    if state["env_override"]:
        print("  注意          环境变量 %s 正在覆盖配置文件" % injection_ab.ENV_SWITCH)
    print()
    if state["enabled"]:
        print("  开启后有一小部分 prompt 不会收到记忆。这是实验的真实代价。")
        if state["prompts_for_min_samples"]:
            print("  按当前比例，约 %d 条 prompt 才能凑够 %d 条对照样本。"
                  % (state["prompts_for_min_samples"], state["min_samples"]))
            print("  在此之前，任何「注入有效/无效」的结论都不成立。")
        print()
        print("  查看进度： python3 scripts/injection_report.py")
        print("  关闭实验： python3 scripts/ab_control.py off")
    else:
        print("  关闭时行为与改造前完全一致：每个 prompt 都注入。")
        print("  开启实验： python3 scripts/ab_control.py on [比例]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
