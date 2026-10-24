#!/usr/bin/env python3
"""Print what to resume after an interruption.

The shared task board is the durable state: an interrupted teammate leaves its
task in the in_progress state.  This script lists those tasks together with the
current agent availability, so the Lead can wake exactly the teammates that
were mid-flight instead of guessing or re-dispatching finished work.

Usage:
    python3 scripts/team_resume.py            # human-readable checklist
    python3 scripts/team_resume.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def collect() -> dict:
    """Return the resumable tasks and every teammate's state."""
    from hippomind.team_state import read_agents, read_tasks

    agents = read_agents()
    tasks = read_tasks()
    running = {agent.get("target") for agent in agents if agent.get("status") == "running"}
    resumable = []
    for task in tasks:
        if task.get("status") != "in_progress":
            continue
        owner = task.get("ownerName") or ""
        resumable.append({
            "task_id": task.get("id", ""),
            "subject": task.get("subject", ""),
            "owner": owner,
            "owner_running": owner in running,
            "revision": task.get("revision", 0),
            "write_scopes": task.get("writeScopes", []),
        })
    return {"agents": agents, "resumable": resumable,
            "completed": [task.get("id") for task in tasks if task.get("status") == "completed"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Resume checklist for interrupted teammates")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    state = collect()
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    resumable = state["resumable"]
    if not resumable:
        print("没有进行中的子任务；无需唤醒。")
        return 0
    print("需要续接的子任务（磁盘快照可能滞后，最终以 list_agents 为准）：")
    for item in resumable:
        print("  - %s  %s  owner=%s  (revision %s)" % (
            item["task_id"], item["subject"], item["owner"] or "未认领", item["revision"]))
        if item["write_scopes"]:
            print("      写范围: " + ", ".join(item["write_scopes"]))
    print("")
    print("唤醒方式：对每个已中断的 owner 发送：")
    print("  继续 <task-id>：先 team_task_get 看当前进度，检查已写入的文件，不要重做；")
    print("  完成后 team_task_update action=complete，并给 Lead 发报告。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
