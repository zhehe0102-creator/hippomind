"""Cursor native session capture (evidence-first, condition-ready).

Cursor is a VS Code / Electron derivative, so its local history is not one file
format but three unrelated stacks, each with its own schema:

1. **Composer stack (SQLite, IDE)** -- `<user-data>/globalStorage/state.vscdb`
   holds a `cursorDiskKV(key TEXT, value BLOB)` table whose
   `bubbleId:<composerId>:<bubbleId>` rows carry one JSON chat message each
   (`type` 1 = user, 2 = assistant), plus `composerData:<composerId>` rows
   with session metadata.  `<user-data>/workspaceStorage/<hash>/state.vscdb`
   holds an `ItemTable` row keyed `composer.composerData` listing
   `allComposers` (session ids/metadata only, not message text).
2. **Store stack (agent / CLI)** --
   `~/.cursor/projects/<project>/agent-transcripts/<uuid>/<uuid>.jsonl`: one
   `{"role": ..., "message": {"content": [...]}}` object per line.
   `~/.cursor/chats/<md5(cwd)>/<uuid>/store.db`: a protobuf blob DAG.
3. **Legacy ItemTable chat JSON** -- older builds stored whole conversations
   under `workbench.panel.aichat.view.aichat.chatdata` in `ItemTable`.

Every path, table, key and field name used below is cited with a public source
URL in `docs/AGENT_CAPTURE_COVERAGE.md`; nothing here is inferred from
memory.  Tool output, thinking blocks, `agentKv` blobs, `checkpointId`
snapshots and `codeBlockDiff` records are never interpreted as memory.

**The important part: this adapter refuses to pretend it succeeded.**  Cursor's
schemas are undocumented and change between releases, so `probe()` returns
exactly one of three states:

`detected`       a known source with a recognised schema was found;
`not_installed`  no Cursor data directory exists at all;
`unrecognized`   Cursor data exists but none of the known schemas matched.

`unrecognized` is never reported as "0 messages captured": that would turn an
unknown schema into a silent data-loss signal.  The `store.db` protobuf DAG is
reported as present-but-unsupported for the same reason.

This machine has **no Cursor installation**, so the adapter is verified against
synthetic fixtures only -- see `tests/test_cursor_capture.py` and the
limitations section of `docs/AGENT_CAPTURE_COVERAGE.md`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .ledger import LedgerError, SQLiteLedger, utc_now

CURSOR_PREFIX = "transcript:cursor:"

#: The only three states a Cursor source may report.
STATE_DETECTED = "detected"
STATE_NOT_INSTALLED = "not_installed"
STATE_UNRECOGNIZED = "unrecognized"

BUBBLE_KEY_PREFIX = "bubbleId:"
COMPOSER_DATA_KEY = "composer.composerData"
LEGACY_CHAT_KEYS = (
    "workbench.panel.aichat.view.aichat.chatdata",
    "workbench.panel.aichat.chatdata",
)
#: `bubble.type`: 1 = user, 2 = assistant (community reverse engineering).
BUBBLE_ROLES = {1: "user", 2: "assistant"}

MAX_MESSAGE_CHARS = 4_000
MAX_ROWS_PER_CYCLE = 400
MAX_MESSAGES_PER_CYCLE = 200
MAX_VALUE_BYTES = 2_000_000
MAX_LINE_BYTES = 8_000_000
MAX_BYTES_PER_FILE = 2_000_000
MAX_FILES_PER_CYCLE = 40
#: Assistant bubbles are written empty and filled in place while streaming, so a
#: pure "rowid > high-water" cursor would capture the empty shell and never the
#: answer.  Each cycle therefore re-reads this many trailing rows; the ledger's
#: content-hash de-duplication makes the overlap free.
RECENT_RESCAN_ROWS = 200
INGEST_BATCH_SIZE = 1_000  # matches SQLiteLedger.ingest_many's hard limit
DISTILL_PROMPT_MARKER = "HippoMind 的记忆提炼器"


@dataclass(frozen=True)
class CursorMessage:
    """One capturable Cursor message (a bubble or a transcript line)."""

    role: str
    content: str
    timestamp: str
    session_id: str
    project: str
    evidence: str
    source: str
    path: Path
    metadata: Mapping[str, Any]
    #: Stable source event id for identity v3.  Streaming bubbles are
    #: rewritten in place, so the same id arrives again with longer content;
    #: the ledger supersedes the older revision instead of co-existing
    #: with it.  Empty for sources that never revise a record.
    external_id: str = ""


def _iso_timestamp(value: Any) -> str:
    """Normalise a Cursor timestamp to an ISO-8601 UTC string ("" when absent)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # `timingInfo.clientRpcSendTime` is Unix milliseconds.
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("Z", "+00:00").replace(" ", "T", 1)
    # Python 3.9/3.10 accept only 3 or 6 fractional digits.
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _bubble_timestamp(bubble: Mapping[str, Any]) -> str:
    """`createdAt` (newer Cursor) then `timingInfo.clientRpcSendTime` (older)."""
    stamp = _iso_timestamp(bubble.get("createdAt"))
    if stamp:
        return stamp
    timing = bubble.get("timingInfo")
    if isinstance(timing, Mapping):
        return _iso_timestamp(timing.get("clientRpcSendTime"))
    return ""


def _text_blocks(value: Any) -> str:
    """Extract only `type == "text"` parts; tool_use/thinking stay untouched."""
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


def _decode_value(value: Any) -> str:
    """Decode a SQLite `value` cell (BLOB or TEXT) as UTF-8 text."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return ""


def _json_object(text: str) -> Optional[Mapping[str, Any]]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def default_cursor_data_roots() -> List[Path]:
    """VS Code-style `User` directories, per platform."""
    configured = os.environ.get("HIPPOMIND_CURSOR_ROOTS", "").strip()
    if configured:
        return [Path(part).expanduser() for part in configured.split(os.pathsep) if part.strip()]
    home = Path.home()
    return [
        home / "Library" / "Application Support" / "Cursor" / "User",  # macOS
        home / ".config" / "Cursor" / "User",                          # Linux
        home / "AppData" / "Roaming" / "Cursor" / "User",              # Windows
    ]


def default_cursor_store_roots() -> List[Path]:
    """`~/.cursor` -- the Store stack root (`projects/`, `chats/`)."""
    configured = os.environ.get("HIPPOMIND_CURSOR_STORE_ROOTS", "").strip()
    if configured:
        return [Path(part).expanduser() for part in configured.split(os.pathsep) if part.strip()]
    return [Path.home() / ".cursor"]


def _open_readonly(path: Path) -> Optional[sqlite3.Connection]:
    """Open a SQLite file read-only; never create, migrate or lock for write."""
    if not path.is_file() or path.is_symlink():
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _tables(conn: sqlite3.Connection) -> List[str]:
    """Table names; a corrupt/non-database file raises instead of looking empty."""
    return [str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]


def _has_rowid(conn: sqlite3.Connection, table: str) -> bool:
    """True only when `rowid` is really selectable (not a WITHOUT ROWID table)."""
    try:
        conn.execute("SELECT rowid FROM %s LIMIT 1" % table).fetchone()
        return True
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# Probe -- exactly one of detected / not_installed / unrecognized
# ---------------------------------------------------------------------------
def _probe_global_db(path: Path) -> Dict[str, Any]:
    """Recognise the Composer global store: `cursorDiskKV` + `bubbleId:` keys."""
    source: Dict[str, Any] = {"kind": "composer-global", "path": str(path),
                              "recognized": False, "detail": "", "reason": ""}
    conn = _open_readonly(path)
    if conn is None:
        source["reason"] = "unreadable_or_locked"
        return source
    try:
        tables = _tables(conn)
        if "cursorDiskKV" not in tables:
            source["reason"] = "no_cursorDiskKV_table"
            source["detail"] = "tables=%s" % ",".join(sorted(tables)[:12])
            return source
        if not _has_rowid(conn, "cursorDiskKV"):
            # Bounded incremental reads need a monotonic key; without rowid we
            # cannot promise "read each row once", so we say so instead of
            # guessing an ordering column that Cursor may not have.
            source["reason"] = "no_rowid_on_cursorDiskKV"
            return source
        has_bubbles = conn.execute(
            "SELECT 1 FROM cursorDiskKV "
            "WHERE substr(CAST(key AS BLOB),1,9)=CAST(? AS BLOB) LIMIT 1",
            (BUBBLE_KEY_PREFIX,)).fetchone() is not None
        has_composer_data = conn.execute(
            "SELECT 1 FROM cursorDiskKV "
            "WHERE substr(CAST(key AS BLOB),1,13)=CAST(? AS BLOB) LIMIT 1",
            ("composerData:",)).fetchone() is not None
        source["recognized"] = True
        source["detail"] = "cursorDiskKV"
        source["has_bubbles"] = has_bubbles
        source["has_composer_data"] = has_composer_data
        if not has_bubbles:
            source["reason"] = "schema_ok_but_no_bubble_rows_yet"
        return source
    except sqlite3.Error as exc:
        source["reason"] = "sqlite_error:%s" % str(exc)[:120]
        return source
    finally:
        conn.close()


def _probe_workspace_db(path: Path) -> Dict[str, Any]:
    """Recognise a workspace store: `ItemTable` with a composer/chat key."""
    source: Dict[str, Any] = {"kind": "composer-workspace", "path": str(path),
                              "recognized": False, "detail": "", "reason": ""}
    conn = _open_readonly(path)
    if conn is None:
        source["reason"] = "unreadable_or_locked"
        return source
    try:
        if "ItemTable" not in _tables(conn):
            source["reason"] = "no_ItemTable"
            return source
        wanted = (COMPOSER_DATA_KEY,) + LEGACY_CHAT_KEYS
        keys = [str(row[0]) for row in conn.execute(
            "SELECT key FROM ItemTable WHERE key IN (%s)"
            % ",".join("?" for _ in wanted), wanted)]
        source["recognized"] = bool(keys)
        source["detail"] = ",".join(sorted(keys))
        if not keys:
            source["reason"] = "no_composer_keys"
        return source
    except sqlite3.Error as exc:
        source["reason"] = "sqlite_error:%s" % str(exc)[:120]
        return source
    finally:
        conn.close()


def _probe_transcript(path: Path) -> Dict[str, Any]:
    """Recognise a Store-stack agent transcript: role + message.content per line."""
    source: Dict[str, Any] = {"kind": "store-transcript", "path": str(path),
                              "recognized": False, "detail": "", "reason": ""}
    try:
        with path.open("rb") as handle:
            for _ in range(5):
                raw = handle.readline(MAX_LINE_BYTES)
                if not raw:
                    break
                payload = _json_object(raw.decode("utf-8", errors="replace").strip())
                if payload is None:
                    continue
                role = str(payload.get("role") or "").strip().lower()
                message = payload.get("message")
                if role in {"user", "assistant"} and isinstance(message, Mapping) \
                        and isinstance(message.get("content"), (list, str)):
                    source["recognized"] = True
                    source["detail"] = "agent-transcript-jsonl"
                    return source
        source["reason"] = "no_user_or_assistant_record"
        return source
    except OSError:
        source["reason"] = "unreadable"
        return source


def probe(data_roots: Optional[Iterable[os.PathLike]] = None,
          store_roots: Optional[Iterable[os.PathLike]] = None,
          *, max_workspaces: int = 200, max_transcripts: int = MAX_FILES_PER_CYCLE,
          max_store_dbs: int = 5) -> Dict[str, Any]:
    """Discover Cursor's local stores and report one of the three states.

    The third state exists because Cursor's schema is undocumented and versioned:
    a probe that reported "not installed" for an unknown-but-present layout
    would silently drop a real conversation store, and one that reported
    "detected" would let the collector write zero rows and call it success.
    """
    data = [Path(root).expanduser() for root in
            (data_roots if data_roots is not None else default_cursor_data_roots())
            if str(root).strip()]
    store = [Path(root).expanduser() for root in
             (store_roots if store_roots is not None else default_cursor_store_roots())
             if str(root).strip()]
    report: Dict[str, Any] = {
        "agent": "cursor",
        "state": STATE_NOT_INSTALLED,
        "data_roots": [str(path) for path in data],
        "store_roots": [str(path) for path in store],
        "sources": [],
        "present_but_unrecognized": [],
        "notes": [],
    }
    present = False
    for root in data:
        if not root.is_dir():
            continue
        present = True
        global_db = root / "globalStorage" / "state.vscdb"
        if global_db.is_file():
            report["sources"].append(_probe_global_db(global_db))
        workspace_root = root / "workspaceStorage"
        if workspace_root.is_dir():
            try:
                entries = sorted(workspace_root.iterdir())[:max(1, int(max_workspaces))]
            except OSError:
                entries = []
            for entry in entries:
                db = entry / "state.vscdb"
                if db.is_file():
                    report["sources"].append(_probe_workspace_db(db))
    for root in store:
        if not root.is_dir():
            continue
        # `~/.cursor` alone is NOT evidence of Cursor: HippoMind writes
        # `mcp.json`/`rules/`/`skills/` there for every integration, installed or not.
        # Only Cursor-owned subdirectories count.
        if any((root / name).is_dir() for name in ("projects", "chats", "acp-sessions")):
            present = True
        projects = root / "projects"
        if projects.is_dir():
            found = 0
            try:
                for path in sorted(projects.rglob("*.jsonl")):
                    if found >= max(1, int(max_transcripts)):
                        break
                    if not path.is_file() or path.is_symlink():
                        continue
                    if "agent-transcripts" not in path.parts:
                        continue
                    report["sources"].append(_probe_transcript(path))
                    found += 1
            except OSError:
                pass
        chats = root / "chats"
        if chats.is_dir():
            dbs = 0
            try:
                for _ in chats.rglob("store.db"):
                    dbs += 1
                    if dbs >= max(1, int(max_store_dbs)):
                        break
            except OSError:
                dbs = 0
            report["sources"].append({
                "kind": "store-db", "path": str(chats), "recognized": False,
                "supported": False, "detail": "protobuf_dag",
                "reason": "protobuf_store_db_not_parsed",
                "store_dbs_found": dbs,
            })
    recognized = [item for item in report["sources"] if item.get("recognized")]
    report["recognized"] = len(recognized)
    report["total_sources"] = len(report["sources"])
    if not present:
        report["state"] = STATE_NOT_INSTALLED
        report["notes"].append("未发现 Cursor 数据目录（应用未安装，或安装后从未运行过）")
        return report
    if recognized:
        report["state"] = STATE_DETECTED
        return report
    report["state"] = STATE_UNRECOGNIZED
    report["present_but_unrecognized"] = [
        {"kind": item.get("kind"), "path": item.get("path"), "reason": item.get("reason")}
        for item in report["sources"]]
    report["notes"].append(
        "发现 Cursor 数据，但没有任何已知 schema 匹配。这不是「0 条消息」，"
        "而是「无法识别」——按 docs/AGENT_CAPTURE_COVERAGE.md 重新探测后再采集")
    return report


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------
def _clean_content(text: str) -> str:
    """Collapse whitespace, drop noise, and enforce the per-message bound."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) < 2 or DISTILL_PROMPT_MARKER in collapsed:
        return ""
    return collapsed[:MAX_MESSAGE_CHARS]


def _content_revision(content: str) -> str:
    """Stable per-version marker for a streamed record (audit only).

    The revision is *not* an identity input: identity v3 uses the stable
    event id plus the content itself.  Recording which content version a
    row captured keeps the supersede chain explainable after the fact.
    """
    payload = str(content or "").encode("utf-8", errors="replace")
    return hashlib.sha1(payload).hexdigest()[:16]


def _record(message: CursorMessage) -> Dict[str, Any]:
    metadata = dict(message.metadata)
    record: Dict[str, Any] = {
        "content": message.content,
        "kind": "raw_chat",
        "source_agent": "cursor",
        "session_id": message.session_id,
        "project": message.project[:200],
        "role": message.role,
        "scope": "agent:cursor",
        "raw_chat": True,
        "evidence": [message.evidence],
        "timestamp": message.timestamp or utc_now(),
        "metadata": metadata,
    }
    if message.external_id:
        # identity v3: the source event id participates in the raw identity
        # key and is projected into metadata so the next revision of this
        # same event can find this row and supersede it.
        record["event_id"] = message.external_id
        metadata["external_id"] = message.external_id
        metadata["content_revision"] = _content_revision(message.content)
    return record


def _project_from_transcript_path(path: Path) -> str:
    """`~/.cursor/projects/<sanitized>/agent-transcripts/<uuid>/<uuid>.jsonl`."""
    parts = path.parts
    if "projects" in parts:
        index = parts.index("projects")
        if index + 1 < len(parts):
            return parts[index + 1][:200]
    return ""


def _ingest(ledger: SQLiteLedger, messages: List[CursorMessage]) -> Dict[str, int]:
    """Write one bounded batch; the ledger's content hash makes replay free."""
    counts = {"messages": 0, "inserted": 0, "duplicate": 0}
    records = [_record(message) for message in messages]
    for start in range(0, len(records), INGEST_BATCH_SIZE):
        results = ledger.ingest_many(records[start:start + INGEST_BATCH_SIZE])
        counts["messages"] += len(results)
        counts["inserted"] += sum(1 for item in results if item.inserted)
        counts["duplicate"] += sum(1 for item in results if not item.inserted)
    return counts


# ---------------------------------------------------------------------------
# Composer stack (global state.vscdb / cursorDiskKV)
# ---------------------------------------------------------------------------
class CursorComposerScanner:
    """Incremental reader for the global `cursorDiskKV` bubble table."""

    def __init__(self, ledger: SQLiteLedger, databases: Optional[Iterable[os.PathLike]] = None):
        self.ledger = ledger
        self.databases = [Path(path).expanduser() for path in
                          (databases if databases is not None else self._default_databases())]

    @staticmethod
    def _default_databases() -> List[Path]:
        return [root / "globalStorage" / "state.vscdb" for root in default_cursor_data_roots()]

    def _resolved(self) -> List[Path]:
        return [path for path in self.databases if path.is_file()]

    @staticmethod
    def _cursor_id(path: Path) -> str:
        return CURSOR_PREFIX + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]

    def _read_cursor(self, path: Path) -> Dict[str, Any]:
        row = self.ledger.get_cursor(self._cursor_id(path))
        try:
            value = json.loads(row.get("cursor") or "{}")
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _write_cursor(self, path: Path, value: Mapping[str, Any], error: str = "") -> None:
        self.ledger.set_cursor(self._cursor_id(path),
                               json.dumps(value, separators=(",", ":"))[:4000],
                               error=error, metadata={"kind": "cursor-composer",
                                                      "path": str(path)})

    @staticmethod
    def _composer_metadata(conn: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
        """`composerData:<id>` -> {name, project}: metadata only, never text."""
        found: Dict[str, Dict[str, str]] = {}
        try:
            rows = conn.execute(
                "SELECT key, value FROM cursorDiskKV "
                "WHERE substr(CAST(key AS BLOB),1,13)=CAST(? AS BLOB) LIMIT 500",
                ("composerData:",)).fetchall()
        except sqlite3.Error:
            return found
        for row in rows:
            composer_id = str(row["key"] or "")[len("composerData:"):]
            payload = _json_object(_decode_value(row["value"]))
            if not composer_id or payload is None:
                continue
            project = ""
            for key in ("workspaceIdentifier", "workspaceUri"):
                value = payload.get(key)
                uri = value.get("uri") if isinstance(value, Mapping) else None
                if isinstance(uri, Mapping):
                    project = str(uri.get("fsPath") or uri.get("path") or "")
                elif isinstance(value, str):
                    project = value
                if project:
                    break
            found[composer_id] = {
                "name": " ".join(str(payload.get("name") or "").split())[:200],
                "project": (Path(project).name[:200] if project else ""),
            }
        return found

    @staticmethod
    def _bubble_rows(conn: sqlite3.Connection, after_rowid: int, limit: int
                     ) -> Tuple[List[sqlite3.Row], List[sqlite3.Row], int, int]:
        """Rows above the rowid watermark, plus a bounded trailing overlap.

        Assistant bubbles are written empty and filled in place while the answer
        streams, so a pure "rowid > high-water" cursor would store the empty
        shell and never the text.  The overlap re-reads the last
        `RECENT_RESCAN_ROWS` rows each cycle; the ledger's content-hash
        de-duplication makes the overlap free.
        """
        fresh: List[sqlite3.Row] = []
        overlap: List[sqlite3.Row] = []
        oversized = 0
        high = after_rowid
        for row in conn.execute(
                "SELECT rowid AS rid, key, value FROM cursorDiskKV "
                "WHERE substr(CAST(key AS BLOB),1,9)=CAST(? AS BLOB) AND rowid > ? "
                "ORDER BY rowid ASC LIMIT ?",
                (BUBBLE_KEY_PREFIX, after_rowid, max(1, int(limit)))):
            high = max(high, int(row["rid"]))
            if len(_decode_value(row["value"])) > MAX_VALUE_BYTES:
                oversized += 1
                continue
            fresh.append(row)
        if RECENT_RESCAN_ROWS > 0:
            recent = conn.execute(
                "SELECT rowid AS rid, key, value FROM cursorDiskKV "
                "WHERE substr(CAST(key AS BLOB),1,9)=CAST(? AS BLOB) AND rowid <= ? "
                "ORDER BY rowid DESC LIMIT ?",
                (BUBBLE_KEY_PREFIX, after_rowid, RECENT_RESCAN_ROWS)).fetchall()
            overlap = list(reversed(recent))
        return fresh, overlap, high, oversized

    @staticmethod
    def _messages(rows: Iterable[sqlite3.Row], path: Path,
                  metadata: Mapping[str, Mapping[str, str]]
                  ) -> Tuple[List[Tuple[sqlite3.Row, Optional[CursorMessage]]], Dict[str, int]]:
        stats = {"corrupt": 0, "empty": 0, "skipped": 0}
        parsed: List[Tuple[sqlite3.Row, Optional[CursorMessage]]] = []
        for row in rows:
            pieces = str(row["key"] or "").split(":")
            if len(pieces) < 3:
                stats["corrupt"] += 1
                parsed.append((row, None))
                continue
            composer_id, bubble_id = pieces[1], pieces[2]
            bubble = _json_object(_decode_value(row["value"]))
            if bubble is None:
                # NULL payload or a non-JSON blob: counted, never guessed at.
                stats["corrupt"] += 1
                parsed.append((row, None))
                continue
            bubble_type = bubble.get("type")
            role = BUBBLE_ROLES.get(bubble_type) if isinstance(bubble_type, int) else None
            if role is None:
                stats["skipped"] += 1
                parsed.append((row, None))
                continue
            content = _clean_content(_text_blocks(bubble.get("text")))
            if not content:
                # Empty assistant bubbles are the streaming intermediate state;
                # the overlap window re-reads them once the text lands.
                stats["empty"] += 1
                parsed.append((row, None))
                continue
            session = metadata.get(composer_id) or {}
            parsed.append((row, CursorMessage(
                role=role,
                content=content,
                timestamp=_bubble_timestamp(bubble),
                session_id=composer_id,
                project=str(session.get("project") or "")[:200],
                evidence="cursor:bubble:%s:%s" % (composer_id, bubble_id),
                source="cursor-composer",
                path=path,
                # One bubble = one source event: Cursor writes it empty and
                # fills the text in place while the answer streams, so the
                # stable id is the (composer, bubble) pair.
                external_id="cursor:bubble:%s:%s" % (composer_id, bubble_id),
                metadata={
                    "source": "cursor-composer-bubble",
                    "format": "cursor-state-vscdb",
                    "database": str(path),
                    "composer_id": composer_id,
                    "bubble_id": bubble_id,
                    "bubble_type": bubble_type,
                    "bubble_schema_version": bubble.get("_v"),
                    "rowid": int(row["rid"]),
                    "session_name": str(session.get("name") or "")[:200],
                },
            )))
        return parsed, stats

    def sync(self, *, max_messages: int = MAX_MESSAGES_PER_CYCLE,
             max_rows: int = MAX_ROWS_PER_CYCLE) -> Dict[str, Any]:
        started = time.time()
        summary: Dict[str, Any] = {"state": STATE_NOT_INSTALLED, "databases": 0,
                                   "rows": 0, "messages": 0, "inserted": 0,
                                   "duplicate": 0, "errors": 0, "corrupt": 0,
                                   "empty": 0, "oversized": 0, "rescan": 0,
                                   "elapsed_ms": 0.0}
        databases = self._resolved()
        if not databases:
            return summary
        summary["state"] = STATE_DETECTED
        remaining = max(0, int(max_messages))
        row_budget = max(1, int(max_rows))
        for path in databases:
            if remaining <= 0 or row_budget <= 0:
                break
            summary["databases"] += 1
            cursor = self._read_cursor(path)
            try:
                after = max(0, int(cursor.get("rowid") or 0))
            except (TypeError, ValueError):
                after = 0
            conn = _open_readonly(path)
            if conn is None:
                summary["errors"] += 1
                self._write_cursor(path, cursor, error="unreadable_or_locked")
                continue
            try:
                if "cursorDiskKV" not in _tables(conn) or not _has_rowid(conn, "cursorDiskKV"):
                    # Present but not a schema we can read: say so, never "0 rows".
                    summary["state"] = STATE_UNRECOGNIZED
                    summary["errors"] += 1
                    self._write_cursor(path, cursor, error="unrecognized_schema")
                    continue
                metadata = self._composer_metadata(conn)
                fresh, overlap, high, oversized = self._bubble_rows(conn, after, row_budget)
            except sqlite3.Error as exc:
                summary["errors"] += 1
                self._write_cursor(path, cursor, error=str(exc)[:500])
                continue
            finally:
                conn.close()
            summary["oversized"] += oversized
            summary["rows"] += len(fresh)
            row_budget -= len(fresh)
            try:
                parsed, parse_stats = self._messages(fresh, path, metadata)
                rescanned, rescan_stats = self._messages(overlap, path, metadata)
            except Exception as exc:  # one corrupt DB must not stop the loop
                summary["errors"] += 1
                self._write_cursor(path, cursor, error=str(exc)[:500])
                continue
            for key in ("corrupt", "empty"):
                summary[key] += parse_stats[key] + rescan_stats[key]
            # Overlap rows are re-offered every cycle so a bubble that was
            # empty while streaming is captured once it is filled in place.
            # They are reported as "rescan", never as new messages: the ledger
            # de-duplicates by content hash, so re-offering is free and honest
            # telemetry must not show phantom message counts.
            summary["rescan"] += sum(1 for _, message in rescanned if message is not None)
            # The watermark may only pass rows this cycle actually consumed.
            # Advancing it to the highest row *read* would silently skip every
            # bubble beyond the per-cycle message budget, and the overlap window
            # would then re-read them forever instead of the backlog draining.
            consumed_rowid = after
            try:
                pending: List[CursorMessage] = []
                budget_exhausted = False
                for row, message in parsed:
                    if remaining <= 0:
                        budget_exhausted = True
                        break
                    consumed_rowid = max(consumed_rowid, int(row["rid"]))
                    if message is not None:
                        pending.append(message)
                        remaining -= 1
                if not budget_exhausted and high > consumed_rowid:
                    # Advance watermark past skipped oversized rows in the same window.
                    consumed_rowid = int(high)
                if pending:
                    counts = _ingest(self.ledger, pending)
                    for key in counts:
                        summary[key] += counts[key]
                overlap_messages = [message for _, message in rescanned if message is not None]
                if overlap_messages:
                    overlap_counts = _ingest(self.ledger, overlap_messages)
                    summary["inserted"] += overlap_counts["inserted"]
                    summary["duplicate"] += overlap_counts["duplicate"]
            except LedgerError as exc:
                summary["errors"] += 1
                self._write_cursor(path, cursor, error=str(exc)[:500])
                continue
            self._write_cursor(path, {"rowid": consumed_rowid, "updated_at": utc_now(),
                                      "database": str(path)})
        summary["elapsed_ms"] = round((time.time() - started) * 1000.0, 1)
        return summary


# ---------------------------------------------------------------------------
# Store stack (agent transcripts, JSONL)
# ---------------------------------------------------------------------------
class CursorTranscriptScanner:
    """Incremental reader for `~/.cursor/projects/**/agent-transcripts/*.jsonl`."""

    def __init__(self, ledger: SQLiteLedger, roots: Optional[Iterable[os.PathLike]] = None):
        self.ledger = ledger
        self.roots = [Path(root).expanduser() for root in
                      (roots if roots is not None else default_cursor_store_roots())
                      if str(root).strip()]

    def files(self) -> List[Path]:
        found: List[Path] = []
        for root in self.roots:
            projects = root / "projects"
            if not projects.is_dir() or projects.is_symlink():
                continue
            try:
                for path in projects.rglob("*.jsonl"):
                    if "agent-transcripts" not in path.parts:
                        continue
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
        return CURSOR_PREFIX + "jsonl:" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]

    def _read_cursor(self, path: Path) -> Dict[str, Any]:
        row = self.ledger.get_cursor(self._cursor_id(path))
        try:
            value = json.loads(row.get("cursor") or "{}")
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _write_cursor(self, path: Path, value: Mapping[str, Any], error: str = "") -> None:
        self.ledger.set_cursor(self._cursor_id(path),
                               json.dumps(value, separators=(",", ":"))[:4000],
                               error=error, metadata={"kind": "cursor-transcript",
                                                      "path": str(path)})

    @staticmethod
    def _parse_line(raw: bytes, path: Path, line_number: int,
                    project: str) -> Optional[CursorMessage]:
        payload = _json_object(raw.decode("utf-8", errors="replace").strip())
        if payload is None:
            return None
        role = str(payload.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            return None
        message = payload.get("message")
        if not isinstance(message, Mapping):
            return None
        content = _clean_content(_text_blocks(message.get("content")))
        if not content:
            return None
        session_id = path.parent.name if path.parent != path else path.stem
        return CursorMessage(
            role=role,
            content=content,
            timestamp=_iso_timestamp(payload.get("timestamp") or payload.get("createdAt")),
            session_id=session_id,
            project=project,
            evidence="cursor:transcript:%s:%d" % (path.stem, line_number),
            source="cursor-transcript",
            path=path,
            # JSONL lines are append-only; (session file, line) is a stable
            # id for replay de-duplication and for a rewritten tail.
            external_id="cursor:transcript:%s:%d" % (path.stem, line_number),
            metadata={"source": "cursor-agent-transcript",
                      "format": "cursor-store-transcript",
                      "transcript_path": str(path),
                      "line": line_number,
                      "project_dir": project},
        )

    def sync(self, *, max_messages: int = MAX_MESSAGES_PER_CYCLE,
             max_per_file: int = 40, max_files: int = MAX_FILES_PER_CYCLE) -> Dict[str, Any]:
        started = time.time()
        summary: Dict[str, Any] = {"state": STATE_NOT_INSTALLED, "files": 0,
                                   "messages": 0, "inserted": 0, "duplicate": 0,
                                   "errors": 0, "bytes": 0, "elapsed_ms": 0.0}
        paths = self.files()
        if not paths:
            return summary
        summary["state"] = STATE_DETECTED
        remaining = max(0, int(max_messages))
        per_file = max(1, int(max_per_file))
        for path in paths[:max(1, int(max_files))]:
            if remaining <= 0:
                break
            summary["files"] += 1
            cursor = self._read_cursor(path)
            try:
                offset = max(0, int(cursor.get("offset") or 0))
                line_number = max(0, int(cursor.get("line") or 0))
            except (TypeError, ValueError):
                offset, line_number = 0, 0
            try:
                size = path.stat().st_size
            except OSError:
                summary["errors"] += 1
                continue
            if size < offset:
                offset, line_number = 0, 0
            project = _project_from_transcript_path(path)
            messages: List[CursorMessage] = []
            consumed = 0
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    while len(messages) < min(per_file, remaining) and consumed < MAX_BYTES_PER_FILE:
                        raw = handle.readline(MAX_LINE_BYTES)
                        if not raw:
                            break
                        if not raw.endswith(b"\n"):
                            if len(raw) < MAX_LINE_BYTES:
                                # Half-written tail: leave the offset so the next
                                # cycle re-reads the line once it is complete.
                                break
                            # Oversized single line (> MAX_LINE_BYTES): skip to newline
                            # so this file never wedges forever.
                            skipped = len(raw)
                            found_nl = False
                            while skipped < MAX_LINE_BYTES * 8:
                                chunk = handle.readline(MAX_LINE_BYTES)
                                if not chunk:
                                    break
                                skipped += len(chunk)
                                if chunk.endswith(b"\n"):
                                    found_nl = True
                                    break
                            consumed += skipped
                            if found_nl:
                                line_number += 1
                            continue
                        consumed += len(raw)
                        line_number += 1
                        parsed = self._parse_line(raw, path, line_number, project)
                        if parsed is not None:
                            messages.append(parsed)
            except OSError as exc:
                summary["errors"] += 1
                self._write_cursor(path, cursor, error=str(exc)[:500])
                continue
            if messages:
                try:
                    counts = _ingest(self.ledger, messages)
                except LedgerError as exc:
                    summary["errors"] += 1
                    self._write_cursor(path, cursor, error=str(exc)[:500])
                    continue
                for key in counts:
                    summary[key] += counts[key]
                remaining -= counts["messages"]
            summary["bytes"] += consumed
            self._write_cursor(path, {"offset": offset + consumed, "line": line_number,
                                      "updated_at": utc_now(), "path": str(path)})
        summary["elapsed_ms"] = round((time.time() - started) * 1000.0, 1)
        return summary


# ---------------------------------------------------------------------------
# Public entry point -- the one function the daemon calls
# ---------------------------------------------------------------------------
def collect(ledger: SQLiteLedger, *,
            data_roots: Optional[Iterable[os.PathLike]] = None,
            store_roots: Optional[Iterable[os.PathLike]] = None,
            max_messages: int = MAX_MESSAGES_PER_CYCLE,
            max_rows: int = MAX_ROWS_PER_CYCLE) -> Dict[str, Any]:
    """Probe, then capture.  Always returns the three-state `state` field.

    Safe to call on a schedule: it is read-only against Cursor, bounded per
    cycle, and idempotent (the ledger de-duplicates by content hash, and the
    cursors skip work already done).
    """
    report = probe(data_roots, store_roots)
    summary: Dict[str, Any] = {
        "state": report["state"],
        "recognized": report["recognized"],
        "total_sources": report["total_sources"],
        "present_but_unrecognized": report["present_but_unrecognized"],
        "notes": report["notes"],
        "composer": {}, "transcripts": {},
        "messages": 0, "inserted": 0, "duplicate": 0, "errors": 0,
    }
    if report["state"] == STATE_NOT_INSTALLED:
        # Nothing to read, and nothing to lie about.
        return summary
    composer = CursorComposerScanner(
        ledger, [Path(root).expanduser() / "globalStorage" / "state.vscdb"
                 for root in (data_roots if data_roots is not None
                              else default_cursor_data_roots())])
    transcripts = CursorTranscriptScanner(
        ledger, store_roots if store_roots is not None else default_cursor_store_roots())
    try:
        summary["composer"] = composer.sync(max_messages=max_messages, max_rows=max_rows)
    except Exception as exc:  # capture must never take the hub down
        summary["composer"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    try:
        summary["transcripts"] = transcripts.sync(max_messages=max_messages)
    except Exception as exc:
        summary["transcripts"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    for part in ("composer", "transcripts"):
        block = summary[part]
        if not isinstance(block, Mapping) or "error" in block:
            summary["errors"] += 1
            continue
        for key in ("messages", "inserted", "duplicate", "errors"):
            summary[key] += int(block.get(key) or 0)
    return summary


def status(ledger: Optional[SQLiteLedger] = None, *,
           data_roots: Optional[Iterable[os.PathLike]] = None,
           store_roots: Optional[Iterable[os.PathLike]] = None) -> Dict[str, Any]:
    """Probe-only self-check: what would be captured, without writing anything."""
    report = probe(data_roots, store_roots)
    if ledger is not None:
        cursors = []
        for path in CursorComposerScanner(ledger).databases:
            cursors.append({"kind": "composer", "path": str(path),
                            "cursor": ledger.get_cursor(CursorComposerScanner._cursor_id(path)).get("cursor", "")})
        for path in CursorTranscriptScanner(
                ledger, store_roots if store_roots is not None else default_cursor_store_roots()).files():
            cursors.append({"kind": "transcript", "path": str(path),
                            "cursor": ledger.get_cursor(CursorTranscriptScanner._cursor_id(path)).get("cursor", "")})
        report["cursors"] = cursors[:50]
    return report


__all__ = [
    "CURSOR_PREFIX", "CursorComposerScanner", "CursorMessage", "CursorTranscriptScanner",
    "STATE_DETECTED", "STATE_NOT_INSTALLED", "STATE_UNRECOGNIZED",
    "collect", "default_cursor_data_roots", "default_cursor_store_roots", "probe", "status",
]
