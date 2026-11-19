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
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX 跨进程互斥；平台不支持时退化为进程内锁
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows 回退
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

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


@contextmanager
def _index_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _shared_ro_conn():
    conn = sqlite3.connect(f"file:{SHARED_DB}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


# ---------------- 任务存储（tasks.jsonl 权威，sqlite 派生索引） ----------------
#
# 审计 P2 修复（2026-11-13）：旧实现 save_tasks() 以 "w" 截断整文件再逐行重写，
# 崩溃窗口内数据不可恢复；sync_tasks_to_db() 则先 DELETE FROM tasks 再重建表。
# 现在的契约：
#   * tasks.jsonl 只做「读现有内容 -> 解析 -> 合并更新 -> 唯一临时文件 + os.replace」，
#     任何一步失败都保留旧文件原样。
#   * 读改写全程持进程内 RLock + POSIX 跨进程 flock（锁稳定 sidecar 文件，
#     因为 os.replace 会替换数据文件 inode，锁旧 inode 无法拦截后来者）。
#   * sqlite tasks 表只做单事务 upsert + 按 task_id 差集删除失效行，不清空重建。

_TASKS_LOCK = threading.RLock()
_TASKS_LOCK_DEPTH = threading.local()
_JSONL_NEWLINE = chr(10)


def _tasks_lock_path():
    """sidecar 锁文件：与 tasks.jsonl 同目录、同名 + .lock。"""
    return TASKS_FILE.with_name(TASKS_FILE.name + ".lock")


def _acquire_tasks_file_lock():
    """跨进程互斥；无 fcntl 的平台退化为仅进程内锁（Windows 回退）。"""
    lock_path = _tasks_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                # 个别网络/便携文件系统不支持 flock：保持进程内互斥继续工作。
                pass
        elif msvcrt is not None:  # pragma: no cover - Windows
            try:  # 锁被占用时退化为进程内互斥，不让 CLI 卡死
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            except OSError:
                pass
        return fd
    except BaseException:
        os.close(fd)
        raise


def _release_tasks_file_lock(fd):
    """释放并关闭 sidecar 锁文件；失败不掩盖调用方异常。"""
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        elif msvcrt is not None:  # pragma: no cover - Windows
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        os.close(fd)


@contextmanager
def _tasks_transaction_lock():
    """序列化 tasks.jsonl 读改写；嵌套调用只加一次跨进程锁。"""
    depth = getattr(_TASKS_LOCK_DEPTH, "depth", 0)
    _TASKS_LOCK.acquire()
    _TASKS_LOCK_DEPTH.depth = depth + 1
    lock_fd = None
    try:
        if depth == 0:
            lock_fd = _acquire_tasks_file_lock()
        yield
    finally:
        if lock_fd is not None:
            _release_tasks_file_lock(lock_fd)
        _TASKS_LOCK_DEPTH.depth = depth
        _TASKS_LOCK.release()


def _normalise_tasks(tasks):
    """按 task_id 去重（后写覆盖先写）并保持首次出现顺序。"""
    by_id = {}
    for item in tasks:
        if (isinstance(item, dict) and isinstance(item.get("task_id"), str)
                and item["task_id"]):
            by_id[item["task_id"]] = item
    return list(by_id.values())


def _read_tasks_document():
    """解析 tasks.jsonl，返回 (有效任务列表, 无法解析的原始行)。

    坏行不丢弃：写回时原样追加，避免一次原子替换把旧文件里的脏行静默清掉。
    """
    if not TASKS_FILE.exists():
        return [], []
    tasks, invalid = [], []
    for line in TASKS_FILE.read_text("utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except (TypeError, ValueError):
            invalid.append(line)
            continue
        if (isinstance(item, dict) and isinstance(item.get("task_id"), str)
                and item["task_id"]):
            tasks.append(item)
        else:
            invalid.append(line)
    return _normalise_tasks(tasks), invalid


def load_tasks():
    """读取任务列表；读路径无锁（os.replace 保证读到某个完整版本）。"""
    tasks, _invalid = _read_tasks_document()
    return tasks


def _serialise_tasks(tasks, invalid_lines):
    lines = [json.dumps(t, ensure_ascii=False) for t in tasks]
    lines.extend(invalid_lines)
    return "".join(line + _JSONL_NEWLINE for line in lines)


def _fsync_directory(directory):
    """best-effort 目录项落盘，确保 rename 在掉电后可见。"""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_tasks(tasks, invalid_lines):
    """同目录唯一临时文件写入 + fsync + os.replace；失败删除临时文件。"""
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=TASKS_FILE.name + ".", suffix=".tmp",
        dir=str(TASKS_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(_serialise_tasks(tasks, invalid_lines))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_name, 0o644)
        except OSError:
            pass
        os.replace(tmp_name, TASKS_FILE)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _fsync_directory(TASKS_FILE.parent)


def save_tasks(tasks):
    """原子保存完整任务列表（兼容旧调用方；保留历史坏行）。"""
    with _tasks_transaction_lock():
        _tasks, invalid = _read_tasks_document()
        _atomic_write_tasks(_normalise_tasks(list(tasks)), invalid)


def _commit_task_change(mutator):
    """锁内 读 -> 合并更新 -> 原子替换 -> 派生索引同步。

    mutator(tasks) 返回 True 表示确有变更；无变更时不写盘（重复更新幂等）。
    返回 (tasks, changed)。
    """
    with _tasks_transaction_lock():
        tasks, invalid = _read_tasks_document()
        changed = bool(mutator(tasks))
        if not changed:
            return tasks, False
        _atomic_write_tasks(tasks, invalid)
        # 同一把锁内同步 sqlite：提交顺序与 JSONL 一致，后写者用完整合并
        # 结果覆盖索引，不会把并发写入的行误删。
        sync_tasks_to_db(tasks)
        return tasks, True


_TASKS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS tasks ("
    "task_id TEXT PRIMARY KEY, "
    "title TEXT NOT NULL DEFAULT '', "
    "description TEXT NOT NULL DEFAULT '', "
    "status TEXT NOT NULL DEFAULT 'open', "
    "agent TEXT NOT NULL DEFAULT '', "
    "created_at TEXT NOT NULL DEFAULT '', "
    "updated_at TEXT NOT NULL DEFAULT '', "
    "source_session TEXT NOT NULL DEFAULT '', "
    "tags TEXT NOT NULL DEFAULT '[]')"
)


def sync_tasks_to_db(tasks):
    """单事务同步派生索引：upsert 全量 + 仅删除失效行，绝不清空重建。

    索引库缺失/同步失败只告警：tasks.jsonl 是唯一权威来源，已落盘的数据
    不会因派生索引问题回滚。
    """
    payload = _normalise_tasks(list(tasks))
    if not INDEX_DB.exists():
        print(f"警告: 任务已写入 {TASKS_FILE}；索引 {INDEX_DB} 不存在，跳过 sqlite 同步。",
              file=sys.stderr)
        return False
    conn = sqlite3.connect(INDEX_DB, timeout=10.0)
    try:
        with conn:  # 单事务：成功 commit；异常整体 rollback，旧行保持可读
            conn.execute(_TASKS_TABLE_SQL)
            for t in payload:
                conn.execute(
                    "INSERT OR REPLACE INTO tasks"
                    " (task_id,title,description,status,agent,created_at,"
                    "updated_at,source_session,tags) VALUES (?,?,?,?,?,?,?,?,?)",
                    (t["task_id"], t.get("title", ""), t.get("description", ""),
                     t.get("status", "open"), t.get("agent", ""),
                     t.get("created_at", ""), t.get("updated_at", ""),
                     t.get("source_session", ""),
                     json.dumps(t.get("tags", []), ensure_ascii=False)))
            keep = {t["task_id"] for t in payload}
            existing = [row[0] for row in
                        conn.execute("SELECT task_id FROM tasks").fetchall()]
            for task_id in existing:
                if task_id not in keep:
                    conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        return True
    except sqlite3.Error as exc:
        print(f"警告: 任务已写入 {TASKS_FILE}；sqlite 同步失败: {exc}",
              file=sys.stderr)
        return False
    finally:
        conn.close()


def find_task(task_id):
    for t in load_tasks():
        if t["task_id"] == task_id:
            return t
    return None


def create_task(title, description="", status="open", agent="unknown",
                session="", tags=(), task_id=None):
    """创建任务：锁内读现有文件后追加，原子替换；返回新任务 dict。"""
    now = utcnow_iso()
    task = {
        "task_id": str(task_id or uuid.uuid4()),
        "title": str(title or ""),
        "description": str(description or ""),
        "status": status if status in VALID_STATUS else "open",
        "agent": str(agent or "unknown"),
        "created_at": now,
        "updated_at": now,
        "source_session": str(session or ""),
        "tags": list(tags or []),
        "notes": [],
    }

    def mutate(tasks):
        tasks.append(task)
        return True

    _commit_task_change(mutate)
    return task


def update_task(task_id, status=None, note=None, agent=None):
    """更新任务；无实际变化时不写盘，因此重复执行同一更新是幂等的。"""
    if status is not None and status not in VALID_STATUS:
        raise ValueError(f"无效状态 {status}（可选 {VALID_STATUS}）")
    result = {}

    def mutate(tasks):
        target = next((item for item in tasks if item["task_id"] == task_id), None)
        if target is None:
            return False
        changed = False
        if status is not None and target.get("status") != status:
            target["status"] = status
            changed = True
        if note:
            target.setdefault("notes", []).append(
                {"ts": utcnow_iso(), "agent": agent or "unknown", "text": note})
            changed = True
        if agent and target.get("agent") != agent:
            target["agent"] = agent
            changed = True
        if changed:
            target["updated_at"] = utcnow_iso()
        result["task"] = target
        return changed

    _commit_task_change(mutate)
    return result.get("task")


def cmd_task_add(args):
    task = create_task(args.title, args.desc or "", args.status,
                       args.agent or "unknown", args.session or "",
                       args.tags or [])
    print(f"✅ 任务已创建: {task['task_id']}")
    print(f"   标题: {task['title']}")
    print(f"   状态: {task['status']} | 创建者: {task['agent']}")
    return task["task_id"]


def cmd_task_update(args):
    if args.status and args.status not in VALID_STATUS:
        print(f"错误: 无效状态 {args.status}（可选 {VALID_STATUS}）", file=sys.stderr)
        sys.exit(1)
    task = update_task(args.task_id, args.status, args.note, args.agent)
    if task is None:
        print(f"错误: 任务不存在 {args.task_id}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ 任务已更新: {args.task_id} → {task.get('status')}")
    return task


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
        with _shared_ro_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
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
    with _index_conn() as conn:
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
    with _index_conn() as conn:
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
    with _index_conn() as conn:
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
    if not rows:
        print("（无会话）")
        return
    print(f"📚 共 {len(rows)} 个会话:\n")
    for agent, sid, cnt, last_ts, first_user in rows:
        hint = (first_user or "").replace("\n", " ")[:50]
        print(f"[{agent}] {sid[:8]}  {cnt}条消息  最后{str(last_ts)[:16]}  {hint}")


def cmd_stats(args):
    with _index_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT agent, COUNT(*), COUNT(DISTINCT session_id) FROM messages GROUP BY agent")
        rows = cur.fetchall()
    print("📊 收录统计:")
    print(f"  {'Agent':<12} {'消息数':>8} {'会话数':>6}")
    total_m, total_s = 0, 0
    for agent, m, s in rows:
        print(f"  {agent:<12} {m:>8} {s:>6}")
        total_m += m; total_s += s
    print(f"  {'合计':<12} {total_m:>8} {total_s:>6}")
    if SHARED_DB.exists():
        try:
            with _shared_ro_conn() as conn:
                shared_count = conn.execute("SELECT COUNT(*) FROM shared_memories").fetchone()[0]
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
            with _index_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT agent, ts, role, content FROM messages WHERE session_id=? ORDER BY ts LIMIT 30",
                    (t["source_session"],))
                for agent, ts, role, content in cur.fetchall():
                    snippet = (content or "").replace("\n", " ")[:120]
                    print(f"  [{agent}] {role} {str(ts)[:16]}: {snippet}")
        print("=" * 60)
    elif args.session:
        with _index_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT agent, session_id, ts, role, content FROM messages WHERE session_id=? ORDER BY ts LIMIT 50",
                (args.session,))
            rows = cur.fetchall()
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
