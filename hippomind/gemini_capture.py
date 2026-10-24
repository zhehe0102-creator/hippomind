"""Gemini / Antigravity conversation summary capture.

Antigravity stores one row per conversation in a small SQLite summary DB.  It
is not a raw transcript, but it is exactly the useful part for cross-agent
memory: title/preview, project, agent, timestamps and step count.  Reading it
incrementally closes the gap for Gemini without depending on MCP tool calls.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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

    def _read_cursor(self, path: Path) -> str:
        row = self.ledger.get_cursor(self._cursor_id(path))
        return str(row.get("cursor") or "")

    def _write_cursor(self, path: Path, value: str, error: str = "") -> None:
        self.ledger.set_cursor(self._cursor_id(path), value[:4000], error=error,
                               metadata={"kind": "gemini-summary", "path": str(path)})

    def _records(self, path: Path, after: str, limit: int) -> List[Dict[str, Any]]:
        if not path.is_file() or path.is_symlink():
            return []
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            return []
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_summaries)")}
            if not columns:
                return []
            wanted = [name for name in ("conversation_id", "title", "preview",
                                        "step_count", "last_modified_time", "workspace_uris",
                                        "status", "project_id", "agent_name") if name in columns]
            if "conversation_id" not in wanted:
                return []
            query = "SELECT " + ",".join(wanted) + " FROM conversation_summaries"
            params: List[Any] = []
            if "last_modified_time" in columns:
                query += " WHERE last_modified_time > ?"
                params.append(after)
                query += " ORDER BY last_modified_time ASC"
            query += " LIMIT ?"
            params.append(max(1, min(500, int(limit))))
            rows = conn.execute(query, tuple(params)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        records: List[Dict[str, Any]] = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            conversation_id = str(item.get("conversation_id") or "").strip()
            title = " ".join(str(item.get("title") or "").split()).strip()
            preview = " ".join(str(item.get("preview") or "").split()).strip()
            content = "\n".join(part for part in (title, preview) if part)[:4_000]
            if not conversation_id or not content:
                continue
            source_modified = str(item.get("last_modified_time") or "")[:64]
            timestamp = _iso_timestamp(source_modified)
            metadata = {
                "source": "gemini-antigravity-summary",
                "conversation_id": conversation_id,
                "agent_name": str(item.get("agent_name") or ""),
                "project_id": str(item.get("project_id") or ""),
                "status": str(item.get("status") or ""),
                "step_count": item.get("step_count"),
                "workspace_uris": str(item.get("workspace_uris") or ""),
                "source_modified_time": source_modified,
            }
            records.append({
                "content": content,
                "kind": "task",
                "source_agent": "gemini",
                "scope": "shared",
                "task_id": conversation_id,
                "project": str(item.get("project_id") or "")[:200],
                "evidence": ["gemini:%s" % conversation_id],
                "timestamp": timestamp,
                "metadata": metadata,
            })
        return records

    def sync(self, *, max_rows: int = 100) -> Dict[str, Any]:
        summary = {"databases": 0, "rows": 0, "inserted": 0, "duplicate": 0,
                   "errors": 0, "last_modified_time": ""}
        remaining = max(0, int(max_rows))
        for path in self.databases:
            if remaining <= 0:
                break
            if not path.is_file():
                continue
            summary["databases"] += 1
            after = self._read_cursor(path)
            try:
                records = self._records(path, after, remaining)
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
                last = str((records[-1].get("metadata") or {}).get("source_modified_time") or after)
                summary["last_modified_time"] = last
                self._write_cursor(path, last)
                remaining -= len(records)
        self._last_summary = summary
        return summary
