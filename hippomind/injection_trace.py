"""Bounded, privacy-preserving injection traces.

Every automatic injection writes one JSONL line: agent, query hash, item count,
context characters, timestamp and whether it was injected.  The raw query and
memory text are deliberately never stored here; the trace is for tuning the
relevance gate and proving that injection actually ran.

The file is append-only and was previously read whole by the dashboard health
check.  Every reader here is bounded instead:

* recent (and therefore summary and the daemon health check) seeks to the last
  TAIL_READ_BYTES and parses only that window, so a large trace never turns a
  dashboard poll into a full-file read;
* lines are written with mode 0600 because they record which memories an agent
  was offered;
* the writer rotates once at MAX_TRACE_BYTES, keeping one bounded .1
  generation so a long-running install cannot grow without bound;
* time-based retention (HIPPOMIND_TRACE_RETENTION_DAYS, default 30 days,
  0/off disables) drops lines older than the cutoff from the live
  file at most once per process per hour.  The pass keeps unparsable
  lines verbatim, rewrites via a private temp file + os.replace, and
  leaves mode 0600 intact.  The .1 rotation generation is historical
  evidence and is deliberately not pruned.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: Bounded tail read for the read/health path (last 256 KiB).
TAIL_READ_BYTES = 256 * 1024
#: Rotation threshold for the append-only trace file.
MAX_TRACE_BYTES = 8 * 1024 * 1024
#: Trace lines describe injection targets, so the file itself stays private.
TRACE_FILE_MODE = 0o600
#: Default time-based retention for the live trace file.  0 disables it.
DEFAULT_RETENTION_DAYS = 30
#: Environment knob: integer days; 0/off/false/no/disabled turn it off.
RETENTION_ENV = "HIPPOMIND_TRACE_RETENTION_DAYS"
#: At most one retention pass per process per hour.
RETENTION_CHECK_INTERVAL_SECONDS = 3600.0

_write_lock = threading.Lock()
_retention_lock = threading.Lock()
_last_retention_check = 0.0
_last_retention_result: Dict[str, Any] = {}


def trace_path() -> Path:
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "injection.jsonl"


def _hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _rotate_if_needed(path: Path) -> None:
    """Rotate the trace once it exceeds MAX_TRACE_BYTES (keep one backup)."""
    try:
        if path.stat().st_size >= MAX_TRACE_BYTES:
            os.replace(str(path), str(path) + ".1")
    except OSError:
        return


def _append_private_line(path: Path, line: str) -> None:
    """Append one line to a 0600 file with bounded rotation and retention."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        try:
            # Throttled to one pass per process per hour; retention must
            # never be able to block (or break) the tracing path.
            prune_expired()
        except Exception:
            pass
        _rotate_if_needed(path)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                     TRACE_FILE_MODE)
        try:
            payload = line.encode("utf-8")
            while payload:
                written = os.write(fd, payload)
                payload = payload[written:]
        finally:
            os.close(fd)
        try:
            # Existing files written by older releases may be group/world readable;
            # tighten them on the first append instead of only on creation.
            os.chmod(path, TRACE_FILE_MODE)
        except OSError:
            pass


def _tail_lines(path: Path, limit: int) -> List[str]:
    """Return the last *limit* lines using a bounded read from the file tail."""
    size = path.stat().st_size
    if size <= 0:
        return []
    start = max(0, size - TAIL_READ_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        chunk = handle.read()
    lines = chunk.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        # The window starts mid-line; drop the partial first line.
        lines = lines[1:]
    return lines[-limit:]


def retention_days() -> int:
    """Effective retention in days; 0 means time-based pruning is off.

    Anything that is not a non-negative integer falls back to the default,
    and an explicit off value is honoured: privacy cleanup must be easy to
    disable without editing code.
    """
    raw = str(os.environ.get(RETENTION_ENV, "") or "").strip().lower()
    if raw in {"0", "off", "false", "no", "disabled"}:
        return 0
    if not raw or raw == "default":
        return DEFAULT_RETENTION_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(0, min(3650, value))


def parse_trace_at(value: Any) -> Optional[float]:
    """Parse an entry timestamp into epoch seconds; None when unknown."""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def _atomic_replace_bytes(path: Path, payload: bytes) -> None:
    """Write one private temp file next to *path* and atomically replace it."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".retention.tmp",
        dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, TRACE_FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _prune_document(path: Path, cutoff: float) -> Dict[str, Any]:
    """Drop live-file lines older than cutoff; keep unparsable lines verbatim.

    Only the current file is pruned: the .1 generation is rotation history,
    kept as evidence, not part of the live observation stream.
    """
    raw = path.read_bytes()
    try:
        before = path.stat()
    except OSError:
        before = None
    kept: List[bytes] = []
    removed = 0
    invalid = 0
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # Unparsable bytes may be the only surviving audit evidence of an
            # older writer; retention must never drop them silently.
            kept.append(line)
            invalid += 1
            continue
        at = parse_trace_at(value.get("at")) if isinstance(value, dict) else None
        if at is not None and at < cutoff:
            removed += 1
            continue
        kept.append(line)
    if removed == 0:
        return {"pruned": False, "removed": 0, "kept": len(kept),
                "invalid": invalid, "reason": "nothing_expired"}
    # A concurrent append between read and replace would be lost by the
    # replacement.  Abort instead of risking trace loss; next pass retries.
    try:
        after = path.stat()
    except OSError:
        after = None
    if (before is not None and after is not None
            and (before.st_size != after.st_size
                 or before.st_mtime_ns != after.st_mtime_ns)):
        return {"pruned": False, "removed": 0, "kept": len(kept),
                "invalid": invalid, "reason": "concurrent_change"}
    payload = b"\n".join(line for line in kept) + b"\n"
    _atomic_replace_bytes(path, payload)
    return {"pruned": True, "removed": removed, "kept": len(kept),
            "invalid": invalid, "reason": "expired"}


def prune_expired(*, now: Optional[float] = None,
                  force: bool = False) -> Dict[str, Any]:
    """Time-based retention for the live trace file (throttled, atomic).

    The pass runs at most once per process per hour unless *force* is set,
    which is what keeps a hot append path from paying a full-file scan on
    every trace.  It never raises: retention is housekeeping, not part of
    the reporting contract.
    """
    global _last_retention_check, _last_retention_result
    days = retention_days()
    if days <= 0:
        return {"enabled": False, "days": 0, "removed": 0,
                "pruned": False, "reason": "disabled"}
    stamp = time.time() if now is None else float(now)
    with _retention_lock:
        if (not force
                and stamp - _last_retention_check < RETENTION_CHECK_INTERVAL_SECONDS):
            result = dict(_last_retention_result)
            result["throttled"] = True
            return result
        _last_retention_check = stamp
        cutoff = stamp - days * 86400.0
        path = trace_path()
        if not path.is_file():
            outcome: Dict[str, Any] = {"pruned": False, "removed": 0,
                                       "kept": 0, "invalid": 0,
                                       "reason": "missing"}
        else:
            try:
                outcome = _prune_document(path, cutoff)
            except (OSError, ValueError) as exc:
                outcome = {"pruned": False, "removed": 0, "kept": 0,
                           "invalid": 0,
                           "error": type(exc).__name__}
        result = {"enabled": True, "days": days, "cutoff": cutoff,
                  "checked_at": stamp, "throttled": False}
        result.update(outcome)
        _last_retention_result = dict(result)
        return result


def reset_retention_state() -> None:
    """Reset the per-process retention throttle (tests / manual maintenance)."""
    global _last_retention_check, _last_retention_result
    with _retention_lock:
        _last_retention_check = 0.0
        _last_retention_result = {}


def record(*, agent: str, query: str, context: str = "",
           item_count: int = 0, injected: bool = True,
           source: str = "auto",
           memory_ids: Sequence[str] = (),
           arm: str = "",
           arm_reason: str = "") -> Dict[str, Any]:
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent": str(agent or "")[:64],
        "source": str(source or "auto")[:64],
        "query_hash": _hash(query),
        "query_chars": len(str(query or "")),
        "items": int(item_count),
        "chars": len(str(context or "")),
        "injected": bool(injected),
        # Which memories were offered, so a later pass can check whether the
        # agent's next message actually reused any of them.  Bounded and never
        # the memory text itself.
        "memory_ids": [str(item)[:80] for item in list(memory_ids)[:8]],
    }
    if arm:
        entry["arm"] = str(arm or "")[:16]
    if arm_reason:
        entry["arm_reason"] = str(arm_reason or "")[:32]
    path = trace_path()
    try:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        _append_private_line(path, line)
    except OSError:
        return entry
    return entry


def record_server_injection(*, agent: str, query: str, injected: bool,
                            item_count: int = 0, context_chars: int = 0,
                            memory_ids: Sequence[str] = (),
                            empty_reason: str = "", latency_ms: float = 0.0,
                            scope: str = "", task_id: str = "",
                            error: str = "", arm: str = "",
                            source: str = "http") -> Dict[str, Any]:
    """Record one HTTP-side injection decision, including empty/control runs.

    Kept separate from record() so CLI/hook callers stay byte-compatible while
    the daemon can add server-only fields: empty_reason, latency_ms, scope/
    task_id and error.  Raw query and memory text are still never stored; only
    their hash/length/id projection reaches the file.
    """
    try:
        latency = max(0.0, min(600_000.0, float(latency_ms)))
    except (TypeError, ValueError):
        latency = 0.0
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent": str(agent or "http")[:64] or "http",
        "source": str(source or "http")[:64],
        "query_hash": _hash(query),
        "query_chars": len(str(query or "")),
        "items": int(item_count),
        "chars": int(context_chars),
        "injected": bool(injected),
        "memory_ids": [str(item)[:80] for item in list(memory_ids)[:8]],
        "scope": str(scope or "")[:128],
        "task_id": str(task_id or "")[:128],
        "latency_ms": round(latency, 1),
        "empty_reason": str(empty_reason or "")[:160],
    }
    if arm:
        entry["arm"] = str(arm)[:32]
    if error:
        entry["error"] = str(error)[:80]
    path = trace_path()
    try:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        _append_private_line(path, line)
    except OSError:
        return entry
    return entry

def recent(limit: int = 20) -> List[Dict[str, Any]]:
    path = trace_path()
    if not path.is_file():
        return []
    try:
        lines = _tail_lines(path, max(1, int(limit)))
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
    """Bounded window summary over the same tail-read view as recent()."""
    days = retention_days()
    retention = {
        "days": days,
        "enabled": days > 0,
        "check_interval_seconds": int(RETENTION_CHECK_INTERVAL_SECONDS),
        "last_check_at": round(_last_retention_check, 3),
        "last_removed": int(_last_retention_result.get("removed") or 0),
    }
    rows = recent(200)
    retention["last_pruned"] = bool(_last_retention_result.get("pruned"))
    if not rows:
        return {"count": 0, "last_at": "", "last_agent": "", "injected": 0,
                "empty": 0, "errors": 0, "retention": retention}
    injected = sum(1 for row in rows if row.get("injected"))
    return {"count": len(rows), "last_at": rows[-1].get("at", ""),
            "last_agent": rows[-1].get("agent", ""),
            "injected": injected,
            "empty": len(rows) - injected,
            "errors": sum(1 for row in rows if row.get("error")),
            "retention": retention}
