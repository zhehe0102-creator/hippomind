#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Chat Query —— 全电脑 Agent 记忆共通查询工具

任何 agent（Codex / Claude / Pi / Gemini / DSH 等）都可以通过本工具
访问全部已收录的聊天记录与任务，实现记录共通、任务共通。

用法:
  python3 query.py search "关键词" [--agent codex] [--limit 20]
  python3 query.py recent [--agent X] [--limit 20] [--role user|assistant]
  python3 query.py sessions [--agent X]
  python3 query.py tasks [--status open|in_progress|done|all] [--agent X]
  python3 query.py task-add "标题" --desc "描述" [--agent X] [--status in_progress]
  python3 query.py task-update <task_id> --status done --note "接续说明"
  python3 query.py task-show <task_id>
  python3 query.py stats
  python3 query.py context --task <task_id>     # 生成任务上下文摘要（供接续 agent 使用）
  python3 query.py context --session <session_id>

统一 schema:
  messages: agent, session_id, event_id, ts, role, content, model, cwd, title, source_file
  tasks:    task_id, title, description, status, agent, created_at, updated_at, source_session, tags
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE = Path(os.environ.get("AGENT_ARCHIVE", Path(__file__).resolve().parents[1]))
INDEX_DB = ARCHIVE / "index" / "index.sqlite"
TASKS_FILE = ARCHIVE / "tasks" / "tasks.jsonl"
SHARED_DB = ARCHIVE / "index" / "hippomind-commons.sqlite"

VALID_STATUS = ("open", "in_progress", "blocked", "done", "archived")
VALID_ROLES = ("user", "assistant", "system", "tool")


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def connect():
    if not INDEX_DB.exists():
        print(f"错误: 索引不存在 {INDEX_DB}。请先运行 collect.py 收录记录。", file=sys.stderr)
        sys.exit(2)
    return sqlite3.connect(INDEX_DB)


# ---------------- 任务存储（tasks.jsonl 为主，同步到 sqlite） ----------------

def load_tasks():
    if not TASKS_FILE.exists():
        return []
    tasks = []
    for line in TASKS_FILE.read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                tasks.append(json.loads(line))
            except Exception:
                pass
    return tasks


def save_tasks(tasks):
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TASKS_FILE, "w", encoding="utf-8") as fh:
        for t in tasks:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")


def sync_tasks_to_db(tasks):
    conn = connect()
    conn.execute("DELETE FROM tasks")
    for t in tasks:
        conn.execute(
            "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
            (t["task_id"], t.get("title", ""), t.get("description", ""),
             t.get("status", "open"), t.get("agent", ""),
             t.get("created_at", ""), t.get("updated_at", ""),
             t.get("source_session", ""),
             json.dumps(t.get("tags", []), ensure_ascii=False)))
    conn.commit()
    conn.close()


def find_task(task_id):
    for t in load_tasks():
        if t["task_id"] == task_id:
            return t
    return None


def cmd_task_add(args):
    tasks = load_tasks()
    task_id = str(uuid.uuid4())
    now = utcnow_iso()
    t = {
        "task_id": task_id,
        "title": args.title,
        "description": args.desc or "",
        "status": args.status if args.status in VALID_STATUS else "open",
        "agent": args.agent or "unknown",
        "created_at": now,
        "updated_at": now,
        "source_session": args.session or "",
        "tags": args.tags or [],
        "notes": [],
    }
    tasks.append(t)
    save_tasks(tasks)
    sync_tasks_to_db(tasks)
    print(f"✅ 任务已创建: {task_id}")
    print(f"   标题: {args.title}")
    print(f"   状态: {t['status']} | 创建者: {t['agent']}")
    return task_id


def cmd_task_update(args):
    tasks = load_tasks()
    # [FIX] 必须在同一个列表对象上修改，否则 find_task 返回的是副本
    t = next((x for x in tasks if x["task_id"] == args.task_id), None)
    if not t:
        print(f"错误: 任务不存在 {args.task_id}", file=sys.stderr)
        sys.exit(1)
    if args.status:
        if args.status not in VALID_STATUS:
            print(f"错误: 无效状态 {args.status}（可选 {VALID_STATUS}）", file=sys.stderr)
            sys.exit(1)
        t["status"] = args.status
    if args.note:
        t.setdefault("notes", []).append({
            "ts": utcnow_iso(), "agent": args.agent or "unknown", "text": args.note})
    if args.agent:
        t["agent"] = args.agent
    t["updated_at"] = utcnow_iso()
    save_tasks(tasks)
    sync_tasks_to_db(tasks)
    print(f"✅ 任务已更新: {args.task_id} → {t['status']}")
    return t


def cmd_task_show(args):
    t = find_task(args.task_id)
    if not t:
        print(f"错误: 任务不存在 {args.task_id}", file=sys.stderr)
        sys.exit(1)
    print(f"任务: {t['task_id']}")
    print(f"  标题: {t.get('title')}")
    print(f"  状态: {t.get('status')} | 负责agent: {t.get('agent')}")
    print(f"  创建: {t.get('created_at')}")
    print(f"  更新: {t.get('updated_at')}")
    if t.get("description"):
        print(f"  描述: {t['description']}")
    if t.get("tags"):
        print(f"  标签: {', '.join(t['tags'])}")
    for n in t.get("notes", []):
        print(f"  📝 [{n.get('ts','')[:19]}] {n.get('agent')}: {n.get('text')}")


def cmd_tasks(args):
    tasks = load_tasks()
    if args.status and args.status != "all":
        tasks = [t for t in tasks if t.get("status") == args.status]
    if args.agent:
        tasks = [t for t in tasks if t.get("agent") == args.agent]
    if not tasks:
        print("（无任务）")
        return
    for t in sorted(tasks, key=lambda x: x.get("updated_at", ""), reverse=True):
        print(f"{t.get('status','?'):<11} {t['task_id'][:8]}  [{t.get('agent','?'):<10}] {t.get('title','')[:60]}  (更新 {t.get('updated_at','')[:16]})")


# ---------------- 查询 ----------------

def shared_terms(query):
    terms = re.findall(r"[a-z0-9_]{2,}", query.lower())
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        terms.extend([run] if len(run) <= 2 else
                     [run[i:i + 2] for i in range(len(run) - 1)])
    return list(dict.fromkeys(terms))[:10]


def search_shared(query, agent=None, limit=5):
    """兼容查询 hippomind 的跨 Agent 共享语义账本。"""
    if not SHARED_DB.exists() or limit <= 0:
        return []
    terms = shared_terms(query)
    if not terms:
        return []
    where = " OR ".join("LOWER(content) LIKE ?" for _ in terms)
    params = [f"%{term}%" for term in terms]
    sql = ("SELECT memory_id,content,kind,source_agent,source_session,confidence,"
           "source_refs,updated_at FROM shared_memories WHERE (" + where + ") "
           "AND (superseded_by IS NULL OR superseded_by='') "
           "AND (valid_until IS NULL OR valid_until='' OR valid_until>?)")
    params.append(utcnow_iso())
    if agent:
        sql += " AND source_agent=?"
        params.append(agent)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(limit * 20, 50))
    try:
        conn = sqlite3.connect(f"file:{SHARED_DB}?mode=ro", uri=True)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    ranked = []
    for row in rows:
        lowered = (row[1] or "").lower()
        coverage = sum(1 for term in terms if term in lowered) / len(terms)
        ranked.append((coverage + 0.1 * float(row[5]), row))
    ranked.sort(reverse=True, key=lambda item: item[0])
    return [row for _, row in ranked[:limit]]

def cmd_search(args):
    conn = connect()
    cur = conn.cursor()
    sql = "SELECT agent, session_id, ts, role, content, title FROM messages WHERE content LIKE ?"
    params = [f"%{args.query}%"]
    if args.agent:
        sql += " AND agent = ?"
        params.append(args.agent)
    if args.role:
        sql += " AND role = ?"
        params.append(args.role)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(args.limit)
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    shared = search_shared(args.query, args.agent, min(5, args.limit))
    if not rows and not shared:
        print(f"（未找到包含 '{args.query}' 的记录）")
        return
    total = min(args.limit, len(shared) + len(rows))
    print(f"🔍 搜索 '{args.query}': {total} 条结果\n")
    for mid, content, kind, agent, sid, confidence, refs, updated in shared:
        snippet = (content or "").replace("\n", " ")[:160]
        print(f"[共享记忆/{agent}] {mid[:18]} {kind} 置信{float(confidence):.0%}")
        print(f"  {snippet}")
        print(f"  证据: {refs[:140]}")
        print()
    remaining = max(0, args.limit - len(shared))
    for agent, sid, ts, role, content, title in rows[:remaining]:
        snippet = (content or "").replace("\n", " ")[:160]
        print(f"[{agent}] {sid[:8]} {role} {str(ts)[:16]}")
        print(f"  {snippet}")
        print()


def cmd_recent(args):
    conn = connect()
    cur = conn.cursor()
    sql = "SELECT agent, session_id, ts, role, content, title FROM messages"
    conds, params = [], []
    if args.agent:
        conds.append("agent = ?"); params.append(args.agent)
    if args.role:
        conds.append("role = ?"); params.append(args.role)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(args.limit)
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("（暂无记录）")
        return
    print(f"🕐 最近 {len(rows)} 条消息:\n")
    for agent, sid, ts, role, content, title in rows:
        snippet = (content or "").replace("\n", " ")[:140]
        print(f"[{agent}] {sid[:8]} {role:<9} {str(ts)[:16]}")
        print(f"  {snippet}")
        print()


def cmd_sessions(args):
    conn = connect()
    cur = conn.cursor()
    sql = ("SELECT agent, session_id, COUNT(*), MAX(ts), "
           "MAX(CASE WHEN role='user' THEN content END) FROM messages")
    conds, params = [], []
    if args.agent:
        conds.append("agent = ?"); params.append(args.agent)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " GROUP BY agent, session_id ORDER BY MAX(ts) DESC"
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("（无会话）")
        return
    print(f"📚 共 {len(rows)} 个会话:\n")
    for agent, sid, cnt, last_ts, first_user in rows:
        hint = (first_user or "").replace("\n", " ")[:50]
        print(f"[{agent}] {sid[:8]}  {cnt}条消息  最后{str(last_ts)[:16]}  {hint}")


def cmd_stats(args):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT agent, COUNT(*), COUNT(DISTINCT session_id) FROM messages GROUP BY agent")
    rows = cur.fetchall()
    conn.close()
    print("📊 收录统计:")
    print(f"  {'Agent':<12} {'消息数':>8} {'会话数':>6}")
    total_m, total_s = 0, 0
    for agent, m, s in rows:
        print(f"  {agent:<12} {m:>8} {s:>6}")
        total_m += m; total_s += s
    print(f"  {'合计':<12} {total_m:>8} {total_s:>6}")
    if SHARED_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{SHARED_DB}?mode=ro", uri=True)
            shared_count = conn.execute("SELECT COUNT(*) FROM shared_memories").fetchone()[0]
            conn.close()
            print(f"  {'共享语义记忆':<12} {shared_count:>8}")
        except sqlite3.Error:
            pass
    tasks = load_tasks()
    print(f"\n  任务: {len(tasks)} 个（{sum(1 for t in tasks if t.get('status')=='open')} open / "
          f"{sum(1 for t in tasks if t.get('status')=='in_progress')} in_progress / "
          f"{sum(1 for t in tasks if t.get('status')=='done')} done）")


def cmd_context(args):
    """生成任务/会话上下文摘要，供接续 agent 快速了解全貌"""
    if args.task:
        t = find_task(args.task)
        if not t:
            print(f"错误: 任务不存在 {args.task}", file=sys.stderr)
            sys.exit(1)
        print("=" * 60)
        print(f"📋 任务接续上下文 — {t['title']}")
        print("=" * 60)
        print(f"任务ID: {t['task_id']}")
        print(f"状态:   {t['status']} | 负责agent: {t.get('agent')}")
        print(f"描述:   {t.get('description')}")
        print(f"\n进展记录:")
        for n in t.get("notes", []):
            print(f"  [{n.get('ts','')[:16]}] {n.get('agent')}: {n.get('text')}")
        print(f"\n相关聊天记录（该任务的 source_session 或最近相关消息）:")
        if t.get("source_session"):
            conn = connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT agent, ts, role, content FROM messages WHERE session_id=? ORDER BY ts LIMIT 30",
                (t["source_session"],))
            for agent, ts, role, content in cur.fetchall():
                snippet = (content or "").replace("\n", " ")[:120]
                print(f"  [{agent}] {role} {str(ts)[:16]}: {snippet}")
            conn.close()
        print("=" * 60)
    elif args.session:
        conn = connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT agent, session_id, ts, role, content FROM messages WHERE session_id=? ORDER BY ts LIMIT 50",
            (args.session,))
        rows = cur.fetchall()
        conn.close()
        if not rows:
            print(f"（会话 {args.session} 无记录）")
            return
        print("=" * 60)
        print(f"💬 会话上下文 — {args.session[:12]}")
        print("=" * 60)
        for agent, sid, ts, role, content in rows:
            snippet = (content or "").replace("\n", " ")[:200]
            print(f"[{agent}] {role} {str(ts)[:16]}: {snippet}")
        print("=" * 60)
    else:
        print("用法: query.py context --task <id> | --session <id>", file=sys.stderr)
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description="Agent Chat Query（全 agent 记忆共通）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="全文搜索聊天记录")
    p.add_argument("query"); p.add_argument("--agent"); p.add_argument("--role")
    p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_search)

    p = sub.add_parser("recent", help="最近消息")
    p.add_argument("--agent"); p.add_argument("--role")
    p.add_argument("--limit", type=int, default=20); p.set_defaults(func=cmd_recent)

    p = sub.add_parser("sessions", help="会话列表")
    p.add_argument("--agent"); p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("tasks", help="任务列表")
    p.add_argument("--status", default="all")
    p.add_argument("--agent"); p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("task-add", help="创建任务")
    p.add_argument("title"); p.add_argument("--desc", default="")
    p.add_argument("--agent", default="unknown"); p.add_argument("--status", default="open")
    p.add_argument("--tags", nargs="*", default=[]); p.add_argument("--session", default="")
    p.set_defaults(func=cmd_task_add)

    p = sub.add_parser("task-update", help="更新任务")
    p.add_argument("task_id"); p.add_argument("--status")
    p.add_argument("--note"); p.add_argument("--agent"); p.set_defaults(func=cmd_task_update)

    p = sub.add_parser("task-show", help="查看任务详情")
    p.add_argument("task_id"); p.set_defaults(func=cmd_task_show)

    p = sub.add_parser("stats", help="收录统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("context", help="任务/会话接续上下文")
    p.add_argument("--task"); p.add_argument("--session"); p.set_defaults(func=cmd_context)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
