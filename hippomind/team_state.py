"""Read the shared team task board and agent list from disk.

The Team tools expose this through the SDK, but a plain script (and a human
reading the repo) needs the same view without a live session.  The harness
keeps the shared board in the session projection cache:
``$DSH_HOME/storages/session_projcache/sessions/*.json`` each hold an
``agentTeam`` row with ``members`` and ``tasks``.  Missing files simply
produce empty lists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List


def _home() -> Path:
    configured = os.environ.get("DSH_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".dsh"


def _cache_files() -> List[Path]:
    cache = _home() / "storages" / "session_projcache" / "sessions"
    if not cache.is_dir():
        return []
    return sorted(cache.glob("*.json"), key=lambda item: item.stat().st_mtime,
                  reverse=True)[:40]


def _boards() -> List[Dict[str, Any]]:
    """Return every readable agentTeam board, newest session first."""
    boards: List[Dict[str, Any]] = []
    for path in _cache_files():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = value.get("record", {}).get("rows") if isinstance(value, dict) else None
        row = rows.get("agentTeam") if isinstance(rows, dict) else None
        board = row.get("val") if isinstance(row, dict) else None
        if isinstance(board, dict):
            boards.append(board)
    return boards


def read_tasks() -> List[Dict[str, Any]]:
    """Merge task lists across boards; the newest revision per id wins.

    The projection stores ``ownerId`` (a member id); resolve it to the member
    name so the resume checklist can say which teammate to wake.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    owners: Dict[str, str] = {}
    for board in reversed(_boards()):
        for member in board.get("members") or []:
            if isinstance(member, dict) and member.get("id") and member.get("name"):
                owners[str(member["id"])] = str(member["name"])
    for board in _boards():
        for task in board.get("tasks") or []:
            if not isinstance(task, dict) or not task.get("id"):
                continue
            current = merged.get(task["id"])
            if current is None or int(task.get("revision", 0) or 0) >= int(current.get("revision", 0) or 0):
                item = dict(task)
                owner_id = str(item.get("ownerId") or "")
                if owner_id and owners.get(owner_id):
                    item["ownerName"] = owners[owner_id]
                merged[task["id"]] = item
    return list(merged.values())


def read_agents() -> List[Dict[str, Any]]:
    """Merge member lists across boards; the newest board wins."""
    merged: Dict[str, Dict[str, Any]] = {}
    for board in reversed(_boards()):
        for member in board.get("members") or []:
            if not isinstance(member, dict) or not member.get("name"):
                continue
            merged[member["name"]] = member
    for member in merged.values():
        # The projection stores ``phase``; expose a status alias so
        # callers can use one vocabulary for live and on-disk state.
        member.setdefault("status", "running" if member.get("phase") == "active" else "inactive")
    return list(merged.values())
