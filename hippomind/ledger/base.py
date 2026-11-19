"""Connection management, transactions, bootstrap and schema forwarders.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from collections import deque
from .. import schema as _schema
from ._helpers import (
    LedgerError,
    SCHEMA_VERSION,
    default_ledger_path,
)

class BaseMixin:
    """Connection management, transactions, bootstrap and schema forwarders."""

    def __init__(self, path: Optional[os.PathLike] = None, *, read_only: bool = False):
        self.read_only = bool(read_only)
        self.path = Path(path or default_ledger_path()).expanduser()
        if self.path.exists() and (not self.path.is_file() or self.path.is_symlink()):
            raise LedgerError(f"账本路径必须是普通文件（不能是目录或符号链接）: {self.path}")
        if not self.path.name or self.path.name in {".", ".."}:
            raise LedgerError("账本路径无效")
        if self.read_only:
            # 只读账本（LAN 镜像/巡检副本）打开时绝不能创建、迁移或修整任何文件。
            if not self.path.exists():
                raise LedgerError(f"只读模式要求账本文件已存在: {self.path}")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local_lock = threading.RLock()
        self._tls = threading.local()
        self._generation = 0
        # 跨线程 reader 注册表：线程本地缓存是快路径，注册表只服务于 close()。
        # ThreadingHTTPServer 每个连接一个短生命周期线程，只靠 thread-local
        # 会在长跑 daemon 里留下一条条永不关闭的连接；close() 用 ident→conn
        # 一次性回收（reader 连接以 check_same_thread=False 创建，允许代理关闭）。
        self._readers: Dict[int, sqlite3.Connection] = {}
        self._reader_lock = threading.Lock()
        self._reader_epoch = 0
        # search 结果 LRU：键含 generation（任何写事务自增即整体失效）。
        # TTL 只是防御外部进程写者的兜底；深拷贝隔离调用方突变。
        self._search_cache: Dict[Tuple[Any, ...], Tuple[float, List[Dict[str, Any]]]] = {}
        self._search_cache_ttl = 5.0
        # 慢查询哨兵（对齐 mem0 PERFORMANCE_SLOW_QUERY_THRESHOLD 语义）：
        # 最近 N 次 search 延迟环 + 超 2s 计数，经 stats() 暴露 p95。
        self._search_durations: "deque[float]" = deque(maxlen=256)
        self._slow_search_threshold = 2.0
        self._slow_search_count = 0
        self._search_lock = threading.Lock()
        if self.read_only:
            self._validate_schema_read_only()
        else:
            self._bootstrap()

    @property
    def generation(self) -> int:
        """Monotonic in-process write generation for short-lived query caches."""
        return self._generation

    def _connect(self, write: bool = False, *,
                 check_same_thread: bool = True) -> sqlite3.Connection:
        """Open one connection; read-only ledgers fail every write path here.

        A read-only ledger opens the file through a mode=ro URI and enables
        PRAGMA query_only.  Both layers matter: the URI is enforced by the
        SQLite VFS (the file cannot be modified even by code that never goes
        through this class), and query_only stops an accidental write statement
        before it can acquire a write lock.  No journal-mode or other durable
        PRAGMA is executed in this mode.
        """
        if write and self.read_only:
            raise LedgerError("只读账本拒绝写连接")
        if self.read_only:
            uri = "file:" + urllib.parse.quote(
                str(self.path.resolve()), safe="/") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=15.0,
                                   isolation_level=None,
                                   check_same_thread=check_same_thread)
        else:
            conn = sqlite3.connect(str(self.path), timeout=15.0,
                                   isolation_level=None,
                                   check_same_thread=check_same_thread)
        conn.row_factory = sqlite3.Row
        try:
            # 均为连接级设置：读连接不需要 WAL/同步/损失性 PRAGMA。
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            try:
                conn.execute("PRAGMA trusted_schema=OFF")
            except sqlite3.DatabaseError:
                pass
            # 长跑 daemon 的读路径调优：更大的页缓存减少重复磁盘读，mmap 让
            # OS 管理热页，临时表驻留内存。均为连接级参数，不影响持久性语义。
            try:
                conn.execute("PRAGMA cache_size=-16000")     # 16 MiB 页缓存
                conn.execute("PRAGMA mmap_size=134217728")   # 128 MiB 内存映射
                conn.execute("PRAGMA temp_store=MEMORY")
            except sqlite3.DatabaseError:
                pass
            if self.read_only:
                conn.execute("PRAGMA query_only=ON")
            elif write:
                conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            conn.close()
            raise
        return conn

    def _reader(self) -> sqlite3.Connection:
        """Thread-local cached read connection.

        cProfile on the recall path showed ~15% of wall time spent in
        connect+PRAGMA+close churn (one fresh connection per query). A
        per-thread cached reader removes that overhead. Safety notes:
        - autocommit mode means every statement gets a fresh WAL snapshot,
          so long-lived readers never see stale data after other writers
          commit;
        - ``threading.local`` guarantees the connection is only ever used
          by its creating thread (sqlite3 check_same_thread stays True);
        - write paths still use dedicated short-lived connections via
          ``_transaction``/``_bootstrap``.
        """
        conn = getattr(self._tls, "reader", None)
        epoch = getattr(self._tls, "reader_epoch", -1)
        if conn is not None and epoch == self._reader_epoch:
            return conn
        # close() 之后（epoch 变化）或首次读取：建一条新连接并登记。
        # 读到一半被 close() 抢走连接属于进程停机场景，下一次读取会重连。
        self._prune_readers()
        conn = self._connect(check_same_thread=False)
        ident = threading.get_ident()
        with self._reader_lock:
            epoch = self._reader_epoch
            previous = self._readers.get(ident)
            self._readers[ident] = conn
        self._tls.reader = conn
        self._tls.reader_epoch = epoch
        if previous is not None and previous is not conn:
            try:
                previous.close()
            except sqlite3.Error:
                pass
        return conn

    @staticmethod
    def _copy_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """检索行的定向拷贝：比 copy.deepcopy 快数倍（无递归/memo 开销），
        同时保证调用方后续修改不会污染查询缓存（前景轮九性能优化）。"""
        out: List[Dict[str, Any]] = []
        for row in rows:
            cloned = dict(row)
            for key in ("evidence", "tags"):
                value = cloned.get(key)
                if isinstance(value, list):
                    cloned[key] = list(value)
            value = cloned.get("score_parts")
            if isinstance(value, dict):
                cloned["score_parts"] = dict(value)
            value = cloned.get("metadata")
            if isinstance(value, dict):
                meta_copy = dict(value)
                ctx = meta_copy.get("context")
                if isinstance(ctx, dict):
                    meta_copy["context"] = {
                        ck: list(cv) if isinstance(cv, list) else cv
                        for ck, cv in ctx.items()
                    }
                cloned["metadata"] = meta_copy
            out.append(cloned)
        return out

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """Read-only connection for every read path.

        Historically this helper opened and closed a fresh connection per
        call to guarantee deterministic cleanup even when daemon error
        handlers keep exception frames (and tracebacks) alive. The cached
        thread-local reader keeps that determinism — the connection is not
        tied to any with-block lifetime — while eliminating per-query
        connect/close cost. Use :meth:`close` to release it explicitly.
        """
        yield self._reader()

    def close(self) -> None:
        """Close every thread's cached read connection (idempotent).

        Thread-local caching means a reader connection is created by the thread
        that first reads.  Short-lived request threads therefore used to leave
        one open connection each until process exit.  The registry lets close()
        reclaim all of them; bumping the epoch tells any still-running thread to
        reconnect on its next read, so a closed ledger is usable again.
        """
        with self._reader_lock:
            self._reader_epoch += 1
            readers = list(self._readers.values())
            self._readers.clear()
        self._tls.reader = None
        self._tls.reader_epoch = self._reader_epoch
        for conn in readers:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _prune_readers(self) -> None:
        """Drop reader connections whose owning thread has already exited."""
        active = {thread.ident for thread in threading.enumerate()}
        stale: List[sqlite3.Connection] = []
        with self._reader_lock:
            for ident in [name for name in self._readers if name not in active]:
                stale.append(self._readers.pop(ident))
        for conn in stale:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def maintenance_checkpoint(self) -> Dict[str, Any]:
        """Bounded WAL housekeeping for a long-running daemon.

        PASSIVE auto-checkpoints never shrink the WAL file and can be starved
        indefinitely by overlapping readers; a TRUNCATE checkpoint at a quiet
        batch boundary is the only mode that resets the sidecar to 0 bytes.
        ``PRAGMA optimize`` refreshes query-planner statistics on the same
        low-frequency schedule. Both tolerate busy readers: TRUNCATE simply
        reports busy=1 and the next cycle retries.
        """
        if self.read_only:
            # A read-only connection cannot checkpoint the WAL or optimize the
            # schema. The daemon shutdown path calls this unconditionally, so
            # return the same shape as an explicit no-op instead of an error.
            return {"wal_truncated": False, "wal_busy": False, "wal_pages": 0,
                    "checkpointed_pages": 0, "optimize": "skipped",
                    "read_only": True}
        summary = {"wal_truncated": False, "wal_busy": False, "wal_pages": 0,
                   "checkpointed_pages": 0, "optimize": "skipped"}
        try:
            with self._reading() as conn:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row is not None:
                    busy, log_pages, checkpointed = int(row[0]), int(row[1]), int(row[2])
                    summary.update({"wal_busy": bool(busy), "wal_pages": log_pages,
                                    "checkpointed_pages": checkpointed,
                                    "wal_truncated": not busy})
                try:
                    conn.execute("PRAGMA analysis_limit=400")
                    conn.execute("PRAGMA optimize")
                    summary["optimize"] = "ok"
                except sqlite3.DatabaseError:
                    summary["optimize"] = "error"
        except sqlite3.Error as exc:
            summary["optimize"] = f"error: {type(exc).__name__}"
        return summary

    def _validate_schema_read_only(self) -> None:
        """Validate an existing ledger without migrating or touching it.

        A read-only copy can only be served when a writable process already
        brought it to the current schema version.  Refusing older (and newer)
        versions turns a missing column into an explicit startup error, and
        guarantees that constructing a read-only ledger never writes.
        """
        try:
            conn = self._connect()
        except sqlite3.DatabaseError as exc:
            raise LedgerError(f"只读模式无法打开账本（文件可能不是 SQLite 数据库）: {exc}") from exc
        try:
            try:
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            except sqlite3.DatabaseError as exc:
                raise LedgerError(
                    f"只读模式无法读取账本 schema（文件可能不是 HippoMind 账本）: {exc}") from exc
            try:
                version = int(row[0]) if row else 0
            except (TypeError, ValueError):
                raise LedgerError("只读账本 schema_meta 的版本值无效")
            if version != SCHEMA_VERSION:
                raise LedgerError(
                    "只读模式要求 schema 版本 %d（当前 %d）：只读模式不执行迁移，"
                    "请先用可写进程升级账本" % (SCHEMA_VERSION, version))
        finally:
            conn.close()

    def _bootstrap(self) -> None:
        with self._local_lock:
            conn = self._connect(write=True)
            try:
                # WAL enables readers and independent agent writers to coexist.
                journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(journal).lower() != "wal":
                    raise LedgerError(f"SQLite 未能启用 WAL（当前: {journal}）")
                conn.execute("CREATE TABLE IF NOT EXISTS schema_meta "
                             "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                version = int(row[0]) if row else 0
                if version > SCHEMA_VERSION:
                    raise LedgerError("账本 schema 比当前 HippoMind 更新，拒绝降级写入")
                if version < 1:
                    self._migrate_v1(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(1),))
                    version = 1
                if version < 2:
                    self._migrate_v2(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(2),))
                    version = 2
                if version < 3:
                    self._migrate_v3(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(3),))
                    version = 3
                if version < 4:
                    self._migrate_v4(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(4),))
                    version = 4
                if version < 5:
                    self._migrate_v5(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(5),))
                    version = 5
                if version < 6:
                    self._migrate_v6(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(6),))
                    version = 6
                if version < 7:
                    self._migrate_v7(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(7),))
                    version = 7
                if version < 8:
                    self._migrate_v8(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(8),))
                    version = 8
                if version < 9:
                    self._migrate_v9(conn)
                    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                                 (str(9),))
                    version = 9
                if version != SCHEMA_VERSION:
                    raise LedgerError(f"不支持的账本 schema 版本: {version}")
                conn.commit()
            finally:
                conn.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _transaction(self, callback, *, bump: bool = True):
        if self.read_only:
            # Every durable write path (ingest/feedback/maintain/purge/
            # consolidate/credentials...) funnels through _transaction, so this
            # is the single guard that makes a read-only ledger truly read-only.
            raise LedgerError("只读账本拒绝写事务")
        last_error: Optional[Exception] = None
        for attempt in range(7):
            with self._local_lock:
                conn = self._connect(write=True)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    value = callback(conn)
                    conn.commit()
                    if bump:
                        self._generation += 1
                    return value
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    last_error = exc
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            time.sleep(0.015 * (2 ** attempt))
        raise LedgerError("账本忙，稍后重试") from last_error

    # ------------------------------------------------------------------
    # Schema (see hippomind/schema.py)
    # ------------------------------------------------------------------
    # The migration chain used to live here, in a 3,662-line class.  It now
    # lives in a dedicated module so the schema can be reviewed and diffed on
    # its own.  These aliases keep the historical ``SQLiteLedger._migrate_vN``
    # call sites (including tests) working unchanged.
    _migrate_v1 = staticmethod(_schema._migrate_v1)

    _migrate_v2 = staticmethod(_schema._migrate_v2)

    _migrate_v3 = staticmethod(_schema._migrate_v3)

    _migrate_v4 = staticmethod(_schema._migrate_v4)

    _migrate_v5 = staticmethod(_schema._migrate_v5)

    _migrate_v6 = staticmethod(_schema._migrate_v6)

    _migrate_v7 = staticmethod(_schema._migrate_v7)

    _migrate_v8 = staticmethod(_schema._migrate_v8)

    _migrate_v9 = staticmethod(_schema._migrate_v9)

    _rebuild_fts = staticmethod(_schema._rebuild_fts)
