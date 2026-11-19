"""Raw evidence tiering: archive, purge, collapse, redact and scan.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import time
from ..sanitize import MAX_ARCHIVE_MESSAGE_CHARS, sanitize_chat_content
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from ..schema import fts_indexed_predicate
from ._helpers import (
    DEFAULT_RAW_ARCHIVE_MAX_BYTES,
    DEFAULT_RAW_RETENTION_DAYS,
    LedgerError,
    MAX_CONTENT_CHARS,
    RAW_ARCHIVE_DIR_NAME,
    _clamp_int,
    _epoch,
    _hash,
    _json,
    utc_now,
)


def _archive_metadata(encoded: Optional[str]) -> Dict[str, Any]:
    """Decode one row's ``metadata_json`` for an archive payload.

    The archive is the *only* surviving copy once rows are deleted, so the
    provenance blob has to travel with the content: ``archive_rowid`` /
    ``agent_chat_path`` are the pointers back into the external Agent-Chat
    archive, and the 4.27GB pre-compaction backup that also holds them is a
    temporary authority, not a permanent one.

    Decoding is deliberately fail-safe.  ``metadata_json`` is written by
    :func:`._json` for every live row, so malformed values can only be legacy
    corruption; returning ``{}`` there keeps the archive pass from aborting
    (the fail-closed rule protects the *delete*, not the operator's ability to
    retire a row) instead of silently preventing a purge forever.
    """
    try:
        value = json.loads(encoded or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _fts_indexed(conn: sqlite3.Connection, memory_id: str) -> bool:
    """Whether ``memory_id`` satisfies the shared hot-index predicate.

    Reading it back from the database (instead of trusting a column already
    selected by the caller) keeps every FTS writer on the single definition in
    :data:`hippomind.schema.FTS_INDEXED_PREDICATE`.
    """
    return conn.execute(
        "SELECT 1 FROM memories WHERE memory_id=? AND " + fts_indexed_predicate(),
        (memory_id,)).fetchone() is not None


class ArchiveMixin:
    """Raw evidence tiering: archive, purge, collapse, redact and scan."""

    def scan_raw_secrets(self, *, batch_size: int = 250) -> Dict[str, int]:
        """Scan raw evidence without returning or logging its contents.

        The result is intentionally aggregate-only so a maintenance report
        cannot become a new secret exfiltration channel.
        """
        batch_size = _clamp_int(batch_size, 250, 10, 1_000)
        scanned = affected = redactions = truncated = 0
        after = ""
        while True:
            with self._reading() as conn:
                rows = conn.execute(
                    "SELECT memory_id,content FROM memories WHERE raw_chat=1 AND memory_id>? "
                    "ORDER BY memory_id ASC LIMIT ?", (after, batch_size)
                ).fetchall()
            if not rows:
                break
            after = str(rows[-1]["memory_id"])
            for row in rows:
                _cleaned, count, was_truncated = sanitize_chat_content(
                    row["content"], limit=MAX_ARCHIVE_MESSAGE_CHARS)
                scanned += 1
                affected += int(bool(count or was_truncated))
                redactions += int(count)
                truncated += int(was_truncated)
        return {"scanned": scanned, "affected": affected,
                "redactions": redactions, "truncated": truncated}

    def _raw_archive_root(self, archive_dir: Optional[os.PathLike] = None) -> Path:
        root = (Path(archive_dir).expanduser() if archive_dir else
                self.path.parent / (self.path.stem + "." + RAW_ARCHIVE_DIR_NAME))
        if root.exists() and (not root.is_dir() or root.is_symlink()):
            raise LedgerError(f"原始证据归档目录无效: {root}")
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        return root

    @staticmethod
    def _prune_raw_archives(root: Path, max_bytes: int) -> Tuple[int, int]:
        files = []
        total = 0
        for pattern in ("raw-*.jsonl.gz", "purged-*.jsonl.gz", "dedupe-*.jsonl.gz"):
            for path in root.glob(pattern):
                try:
                    st = path.stat()
                    size = st.st_size
                except OSError:
                    continue
                files.append((st.st_mtime_ns, path, size))
                total += size
        removed = 0
        for _mtime, path, size in sorted(files):
            if total <= max_bytes:
                break
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        return removed, max(0, total)

    def purge_expired_raw(self, *, before_days: int = DEFAULT_RAW_RETENTION_DAYS,
                          archive_dir: Optional[os.PathLike] = None,
                          max_bytes: int = DEFAULT_RAW_ARCHIVE_MAX_BYTES,
                          batch_size: int = 2_000,
                          dry_run: bool = False) -> Dict[str, Any]:
        """Physically drop raw evidence past its retention window.

        This is the tier that was missing.  ``maintain`` moved old raw rows to
        ``lifecycle='cold'`` and ``archive_raw_evidence`` wrote a compressed copy,
        but **nothing ever deleted a row**, so a 2-month-old ledger held 1.47M raw
        messages and 4.48GB of file.  A retention policy that never deletes is a
        rota that never rotates.

        Contract (strictly fail-closed):
        1. Select rows whose ``created_at`` is older than ``before_days``.
        2. Write them to a compressed archive file and ``fsync`` it.
        3. Re-read that exact file and confirm every selected ``memory_id`` is
           present.
        4. Only then delete the rows, in bounded batches, in one transaction.
        Any failure in 2 or 3 leaves the database untouched and is reported in
        ``error``; deletion is attempted on the next maintenance pass.

        Each archived record is a full provenance copy, not just a content dump:
        it carries the row's ``metadata`` (decoded ``metadata_json``) alongside
        the existing identity/content/digest fields, because after step 4 the
        archive is the only place ``archive_rowid`` / ``agent_chat_path`` still
        exist.  Keys are additive — no existing key is renamed or dropped.

        Returns counts only — never content — so it is safe to log.
        """
        before_days = _clamp_int(before_days, DEFAULT_RAW_RETENTION_DAYS, 1, 36_500)
        max_bytes = _clamp_int(max_bytes, DEFAULT_RAW_ARCHIVE_MAX_BYTES, 1 << 20,
                               2 * 1024 * 1024 * 1024)
        batch_size = _clamp_int(batch_size, 2_000, 50, 20_000)
        summary: Dict[str, Any] = {
            "purge_candidates": 0, "purged": 0, "events_purged": 0,
            "archive_path": "", "dry_run": bool(dry_run),
            "retention_days": before_days,
        }
        cutoff_epoch = time.time() - before_days * 86_400
        cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).isoformat()

        with self._reading() as conn:
            summary["purge_candidates"] = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE raw_chat=1 AND created_at<=?",
                (cutoff_iso,),
            ).fetchone()[0])
        if dry_run or not summary["purge_candidates"]:
            return summary

        root = self._raw_archive_root(archive_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = root / f"purged-{stamp}-{_hash(str(self.path), str(cutoff_iso))[:12]}.jsonl.gz"

        # ---- 1) stream the rows we are about to delete into the archive ----
        selected: List[str] = []
        selected_events: List[str] = []
        try:
            with gzip.open(archive_path, "wb") as handle:
                after = ""
                while True:
                    with self._reading() as conn:
                        batch = conn.execute("""
                            SELECT m.memory_id,m.origin_event_id,m.content,m.source_agent,
                                   m.session_id,m.task_id,m.project,m.role,m.scope,
                                   m.evidence_json,m.created_at,m.metadata_json,
                                   e.occurred_at,e.content_hash
                            FROM memories AS m
                            JOIN ledger_events AS e ON e.event_id=m.origin_event_id
                            WHERE m.raw_chat=1 AND m.created_at<=? AND m.memory_id>?
                            ORDER BY m.memory_id ASC LIMIT ?
                        """, (cutoff_iso, after, batch_size)).fetchall()
                    if not batch:
                        break
                    after = str(batch[-1]["memory_id"])
                    for row in batch:
                        payload = {
                            "memory_id": row["memory_id"],
                            "event_id": row["origin_event_id"],
                            "content": row["content"],
                            "content_sha256": row["content_hash"],
                            "source_agent": row["source_agent"],
                            "session_id": row["session_id"],
                            "task_id": row["task_id"],
                            "project": row["project"],
                            "role": row["role"],
                            "scope": row["scope"],
                            "evidence": row["evidence_json"],
                            "created_at": row["created_at"],
                            "occurred_at": row["occurred_at"],
                            # Provenance: without this key the archive is the only
                            # copy of the content but carries none of the pointers
                            # (archive_rowid / agent_chat_path) that make the deleted
                            # row auditable against the external chat archive.
                            "metadata": _archive_metadata(row["metadata_json"]),
                            "purged_at": utc_now(),
                        }
                        handle.write((json.dumps(payload, ensure_ascii=False,
                                                 separators=(",", ":")) + "\n").encode("utf-8"))
                        selected.append(str(row["memory_id"]))
                        selected_events.append(str(row["origin_event_id"]))
            try:
                os.chmod(archive_path, 0o600)
            except OSError:
                pass
            # Force the archive to disk before any row is removed.
            with open(archive_path, "rb") as handle:
                os.fsync(handle.fileno())
        except (OSError, sqlite3.Error) as exc:
            summary["error"] = f"archive failed: {type(exc).__name__}: {exc}"[:300]
            summary["purged"] = 0
            try:
                archive_path.unlink()
            except OSError:
                pass
            return summary

        # ---- 2) verify the archive really contains every selected id -------
        try:
            found = set()
            with gzip.open(archive_path, "rb") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    found.add(str(json.loads(line).get("memory_id", "")))
            missing = set(selected) - found
        except (OSError, ValueError) as exc:
            summary["error"] = f"archive verify failed: {type(exc).__name__}: {exc}"[:300]
            return summary
        if missing:
            summary["error"] = f"archive missing {len(missing)} rows; refusing to delete"
            return summary

        # ---- 3) delete only after the archive is proven complete -----------
        def remove(conn: sqlite3.Connection) -> Dict[str, int]:
            removed_events = 0
            for start in range(0, len(selected), batch_size):
                chunk = selected[start:start + batch_size]
                events = selected_events[start:start + batch_size]
                ph = ",".join("?" for _ in chunk)
                eph = ",".join("?" for _ in events)
                # Order matters: child rows first, projection, then the event that
                # pins the projection through memories.origin_event_id.
                conn.execute(f"DELETE FROM memory_fts WHERE memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memory_relations WHERE child_memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memory_relations WHERE parent_memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memory_claims WHERE memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memory_feedback WHERE memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM memories WHERE memory_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM ledger_events WHERE event_id IN ({eph})", events)
                removed_events += len(events)
            return {"events": removed_events}

        try:
            self._transaction(remove)
        except (sqlite3.Error, LedgerError) as exc:
            summary["error"] = f"delete failed after archive: {type(exc).__name__}: {exc}"[:300]
            return summary

        summary["purged"] = len(selected)
        summary["events_purged"] = len(selected_events)
        summary["archive_path"] = str(archive_path)
        removed, remaining = self._prune_raw_archives(root, max_bytes)
        summary["archives_pruned"] = removed
        summary["archive_bytes"] = remaining
        return summary

    def archive_raw_evidence(self, *, before_days: int = DEFAULT_RAW_RETENTION_DAYS,
                             archive_dir: Optional[os.PathLike] = None,
                             max_bytes: int = DEFAULT_RAW_ARCHIVE_MAX_BYTES,
                             batch_size: int = 250) -> Dict[str, int]:
        """Write bounded, redacted raw evidence to compressed JSONL.

        Rows stay in SQLite as the audit projection; the compressed copy is a
        recoverable cold tier and is marked in metadata to make repeated daemon
        maintenance idempotent. Archive files are capped oldest-first.
        """
        before_days = _clamp_int(before_days, DEFAULT_RAW_RETENTION_DAYS, 1, 36500)
        max_bytes = _clamp_int(max_bytes, DEFAULT_RAW_ARCHIVE_MAX_BYTES, 1 << 20,
                               2 * 1024 * 1024 * 1024)
        batch_size = _clamp_int(batch_size, 250, 10, 1_000)
        root = self._raw_archive_root(archive_dir)
        cutoff = time.time() - before_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        now = utc_now()
        archive_path: Optional[Path] = None
        # 会话级已归档集合：同一维护调用内跨批次累积，避免每个批次都重读
        # 整个 gzip 归档文件（文件最大可达数 MB，批次多时是平方级浪费）。
        archived_ids_seen: set = set()
        archive_membership_loaded = False
        written = redactions = truncated = scanned = 0
        after = ""
        while True:
            with self._reading() as conn:
                rows = conn.execute("""
                    SELECT m.memory_id,m.origin_event_id,m.content,m.lifecycle,m.source_agent,m.session_id,
                           m.task_id,m.project,m.role,m.scope,m.evidence_json,m.created_at,
                           m.metadata_json,e.occurred_at
                    FROM memories AS m JOIN ledger_events AS e ON e.event_id=m.origin_event_id
                    WHERE m.raw_chat=1 AND m.memory_id>? AND m.lifecycle IN ('active','cold')
                      AND (m.lifecycle='cold' OR m.created_at<=?)
                      AND m.metadata_json NOT LIKE '%raw_archive_path%'
                    ORDER BY m.memory_id ASC LIMIT ?
                """, (after, cutoff_iso, batch_size)).fetchall()
            if not rows:
                break
            after = str(rows[-1]["memory_id"])
            eligible = []
            for row in rows:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                if isinstance(metadata, Mapping) and metadata.get("raw_archive_path"):
                    continue
                source_epoch = _epoch(row["created_at"] or row["occurred_at"])
                if source_epoch and source_epoch > cutoff and row["lifecycle"] == "active":
                    continue
                cleaned, count, was_truncated = sanitize_chat_content(
                    row["content"], limit=MAX_ARCHIVE_MESSAGE_CHARS)
                scanned += 1
                redactions += int(count)
                truncated += int(was_truncated)
                try:
                    evidence = json.loads(row["evidence_json"] or "[]")
                except (TypeError, ValueError):
                    evidence = []
                eligible.append((row, cleaned, count, was_truncated, evidence))
            if not eligible:
                continue
            if archive_path is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                archive_path = root / f"raw-{stamp}-{_hash(str(self.path), now)[:12]}.jsonl.gz"

            def mark_rows(rows: List[tuple]) -> None:
                def mark(conn: sqlite3.Connection) -> None:
                    for row, _cleaned, count, was_truncated, _evidence in rows:
                        try:
                            metadata = json.loads(row["metadata_json"] or "{}")
                        except (TypeError, ValueError):
                            metadata = {}
                        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
                        metadata.update({"raw_archive_path": str(archive_path), "raw_archived_at": now,
                                         "raw_archive_redactions": int(count),
                                         "raw_archive_truncated": bool(was_truncated)})
                        conn.execute("UPDATE memories SET metadata_json=?,updated_at=? WHERE memory_id=?",
                                     (_json(metadata, "metadata"), now, row["memory_id"]))
                self._transaction(mark)

            # 崩溃窗口幂等：gzip 追加与标记事务是两个介质步骤，中间断电会让
            # 同一批行因未标记而被再次选中。写入前先读回归档文件里已有的
            # memory_id 并跳过（残帧容忍），重复维护不再产生重复归档行。
            if not archive_membership_loaded:
                archive_membership_loaded = True
                if archive_path.exists() and archive_path.stat().st_size > 0:
                    try:
                        with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
                            for line in handle:
                                try:
                                    payload = json.loads(line)
                                except (TypeError, ValueError):
                                    continue
                                if isinstance(payload, Mapping):
                                    archived_ids_seen.add(str(payload.get("memory_id", "")))
                    except (OSError, EOFError):
                        archived_ids_seen = set()
            pending = [item for item in eligible
                       if str(item[0]["memory_id"]) not in archived_ids_seen]
            if not pending:
                # 全部已在崩溃前的批次落盘：只补标记即可。
                mark_rows(eligible)
                continue
            eligible = pending
            with gzip.open(archive_path, "ab") as handle:
                for row, cleaned, count, was_truncated, evidence in eligible:
                    payload = {
                        "memory_id": row["memory_id"], "event_id": row["origin_event_id"],
                        "content": cleaned, "source_agent": row["source_agent"],
                        "session_id": row["session_id"], "task_id": row["task_id"],
                        "project": row["project"], "role": row["role"], "scope": row["scope"],
                        "evidence": evidence if isinstance(evidence, list) else [],
                        "occurred_at": row["occurred_at"], "archived_at": now,
                        "redactions": int(count), "truncated": bool(was_truncated),
                    }
                    handle.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                    written += 1
            archived_ids_seen.update(str(row["memory_id"]) for row, *_ in eligible)
            try:
                os.chmod(archive_path, 0o600)
            except OSError:
                pass
            mark_rows(eligible)
        removed, remaining_bytes = self._prune_raw_archives(root, max_bytes)
        return {"raw_compressed": written, "raw_redactions": redactions,
                "raw_truncated": truncated, "raw_scanned": scanned,
                "raw_archive_bytes": remaining_bytes, "raw_archives_pruned": removed}

    def redact_raw_evidence(self, *, limit: int = MAX_ARCHIVE_MESSAGE_CHARS,
                             batch_size: int = 250) -> Dict[str, int]:
        """Redact and bound already-ingested raw evidence in both projections.

        Raw chat is untrusted audit material.  A security remediation is the
        one deliberate exception to the append-only content rule: it keeps
        event IDs, hashes, timestamps and evidence references stable while
        replacing exposed secrets in both ``ledger_events`` and ``memories``.
        Run this only while the daemon is stopped so every FTS row can be
        updated consistently without fighting an active watcher.
        """
        limit = _clamp_int(limit, MAX_ARCHIVE_MESSAGE_CHARS, 200, MAX_CONTENT_CHARS)
        batch_size = _clamp_int(batch_size, 250, 10, 1_000)
        summary = {"scanned": 0, "changed": 0, "redacted": 0, "truncated": 0,
                   "fts_reindexed": 0}
        after = ""
        now = utc_now()

        def updated_metadata(encoded: str, *, redactions: int, truncated: bool) -> str:
            try:
                value = json.loads(encoded or "{}")
            except (TypeError, ValueError):
                value = {}
            metadata = dict(value) if isinstance(value, Mapping) else {}
            metadata["sanitization"] = {
                "redacted_at": now,
                "redactions": int(redactions),
                "truncated": bool(truncated),
            }
            return _json(metadata, "metadata")

        while True:
            with self._reading() as conn:
                rows = conn.execute("""
                    SELECT m.memory_id,m.origin_event_id,m.content AS memory_content,
                           m.metadata_json AS memory_metadata,m.lifecycle,m.task_id,m.project,
                           m.source_agent,m.kind,e.content AS event_content,
                           e.metadata_json AS event_metadata
                    FROM memories AS m
                    JOIN ledger_events AS e ON e.event_id=m.origin_event_id
                    WHERE m.raw_chat=1 AND m.memory_id>?
                    ORDER BY m.memory_id ASC LIMIT ?
                """, (after, batch_size)).fetchall()
            if not rows:
                break
            after = str(rows[-1]["memory_id"])

            def write(conn: sqlite3.Connection) -> Dict[str, int]:
                result = {"scanned": len(rows), "changed": 0, "redacted": 0,
                          "truncated": 0, "fts_reindexed": 0}
                for row in rows:
                    memory_text, memory_redactions, memory_truncated = sanitize_chat_content(
                        row["memory_content"], limit=limit)
                    event_text, event_redactions, event_truncated = sanitize_chat_content(
                        row["event_content"], limit=limit)
                    redactions = (memory_redactions if row["memory_content"] == row["event_content"]
                                  else memory_redactions + event_redactions)
                    truncated = bool(memory_truncated or event_truncated)
                    changed = (memory_text != row["memory_content"] or event_text != row["event_content"])
                    if not changed:
                        continue
                    memory_metadata = updated_metadata(
                        row["memory_metadata"], redactions=redactions, truncated=truncated)
                    event_metadata = updated_metadata(
                        row["event_metadata"], redactions=redactions, truncated=truncated)
                    conn.execute("UPDATE ledger_events SET content=?,metadata_json=? WHERE event_id=?",
                                 (event_text, event_metadata, row["origin_event_id"]))
                    conn.execute("UPDATE memories SET content=?,metadata_json=?,updated_at=? WHERE memory_id=?",
                                 (memory_text, memory_metadata, now, row["memory_id"]))
                    # FTS is a separate copy of the content. Delete first so
                    # a secret cannot survive in an old full-text segment.
                    conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
                    # Re-index only if the row belongs in the hot index.  This
                    # used to test ``lifecycle == 'active'`` alone, which
                    # re-inserted quarantined/conflicted rows and re-created the
                    # garbage the single predicate exists to keep out
                    # (measured: stale 0 -> 1, drift True after one redact).
                    if _fts_indexed(conn, row["memory_id"]):
                        conn.execute("""
                            INSERT INTO memory_fts(memory_id,content,task_id,project,source_agent,kind)
                            VALUES(?,?,?,?,?,?)
                        """, (row["memory_id"], memory_text, row["task_id"], row["project"],
                              row["source_agent"], row["kind"]))
                    result["changed"] += 1
                    result["redacted"] += redactions
                    result["truncated"] += int(truncated)
                    result["fts_reindexed"] += 1
                return result

            batch = self._transaction(write)
            for key, value in batch.items():
                summary[key] += int(value)
        return summary

    def collapse_duplicate_rows(self, *, archive_dir: Optional[os.PathLike] = None,
                                batch_size: int = 5_000,
                                dry_run: bool = False) -> Dict[str, Any]:
        """Collapse rows whose normalised content is byte-identical.

        The capture storm stored the same message repeatedly; the live ledger held
        1,404,190 redundant raw rows across 39,345 duplicate groups.  Retrieval
        only has an 8-item budget, so duplicates burn it.

        Safety rules:
          * Only *raw evidence* is collapsed.  Structured records keep bitemporal
            versions even when the text repeats.
          * The survivor is the earliest row (stable, reproducible ordering).
          * Every removed row is first written to the same verified gzip archive
            used by :meth:`purge_expired_raw`, so nothing becomes unrecoverable.
            Each record also carries the row's ``metadata``, so provenance
            pointers survive the delete.
          * The grouping table is rebuilt from scratch on every run.  A crash
            cannot leave a stale snapshot that a later run would trust.
          * ``dry_run`` reports the exact counts and touches nothing.

        Returns counts only.
        """
        batch_size = _clamp_int(batch_size, 5_000, 100, 50_000)
        summary: Dict[str, Any] = {"duplicate_groups": 0, "collapsed": 0,
                                   "archive_path": "", "dry_run": bool(dry_run)}

        if not self.read_only:
            self._transaction(lambda conn: conn.execute("DROP TABLE IF EXISTS _dedupe_groups"))

        with self._reading() as conn:
            summary["duplicate_groups"] = int(conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM memories WHERE raw_chat=1 AND content!=''
                    GROUP BY content, source_agent, session_id, role
                    HAVING COUNT(*)>1
                )
            """).fetchone()[0])
        if dry_run or not summary["duplicate_groups"]:
            return summary

        def rebuild_groups(conn: sqlite3.Connection) -> None:
            conn.execute("DROP TABLE IF EXISTS _dedupe_groups")
            conn.execute("""
                CREATE TABLE _dedupe_groups AS
                SELECT substr(
                           MAX(
                               CASE WHEN COALESCE(json_extract(metadata_json, '$.identity_version'), 0) >= 2
                                    THEN '1:' ELSE '0:' END || memory_id
                           ), 3
                       ) AS survivor_id,
                       content, source_agent, session_id, role, COUNT(*) AS n
                FROM memories WHERE raw_chat=1 AND content!=''
                GROUP BY content, source_agent, session_id, role HAVING COUNT(*)>1
            """)

        self._transaction(rebuild_groups)
        try:
            with self._reading() as conn:
                summary["duplicate_groups"] = int(conn.execute(
                    "SELECT COUNT(*) FROM _dedupe_groups").fetchone()[0])
            if not summary["duplicate_groups"]:
                return summary

            root = self._raw_archive_root(archive_dir)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive_path = root / f"dedupe-{stamp}-{_hash(str(self.path))[:12]}.jsonl.gz"
            removed: List[str] = []
            removed_events: List[str] = []
            with gzip.open(archive_path, "wb") as handle:
                after = ""
                while True:
                    with self._reading() as conn:
                        batch = conn.execute("""
                            SELECT m.memory_id,m.origin_event_id,m.content,m.source_agent,
                                   m.session_id,m.task_id,m.project,m.role,m.scope,
                                   m.created_at,m.metadata_json,e.occurred_at,e.content_hash
                            FROM memories AS m
                            JOIN ledger_events AS e ON e.event_id=m.origin_event_id
                            JOIN _dedupe_groups g
                              ON g.content=m.content
                             AND g.source_agent=m.source_agent
                             AND g.session_id=m.session_id
                             AND g.role=m.role
                            WHERE m.raw_chat=1 AND m.memory_id!=g.survivor_id
                              AND m.memory_id>?
                            ORDER BY m.memory_id ASC LIMIT ?
                        """, (after, batch_size)).fetchall()
                    if not batch:
                        break
                    after = str(batch[-1]["memory_id"])
                    for row in batch:
                        handle.write((json.dumps({
                            "reason": "duplicate",
                            "memory_id": row["memory_id"],
                            "event_id": row["origin_event_id"],
                            "content": row["content"],
                            "content_sha256": row["content_hash"],
                            "source_agent": row["source_agent"],
                            "session_id": row["session_id"],
                            "created_at": row["created_at"],
                            # Same provenance rule as purge_expired_raw: a
                            # collapsed row's metadata (notably archive_rowid)
                            # exists nowhere else after the delete.
                            "metadata": _archive_metadata(row["metadata_json"]),
                            "collapsed_at": utc_now(),
                        }, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                        removed.append(str(row["memory_id"]))
                        removed_events.append(str(row["origin_event_id"]))
            try:
                os.chmod(archive_path, 0o600)
            except OSError:
                pass
            with open(archive_path, "rb") as handle:
                os.fsync(handle.fileno())

            # Verify before deleting (same fail-closed rule as purge).
            found = set()
            with gzip.open(archive_path, "rb") as handle:
                for line in handle:
                    if line.strip():
                        found.add(str(json.loads(line).get("memory_id", "")))
            if set(removed) - found:
                summary["error"] = "archive incomplete; refusing to delete"
                return summary

            def remove(conn: sqlite3.Connection) -> None:
                for start in range(0, len(removed), batch_size):
                    chunk = removed[start:start + batch_size]
                    events = removed_events[start:start + batch_size]
                    ph = ",".join("?" for _ in chunk)
                    eph = ",".join("?" for _ in events)
                    conn.execute(f"DELETE FROM memory_fts WHERE memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM memory_relations WHERE child_memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM memory_relations WHERE parent_memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM memory_claims WHERE memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM memory_feedback WHERE memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM memories WHERE memory_id IN ({ph})", chunk)
                    conn.execute(f"DELETE FROM ledger_events WHERE event_id IN ({eph})", events)

            self._transaction(remove)
            summary["collapsed"] = len(removed)
            summary["archive_path"] = str(archive_path)
            return summary
        finally:
            self._transaction(lambda conn: conn.execute("DROP TABLE IF EXISTS _dedupe_groups"))
