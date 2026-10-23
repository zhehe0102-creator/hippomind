"""Incremental capture of native agent transcripts.

The watcher already understands Agent-Chat-Archive.  Two widely used agents
(Claude Code and Codex CLI) keep their own JSONL transcripts outside that
archive; without this adapter their sessions never reach the shared ledger.
This module reads those files incrementally (byte offsets stored in
source_cursors), extracts only user/assistant text, and ingests it as raw
evidence.  It never interprets tool output, system prompts or developer
messages as memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from .ledger import LedgerError, SQLiteLedger, utc_now

MAX_MESSAGE_CHARS = 4_000
MAX_BYTES_PER_FILE = 2_000_000
MAX_MESSAGES_PER_CYCLE = 200
CURSOR_PREFIX = "transcript:"
SKIP_ROLES = {"system", "developer", "tool"}
DISTILL_PROMPT_MARKER = "HippoMind 的记忆提炼器"


@dataclass(frozen=True)
class TranscriptMessage:
    agent: str
    role: str
    content: str
    timestamp: str
    session_id: str
    cwd: str
    path: Path
    line_number: int
    sidechain: bool = False


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: List[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text") or block.get("input_text") or block.get("output_text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    return ""


def _timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:64]


def iter_claude(path: Path) -> Iterator[TranscriptMessage]:
    """Yield user/assistant text from a Claude Code JSONL file."""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line_number, line in enumerate(handle, 1):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, Mapping):
                continue
            if payload.get("type") not in {"user", "assistant"}:
                continue
            message = payload.get("message")
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or payload.get("type") or "").strip().lower()
            if role in SKIP_ROLES or role not in {"user", "assistant"}:
                continue
            content = _text_content(message.get("content"))
            if not content or len(content) < 2:
                continue
            yield TranscriptMessage(
                agent="claude",
                role=role,
                content=content[:MAX_MESSAGE_CHARS],
                timestamp=_timestamp(payload.get("timestamp")),
                session_id=str(payload.get("sessionId") or payload.get("session_id") or ""),
                cwd=str(payload.get("cwd") or ""),
                path=path,
                line_number=line_number,
                sidechain=bool(payload.get("isSidechain")),
            )


def iter_codex(path: Path) -> Iterator[TranscriptMessage]:
    """Yield user/assistant text from a Codex rollout JSONL file."""
    session_id = ""
    cwd = ""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line_number, line in enumerate(handle, 1):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, Mapping):
                continue
            kind = str(payload.get("type") or "")
            body = payload.get("payload") if isinstance(payload.get("payload"), Mapping) else {}
            if kind == "session_meta":
                session_id = str(body.get("session_id") or body.get("id") or session_id)
                cwd = str(body.get("cwd") or cwd)
                continue
            if kind != "response_item" or body.get("type") != "message":
                continue
            role = str(body.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = _text_content(body.get("content"))
            if not content or len(content) < 2:
                continue
            yield TranscriptMessage(
                agent="codex",
                role=role,
                content=content[:MAX_MESSAGE_CHARS],
                timestamp=_timestamp(payload.get("timestamp")),
                session_id=session_id,
                cwd=cwd,
                path=path,
                line_number=line_number,
            )


READERS = {"claude": iter_claude, "codex": iter_codex}


class TranscriptScanner:
    """Incremental, bounded scanner for native agent transcripts."""

    def __init__(self, ledger: SQLiteLedger, roots: Optional[Iterable[os.PathLike]] = None):
        self.ledger = ledger
        if roots is None:
            roots = self.default_roots()
        self.roots = [Path(root).expanduser() for root in roots if str(root).strip()]
        self._last_summary: Dict[str, Any] = {}

    @staticmethod
    def default_roots() -> List[Path]:
        configured = os.environ.get("HIPPOMIND_TRANSCRIPT_ROOTS", "").strip()
        if configured:
            return [Path(part) for part in configured.split(os.pathsep) if part.strip()]
        return [Path.home() / ".claude" / "projects",
                Path.home() / ".codex" / "sessions"]

    def files(self) -> List[Tuple[str, Path]]:
        found: List[Tuple[str, Path]] = []
        for root in self.roots:
            if not root.is_dir() or root.is_symlink():
                continue
            agent = "claude" if "claude" in str(root).lower() else "codex"
            for path in root.rglob("*.jsonl"):
                if path.is_file() and not path.is_symlink():
                    found.append((agent, path))
        return sorted(found, key=lambda item: str(item[1]))

    def _cursor_id(self, agent: str, path: Path) -> str:
        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]
        return CURSOR_PREFIX + agent + ":" + digest

    def _read_cursor(self, cursor_id: str) -> Dict[str, Any]:
        row = self.ledger.get_cursor(cursor_id)
        try:
            value = json.loads(row.get("cursor") or "{}")
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _write_cursor(self, cursor_id: str, value: Mapping[str, Any], error: str = "") -> None:
        self.ledger.set_cursor(cursor_id, json.dumps(value, separators=(",", ":")),
                               error=error, metadata={"kind": "transcript"})

    def _messages(self, agent: str, path: Path, offset: int,
                  max_messages: int) -> Tuple[List[TranscriptMessage], int, bool]:
        reader = READERS.get(agent)
        if reader is None:
            return [], offset, True
        try:
            size = path.stat().st_size
        except OSError:
            return [], offset, True
        if size < offset:
            offset = 0
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(MAX_BYTES_PER_FILE)
        except OSError:
            return [], offset, True
        if not chunk:
            return [], offset, True
        consumed = 0
        messages: List[TranscriptMessage] = []
        session_id = ""
        cwd = ""
        line_number = 0
        # Only advance past complete lines; a writer may be mid-line.
        for raw_line in chunk.splitlines(keepends=True):
            if not raw_line.endswith(bytes([10])):
                break
            consumed += len(raw_line)
            line_number += 1
            try:
                line = raw_line.decode("utf-8", errors="replace")
                payload = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            if agent == "codex" and payload.get("type") == "session_meta":
                body = payload.get("payload") if isinstance(payload.get("payload"), Mapping) else {}
                session_id = str(body.get("session_id") or body.get("id") or session_id)
                cwd = str(body.get("cwd") or cwd)
                continue
            # Reuse the per-file reader logic by parsing the already decoded line.
            temp = self._message_from_payload(agent, payload, path, line_number)
            if temp is not None:
                if agent == "codex":
                    temp = replace(temp, session_id=session_id, cwd=cwd)
                messages.append(temp)
                if len(messages) >= max_messages:
                    break
        return messages, offset + consumed, False

    @staticmethod
    def _message_from_payload(agent: str, payload: Mapping[str, Any], path: Path,
                              line_number: int) -> Optional[TranscriptMessage]:
        # Keep this helper in sync with the iterators above.  It is deliberately
        # small and only handles the two documented transcript schemas.
        if agent == "claude":
            if payload.get("type") not in {"user", "assistant"}:
                return None
            message = payload.get("message")
            if not isinstance(message, Mapping):
                return None
            role = str(message.get("role") or "").lower()
            if role not in {"user", "assistant"}:
                return None
            content = _text_content(message.get("content"))
            if len(content) < 2 or DISTILL_PROMPT_MARKER in content:
                return None
            return TranscriptMessage("claude", role, content[:MAX_MESSAGE_CHARS],
                                     _timestamp(payload.get("timestamp")),
                                     str(payload.get("sessionId") or ""),
                                     str(payload.get("cwd") or ""), path, line_number,
                                     bool(payload.get("isSidechain")))
        if agent == "codex":
            if payload.get("type") != "response_item":
                return None
            body = payload.get("payload") if isinstance(payload.get("payload"), Mapping) else {}
            if body.get("type") != "message":
                return None
            role = str(body.get("role") or "").lower()
            if role not in {"user", "assistant"}:
                return None
            content = _text_content(body.get("content"))
            if len(content) < 2 or DISTILL_PROMPT_MARKER in content:
                return None
            return TranscriptMessage("codex", role, content[:MAX_MESSAGE_CHARS],
                                     _timestamp(payload.get("timestamp")), "", "", path, line_number)
        return None

    def _record(self, message: TranscriptMessage) -> Dict[str, Any]:
        project = Path(message.cwd).name if message.cwd else ""
        return {
            "content": message.content,
            "kind": "raw_chat",
            "source_agent": message.agent,
            "session_id": message.session_id,
            "project": project[:200],
            "role": message.role,
            "scope": "agent:" + message.agent,
            "raw_chat": True,
            "evidence": ["transcript:%s:%s" % (message.path, message.line_number)],
            "timestamp": message.timestamp or utc_now(),
            "metadata": {"transcript_path": str(message.path),
                         "line": message.line_number,
                         "cwd": message.cwd,
                         "sidechain": message.sidechain,
                         "source": "native-transcript"},
        }

    def sync(self, *, max_messages: int = MAX_MESSAGES_PER_CYCLE,
             max_per_file: int = 40) -> Dict[str, Any]:
        started = time.time()
        summary = {"files": 0, "messages": 0, "inserted": 0, "duplicate": 0,
                   "errors": 0, "bytes": 0, "elapsed_ms": 0.0}
        remaining = max(0, int(max_messages))
        for agent, path in self.files():
            if remaining <= 0:
                break
            summary["files"] += 1
            cursor_id = self._cursor_id(agent, path)
            cursor = self._read_cursor(cursor_id)
            offset = int(cursor.get("offset", 0) or 0)
            try:
                messages, next_offset, done = self._messages(agent, path, offset,
                                                              min(max_per_file, remaining))
            except Exception as exc:  # one bad transcript must not stop the loop
                summary["errors"] += 1
                self._write_cursor(cursor_id, cursor, error=str(exc)[:500])
                continue
            if messages:
                records = [self._record(message) for message in messages]
                try:
                    results = self.ledger.ingest_many(records)
                    summary["messages"] += len(records)
                    summary["inserted"] += sum(1 for result in results if result.inserted)
                    summary["duplicate"] += sum(1 for result in results if not result.inserted)
                except LedgerError as exc:
                    summary["errors"] += 1
                    self._write_cursor(cursor_id, cursor, error=str(exc)[:500])
                    continue
            summary["bytes"] += max(0, next_offset - offset)
            remaining -= len(messages)
            self._write_cursor(cursor_id, {"offset": next_offset,
                                           "size": path.stat().st_size if path.exists() else 0,
                                           "updated_at": utc_now()})
        summary["elapsed_ms"] = round((time.time() - started) * 1000.0, 1)
        self._last_summary = summary
        return summary

    def status(self) -> Dict[str, Any]:
        files = self.files()
        rows = []
        for agent, path in files:
            cursor = self.ledger.get_cursor(self._cursor_id(agent, path))
            rows.append({"agent": agent, "path": str(path),
                         "offset": cursor.get("cursor", ""),
                         "updated_at": cursor.get("updated_at", ""),
                         "error": cursor.get("last_error", "")})
        return {"roots": [str(root) for root in self.roots],
                "files": len(files), "sources": rows[:50],
                "last_sync": self._last_summary}
