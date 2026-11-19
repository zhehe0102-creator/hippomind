#!/usr/bin/env python3
"""冻结副本数据治理演练台账 (WP-L)。

为什么需要这个工具
    生产治理动作（repair_claim_states / backfill_valid_until / maintain /
    collapse_duplicate_rows）都会写账本。直接在真实账本上演练风险不可接受，
    而本机已不持有生产主账本，所以演练必须能同时证明两件事：

      1. 输入账本（--source，或内建合成账本）只以只读方式读取，
         其 size/mtime/sha256 在演练前后完全不变；
      2. 治理步骤在**冻结副本**上按顺序执行并测量收敛结果，
         源账本永远不是写入目标。

安全契约
    * 源账本用 SQLite online backup API 生成一致性快照（cp 会漏掉 WAL 中
      已提交但未 checkpoint 的页）。快照落盘后立刻做 integrity_check。
    * 所有治理写入只发生在冻结快照派生出的副本上：
        - apply：副本即交付物，保留在 --workdir 或 --target；
        - dry-run（默认）：在一次性 simulation 副本上真实执行步骤以测量
          "如果 apply 会收敛到什么状态"，结束时删除该副本。
      两种模式下源账本与冻结快照都不会被写入。
    * --apply 只作用于副本：--target 与 --source 指向同一文件直接拒绝；
      只给 --target 而不给 --source（无法证明它是副本）时也拒绝。
    * 每一步独立 try/except，单步失败只把 ok 记成 false 并继续其余步骤，
      JSON 输出能定位到具体步骤和错误；不会吞掉异常。

步骤（全部在冻结副本上）
    1. repair_claim_states     -- claim 状态与记忆可召回性双向收敛
    2. backfill_valid_until    -- 直接复用 scripts/backfill_valid_until.py
                                  的 scan/apply（不重复实现回填规则）
    3. collapse_duplicate_rows -- 归一化内容完全相同的 raw 行折叠（先归档）
    4. maintain(archive_raw=False) -- 衰减/过期/冷置/索引收敛统计
    5. repair_claim_states     -- maintain 改变可召回性后再收敛一次
    6. identity_audit          -- claims 悬空 / FTS 漂移 / 重复候选只读审计
    7. commons_reconcile       -- 仅在显式给出 --commons-archive 时做只读对账
                                  （默认绝不触碰真实 Agent 归档）

用法
    # 合成账本默认演练（>=5k 行，dry-run）
    PYTHONPATH="$(pwd)" /usr/bin/python3 scripts/governance_rehearsal.py

    # 冻结快照 + 在副本上落盘治理动作（源账本不写）
    PYTHONPATH="$(pwd)" /usr/bin/python3 scripts/governance_rehearsal.py \
        --source /path/to/hub.sqlite --workdir /tmp/rehearsal --apply --json

    # 已有副本上重复校验幂等性
    PYTHONPATH="$(pwd)" /usr/bin/python3 scripts/governance_rehearsal.py \
        --source /path/to/hub.sqlite --target /tmp/rehearsal/work.sqlite --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# 直接复用既有回填实现：规则（胜者 valid_from / 无法定日期的跳过语义）只有
# 一份，演练与生产脚本不会漂移。
import backfill_valid_until  # noqa: E402

from hippomind.ledger import SQLiteLedger, utc_now  # noqa: E402
from hippomind.schema import fts_indexed_predicate  # noqa: E402

SCHEMA_TAG = "hippomind-governance-rehearsal/1"
DEFAULT_SYNTHETIC_ROWS = 5200
#: 合成账本至少要有这么多行，才算"代表性"冻结副本演练。
MIN_SYNTHETIC_ROWS = 5000
#: 账本表契约：缺任何一张表的 SQLite 文件都不是可演练的 HippoMind 账本。
REQUIRED_TABLES = ("memories", "memory_claims", "memory_fts", "ledger_events")


class RehearsalError(RuntimeError):
    """拒绝执行 / 输入校验失败（CLI 退出码 2）。"""


# ---------------------------------------------------------------------------
# 文件指纹与一致性快照
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path) -> Dict[str, Any]:
    """size + mtime_ns + sha256: the three facts the source must keep."""
    target = Path(path)
    stat = target.stat()
    return {"path": str(target), "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns), "sha256": _sha256(target)}


def _same_path(left: Path, right: Path) -> bool:
    if Path(left).resolve() == Path(right).resolve():
        return True
    try:
        return os.path.samefile(str(left), str(right))
    except OSError:
        return False


def _sqlite_uri(path: Path, mode: str) -> str:
    return Path(path).resolve().as_uri() + "?mode=" + mode


def open_readonly(path: Path) -> sqlite3.Connection:
    """Read-only connection used for every measurement on a frozen artifact.

    mode=ro cannot open a WAL database whose -shm sidecar is missing, so the
    fallback opens the file normally and enforces query_only: every write is
    still refused at the SQLite layer, and the rehearsal only ever issues
    SELECT/PRAGMA statements through this connection.
    """
    try:
        conn = sqlite3.connect(_sqlite_uri(path, "ro"), uri=True, timeout=10.0)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error:
        conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=10000")
    except sqlite3.DatabaseError:
        pass
    return conn


def _snapshot_copy(source: Path, dest: Path) -> Dict[str, Any]:
    """Consistent copy through the SQLite online backup API.

    The copy is forced back to a rollback journal before it is used as a frozen
    artifact: a WAL-flagged database opened read-only without its -shm fails,
    and opening it read-write would let measurements mutate the artifact.
    """
    source = Path(source)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        side = Path(str(dest) + suffix)
        if side.exists():
            side.unlink()
    src = sqlite3.connect(_sqlite_uri(source, "ro"), uri=True, timeout=30.0)
    try:
        dst = sqlite3.connect(str(dest), timeout=30.0)
        try:
            src.backup(dst)
            dst.commit()
            try:
                dst.execute("PRAGMA journal_mode=DELETE").fetchone()
            except sqlite3.Error:
                pass
            integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        finally:
            dst.close()
    finally:
        src.close()
    if integrity != "ok":
        raise RehearsalError("一致性快照完整性检查失败: %s" % integrity)
    return file_fingerprint(dest)


def validate_source(path: os.PathLike) -> Path:
    """Reject anything that is not a readable HippoMind SQLite ledger."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise RehearsalError("源账本不存在: %s" % source)
    if source.is_symlink():
        raise RehearsalError("源账本不能是符号链接（拒绝跟随到未知目标）: %s" % source)
    try:
        conn = open_readonly(source)
    except sqlite3.Error as exc:
        raise RehearsalError("源账本不是可读的 SQLite 文件: %s: %s"
                             % (type(exc).__name__, exc))
    try:
        names = {str(row[0]) for row in
                 conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in REQUIRED_TABLES if name not in names]
        if missing:
            raise RehearsalError("源账本缺少 HippoMind 表 [%s]，不是可演练的账本: %s"
                                 % (", ".join(missing), source))
        conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        conn.execute("SELECT COUNT(*) FROM memory_claims").fetchone()
    except sqlite3.Error as exc:
        raise RehearsalError("源账本读取失败: %s: %s" % (type(exc).__name__, exc))
    finally:
        conn.close()
    return source


def _remove_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        side = Path(str(path) + suffix)
        try:
            if side.exists():
                side.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 合成代表性脏账本
# ---------------------------------------------------------------------------
def _iso_ago(days: float = 0.0, seconds: float = 0.0) -> str:
    stamp = time.time() - days * 86_400.0 - seconds
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def build_synthetic_ledger(path: os.PathLike, *,
                           rows: int = DEFAULT_SYNTHETIC_ROWS) -> Dict[str, Any]:
    """Build a representative dirty ledger for rehearsal.

    The write path intentionally refuses several of these shapes today (raw
    identity dedupe, streaming revision supersede, claim/memory convergence),
    so a historical ledger is simulated by ingesting through the ledger and
    then applying the exact historical damage with SQL: duplicate raw rows,
    streaming drafts and finals, dangling active claims, superseded claims with
    an empty valid_until, expired claims and cold-archived rows.
    """
    target = Path(path)
    rows = max(60, int(rows))
    target.parent.mkdir(parents=True, exist_ok=True)
    _remove_sqlite(target)
    ledger = SQLiteLedger(target)
    now_iso = utc_now()
    dup_groups = max(6, rows // 250)
    dup_copies = 4
    stream_pairs = max(3, rows // 500)
    dangling = max(8, rows // 200)
    backfill = max(8, rows // 200)
    cold = max(10, rows // 200)
    expired = max(4, rows // 100)
    structured = max(20, int(rows * 0.25))
    old_rows = max(3, rows // 100)
    planned = (dup_groups * dup_copies + stream_pairs * 2 + dangling
               + backfill * 2 + cold + expired + structured + old_rows)
    normal = max(40, rows - planned)
    sequence = [0]
    batch: List[Dict[str, Any]] = []

    def _flush() -> None:
        # ingest_many caps one transaction at 1000 records; chunk so a 5k+
        # synthetic ledger never trips the batch limit.
        while batch:
            chunk = batch[:400]
            del batch[:400]
            ledger.ingest_many(chunk)

    def _raw(content: str, session: str, role: str = "assistant",
             agent: str = "codex", scope: str = "",
             event_id: str = "", timestamp: str = "") -> None:
        sequence[0] += 1
        record: Dict[str, Any] = {
            "content": content,
            "kind": "raw_chat",
            "raw_chat": True,
            "source_agent": agent,
            "session_id": session,
            "role": role,
            "scope": scope or ("agent:" + agent),
            "evidence": ["synth:%s:%d" % (agent, sequence[0])],
            "timestamp": timestamp or now_iso,
        }
        if event_id:
            record["event_id"] = event_id
        batch.append(record)

    try:
        for index in range(normal):
            _raw("合成普通消息 %d：记录当前进度与决定，用于代表性样本。" % index,
                 "syn-bulk-%d" % (index % 37))
        # Duplicate raw rows: identical content/session/role but distinct source
        # event ids, which is how the append-only ledger legitimately kept them
        # until a hygiene pass collapsed them.
        for group in range(dup_groups):
            body = "重复采集的同一段对话 %d：决定保留该方案并继续验收。" % group
            session = "syn-dup-%d" % group
            for copy_index in range(dup_copies):
                _raw(body, session,
                     event_id="syn-dup-%d-%d" % (group, copy_index))
        _flush()

        for index in range(structured):
            batch.append({
                "content": "结构化记忆 %d：配置项与验收状态记录。" % index,
                "kind": "fact",
                "source_agent": "codex",
                "session_id": "syn-structured-%d" % (index % 29),
                "scope": "shared",
                "evidence": ["synth:structured:%d" % index],
                "timestamp": now_iso,
                "claims": [{
                    "subject": "syn-entity-%d" % (index % 53),
                    "predicate": "config",
                    "slot_key": "syn-slot-%d" % index,
                    "value": "value-%d" % index,
                    "confidence": 0.8,
                }],
            })
        for index in range(expired):
            past_from = _iso_ago(200.0)
            past_until = _iso_ago(150.0)
            batch.append({
                "content": "已过期的历史判断 %d：旧端口配置已被替换。" % index,
                "kind": "fact",
                "source_agent": "codex",
                "session_id": "syn-expired-%d" % index,
                "scope": "shared",
                "evidence": ["synth:expired:%d" % index],
                "timestamp": _iso_ago(200.0),
                "valid_from": past_from,
                "valid_until": past_until,
                "claims": [{
                    "subject": "syn-expired",
                    "predicate": "config",
                    "slot_key": "syn-expired-slot-%d" % index,
                    "value": "old-%d" % index,
                    "confidence": 0.7,
                    "valid_from": past_from,
                    "valid_until": past_until,
                }],
            })
        for index in range(old_rows):
            _raw("旧会话消息 %d：原始证据已超过保留窗口。" % index,
                 "syn-old-%d" % index, timestamp=_iso_ago(200.0 + index))
        _flush()

        # Streaming draft -> final: same event id, revised content.  The current
        # write path supersedes the draft; the pair is still a representative
        # capture shape the rehearsal must audit.
        stream_ids = []
        for index in range(stream_pairs):
            event_id = "syn-stream-%d" % index
            base = {
                "kind": "raw_chat", "raw_chat": True, "source_agent": "cursor",
                "session_id": "syn-stream-%d" % index, "role": "assistant",
                "scope": "agent:cursor",
                "event_id": event_id, "timestamp": now_iso,
            }
            draft = ledger.ingest(dict(base, content="流式草稿 %d：先使用 8000。" % index,
                                       evidence=["synth:stream:%d:draft" % index]))
            final = ledger.ingest(dict(base, content="流式草稿 %d：最终改为 9000。" % index,
                                       evidence=["synth:stream:%d:final" % index]))
            stream_ids.append((draft.memory_id, final.memory_id))

        # Dangling claims: an active claim on a memory that is not recallable
        # (round-12 shape: quarantine left the claim active).
        dangling_ids = []
        for index in range(dangling):
            result = ledger.ingest({
                "content": "悬空 claim 样本 %d：配置值待定，用于复现悬空状态。" % index,
                "kind": "fact",
                "source_agent": "codex",
                "session_id": "syn-dangling-%d" % index,
                "scope": "shared",
                "evidence": ["synth:dangling:%d" % index],
                "timestamp": now_iso,
                "claims": [{
                    "subject": "syn-dangling",
                    "predicate": "config",
                    "slot_key": "syn-dangling-slot-%d" % index,
                    "value": "pending-%d" % index,
                    "confidence": 0.7,
                }],
            })
            dangling_ids.append(result.memory_id)

        # Backfill targets: superseded claim + empty valid_until + a real winner.
        backfill_pairs = []
        for index in range(backfill):
            loser_from = _iso_ago(120.0)
            winner_from = _iso_ago(60.0)
            slot = "syn-history-slot-%d" % index
            loser = ledger.ingest({
                "content": "历史决策旧值 %d：旧方案已让位。" % index,
                "kind": "fact", "source_agent": "codex", "scope": "shared",
                "session_id": "syn-history-%d" % index,
                "evidence": ["synth:history:%d:old" % index],
                "timestamp": loser_from, "valid_from": loser_from,
                "claims": [{"subject": "syn-history", "predicate": "decision",
                            "slot_key": slot, "value": "old-%d" % index,
                            "confidence": 0.5, "valid_from": loser_from}],
            })
            winner = ledger.ingest({
                "content": "历史决策新值 %d：新方案接替旧方案。" % index,
                "kind": "fact", "source_agent": "codex", "scope": "shared",
                "session_id": "syn-history-%d" % index,
                "evidence": ["synth:history:%d:new" % index],
                "timestamp": winner_from, "valid_from": winner_from,
                "claims": [{"subject": "syn-history", "predicate": "decision",
                            "slot_key": slot, "value": "new-%d" % index,
                            "confidence": 0.95, "valid_from": winner_from}],
            })
            backfill_pairs.append((loser.memory_id, winner.memory_id, slot))

        cold_ids = []
        for index in range(cold):
            result = ledger.ingest({
                "content": "已冷归档的旧事实 %d：审计保留、不再召回。" % index,
                "kind": "fact", "source_agent": "codex", "scope": "shared",
                "session_id": "syn-cold-%d" % index,
                "evidence": ["synth:cold:%d" % index],
                "timestamp": now_iso,
                "claims": [{"subject": "syn-cold", "predicate": "status",
                            "slot_key": "syn-cold-slot-%d" % index,
                            "value": "archived-%d" % index, "confidence": 0.6}],
            })
            cold_ids.append(result.memory_id)
    finally:
        ledger.close()

    # Historical damage: direct SQL, because the current write path refuses
    # these shapes by design (that is the point of the frozen replica).
    conn = sqlite3.connect(str(target), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        for memory_id in dangling_ids:
            conn.execute("UPDATE memories SET quality_state='quarantine',updated_at=? "
                         "WHERE memory_id=?", (now_iso, memory_id))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        for loser_id, winner_id, slot_key in backfill_pairs:
            conn.execute("UPDATE memory_claims SET state='superseded',valid_until='' "
                         "WHERE memory_id=? AND slot_key=?", (loser_id, slot_key))
            conn.execute("UPDATE memories SET lifecycle='superseded',"
                         "quality_state='superseded',superseded_by=?,valid_until='',"
                         "updated_at=? WHERE memory_id=?",
                         (winner_id, now_iso, loser_id))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (loser_id,))
        for memory_id in cold_ids:
            conn.execute("UPDATE memories SET lifecycle='cold',archived_at=?,updated_at=? "
                         "WHERE memory_id=?", (now_iso, now_iso, memory_id))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    shape = collect_metrics(target)
    summary = {"rows": shape["memories"],
               "duplicate_groups": shape["duplicate_groups"],
               "duplicate_redundant_rows": shape["duplicate_redundant"],
               "dangling_claims": shape["dangling_claims"],
               "missing_windows": shape["missing_windows"],
               "cold_rows": shape["cold_rows"],
               "expired_rows": shape["expired_rows"],
               "stream_pairs": len(stream_ids),
               "backfill_pairs": len(backfill_pairs),
               "dangling_samples": dangling_ids[:3],
               "backfill_samples": [pair[0] for pair in backfill_pairs[:3]]}
    if int(summary["rows"]) < min(rows, 60):
        raise RehearsalError("合成账本行数异常: %s" % summary)
    return summary


# ---------------------------------------------------------------------------
# 指标（只读）
# ---------------------------------------------------------------------------
def collect_metrics(path: os.PathLike) -> Dict[str, Any]:
    """Before/after metrics.  Read-only; never opens the artifact read-write."""
    conn = open_readonly(Path(path))
    try:
        def count(sql: str, params: Sequence[Any] = ()) -> int:
            row = conn.execute(sql, tuple(params)).fetchone()
            return int(row[0] or 0)

        fts_indexed = fts_indexed_predicate("m")
        duplicate_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(n - 1), 0) FROM ("
            " SELECT COUNT(*) AS n FROM memories"
            " WHERE raw_chat=1 AND content!=''"
            " GROUP BY content, source_agent, session_id, role"
            " HAVING COUNT(*)>1)").fetchone()
        page_size = count("PRAGMA page_size")
        page_count = count("PRAGMA page_count")
        freelist = count("PRAGMA freelist_count")
        metrics: Dict[str, Any] = {
            "memories": count("SELECT COUNT(*) FROM memories"),
            "raw_rows": count("SELECT COUNT(*) FROM memories WHERE raw_chat=1"),
            "claims": count("SELECT COUNT(*) FROM memory_claims"),
            "active_claims": count(
                "SELECT COUNT(*) FROM memory_claims WHERE state='active'"),
            "superseded_claims": count(
                "SELECT COUNT(*) FROM memory_claims WHERE state='superseded'"),
            "conflict_claims": count(
                "SELECT COUNT(*) FROM memory_claims WHERE state='conflict'"),
            "dangling_claims": count(
                "SELECT COUNT(*) FROM memory_claims c LEFT JOIN memories m"
                " ON m.memory_id=c.memory_id WHERE c.state='active'"
                " AND (m.memory_id IS NULL OR m.quality_state!='accepted')"),
            "missing_windows": count(
                "SELECT COUNT(*) FROM memory_claims"
                " WHERE state='superseded' AND valid_until=''"),
            "missing_memory_windows": count(
                "SELECT COUNT(*) FROM memories"
                " WHERE lifecycle='superseded' AND valid_until=''"),
            "duplicate_groups": int(duplicate_row[0] or 0),
            "duplicate_redundant": int(duplicate_row[1] or 0),
            "cold_rows": count(
                "SELECT COUNT(*) FROM memories WHERE lifecycle='cold'"),
            "expired_rows": count(
                "SELECT COUNT(*) FROM memories WHERE lifecycle='expired'"),
            "superseded_memories": count(
                "SELECT COUNT(*) FROM memories WHERE lifecycle='superseded'"),
            "fts_rows": count("SELECT COUNT(*) FROM memory_fts"),
            "fts_expected": count(
                "SELECT COUNT(*) FROM memories m WHERE " + fts_indexed),
            "fts_unindexed": count(
                "SELECT COUNT(*) FROM memories m WHERE " + fts_indexed
                + " AND NOT EXISTS (SELECT 1 FROM memory_fts f"
                " WHERE f.memory_id=m.memory_id)"),
            "storage": {
                "page_size": page_size,
                "page_count": page_count,
                "freelist": freelist,
                "db_bytes": page_size * page_count,
                "reclaimable_bytes": page_size * freelist,
            },
        }
        metrics["reclaimable_bytes"] = int(metrics["storage"]["reclaimable_bytes"])
        metrics["db_bytes"] = int(metrics["storage"]["db_bytes"])
        return metrics
    finally:
        conn.close()


def _delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> Dict[str, Any]:
    keys = ("dangling_claims", "missing_windows", "missing_memory_windows",
            "duplicate_groups", "duplicate_redundant", "active_claims",
            "superseded_claims", "conflict_claims", "cold_rows", "expired_rows",
            "fts_rows", "fts_unindexed", "reclaimable_bytes", "db_bytes")
    delta: Dict[str, Any] = {}
    for key in keys:
        try:
            delta[key] = int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0)
        except (TypeError, ValueError):
            delta[key] = None
    return delta


# ---------------------------------------------------------------------------
# 治理步骤（全部只作用于传入的副本路径）
# ---------------------------------------------------------------------------
def _step_result(name: str, fn, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    started = time.time()
    ok = True
    error = ""
    detail: Dict[str, Any] = {}
    try:
        value = fn(*args, **kwargs)
        if isinstance(value, Mapping):
            detail = dict(value)
        elif value is not None:
            detail = {"result": value}
    except Exception as exc:  # noqa: BLE001 - per-step isolation is the contract
        ok = False
        error = "%s: %s" % (type(exc).__name__, str(exc)[:500])
    return {"name": name, "ok": ok, "error": error,
            "elapsed_ms": round((time.time() - started) * 1000.0, 1),
            "detail": detail}


def step_repair_claim_states(work_path: Path) -> Dict[str, Any]:
    ledger = SQLiteLedger(Path(work_path))
    try:
        return ledger.repair_claim_states()
    finally:
        ledger.close()


def step_backfill_valid_until(work_path: Path) -> Dict[str, Any]:
    """Reuse scripts/backfill_valid_until.py rather than re-implementing it."""
    work_path = Path(work_path)
    conn = backfill_valid_until.open_readonly(work_path)
    try:
        rows = backfill_valid_until.scan(conn)
    finally:
        conn.close()
    detail: Dict[str, Any] = dict(backfill_valid_until.summarise(rows))
    detail["claims_updated"] = 0
    detail["memories_updated"] = 0
    if rows:
        applied = backfill_valid_until.apply_windows(work_path, rows)
        detail["claims_updated"] = int(applied.get("claims_updated", 0) or 0)
        detail["memories_updated"] = int(
            backfill_valid_until.apply_memory_windows(work_path, rows))
    return detail


def step_collapse_duplicates(work_path: Path, archive_dir: Path) -> Dict[str, Any]:
    ledger = SQLiteLedger(Path(work_path))
    try:
        return ledger.collapse_duplicate_rows(archive_dir=Path(archive_dir),
                                              dry_run=False)
    finally:
        ledger.close()


def step_maintain(work_path: Path) -> Dict[str, Any]:
    ledger = SQLiteLedger(Path(work_path))
    try:
        result = ledger.maintain(archive_raw=False)
    finally:
        ledger.close()
    keys = ("decayed", "expired", "archived", "raw_archived", "decay_events",
            "fts_pruned", "events_archived", "raw_compressed", "raw_purged",
            "storage", "auto_vacuum")
    return {key: result.get(key) for key in keys if key in result}


def step_identity_audit(work_path: Path) -> Dict[str, Any]:
    ledger = SQLiteLedger(Path(work_path))
    try:
        claims = ledger.claims_consistency_report()
        fts = ledger.fts_sync_report()
        integrity = ledger.integrity_check()
        duplicates = ledger.duplicate_candidates(limit=20)
        return {"claims": claims, "fts": fts,
                "integrity": str(integrity.get("status", "")),
                "duplicate_suggestions": int(duplicates.get("suggestions", 0) or 0)}
    finally:
        ledger.close()


def step_commons_reconcile(work_path: Path,
                           commons_archive: Optional[str]) -> Dict[str, Any]:
    """Read-only reconciliation against Agent-Chat-Archive, opt-in only.

    There is no write-capable commons reconcile API in this repository, so the
    step reports the inventory difference (shared rows missing on either side)
    instead of pretending to mutate.  Without --commons-archive it skips, so a
    rehearsal never touches the real Agent archive by accident.
    """
    if not commons_archive:
        return {"skipped": "未提供 --commons-archive；默认不触碰真实 Agent 归档"}
    try:
        from hippomind.commons import MemoryCommons  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - optional dependency boundary
        return {"skipped": "MemoryCommons 不可导入: %s: %s"
                           % (type(exc).__name__, str(exc)[:200])}
    try:
        commons = MemoryCommons(str(commons_archive))
    except (FileNotFoundError, OSError) as exc:
        return {"skipped": "Agent-Chat-Archive 不可用: %s" % str(exc)[:200]}
    if not commons.shared_db.exists():
        return {"skipped": "commons 共享库不存在: %s" % commons.shared_db}
    shared_ids: set = set()
    conn = sqlite3.connect(_sqlite_uri(commons.shared_db, "ro"), uri=True,
                           timeout=10.0)
    try:
        for row in conn.execute("SELECT memory_id FROM shared_memories LIMIT 100000"):
            shared_ids.add(str(row[0]))
    finally:
        conn.close()
    ledger_ids: set = set()
    conn = sqlite3.connect(_sqlite_uri(Path(work_path), "ro"), uri=True,
                           timeout=10.0)
    try:
        for row in conn.execute("SELECT memory_id FROM memories LIMIT 100000"):
            ledger_ids.add(str(row[0]))
    finally:
        conn.close()
    orphan = sorted(shared_ids - ledger_ids)
    local_only = sorted(ledger_ids - shared_ids)
    return {"archive": str(commons.archive_root),
            "commons_rows": len(shared_ids), "ledger_rows": len(ledger_ids),
            "orphan_in_commons": len(orphan), "local_only": len(local_only),
            "orphan_samples": orphan[:5], "local_only_samples": local_only[:5]}


# ---------------------------------------------------------------------------
# 演练编排
# ---------------------------------------------------------------------------
def rehearse(*, source: Optional[os.PathLike] = None,
             target: Optional[os.PathLike] = None,
             workdir: Optional[os.PathLike] = None,
             apply: bool = False, keep: bool = False,
             rows: int = DEFAULT_SYNTHETIC_ROWS,
             commons_archive: Optional[str] = None,
             work_root: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """Run the frozen-copy governance rehearsal and return the report dict."""
    started = time.time()
    if apply and source is None and target is not None:
        raise RehearsalError(
            "--apply 只能作用于冻结副本：只给 --target 无法证明它是副本，拒绝直接写该路径")
    if work_root is not None:
        root = Path(work_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        retained_root = True
    elif workdir is not None:
        root = Path(workdir).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        retained_root = True
    else:
        root = Path(tempfile.mkdtemp(prefix="hippo-governance-"))
        retained_root = False

    result: Dict[str, Any] = {"schema": SCHEMA_TAG, "started_at": utc_now(),
                              "dry_run": not apply, "ok": False,
                              "workdir": str(root), "mode": "", "steps": [],
                              "source": {}, "frozen": {}, "copies": {},
                              "before": {}, "after": {}, "delta": {},
                              "synthetic": None, "reclaimable_bytes": {},
                              "step_errors": [], "elapsed_ms": 0.0}

    # 1. Input: frozen source ledger or a synthetic representative ledger.
    synthetic_summary: Optional[Dict[str, Any]] = None
    if source is not None:
        source_path = validate_source(source)
        mode = "frozen-source"
        source_before = file_fingerprint(source_path)
    else:
        source_path = root / "synthetic-source.sqlite"
        synthetic_summary = build_synthetic_ledger(source_path, rows=rows)
        mode = "synthetic"
        source_before = file_fingerprint(source_path)
    result["mode"] = mode
    result["source"] = {"path": str(source_path),
                        "fingerprint_before": source_before}

    # 2. Consistent snapshot of the input; the snapshot is the only thing the
    #    rehearsal may ever mutate (through disposable copies).
    frozen = root / "frozen.sqlite"
    _snapshot_copy(source_path, frozen)
    frozen_initial = file_fingerprint(frozen)
    result["frozen"] = {"path": str(frozen), "fingerprint": frozen_initial}

    target_path: Optional[Path] = None
    if target is not None:
        target_path = Path(target).expanduser().resolve()
        if _same_path(target_path, source_path):
            raise RehearsalError(
                "--target 与源账本指向同一文件；apply 只能作用于副本: %s" % target_path)
        if target_path.exists() and not target_path.is_file():
            raise RehearsalError("--target 必须是普通文件: %s" % target_path)

    # 3. Before-state: an existing target is itself a frozen copy (this is what
    #    makes a second --apply run an idempotency check); otherwise the
    #    before-state is the source snapshot.
    base_snapshot = frozen
    target_before: Optional[Dict[str, Any]] = None
    if target_path is not None and target_path.exists():
        target_before = file_fingerprint(target_path)
        base_snapshot = root / "frozen-target.sqlite"
        _snapshot_copy(target_path, base_snapshot)
    base_fingerprint = file_fingerprint(base_snapshot)
    result["before"] = collect_metrics(base_snapshot)

    # 4. Active copy: apply mutates the delivered copy; dry-run mutates a
    #    disposable simulation copy and throws it away.
    delivered: Optional[Path] = target_path
    delivered_created = False
    if apply:
        if delivered is None:
            delivered = root / "work.sqlite"
            _snapshot_copy(base_snapshot, delivered)
            delivered_created = True
        elif not delivered.exists():
            _snapshot_copy(base_snapshot, delivered)
            delivered_created = True
        active = delivered
    else:
        active = root / "simulation.sqlite"
        _snapshot_copy(base_snapshot, active)

    archive_dir = root / ("raw-archive" if apply else "raw-archive-simulation")
    steps: List[Dict[str, Any]] = []
    steps.append(_step_result("repair_claim_states", step_repair_claim_states, active))
    steps.append(_step_result("backfill_valid_until", step_backfill_valid_until, active))
    steps.append(_step_result("collapse_duplicate_rows", step_collapse_duplicates,
                              active, archive_dir))
    steps.append(_step_result("maintain", step_maintain, active))
    # maintain can move memories out of recall (expired/cold); converge claims
    # once more so the after-state is a fixed point rather than a half-fix.
    steps.append(_step_result("repair_claim_states_final", step_repair_claim_states, active))
    # maintain expired/cold rows during this pass; its own FTS prune runs at the
    # start of a tick, so a second pass is what turns the after-state into a
    # fixed point (FTS rows == expected index set) instead of a half-tick.
    steps.append(_step_result("maintain_converge", step_maintain, active))
    steps.append(_step_result("identity_audit", step_identity_audit, active))
    steps.append(_step_result("commons_reconcile", step_commons_reconcile,
                              active, commons_archive))

    result["steps"] = steps
    result["step_errors"] = [item["name"] for item in steps if not item["ok"]]
    result["after"] = collect_metrics(active)
    result["delta"] = _delta(result["before"], result["after"])
    result["reclaimable_bytes"] = {
        "before": int(result["before"].get("reclaimable_bytes", 0) or 0),
        "after": int(result["after"].get("reclaimable_bytes", 0) or 0),
    }
    result["reclaimable_bytes"]["delta"] = (
        result["reclaimable_bytes"]["after"] - result["reclaimable_bytes"]["before"])

    # 5. Prove the input and the frozen snapshot were never written.
    source_after = file_fingerprint(source_path)
    frozen_after = file_fingerprint(frozen)
    source_unchanged = (source_after["sha256"] == source_before["sha256"]
                        and source_after["size"] == source_before["size"]
                        and source_after["mtime_ns"] == source_before["mtime_ns"])
    frozen_unchanged = (frozen_after["sha256"] == frozen_initial["sha256"]
                        and frozen_after["size"] == frozen_initial["size"]
                        and frozen_after["mtime_ns"] == frozen_initial["mtime_ns"])
    result["source"]["fingerprint_after"] = source_after
    result["source"]["unchanged"] = source_unchanged
    result["frozen"]["fingerprint_after"] = frozen_after
    result["frozen"]["unchanged"] = frozen_unchanged

    target_after: Optional[Dict[str, Any]] = None
    if target_path is not None:
        target_after = file_fingerprint(target_path)
    delivered_after = file_fingerprint(delivered) if (apply and delivered is not None) else None
    delivered_changed = False
    if apply and delivered_after is not None:
        reference = target_before or base_fingerprint
        delivered_changed = delivered_after["sha256"] != reference["sha256"]
    result["copies"] = {
        "delivered": str(delivered) if delivered is not None else "",
        "delivered_created": delivered_created,
        "delivered_fingerprint_after": delivered_after,
        "delivered_changed": delivered_changed,
        "simulation": str(active) if not apply else "",
        "simulation_removed": False,
        "target": str(target_path) if target_path is not None else "",
        "target_fingerprint_after": target_after,
        "target_unchanged": (
            target_after is not None and target_before is not None
            and target_after["sha256"] == target_before["sha256"]
            and target_after["mtime_ns"] == target_before["mtime_ns"]),
    }
    result["synthetic"] = synthetic_summary

    # 6. Any source mutation is a refusal condition, not a warning.
    if not source_unchanged:
        raise RehearsalError(
            "源账本在演练期间发生变化（sha256/mtime/size 不一致），拒绝继续: %s"
            % source_path)
    if not frozen_unchanged:
        raise RehearsalError("冻结快照在演练期间发生变化，拒绝继续: %s" % frozen)
    if not apply and target_path is not None and target_before is not None:
        if target_after is None or target_after["sha256"] != target_before["sha256"]:
            raise RehearsalError("dry-run 不得修改 --target 副本: %s" % target_path)

    # 7. Cleanup: dry-run never leaves a mutated artifact behind.
    if not apply:
        _remove_sqlite(active)
        shutil.rmtree(archive_dir, ignore_errors=True)
        if not retained_root and not keep:
            shutil.rmtree(root, ignore_errors=True)
    if not apply:
        result["copies"]["simulation_removed"] = not Path(active).exists()
    result["workdir_retained"] = root.exists()
    result["ok"] = (not result["step_errors"] and source_unchanged
                    and frozen_unchanged)
    result["elapsed_ms"] = round((time.time() - started) * 1000.0, 1)
    return result


# ---------------------------------------------------------------------------
# 人类摘要
# ---------------------------------------------------------------------------
def _fmt(value: Any) -> str:
    try:
        return "{:,}".format(int(value))
    except (TypeError, ValueError):
        return str(value)


def render(result: Mapping[str, Any]) -> str:
    lines: List[str] = []
    add = lines.append
    copies = result.get("copies", {}) or {}
    source = result.get("source", {}) or {}
    frozen = result.get("frozen", {}) or {}
    before = result.get("before", {}) or {}
    after = result.get("after", {}) or {}
    delta = result.get("delta", {}) or {}
    add("HippoMind 数据治理演练 · 冻结副本台账")
    add("=" * 78)
    add("")
    add("1. 输入与冻结")
    add("   模式                      %s" % ("合成代表性账本" if result.get("mode") == "synthetic"
                                           else "源账本冻结快照"))
    add("   源账本                    %s" % source.get("path", ""))
    add("   源账本不变                %s（sha256 %s -> %s）"
        % ("是" if source.get("unchanged") else "否",
           str((source.get("fingerprint_before") or {}).get("sha256", ""))[:16],
           str((source.get("fingerprint_after") or {}).get("sha256", ""))[:16]))
    add("   冻结快照                  %s" % frozen.get("path", ""))
    add("   冻结快照不变              %s" % ("是" if frozen.get("unchanged") else "否"))
    if result.get("dry_run"):
        add("   工作副本                  一次性 simulation（演练后已删除）")
    else:
        add("   工作副本                  %s（apply 交付物，已保留）" % copies.get("delivered", ""))
        add("   副本发生变化              %s" % ("是" if copies.get("delivered_changed") else "否"))
    if result.get("synthetic"):
        synth = result["synthetic"]
        add("   合成账本形状              %s 行 / 重复组 %s / 悬空 claim %s / 缺失窗口 %s"
            % (_fmt(synth.get("rows")), _fmt(synth.get("duplicate_groups")),
               _fmt(synth.get("dangling_claims")), _fmt(synth.get("missing_windows"))))
    add("")
    add("2. 治理步骤")
    for item in result.get("steps", []):
        mark = "OK  " if item.get("ok") else "ERR "
        add("   %s %-28s %8.1fms  %s" % (mark, item.get("name", ""),
                                        float(item.get("elapsed_ms", 0.0)),
                                        item.get("error", "")))
    add("")
    add("3. 治理指标（冻结副本上 before -> after）")
    labels = (
        ("dangling_claims", "悬空 claim（active 但记忆不可召回）"),
        ("missing_windows", "缺失失效窗口（superseded + 空 valid_until）"),
        ("missing_memory_windows", "记忆缺失失效窗口"),
        ("duplicate_groups", "重复 raw 组（内容/来源/会话/角色相同）"),
        ("duplicate_redundant", "重复 raw 冗余行"),
        ("cold_rows", "cold 归档行"),
        ("expired_rows", "expired 行"),
        ("fts_unindexed", "FTS 应索引未索引"),
        ("reclaimable_bytes", "可回收字节（freelist）"),
        ("db_bytes", "数据库文件字节"),
    )
    for key, label in labels:
        add("   %-46s %12s -> %-12s (%+d)"
            % (label, _fmt(before.get(key)), _fmt(after.get(key)),
               int(delta.get(key, 0) or 0)))
    add("")
    add("4. 结论")
    add("   演练结果                  %s" % ("通过" if result.get("ok") else "存在失败步骤"))
    if result.get("dry_run"):
        add("   DRY-RUN：治理步骤在一次性副本上执行并测量，源账本与冻结快照均未修改；")
        add("            加 --apply 才会把收敛结果保留在冻结副本上。")
    else:
        add("   APPLY：治理动作只落在冻结副本上；源账本 sha256/mtime/size 未变化。")
    add("   总耗时                    %.1fms" % float(result.get("elapsed_ms", 0.0)))
    return chr(10).join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="在冻结副本上演练数据治理（默认 dry-run；源账本只读且前后校验）")
    parser.add_argument("--source", default="",
                        help="源账本路径；缺省时生成 >=5k 行合成代表性账本")
    parser.add_argument("--target", default="",
                        help="已有冻结副本路径（apply 的交付物；不能等于 --source）")
    parser.add_argument("--workdir", default="",
                        help="保留冻结快照/副本的目录（缺省用临时目录）")
    parser.add_argument("--apply", action="store_true",
                        help="把治理结果保留在冻结副本上（源账本永不写入）")
    parser.add_argument("--keep", action="store_true",
                        help="dry-run 也保留冻结快照目录（不保留 simulation）")
    parser.add_argument("--rows", type=int, default=DEFAULT_SYNTHETIC_ROWS,
                        help="合成账本行数（默认 %d；仅回归时调小）" % DEFAULT_SYNTHETIC_ROWS)
    parser.add_argument("--commons-archive", default="",
                        help="可选：Agent-Chat-Archive 路径，做只读 commons 对账")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)
    try:
        result = rehearse(source=args.source or None,
                          target=args.target or None,
                          workdir=args.workdir or None,
                          apply=bool(args.apply),
                          keep=bool(args.keep),
                          rows=int(args.rows),
                          commons_archive=args.commons_archive or None)
    except RehearsalError as exc:
        print("治理演练拒绝执行: %s" % exc, file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print("治理演练失败: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
