"""Gemini / Antigravity conversation summary capture.

Antigravity stores one row per conversation in a small SQLite summary DB.  It
is not a raw transcript, but it is exactly the useful part for cross-agent
memory: title/preview, project, agent, timestamps and step count.  Reading it
incrementally closes the gap for Gemini without depending on MCP tool calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .ledger import LedgerError, SQLiteLedger, utc_now

CURSOR_PREFIX = "transcript:gemini:"


def _iso_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return utc_now()
    text = text.replace("Z", "+00:00").replace(" ", "T", 1)
    # Gemini stores variable-length fractional seconds (e.g. .09725); the
    # ledger requires ISO-8601, so normalise the fraction to microseconds.
    text = re.sub(r"\.(\d+)", lambda match: "." + (match.group(1) + "000000")[:6], text, count=1)
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text[:64]


def default_gemini_summary_databases() -> List[Path]:
    home = Path.home()
    return [
        home / ".gemini" / "antigravity" / "conversation_summaries.db",
        home / ".gemini" / "antigravity-cli" / "conversation_summaries.db",
    ]


class GeminiSummaryScanner:
    def __init__(self, ledger: SQLiteLedger, databases: Optional[Iterable[os.PathLike]] = None):
        self.ledger = ledger
        self.databases = [Path(path).expanduser() for path in
                          (databases if databases is not None else default_gemini_summary_databases())]
        self._last_summary: Dict[str, Any] = {}

    @staticmethod
    def _cursor_id(path: Path) -> str:
        return CURSOR_PREFIX + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _empty_cursor() -> Dict[str, str]:
        return {"last_modified_time": "", "conversation_id": ""}

    def _read_cursor(self, path: Path) -> Dict[str, str]:
        """Current compound keyset cursor, tolerant of the legacy shape.

        The old cursor was a bare last_modified_time string.  It is still
        understood: it becomes a keyset with an empty conversation id, so rows
        sharing that exact timestamp are re-read once (and deduped by the
        ledger) instead of being skipped forever.
        """
        row = self.ledger.get_cursor(self._cursor_id(path))
        raw = str(row.get("cursor") or "").strip()
        if not raw:
            return self._empty_cursor()
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = raw
        if isinstance(value, Mapping):
            return {"last_modified_time": str(value.get("last_modified_time") or ""),
                    "conversation_id": str(value.get("conversation_id") or "")}
        return {"last_modified_time": str(value or ""), "conversation_id": ""}

    def _write_cursor(self, path: Path, value: Mapping[str, str], error: str = "") -> None:
        payload = {"last_modified_time": str(value.get("last_modified_time") or ""),
                   "conversation_id": str(value.get("conversation_id") or "")}
        self.ledger.set_cursor(self._cursor_id(path),
                               json.dumps(payload, separators=(",", ":"))[:4_000],
                               error=error,
                               metadata={"kind": "gemini-summary", "path": str(path)})

    def _records(self, path: Path, after: Mapping[str, str],
                 limit: int) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """Fetch the next keyset page plus the keyset of the last fetched row.

        Ordering is by the compound key (last_modified_time, conversation_id).
        A single-column cursor silently lost rows written in the same
        timestamp: several conversations summarised in one second produced only
        one captured row, because 'last_modified_time > cursor' skipped every
        sibling of the cursor row.
        """
        empty = self._empty_cursor()
        if not path.is_file() or path.is_symlink():
            return [], empty
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return [], empty
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_summaries)")}
            if not columns or "conversation_id" not in columns:
                return [], empty
            wanted = [name for name in ("conversation_id", "title", "preview",
                                        "step_count", "last_modified_time", "workspace_uris",
                                        "status", "project_id", "agent_name") if name in columns]
            query = "SELECT " + ",".join(wanted) + " FROM conversation_summaries"
            params: List[Any] = []
            if "last_modified_time" in columns:
                # Compound keyset: strictly greater timestamp, or the same
                # timestamp with a greater conversation id.  Without the second
                # arm, every same-timestamp sibling after max_rows is skipped.
                query += (" WHERE last_modified_time > ?"
                          " OR (last_modified_time = ? AND conversation_id > ?)")
                params.extend([str(after.get("last_modified_time") or ""),
                               str(after.get("last_modified_time") or ""),
                               str(after.get("conversation_id") or "")])
                query += " ORDER BY last_modified_time ASC, conversation_id ASC"
            query += " LIMIT ?"
            params.append(max(1, min(500, int(limit))))
            rows = conn.execute(query, tuple(params)).fetchall()
        except sqlite3.Error:
            return [], empty
        finally:
            conn.close()
        records: List[Dict[str, Any]] = []
        last = empty
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            conversation_id = str(item.get("conversation_id") or "").strip()
            # Advance the keyset over every fetched row, including ones skipped
            # below for having no content: otherwise a blank row stalls the
            # cursor and the page is re-read on every pass.
            last = {"last_modified_time": str(item.get("last_modified_time") or "")[:64],
                    "conversation_id": conversation_id[:256]}
            title = " ".join(str(item.get("title") or "").split()).strip()
            preview = " ".join(str(item.get("preview") or "").split()).strip()
            content = "\n".join(part for part in (title, preview) if part)[:4_000]
            if not conversation_id or not content:
                continue
            source_modified = str(item.get("last_modified_time") or "")[:64]
            timestamp = _iso_timestamp(source_modified)
            external_id = "gemini:" + conversation_id
            metadata = {
                "source": "gemini-antigravity-summary",
                "conversation_id": conversation_id,
                "agent_name": str(item.get("agent_name") or ""),
                "project_id": str(item.get("project_id") or ""),
                "status": str(item.get("status") or ""),
                "step_count": item.get("step_count"),
                "workspace_uris": str(item.get("workspace_uris") or ""),
                "source_modified_time": source_modified,
                "external_id": external_id,
            }
            records.append({
                "content": content,
                "kind": "task",
                "source_agent": "gemini",
                "scope": "shared",
                "event_id": external_id,
                "task_id": conversation_id,
                "project": str(item.get("project_id") or "")[:200],
                "evidence": ["gemini:%s" % conversation_id],
                "timestamp": timestamp,
                "metadata": metadata,
            })
        return records, last

    def sync(self, *, max_rows: int = 100) -> Dict[str, Any]:
        summary = {"databases": 0, "rows": 0, "inserted": 0, "duplicate": 0,
                   "errors": 0, "last_modified_time": "", "last_conversation_id": ""}
        remaining = max(0, int(max_rows))
        for path in self.databases:
            if remaining <= 0:
                break
            if not path.is_file():
                continue
            summary["databases"] += 1
            after = self._read_cursor(path)
            try:
                records, last = self._records(path, after, remaining)
            except Exception as exc:  # one corrupt DB must not stop the loop
                summary["errors"] += 1
                self._write_cursor(path, after, error=str(exc)[:500])
                continue
            if records:
                try:
                    results = self.ledger.ingest_many(records)
                    summary["rows"] += len(records)
                    summary["inserted"] += sum(1 for result in results if result.inserted)
                    summary["duplicate"] += sum(1 for result in results if not result.inserted)
                except LedgerError as exc:
                    summary["errors"] += 1
                    self._write_cursor(path, after, error=str(exc)[:500])
                    continue
                remaining -= len(records)
            if last.get("last_modified_time") or last.get("conversation_id"):
                summary["last_modified_time"] = last["last_modified_time"]
                summary["last_conversation_id"] = last["conversation_id"]
                self._write_cursor(path, last)
        self._last_summary = summary
        return summary
