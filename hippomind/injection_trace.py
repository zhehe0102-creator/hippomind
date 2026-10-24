"""Bounded, privacy-preserving injection traces.

Every automatic injection writes one JSONL line: agent, query hash, item count,
context characters, timestamp and whether it was injected.  The raw query and
memory text are deliberately never stored here; the trace is for tuning the
relevance gate and proving that injection actually ran.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


def trace_path() -> Path:
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "injection.jsonl"


def _hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def record(*, agent: str, query: str, context: str = "",
           item_count: int = 0, injected: bool = True,
           source: str = "auto") -> Dict[str, Any]:
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent": str(agent or "")[:64],
        "source": str(source or "auto")[:64],
        "query_hash": _hash(query),
        "query_chars": len(str(query or "")),
        "items": int(item_count),
        "chars": len(str(context or "")),
        "injected": bool(injected),
    }
    path = trace_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        return entry
    return entry


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    path = trace_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, int(limit)):]
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, Mapping):
            rows.append(dict(value))
    return rows


def summary() -> Dict[str, Any]:
    rows = recent(200)
    if not rows:
        return {"count": 0, "last_at": "", "last_agent": "", "injected": 0}
    return {"count": len(rows), "last_at": rows[-1].get("at", ""),
            "last_agent": rows[-1].get("agent", ""),
            "injected": sum(1 for row in rows if row.get("injected"))}
