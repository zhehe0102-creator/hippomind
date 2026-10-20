"""Write-side commands: ingest, feedback, pin/pins, forget, facts, skills,
resolve, conflicts and the entity graph.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping
from ..ledger import LedgerError, SQLiteLedger
from . import (_agent_token, _daemon_reachable, _paths, _port, _request)
from .common import _print, _reject_restricted_direct


def cmd_graph(args) -> int:
    """实体图谱：从 claim 主语出发的因果/关联邻域（1–2 跳）。"""
    _reject_restricted_direct(args, "graph")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    report = ledger.entity_graph(
        str(args.subject), scope=args.scope or "shared",
        hops=max(1, min(2, int(args.hops))),
        limit=max(10, min(2000, int(args.limit))))
    if args.format == "json":
        _print(report)
        return 0
    if not report.get("found"):
        print(f"(未找到实体「{args.subject}」的活跃 claim)")
        return 0
    print(f"实体「{report['subject']}」· {len(report['nodes'])} 节点 · "
          f"{len(report['edges'])} 边 · {report['hops']} 跳")
    for node in report["nodes"]:
        hop = node.get("hop")
        mark = "● 起点" if hop == 0 else "○"
        print(f"  {mark} {node['subject']}  (claims={node['claims']}, "
              f"memories={node['memories']})")
    for edge in report["edges"]:
        print(f"  {edge['from']} --{edge['relation_type']}"
              f"({edge['confidence']:.2f})--> {edge['to']}")
    return 0


def _ingest_record(args) -> Dict[str, Any]:
    if args.file:
        with Path(args.file).expanduser().open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    elif args.json:
        data = json.loads(args.json)
    else:
        if not args.content:
            raise LedgerError("ingest 需要 --content、--json 或 --file")
        data = {
            "content": args.content, "kind": args.kind, "source_agent": args.agent,
            "session_id": args.session, "task_id": args.task, "project": args.project,
            "role": args.role, "scope": args.scope, "evidence": args.evidence,
        }
    if isinstance(data, list):
        return {"records": data}
    if isinstance(data, Mapping):
        return dict(data)
    raise LedgerError("ingest 输入必须是对象或对象列表")


def cmd_facts(args) -> int:
    """事实投影：列出活跃 claim 的结构化视图。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    facts = ledger.list_facts(scope=args.scope, subject=args.subject,
                              limit=args.limit)
    if args.format == "json":
        _print({"facts": facts})
        return 0
    for fact in facts:
        print(f"[{fact['subject']}] {fact['predicate']}.{fact['slot_key']} = "
              f"{json.dumps(fact['value'], ensure_ascii=False)}  "
              f"(置信 {fact['confidence']:.2f} · {fact['source_agent']})")
    print(f"-- 共 {len(facts)} 条事实")
    return 0


def cmd_skills(args) -> int:
    """技能投影：列出 procedural 记忆的结构化视图。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    skills = ledger.list_skills(scope=args.scope, limit=args.limit)
    if args.format == "json":
        _print({"skills": skills})
        return 0
    for skill in skills:
        print(f"◆ {skill['content'][:60]}  (置信 {skill['confidence']:.2f} · "
              f"{skill['source_agent']})")
        for trigger in skill["trigger_pattern"][:3]:
            print(f"   触发: {trigger[:70]}")
        for step in skill["execution_pipeline"][:5]:
            action = step.get("action", "") if isinstance(step, dict) else str(step)
            print(f"   步骤: {action[:70]}")
    print(f"-- 共 {len(skills)} 条技能")
    return 0


def cmd_forget(args) -> int:
    """遗忘请求：软遗忘移除投影并落审计；--purge 连同事件内容一并清除。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    result = ledger.forget(scope=args.scope, before_time=args.before,
                           memory_ids=args.memory_id or [],
                           source_agent=args.source_agent, purge=args.purge,
                           import_batch=getattr(args, "batch", ""),
                           dry_run=bool(getattr(args, "dry_run", False)))
    _print(result)
    return 0


def cmd_resolve(args) -> int:
    """人工裁决 Slot-Value 冲突：本地直接操作账本（管理员维护操作）。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    result = ledger.resolve_claim(args.claim, args.winner,
                                  source_agent=args.source_agent)
    _print(result)
    return 0


def cmd_ingest(args) -> int:
    paths = _paths(args)
    payload = _ingest_record(args)
    try:
        result = _request(paths, _port(args, paths), "POST", "/ingest", payload,
                          token=_agent_token(args, paths))
    except LedgerError:
        # daemon 不可达时安全直写；daemon 实际在跑（错误另有原因，例如权限
        # 不足或只读模式拒绝）则原样抛出，绝不绕过 daemon 直写主账本。
        if _daemon_reachable(paths, _port(args, paths)):
            raise
        _reject_restricted_direct(args, "ingest 离线直写")
        ledger = SQLiteLedger(paths.ledger_path)
        ir = ledger.ingest(payload)
        result = {"results": [{"event_id": ir.event_id,
                               "memory_id": ir.memory_id,
                               "inserted": ir.inserted,
                               "content_hash": ir.content_hash}],
                  "mode": "offline-direct"}
    _print(result)
    return 0


def cmd_feedback(args) -> int:
    paths = _paths(args)
    body = {"memory_id": args.memory_id, "signal": args.signal, "note": args.note,
            "source_agent": args.source_agent, "feedback_id": args.feedback_id}
    try:
        result = _request(paths, _port(args, paths), "POST", "/feedback", body,
                          token=_agent_token(args, paths))
    except LedgerError:
        # daemon 不可达时安全直写；daemon 在跑（含明确拒绝）则原样抛出。
        if _daemon_reachable(paths, _port(args, paths)):
            raise
        ledger = SQLiteLedger(paths.ledger_path)
        updated = ledger.feedback(body["memory_id"], float(body["signal"]),
                                  str(body.get("note", "")),
                                  str(body.get("source_agent", "")))
        result = {"updated": bool(updated), "mode": "offline-direct"}
    _print(result)
    return 0


def cmd_pin(args) -> int:
    """置顶/取消置顶一条记忆（操作员手动加权）。"""
    _reject_restricted_direct(args, "pin")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    result = ledger.pin_memory(args.memory_id, pinned=not args.unpin)
    _print(result)


def cmd_conflicts(args) -> int:
    """列出冲突/被取代的 claim 谱系（resolve 的前置视图）。"""
    _reject_restricted_direct(args, "conflicts")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    report = ledger.list_conflicts(scope=args.scope or "shared",
                                   limit=max(1, min(1000, int(args.limit))))
    if args.format == "json":
        _print(report)
    else:
        if not report["items"]:
            print("(无冲突或被取代的 claim)")
        for item in report["items"]:
            preview = str(item.get("content") or "")[:50]
            print(f"{item['claim_id'][:18]}  [{item['state']}]  "
                  f"{item['subject']}.{item['predicate']}.{item['slot_key']}  "
                  f"conf={item['confidence']:.2f}  {preview}")
        print(f"共 {report['total']} 条", file=sys.stderr)
    return 0


def cmd_pins(args) -> int:
    """列出全部置顶记忆（跨 scope 概览）。"""
    _reject_restricted_direct(args, "pins")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    rows = ledger.list_pinned(scope=args.scope or "shared",
                              limit=max(1, min(1000, int(args.limit))))
    if args.format == "json":
        _print({"pinned": rows, "total": len(rows)})
    else:
        if not rows:
            print("(无置顶记忆)")
        for row in rows:
            print(f"{row['memory_id']}  [{row['kind']}]  {str(row['content'])[:60]}")
        print(f"共 {len(rows)} 条", file=sys.stderr)
    return 0
