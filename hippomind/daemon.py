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
from typing import Any, Dict, Mapping, Optional

# 空闲睡眠巩固：连续无外部活动（请求 + watcher 增量）达到该秒数后触发。
SLEEP_IDLE_SECONDS = 600.0
from urllib.parse import parse_qs, urlsplit

from .access import AccessDenied, AccessPolicy
from .context_pack import DEFAULT_MAX_CHARS, DEFAULT_MAX_TOKENS, ContextPackBuilder, ContextPackOptions
from .injection import InjectionState
from .ledger import LedgerError, SQLiteLedger, default_ledger_path, utc_now
from .provider_manifest import DEFAULT_DISCOVERY_INTERVAL_SECONDS, AgentDiscovery
from .watcher import ArchiveWatcher, default_dsh_sessions_root


API_VERSION = "v1"
SERVER_VERSION = "0.9.2"
MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 512_000
MAX_CONCURRENT_REQUESTS = 16


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
        self.ledger = SQLiteLedger(paths.ledger_path)
        self.contexts = ContextPackBuilder(self.ledger)
        # Automatic injection is deliberately independent from collection.
        # A disabled Agent keeps contributing sanitized evidence, but receives
        # an empty Context Pack until it is enabled again.
        self.injection = InjectionState(paths.state_path.parent)
        self.watcher = ArchiveWatcher(self.ledger, archive_root=archive_root,
                                      poll_interval=watch_interval,
                                      dsh_sessions_root=default_dsh_sessions_root())
        self.watch_enabled = bool(watch)
        self.require_auth = bool(require_auth)
        self.read_only = bool(read_only)
        # Discovery walks the home directory, so it runs at most once per
        # interval; between scans the persisted report is reused.
        self.discovery = AgentDiscovery(paths.state_path.parent / "agent-discovery.json",
                                        interval=discovery_interval)
        self._discovery_thread: Optional[threading.Thread] = None
        self._sleep_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 空闲睡眠巩固：外部活动（HTTP 请求）会推迟触发；watcher 增量同样算活动。
        self._last_activity_monotonic = time.monotonic()
        # 健康自监控（迭代55）：后台周期任务的成功/失败计数进 /stats，
        # 让「daemon 看起来活着但巩固一直在静默失败」这类问题可观测。
        self.health = {"sleep_cycles": 0, "sleep_errors": 0,
                       "last_sleep_report_at": ""}
        self._last_sleep_run: Optional[float] = None
        self.last_sleep_report: Optional[Dict[str, Any]] = None
        # Latest controlled auto-install outcome (per detected agent); only
        # written by the discovery loop, read by status endpoints.
        self.auto_install: Dict[str, Any] = {}
        self.token = self._load_or_create_token() if self.require_auth else ""
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
            # 只读模式（LAN 镜像/巡检副本）：任何凭据都降级为只读共享视图。
            return AccessPolicy.read_only_shared() if policy is not None else None
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

    def start_watcher(self) -> None:
        if not self.watch_enabled or self._watch_thread is not None:
            return
        self._watch_thread = threading.Thread(target=self.watcher.run, name="hippomind-watcher", daemon=True)
        self._watch_thread.start()

    def start_discovery_loop(self) -> None:
        """Background low-frequency agent discovery; cached between scans."""
        if self._discovery_thread is not None:
            return

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
                        self._auto_install_detected(report)
                except Exception:
                    # Discovery must never take the hub down; the cached
                    # report stays valid until the next successful scan.
                    pass
                self._stop_event.wait(check_interval)

        self._discovery_thread = threading.Thread(target=loop, name="hippomind-discovery", daemon=True)
        self._discovery_thread.start()

    def append_metrics_trend(self) -> Optional[Dict[str, Any]]:
        """把关键指标追加到 runtime_dir/metrics.jsonl（每小时至多一行）。"""
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
            with self._metrics_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._last_metrics_log = now
            return entry
        except Exception:
            return None

    def start_metrics_trend_thread(self, *, tick_seconds: float = 60.0) -> None:
        """后台线程：每分钟醒来检查是否到了写趋势行的时间（每小时一行）。"""

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
        }

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
        outcome: Dict[str, Any] = {"reloaded_at": utc_now()}
        try:
            report = self.discovery.maybe_discover(force=True)
            outcome["discovery"] = self.discovery.summary()
            outcome["auto_install"] = self._auto_install_detected(report)
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
        now = time.monotonic()
        activity_sources = [self._last_activity_monotonic]
        last_import = getattr(self.watcher, "last_import_monotonic", None)
        if last_import is not None:
            activity_sources.append(last_import)
        idle_for = now - max(activity_sources)
        since_last = (now - self._last_sleep_run
                      if self._last_sleep_run is not None else float("inf"))
        if idle_for < SLEEP_IDLE_SECONDS or since_last < SLEEP_IDLE_SECONDS:
            return None
        self._last_sleep_run = now
        report: Dict[str, Any] = {"triggered_at": utc_now(),
                                  "idle_seconds": round(idle_for, 1)}
        try:
            maintained = self.ledger.maintain(archive_raw=True)
            report["consolidated"] = len(self.ledger.consolidate())
            report["decayed"] = maintained.get("decayed", 0)
            report["raw_compressed"] = maintained.get("raw_compressed", 0)
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
        if self._sleep_thread is not None:
            return

        def loop() -> None:
            while not self._stop_event.is_set():
                try:
                    report = self._maybe_sleep_consolidate()
                    if report is not None and "error" not in report:
                        print(f"[hippomindd] 空闲巩固：规则 +{report['consolidated']}，"
                              f"衰减 {report['decayed']} 行，raw 归档 "
                              f"{report['raw_compressed']} 条")
                except Exception:
                    pass
                self._stop_event.wait(60.0)

        self._sleep_thread = threading.Thread(target=loop, name="hippomind-sleep",
                                              daemon=True)
        self._sleep_thread.start()

    def shutdown(self) -> None:
        """优雅停机：停采集与监听后做 WAL TRUNCATE 检查点并关闭本线程读连接。

        账本写入是同步事务（无待刷队列），因此收尾重点是 WAL 侧车归零——
        下次启动无需恢复，备份也只需主库文件。任何一步失败都不应阻止
        进程退出。
        """
        self._stop_event.set()
        self.watcher.stop()
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, name="hippomind-shutdown", daemon=True).start()
        try:
            summary = self.ledger.maintenance_checkpoint()
            if summary.get("wal_truncated"):
                logging.getLogger("hippomindd").info("停机检查点：WAL 已归零")
        except Exception:
            pass
        try:
            self.ledger.close()
        except Exception:
            pass


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
            debug=bool(raw.get("debug", data.get("debug", False))),
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

    def _handle_get(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._write(200, {"status": "ok", "api_version": API_VERSION, "pid": os.getpid(),
                              "server_version": SERVER_VERSION, "watch_enabled": self.hub.watch_enabled,
                              "ledger_id": ledger_identity(self.hub.paths.ledger_path)})
            return
        if path == "/stats":
            policy = self._require_auth()
            if policy is not None:
                self._write(200, self.hub.stats(policy))
            return
        if path == "/graph":
            policy = self._require_auth()
            if policy is not None:
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
                self._write(200, self.hub.ledger.entity_graph(
                    subject, scope=scope, hops=hops, limit=limit))
            return
        if path == "/conflicts":
            # 冲突裁决入口只读视图：claim 元数据 + 记忆摘要，不含全文。
            policy = self._require_auth()
            if policy is not None:
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
        self._write(404, {"error": "unknown endpoint"})

    def _handle_post(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._write(405, {"error": "use GET /health"})
            return
        policy = self._require_auth()
        if policy is None:
            return
        data = self._read_json()
        if data is None:
            return
        try:
            if path == "/ingest":
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
                )
                self._write(200, {"hits": [self._public_memory(hit) for hit in hits],
                                  "offset": offset, "limit": limit})
            elif path == "/context-pack":
                agent = str(data.get("agent", "all") or "all")
                if not policy.admin:
                    if agent not in {"", "all", policy.agent_id}:
                        raise AccessDenied(f"agent {policy.agent_id} 不能读取 {agent} 的上下文开关")
                    agent = policy.agent_id
                try:
                    injection = self.hub.injection.status(agent)
                except LedgerError:
                    # A damaged local preference must never cause accidental
                    # context disclosure. The caller can keep working with no
                    # injected history and repair the setting explicitly.
                    injection = {"agent": agent, "enabled": False, "error": "invalid_state"}
                if not injection.get("enabled", False):
                    self._write(200, {"items": [], "text": "", "injection_enabled": False,
                                      "injection_agent": injection.get("agent", agent)})
                    return
                options = self._options(data)
                options.scope = policy.enforce_options(options.scope, raw_chat=options.raw_chat)
                pack = self.hub.contexts.build(str(data.get("query", "")), options)
                pack["injection_enabled"] = True
                pack["injection_agent"] = injection.get("agent", agent)
                self._write(200, pack)
            elif path == "/feedback":
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
        self.server.service.note_activity()
        self._limited(self._handle_get)

    def do_POST(self) -> None:
        self.server.service.note_activity()
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
    service.start_watcher()
    service.start_discovery_loop()
    service.start_metrics_trend_thread()
    service.start_sleep_loop()

    def stop_signal(_signum, _frame) -> None:
        service.shutdown()

    def reload_signal(_signum, _frame) -> None:
        # SIGHUP 热重载：强制重扫发现 + 受控自动安装；失败不中断 daemon。
        try:
            service.reload()
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
        service.watcher.stop()
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
