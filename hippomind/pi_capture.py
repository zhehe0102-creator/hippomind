"""Pi coding-agent native session capture.

Pi (the `pi` CLI / PiDeck) appends one JSONL file per session under
`~/.pi/agent/sessions/<encoded-cwd>/<ISO timestamp>_<uuid>.jsonl`.  Every line
is one immutable record: a `session` header, `message` entries
(user/assistant/toolResult/system), `compaction` summaries, `session_info`
renames, `deleted` tombstones and harness-specific `custom` events.

This adapter mirrors :mod:`hippomind.transcripts`: it reads complete lines
starting at a byte offset stored in `source_cursors`, keeps only user/assistant
*text* blocks plus compaction summaries, and never interprets tool output,
thinking blocks, system prompts or injected harness context.  A daemon restart,
a partially written line or a deleted message therefore cannot duplicate rows
or lose the session identity that later records depend on.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .ledger import LedgerError, SQLiteLedger, utc_now

CURSOR_PREFIX = "transcript:pi:"
MAX_MESSAGE_CHARS = 4_000
# Compaction summaries are the closest thing Pi has to a distilled conversation
# record; they are much longer than a chat line, so keep a larger (but still
# bounded) slice of them.
MAX_SUMMARY_CHARS = 8_000
MAX_BYTES_PER_FILE = 2_000_000
# Pi records can be large (tool results with big payloads); a single JSONL line
# must be read whole before it can be parsed or skipped.
MAX_LINE_BYTES = 8_000_000
MAX_SKIP_BYTES = 64_000_000
MAX_MESSAGES_PER_CYCLE = 200
MAX_PER_FILE = 40
INGEST_BATCH_SIZE = 1_000  # matches SQLiteLedger.ingest_many's hard limit
SKIP_ROLES = {"system", "toolresult", "tool", "developer"}
CAPTURE_ROLES = {"user", "assistant"}
DISTILL_PROMPT_MARKER = "HippoMind 的记忆提炼器"


@dataclass(frozen=True)
class PiEntry:
    """One capturable Pi record (a chat message or a compaction summary)."""

    kind: str  # "raw_chat" or "task"
    role: str
    content: str
    timestamp: str
    session_id: str
    cwd: str
    session_name: str
    parent_session: str
    path: Path
    line_number: int
    entry_id: str


def _iso_timestamp(value: Any) -> str:
    """Normalise a Pi timestamp to an ISO-8601 UTC string (or "" when absent)."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("Z", "+00:00").replace(" ", "T", 1)
    # Python 3.9/3.10 only accept 3 or 6 fractional digits; Pi writes variable
    # precision, so normalise the fraction before parsing.
    text = re.sub(r"\.(\d+)", lambda match: "." + (match.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _json_loads(raw: bytes) -> Optional[Mapping[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _text_content(value: Any) -> str:
    """Extract only text blocks; thinking/toolCall/image blocks stay untouched."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        parts: List[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    return ""


def default_pi_session_roots() -> List[Path]:
    configured = os.environ.get("HIPPOMIND_PI_SESSION_ROOTS", "").strip()
    if configured:
        return [Path(part).expanduser() for part in configured.split(os.pathsep) if part.strip()]
    return [Path.home() / ".pi" / "agent" / "sessions"]


def _new_context() -> Dict[str, str]:
    return {"session_id": "", "cwd": "", "session_name": "", "parent_session": ""}


class PiSessionScanner:
    """Incremental, bounded scanner for Pi native session JSONL logs."""

    def __init__(self, ledger: SQLiteLedger,
                 roots: Optional[Iterable[os.PathLike]] = None):
        self.ledger = ledger
        self.roots = [Path(root).expanduser() for root in
                      (roots if roots is not None else default_pi_session_roots())
                      if str(root).strip()]
        self._last_summary: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Source discovery / cursor plumbing
    # ------------------------------------------------------------------
    def files(self) -> List[Path]:
        """All Pi session files, newest first so live sessions are captured first."""
        found: List[Path] = []
        for root in self.roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for path in root.rglob("*.jsonl"):
                try:
                    if path.is_file() and not path.is_symlink():
                        found.append(path)
                except OSError:
                    continue
        return sorted(found, key=self._sort_key)

    @staticmethod
    def _sort_key(path: Path) -> Tuple[float, str]:
        try:
            return (-path.stat().st_mtime, str(path))
        except OSError:
            return (0.0, str(path))

    @staticmethod
    def _cursor_id(path: Path) -> str:
        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
        return CURSOR_PREFIX + digest

    def _read_cursor(self, cursor_id: str) -> Dict[str, Any]:
        row = self.ledger.get_cursor(cursor_id)
        try:
            value = json.loads(row.get("cursor") or "{}")
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _write_cursor(self, cursor_id: str, value: Mapping[str, Any], error: str = "") -> None:
        self.ledger.set_cursor(cursor_id, json.dumps(value, separators=(",", ":")),
                               error=error, metadata={"kind": "pi-session"})

    def _head_context(self, path: Path) -> Dict[str, str]:
        """Read the session header without walking the whole file."""
        context = _new_context()
        try:
            with path.open("rb") as handle:
                head = handle.readline(64 * 1024)
        except OSError:
            return context
        payload = _json_loads(head)
        if payload is not None and payload.get("type") == "session":
            context["session_id"] = str(payload.get("id") or "")
            context["cwd"] = str(payload.get("cwd") or "")
            context["parent_session"] = str(payload.get("parentSession") or "")
        return context

    # ------------------------------------------------------------------
    # Bounded line reading
    # ------------------------------------------------------------------
    @staticmethod
    def _skip_to_newline(handle) -> Tuple[int, bool]:
        """Discard bytes until the next newline. Returns (bytes, hit_cap)."""
        consumed = 0
        while consumed < MAX_SKIP_BYTES:
            chunk = handle.readline(MAX_LINE_BYTES)
            if not chunk:
                return consumed, False
            consumed += len(chunk)
            if chunk.endswith(b"\n"):
                return consumed, False
        return consumed, True

    def _read_lines(self, path: Path, offset: int, *, max_lines: int,
                    max_bytes: int, resync: bool) -> Tuple[List[bytes], int, Dict[str, int]]:
        """Read complete lines only; never block on a partially written tail."""
        stats = {"oversized": 0, "resync": 0, "incomplete": 0}
        try:
            handle = path.open("rb")
        except OSError:
            return [], offset, stats
        consumed = 0
        lines: List[bytes] = []
        with handle:
            handle.seek(offset)
            if resync:
                skipped, hit_cap = self._skip_to_newline(handle)
                consumed += skipped
                # Keep the resync marker only while the oversized record is
                # still unfinished; clearing it early would let the next cycle
                # start parsing from the middle of a JSON line.
                stats["resync"] = 1 if hit_cap else 0
            while len(lines) < max_lines and consumed < max_bytes:
                raw = handle.readline(MAX_LINE_BYTES)
                if not raw:
                    break
                if raw.endswith(b"\n"):
                    lines.append(raw)
                    consumed += len(raw)
                    continue
                if len(raw) < MAX_LINE_BYTES:
                    # The writer is mid-line: leave the offset untouched so the
                    # next cycle re-reads this record once it is complete.
                    stats["incomplete"] = 1
                    break
                # A record larger than MAX_LINE_BYTES cannot be parsed; skip it
                # without buffering the whole payload.
                rest, hit_cap = self._skip_to_newline(handle)
                consumed += len(raw) + rest
                stats["oversized"] += 1
                if hit_cap:
                    stats["resync"] = 1
                    break
        return lines, offset + consumed, stats

    # ------------------------------------------------------------------
    # Record parsing
    # ------------------------------------------------------------------
    def _parse(self, lines: List[bytes], context: Dict[str, str], start_line: int,
               max_entries: int, path: Path) -> Tuple[List[PiEntry], Dict[str, str]]:
        payloads = [_json_loads(raw) for raw in lines]
        deleted = set()
        for payload in payloads:
            if payload is not None and payload.get("type") == "deleted":
                original = str(payload.get("originalEntryId") or "")
                if original:
                    deleted.add(original)
        entries: List[PiEntry] = []
        line_number = start_line
        for payload in payloads:
            line_number += 1
            if payload is None:
                continue
            record_type = payload.get("type")
            if record_type == "session":
                context["session_id"] = str(payload.get("id") or context["session_id"])
                context["cwd"] = str(payload.get("cwd") or context["cwd"])
                context["parent_session"] = str(payload.get("parentSession") or "")
                continue
            if record_type == "session_info":
                context["session_name"] = str(payload.get("name") or context["session_name"])[:200]
                continue
            if record_type == "compaction":
                if len(entries) >= max_entries:
                    break
                summary = " ".join(str(payload.get("summary") or "").split())
                if len(summary) < 2 or DISTILL_PROMPT_MARKER in summary:
                    continue
                entries.append(PiEntry(
                    kind="task",
                    role="",
                    content=summary[:MAX_SUMMARY_CHARS],
                    timestamp=_iso_timestamp(payload.get("timestamp")),
                    session_id=context["session_id"],
                    cwd=context["cwd"],
                    session_name=context["session_name"],
                    parent_session=context["parent_session"],
                    path=path,
                    line_number=line_number,
                    entry_id=str(payload.get("id") or ""),
                ))
                continue
            if record_type != "message":
                continue
            message = payload.get("message")
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role not in CAPTURE_ROLES or role in SKIP_ROLES:
                continue
            entry_id = str(payload.get("id") or "")
            if entry_id and entry_id in deleted:
                # Pi's resend/truncate flow tombstones messages; never turn a
                # message the user removed back into memory.
                continue
            content = _text_content(message.get("content"))
            if len(content) < 2 or DISTILL_PROMPT_MARKER in content:
                continue
            if len(entries) >= max_entries:
                break
            entries.append(PiEntry(
                kind="raw_chat",
                role=role,
                content=content[:MAX_MESSAGE_CHARS],
                timestamp=_iso_timestamp(payload.get("timestamp")),
                session_id=context["session_id"],
                cwd=context["cwd"],
                session_name=context["session_name"],
                parent_session=context["parent_session"],
                path=path,
                line_number=line_number,
                entry_id=entry_id,
            ))
        return entries, context

    # ------------------------------------------------------------------
    # Ledger records
    # ------------------------------------------------------------------
    @staticmethod
    def _project(cwd: str) -> str:
        if not cwd:
            return ""
        return Path(cwd).name[:200]

    def _record(self, entry: PiEntry) -> Dict[str, Any]:
        project = self._project(entry.cwd)
        stem = entry.path.stem
        if entry.kind == "task":
            return {
                "content": entry.content,
                "kind": "task",
                "source_agent": "pi",
                "session_id": entry.session_id,
                "task_id": entry.session_id,
                "project": project,
                "scope": "shared",
                "evidence": ["pi:compaction:%s:%s" % (entry.session_id or stem,
                                                      entry.entry_id or entry.line_number)],
                "timestamp": entry.timestamp or utc_now(),
                "metadata": {
                    "source": "pi-compaction",
                    "transcript_path": str(entry.path),
                    "line": entry.line_number,
                    "cwd": entry.cwd,
                    "session_name": entry.session_name,
                    "parent_session": entry.parent_session,
                    "compaction_id": entry.entry_id,
                    "format": "pi-session",
                },
            }
        return {
            "content": entry.content,
            "kind": "raw_chat",
            "source_agent": "pi",
            "session_id": entry.session_id,
            "project": project,
            "role": entry.role,
            "scope": "agent:pi",
            "raw_chat": True,
            "evidence": ["pi:%s:%s" % (stem, entry.line_number)],
            "timestamp": entry.timestamp or utc_now(),
            "metadata": {
                "source": "native-transcript",
                "transcript_path": str(entry.path),
                "line": entry.line_number,
                "cwd": entry.cwd,
                "session_name": entry.session_name,
                "parent_session": entry.parent_session,
                "entry_id": entry.entry_id,
                "format": "pi-session",
            },
        }

    # ------------------------------------------------------------------
    # Public sync API
    # ------------------------------------------------------------------
    def sync(self, *, max_messages: int = MAX_MESSAGES_PER_CYCLE,
             max_per_file: int = MAX_PER_FILE) -> Dict[str, Any]:
        started = time.time()
        summary = {"files": 0, "messages": 0, "summaries": 0, "inserted": 0,
                   "duplicate": 0, "errors": 0, "bytes": 0, "oversized": 0,
                   "resync": 0, "elapsed_ms": 0.0}
        remaining = max(0, int(max_messages))
        per_file = max(1, int(max_per_file))
        for path in self.files():
            if remaining <= 0:
                break
            summary["files"] += 1
            cursor_id = self._cursor_id(path)
            cursor = self._read_cursor(cursor_id)
            try:
                offset = max(0, int(cursor.get("offset") or 0))
                line_number = max(0, int(cursor.get("line") or 0))
            except (TypeError, ValueError):
                offset, line_number = 0, 0
            context = _new_context()
            for key in context:
                context[key] = str(cursor.get(key) or "")
            if offset > 0 and not context["session_id"]:
                context = self._head_context(path)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < offset:
                # The file was replaced/truncated; start over rather than
                # trusting a stale byte offset.
                offset, line_number = 0, 0
                context = _new_context()
            try:
                lines, next_offset, read_stats = self._read_lines(
                    path, offset, max_lines=min(per_file, remaining),
                    max_bytes=MAX_BYTES_PER_FILE,
                    resync=bool(cursor.get("resync")))
            except Exception as exc:  # one unreadable file must not stop the loop
                summary["errors"] += 1
                self._write_cursor(cursor_id, cursor, error=str(exc)[:500])
                continue
            entries, context = self._parse(lines, context, line_number,
                                           min(per_file, remaining), path)
            if entries:
                records = [self._record(entry) for entry in entries]
                try:
                    # The ledger rejects batches above 1000 records; a large
                    # historical file may yield more than that in one cycle.
                    for start in range(0, len(records), INGEST_BATCH_SIZE):
                        results = self.ledger.ingest_many(records[start:start + INGEST_BATCH_SIZE])
                        summary["inserted"] += sum(1 for result in results if result.inserted)
                        summary["duplicate"] += sum(1 for result in results if not result.inserted)
                except LedgerError as exc:
                    summary["errors"] += 1
                    self._write_cursor(cursor_id, cursor, error=str(exc)[:500])
                    continue
                summary["messages"] += sum(1 for entry in entries if entry.kind == "raw_chat")
                summary["summaries"] += sum(1 for entry in entries if entry.kind == "task")
            summary["bytes"] += max(0, next_offset - offset)
            summary["oversized"] += read_stats["oversized"]
            summary["resync"] += read_stats["resync"]
            remaining -= len(entries)
            next_cursor = {
                "offset": next_offset,
                "line": line_number + len(lines) + read_stats["oversized"],
                "size": path.stat().st_size if path.exists() else 0,
                "updated_at": utc_now(),
            }
            next_cursor.update(context)
            if read_stats["resync"]:
                next_cursor["resync"] = 1
            self._write_cursor(cursor_id, next_cursor)
        summary["elapsed_ms"] = round((time.time() - started) * 1000.0, 1)
        self._last_summary = summary
        return summary

    def status(self) -> Dict[str, Any]:
        rows = []
        for path in self.files():
            cursor = self._read_cursor(self._cursor_id(path))
            rows.append({"path": str(path),
                         "offset": cursor.get("offset", 0),
                         "line": cursor.get("line", 0),
                         "session_id": cursor.get("session_id", ""),
                         "updated_at": cursor.get("updated_at", "")})
        return {"roots": [str(root) for root in self.roots],
                "files": len(rows), "sources": rows[:50],
                "last_sync": self._last_summary}


__all__ = ["CURSOR_PREFIX", "PiEntry", "PiSessionScanner", "default_pi_session_roots"]
