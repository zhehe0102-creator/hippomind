"""Local-only HippoMind Memory Hub daemon and HTTP API.

The service deliberately binds to loopback, enforces small request/response
budgets, and uses a 0600 local token for every data endpoint.  ``/health`` is
the sole unauthenticated endpoint so launchers can safely detect an existing
daemon without exposing memory contents.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import mimetypes
from typing import Any, Dict, Mapping, Optional

# 空闲睡眠巩固：连续无外部活动（请求 + watcher 增量）达到该秒数后触发。
SLEEP_IDLE_SECONDS = 600.0
from urllib.parse import parse_qs, unquote, urlsplit

from .access import AccessDenied, AccessPolicy
from .context_pack import DEFAULT_MAX_CHARS, DEFAULT_MAX_TOKENS, ContextPackBuilder, ContextPackOptions
from .fact_cards import fact_cards_enabled  # noqa: E402
from .injection import InjectionState
from . import injection_trace  # noqa: E402
from . import injection_ab  # noqa: E402
from .cursor_capture import collect as collect_cursor  # noqa: E402
from .distill import DistillEngine  # noqa: E402
from .gemini_capture import GeminiSummaryScanner  # noqa: E402
from .pi_capture import PiSessionScanner  # noqa: E402
from .session_digest import SessionDigester  # noqa: E402
from .gui_api import GuiApi, GuiApiError  # noqa: E402  (module keeps its legacy import block)
from .transcripts import TranscriptScanner  # noqa: E402
from .ledger import LedgerError, SQLiteLedger, default_ledger_path, utc_now
from .provider_manifest import DEFAULT_DISCOVERY_INTERVAL_SECONDS, AgentDiscovery
from .watcher import ArchiveWatcher, default_dsh_sessions_root


API_VERSION = "v1"
#: Reported over HTTP and in ``hippomind version``. Sourced from the package
#: so a release only has to bump one place.
from . import __version__ as _PACKAGE_VERSION  # noqa: E402  (kept near its use)
SERVER_VERSION = _PACKAGE_VERSION
MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 512_000
MAX_CONCURRENT_REQUESTS = 16
#: Background metrics trend log rotation bound (metrics.jsonl + one .1).
METRICS_LOG_MAX_BYTES = 8 * 1024 * 1024


def render_prometheus(stats: Mapping[str, Any], uptime_s: float) -> str:
    """把 stats 摘要渲染成 Prometheus 文本格式（v0.0.4）。"""
    lines: list = []

    def emit(name: str, mtype: str, help_text: str, value) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        lines.append(f"{name} {value}")

    ledger_stats = stats.get("ledger") if isinstance(stats.get("ledger"), Mapping) else stats

    def count(key: str) -> int:
        value = ledger_stats.get(key, 0)
        if isinstance(value, Mapping):
            return int(value.get("total", 0))
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    emit("hippomind_memories_total", "gauge", "Total memory projections",
         count("memories"))
    emit("hippomind_events_total", "gauge", "Ledger event count",
         count("events"))
    emit("hippomind_claims_active", "gauge", "Active slot-value claims",
         count("active_claims"))
    emit("hippomind_claims_conflict", "gauge", "Slot-value claims awaiting resolution",
         count("conflict_claims"))
    emit("hippomind_memories_pinned", "gauge", "Pinned memory projections",
         count("pinned"))
    wal = stats.get("wal_bytes", 0)
    emit("hippomind_wal_bytes", "gauge", "WAL file size in bytes", wal)
    cache = stats.get("context_cache") or (
        ledger_stats.get("context_cache") if isinstance(ledger_stats, Mapping) else None) or {}
    if isinstance(cache, Mapping):
        hits = cache.get("hits", 0)
        misses = cache.get("misses", 0)
        emit("hippomind_context_cache_hits_total", "counter",
             "Context pack cache hits", hits)
        emit("hippomind_context_cache_misses_total", "counter",
             "Context pack cache misses", misses)
    health = stats.get("health", {})
    if isinstance(health, Mapping):
        emit("hippomind_sleep_cycles_total", "counter",
             "Sleep consolidation cycles", health.get("sleep_cycles", 0))
        emit("hippomind_sleep_errors_total", "counter",
             "Sleep consolidation errors", health.get("sleep_errors", 0))
        # Retention convergence: these are the numbers that would have made the
        # 4.48GB growth visible while it was happening.
        emit("hippomind_raw_purged_total", "counter",
             "Raw evidence rows physically deleted after archival",
             health.get("raw_purged_total", 0))
        emit("hippomind_bytes_reclaimed_total", "counter",
             "File bytes returned to the OS by automatic shrinking",
             health.get("bytes_reclaimed_total", 0))
        storage = health.get("last_storage")
        if isinstance(storage, Mapping):
            emit("hippomind_db_bytes", "gauge", "Ledger file size in bytes",
                 storage.get("db_bytes", 0))
            emit("hippomind_freelist_pages", "gauge",
                 "Pages on the freelist awaiting reclaim", storage.get("freelist", 0))
            emit("hippomind_reclaimable_bytes", "gauge",
                 "Bytes recoverable by a shrink pass",
                 storage.get("reclaimable_bytes", 0))
    emit("hippomind_uptime_seconds", "gauge", "Daemon uptime in seconds",
         round(float(uptime_s), 3))
    return "\n".join(lines) + "\n"


def ledger_identity(path: os.PathLike | str) -> str:
    """Stable non-reversible identifier used only for daemon ownership checks."""
    try:
        value = str(Path(path).expanduser().resolve())
    except OSError:
        value = str(Path(path).expanduser())
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class RuntimePaths:
    ledger_path: Path
    state_path: Path
    token_path: Path

    @classmethod
    def for_ledger(cls, ledger_path: Optional[os.PathLike] = None,
                   runtime_dir: Optional[os.PathLike] = None) -> "RuntimePaths":
        ledger = Path(ledger_path or default_ledger_path()).expanduser()
        if ledger.exists() and (not ledger.is_file() or ledger.is_symlink()):
            raise LedgerError(f"账本路径必须是普通文件（不能是目录或符号链接）: {ledger}")
        directory = Path(runtime_dir).expanduser() if runtime_dir else ledger.parent
        if directory.exists() and (not directory.is_dir() or directory.is_symlink()):
            raise LedgerError(f"daemon runtime 目录无效: {directory}")
        return cls(ledger, directory / "hippomindd.json", directory / "hippomindd.token")


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_runtime_state(paths: RuntimePaths) -> Dict[str, Any]:
    try:
        with paths.state_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_local_token(paths: RuntimePaths) -> str:
    try:
        stat = paths.token_path.stat()
        if paths.token_path.is_symlink() or not paths.token_path.is_file() or stat.st_size > 1024:
            return ""
        if os.name != "nt" and stat.st_mode & 0o077:
            return ""
        return paths.token_path.read_text("utf-8").strip()
    except OSError:
        return ""


class MemoryHubService:
    def __init__(self, paths: RuntimePaths, *, archive_root: Optional[os.PathLike] = None,
                 watch: bool = True, watch_interval: float = 30.0, require_auth: bool = True,
                 read_only: bool = False,
                 discovery_interval: float = DEFAULT_DISCOVERY_INTERVAL_SECONDS):
        self.paths = paths
        self._started_at = time.time()
        self._metrics_log_path = paths.state_path.parent / "metrics.jsonl"
        self._last_metrics_log = 0.0
        # Read-only must be known before the ledger is opened: a mirror has to
        # serve the existing file as-is and must never bootstrap/migrate it.
        self.read_only = bool(read_only)
        self.ledger = SQLiteLedger(paths.ledger_path, read_only=self.read_only)
        # Optional ledger-native semantic space: only used when a fitted model
        # exists.  It improves pack recall (~+2 points on the labelled set) at a
        # modest latency cost, and silently falls back when absent.
        try:
            from .semantic_space import load_embedder
            _embedder = load_embedder()
        except Exception:
            _embedder = None
        # Optional neural vector index (ONNX sentence embeddings).  It feeds the
        # **candidate generation** stage as a third recall channel (RRF), which
        # is where it measurably helps: on the labelled paraphrase set recall@8
        # went 0.207 -> 0.453 while the keyword set stayed at 1.000.  Loading is
        # lazy and failure-tolerant: a missing model, missing onnxruntime or a
        # corrupt index simply leaves the lexical path untouched.
        self._vector_index = None
        self._vector_index_ready = False
        self.contexts = ContextPackBuilder(self.ledger, embedder=_embedder,
                                           vector_index=None)
        # Automatic injection is deliberately independent from collection.
        # A disabled Agent keeps contributing sanitized evidence, but receives
        # an empty Context Pack until it is enabled again.
        self.injection = InjectionState(paths.state_path.parent)
        self.watcher = ArchiveWatcher(self.ledger, archive_root=archive_root,
                                      poll_interval=watch_interval,
                                      dsh_sessions_root=default_dsh_sessions_root())
        # Native transcript capture + automatic distillation (v2).  Both share
        # the daemon's ledger handle and are stopped by _stop_event.
        self.transcripts = TranscriptScanner(self.ledger)
        self.gemini_capture = GeminiSummaryScanner(self.ledger)
        self.pi_capture = PiSessionScanner(self.ledger)
        self.digester = SessionDigester(self.ledger)
        self.distiller = DistillEngine(self.ledger)
        self.watch_enabled = bool(watch)
        self.require_auth = bool(require_auth)
        # Discovery walks the home directory, so it runs at most once per
        # interval; between scans the persisted report is reused.
        self.discovery = AgentDiscovery(paths.state_path.parent / "agent-discovery.json",
                                        interval=discovery_interval)
        self._discovery_thread: Optional[threading.Thread] = None
        self._sleep_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Graceful shutdown is idempotent: exactly one caller performs the
        # join/checkpoint/close sequence, concurrent callers wait for it.
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._shutdown_complete = threading.Event()
        self._server_stop_thread: Optional[threading.Thread] = None
        # 空闲睡眠巩固：外部活动（HTTP 请求）会推迟触发；watcher 增量同样算活动。
        self._last_activity_monotonic = time.monotonic()
        # 健康自监控（迭代55）：后台周期任务的成功/失败计数进 /stats，
        # 让「daemon 看起来活着但巩固一直在静默失败」这类问题可观测。
        self.health = {"sleep_cycles": 0, "sleep_errors": 0,
                       "last_sleep_report_at": "",
                       # Observability: which mode is served and which background
                       # loops actually run ("running" vs "skipped:read_only").
                       "read_only": bool(self.read_only), "background": {},
                       # Convergence counters: a long-running hub must be able to
                       # show that it is shrinking, not just "healthy".
                       "raw_purged_total": 0, "bytes_reclaimed_total": 0,
                       "last_storage": {},
                       # Automatic capture/distillation telemetry (v2).
                       "capture": {}, "distill": {}}
        self._last_sleep_run: Optional[float] = None
        self.last_sleep_report: Optional[Dict[str, Any]] = None
        # Latest controlled auto-install outcome (per detected agent); only
        # written by the discovery loop, read by status endpoints.
        self.auto_install: Dict[str, Any] = {}
        self.token = self._load_or_create_token() if self.require_auth else ""
        # Read-only admin console API (/api/v1); it shares the hub's single
        # ledger handle and never opens a second writer.
        self.gui = GuiApi(self)
        self.started_at = utc_now()
        self._server: Optional[ThreadingHTTPServer] = None
        self._watch_thread: Optional[threading.Thread] = None

    def _load_or_create_token(self) -> str:
        existing = read_local_token(self.paths)
        if existing:
            try:
                if self.paths.token_path.is_symlink() or not self.paths.token_path.is_file():
                    raise LedgerError("daemon token 必须是普通文件")
                os.chmod(self.paths.token_path, 0o600)
            except OSError as exc:
                raise LedgerError("无法保护 daemon token 文件") from exc
            return existing
        token = secrets.token_urlsafe(32)
        self.paths.token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="." + self.paths.token_path.name + ".",
                                        suffix=".tmp", dir=str(self.paths.token_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.paths.token_path)
            try:
                os.chmod(self.paths.token_path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return token

    def access_policy(self, supplied: str) -> Optional[AccessPolicy]:
        policy = self._resolve_policy(supplied)
        if getattr(self, "read_only", False):
            # Read-only mode (LAN mirror / inspection copy) downgrades every
            # credential's *data plane*: no ingest, feedback, sync or reload.
            if policy is None:
                return None
            if policy.admin:
                # ...but the local operator who started this process must still
                # be able to stop it.  Dropping shutdown together with the write
                # verbs created a trap: the read-only daemon could never be
                # stopped by `hippomind stop`, and `start --restart` onto it
                # deadlocked in the 5-second stop wait ("拒绝并发启动第二个
                # 服务").  Exactly one permission is added to the downgraded
                # view -- admin stays False so /admin/reload remains refused and
                # the data plane (scopes, raw-chat export) is untouched.
                downgraded = AccessPolicy.read_only_shared()
                return AccessPolicy(downgraded.agent_id, downgraded.scopes,
                                    downgraded.permissions + ("shutdown",), False)
            return AccessPolicy.read_only_shared()
        return policy

    def _resolve_policy(self, supplied: str) -> Optional[AccessPolicy]:
        if not self.require_auth:
            return AccessPolicy.administrator()
        if supplied and self.token and hmac.compare_digest(supplied, self.token):
            return AccessPolicy.administrator()
        credential = self.ledger.resolve_agent_credential(supplied) if supplied else None
        return AccessPolicy.from_credential(credential) if credential is not None else None

    def authenticated(self, supplied: str) -> bool:
        return self.access_policy(supplied) is not None

    def _set_background_state(self, name: str, state: str) -> None:
        """Record which background loops this process actually runs.

        Without this, a read-only mirror and a daemon whose watcher silently
        died look identical from /stats: both just have no loop. Exposed through
        the health record -> /stats and /metrics.
        """
        background = self.health.get("background")
        if not isinstance(background, dict):
            background = {}
            self.health["background"] = background
        background[name] = state

    def start_watcher(self) -> None:
        if self.read_only:
            self._set_background_state("watcher", "skipped:read_only")
            return
        if not self.watch_enabled or self._watch_thread is not None:
            self._set_background_state(
                "watcher",
                "skipped:watch_disabled" if not self.watch_enabled else "running")
            return
        self._set_background_state("watcher", "running")
        self._watch_thread = threading.Thread(target=self.watcher.run, name="hippomind-watcher", daemon=True)
        self._watch_thread.start()

    def start_discovery_loop(self) -> None:
        """Background low-frequency agent discovery; cached between scans.

        Read-only mode never runs it: discovery is the only caller of the
        controlled auto-install, and installing integrations rewrites Agent
        configuration files under HOME.
        """
        if self.read_only:
            self._set_background_state("discovery", "skipped:read_only")
            return
        if self._discovery_thread is not None:
            self._set_background_state("discovery", "running")
            return
        self._set_background_state("discovery", "running")

        def loop() -> None:
            check_interval = min(600.0, max(30.0, self.discovery.interval / 8.0))
            last_scanned = ""
            while not self._stop_event.is_set():
                try:
                    report = self.discovery.maybe_discover()
                    scanned_at = str(report.get("scanned_at", "")) if isinstance(report, dict) else ""
                    if scanned_at and scanned_at != last_scanned:
                        # A fresh scan (or the first cached one after startup)
                        # is the only trigger for the controlled auto-install.
                        last_scanned = scanned_at
                        # 尊重 HIPPOMIND_HOME：测试与沙箱用它把接入安装
                        # 限制在临时目录里，避免写进用户真实的 ~/.claude 等
                        # 配置（本机实测：跑测试会污染 6 个真实配置文件）。
                        self._auto_install_detected(
                            report, home=os.environ.get("HIPPOMIND_HOME") or None)
                except Exception:
                    # Discovery must never take the hub down; the cached
                    # report stays valid until the next successful scan.
                    pass
                self._stop_event.wait(check_interval)

        self._discovery_thread = threading.Thread(target=loop, name="hippomind-discovery", daemon=True)
        self._discovery_thread.start()

    def append_metrics_trend(self) -> Optional[Dict[str, Any]]:
        """把关键指标追加到 runtime_dir/metrics.jsonl（每小时至多一行）。"""
        if self.read_only:
            # A read-only mirror must not append to its runtime trend log: the
            # mirror is not the writer of record for this ledger.
            return None
        if self._metrics_log_path is None:
            return None
        now = time.time()
        if now - self._last_metrics_log < 3600:
            return None
        try:
            stats = self.stats(AccessPolicy.administrator())
            ledger_stats = stats.get("ledger") if isinstance(stats.get("ledger"), Mapping) else {}
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "memories": int(ledger_stats.get("memories", 0)),
                "events": int(ledger_stats.get("events", 0)),
                "active_claims": int(ledger_stats.get("active_claims", 0)),
                "wal_bytes": int(stats.get("wal_bytes", 0)),
                "uptime_s": round(now - self._started_at, 1),
            }
            self._metrics_log_path.parent.mkdir(parents=True, exist_ok=True)
            self._append_private_line(self._metrics_log_path,
                                      json.dumps(entry, ensure_ascii=False) + "\n")
            self._last_metrics_log = now
            return entry
        except Exception:
            return None

    @staticmethod
    def _append_private_line(path: Path, line: str) -> None:
        """Append one 0600 line, rotating at METRICS_LOG_MAX_BYTES.

        The runtime directory is shared by the console, so these files must not
        be world-readable; and an hourly append that never rotates grows for the
        lifetime of the daemon. Rotation keeps one bounded .1 generation.
        """
        try:
            if path.exists() and path.stat().st_size >= METRICS_LOG_MAX_BYTES:
                os.replace(str(path), str(path) + ".1")
        except OSError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            payload = line.encode("utf-8")
            while payload:
                written = os.write(fd, payload)
                payload = payload[written:]
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def start_warmup(self) -> None:
        """Warm SQLite page cache and the pack builder before the first hook.

        A cold daemon pays ~650ms for the first context-pack (SQLite page cache
        plus FTS load), which is right at DSH's 700ms injection timeout.  A
        background warmup makes the first real injection fast instead of
        making the user's first prompt pay for it.
        """
        if self.read_only or self._stop_event.is_set():
            self._set_background_state(
                "warmup", "skipped:read_only" if self.read_only else "skipped:stopped")
            return
        self._set_background_state("warmup", "running")

        def warm() -> None:
            try:
                self.ledger.search("warmup", scope="shared", limit=8,
                                   include_raw_chat=False, require_evidence=True)
                self.contexts.build("warmup", ContextPackOptions(max_items=1, min_score=0.2))
            except Exception:
                pass

        threading.Thread(target=warm, name="hippomind-warmup", daemon=True).start()

    def start_vector_index_loop(self, *, tick_seconds: float = 900.0) -> None:
        """Build/refresh the neural vector index off the request path.

        Building from scratch costs ~90-170s on the production ledger, so it must
        never happen inside a request.  The first build happens here, in the
        background; afterwards each tick does an incremental sync (sub-second)
        so newly captured memories become semantically reachable.
        """
        if self.read_only or not self.watch_enabled or self._stop_event.is_set():
            self._set_background_state(
                "vector_index",
                "skipped:read_only" if self.read_only else
                ("skipped:watch_disabled" if not self.watch_enabled else "skipped:stopped"))
            return
        self._set_background_state("vector_index", "running")

        def loop() -> None:
            while not self._stop_event.is_set():
                try:
                    from .semantic_onnx import load_onnx_embedder
                    from .vector_index import load_granular_index
                    embedder = load_onnx_embedder()
                    if embedder is None:
                        self.health["vector_index"] = {"state": "unavailable"}
                    else:
                        # claim 级优先：真正的瓶颈是索引粒度。一条提炼记忆把
                        # 5-10 个互不相关的事实塞进 ~550 字符正文，整体
                        # embedding 被稀释成一个"什么都像一点"的中心点。按
                        # claim 建索引后 semantic recall 0.200 -> 0.467（纯索引
                        # 口径）、pipeline 0.067 -> 0.733，keyword 集不退化。
                        # 这里必须 sync=True：daemon 负责构建与增量，注入路径
                        # （auto_context）只读盘——全量构建 56-86s，而注入预算
                        # 只有 700ms，在请求里重建等于把冷启动变成超时。
                        index = load_granular_index(self.ledger, embedder, sync=True)
                        if index is None:
                            self.health["vector_index"] = {"state": "unavailable"}
                        else:
                            self._vector_index = index
                            self._vector_index_ready = True
                            # Swap the live builder onto the index so new packs
                            # use semantic recall without a restart.
                            self.contexts.vector_index = index
                            stats = dict(index.stats(), state="ready")
                            stats["granularity"] = str(
                                getattr(index, "granularity", "claim"))
                            self.health["vector_index"] = stats
                except Exception as exc:  # never take the hub down
                    self.health["vector_index"] = {
                        "state": "error",
                        "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
                self._stop_event.wait(tick_seconds)

        threading.Thread(target=loop, name="hippomind-vector-index",
                         daemon=True).start()

    def start_entity_index_loop(self, *, tick_seconds: float = 900.0) -> None:
        """把实体倒排挂到 live builder 上（默认关闭，仅显式开启时）。

        与 start_vector_index_loop 的区别：实体索引**不需要**独立的同步循环。
        EntityIndex.build() 实测暖态 30ms（生产 2,815 条 claim / 2,490 个实体），
        而且 load_entity_index() 已按 (账本, generation) 做进程内缓存——写事务
        一提交就自动重建。所以这里只需要：
          1. 开关打开时构建一次并挂上；
          2. 每 tick 重新调一次 load_entity_index()：缓存命中时是空操作，
             generation 变了才会真重建。

        这样 daemon 重启后不必等 900s，索引也不会「比账本旧」。
        """
        try:
            from .entity_index import entity_channel_enabled
        except Exception:
            return
        if not entity_channel_enabled():
            self.health["entity_index"] = {"state": "disabled"}
            self._set_background_state("entity_index", "disabled")
            return
        # 刻意**不**用 read_only / watch_enabled 门禁（向量通道用了，这里不用）：
        # 实体倒排的构建是纯读（ledger._reading()），既不采集也不写盘，所以
        # 只读镜像和 --no-watch 的 daemon 同样能用它。把只读能力挡在门禁外
        # 是没必要的功能损失——而且会让 health 里连个状态都没有，运维看不到
        # 「到底开没开」。
        if self._stop_event.is_set():
            self.health["entity_index"] = {"state": "stopped"}
            self._set_background_state("entity_index", "skipped:stopped")
            return
        # Deliberately allowed in read-only mode: building the entity inverted
        # index only reads the ledger (ledger._reading()).
        self._set_background_state(
            "entity_index", "running:read_only" if self.read_only else "running")

        def loop() -> None:
            while not self._stop_event.is_set():
                try:
                    from .entity_index import load_entity_index
                    index = load_entity_index(self.ledger)
                    if index is None:
                        self.health["entity_index"] = {"state": "unavailable"}
                    else:
                        # 换到 live builder：新 pack 立刻用上实体通道。
                        self.contexts.entity_index = index
                        self.health["entity_index"] = dict(index.stats(),
                                                           state="ready")
                except Exception as exc:  # never take the hub down
                    self.health["entity_index"] = {
                        "state": "error",
                        "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
                self._stop_event.wait(tick_seconds)

        threading.Thread(target=loop, name="hippomind-entity-index",
                         daemon=True).start()

    def start_capture_loop(self, *, tick_seconds: float = 60.0) -> None:
        """Continuously capture native Claude/Codex transcripts."""
        if self.read_only or not self.watch_enabled or self._stop_event.is_set():
            self._set_background_state(
                "capture",
                "skipped:read_only" if self.read_only else
                ("skipped:watch_disabled" if not self.watch_enabled else "skipped:stopped"))
            return
        self._set_background_state("capture", "running")

        def loop() -> None:
            while not self._stop_event.is_set():
                try:
                    summary = self.transcripts.sync(max_messages=100)
                    try:
                        summary["gemini"] = self.gemini_capture.sync(max_rows=50)
                    except Exception as gemini_exc:
                        summary["gemini"] = {"error": "%s: %s" % (type(gemini_exc).__name__, str(gemini_exc)[:200])}
                    try:
                        summary["pi"] = self.pi_capture.sync(max_messages=100)
                    except Exception as pi_exc:
                        summary["pi"] = {"error": "%s: %s" % (type(pi_exc).__name__, str(pi_exc)[:200])}
                    try:
                        # Cursor 有三套互不相同的存储栈；本机未安装时应报
                        # state="not_installed"，有数据但 schema 不认识时报
                        # "unrecognized"——绝不返回 0 条冒充成功。
                        summary["cursor"] = collect_cursor(self.ledger, max_messages=100)
                    except Exception as cursor_exc:
                        summary["cursor"] = {"error": "%s: %s" % (type(cursor_exc).__name__, str(cursor_exc)[:200])}
                    self.health["capture"] = summary
                except Exception as exc:  # capture must never take the hub down
                    self.health["capture"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
                self._stop_event.wait(tick_seconds)

        threading.Thread(target=loop, name="hippomind-transcripts", daemon=True).start()

    def start_distill_loop(self, *, tick_seconds: float = 300.0) -> None:
        """Continuously distill new evidence into claims, with bounded batches."""
        if self.read_only or not self.watch_enabled or self._stop_event.is_set():
            self._set_background_state(
                "distill",
                "skipped:read_only" if self.read_only else
                ("skipped:watch_disabled" if not self.watch_enabled else "skipped:stopped"))
            return
        self._set_background_state("distill", "running")

        def loop() -> None:
            while not self._stop_event.is_set():
                try:
                    # limit=64 来自端到端实测（不是估算）：一轮 256 行 / 8 次
                    # 调用 / 约 210s。配合下面 300s 的 tick，占空比约 70%，
                    # 留出余量给会话摘要与冲突收敛。实测吞吐 162.9 -> ~3072 条/小时。
                    # 注意：批次大小保持默认 8（不随 limit 放大）——配对实验
                    # 显示批 16 会静默丢掉约 11% 的条目（标成"无可提炼内容"），
                    # 那是拿质量换速度，不做。tick 也不要降到 120s：210s 的活塞进
                    # 120s 会背靠背连跑，退化成不停跑。
                    # limit 可用 HIPPOMIND_DISTILL_LIMIT 覆盖：2026-10-24 实测
                    # daemon 每 100 秒新增 21 条 distill 产物（~756 条/小时），
                    # 其中 51% 是重复内容——入库去重落地前先用它把速率压下来。
                    summary = self.distiller.run(
                        limit=int(os.environ.get("HIPPOMIND_DISTILL_LIMIT", "64")))
                    # Whole-session digests are the throughput path for the
                    # 250k-message backlog: one model call per session instead
                    # of one per message.  Bounded so it never starves the
                    # per-memory queue.
                    try:
                        summary["digest"] = self.digester.run(limit=2)
                    except Exception as digest_exc:
                        summary["digest"] = {"error": "%s: %s" % (
                            type(digest_exc).__name__, str(digest_exc)[:200])}
                    self.health["distill"] = summary
                except Exception as exc:
                    self.health["distill"] = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
                self._stop_event.wait(tick_seconds)

        threading.Thread(target=loop, name="hippomind-distill", daemon=True).start()

    def start_metrics_trend_thread(self, *, tick_seconds: float = 60.0) -> None:
        """后台线程：每分钟醒来检查是否到了写趋势行的时间（每小时一行）。"""
        if self.read_only:
            self._set_background_state("metrics_trend", "skipped:read_only")
            return
        self._set_background_state("metrics_trend", "running")

        def loop() -> None:
            while not self._stop_event.is_set():
                self.append_metrics_trend()
                self._stop_event.wait(tick_seconds)

        thread = threading.Thread(target=loop, name="hippomind-metrics-trend", daemon=True)
        thread.start()

    def _auto_install_detected(self, report: Mapping[str, Any],
                               home: Optional[os.PathLike] = None) -> Dict[str, Any]:
        """Install integrations for manifest-known, actually detected agents.

        Controlled by construction: only provider-manifest agents with local
        detection evidence are touched — unknown runtimes stay report-only.
        Each install is verified and repaired once; failures are recorded in
        ``self.auto_install`` and never propagate to the caller.
        """
        if self.read_only:
            # Defense in depth: even a direct call cannot rewrite Agent configs
            # from a read-only mirror.
            return {"ran_at": utc_now(), "skipped": "read_only", "agents": {}}
        from .agent_install import agent_install_status, install_agents
        known = report.get("known", {}) if isinstance(report, dict) else {}
        detected = sorted(str(agent) for agent, item in known.items()
                          if isinstance(item, Mapping) and item.get("detected"))
        package_root = Path(__file__).resolve().parents[1]
        outcome: Dict[str, Any] = {"ran_at": utc_now(), "agents": {}}
        for agent in detected:
            try:
                status = self._status_one(agent_install_status, agent, home)
                if self._integration_complete(status):
                    outcome["agents"][agent] = {"state": "already_installed"}
                    continue
                install_agents(self.ledger, self.paths, [agent],
                               package_root=package_root, home=home)
                status = self._status_one(agent_install_status, agent, home)
                if not self._integration_complete(status):
                    # One repair pass mirrors the CLI install flow.
                    install_agents(self.ledger, self.paths, [agent],
                                   package_root=package_root, home=home)
                    status = self._status_one(agent_install_status, agent, home)
                complete = self._integration_complete(status)
                outcome["agents"][agent] = {"state": "installed" if complete else "degraded",
                                            "verified": complete, "status": status}
            except Exception as exc:  # one agent must never stop the others
                outcome["agents"][agent] = {"state": "error", "error": str(exc)[:300]}
        self.auto_install = outcome
        return outcome

    def _status_one(self, status_fn: Any, agent: str, home: Optional[os.PathLike]) -> Dict[str, Any]:
        for item in status_fn(self.ledger, self.paths, [agent], home=home):
            if item.get("agent") == agent:
                return item
        return {}

    @staticmethod
    def _integration_complete(status: Mapping[str, Any]) -> bool:
        """A tier is complete when its real integration is installed and valid."""
        if not isinstance(status, Mapping):
            return False
        if not status.get("credential_valid") or not status.get("rules_installed"):
            return False
        mcp = status.get("mcp_registration")
        if isinstance(mcp, Mapping) and mcp.get("supported") and not mcp.get("registered"):
            return False
        hook = status.get("native_context_hook")
        if isinstance(hook, Mapping) and not hook.get("installed"):
            return False
        plugin = status.get("native_dsh_plugin")
        if isinstance(plugin, Mapping) and plugin.get("detected") and not plugin.get("installed"):
            return False
        return True

    def state(self, host: str, port: int) -> Dict[str, Any]:
        return {
            "pid": os.getpid(), "host": host, "port": port, "started_at": self.started_at,
            "api_version": API_VERSION, "ledger": str(self.paths.ledger_path),
            "watch_enabled": self.watch_enabled, "auto_install": self.auto_install,
            "read_only": bool(self.read_only),
        }

    #: Trace window size/recent samples exposed by /stats (bounded tail read).
    INJECTION_TRACE_WINDOW = 200
    INJECTION_TRACE_RECENT = 10

    def _injection_trace_window(self, policy: AccessPolicy) -> Dict[str, Any]:
        """最近注入 trace 的窗口统计与末尾样本（复用 tail 读取）。

        读取量由 injection_trace.recent() 的 TAIL_READ_BYTES 硬上限约束，
        与 trace 文件总大小无关；关闭 debug/空结果/错误样本同样计入。
        """
        try:
            rows = injection_trace.recent(self.INJECTION_TRACE_WINDOW)
        except Exception:
            rows = []
        injected = sum(1 for row in rows if row.get("injected"))
        arms = {injection_ab.TREATMENT: 0, injection_ab.CONTROL: 0, "unknown": 0}
        for row in rows:
            arm = str(row.get("arm") or "") or "unknown"
            arms[arm] = arms.get(arm, 0) + 1
        window: Dict[str, Any] = {
            "count": len(rows),
            "injected": injected,
            "empty": len(rows) - injected,
            "errors": sum(1 for row in rows if row.get("error")),
            "arms": arms,
            "last_at": str(rows[-1].get("at", "")) if rows else "",
            "last_agent": str(rows[-1].get("agent", "")) if rows else "",
            "recent": [dict(row) for row in rows[-self.INJECTION_TRACE_RECENT:]],
        }
        if not policy.admin:
            # memory_ids/query_hash 是各 Agent 自己的可见面：scoped caller
            # 只看本 Agent 的样本，聚合计数仍可作为健康信号。
            window["restricted"] = True
            window["recent"] = [row for row in window["recent"]
                                if str(row.get("agent", "")) == policy.agent_id]
        return window

    def stats(self, policy: Optional[AccessPolicy] = None) -> Dict[str, Any]:
        policy = policy or AccessPolicy.administrator()
        policy.can_read_stats()
        if policy.admin:
            ledger_stats: Dict[str, Any] = self.ledger.stats()
        else:
            # Do not disclose other agents' projects, raw retention volume, or
            # the filesystem location to a scoped caller. Scope-level counts
            # are intentionally omitted until a query has an explicit need.
            ledger_stats = {"access": "restricted", "scopes": list(policy.scopes),
                            "raw_chat": 0, "agent_credentials": 0}
        response = {"ledger": ledger_stats, "watcher": {"available": self.watcher.available,
                    "poll_interval": self.watcher.poll_interval},
                    "agent_discovery": self.discovery.summary(ensure=True),
                    "auto_install": self.auto_install,
                    "context_cache": self.contexts.cache_stats(),
                    "health": dict(self.health),
                    "started_at": self.started_at,
                    "pid": os.getpid() if policy.admin else 0}
        response["injection_trace"] = self._injection_trace_window(policy)
        # WAL 体积告警：正常情况下维护周期 TRUNCATE 检查点会归零侧车；
        # 持续超阈值说明检查点被长读饿住或写入异常放大，需要运维关注。
        try:
            wal = Path(str(self.ledger.path) + "-wal")
            wal_bytes = wal.stat().st_size if wal.exists() else 0
            response["wal_bytes"] = wal_bytes
            if wal_bytes > 64 * 1024 * 1024:
                response["warnings"] = [
                    f"WAL 侧车 {wal_bytes // (1024 * 1024)} MiB 超过 64 MiB 阈值——"
                    "检查是否有长事务阻塞检查点"]
        except OSError:
            pass
        if not policy.admin:
            # 与受限 ledger 统计同一隔离意图：其他 Agent 的检测名单和安装
            # 状态不属于 scoped caller 的可见面。
            response["agent_discovery"] = {"available": False, "restricted": True}
            response["auto_install"] = {}
        return response

    def reload(self) -> Dict[str, Any]:
        """热重载：强制重扫 Agent 发现并触发一轮受控自动安装。

        注入开关（InjectionState）每次调用都重新读取，无需缓存失效；
        watcher 归档根在启动时固定，重载不改变采集范围。任何失败都
        不应中断 daemon——错误以字段形式返回。
        """
        if self.read_only:
            # Discovery + controlled auto-install rewrite Agent configuration
            # files under HOME; a read-only mirror never does that.
            return {"reloaded_at": utc_now(), "skipped": "read_only"}
        outcome: Dict[str, Any] = {"reloaded_at": utc_now()}
        try:
            report = self.discovery.maybe_discover(force=True)
            outcome["discovery"] = self.discovery.summary()
            # 同样尊重 HIPPOMIND_HOME：reload 是 HTTP 端点，测试与沙箱会调用它，
            # 不传 home 就会写进用户真实配置（本机实测：跑测试污染 6 个真实文件）。
            outcome["auto_install"] = self._auto_install_detected(
                report, home=os.environ.get("HIPPOMIND_HOME") or None)
        except Exception as exc:
            outcome["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return outcome

    def note_activity(self) -> None:
        """记录一次外部活动（HTTP 请求等），推迟空闲睡眠巩固。"""
        self._last_activity_monotonic = time.monotonic()

    def _maybe_sleep_consolidate(self) -> Optional[Dict[str, Any]]:
        """空闲睡眠巩固（sleep-time compute）：无请求且无新增导入超过阈值时，
        立即执行一轮深维护（巩固 + raw 归档 + WAL 检查点）。

        返回本轮报告；未触发返回 None。同一空闲期内只跑一次。
        """
        if self.read_only:
            # Repeated here (not only in start_sleep_loop) so a direct call can
            # never decay/purge/checkpoint the read-only copy. The ledger write
            # guard would refuse anyway; not even attempting keeps the mirror
            # provably free of maintenance side effects.
            return None
        now = time.monotonic()
        activity_sources = [self._last_activity_monotonic]
        last_import = getattr(self.watcher, "last_import_monotonic", None)
        if last_import is not None:
            activity_sources.append(last_import)
        idle_for = now - max(activity_sources)
        since_last = (now - self._last_sleep_run
                      if self._last_sleep_run is not None else float("inf"))
        import_idle = (now - last_import) if last_import is not None else float("inf")
        max_interval = float(getattr(self, "_sleep_max_interval", 3600.0))
        force_due = (
            self._last_sleep_run is not None
            and since_last >= max_interval
            and import_idle >= 30.0
        )
        if not force_due and (idle_for < SLEEP_IDLE_SECONDS or since_last < SLEEP_IDLE_SECONDS):
            return None
        self._last_sleep_run = now
        report: Dict[str, Any] = {"triggered_at": utc_now(),
                                  "idle_seconds": round(idle_for, 1)}
        try:
            # The daemon is the only component that runs unattended for weeks,
            # so it is the one that must actually enforce the raw retention
            # horizon.  Without purge_raw=True, "retention" only demotes rows to
            # cold and the file grows forever (observed: 1.47M raw rows, 4.48GB).
            maintained = self.ledger.maintain(archive_raw=True, purge_raw=True)
            report["consolidated"] = len(self.ledger.consolidate())
            report["decayed"] = maintained.get("decayed", 0)
            report["raw_compressed"] = maintained.get("raw_compressed", 0)
            # Convergence must be observable from a long-running process. Without
            # these counters an operator can watch the daemon stay healthy while
            # the ledger silently grows to 4.48GB: nothing reported how many rows
            # were actually purged, nor how much file space was reclaimable.
            purged = maintained.get("raw_purged") or {}
            report["raw_purged"] = int(purged.get("purged", 0) or 0)
            report["raw_events_purged"] = int(purged.get("events_purged", 0) or 0)
            report["purge_error"] = str(purged.get("error", "") or "")
            storage = maintained.get("storage") or {}
            report["storage"] = {
                "freelist": int(storage.get("freelist", 0) or 0),
                "page_count": int(storage.get("page_count", 0) or 0),
                "db_bytes": int(storage.get("db_bytes", 0) or 0),
                "reclaimable_bytes": int(storage.get("reclaimable_bytes", 0) or 0),
            }
            shrink = maintained.get("auto_vacuum") or {}
            report["auto_vacuum"] = {
                "vacuumed": bool(shrink.get("vacuumed")),
                "mode": str(shrink.get("mode", "none")),
                "reclaimed_bytes": int(shrink.get("reclaimed_bytes", 0) or 0),
                "reason": str(shrink.get("reason", "")),
            }
            # Keep the cumulative view on the health record so /stats shows the
            # whole-run trend, not just the most recent cycle.
            self.health["raw_purged_total"] += report["raw_purged"]
            self.health["bytes_reclaimed_total"] += report["auto_vacuum"]["reclaimed_bytes"]
            self.health["last_storage"] = dict(report["storage"])
            wal = self.ledger.maintenance_checkpoint()
            report["wal_truncated"] = bool(wal.get("wal_truncated"))
            self.health["sleep_cycles"] += 1
            self.health["last_sleep_report_at"] = utc_now()
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            self.health["sleep_errors"] += 1
        self.last_sleep_report = report
        return report

    def start_sleep_loop(self) -> None:
        """低频空闲检测线程：每分钟检查一次是否满足睡眠巩固条件。"""
        if self.read_only:
            self._set_background_state("sleep", "skipped:read_only")
            return
        if self._sleep_thread is not None:
            self._set_background_state("sleep", "running")
            return
        self._set_background_state("sleep", "running")

        def loop() -> None:
            while not self._stop_event.is_set():
                try:
                    report = self._maybe_sleep_consolidate()
                    if report is not None and "error" not in report:
                        # 运维日志必须同时显示「删了多少」和「文件缩了多少」：
                        # 只打归档数会让一个永不删除的保留策略看起来正常。
                        print(f"[hippomindd] 空闲巩固：规则 +{report['consolidated']}，"
                              f"衰减 {report['decayed']} 行，raw 归档 "
                              f"{report['raw_compressed']} 条，"
                              f"物理删除 {report.get('raw_purged', 0)} 条，"
                              f"空闲页 {report.get('storage', {}).get('freelist', 0)}")
                except Exception:
                    pass
                self._stop_event.wait(60.0)

        self._sleep_thread = threading.Thread(target=loop, name="hippomind-sleep",
                                              daemon=True)
        self._sleep_thread.start()

    def begin_shutdown(self) -> None:
        """Stop serving quickly; the checkpoint happens in shutdown()."""
        self._stop_event.set()
        try:
            self.watcher.stop()
        except Exception:
            pass
        try:
            self.gui.close()
        except Exception:
            pass
        self._request_server_stop()

    def _request_server_stop(self) -> None:
        if self._server is None:
            return
        thread = getattr(self, "_server_stop_thread", None)
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=self._server.shutdown,
                                  name="hippomind-shutdown", daemon=True)
        self._server_stop_thread = thread
        thread.start()

    def _join_server_stop(self, timeout: float = 5.0) -> None:
        thread = getattr(self, "_server_stop_thread", None)
        if thread is not None:
            thread.join(timeout=timeout)

    def _join_background_threads(self, *, timeout: float = 8.0) -> list:
        """Wait for service-owned loops so no writer commits after the checkpoint."""
        deadline = time.monotonic() + max(0.1, timeout)
        current = threading.current_thread()
        survivors = []
        for thread in threading.enumerate():
            if thread is current or not thread.name.startswith("hippomind-"):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=min(remaining, 2.0))
            if thread.is_alive():
                survivors.append(thread.name)
        return survivors

    def _final_checkpoint(self, *, attempts: int = 5, delay: float = 0.05) -> Dict[str, Any]:
        """TRUNCATE the WAL with bounded retries (a reader may report busy once)."""
        summary: Dict[str, Any] = {}
        for _attempt in range(max(1, attempts)):
            try:
                summary = self.ledger.maintenance_checkpoint()
            except Exception as exc:
                summary = {"wal_truncated": False, "wal_busy": False,
                           "wal_pages": 0, "error": type(exc).__name__}
            if summary.get("wal_truncated") or int(summary.get("wal_pages") or 0) == 0:
                return summary
            time.sleep(delay)
        logging.getLogger("hippomindd").warning(
            "停机检查点重试 %d 次后 WAL 未归零: busy=%s pages=%s",
            attempts, summary.get("wal_busy"), summary.get("wal_pages"))
        return summary

    def shutdown(self) -> None:
        """Graceful stop: stop writers, TRUNCATE checkpoint, reclaim readers.

        Ordering matters.  Checkpointing before the background writers stop
        leaves their commits in the WAL after shutdown returns; checkpointing
        before readers are reclaimed lets TRUNCATE report busy.  So: stop the
        server, join service threads, retry the checkpoint, close every reader,
        then retry once more and close again.  Idempotent by design: the first
        caller does the work, concurrent callers wait for completion.
        """
        with self._shutdown_lock:
            first = not self._shutdown_started
            if first:
                self._shutdown_started = True
        if not first:
            self._shutdown_complete.wait(15.0)
            return
        try:
            self.begin_shutdown()
            self._join_server_stop()
            self._join_background_threads()
            self._final_checkpoint()
            try:
                self.ledger.close()
            except Exception:
                pass
            self._final_checkpoint(attempts=2, delay=0.02)
            try:
                self.ledger.close()
            except Exception:
                pass
        finally:
            self._shutdown_complete.set()

class MemoryHubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # 默认 backlog=5 在多 agent 并发 + HTTP/1.1 keep-alive 下会溢出，
    # 内核直接 reset 新连接（前景轮九审查发现：8 客户端压测第 4 轮起
    # 出现 Connection reset by peer）。提到 128 与 MAX_CONCURRENT_REQUESTS 同量级。
    request_queue_size = 128

    def __init__(self, address, service: MemoryHubService):
        self.service = service
        self.request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(address, MemoryHubRequestHandler)


class MemoryHubRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hippomindd/" + SERVER_VERSION

    @property
    def hub(self) -> MemoryHubService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        # Avoid logging request bodies, tokens, or memory data.
        sys.stderr.write("[hippomindd] " + (fmt % args) + "\n")

    def _write(self, status: int, data: Mapping[str, Any], close: bool = False) -> None:
        try:
            raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            status, raw = 500, b'{"error":"response serialization failed"}'
        if len(raw) > MAX_RESPONSE_BYTES:
            status, raw = 413, b'{"error":"response exceeds configured budget"}'
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # A client timeout/disconnect is not an application error and a
            # second attempt to write an error response would just raise again.
            self.close_connection = True
            return
        if close:
            self.close_connection = True

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write(400, {"error": "invalid Content-Length"})
            return None
        if length <= 0:
            self._write(400, {"error": "JSON request body required"})
            return None
        if length > MAX_REQUEST_BYTES:
            # 排空请求体后再回 413：否则客户端仍在发送时连接被 RST，
            # 永远收不到这个错误响应（前景轮九审查修复）。排空设上限，
            # 防止伪造的超大 Content-Length 拖住工作线程。
            remaining = length
            drain_cap = MAX_REQUEST_BYTES * 8
            while 0 < remaining <= drain_cap:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._write(413, {"error": "request exceeds configured budget"},
                        close=True)
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._write(400, {"error": "invalid JSON"})
            return None
        if not isinstance(data, dict):
            self._write(400, {"error": "JSON root must be an object"})
            return None
        return data

    def _require_auth(self) -> Optional[AccessPolicy]:
        token = self.headers.get("X-HippoMind-Token", "")
        policy = self.hub.access_policy(token)
        if policy is not None:
            return policy
        self._write(401, {"error": "local authorization token required"})
        return None

    @staticmethod
    def _options(data: Mapping[str, Any]) -> ContextPackOptions:
        raw = data.get("options", {})
        raw = raw if isinstance(raw, Mapping) else {}
        return ContextPackOptions(
            max_items=raw.get("max_items", data.get("max_items", 8)),
            max_chars=raw.get("max_chars", data.get("max_chars", DEFAULT_MAX_CHARS)),
            max_tokens=raw.get("max_tokens", data.get("max_tokens", DEFAULT_MAX_TOKENS)),
            raw_chat=bool(raw.get("raw_chat", data.get("raw_chat", False))),
            require_evidence=bool(raw.get("require_evidence", data.get("require_evidence", True))),
            scope=raw.get("scope", data.get("scope", "shared")),
            project=raw.get("project", data.get("project", "")),
            task_id=raw.get("task_id", raw.get("task", data.get("task_id", data.get("task", "")))),
            source_agents=raw.get("source_agents", raw.get("sources", data.get("source_agents", []))) or [],
            roles=raw.get("roles", data.get("roles", [])) or [],
            from_time=raw.get("from_time", data.get("from_time", "")),
            to_time=raw.get("to_time", data.get("to_time", "")),
            as_of=raw.get("as_of", data.get("as_of", "")),
            # WP-M：会话感知 follow-up 锚定。session_id 由宿主 hooks / DSH
            # 插件 / CLI 透传；query_context 允许单次请求关闭（全局开关是
            # HIPPOMIND_QUERY_CONTEXT，在 context_pack/query_context 里判）。
            session_id=str(raw.get("session_id", data.get("session_id", "")) or "")[:256],
            query_context=bool(raw.get("query_context",
                                       data.get("query_context", True))),
            # WP-Q：事实卡默认跟随 HIPPOMIND_FACT_CARDS（默认关闭）；单次请求
            # 可用 body.options.fact_cards 显式覆盖。自动注入 path 与显式 API
            # 共用本方法，所以显式调用方想关就传 false。
            fact_cards=bool(raw.get("fact_cards",
                                    data.get("fact_cards", fact_cards_enabled()))),
            max_fact_card_chars=raw.get("max_fact_card_chars",
                                        data.get("max_fact_card_chars", 600)),
            debug=bool(raw.get("debug", data.get("debug", False))),
            # /context-pack is the automatic-injection endpoint, so it
            # defaults to the same relevance gate as the CLI auto path.
            # Explicit callers (MCP, scripts) can pass min_score=0.
            min_score=raw.get("min_score", data.get("min_score",
                float(os.environ.get("HIPPOMIND_MIN_SCORE", "0.20")))),
            hybrid_rerank=bool(raw.get("hybrid_rerank",
                                      data.get("hybrid_rerank", True))),
            workspace_cwd=str(raw.get("workspace_cwd",
                data.get("workspace_cwd", data.get("cwd", ""))) or "")[:512],
            include_pinned=bool(raw.get("include_pinned",
                data.get("include_pinned", False))),
        )

    @staticmethod
    def _public_memory(item: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "memory_id": item.get("memory_id", ""), "kind": item.get("kind", ""),
            "content": str(item.get("content", ""))[:12_000], "source_agent": item.get("source_agent", ""),
            "session_id": item.get("session_id", ""), "task_id": item.get("task_id", ""),
            "project": item.get("project", ""), "role": item.get("role", ""), "scope": item.get("scope", ""),
            "created_at": item.get("created_at", ""), "lifecycle": item.get("lifecycle", ""),
            "weight": item.get("weight", 0.0), "confidence": item.get("confidence", 0.0),
            "utility": item.get("utility", 0.0), "evidence": item.get("evidence", []),
            "score": round(float(item.get("score", 0.0)), 4),
        }

    @staticmethod
    def _query_data(raw_query: str) -> Dict[str, Any]:
        """GET query string -> 与 POST JSON body 同形的 data 映射。"""
        params_ = parse_qs(raw_query)
        data: Dict[str, Any] = {}
        for key, values in params_.items():
            if values:
                data[str(key)] = values[0]
        for key in ("max_items", "max_chars", "max_tokens"):
            if key in data:
                try:
                    data[key] = int(data[key])
                except (TypeError, ValueError):
                    data.pop(key)
        if "min_score" in data:
            try:
                data["min_score"] = float(data["min_score"])
            except (TypeError, ValueError):
                data.pop("min_score")
        for key in ("raw_chat", "require_evidence", "hybrid_rerank", "debug",
                    "query_context", "fact_cards"):
            if key in data:
                data[key] = str(data[key]).strip().lower() in {"1", "true", "yes", "on"}
        return data

    @staticmethod
    def _trace_context_pack(*, agent: str, query: str, injected: bool,
                            item_count: int, text: str, memory_ids: list,
                            empty_reason: str, latency_ms: float, scope: str,
                            task_id: str, error: str = "",
                            arm: str = "") -> None:
        """旁路写一条 HTTP 注入 trace；观测失败绝不影响请求本身。"""
        try:
            injection_trace.record_server_injection(
                agent=agent, query=query, injected=injected,
                item_count=item_count, context_chars=len(text or ""),
                memory_ids=memory_ids, empty_reason=empty_reason,
                latency_ms=latency_ms, scope=scope, task_id=task_id,
                error=error, arm=arm, source="http")
        except Exception as exc:
            # 只记录异常类型：错误正文可能带路径/数据，不进日志。
            sys.stderr.write(
                f"[hippomindd] injection trace failure: {type(exc).__name__}\n")

    def _serve_context_pack(self, policy: AccessPolicy,
                            data: Mapping[str, Any]) -> None:
        """GET/POST /context-pack 的唯一注入与 trace 路径。"""
        started = time.perf_counter()
        query = str(data.get("query", ""))
        agent = str(data.get("agent", "all") or "all")
        if not policy.admin:
            if agent not in {"", "all", policy.agent_id}:
                raise AccessDenied(
                    f"agent {policy.agent_id} 不能读取 {agent} 的上下文开关")
            agent = policy.agent_id
        trace_agent = str(data.get("agent") or "").strip()
        if trace_agent in {"", "all"}:
            trace_agent = "" if policy.admin else str(policy.agent_id or "")
        trace_agent = (trace_agent or "http")[:64]
        options = self._options(data)
        options.scope = policy.enforce_options(options.scope, raw_chat=options.raw_chat)
        if options.session_id and not options.anchor_scopes:
            extra_scopes = [s for s in getattr(policy, "scopes", ()) or () if s and s != options.scope]
            if not extra_scopes and trace_agent and trace_agent != "http":
                extra_scopes = [f"agent:{trace_agent}"]
            options.anchor_scopes = tuple(extra_scopes)
        trace_scope = str(options.scope or "")
        trace_task = str(options.task_id or "")[:128]
        # A/B runs after the host decided this prompt wants memory and before
        # the build, so an empty retrieval cannot consume a control slot.  The
        # control arm still retrieves (matched pair), only delivery differs.
        caller_ab_handled = bool(data.get("ab_handled", False))
        if caller_ab_handled:
            ab_arm = str(data.get("ab_arm") or "").strip()[:32]
        else:
            ab_decision = injection_ab.assign(agent=trace_agent, query=query)
            ab_arm = (str(ab_decision.get("arm") or "")
                      if ab_decision.get("enabled") else "")
        if ab_arm == injection_ab.CONTROL:
            options.record_touch = False
        try:
            injection = self.hub.injection.status(agent)
        except LedgerError:
            injection = {"agent": agent, "enabled": False, "error": "invalid_state"}
        if not injection.get("enabled", False):
            # 关闭开关同样是「未注入」对照样本，必须留 trace。
            reason = "injection_state_error" if injection.get("error") else "injection_disabled"
            if not caller_ab_handled:
                self._trace_context_pack(
                    agent=trace_agent, query=query, injected=False, item_count=0,
                    text="", memory_ids=[], empty_reason=reason,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    scope=trace_scope, task_id=trace_task)
            self._write(200, {"items": [], "text": "",
                              "injection_enabled": False,
                              "injection_agent": injection.get("agent", agent)})
            return
        debug_requested = bool(options.debug)
        # memory_ids 只有 build() 的 debug 视图会披露（items 本身不带 id）。
        # 内部强制开启 debug 取 id，再按调用方是否请求过 debug 剥离。
        options.debug = True
        try:
            pack = self.hub.contexts.build(query, options)
        except Exception as exc:
            if not caller_ab_handled:
                self._trace_context_pack(
                    agent=trace_agent, query=query, injected=False, item_count=0,
                    text="", memory_ids=[], empty_reason="error",
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    scope=trace_scope, task_id=trace_task,
                    error=type(exc).__name__)
            raise
        # build() 的缓存按 options.debug 分键且返回共享 dict：这里先浅拷贝，
        # 避免响应剥离 debug / 追加 injection_* 字段时污染缓存条目。
        pack = dict(pack)
        debug = pack.get("debug") if isinstance(pack.get("debug"), Mapping) else {}
        if not debug_requested:
            pack.pop("debug", None)
        items = pack.get("items") if isinstance(pack.get("items"), list) else []
        text = str(pack.get("text", "") or "")
        memory_ids = []
        selected = debug.get("selected") if isinstance(debug.get("selected"), list) else []
        for entry in selected:
            entry_ids = entry.get("memory_ids") if isinstance(entry, Mapping) else None
            for memory_id in entry_ids if isinstance(entry_ids, list) else []:
                value = str(memory_id)
                if value and value not in memory_ids:
                    memory_ids.append(value)
        injected = bool(items)
        empty_reason = ""
        if not injected:
            empty_reason = str(debug.get("empty_reason") or "")
            if not empty_reason:
                count = debug.get("candidate_count")
                if count is None and isinstance(debug.get("candidates"), list):
                    count = len(debug["candidates"])
                empty_reason = ("candidates=%d" % int(count)
                                if isinstance(count, int) else "candidates=0")
        if ab_arm == injection_ab.CONTROL and not caller_ab_handled:
            # Matched control: retrieval and scoring ran on exactly the same
            # candidate set as the treatment arm; only delivery is withheld.
            # Recording the offered ids keeps both arms evaluable.
            self._trace_context_pack(
                agent=trace_agent, query=query, injected=False,
                item_count=0, text="", memory_ids=memory_ids,
                empty_reason="ab_control",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                scope=trace_scope, task_id=trace_task, arm=ab_arm)
            self._write(200, {"items": [], "text": "",
                              "injection_enabled": True,
                              "injection_agent": injection.get("agent", agent),
                              "injection_arm": ab_arm})
            return
        if not caller_ab_handled:
            self._trace_context_pack(
                agent=trace_agent, query=query, injected=injected,
                item_count=len(items), text=text if injected else "",
                memory_ids=memory_ids, empty_reason=empty_reason,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                scope=trace_scope, task_id=trace_task, arm=ab_arm)
        pack["injection_enabled"] = True
        pack["injection_agent"] = injection.get("agent", agent)
        if ab_arm:
            pack["injection_arm"] = ab_arm
        self._write(200, pack)

    def _safe_serve_context_pack(self, policy: AccessPolicy,
                                 data: Mapping[str, Any]) -> None:
        """把共享注入路径的异常映射成与 POST 原有语义一致的 HTTP 状态。"""
        try:
            self._serve_context_pack(policy, data)
        except AccessDenied as exc:
            self._write(403, {"error": str(exc)})
        except LedgerError as exc:
            self._write(400, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            self._write(400, {"error": str(exc)})
        except Exception as exc:
            sys.stderr.write(
                f"[hippomindd] context-pack failure: {type(exc).__name__}: {exc}\n")
            self._write(500, {"error": "internal server error"})

    def _handle_get(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._write(200, {"status": "ok", "api_version": API_VERSION, "pid": os.getpid(),
                              "server_version": SERVER_VERSION, "watch_enabled": self.hub.watch_enabled,
                              "read_only": bool(getattr(self.hub, "read_only", False)),
                              "ledger_id": ledger_identity(self.hub.paths.ledger_path)})
            return
        if path == "/stats":
            policy = self._require_auth()
            if policy is not None:
                self._write(200, self.hub.stats(policy))
            return
        if path == "/context-pack":
            # GET/POST 共用 _serve_context_pack：两种方法写同一条统一 trace。
            policy = self._require_auth()
            if policy is not None:
                self.server.service.note_activity()
                self._safe_serve_context_pack(
                    policy, self._query_data(urlsplit(self.path).query))
            return
        if path == "/graph":
            policy = self._require_auth()
            if policy is not None:
                self.server.service.note_activity()
                params_ = parse_qs(urlsplit(self.path).query)
                subject = (params_.get("subject") or [""])[0]
                if not subject:
                    self._write(400, {"error": "subject query parameter required"})
                    return
                try:
                    scope = policy.enforce_options(
                        (params_.get("scope") or ["shared"])[0])
                except AccessDenied:
                    self._write(403, {"error": "scope not allowed"})
                    return
                try:
                    hops = max(1, min(2, int((params_.get("hops") or ["1"])[0])))
                except ValueError:
                    hops = 1
                try:
                    limit = max(10, min(2000,
                                        int((params_.get("limit") or ["200"])[0])))
                except ValueError:
                    limit = 200
                # 节点/边仅含主语、计数与 memory_id，不含记忆正文。
                as_of_arg = str((params_.get("as_of") or [""])[0] or "").strip()
                self._write(200, self.hub.ledger.entity_graph(
                    subject, scope=scope, hops=hops, limit=limit, as_of=as_of_arg))
            return
        if path == "/conflicts":
            # 冲突裁决入口只读视图：claim 元数据 + 记忆摘要，不含全文。
            policy = self._require_auth()
            if policy is not None:
                self.server.service.note_activity()
                params_ = parse_qs(urlsplit(self.path).query)
                try:
                    scope = policy.enforce_options(
                        (params_.get("scope") or ["shared"])[0])
                except AccessDenied:
                    self._write(403, {"error": "scope not allowed"})
                    return
                try:
                    limit = max(1, min(200,
                                       int((params_.get("limit") or ["50"])[0])))
                except ValueError:
                    limit = 50
                report = self.hub.ledger.list_conflicts(scope=scope, limit=limit)
                for item in report.get("items", []):
                    content = str(item.get("content") or "")
                    item["content"] = content[:120]
                self._write(200, report)
            return
        if path == "/metrics":
            # Prometheus 抓取端点：仅暴露计数/体积，不含记忆内容。
            # 匿名抓取曾把账本规模/健康面暴露给本机任意进程；现在与 /stats
            # 一样要求本地凭据（/health 仍是唯一免鉴权端点）。载荷只有聚合
            # 计数与体积，因此按管理员视图渲染以便 Prometheus 在只读镜像上
            # 也能抓到真实规模。
            policy = self._require_auth()
            if policy is None:
                return
            stats = self.hub.stats(AccessPolicy.administrator())
            body = render_prometheus(
                stats, time.time() - getattr(self.hub, "_started_at", time.time()))
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/v1" or path.startswith("/api/v1/"):
            policy = self._require_auth()
            if policy is None:
                return
            params_ = parse_qs(urlsplit(self.path).query)
            try:
                self._write(200, self.hub.gui.get(path, params_, policy))
            except GuiApiError as exc:
                self._write(exc.status, {"error": str(exc)})
            except LedgerError as exc:
                self._write(400, {"error": str(exc)})
            except (TypeError, ValueError) as exc:
                self._write(400, {"error": str(exc)})
            except Exception as exc:
                sys.stderr.write(
                    "[hippomindd] gui api failure: %s: %s\n" % (type(exc).__name__, exc))
                self._write(500, {"error": "internal server error"})
            return
        if path == "/ui" or path.startswith("/ui/"):
            # Preserve the trailing slash: it decides whether index.html is
            # served or redirected, and relative asset URLs depend on it.
            # The routing path above was right-stripped.
            raw_ui_path = urlsplit(self.path).path or "/"
            self._serve_ui(raw_ui_path)
            return
        self._write(404, {"error": "unknown endpoint"})

    # ------------------------------------------------------------------
    # Web GUI (static, same-origin)
    # ------------------------------------------------------------------
    # The browser app is served by the daemon itself rather than opened as a
    # file:// page: a file page has an opaque origin, so every fetch to the
    # loopback API would be a cross-origin request the daemon never agreed to
    # (no CORS headers, and adding them would widen the attack surface for a
    # page anyone could host).  Serving from the same origin keeps the token
    # header same-origin and lets the UI use relative URLs.
    #
    # Only files inside ``hippomind/web/`` are reachable, resolved paths are
    # checked against traversal, and the token is never embedded in the HTML.
    UI_ROOT = Path(__file__).resolve().parent / "web"

    #: A strict same-origin policy for the console. Scripts come only from this
    #: daemon; inline style is allowed because charts set computed style through
    #: CSSOM (CSP style-src also governs style attributes). The frame/objects
    #: directives make clickjacking and plugin embedding impossible.
    UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
              "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
              "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")

    def _serve_ui(self, path: str) -> None:
        # /ui without the trailing slash would make every relative asset URL
        # resolve against / (css/app.js 404). Redirect to the canonical root
        # instead of rewriting the HTML, so the page stays cacheable as a
        # plain static file.
        if path == "/ui":
            self.send_response(302)
            self.send_header("Location", "/ui/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        relative = unquote(path[len("/ui"):].lstrip("/")) or "index.html"
        if not relative or relative.startswith("."):
            self._write(400, {"error": "invalid UI asset path"})
            return
        candidate = (self.UI_ROOT / relative).resolve()
        try:
            candidate.relative_to(self.UI_ROOT.resolve())
        except ValueError:
            self._write(403, {"error": "path outside the UI root"})
            return
        if not candidate.is_file():
            self._write(404, {"error": "no such UI asset"})
            return
        try:
            payload = candidate.read_bytes()
        except OSError:
            self._write(500, {"error": "UI asset unreadable"})
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
                "application/javascript", "application/json", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # The app is local and versioned with the daemon; revalidate every load
        # so an upgraded daemon never serves a stale bundle from the browser.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", self.UI_CSP)
        self.end_headers()
        self.wfile.write(payload)

    def _handle_post(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._write(405, {"error": "use GET /health"})
            return
        if path == "/api/v1/ui-token/exchange":
            # The URL opened by the CLI carries a single-use nonce in the
            # fragment; the page exchanges it for the local token. Unknown or
            # expired nonces get 401 and no timing oracle beyond one dict pop.
            data = self._read_json()
            if data is None:
                return
            try:
                self._write(200, self.hub.gui.exchange_ui_token(data))
            except GuiApiError as exc:
                self._write(exc.status, {"error": str(exc)})
            return
        policy = self._require_auth()
        if policy is None:
            return
        data = self._read_json()
        if data is None:
            return
        try:
            if path.startswith("/api/v1/"):
                self.server.service.note_activity()
                self._write(200, self.hub.gui.post(path, data, policy))
            elif path == "/ingest":
                self.server.service.note_activity()
                records = data.get("records", [data])
                if not isinstance(records, list) or not records or len(records) > 128:
                    raise LedgerError("records 必须是 1 到 128 条对象")
                for record in records:
                    if not isinstance(record, Mapping):
                        raise LedgerError("records 中每条记录必须是对象")
                inserted = self.hub.ledger.ingest_many(policy.enforce_ingest(record) for record in records)
                results = [{"event_id": result.event_id, "memory_id": result.memory_id,
                            "inserted": result.inserted, "content_hash": result.content_hash}
                           for result in inserted]
                self._write(200, {"accepted": len(results), "inserted": sum(r["inserted"] for r in results),
                                  "results": results})
            elif path == "/recall":
                self.server.service.note_activity()
                query = str(data.get("query", ""))
                options = self._options(data).normalise()
                options.scope = policy.enforce_options(options.scope, raw_chat=options.raw_chat)
                limit = max(1, min(100, int(data.get("limit", options.max_items))))
                offset = max(0, min(100_000, int(data.get("offset", 0))))
                hits = self.hub.ledger.search(
                    query, scope=options.scope, task_id=options.task_id, project=options.project,
                    source_agents=options.source_agents, roles=options.roles,
                    from_time=options.from_time, to_time=options.to_time,
                    include_raw_chat=options.raw_chat, require_evidence=options.require_evidence,
                    limit=limit, offset=offset, as_of=options.as_of,
                    embedder=self.hub.contexts.embedder,
                    vector_index=self.hub.contexts.vector_index,
                    entity_index=self.hub.contexts.entity_index,
                )
                self._write(200, {"hits": [self._public_memory(hit) for hit in hits],
                                  "offset": offset, "limit": limit})
            elif path == "/context-pack":
                self.server.service.note_activity()
                self._safe_serve_context_pack(policy, data)
            elif path == "/feedback":
                self.server.service.note_activity()
                memory = self.hub.ledger.get_memory(str(data.get("memory_id", "")))
                if memory is None:
                    raise LedgerError("未找到记忆")
                policy.enforce_feedback_scope(memory.get("scope", ""))
                feedback_source = policy.enforce_feedback_source(data.get("source_agent", ""))
                changed = self.hub.ledger.feedback(
                    str(data.get("memory_id", "")), data.get("signal", 0.0),
                    str(data.get("note", "")), feedback_source,
                    str(data.get("feedback_id", "")),
                )
                self._write(200, {"updated": changed})
            elif path == "/sync":
                self.server.service.note_activity()
                policy.require("sync")
                self._write(200, self.hub.watcher.sync_once())
            elif path == "/stats":
                self._write(200, self.hub.stats(policy))
            elif path == "/admin/reload":
                if not policy.admin:
                    raise AccessDenied("热重载需要管理员凭据")
                self._write(200, self.hub.reload())
            elif path == "/shutdown":
                policy.require("shutdown")
                self._write(202, {"status": "shutting_down"}, close=True)
                # Let the response reach the local launcher before closing the
                # listening socket; immediate shutdown can reset this request.
                threading.Thread(target=lambda: (time.sleep(0.08), self.hub.shutdown()),
                                 name="hippomind-delayed-shutdown", daemon=True).start()
            else:
                self._write(404, {"error": "unknown endpoint"})
        except GuiApiError as exc:
            self._write(exc.status, {"error": str(exc)})
        except AccessDenied as exc:
            self._write(403, {"error": str(exc)})
        except LedgerError as exc:
            self._write(400, {"error": str(exc)})
        except (TypeError, ValueError) as exc:
            self._write(400, {"error": str(exc)})
        except Exception as exc:
            # The detailed exception remains local server stderr; the client
            # gets no internal paths, records, or stack traces.
            sys.stderr.write(f"[hippomindd] request failure: {type(exc).__name__}: {exc}\n")
            self._write(500, {"error": "internal server error"})

    def _limited(self, callback) -> None:
        slots = self.server.request_slots  # type: ignore[attr-defined]
        if not slots.acquire(blocking=False):
            self._write(503, {"error": "server is busy"})
            return
        try:
            callback()
        finally:
            slots.release()

    def do_GET(self) -> None:
        self._limited(self._handle_get)

    def do_POST(self) -> None:
        self._limited(self._handle_post)


def run_daemon(*, ledger_path: Optional[os.PathLike] = None,
               runtime_dir: Optional[os.PathLike] = None, host: str = "127.0.0.1",
               port: int = 8765, archive_root: Optional[os.PathLike] = None,
               watch: bool = True, watch_interval: float = 30.0,
               require_auth: bool = True,
               read_only: bool = False) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise LedgerError("hippomindd 只能绑定 localhost")
    if not 1 <= int(port) <= 65535:
        raise LedgerError("port 必须在 1 到 65535 之间")
    paths = RuntimePaths.for_ledger(ledger_path, runtime_dir)
    service = MemoryHubService(paths, archive_root=archive_root, watch=watch,
                               watch_interval=watch_interval, require_auth=require_auth,
                               read_only=read_only)
    server = MemoryHubHTTPServer((host, int(port)), service)
    service._server = server
    _atomic_json(paths.state_path, service.state(host, int(port)))
    if read_only:
        # Read-only mirror: the only retained background work is the *pure
        # read* entity index build. Watcher, discovery (auto-install), capture,
        # distill, vector sync, sleep consolidation, metrics-trend and warmup
        # either write the ledger or rewrite Agent config, so they are never
        # started. Each start_* carries the same gate; skipping here keeps the
        # startup path auditable in one place.
        service.start_entity_index_loop()
    else:
        service.start_watcher()
        service.start_warmup()
        service.start_discovery_loop()
        service.start_capture_loop()
        service.start_vector_index_loop()
        service.start_entity_index_loop()
        service.start_distill_loop()
        service.start_metrics_trend_thread()
        service.start_sleep_loop()
    # 遗忘审计报告首次调用要付一次无索引全表扫描（ledger_events 279 MiB，
    # 冷页实测 0.7-2.7s），其余部分只要 8-9ms。控制台首屏会卡在这上面。
    # 预热是**显式 opt-in**（GuiApi 的 __init__ 不启动任何后台线程——那样
    # 会持只读连接建出 -shm 文件，把 test_mcp_register 的 rmtree 撞成
    # ENOTEMPTY）。这里在后台线程里跑一次，让第一个真实请求命中缓存。
    if not read_only:
        try:
            service.gui.start_forgetting_prefetch()
        except Exception:
            # 预热失败不影响 daemon 启动：缓存未命中只是慢一点，不是故障。
            pass

    def stop_signal(_signum, _frame) -> None:
        # Signal handlers must return fast: only stop accepting work here;
        # the join/checkpoint sequence runs in the finally block below.
        service.begin_shutdown()

    def reload_signal(_signum, _frame) -> None:
        # SIGHUP 热重载：在信号处理器中只起守护线程，避免重入锁或阻塞事件循环。
        def _bg_reload() -> None:
            try:
                service.reload()
            except Exception:
                pass
        try:
            threading.Thread(target=_bg_reload, name="hippomind-sighup-reload",
                             daemon=True).start()
        except Exception:
            pass

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, stop_signal)
        if hasattr(signal, "SIGHUP"):
            previous[signal.SIGHUP] = signal.signal(signal.SIGHUP, reload_signal)
    try:
        server.serve_forever(poll_interval=0.35)
    finally:
        # Full graceful stop before the process exits, whichever path got us
        # here (SIGTERM only begins shutdown; /shutdown requests and plain
        # serve_forever exits land here).  Idempotent, so a second call is a
        # no-op and a concurrent call waits for the first to finish.
        service.shutdown()
        server.server_close()
        current = load_runtime_state(paths)
        if current.get("pid") == os.getpid():
            try:
                paths.state_path.unlink()
            except OSError:
                pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hippomindd 本机 Memory Hub 服务")
    parser.add_argument("--ledger", help="SQLite/WAL 账本路径")
    parser.add_argument("--runtime-dir", help="daemon 状态与本机 token 目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--archive", help="Agent-Chat-Archive 根目录")
    parser.add_argument("--no-watch", action="store_true", help="不启动归档 watcher")
    parser.add_argument("--watch-interval", type=float, default=30.0)
    parser.add_argument("--no-auth", action="store_true", help="仅供隔离测试；不要在常规本机运行使用")
    parser.add_argument("--read-only", action="store_true",
                        help="只读模式：所有凭据降级为共享只读视图（LAN 镜像/巡检副本）")
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    run_daemon(ledger_path=args.ledger, runtime_dir=args.runtime_dir, host=args.host, port=args.port,
               archive_root=args.archive, watch=not args.no_watch, watch_interval=args.watch_interval,
               require_auth=not args.no_auth, read_only=bool(args.read_only))


if __name__ == "__main__":
    main()
