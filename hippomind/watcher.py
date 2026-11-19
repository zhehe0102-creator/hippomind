"""Low-frequency incremental collector for Agent-Chat-Archive sources.

No third-party filesystem watcher is required.  Each source uses an explicit
cursor and is checked at a deliberately low cadence; a broken source records
its own error and never stops the daemon or unrelated sources.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .ledger import LedgerError, SQLiteLedger, _hash, utc_now
from .raw_extract import extract_structured_records
from .sanitize import MAX_ARCHIVE_MESSAGE_CHARS, sanitize_chat_content

# 注入包防回灌：任何宿主把 HippoMind 注入内容以 user 角色写进归档时，
# 该行绝不能作为新证据回流账本，否则会形成「注入→归档→再注入」自放大回路。
INJECTION_MARKER = "<MEMORY_DATA_UNTRUSTED>"


DEFAULT_ARCHIVES = (
    Path.home() / "Library" / "Application Support" / "Agent-Chat-Archive",
    Path("/Volumes/移动硬盘/Agent-Chat-Archive"),
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ERROR_BACKOFF_BASE_SECONDS = 30.0
ERROR_BACKOFF_MAX_SECONDS = 3_600.0


def default_dsh_sessions_root() -> Optional[Path]:
    """Production default: ``~/.dsh/sessions`` when present (env overridable).

    Only daemon/CLI entry points call this; the watcher itself stays hermetic
    so isolated constructions never read the user's real sessions.
    """
    from .dsh_sessions import dsh_sessions_root
    env = os.environ.get("DSH_SESSIONS_ROOT", "").strip()
    candidate = Path(env).expanduser() if env else dsh_sessions_root()
    try:
        return candidate if candidate.is_dir() else None
    except OSError:
        return None


@dataclass
class WatchSummary:
    imported: int = 0
    duplicate: int = 0
    sources: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def add(self, source: str, result: Mapping[str, Any]) -> None:
        data = dict(result)
        self.sources[source] = data
        self.imported += int(data.get("imported", 0))
        self.duplicate += int(data.get("duplicate", 0))

    def to_dict(self) -> Dict[str, Any]:
        return {"imported": self.imported, "duplicate": self.duplicate, "sources": self.sources}


class ArchiveWatcher:
    """Incrementally import known Agent-Chat-Archive data without copying it blindly."""

    def __init__(self, ledger: SQLiteLedger, archive_root: Optional[os.PathLike] = None,
                 poll_interval: float = 30.0, batch_size: int = 250,
                 dsh_sessions_root: Optional[os.PathLike] = None,
                 webhook_url: str = ""):
        self.ledger = ledger
        # 可选 webhook：同步发现新 Agent 或出错时 POST 摘要（失败仅记日志，
        # 绝不中断采集——通知是增益不是依赖）。首轮静默建基线，之后才武装。
        self.webhook_url = str(webhook_url or os.environ.get("HIPPO_WEBHOOK_URL", ""))
        self._webhook_armed = False
        # 多根交错：每轮把源列表按周期数轮转一位，避免固定顺序下
        # 排在前面的归档根长期优先消费轮询预算。
        self._sync_round = 0
        # 最近一次「真正导入新数据」的单调时钟；空转轮询不更新。
        # daemon 的空闲睡眠巩固以此判断归档侧是否安静。
        # None 表示从未导入过——不能用 0.0 哨兵：monotonic 在新进程里近 0，
        # 与真实时钟值混用会让 max()/差值判定失效。
        self.last_import_monotonic: Optional[float] = None
        # 已见过的归档 source_agent 集合；None 表示尚未初始化（首轮静默建基线，
        # 之后新出现的 Agent 会在同步摘要中报告——多 Agent 感知）。
        self._known_agents: Optional[set] = None
        self.archive_roots = self._resolve_archives(archive_root)
        # Preserve the original public attribute for callers that display one
        # primary archive, while synchronising every available root below.
        self.archive_root = self.archive_roots[0] if self.archive_roots else None
        self.poll_interval = max(5.0, min(3600.0, float(poll_interval)))
        self.batch_size = max(10, min(2_000, int(batch_size)))
        self.dsh_sessions_root = self._resolve_dsh_sessions(dsh_sessions_root)
        self._stop = threading.Event()
        self._maintenance_cycles = 0
        self._last_summary: Dict[str, Any] = {"imported": 0, "duplicate": 0, "sources": {}}

    @staticmethod
    def _resolve_dsh_sessions(explicit: Optional[os.PathLike]) -> Optional[Path]:
        """Resolve the DSH sessions tree for this watcher.

        The watcher never touches the real home implicitly: the tree comes
        from an explicit argument or the ``DSH_SESSIONS_ROOT`` override, and
        production entry points (daemon, CLI) attach the default
        ``~/.dsh/sessions`` themselves. Absent trees disable the source.
        """
        from .dsh_sessions import dsh_sessions_root
        if explicit is None:
            env = os.environ.get("DSH_SESSIONS_ROOT", "").strip()
            if not env:
                return None
            candidate = Path(env).expanduser()
        else:
            candidate = Path(explicit).expanduser()
        try:
            return candidate if candidate.is_dir() else None
        except OSError:
            return None

    @staticmethod
    def _resolve_archives(explicit: Optional[os.PathLike]) -> List[Path]:
        candidates: List[Path] = []
        if explicit:
            if isinstance(explicit, (str, os.PathLike)):
                candidates.append(Path(explicit).expanduser())
            else:
                try:
                    candidates.extend(Path(value).expanduser() for value in explicit)  # type: ignore[union-attr]
                except TypeError:
                    candidates.append(Path(explicit).expanduser())
        else:
            for name in ("HIPPOMIND_ARCHIVE", "AGENT_ARCHIVE"):
                if os.environ.get(name):
                    candidates.append(Path(os.environ[name]).expanduser())
            candidates.extend(DEFAULT_ARCHIVES)
        resolved: List[Path] = []
        seen = set()
        for candidate in candidates:
            if candidate.name == "index.sqlite":
                candidate = candidate.parent.parent
            if candidate.exists() and candidate.is_dir():
                try:
                    candidate = candidate.resolve()
                except OSError:
                    pass
                key = str(candidate)
                if key not in seen:
                    resolved.append(candidate)
                    seen.add(key)
        return resolved

    @classmethod
    def _resolve_archive(cls, explicit: Optional[os.PathLike]) -> Optional[Path]:
        """Compatibility helper for older callers that expect one root."""
        roots = cls._resolve_archives(explicit)
        return roots[0] if roots else None

    @property
    def available(self) -> bool:
        return bool(self.archive_roots)

    @property
    def status(self) -> Dict[str, Any]:
        return {"archive": str(self.archive_root) if self.archive_root else "",
                "archives": [str(path) for path in self.archive_roots],
                "dsh_sessions": str(self.dsh_sessions_root) if self.dsh_sessions_root else "",
                "available": self.available, "poll_interval": self.poll_interval,
                "last_sync": self._last_summary}

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _source_id(prefix: str, path: Path) -> str:
        return prefix + ":" + _hash(str(path.resolve()))[:24]

    def _record_error(self, source_id: str, error: Exception) -> Dict[str, Any]:
        previous = self.ledger.get_cursor(source_id)
        metadata = previous.get("metadata", {}) if isinstance(previous.get("metadata", {}), Mapping) else {}
        message = str(error)[:2_000]
        # Keep retries attached to the source and stable error category rather
        # than volatile details such as memory addresses, PIDs, or a rebuilt
        # source pathname. Otherwise a single persistent failure can restart
        # at the base delay on every poll.
        signature = _hash(type(error).__name__, getattr(error, "errno", ""))[:24]
        streak = int(metadata.get("error_streak", 0) or 0) + 1
        if metadata.get("error_signature") != signature:
            streak = 1
        delay = min(ERROR_BACKOFF_MAX_SECONDS,
                    ERROR_BACKOFF_BASE_SECONDS * (2 ** min(streak - 1, 7)))
        updated = dict(metadata)
        updated.update({"error_signature": signature, "error_streak": streak,
                        "retry_not_before": time.time() + delay,
                        "retry_after_seconds": delay})
        self.ledger.set_cursor(source_id, previous.get("cursor", ""), error=message, metadata=updated)
        return {"imported": 0, "duplicate": 0, "error": message,
                "retry_after_seconds": delay, "error_streak": streak}

    def _in_error_backoff(self, source_id: str) -> Optional[Dict[str, Any]]:
        cursor = self.ledger.get_cursor(source_id)
        metadata = cursor.get("metadata", {}) if isinstance(cursor.get("metadata", {}), Mapping) else {}
        try:
            retry_at = float(metadata.get("retry_not_before", 0.0) or 0.0)
        except (TypeError, ValueError):
            retry_at = 0.0
        if cursor.get("last_error") and retry_at > time.time():
            return {"error": str(cursor["last_error"]), "retry_after_seconds": max(0.0, retry_at - time.time()),
                    "error_streak": int(metadata.get("error_streak", 1) or 1)}
        return None

    def _clear_error(self, source_id: str) -> None:
        cursor = self.ledger.get_cursor(source_id)
        metadata = cursor.get("metadata", {}) if isinstance(cursor.get("metadata", {}), Mapping) else {}
        if not cursor.get("last_error") and not metadata.get("error_signature"):
            return
        cleaned = {key: value for key, value in metadata.items()
                   if key not in {"error_signature", "error_streak", "retry_not_before", "retry_after_seconds"}}
        self.ledger.set_cursor(source_id, cursor.get("cursor", ""), metadata=cleaned)

    @staticmethod
    def _path_identity(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_dev}:{stat.st_ino}"

    def _ingest_records(self, records: List[Mapping[str, Any]]) -> Dict[str, Any]:
        """Keep a malformed source row from blocking the rest of its batch."""
        if not records:
            return {"imported": 0, "duplicate": 0, "rejected": 0, "outcomes": []}
        try:
            results = self.ledger.ingest_many(records)
            return {"imported": sum(result.inserted for result in results),
                    "duplicate": sum(not result.inserted for result in results), "rejected": 0,
                    "outcomes": [{"ok": True, "inserted": result.inserted,
                                  "memory_id": result.memory_id} for result in results]}
        except (LedgerError, sqlite3.Error) as batch_error:
            imported = duplicate = rejected = 0
            errors: List[str] = []
            outcomes: List[Dict[str, Any]] = []
            for record in records:
                try:
                    result = self.ledger.ingest(record)
                    imported += int(result.inserted)
                    duplicate += int(not result.inserted)
                    outcomes.append({"ok": True, "inserted": result.inserted, "memory_id": result.memory_id})
                except (LedgerError, sqlite3.Error) as exc:
                    rejected += 1
                    # LedgerError 是确定性校验失败（同样输入重试必然同样失败）；
                    # sqlite3.Error 视为瞬时故障，调用方可保留 cursor 重试。
                    outcomes.append({"ok": False, "error": str(exc)[:500],
                                     "deterministic": isinstance(exc, LedgerError)})
                    if len(errors) < 3:
                        errors.append(str(exc))
            return {"imported": imported, "duplicate": duplicate, "rejected": rejected,
                    "batch_error": str(batch_error), "errors": errors, "outcomes": outcomes}

    @staticmethod
    def _has_transient_failure(result: Mapping[str, Any]) -> bool:
        """Return true only when a rejected row should be retried.

        Malformed JSON and deterministic ledger validation failures are safe to
        advance past. SQLite/OS failures are different: advancing their file
        cursor would permanently lose a valid source row.
        """
        return any(
            isinstance(outcome, Mapping)
            and not outcome.get("ok")
            and not outcome.get("deterministic")
            for outcome in result.get("outcomes", [])
        )

    def _derive_raw(self, records: Sequence[Mapping[str, Any]], outcomes: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
        """Create low-confidence, evidence-linked hints after raw evidence lands."""
        derived: List[Mapping[str, Any]] = []
        for record, outcome in zip(records, outcomes):
            if not outcome.get("ok") or not outcome.get("memory_id"):
                continue
            source = str(record.get("source_agent", "unknown"))
            session = str(record.get("session_id", ""))
            task_id = str(record.get("task_id", ""))
            if not task_id and session:
                task_id = "chat-" + _hash(source, session)[:24]
            derived.extend(extract_structured_records(record, str(outcome["memory_id"]), derived_task_id=task_id))
        if not derived:
            return {"derived": 0, "derived_rejected": 0}
        result = self._ingest_records(list(derived))
        return {"derived": int(result["imported"]), "derived_rejected": int(result["rejected"])}

    def _sync_messages(self, db_path: Path) -> Dict[str, Any]:
        source_id = self._source_id("archive-messages", db_path)
        cursor = self.ledger.get_cursor(source_id)
        try:
            last_rowid = max(0, int(cursor.get("cursor") or 0))
        except (TypeError, ValueError):
            last_rowid = 0
        identity = self._path_identity(db_path)
        metadata = cursor.get("metadata", {}) if isinstance(cursor.get("metadata", {}), Mapping) else {}
        imported = duplicate = 0
        rebuilt = bool(metadata.get("identity") and metadata.get("identity") != identity)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            required = {"agent", "session_id", "role", "content"}
            if not required.issubset(columns):
                raise LedgerError("messages 表缺少必要列")
            max_rowid = int(conn.execute("SELECT COALESCE(MAX(rowid),0) FROM messages").fetchone()[0])
            fingerprint_row = conn.execute(
                "SELECT event_id,content FROM messages WHERE rowid=?", (max_rowid,)
            ).fetchone() if max_rowid else None
            fingerprint = _hash(max_rowid,
                                 fingerprint_row[0] if fingerprint_row else "",
                                 str(fingerprint_row[1] if fingerprint_row else "")[:512])
            if last_rowid > max_rowid:
                rebuilt = True
            previous_max_rowid = int(metadata.get("max_rowid", 0) or 0)
            if (metadata.get("fingerprint") and metadata.get("fingerprint") != fingerprint
                    and last_rowid and previous_max_rowid >= max_rowid):
                rebuilt = True
            if rebuilt:
                last_rowid = 0
            selected = [name for name in ("agent", "session_id", "event_id", "ts", "role", "content",
                                          "model", "cwd", "title", "source_file") if name in columns]
            rows = conn.execute(
                "SELECT rowid AS _rowid," + ",".join(selected) +
                " FROM messages WHERE rowid>? ORDER BY rowid ASC LIMIT ?", (last_rowid, self.batch_size)
            ).fetchall()
        finally:
            conn.close()
        latest = last_rowid
        records = []
        rowids: List[int] = []
        skipped_role_rowids: set[int] = set()
        skipped_empty = 0
        skipped_roles = 0
        skipped_injection = 0
        redacted_messages = 0
        redactions = 0
        truncated_messages = 0
        for row in rows:
            rowid = int(row["_rowid"])
            role = str(row["role"] or "").strip().lower()
            content = row["content"] or ""
            if not str(content).strip():
                # 归档工具会写入零长度 system 事件：没有可提炼信息，重试永远不会
                # 成功。跳过并推进 cursor，否则后续所有消息会被永久阻塞。
                skipped_empty += 1
                continue
            if role not in {"user", "assistant"}:
                # Tool/system/developer transcripts commonly contain prompts,
                # environment dumps and credentials.  They are evidence for
                # the host archive, never durable shared memory.
                skipped_role_rowids.add(rowid)
                skipped_roles += 1
                continue
            if INJECTION_MARKER in str(content):
                # 与 dsh_sessions 的 UNTRUSTED_MARKER 防线同源：注入包即使被
                # 宿主以 user 角色落盘，也只推进游标、永不回灌共享账本。
                skipped_role_rowids.add(rowid)
                skipped_injection += 1
                continue
            content, found_redactions, was_truncated = sanitize_chat_content(
                content, limit=MAX_ARCHIVE_MESSAGE_CHARS)
            redactions += found_redactions
            redacted_messages += bool(found_redactions)
            truncated_messages += bool(was_truncated)
            event_id = str(row["event_id"] or "") if "event_id" in row.keys() else ""
            record = {
                "kind": "tool_output" if role == "tool" else "raw_chat",
                "raw_chat": True,
                "content": content,
                "source_agent": row["agent"] or "unknown",
                "session_id": row["session_id"] or "",
                "event_id": event_id or f"rowid:{rowid}",
                "timestamp": row["ts"] if "ts" in row.keys() else "",
                "role": role,
                "scope": "shared",
                "evidence": ["archive:" + (event_id or str(rowid))],
                "metadata": {
                    "archive": str(db_path), "archive_rowid": rowid,
                    "model": row["model"] if "model" in row.keys() else "",
                    "cwd": row["cwd"] if "cwd" in row.keys() else "",
                    "title": row["title"] if "title" in row.keys() else "",
                    "source_file": row["source_file"] if "source_file" in row.keys() else "",
                },
            }
            records.append(record)
            rowids.append(rowid)
        written = self._ingest_records(records)
        imported += int(written["imported"])
        duplicate += int(written["duplicate"])
        derived = self._derive_raw(records, written.get("outcomes", []))
        # 新 Agent 感知：归档中出现未见过的 source_agent 时报告一次。
        new_agents: List[str] = []
        agents_in_batch = {str(r.get("source_agent") or "unknown") for r in records}
        if self._known_agents is None:
            self._known_agents = set(agents_in_batch)
        else:
            new_agents = sorted(agents_in_batch - self._known_agents)
            self._known_agents |= agents_in_batch
        outcome_by_rowid = dict(zip(rowids, written.get("outcomes", [])))
        empty_rowids = {int(row["_rowid"]) for row in rows
                        if not str(row["content"] or "").strip()}
        blocked_rowid = 0
        rejected_rowids: List[int] = []
        for row in rows:
            rowid = int(row["_rowid"])
            if rowid in empty_rowids or rowid in skipped_role_rowids:
                latest = rowid
                continue
            outcome = outcome_by_rowid.get(rowid)
            if outcome is None:
                break
            if outcome.get("ok"):
                latest = rowid
            elif outcome.get("deterministic"):
                # 确定性校验失败重试必然同样失败：登记后跳过，防止整条源永久阻塞。
                # 需要补救时可通过数据库重建（identity 变化触发 cursor 归零重放）。
                rejected_rowids.append(rowid)
                latest = rowid
            else:
                blocked_rowid = rowid
                break
        cursor_metadata = {"path": str(db_path), "kind": "messages", "identity": identity,
                           "max_rowid": max_rowid, "rebuilt": rebuilt,
                           "fingerprint": fingerprint,
                           "skipped_empty": skipped_empty, "skipped_roles": skipped_roles,
                           "skipped_injection": skipped_injection,
                           "redacted_messages": redacted_messages, "redactions": redactions,
                           "truncated_messages": truncated_messages,
                           "rejected_rowids": rejected_rowids}
        if blocked_rowid:
            self.ledger.set_cursor(source_id, str(latest),
                                   error=f"rowid {blocked_rowid} 瞬时写入失败，保留 cursor 以便重试",
                                   metadata={**cursor_metadata, "blocked_rowid": blocked_rowid})
        elif rows or rebuilt:
            self.ledger.set_cursor(source_id, str(latest), metadata=cursor_metadata)
        return {"imported": imported, "duplicate": duplicate, "cursor": str(latest), "rows": len(rows),
                "rejected": written["rejected"], "skipped_empty": skipped_empty,
                "rejected_rowids": rejected_rowids[:20], "blocked_rowid": blocked_rowid,
                "skipped_roles": skipped_roles, "redacted_messages": redacted_messages,
                "skipped_injection": skipped_injection,
                "new_agents": new_agents,
                "redactions": redactions, "truncated_messages": truncated_messages,
                "rebuilt": rebuilt, **derived}

    @staticmethod
    def _file_cursor(path: Path) -> str:
        stat = path.stat()
        # Include a bounded content fingerprint: editors can rewrite a JSONL
        # source in place while preserving inode, size and even mtime.
        with path.open("rb") as handle:
            head = handle.read(2048)
            try:
                handle.seek(max(0, stat.st_size - 2048))
            except OSError:
                pass
            tail = handle.read(2048)
        return f"{stat.st_mtime_ns}:{stat.st_size}:{_hash(head, tail)[:24]}"

    def _sync_task_jsonl(self, path: Path) -> Dict[str, Any]:
        source_id = self._source_id("archive-tasks", path)
        current = self._file_cursor(path)
        previous = self.ledger.get_cursor(source_id)
        identity = self._path_identity(path)
        metadata = previous.get("metadata", {}) if isinstance(previous.get("metadata", {}), Mapping) else {}
        if previous.get("cursor") == current and metadata.get("identity") == identity and not previous.get("last_error"):
            return {"imported": 0, "duplicate": 0, "cursor": current, "unchanged": True}
        imported = duplicate = 0
        records = []
        rejected = 0
        errors: List[str] = []
        # 字节偏移增量读取：tasks.jsonl 是 append-only 流，任何一次追加都会
        # 改变指纹；全量 readlines 重解析的成本随文件线性增长。仅在 identity
        # 未变且文件没有收缩时从上次偏移继续，只处理完整行（半行留到下轮）。
        try:
            previous_offset = max(0, int(metadata.get("byte_offset", 0) or 0))
        except (TypeError, ValueError):
            previous_offset = 0
        file_size = path.stat().st_size
        incremental = (metadata.get("identity") == identity
                       and 0 < previous_offset <= file_size)
        if incremental:
            # 原位重写防护：偏移必须落在完整行边界上，否则退回全量重读。
            try:
                with path.open("rb") as probe:
                    probe.seek(previous_offset - 1)
                    incremental = probe.read(1) == b"\n"
            except OSError:
                incremental = False
        start_offset = previous_offset if incremental else 0
        lines: List[str] = []
        # 二进制读取：consumed_offset 必须是真实字节偏移（文本模式 tell()
        # 返回的是不透明 cookie，回写游标后下次 seek 语义无保证）。
        consumed_offset = start_offset
        with path.open("rb") as handle:
            if start_offset:
                handle.seek(start_offset)
            while True:
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    # 写入中的残行：不消费，游标停在完整行边界。
                    break
                consumed_offset = handle.tell()
                lines.append(raw.decode("utf-8", errors="replace"))
        for number, line in enumerate(lines, 1):
            try:
                task = json.loads(line)
            except (TypeError, ValueError) as exc:
                rejected += 1
                if len(errors) < 3:
                    errors.append(f"line {number}: {exc}")
                continue
            if not isinstance(task, Mapping):
                rejected += 1
                if len(errors) < 3:
                    errors.append(f"line {number}: task 必须是对象")
                continue
            task_id = str(task.get("task_id", "")).strip() or f"line-{number}"
            title = str(task.get("title", "")).strip()
            description = str(task.get("description", "")).strip()
            searchable_content = "\n".join(value for value in (title, description) if value) or task_id
            notes = task.get("notes", []) if isinstance(task.get("notes", []), list) else []
            note_text = [str(note.get("text", "")).strip() for note in notes if isinstance(note, Mapping)]
            progress = [str(task.get("status", "")).strip()] + note_text[-3:]
            records.append({
                "kind": "task", "content": searchable_content,
                "source_agent": task.get("agent", "unknown"), "session_id": task.get("source_session", ""),
                "event_id": task_id + ":" + str(task.get("updated_at", task.get("created_at", ""))),
                "task_id": task_id, "project": task.get("project", ""), "role": "task",
                "timestamp": task.get("updated_at", task.get("created_at", "")), "scope": "shared",
                "evidence": ["task:" + task_id], "tags": task.get("tags", []),
                "context": {"goal": [description or title], "progress": progress},
                "metadata": {"archive": str(path), "task_status": task.get("status", ""),
                             "task_created_at": task.get("created_at", "")},
                "importance": 7, "confidence": 0.8,
            })
        written = self._ingest_records(records)
        imported += int(written["imported"])
        duplicate += int(written["duplicate"])
        rejected += int(written["rejected"])
        # 新 Agent 感知（与 messages 源同一基线语义：首轮静默建集合）。
        new_agents: List[str] = []
        agents_in_batch = {str(r.get("source_agent") or "unknown") for r in records}
        if self._known_agents is None:
            self._known_agents = set(agents_in_batch)
        else:
            new_agents = sorted(agents_in_batch - self._known_agents)
            self._known_agents |= agents_in_batch
        cursor_metadata = {"path": str(path), "kind": "tasks", "identity": identity,
                           "lines": len(lines), "rejected": rejected,
                           "byte_offset": consumed_offset,
                           "incremental": incremental,
                           "rejection_reasons": (errors + list(written.get("errors", [])))[:3]}
        if self._has_transient_failure(written):
            self.ledger.set_cursor(
                source_id, str(previous.get("cursor", "")),
                error="; ".join(list(written.get("errors", []))[:3]) or "任务记录写入暂时失败",
                metadata={**cursor_metadata, "retryable": True},
            )
        else:
            # Bad lines are deterministic input defects. Record their evidence
            # in cursor metadata but advance so one malformed row cannot block
            # all later tasks forever.
            self.ledger.set_cursor(source_id, current, metadata=cursor_metadata)
        return {"imported": imported, "duplicate": duplicate, "cursor": current, "rows": len(lines),
                "rejected": rejected,
                "new_agents": new_agents,
                "rebuilt": bool(metadata.get("identity") and metadata.get("identity") != identity)}

    def _sync_results_jsonl(self, path: Path) -> Dict[str, Any]:
        source_id = self._source_id("archive-results", path)
        current = self._file_cursor(path)
        previous = self.ledger.get_cursor(source_id)
        identity = self._path_identity(path)
        metadata = previous.get("metadata", {}) if isinstance(previous.get("metadata", {}), Mapping) else {}
        if previous.get("cursor") == current and metadata.get("identity") == identity and not previous.get("last_error"):
            return {"imported": 0, "duplicate": 0, "cursor": current, "unchanged": True}
        imported = duplicate = 0
        records = []
        rejected = 0
        errors: List[str] = []
        rows = 0
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                rows = number
                try:
                    entry = json.loads(line)
                except (TypeError, ValueError) as exc:
                    rejected += 1
                    if len(errors) < 3:
                        errors.append(f"line {number}: {exc}")
                    continue
                if not isinstance(entry, Mapping):
                    rejected += 1
                    if len(errors) < 3:
                        errors.append(f"line {number}: result 必须是对象")
                    continue
                content = str(entry.get("content", entry.get("result", entry.get("summary", "")))).strip()
                if not content:
                    rejected += 1
                    if len(errors) < 3:
                        errors.append(f"line {number}: content 不能为空")
                    continue
                external_id = str(entry.get("event_id", entry.get("id", f"line-{number}")))
                records.append({
                    "kind": "result", "content": content, "source_agent": entry.get("agent", "unknown"),
                    "session_id": entry.get("session_id", ""), "task_id": entry.get("task_id", ""),
                    "project": entry.get("project", ""), "role": entry.get("role", "result"),
                    "event_id": external_id, "timestamp": entry.get("timestamp", entry.get("ts", "")),
                    "scope": entry.get("scope", "shared"), "evidence": entry.get("evidence", ["result:" + external_id]),
                    "context": entry.get("context", {"progress": [content]}), "metadata": {"archive": str(path)},
                })
        written = self._ingest_records(records)
        imported += int(written["imported"])
        duplicate += int(written["duplicate"])
        rejected += int(written["rejected"])
        cursor_metadata = {"path": str(path), "kind": "results", "identity": identity,
                           "lines": rows, "rejected": rejected,
                           "rejection_reasons": (errors + list(written.get("errors", [])))[:3]}
        if self._has_transient_failure(written):
            self.ledger.set_cursor(
                source_id, str(previous.get("cursor", "")),
                error="; ".join(list(written.get("errors", []))[:3]) or "结果记录写入暂时失败",
                metadata={**cursor_metadata, "retryable": True},
            )
        else:
            self.ledger.set_cursor(source_id, current, metadata=cursor_metadata)
        return {"imported": imported, "duplicate": duplicate, "cursor": current, "rows": rows,
                "rejected": rejected, "rebuilt": bool(metadata.get("identity") and metadata.get("identity") != identity)}

    def _sync_dsh_session_file(self, path: Path) -> Dict[str, Any]:
        """Convert one multi-frame DSH session file into bounded raw records."""
        from .dsh_sessions import build_turn_records, decode_session_file, parse_session_events
        source_id = self._source_id("dsh-session", path)
        current = self._file_cursor(path)
        previous = self.ledger.get_cursor(source_id)
        identity = self._path_identity(path)
        metadata = previous.get("metadata", {}) if isinstance(previous.get("metadata", {}), Mapping) else {}
        if (previous.get("cursor") == current and metadata.get("identity") == identity
                and not previous.get("last_error")):
            return {"imported": 0, "duplicate": 0, "cursor": current, "unchanged": True}
        text = decode_session_file(path, package_root=PACKAGE_ROOT)
        events = parse_session_events(text)
        records = build_turn_records(events, path)
        written = self._ingest_records(records)
        derived = self._derive_raw(records, written.get("outcomes", []))
        cursor_metadata = {"path": str(path), "kind": "dsh-session", "identity": identity,
                           "events": len(events), "turns": len(records)}
        if self._has_transient_failure(written):
            # 与 tasks/results/messages 一致：瞬时写入失败保留旧游标重试，
            # 否则指纹不变会导致这批轮次在文件再次追加前永久丢失。
            self.ledger.set_cursor(
                source_id, str(previous.get("cursor", "")),
                error="; ".join(list(written.get("errors", []))[:3]) or "DSH 会话写入暂时失败",
                metadata={**cursor_metadata, "retryable": True},
            )
        else:
            self.ledger.set_cursor(source_id, current, metadata=cursor_metadata)
        return {"imported": int(written["imported"]), "duplicate": int(written["duplicate"]),
                "cursor": current, "rows": len(events), "turns": len(records),
                "rejected": int(written.get("rejected", 0)), **derived}

    def _sync_dsh_sessions(self) -> Dict[str, Any]:
        """Aggregate every DSH session file; per-file failures stay isolated."""
        from .dsh_sessions import find_session_files
        assert self.dsh_sessions_root is not None
        files = find_session_files(self.dsh_sessions_root)
        removed = self._prune_dsh_cursors({self._source_id("dsh-session", f)
                                           for f in files})
        if not files:
            return {"imported": 0, "duplicate": 0, "files": 0,
                    "cursors_pruned": removed, "error": "DSH 会话目录为空"}
        imported = duplicate = failed = 0
        errors: List[str] = []
        for path in files:
            source_id = self._source_id("dsh-session", path)
            delayed = self._in_error_backoff(source_id)
            if delayed is not None:
                failed += 1
                if len(errors) < 3:
                    errors.append(f"{path.name}: backoff")
                continue
            try:
                result = self._sync_dsh_session_file(path)
                imported += int(result.get("imported", 0))
                duplicate += int(result.get("duplicate", 0))
                self._clear_error(source_id)
            except (OSError, ValueError, LedgerError, sqlite3.Error, subprocess.SubprocessError) as exc:
                failed += 1
                self._record_error(source_id, exc)
                if len(errors) < 3:
                    errors.append(f"{path.name}: {str(exc)[:200]}")
        return {"imported": imported, "duplicate": duplicate, "files": len(files),
                "failed": failed, "errors": errors, "cursors_pruned": removed}

    def _prune_dsh_cursors(self, existing_ids: set) -> int:
        """游标清理：会话文件被删除后，其 source_cursors 行会永久残留。

        每轮同步顺带检查一次（行数有限，开销可忽略）；路径已不存在的
        dsh-session 游标被删除并计数。
        """
        removed = 0
        try:
            for row in self.ledger.list_cursors(prefix="dsh-session:"):
                if row["source_id"] in existing_ids:
                    continue
                path = Path(str(row["metadata"].get("path", "")))
                if path and not path.exists():
                    if self.ledger.delete_cursor(row["source_id"]):
                        removed += 1
        except (OSError, LedgerError):
            pass
        return removed

    def _notify_webhook(self, summary: Dict[str, Any]) -> None:
        """把值得关注的同步事件推给用户配置的 URL（尽力而为）。"""
        if not self.webhook_url or not self._webhook_armed:
            return
        sources = summary.get("sources", {})
        source_new_agents = [a for v in sources.values() if isinstance(v, dict)
                             for a in (v.get("new_agents") or [])]
        interesting = (summary.get("new_agents") or source_new_agents
                       or any(isinstance(v, dict) and v.get("error")
                              for v in sources.values()))
        if not interesting:
            return
        try:
            import urllib.request as _rq
            payload = json.dumps({
                "kind": "hippomind.watch",
                "at": utc_now(),
                "new_agents": sorted(set(source_new_agents) |
                                     set(summary.get("new_agents") or [])),
                "sources": {k: {"imported": v.get("imported"), "error": v.get("error")}
                            for k, v in summary.get("sources", {}).items()
                            if isinstance(v, dict)},
            }, ensure_ascii=False).encode()
            req = _rq.Request(self.webhook_url, data=payload,
                              headers={"Content-Type": "application/json"})
            _rq.urlopen(req, timeout=3.0).close()
        except Exception:
            pass

    def sync_once(self) -> Dict[str, Any]:
        summary = WatchSummary()
        if not self.archive_roots and self.dsh_sessions_root is None:
            summary.add("archive", {"imported": 0, "duplicate": 0, "error": "未找到 Agent-Chat-Archive"})
            self._last_summary = summary.to_dict()
            return self._last_summary
        sources: List[Tuple[str, Path, Any]] = []
        multiple_roots = len(self.archive_roots) > 1
        for root in self.archive_roots:
            suffix = "@" + _hash(str(root))[:8] if multiple_roots else ""
            chat_db = root / "index" / "index.sqlite"
            if chat_db.exists():
                sources.append(("messages" + suffix, chat_db, self._sync_messages))
            tasks_file = root / "tasks" / "tasks.jsonl"
            if tasks_file.exists():
                sources.append(("tasks" + suffix, tasks_file, self._sync_task_jsonl))
            for result_file in (root / "results.jsonl", root / "results" / "results.jsonl",
                                root / "tasks" / "results.jsonl"):
                if result_file.exists():
                    sources.append(("results" + suffix + ":" + _hash(str(result_file))[:8],
                                    result_file, self._sync_results_jsonl))
        if not sources and self.dsh_sessions_root is None:
            summary.add("archive", {"imported": 0, "duplicate": 0, "error": "归档中尚无可读 source"})
        if sources and len(sources) > 1:
            shift = self._sync_round % len(sources)
            sources = sources[shift:] + sources[:shift]
        self._sync_round += 1
        for name, path, handler in sources:
            failure_source_id = self._source_id("failed-" + name, path)
            delayed = self._in_error_backoff(failure_source_id)
            if delayed is not None:
                summary.add(name, {"imported": 0, "duplicate": 0, "error": delayed["error"],
                                   "skipped_backoff": True,
                                   "retry_after_seconds": delayed["retry_after_seconds"],
                                   "error_streak": delayed["error_streak"]})
                continue
            try:
                summary.add(name, handler(path))
                self._clear_error(failure_source_id)
            except Exception as exc:  # individual sources must not take down the hub
                summary.add(name, self._record_error(failure_source_id, exc))
        if self.dsh_sessions_root is not None:
            try:
                summary.add("dsh-sessions", self._sync_dsh_sessions())
            except Exception as exc:  # the DSH source must not take down the hub
                summary.add("dsh-sessions", {"imported": 0, "duplicate": 0,
                                             "error": str(exc)[:300]})
        self._last_summary = summary.to_dict()
        try:
            sources = self._last_summary.get("sources", {})
            total_new = sum(int(v.get("imported", 0)) for v in sources.values()
                            if isinstance(v, dict))
            if total_new > 0:
                self.last_import_monotonic = time.monotonic()
        except (AttributeError, TypeError):
            pass
        self._notify_webhook(self._last_summary)
        self._webhook_armed = True
        return self._last_summary

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                summary = self.sync_once()
            except Exception as exc:  # the polling thread must self-heal
                summary = {"imported": 0, "duplicate": 0,
                           "error": f"watcher cycle failed: {type(exc).__name__}: {str(exc)[:240]}",
                           "sources": {}}
            try:
                self._maintenance_cycles += 1
                # Full compressed evidence archival reads cold raw rows and
                # touches the filesystem. Run it roughly every ten minutes at
                # the default cadence; ordinary decay remains cheap per tick.
                archive_now = self._maintenance_cycles % 20 == 0
                summary["maintenance"] = self.ledger.maintain(
                    archive_raw=archive_now,
                )
                if archive_now:
                    # 批次边界的 TRUNCATE 检查点：PASSIVE 自动检查点会被重叠
                    # 读无限饿死，WAL 只增不减；空闲批次末尾截断是长跑 daemon
                    # 唯一能把 -wal 文件真正归零的手段（busy 时下轮重试）。
                    summary["wal"] = self.ledger.maintenance_checkpoint()
                if self._maintenance_cycles % 20 == 0:
                    summary["consolidated"] = len(self.ledger.consolidate())
            except Exception as exc:  # 维护失败同样不允许杀死轮询线程
                summary["maintenance_error"] = (f"{type(exc).__name__}: {str(exc)[:240]}")
            self._stop.wait(self.poll_interval)
