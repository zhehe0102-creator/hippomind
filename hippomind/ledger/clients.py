"""Credentials, trust, export/import, backup, recovery and restore.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from ..schema import fts_indexed_predicate
from ._helpers import (
    AGENT_PERMISSIONS,
    AgentCredential,
    DEFAULT_AGENT_TRUST,
    LedgerError,
    MAX_CONTENT_CHARS,
    MAX_EXPORT_ROWS,
    PORTABLE_EXCLUDED_EVENT_KINDS,
    SCHEMA_VERSION,
    _clamp_float,
    _clamp_int,
    _hash,
    _json,
    _short,
    _strings,
    _text,
    _timestamp,
    utc_now,
)

class ClientsMixin:
    """Credentials, trust, export/import, backup, recovery and restore."""

    @staticmethod
    def _credential_values(agent_id: object, scopes: Optional[Sequence[str]],
                           permissions: Optional[Sequence[str]]) -> Tuple[str, List[str], List[str]]:
        agent = _short(agent_id, 128)
        if not agent:
            raise LedgerError("agent_id 不能为空")
        cleaned_scopes = _strings(scopes or (), 64, 128, "scopes")
        if not cleaned_scopes:
            cleaned_scopes = ["shared", "agent:" + agent]
        cleaned_permissions = _strings(permissions or (), 16, 64, "permissions")
        if not cleaned_permissions:
            cleaned_permissions = ["read", "ingest", "feedback", "stats"]
        invalid = [item for item in cleaned_permissions if item not in AGENT_PERMISSIONS]
        if invalid:
            raise LedgerError("不支持的 agent permission: " + ", ".join(invalid))
        return agent, cleaned_scopes, cleaned_permissions

    def issue_agent_credential(self, agent_id: str, *, scopes: Optional[Sequence[str]] = None,
                               permissions: Optional[Sequence[str]] = None,
                               token: str = "") -> Tuple[AgentCredential, str]:
        """Create a scoped bearer token and return its plaintext only once."""
        agent, cleaned_scopes, cleaned_permissions = self._credential_values(agent_id, scopes, permissions)
        raw_token = _text(token, 512, "agent token") if token else secrets.token_urlsafe(32)
        if len(raw_token) < 24:
            raise LedgerError("agent token 至少需要 24 个字符")
        token_hash = _hash("agent-token", raw_token)
        token_id = "agt_" + _hash(agent, token_hash)[:24]
        now = utc_now()
        credential = AgentCredential(token_id, agent, tuple(cleaned_scopes), tuple(cleaned_permissions), now)

        def write(conn: sqlite3.Connection) -> None:
            existing = conn.execute("SELECT token_id FROM agent_credentials WHERE token_hash=?", (token_hash,)).fetchone()
            if existing is not None:
                raise LedgerError("agent token 已存在；请生成新 token")
            conn.execute("""
                INSERT INTO agent_credentials(token_id,token_hash,agent_id,scopes_json,permissions_json,created_at,revoked_at)
                VALUES(?,?,?,?,?,?, '')
            """, (token_id, token_hash, agent, _json(cleaned_scopes, "scopes"),
                  _json(cleaned_permissions, "permissions"), now))

        self._transaction(write)
        return credential, raw_token

    def resolve_agent_credential(self, token: str) -> Optional[AgentCredential]:
        token = _text(token, 512, "agent token")
        if not token:
            return None
        token_hash = _hash("agent-token", token)
        with self._reading() as conn:
            row = conn.execute("""
                SELECT token_id,agent_id,scopes_json,permissions_json,created_at
                FROM agent_credentials WHERE token_hash=? AND revoked_at=''
            """, (token_hash,)).fetchone()
        if row is None:
            return None
        try:
            scopes = _strings(json.loads(row["scopes_json"]), 64, 128, "scopes")
            permissions = _strings(json.loads(row["permissions_json"]), 16, 64, "permissions")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not scopes or any(item not in AGENT_PERMISSIONS for item in permissions):
            return None
        return AgentCredential(str(row["token_id"]), str(row["agent_id"]), tuple(scopes),
                               tuple(permissions), str(row["created_at"]))

    def revoke_agent_credential(self, token_id: str) -> bool:
        token_id = _short(token_id, 80)
        if not token_id:
            raise LedgerError("token_id 不能为空")
        now = utc_now()

        def write(conn: sqlite3.Connection) -> bool:
            result = conn.execute("UPDATE agent_credentials SET revoked_at=? WHERE token_id=? AND revoked_at=''",
                                  (now, token_id))
            return bool(result.rowcount)

        return self._transaction(write)

    def set_agent_trust(self, agent_id: str, baseline: float, *, source: str = "local-admin") -> Dict[str, Any]:
        """Set an operator-controlled trust prior; agents cannot self-promote."""
        agent_id = _short(agent_id, 128).lower()
        if not agent_id:
            raise LedgerError("agent_id 不能为空")
        baseline = _clamp_float(baseline, DEFAULT_AGENT_TRUST, 0.0, 1.0)
        source = _short(source, 128) or "local-admin"
        now = utc_now()

        def write(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute("""INSERT INTO agent_trust(agent_id,baseline,updated_at,source) VALUES(?,?,?,?)
                            ON CONFLICT(agent_id) DO UPDATE SET baseline=excluded.baseline,
                            updated_at=excluded.updated_at,source=excluded.source""",
                         (agent_id, baseline, now, source))
            return {"agent_id": agent_id, "baseline": baseline, "updated_at": now, "source": source}

        return self._transaction(write)

    def get_agent_trust(self, agent_id: str) -> float:
        agent_id = _short(agent_id, 128).lower()
        with self._reading() as conn:
            return self._agent_trust_for(conn, agent_id)

    def export_json(self, path: os.PathLike, max_rows: int = MAX_EXPORT_ROWS, *, include_raw_chat: bool = False) -> Path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        max_rows = _clamp_int(max_rows, MAX_EXPORT_ROWS, 1, MAX_EXPORT_ROWS)
        with self._reading() as conn:
            if include_raw_chat:
                events = [dict(row) for row in conn.execute("SELECT * FROM ledger_events ORDER BY occurred_at LIMIT ?", (max_rows,))]
            else:
                placeholders = ",".join("?" for _ in PORTABLE_EXCLUDED_EVENT_KINDS)
                events = [dict(row) for row in conn.execute(
                    f"SELECT * FROM ledger_events WHERE event_type NOT IN ({placeholders}) ORDER BY occurred_at LIMIT ?",
                    (*sorted(PORTABLE_EXCLUDED_EVENT_KINDS), max_rows))]
        payload = {"hippomind_memory_hub_export": 2, "schema_version": SCHEMA_VERSION,
                   "exported_at": utc_now(), "portable": True,
                   "raw_chat_included": bool(include_raw_chat), "events": events}
        fd, temp_name = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return target

    def import_json(self, path: os.PathLike, source_agent: str = "legacy-json") -> Dict[str, int]:
        source = Path(path).expanduser()
        if not source.exists():
            raise LedgerError(f"JSON 文件不存在: {source}")
        if source.stat().st_size > 128 * 1024 * 1024:
            raise LedgerError("JSON 导入文件超过 128 MiB 安全上限")
        with source.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, Mapping):
            raise LedgerError("JSON 根节点必须是对象")
        imported = duplicate = 0
        if data.get("hippomind_memory_hub_export") in {1, 2}:
            for event in data.get("events", []):
                if not isinstance(event, Mapping) or event.get("event_type") in {"feedback", "supersede"}:
                    continue
                result = self.ingest({
                    "kind": event.get("event_type", "fact"), "content": event.get("content", ""),
                    "source_agent": event.get("source_agent", source_agent),
                    "session_id": event.get("session_id", ""), "task_id": event.get("task_id", ""),
                    "project": event.get("project", ""), "role": event.get("role", ""),
                    "scope": event.get("scope", "shared"), "timestamp": event.get("occurred_at", ""),
                    "evidence": json.loads(event.get("evidence_json", "[]") or "[]"),
                    "metadata": json.loads(event.get("metadata_json", "{}") or "{}"),
                })
                imported += int(result.inserted)
                duplicate += int(not result.inserted)
            return {"imported": imported, "duplicate": duplicate}
        hippo = data.get("hippo_data", data)
        if not isinstance(hippo, Mapping):
            raise LedgerError("未识别的 HippoMind JSON 格式")
        for field, kind, content_key in (("episodic", "episode", "situation"),
                                         ("semantic", "semantic", "rule"),
                                         ("archive", "archive", "situation")):
            values = hippo.get(field, [])
            if isinstance(values, Mapping):
                values = list(values.values())
            if not isinstance(values, list):
                continue
            for entry in values:
                if not isinstance(entry, Mapping):
                    continue
                content = _short(entry.get(content_key, ""), MAX_CONTENT_CHARS)
                if not content:
                    continue
                legacy_id = _short(entry.get("id", ""), 128)
                metadata = {"legacy_json": str(source), "legacy_kind": field,
                            "legacy_record": dict(entry)}
                result = self.ingest({
                    "kind": kind, "content": content, "source_agent": source_agent,
                    "session_id": str(source), "event_id": f"{source.name}:{field}:{legacy_id}",
                    "scope": _short(entry.get("scope", "agent"), 128) or "agent",
                    "timestamp": entry.get("created", ""), "tags": entry.get("tags", []),
                    "weight": entry.get("weight", 0.5), "importance": entry.get("importance", 5),
                    "confidence": entry.get("confidence", 0.5), "utility": entry.get("utility", 0.0),
                    "evidence": ["legacy:" + _hash(str(source), field, legacy_id)[:32]],
                    "metadata": metadata,
                })
                imported += int(result.inserted)
                duplicate += int(not result.inserted)
        return {"imported": imported, "duplicate": duplicate}

    def export_rows(self, *, scope: str = "shared", include_raw_chat: bool = False,
                    limit: int = 0, since: str = "") -> List[Dict[str, Any]]:
        """导出可再导入的记忆记录（ingest 兼容格式，content_hash 幂等）。

        默认只导 accepted 且非 raw 的行；raw chat 是不可信审计证据，
        需要显式 ``include_raw_chat=True`` 才随包导出。
        ``since`` 只导该 ISO 时刻之后创建的记忆（增量导出）。
        """
        scope = _short(scope, 128) or "shared"
        limit = _clamp_int(limit, 0, 0, 1_000_000)
        clauses = ["m.scope=?", "m.quality_state='accepted'",
                   "m.lifecycle IN ('active','cold')",
                   "(m.valid_until='' OR m.valid_until>?)"]
        params: List[object] = [scope, utc_now()]
        since_iso = _timestamp(since) if since else ""
        if since_iso:
            clauses.append("m.created_at>?")
            params.append(since_iso)
        if not include_raw_chat:
            clauses.append("m.raw_chat=0")
        sql = ("SELECT m.*, e.occurred_at FROM memories m "
               "JOIN ledger_events e ON e.event_id=m.origin_event_id "
               "WHERE " + " AND ".join(clauses) +
               " ORDER BY m.created_at ASC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows: List[Dict[str, Any]] = []
        with self._reading() as conn:
            for row in conn.execute(sql, params):
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                try:
                    evidence = json.loads(row["evidence_json"] or "[]")
                except (TypeError, ValueError):
                    evidence = []
                try:
                    tags = json.loads(row["tags_json"] or "[]")
                except (TypeError, ValueError):
                    tags = []
                record: Dict[str, Any] = {
                    "kind": row["kind"], "content": row["content"],
                    "source_agent": row["source_agent"],
                    "session_id": row["session_id"], "task_id": row["task_id"],
                    "project": row["project"], "role": row["role"],
                    "scope": row["scope"], "evidence": evidence,
                    "timestamp": row["occurred_at"],
                    "importance": int(row["importance"]),
                    "confidence": float(row["confidence"]),
                }
                if tags:
                    record["tags"] = tags
                context = metadata.get("context") if isinstance(metadata, Mapping) else None
                if isinstance(context, Mapping) and context:
                    record["context"] = context
                claims = metadata.get("claims") if isinstance(metadata, Mapping) else None
                if isinstance(claims, list) and claims:
                    first = claims[0]
                    if isinstance(first, Mapping):
                        for key in ("subject", "predicate", "slot_key"):
                            if first.get(key):
                                record[key] = str(first[key])[:256]
                        if "value" in first:
                            record["value"] = first["value"]
                        if first.get("confidence") is not None:
                            record["confidence"] = max(record["confidence"],
                                                       float(first["confidence"]))
                if row["valid_from"]:
                    record["valid_from"] = row["valid_from"]
                if row["valid_until"]:
                    record["valid_until"] = row["valid_until"]
                rows.append(record)
        return rows

    def export_sql(self, *, scope: str = "shared", include_raw_chat: bool = False,
                   limit: int = 0, since: str = "") -> str:
        """导出为可回放 SQL（审计友好）：ledger_events + memories + FTS 重建。

        与 JSONL 导出同源同过滤（export_rows 的 SQL 形态）。回放目标必须是
        同 schema 版本的空库（sqlite3 target.db < dump.sql）；INSERT OR
        IGNORE 保证重复回放幂等。claims/relations/feedback 不随包——它们是
        派生数据，正式迁移请用 backup/restore 或 import。
        """
        rows = self.export_rows(scope=scope, include_raw_chat=include_raw_chat,
                                limit=limit, since=since)
        # 取原始行（含未解析的 json 字段与事件列）
        scope_val = _short(scope, 128) or "shared"
        since_iso = _timestamp(since) if since else ""
        clauses = ["m.scope=?", "m.quality_state='accepted'",
                   "m.lifecycle IN ('active','cold')",
                   "(m.valid_until='' OR m.valid_until>?)"]
        params: List[object] = [scope_val, utc_now()]
        if since_iso:
            clauses.append("m.created_at>?")
            params.append(since_iso)
        if not include_raw_chat:
            clauses.append("m.raw_chat=0")
        sql = ("SELECT m.*, e.event_id AS _event_id, e.event_type AS _event_type, "
               "e.occurred_at AS _occurred_at, e.ingested_at AS _ingested_at, "
               "e.source_agent AS _e_source_agent, e.session_id AS _e_session_id, "
               "e.task_id AS _e_task_id, e.project AS _e_project, e.role AS _e_role, "
               "e.evidence_json AS _e_evidence_json, e.content AS _e_content, "
               "e.content_hash AS _e_content_hash, e.metadata_json AS _e_metadata_json "
               "FROM memories m JOIN ledger_events e ON e.event_id=m.origin_event_id "
               "WHERE " + " AND ".join(clauses) + " ORDER BY m.created_at ASC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        out: List[str] = [
            "-- HippoMind 审计导出（可回放）",
            f"-- generated: {utc_now()}",
            f"-- scope: {scope_val} | rows: {len(rows)} | "
            f"schema_version: {SCHEMA_VERSION}",
            "-- 回放: sqlite3 target.db < this_file.sql （须为同版本空库）",
            "BEGIN TRANSACTION;",
        ]

        def q(value) -> str:
            if value is None:
                return "NULL"
            return "'" + str(value).replace("'", "''") + "'"

        seen_events: set = set()
        with self._reading() as conn:
            for row in conn.execute(sql, params):
                eid = row["_event_id"]
                if eid not in seen_events:
                    seen_events.add(eid)
                    out.append(
                        "INSERT OR IGNORE INTO ledger_events(event_id,event_type,"
                        "occurred_at,ingested_at,source_agent,session_id,task_id,"
                        "project,role,scope,evidence_json,content,content_hash,"
                        "metadata_json) VALUES("
                        + ",".join(q(row[k]) for k in (
                            "_event_id", "_event_type", "_occurred_at",
                            "_ingested_at", "_e_source_agent", "_e_session_id",
                            "_e_task_id", "_e_project", "_e_role", "scope",
                            "_e_evidence_json", "_e_content", "_e_content_hash",
                            "_e_metadata_json")) + ");")
                out.append(
                    "INSERT OR IGNORE INTO memories(memory_id,origin_event_id,kind,"
                    "content,source_agent,session_id,task_id,project,role,scope,"
                    "evidence_json,tags_json,metadata_json,created_at,updated_at,"
                    "last_access_at,last_weighted_at,valid_until,superseded_by,"
                    "archived_at,lifecycle,raw_chat,weight,importance,touch_count,"
                    "utility,confidence,success_count,failure_count,pinned) VALUES("
                    + ",".join(q(row[k]) for k in (
                        "memory_id", "origin_event_id", "kind", "content",
                        "source_agent", "session_id", "task_id", "project",
                        "role", "scope", "evidence_json", "tags_json",
                        "metadata_json", "created_at", "updated_at",
                        "last_access_at", "last_weighted_at", "valid_until",
                        "superseded_by", "archived_at", "lifecycle", "raw_chat",
                        "weight", "importance", "touch_count", "utility",
                        "confidence", "success_count", "failure_count",
                        "pinned")) + ");")
        # Rebuild only the hot index, using the same single predicate as every
        # other writer.  The unconditional ``SELECT ... FROM memories`` used to
        # copy cold/quarantined/conflicted rows into the target ledger's FTS,
        # which the target immediately reported as drift.  A dump carries only
        # ``accepted`` rows in ``active``/``cold`` lifecycle (see the clause
        # above), so this is exactly "the active subset".
        out.append("INSERT INTO memory_fts(memory_id, content) "
                   "SELECT memory_id, content FROM memories WHERE "
                   + fts_indexed_predicate() + ";")
        out.append("COMMIT;")
        return "\n".join(out) + "\n"

    def backup_to(self, path: os.PathLike) -> Path:
        target = Path(path).expanduser()
        if target.exists() and (not target.is_file() or target.is_symlink()):
            raise LedgerError(f"备份目标必须是普通文件: {target}")
        try:
            if target.resolve() == self.path.resolve():
                raise LedgerError("备份目标不能覆盖生产账本")
        except OSError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="." + target.name + ".",
                                         suffix=".tmp", dir=str(target.parent))
        os.close(fd)
        temp = Path(temp_name)
        source_conn = None
        dest_conn = None
        try:
            source_conn = self._connect()
            dest_conn = sqlite3.connect(str(temp))
            source_conn.backup(dest_conn)
            dest_conn.commit()
            integrity = dest_conn.execute("PRAGMA integrity_check").fetchone()[0]
            if str(integrity).lower() != "ok":
                raise LedgerError("SQLite 备份完整性检查失败")
            dest_conn.close()
            dest_conn = None
            source_conn.close()
            source_conn = None
            os.replace(temp, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            return target
        finally:
            if dest_conn is not None:
                dest_conn.close()
            if source_conn is not None:
                source_conn.close()
            # SQLite can create sidecars for the disposable destination even
            # though the final backup is a single atomically replaced file.
            # Only remove names derived from this invocation's unique temp.
            for candidate in (temp, Path(str(temp) + "-wal"), Path(str(temp) + "-shm")):
                try:
                    candidate.unlink()
                except OSError:
                    pass

    def recovery_check(self) -> Dict[str, Any]:
        """Run a disposable backup/open/restore validation drill.

        No live projection is replaced. The returned counts are safe to record
        in health telemetry and prove that WAL pages can be recovered.
        """
        fd, name = tempfile.mkstemp(prefix=".hippomind-recovery-", suffix=".sqlite",
                                    dir=str(self.path.parent))
        os.close(fd)
        temporary = Path(name)
        try:
            self.backup_to(temporary)
            with sqlite3.connect(str(temporary)) as conn:
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower()
                version = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
                events = int(conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0])
            if integrity != "ok" or int(version) > SCHEMA_VERSION:
                raise LedgerError("恢复演练未通过 schema/integrity 校验")
            return {"ok": True, "integrity": integrity, "schema_version": int(version),
                    "events": events, "backup": str(temporary)}
        finally:
            for candidate in (temporary, Path(str(temporary) + "-wal"),
                              Path(str(temporary) + "-shm")):
                try:
                    candidate.unlink()
                except OSError:
                    pass

    def restore_from(self, backup_path: os.PathLike) -> None:
        """Restore a validated SQLite backup into this ledger's projection.

        Callers should stop the daemon first.  Validation happens before the
        destination is touched; SQLite's backup API then performs the copy.
        """
        source_path = Path(backup_path).expanduser()
        if not source_path.exists():
            raise LedgerError(f"备份不存在: {source_path}")
        # A normal absolute-path connection is more portable across the
        # macOS SQLite builds than a hand-built ``file:`` URI. query_only keeps
        # the validated backup source read-only for this operation.
        source = sqlite3.connect(str(source_path))
        source.execute("PRAGMA query_only=ON")
        try:
            row = source.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is None or int(row[0]) > SCHEMA_VERSION:
                raise LedgerError("备份不是兼容的 HippoMind 账本")
            required_tables = {"ledger_events", "memories", "memory_fts", "source_cursors"}
            present = {str(item[0]) for item in source.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}
            if not required_tables.issubset(present):
                raise LedgerError("备份缺少 HippoMind 必要表，拒绝恢复")
            integrity = str(source.execute("PRAGMA integrity_check").fetchone()[0]).lower()
            if integrity != "ok":
                raise LedgerError("备份完整性检查失败，拒绝恢复")
            dest = sqlite3.connect(str(self.path))
            try:
                source.backup(dest)
                dest.commit()
            finally:
                dest.close()
        finally:
            source.close()
        self._bootstrap()

