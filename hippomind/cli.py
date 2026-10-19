"""Command line interface for the long-running HippoMind Memory Hub."""

from __future__ import annotations

import argparse
import contextlib
import json
import tempfile
import getpass
import hashlib
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .agent_config import load_agent_config
from .agent_install import agent_install_status, install_agents
from .auto_context import (
    AUTO_MAX_CHARS,
    AUTO_MAX_ITEMS,
    AUTO_TRIGGER_MODES,
    PROMPT_CLOSE,
    PROMPT_OPEN,
    build_auto_context,
    agent_auto_rules,
    extract_memory_request,
    install_agent_rules,
    render_untrusted_context,
)
from .context_pack import DEFAULT_MAX_CHARS, DEFAULT_MAX_TOKENS, ContextPackBuilder, ContextPackOptions
from .daemon import (API_VERSION, RuntimePaths, SERVER_VERSION,
                     ledger_identity, load_runtime_state, read_local_token,
                     run_daemon)
from .injection import InjectionState
from .ledger import LedgerError, SCHEMA_VERSION, SQLiteLedger, utc_now
from .embeddings import HashingNGramEmbedding
from .provider_manifest import AgentDiscovery, integration_tier, provider_manifest
from .watcher import ArchiveWatcher, default_dsh_sessions_root


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _paths(args) -> RuntimePaths:
    config = _agent_settings(args)
    ledger = getattr(args, "ledger", None) or config.get("ledger") or None
    runtime_dir = getattr(args, "runtime_dir", None) or config.get("runtime_dir") or None
    return RuntimePaths.for_ledger(ledger, runtime_dir)


def _agent_settings(args) -> Dict[str, Any]:
    path = str(getattr(args, "agent_config", "") or "").strip()
    return load_agent_config(path) if path else {}


def _agent_token(args, paths: RuntimePaths) -> str:
    config = _agent_settings(args)
    return str(config.get("token", "")) if config else read_local_token(paths)


def _reject_restricted_direct(args, operation: str) -> None:
    if str(getattr(args, "agent_config", "") or "").strip():
        raise LedgerError(f"受限 Agent 不能绕过 daemon 直接执行 {operation}")


def _port(args, paths: RuntimePaths) -> int:
    state = load_runtime_state(paths)
    config = _agent_settings(args)
    return int(getattr(args, "port", None) or state.get("port") or config.get("port") or 8765)


def _request(paths: RuntimePaths, port: int, method: str, endpoint: str,
             body: Optional[Mapping[str, Any]] = None, auth: bool = True,
             token: Optional[str] = None) -> Dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{endpoint}", data=payload, method=method)
    request.add_header("Accept", "application/json")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if auth:
        supplied = token if token is not None else read_local_token(paths)
        if supplied:
            request.add_header("X-HippoMind-Token", supplied)
    try:
        with urllib.request.urlopen(request, timeout=4.0) as response:
            raw = response.read(600_000)
    except urllib.error.HTTPError as exc:
        raw = exc.read(32_000)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = {"error": f"HTTP {exc.code}"}
        raise LedgerError(str(data.get("error", f"HTTP {exc.code}"))) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise LedgerError("hippomindd 未运行或不可达") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LedgerError("hippomindd 返回了无效 JSON") from exc
    return data if isinstance(data, dict) else {"result": data}


def _health(paths: RuntimePaths, port: int) -> Dict[str, Any]:
    return _request(paths, port, "GET", "/health", auth=False)


def _daemon_serves_ledger(paths: RuntimePaths, port: int) -> bool:
    """True 仅当端口上的 daemon 正在服务目标账本本身。

    之前维护/恢复守卫只要端口上有任何 daemon 就拒绝，导致对一份
    完全无关的临时账本做维护也被误伤（测试与多账本场景）。
    daemon 不可达时视为安全（无并发写方）。
    """
    try:
        health = _health(paths, port)
    except LedgerError:
        return False
    actual = str(health.get("ledger_id", ""))
    return bool(actual) and actual == ledger_identity(paths.ledger_path)


def _health_for_paths(paths: RuntimePaths, port: int) -> Dict[str, Any]:
    """Refuse to reuse or stop a healthy daemon serving another ledger."""
    health = _health(paths, port)
    expected = ledger_identity(paths.ledger_path)
    actual = str(health.get("ledger_id", ""))
    if actual != expected:
        if actual:
            raise LedgerError("端口上的 hippomindd 正在服务另一份账本；请为该账本指定其他 --port")
        raise LedgerError("端口上的 hippomindd 版本无法确认账本归属；请使用 --restart 升级受管服务")
    return health


def _legacy_health_is_owned(paths: RuntimePaths, health: Mapping[str, Any], port: int) -> bool:
    """Permit one explicit upgrade of a pre-ledger-id daemon, never a reuse."""
    state = load_runtime_state(paths)
    try:
        state_ledger = str(Path(str(state.get("ledger", ""))).expanduser().resolve())
        expected_ledger = str(paths.ledger_path.expanduser().resolve())
    except OSError:
        return False
    return (state_ledger == expected_ledger and state.get("pid") == health.get("pid")
            and int(state.get("port", 0) or 0) == int(port))


@contextlib.contextmanager
def _daemon_lifecycle_lock(paths: RuntimePaths):
    """Serialize start/stop/restart across concurrently running agents."""
    lock_path = paths.state_path.parent / "hippomindd.lifecycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            fcntl = None
        yield
    finally:
        try:
            if "fcntl" in locals() and fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", help="SQLite/WAL 账本路径")
    parser.add_argument("--runtime-dir", help="daemon 状态/token 目录")
    parser.add_argument("--port", type=int, help="daemon HTTP 端口，默认 8765")
    parser.add_argument("--agent-config", help="受限 Agent 的 0600 JSON 凭据文件")


def cmd_graph(args) -> int:
    """实体图谱：从 claim 主语出发的因果/关联邻域（1–2 跳）。"""
    _reject_restricted_direct(args, "graph")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    report = ledger.entity_graph(
        str(args.subject), scope=args.scope or "shared",
        hops=max(1, min(2, int(args.hops))),
        limit=max(10, min(2000, int(args.limit))))
    if args.format == "json":
        _print(report)
        return 0
    if not report.get("found"):
        print(f"(未找到实体「{args.subject}」的活跃 claim)")
        return 0
    print(f"实体「{report['subject']}」· {len(report['nodes'])} 节点 · "
          f"{len(report['edges'])} 边 · {report['hops']} 跳")
    for node in report["nodes"]:
        hop = node.get("hop")
        mark = "● 起点" if hop == 0 else "○"
        print(f"  {mark} {node['subject']}  (claims={node['claims']}, "
              f"memories={node['memories']})")
    for edge in report["edges"]:
        print(f"  {edge['from']} --{edge['relation_type']}"
              f"({edge['confidence']:.2f})--> {edge['to']}")
    return 0


def cmd_version(args) -> int:
    """输出版本/schema/环境信息（诊断与支持用）。"""
    try:
        from .version import VERSION as pkg_version
    except Exception:
        pkg_version = SERVER_VERSION
    info = {
        "package": pkg_version,
        "server": SERVER_VERSION,
        "api": API_VERSION,
        "schema_version": SCHEMA_VERSION,
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }
    paths = _paths(args)
    ledger_path = paths.ledger_path
    info["ledger"] = str(ledger_path)
    if Path(ledger_path).exists():
        try:
            led = SQLiteLedger(ledger_path)
            stats = led.stats()
            info["ledger_memories"] = int(stats.get("memories", 0))
            info["ledger_events"] = int(stats.get("events", 0))
            info["journal_mode"] = str(stats.get("journal_mode", ""))
        except Exception as exc:
            info["ledger_error"] = str(exc)[:200]
    else:
        info["ledger_exists"] = False
    _print(info)
    return 0


def cmd_init(args) -> int:
    _reject_restricted_direct(args, "init")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    result: Dict[str, Any] = {"initialized": True, "stats": ledger.stats()}
    if args.import_json:
        result["import"] = ledger.import_json(args.import_json, source_agent=args.source_agent)
    _print(result)
    return 0


def cmd_start(args) -> int:
    _reject_restricted_direct(args, "start")
    paths = _paths(args)
    port = _port(args, paths)
    if args.restart:
        health, reused, log_path = _restart_daemon(
            paths, port, archive=args.archive, watch_interval=args.watch_interval, no_watch=args.no_watch,
            read_only=bool(getattr(args, "read_only", False)),
        )
        _print({"running": True, "restarted": not reused, "health": health,
                "state": str(paths.state_path), "log": str(log_path)})
        return 0
    if args.foreground:
        run_daemon(ledger_path=paths.ledger_path, runtime_dir=paths.state_path.parent,
                   port=port, archive_root=args.archive, watch=not args.no_watch,
                   watch_interval=args.watch_interval,
                   read_only=bool(getattr(args, "read_only", False)))
        return 0
    health, reused, log_path = _start_or_reuse_daemon(
        paths, port, archive=args.archive, watch_interval=args.watch_interval, no_watch=args.no_watch,
        read_only=bool(getattr(args, "read_only", False)),
    )
    _print({"running": True, "reused": reused, "health": health,
            "state": str(paths.state_path), "log": str(log_path)})
    return 0


def _start_or_reuse_daemon(paths: RuntimePaths, port: int, *, archive: Optional[str] = None,
                           watch_interval: float = 30.0, no_watch: bool = False,
                           read_only: bool = False) -> tuple[Dict[str, Any], bool, Path]:
    """Start once and return a usable daemon without emitting CLI output."""
    with _daemon_lifecycle_lock(paths):
        return _start_or_reuse_daemon_locked(paths, port, archive=archive,
                                             watch_interval=watch_interval, no_watch=no_watch,
                                             read_only=read_only)


def _start_or_reuse_daemon_locked(paths: RuntimePaths, port: int, *, archive: Optional[str] = None,
                                  watch_interval: float = 30.0, no_watch: bool = False,
                                  read_only: bool = False) -> tuple[Dict[str, Any], bool, Path]:
    """Lifecycle-lock-held implementation of :func:`_start_or_reuse_daemon`."""
    try:
        health = _health_for_paths(paths, port)
        return health, True, paths.state_path.parent / "hippomindd.log"
    except LedgerError as exc:
        if "另一份账本" in str(exc) or "无法确认账本归属" in str(exc):
            raise
    command = [sys.executable, "-m", "hippomind.daemon", "--ledger", str(paths.ledger_path),
               "--runtime-dir", str(paths.state_path.parent), "--port", str(port),
               "--watch-interval", str(watch_interval)]
    if archive:
        command.extend(["--archive", archive])
    if no_watch:
        command.append("--no-watch")
    if read_only:
        command.append("--read-only")
    log_path = paths.state_path.parent / "hippomindd.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # The daemon deliberately avoids request bodies, but startup/errors can
    # still contain local paths and source diagnostics. Keep the log private
    # before handing it to the child process.
    try:
        log_path.touch(exist_ok=True)
        os.chmod(log_path, 0o600)
    except OSError:
        pass
    package_root = Path(__file__).resolve().parents[1]
    with log_path.open("ab") as log:
        subprocess.Popen(command, cwd=str(package_root), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 6.0
    last_error = ""
    while time.monotonic() < deadline:
        try:
            health = _health_for_paths(paths, port)
            return health, False, log_path
        except LedgerError as exc:
            last_error = str(exc)
            time.sleep(0.12)
    raise LedgerError(f"hippomindd 启动失败：{last_error}；请查看 {log_path}")


def _restart_daemon(paths: RuntimePaths, port: int, *, archive: Optional[str] = None,
                    watch_interval: float = 30.0, no_watch: bool = False,
                    verify_token: str = "") -> tuple[Dict[str, Any], bool, Path]:
    """Restart a locally managed daemon after a binary/schema upgrade.

    The caller must already have local administrator authority. This helper is
    also used by scoped preflight only after its credential is verified in the
    local ledger, preventing an arbitrary failed token from cycling a service.
    """
    with _daemon_lifecycle_lock(paths):
        log_path = paths.state_path.parent / "hippomindd.log"
        if verify_token:
            try:
                health = _health_for_paths(paths, port)
                _request(paths, port, "GET", "/stats", token=verify_token)
                # Another Agent may have completed the refresh while this
                # caller waited for the lifecycle lock. Do not cycle it again.
                return health, True, log_path
            except LedgerError:
                pass
        try:
            running_health = _health(paths, port)
            if (str(running_health.get("ledger_id", "")) != ledger_identity(paths.ledger_path)
                    and not _legacy_health_is_owned(paths, running_health, port)):
                raise LedgerError("端口上的 hippomindd 正在服务另一份账本；拒绝停止它")
            _request(paths, port, "POST", "/shutdown", {})
        except LedgerError as exc:
            if "另一份账本" in str(exc):
                raise
            # It may already be down, or the old process may close the
            # connection immediately after accepting shutdown. Wait below.
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                _health(paths, port)
                time.sleep(0.1)
            except LedgerError:
                break
        else:
            raise LedgerError("hippomindd 未在 5 秒内停止，拒绝并发启动第二个服务")
        return _start_or_reuse_daemon_locked(paths, port, archive=archive,
                                              watch_interval=watch_interval, no_watch=no_watch)


def _verified_scoped_credential(args, paths: RuntimePaths) -> bool:
    """Only a locally valid scoped config may request the upgrade self-heal."""
    if not str(getattr(args, "agent_config", "") or "").strip():
        return False
    try:
        return SQLiteLedger(paths.ledger_path).resolve_agent_credential(_agent_token(args, paths)) is not None
    except LedgerError:
        return False


def cmd_stop(args) -> int:
    _reject_restricted_direct(args, "stop")
    paths = _paths(args)
    port = _port(args, paths)
    try:
        _health_for_paths(paths, port)
        result = _request(paths, port, "POST", "/shutdown", {}, token=_agent_token(args, paths))
    except LedgerError as exc:
        if "另一份账本" in str(exc) or "无法确认账本归属" in str(exc):
            raise
        # A process can close its listening socket just after accepting the
        # response. Treat that transport race as pending shutdown, then verify
        # with health rather than incorrectly reporting a failed stop.
        result = {"status": "shutdown_pending", "transport": str(exc)}
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            _health_for_paths(paths, port)
            time.sleep(0.1)
        except LedgerError:
            break
    try:
        _health_for_paths(paths, port)
        result["running"] = True
        result["message"] = "daemon 仍在运行"
    except LedgerError:
        result["running"] = False
    _print(result)
    return 0


def _monitor_snapshot(paths, port, args) -> Dict[str, Any]:
    """单次巡检快照：daemon 健康 + 账本规模。"""
    health = _health(paths, port)
    stats = _request(paths, port, "GET", "/stats", token=_agent_token(args, paths))
    ledger_stats = stats.get("ledger", stats) if isinstance(stats, dict) else {}
    return {
        "time": utc_now(),
        "status": str(health.get("status", "?")),
        "memories": int(ledger_stats.get("memories", 0) or 0),
        "events": int(ledger_stats.get("events", 0) or 0),
    }


def cmd_monitor(args) -> int:
    """只读巡检循环：每 interval 秒打印一行摘要，Ctrl-C 退出。

    --json：只输出一次机器可读快照（time/status/memories/events），
    daemon 离线时退出码 1——供 cron/脚本消费。
    """
    paths = _paths(args)
    port = _port(args, paths)
    as_json = bool(getattr(args, "json", False))
    interval = max(1.0, float(getattr(args, "interval", 5.0)))
    rounds = int(getattr(args, "rounds", 0) or 0)
    if as_json:
        try:
            snap = _monitor_snapshot(paths, port, args)
        except LedgerError as exc:
            _print({"status": "offline", "error": str(exc)})
            return 1
        _print(snap)
        return 0 if snap["status"] == "ok" else 1
    print(f"hippomind monitor：每 {interval:g}s 巡检一次"
          + (f"，共 {rounds} 轮" if rounds else "，Ctrl-C 退出"))
    try:
        done = 0
        while True:
            try:
                snap = _monitor_snapshot(paths, port, args)
                print(f"[{snap['time'][11:19]}] {snap['status']}  "
                      f"memories={snap['memories']:,}  events={snap['events']:,}",
                      flush=True)
            except LedgerError as exc:
                print(f"[{utc_now()[11:19]}] OFFLINE: {exc}", flush=True)
            done += 1
            if rounds and done >= rounds:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nmonitor 结束")
    return 0


def cmd_status(args) -> int:
    paths = _paths(args)
    state = load_runtime_state(paths)
    port = _port(args, paths)
    try:
        health = _health_for_paths(paths, port)
        output = {"running": True, "health": health, "state": state}
        try:
            output["stats"] = _request(paths, port, "GET", "/stats", token=_agent_token(args, paths))
        except LedgerError:
            output["stats"] = {"available": False}
    except LedgerError as exc:
        output = {"running": False, "state": state, "message": str(exc)}
    _print(output)
    return 0


def _secure_local_file(path: Path) -> tuple[bool, str]:
    """Check a local control file without ever reading its secret value."""
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return False, "未创建"
    except OSError as exc:
        return False, f"无法读取文件状态：{exc}"
    if path.is_symlink() or not path.is_file():
        return False, "必须是普通文件，不能是符号链接"
    if os.name != "nt" and stat.st_mode & 0o077:
        return False, "权限过宽（应为 0600）"
    return True, "存在且权限为 0600"


def cmd_doctor(args) -> int:
    """Give users a compact, read-only readiness report.

    Unlike status this command intentionally omits raw paths, tokens and full
    agent metadata. Deep mode adds an aggregate raw-evidence scan, still
    without printing any message content.
    """
    paths = _paths(args)
    checks = []
    health: Dict[str, Any] = {}
    stats: Dict[str, Any] = {}
    ledger_ok = False

    ledger_secure, ledger_detail = _secure_local_file(paths.ledger_path)
    if ledger_secure:
        try:
            stats = SQLiteLedger(paths.ledger_path).stats()
            ledger_ok = True
            journal = str(stats.get("journal_mode", "")).upper()
            schema = stats.get("schema_version", "?")
            ledger_detail = f"schema {schema} · {journal or '未知日志模式'} · {stats.get('events', 0)} 个事件"
        except (LedgerError, OSError, ValueError) as exc:
            ledger_detail = f"账本不可读：{str(exc)[:180]}"
    checks.append({"id": "ledger", "ok": ledger_ok, "severity": "ok" if ledger_ok else "error",
                   "detail": ledger_detail})

    try:
        health = _health_for_paths(paths, _port(args, paths))
        daemon_ok = str(health.get("status", "")) == "ok"
        daemon_detail = (f"运行中 · v{health.get('server_version', '?')} · "
                         f"watcher={'开' if health.get('watch_enabled') else '关'}")
    except LedgerError as exc:
        daemon_ok = False
        daemon_detail = str(exc)[:180]
    checks.append({"id": "daemon", "ok": daemon_ok, "severity": "ok" if daemon_ok else "error",
                   "detail": daemon_detail})

    token_ok, token_detail = _secure_local_file(paths.token_path)
    checks.append({"id": "token", "ok": token_ok, "severity": "ok" if token_ok else "warning",
                   "detail": token_detail})

    try:
        injection = InjectionState(paths.state_path.parent).status("all")
        injection_detail = "默认注入已开启" if injection["enabled"] else "默认注入已关闭（采集仍继续）"
        injection_ok = True
    except LedgerError as exc:
        injection = {"enabled": False}
        injection_detail = str(exc)[:180]
        injection_ok = False
    checks.append({"id": "injection", "ok": injection_ok, "severity": "ok" if injection_ok else "error",
                   "detail": injection_detail})

    kinds = stats.get("kinds", {}) if isinstance(stats, Mapping) else {}
    tool_rows = int(kinds.get("tool_output", 0) or 0)
    raw_detail = "结构化记忆默认隔离 raw/tool/system"
    raw_severity = "ok"
    if tool_rows:
        raw_detail = f"历史账本仍有 {tool_rows} 条 tool_output 投影；需要时运行 scan-raw/redact-raw"
        raw_severity = "warning"
    deep_scan = None
    if args.deep and ledger_ok:
        deep_scan = SQLiteLedger(paths.ledger_path).scan_raw_secrets()
        if int(deep_scan.get("affected", 0)):
            raw_detail += f"；扫描发现 {deep_scan['affected']} 条 raw 证据待清理"
            raw_severity = "warning"
        else:
            raw_detail += "；深度扫描未发现待处理敏感值"
    checks.append({"id": "privacy", "ok": raw_severity == "ok", "severity": raw_severity,
                   "detail": raw_detail})

    warnings = [item["detail"] for item in checks if item["severity"] == "warning"]
    errors = [item["detail"] for item in checks if item["severity"] == "error"]
    payload = {
        "version": "0.9.2", "ready": not errors,
        "checks": checks, "warnings": warnings, "errors": errors,
        "ledger": {"events": stats.get("events", 0), "active": stats.get("active", 0),
                    "raw_chat": stats.get("raw_chat", 0), "kinds": kinds},
        "deep_scan": deep_scan,
    }
    if args.format == "json":
        _print(payload)
    else:
        print("HippoMind doctor v0.9.2")
        for item in checks:
            marker = "OK" if item["severity"] == "ok" else "WARN" if item["severity"] == "warning" else "FAIL"
            print(f"[{marker}] {item['id']}: {item['detail']}")
        print("结论: " + ("READY，可正常使用" if payload["ready"] else "NEEDS ATTENTION，请先处理 FAIL 项"))
    return 0


def _ingest_record(args) -> Dict[str, Any]:
    if args.file:
        with Path(args.file).expanduser().open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    elif args.json:
        data = json.loads(args.json)
    else:
        if not args.content:
            raise LedgerError("ingest 需要 --content、--json 或 --file")
        data = {
            "content": args.content, "kind": args.kind, "source_agent": args.agent,
            "session_id": args.session, "task_id": args.task, "project": args.project,
            "role": args.role, "scope": args.scope, "evidence": args.evidence,
        }
    if isinstance(data, list):
        return {"records": data}
    if isinstance(data, Mapping):
        return dict(data)
    raise LedgerError("ingest 输入必须是对象或对象列表")


def cmd_export(args) -> int:
    """导出记忆为 JSONL（ingest 兼容）或可回放 SQL（审计友好）。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    fmt = getattr(args, "format", "jsonl") or "jsonl"
    if fmt == "sql":
        payload = ledger.export_sql(scope=args.scope,
                                    include_raw_chat=args.include_raw_chat,
                                    limit=args.limit,
                                    since=getattr(args, "since", ""))
        rows = [{"count": payload.count("INSERT OR IGNORE INTO memories")}]
    else:
        rows = ledger.export_rows(scope=args.scope,
                                  include_raw_chat=args.include_raw_chat,
                                  limit=args.limit,
                                  since=getattr(args, "since", ""))
        payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True)
                            for row in rows)
    encrypted = bool(getattr(args, "encrypt", False))
    if encrypted:
        from .crypto_stream import encrypt_bytes
        passphrase = getattr(args, "passphrase", "") or getpass.getpass(
            "导出口令: ")
        if not passphrase:
            raise LedgerError("加密导出需要非空口令")
        blob = encrypt_bytes(payload.encode("utf-8"), passphrase)
    if str(args.output) in ("-", ""):
        if encrypted:
            raise LedgerError("加密导出必须指定输出文件（不能写 stdout）")
        sys.stdout.write(payload + ("\n" if payload else ""))
    else:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + out_path.name + ".",
                                         suffix=".tmp", dir=str(out_path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                data = blob if encrypted else payload.encode("utf-8")
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, out_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        exported_count = (rows[0]["count"] if fmt == "sql" else len(rows))
        result = {"exported": exported_count, "out": str(out_path),
                  "format": fmt}
        if encrypted:
            result["encrypted"] = True
            result["note"] = "混淆级保护；高敏感迁移请配合 age/gpg"
        _print(result)
    return 0


def cmd_import(args) -> int:
    """从 JSONL 导入记忆；content_hash 幂等，重复导入自动去重。

    每条导入记忆的 metadata 会带上 import_batch（源文件内容指纹）与
    import_source（文件路径/stdin），供审计与按批回滚定位。
    """
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    def _stamp(record: dict, batch: str, origin: str) -> dict:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        metadata.setdefault("import_batch", batch)
        metadata.setdefault("import_source", origin)
        record["metadata"] = metadata
        return record

    if args.input in ("-", ""):
        source = sys.stdin
        raw_for_fingerprint = "<stdin>"
        batch_id = "imp-stdin-" + utc_now().replace(":", "")[:19]
    else:
        source_path = Path(args.input).expanduser()
        raw_bytes = source_path.read_bytes()
        from .crypto_stream import decrypt_bytes, is_encrypted
        if is_encrypted(raw_bytes):
            passphrase = getattr(args, "passphrase", "") or getpass.getpass(
                "导入口令: ")
            try:
                plain = decrypt_bytes(raw_bytes, passphrase)
            except ValueError as exc:
                raise LedgerError(str(exc))
            text = plain.decode("utf-8")
            batch_id = "imp-" + hashlib.sha256(raw_bytes).hexdigest()[:12]
            raw_for_fingerprint = str(source_path)
            inserted = duplicate = rejected = 0
            batch: list = []
            for line_number, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    rejected += 1
                    continue
                if isinstance(record, dict):
                    batch.append(_stamp(record, batch_id, raw_for_fingerprint))
            if batch:
                res = ledger.ingest_many(batch)
                inserted += sum(1 for x in res if x.inserted)
                duplicate += sum(1 for x in res if not x.inserted)
            _print({"imported": inserted, "duplicate": duplicate,
                    "rejected": rejected, "batch": batch_id,
                    "decrypted": True})
            return 0
        source = source_path.open("r", encoding="utf-8")
        batch_id = "imp-" + hashlib.sha256(raw_bytes).hexdigest()[:12]
        raw_for_fingerprint = str(source_path)
    inserted = duplicate = rejected = 0
    batch: list = []

    try:
        for line_number, line in enumerate(source, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                # 整行解析失败：若是单对象 JSON（旧版 Hub 导出格式），整文件转交
                # import_json 兼容路径处理。
                if line.lstrip().startswith("{") and line_number == 1:
                    source.close()
                    result = ledger.import_json(args.input,
                                                source_agent=args.source_agent)
                    result["batch"] = batch_id + "-legacy"
                    _print(result)
                    return 0
                rejected += 1
                if rejected <= 3:
                    print(f"[跳过] 第 {line_number} 行不是合法 JSON: {exc}")
                continue
            if not isinstance(record, dict):
                rejected += 1
                continue
            # 旧版 Hub 导出（单对象 JSON，含 hippomind_memory_hub_export 键）：
            # 整文件转交 import_json 兼容路径。
            if "hippomind_memory_hub_export" in record:
                source.close()
                result = ledger.import_json(args.input,
                                            source_agent=args.source_agent)
                result["batch"] = batch_id + "-legacy"
                _print(result)
                return 0
            if args.scope:
                record["scope"] = args.scope
            batch.append(_stamp(record, batch_id, raw_for_fingerprint))
            if len(batch) >= 500:
                result = ledger.ingest_many(batch)
                inserted += sum(1 for r in result if r.inserted)
                duplicate += sum(1 for r in result if not r.inserted)
                batch.clear()
        if batch:
            result = ledger.ingest_many(batch)
            inserted += sum(1 for r in result if r.inserted)
            duplicate += sum(1 for r in result if not r.inserted)
    finally:
        if source is not sys.stdin:
            source.close()
    _print({"imported": inserted, "duplicate": duplicate, "rejected": rejected,
            "batch": batch_id})
    return 0


def cmd_facts(args) -> int:
    """事实投影：列出活跃 claim 的结构化视图。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    facts = ledger.list_facts(scope=args.scope, subject=args.subject,
                              limit=args.limit)
    if args.format == "json":
        _print({"facts": facts})
        return 0
    for fact in facts:
        print(f"[{fact['subject']}] {fact['predicate']}.{fact['slot_key']} = "
              f"{json.dumps(fact['value'], ensure_ascii=False)}  "
              f"(置信 {fact['confidence']:.2f} · {fact['source_agent']})")
    print(f"-- 共 {len(facts)} 条事实")
    return 0


def cmd_skills(args) -> int:
    """技能投影：列出 procedural 记忆的结构化视图。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    skills = ledger.list_skills(scope=args.scope, limit=args.limit)
    if args.format == "json":
        _print({"skills": skills})
        return 0
    for skill in skills:
        print(f"◆ {skill['content'][:60]}  (置信 {skill['confidence']:.2f} · "
              f"{skill['source_agent']})")
        for trigger in skill["trigger_pattern"][:3]:
            print(f"   触发: {trigger[:70]}")
        for step in skill["execution_pipeline"][:5]:
            action = step.get("action", "") if isinstance(step, dict) else str(step)
            print(f"   步骤: {action[:70]}")
    print(f"-- 共 {len(skills)} 条技能")
    return 0


def cmd_forget(args) -> int:
    """遗忘请求：软遗忘移除投影并落审计；--purge 连同事件内容一并清除。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    result = ledger.forget(scope=args.scope, before_time=args.before,
                           memory_ids=args.memory_id or [],
                           source_agent=args.source_agent, purge=args.purge,
                           import_batch=getattr(args, "batch", ""),
                           dry_run=bool(getattr(args, "dry_run", False)))
    _print(result)
    return 0


def cmd_resolve(args) -> int:
    """人工裁决 Slot-Value 冲突：本地直接操作账本（管理员维护操作）。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    result = ledger.resolve_claim(args.claim, args.winner,
                                  source_agent=args.source_agent)
    _print(result)
    return 0


def cmd_ingest(args) -> int:
    paths = _paths(args)
    payload = _ingest_record(args)
    try:
        result = _request(paths, _port(args, paths), "POST", "/ingest", payload,
                          token=_agent_token(args, paths))
    except LedgerError as exc:
        # daemon 不可达时安全直写；若 daemon 实际在跑（错误另有原因）则原样抛出。
        try:
            _health(paths, _port(args, paths))
            raise exc
        except LedgerError:
            pass
        _reject_restricted_direct(args, "ingest 离线直写")
        ledger = SQLiteLedger(paths.ledger_path)
        ir = ledger.ingest(payload)
        result = {"results": [{"event_id": ir.event_id,
                               "memory_id": ir.memory_id,
                               "inserted": ir.inserted,
                               "content_hash": ir.content_hash}],
                  "mode": "offline-direct"}
    _print(result)
    return 0


def _pack_body(args) -> Dict[str, Any]:
    return {
        "query": args.query,
        "options": {
            "max_items": args.max_items, "max_chars": args.max_chars, "max_tokens": args.max_tokens,
            "raw_chat": args.raw_chat,
            "require_evidence": not args.allow_no_evidence, "scope": args.scope,
            "project": args.project, "task_id": args.task, "source_agents": args.source_agent,
            "from_time": args.from_time, "to_time": args.to_time,
            "as_of": getattr(args, "as_of", "") or "",
            "debug": args.debug,
        },
    }


def cmd_context(args) -> int:
    paths = _paths(args)
    body = _pack_body(args)
    if args.offline:
        _reject_restricted_direct(args, "context --offline")
        options = ContextPackOptions(**body["options"])
        builder = ContextPackBuilder(
            SQLiteLedger(paths.ledger_path),
            embedder=(HashingNGramEmbedding()
                      if getattr(args, "enable_ngram_vector", False) else None))
        result = builder.build(args.query, options)
    else:
        result = _request(paths, _port(args, paths), "POST", "/context-pack", body,
                          token=_agent_token(args, paths))
    if args.text_only:
        print(result.get("text", ""))
    else:
        _print(result)
    return 0


def _auto_context_request(args) -> str:
    """Read the current request without forcing a fragile shell-quoted arg."""
    request = str(getattr(args, "request", "") or "")
    if bool(getattr(args, "stdin", False)):
        if request.strip():
            raise LedgerError("auto-context 的 request 参数不能与 --stdin 同时使用")
        request = sys.stdin.read(16_385)
        if len(request) > 16_384:
            raise LedgerError("auto-context stdin 超过 16 KiB 上限")
    request = request.strip()
    if not request:
        raise LedgerError("auto-context 需要本轮完整用户要求，或使用 --stdin")
    return request


def _auto_context_body(args, request: str) -> Dict[str, Any]:
    requested_chars = max(200, min(AUTO_MAX_CHARS, int(args.max_chars)))
    payload_chars = max(200, requested_chars - len(PROMPT_OPEN) - len(PROMPT_CLOSE) - 2)
    return {
        # Every native adapter sends the same marker-selected text. Workspace
        # hints used to differ between Pi and Codex and could reorder otherwise
        # identical shared candidates.
        "query": request,
        "options": {
            "max_items": max(1, min(AUTO_MAX_ITEMS, int(args.max_items))),
            "max_chars": payload_chars,
            "raw_chat": False,
            "require_evidence": True,
            "scope": args.scope,
            "project": args.project,
            "task_id": args.task,
            "debug": args.debug,
        },
    }


def fetch_auto_context(args, request: str) -> Dict[str, Any]:
    """Resolve a strict Context Pack for a current natural-language request.

    This is shared by the CLI and Codex's native UserPromptSubmit hook.
    It deliberately keeps the existing fail-closed behavior for a scoped Agent:
    if its daemon credential cannot be used, no offline master-ledger fallback
    is attempted.
    """
    trigger_mode = str(getattr(args, "trigger_mode", "always") or "always")
    decision = extract_memory_request(request, mode=trigger_mode)
    bounded_chars = max(200, min(AUTO_MAX_CHARS, int(getattr(args, "max_chars", AUTO_MAX_CHARS))))
    bounded_items = max(1, min(AUTO_MAX_ITEMS, int(getattr(args, "max_items", AUTO_MAX_ITEMS))))
    if not decision["triggered"]:
        return {"items": [], "text": "", "auto_context": {
            "agent": getattr(args, "agent", ""), "source": "not_triggered", "daemon_started": False,
            "raw_chat": False, "max_items": bounded_items, "max_chars": bounded_chars,
            "injection_enabled": False, "triggered": False,
            "trigger_mode": decision["mode"], "trigger_reason": decision["reason"],
        }}
    paths = _paths(args)
    # Collection and maintenance stay online even when an Agent opts out of
    # prompt injection.  A malformed switch fails closed for privacy.
    try:
        injection = InjectionState(paths.state_path.parent).status(getattr(args, "agent", "all"))
    except LedgerError as exc:
        return {"items": [], "text": "", "auto_context": {
            "agent": getattr(args, "agent", ""), "source": "disabled", "daemon_started": False,
            "raw_chat": False, "max_items": max(1, min(AUTO_MAX_ITEMS, int(args.max_items))),
            "max_chars": max(200, min(AUTO_MAX_CHARS, int(args.max_chars))),
            "injection_enabled": False, "disabled_reason": str(exc)[:240],
        }}
    if not injection["enabled"]:
        return {"items": [], "text": "", "auto_context": {
            "agent": getattr(args, "agent", ""), "source": "disabled", "daemon_started": False,
            "raw_chat": False, "max_items": max(1, min(AUTO_MAX_ITEMS, int(args.max_items))),
            "max_chars": max(200, min(AUTO_MAX_CHARS, int(args.max_chars))),
            "injection_enabled": False,
        }}
    port = _port(args, paths)
    restricted = bool(str(getattr(args, "agent_config", "") or "").strip())
    if args.offline and restricted:
        raise LedgerError("受限 Agent 不能使用 auto-context --offline 绕过 daemon")
    request = decision["query"]
    if not request:
        raise LedgerError("auto-context 需要本轮完整用户要求")
    body = _auto_context_body(args, request)
    source = "offline"
    daemon_started = False
    result: Optional[Dict[str, Any]] = None
    start_error = ""
    if not args.offline:
        try:
            result = _request(paths, port, "POST", "/context-pack", body,
                              token=_agent_token(args, paths))
            source = "daemon"
        except LedgerError as exc:
            start_error = str(exc)
            # An old daemon from before scoped credentials only understands the
            # local master token. If the local ledger proves this scoped token
            # is valid, refresh that stale process once and retry safely.
            if (restricted and "local authorization token required" in start_error and
                    _verified_scoped_credential(args, paths)):
                try:
                    _health_result, reused, _log_path = _restart_daemon(
                        paths, port, archive=args.archive, watch_interval=args.watch_interval,
                        no_watch=args.no_watch, verify_token=_agent_token(args, paths),
                    )
                    daemon_started = not reused
                    result = _request(paths, port, "POST", "/context-pack", body,
                                      token=_agent_token(args, paths))
                    source = "daemon"
                except LedgerError as restart_exc:
                    start_error = str(restart_exc)
            elif not args.no_start:
                try:
                    _health_result, reused, _log_path = _start_or_reuse_daemon(
                        paths, port, archive=args.archive, watch_interval=args.watch_interval,
                        no_watch=args.no_watch,
                    )
                    daemon_started = not reused
                    result = _request(paths, port, "POST", "/context-pack", body,
                                      token=_agent_token(args, paths))
                    source = "daemon"
                except LedgerError as start_exc:
                    start_error = str(start_exc)
    if result is None and restricted:
        # Fail closed for a scoped integration. An empty pack lets the host
        # continue without silently reopening the ledger as local admin.
        result = {"items": [], "text": ""}
        source = "unavailable"
    elif result is None:
        result = build_auto_context(
            SQLiteLedger(paths.ledger_path), request, scope=args.scope, project=args.project,
            task_id=args.task, max_items=args.max_items, max_chars=args.max_chars, debug=args.debug,
            cwd=str(getattr(args, "cwd", "") or ""),
        )
    result["auto_context"] = {
        "agent": args.agent,
        "source": source,
        "daemon_started": daemon_started,
        "raw_chat": False,
        "max_items": max(1, min(AUTO_MAX_ITEMS, int(args.max_items))),
        "max_chars": max(200, min(AUTO_MAX_CHARS, int(args.max_chars))),
        "injection_enabled": True,
        "triggered": True,
        "trigger_mode": decision["mode"],
        "trigger_reason": decision["reason"],
    }
    if start_error and source in {"offline", "unavailable"}:
        result["auto_context"]["daemon_error"] = start_error[:400]
    return result


def cmd_auto_context(args) -> int:
    """Preflight a request, gated by the configured memory marker policy."""
    request = _auto_context_request(args)
    result = fetch_auto_context(args, request)
    if args.format == "json":
        _print(result)
    elif args.format == "text":
        print(result.get("text", ""))
    else:
        rendered = render_untrusted_context(result, max_chars=args.max_chars)
        if rendered:
            print(rendered)
    return 0


def cmd_inject(args) -> int:
    """Change only automatic Context Pack injection."""
    _reject_restricted_direct(args, "inject")
    paths = _paths(args)
    _print(InjectionState(paths.state_path.parent).update(args.action, args.agent))
    return 0


def cmd_install_auto(args) -> int:
    """Install a managed explicit-marker rule for one configured Agent."""
    target = Path(args.agents_file).expanduser() if args.agents_file else Path.home() / ".codex" / "AGENTS.md"
    package_root = Path(__file__).resolve().parents[1]
    if args.print_only:
        config = Path(args.agent_config).expanduser() if args.agent_config else None
        print(agent_auto_rules(package_root, agent=args.agent, agent_config=config), end="")
        return 0
    _reject_restricted_direct(args, "install-auto")
    if not args.agents_file:
        paths = _paths(args)
        result = install_agents(SQLiteLedger(paths.ledger_path), paths, [args.agent],
                                package_root=package_root)[0]
        result["behavior"] = "仅在普通用户文本包含记忆：或记忆:时预取并注入 Context Pack"
        _print(result)
        return 0
    changed, path = install_agent_rules(
        target, package_root, agent=args.agent,
        agent_config=Path(args.agent_config).expanduser() if args.agent_config else None,
    )
    _print({"installed": True, "changed": changed, "path": str(path), "agent": args.agent,
            "behavior": "仅在普通用户文本包含记忆：或记忆:时预取并注入 Context Pack"})
    return 0


def cmd_sync(args) -> int:
    paths = _paths(args)
    try:
        result = _request(paths, _port(args, paths), "POST", "/sync", {},
                          token=_agent_token(args, paths))
    except LedgerError:
        _reject_restricted_direct(args, "sync")
        result = ArchiveWatcher(SQLiteLedger(paths.ledger_path), args.archive,
                                poll_interval=args.watch_interval,
                                dsh_sessions_root=default_dsh_sessions_root()).sync_once()
    _print(result)
    return 0


def cmd_watch(args) -> int:
    _reject_restricted_direct(args, "watch")
    watcher = ArchiveWatcher(SQLiteLedger(_paths(args).ledger_path), args.archive,
                             poll_interval=args.watch_interval,
                             dsh_sessions_root=default_dsh_sessions_root())
    if args.once:
        _print(watcher.sync_once())
        return 0
    try:
        watcher.run()
    except KeyboardInterrupt:
        watcher.stop()
    return 0


def cmd_pin(args) -> int:
    """置顶/取消置顶一条记忆（操作员手动加权）。"""
    _reject_restricted_direct(args, "pin")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    result = ledger.pin_memory(args.memory_id, pinned=not args.unpin)
    _print(result)


def cmd_conflicts(args) -> int:
    """列出冲突/被取代的 claim 谱系（resolve 的前置视图）。"""
    _reject_restricted_direct(args, "conflicts")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    report = ledger.list_conflicts(scope=args.scope or "shared",
                                   limit=max(1, min(1000, int(args.limit))))
    if args.format == "json":
        _print(report)
    else:
        if not report["items"]:
            print("(无冲突或被取代的 claim)")
        for item in report["items"]:
            preview = str(item.get("content") or "")[:50]
            print(f"{item['claim_id'][:18]}  [{item['state']}]  "
                  f"{item['subject']}.{item['predicate']}.{item['slot_key']}  "
                  f"conf={item['confidence']:.2f}  {preview}")
        print(f"共 {report['total']} 条", file=sys.stderr)
    return 0


def cmd_pins(args) -> int:
    """列出全部置顶记忆（跨 scope 概览）。"""
    _reject_restricted_direct(args, "pins")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    rows = ledger.list_pinned(scope=args.scope or "shared",
                              limit=max(1, min(1000, int(args.limit))))
    if args.format == "json":
        _print({"pinned": rows, "total": len(rows)})
    else:
        if not rows:
            print("(无置顶记忆)")
        for row in rows:
            print(f"{row['memory_id']}  [{row['kind']}]  {str(row['content'])[:60]}")
        print(f"共 {len(rows)} 条", file=sys.stderr)
    return 0


def cmd_checkup(args) -> int:
    """账本深度体检：完整性/FTS 同步/游标健康/声明一致性（默认只读）。"""
    _reject_restricted_direct(args, "checkup")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    repair_note = None
    if getattr(args, "repair", False):
        try:
            report = ledger.fts_sync_report()
            if report.get("unindexed"):
                outcome = ledger.repair_fts()
                repair_note = {"triggered": True, **outcome}
            else:
                repair_note = {"triggered": False, "reason": "FTS 已同步，无需重建"}
        except Exception as exc:
            repair_note = {"triggered": False, "error": str(exc)[:200]}
    checks: dict = {}
    failed = False

    # 1) SQLite 结构完整性（--repair 在进入检查前已执行，此处即修复后状态）
    try:
        result = ledger.integrity_check()
        checks["integrity"] = result
        if str(result.get("status", "")).lower() != "ok":
            failed = True
    except Exception as exc:
        checks["integrity"] = {"status": "error", "detail": str(exc)[:200]}
        failed = True

    # 2) FTS 同步：投影行 vs FTS 行
    try:
        checks["fts_sync"] = ledger.fts_sync_report()
    except Exception as exc:
        checks["fts_sync"] = {"status": "error", "detail": str(exc)[:200]}
        failed = True

    # 3) 游标健康：路径存在性
    try:
        cursors = ledger.list_cursors()
        missing = [c.get("source_path", "") for c in cursors
                   if c.get("source_path") and not Path(c["source_path"]).exists()]
        checks["cursors"] = {"status": "ok" if cursors else "empty",
                             "total": len(cursors), "missing_paths": len(missing)}
    except Exception as exc:
        checks["cursors"] = {"status": "error", "detail": str(exc)[:200]}
        failed = True

    # 4) claim 一致性：活跃 claim 指向的记忆必须存在且未遗忘
    try:
        checks["claims"] = ledger.claims_consistency_report()
    except Exception as exc:
        checks["claims"] = {"status": "error", "detail": str(exc)[:200]}
        failed = True

    # 5) 概览计数
    try:
        overview = ledger.health_overview()
        checks["overview"] = overview
    except Exception as exc:
        checks["overview"] = {"status": "error", "detail": str(exc)[:200]}

    verdict = "fail" if failed else "ok"
    payload = {"checkup": verdict, "checks": checks}
    if repair_note is not None:
        payload["repair"] = repair_note
    fmt = getattr(args, "format", "json")
    if fmt == "md":
        sys.stdout.write(_render_checkup_md(payload))
    elif fmt == "junit":
        sys.stdout.write(_render_checkup_junit(payload))
    else:
        _print(payload)
    return 1 if failed else 0


def _render_checkup_md(payload: dict) -> str:
    """把 checkup 报告渲染成 markdown（贴 issue 友好）。"""
    lines = [f"# HippoMind Checkup：**{payload['checkup']}**", ""]
    labels = {"integrity": "SQLite 完整性", "fts_sync": "FTS 同步",
              "cursors": "游标健康", "claims": "Claim 一致性",
              "overview": "概览"}
    for name, detail in payload.get("checks", {}).items():
        title = labels.get(name, name)
        status = str(detail.get("status", "-"))
        lines.append(f"## {title} — `{status}`")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(detail, ensure_ascii=False, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    if "repair" in payload:
        lines.append("## Repair")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(payload["repair"], ensure_ascii=False,
                                indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _render_checkup_junit(payload: dict) -> str:
    """把 checkup 报告渲染成 JUnit XML（CI 集成）。"""
    from xml.sax.saxutils import escape, quoteattr

    def tc(name: str, detail: dict) -> str:
        # overview 等信息性条目没有 status 字段——视为通过；
        # 真实错误路径总是显式写入 status="error"。
        status = str(detail.get("status", "ok"))
        attrs = quoteattr(name)
        if status in ("ok", "empty"):
            return f'    <testcase name={attrs} status="{escape(status)}"/>'
        if status == "warn":
            msg = escape(str(detail.get("hint") or detail.get("note") or "warning"))
            return (f'    <testcase name={attrs} status="warn">\n'
                    f'      <skipped message={quoteattr(msg)}/>\n'
                    f'    </testcase>')
        msg = escape(str(detail.get("detail", status))[:300])
        return (f'    <testcase name={attrs} status="fail">\n'
                f'      <failure message={quoteattr(msg)}/>\n'
                f'    </testcase>')

    checks = payload.get("checks", {})
    cases = "".join(tc(n, d) for n, d in checks.items())
    failures = sum(1 for d in checks.values()
                   if str(d.get("status", "ok")) not in ("ok", "empty", "warn"))
    verdict = payload.get("checkup", "unknown")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuites name={quoteattr("hippomind-checkup")} '
            f'tests="{len(checks)}" failures="{failures}" '
            f'verdict={quoteattr(verdict)}>\n'
            f'  <testsuite name="checkup" tests="{len(checks)}" '
            f'failures="{failures}">\n'
            f'{cases}\n  </testsuite>\n</testsuites>\n')


def _backup_registry_path(paths) -> Path:
    return paths.state_path.parent / "backups.json"


def _load_backup_registry(paths) -> list:
    path = _backup_registry_path(paths)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_backup_registry(paths, entries: list) -> None:
    path = _backup_registry_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_backup(args) -> int:
    _reject_restricted_direct(args, "backup")
    path = SQLiteLedger(_paths(args).ledger_path).backup_to(args.output)
    # 校验和：跨机迁移或归档前可核对完整性（sha256sum -c 可直接消费）。
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    entries = [e for e in _load_backup_registry(_paths(args))
               if isinstance(e, dict) and e.get("path") != str(path)]
    entries.append({"path": str(path), "bytes": size,
                    "sha256": digest.hexdigest(),
                    "created_at": utc_now()})
    entries.sort(key=lambda e: str(e.get("created_at", "")), reverse=True)
    _save_backup_registry(_paths(args), entries[:200])
    _print({"backup": str(path), "bytes": size,
            "sha256": digest.hexdigest()})
    return 0


def cmd_redact_raw(args) -> int:
    """Apply the explicit, one-time security cleanup to raw chat evidence."""
    _reject_restricted_direct(args, "redact-raw")
    paths = _paths(args)
    if _daemon_serves_ledger(paths, _port(args, paths)):
        raise LedgerError("清理 raw 证据前请先运行 hippomind stop，避免与 daemon 并发写入")
    result = SQLiteLedger(paths.ledger_path).redact_raw_evidence(
        limit=args.max_chars, batch_size=args.batch_size)
    _print({"redacted_raw_evidence": result, "ledger": str(paths.ledger_path)})
    return 0


def cmd_scan_raw(args) -> int:
    """Report aggregate raw-evidence exposure without printing any content."""
    _reject_restricted_direct(args, "scan-raw")
    result = SQLiteLedger(_paths(args).ledger_path).scan_raw_secrets(batch_size=args.batch_size)
    _print({"raw_evidence_scan": result, "ledger": str(_paths(args).ledger_path)})
    return 0


def cmd_maintain(args) -> int:
    """Run bounded retention/archive maintenance while daemon is stopped."""
    if getattr(args, "vacuum", False):
        print("提示：VACUUM 前建议先 hippomind backup 创建备份；大库可能耗时数秒到数分钟。",
              file=sys.stderr)
    _reject_restricted_direct(args, "maintain")
    paths = _paths(args)
    if _daemon_serves_ledger(paths, _port(args, paths)):
        raise LedgerError("维护 raw 证据前请先运行 hippomind stop，避免与 daemon 并发写入")
    ledger = SQLiteLedger(paths.ledger_path)
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        # A dry run must not write.  Report the exact candidate counts and the
        # file sizes so the operator can decide before anything is touched.
        report = {
            "dry_run": True,
            "memories": ledger.stats()["memories"],
            "purge_candidates": ledger.purge_expired_raw(
                before_days=getattr(args, "raw_ttl_days", 0) or max(args.raw_retention_days, 90),
                dry_run=True,
            )["purge_candidates"],
        }
        try:
            report["duplicate_groups"] = ledger.duplicate_candidates().get("groups", -1)
        except Exception:
            report["duplicate_groups"] = -1
        _print(report)
        return 0
    result = ledger.maintain(
        raw_after_days=args.raw_retention_days,
        raw_capacity=args.raw_capacity,
        raw_archive_dir=args.raw_archive_dir or None,
        raw_archive_max_bytes=args.raw_archive_max_bytes,
        raw_ttl_days=getattr(args, "raw_ttl_days", 0) or None,
        purge_raw=bool(getattr(args, "purge_raw", False)),
    )
    if getattr(args, "dedupe", False):
        try:
            result["dedupe"] = ledger.collapse_duplicate_rows(
                archive_dir=args.raw_archive_dir or None)
        except (LedgerError, sqlite3.Error) as exc:
            result["dedupe"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    # 合并建议：只报告不自动删——合并语义需人工确认（哪条留谱系）。
    try:
        duplicates = ledger.duplicate_candidates()
    except Exception:
        duplicates = {"suggestions": -1}
    if getattr(args, "vacuum", False):
        result["vacuum"] = ledger.vacuum()
    keep = getattr(args, "prune_backups", 0) or 0
    if keep > 0:
        entries = sorted(
            (e for e in _load_backup_registry(paths) if isinstance(e, dict)),
            key=lambda e: str(e.get("created_at", "")), reverse=True)
        kept, removed, skipped = [], [], []
        for index, entry in enumerate(entries):
            f = Path(str(entry.get("path", "")))
            if index < keep:
                kept.append(entry)
                continue
            try:
                if f.exists() and _sha256_of(f) == entry.get("sha256"):
                    f.unlink()
                    removed.append(str(f))
                else:
                    # 文件缺失或内容已变——不动它，仅标记陈旧条目。
                    skipped.append(str(f))
                    kept.append(entry)
            except OSError:
                skipped.append(str(f))
                kept.append(entry)
        _save_backup_registry(paths, kept)
        result["prune_backups"] = {"kept": len(kept), "removed": len(removed),
                                   "skipped": len(skipped)}
    _print({"maintenance": result, "duplicate_candidates": duplicates,
            "ledger": str(paths.ledger_path)})
    return 0


def cmd_recovery_check(args) -> int:
    """Exercise WAL backup/open recovery without replacing the live ledger."""
    _reject_restricted_direct(args, "recovery-check")
    result = SQLiteLedger(_paths(args).ledger_path).recovery_check()
    _print({"recovery_check": result, "ledger": str(_paths(args).ledger_path)})
    return 0


def cmd_restore(args) -> int:
    _reject_restricted_direct(args, "restore")
    paths = _paths(args)
    if _daemon_serves_ledger(paths, _port(args, paths)):
        raise LedgerError("恢复前请先运行 hippomind stop，避免覆盖正在使用的账本")
    source = Path(args.input).expanduser()
    if not source.is_file():
        raise LedgerError(f"备份文件不存在: {source}")
    # 强制校验和核对（衔接 backup 输出的 sha256）：防恢复损坏/调包的备份。
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if getattr(args, "sha256", "") and args.sha256.lower() != actual:
        raise LedgerError(
            f"校验和不匹配：期望 {args.sha256[:16]}…，实际 {actual[:16]}…"
            "——备份可能损坏或被替换，拒绝恢复")
    SQLiteLedger(paths.ledger_path).restore_from(source)
    _print({"restored": str(source), "sha256": actual})
    return 0


def cmd_feedback(args) -> int:
    paths = _paths(args)
    body = {"memory_id": args.memory_id, "signal": args.signal, "note": args.note,
            "source_agent": args.source_agent, "feedback_id": args.feedback_id}
    try:
        result = _request(paths, _port(args, paths), "POST", "/feedback", body,
                          token=_agent_token(args, paths))
    except LedgerError as exc:
        # daemon 不可达时安全直写；daemon 在跑则原样抛出。
        try:
            _health(paths, _port(args, paths))
            raise exc
        except LedgerError:
            pass
        ledger = SQLiteLedger(paths.ledger_path)
        updated = ledger.feedback(body["memory_id"], float(body["signal"]),
                                  str(body.get("note", "")),
                                  str(body.get("source_agent", "")))
        result = {"updated": bool(updated), "mode": "offline-direct"}
    _print(result)
    return 0


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.hippomind.daemon.plist"


def _launchd_path_with_node() -> str:
    entries = []
    node = shutil.which("node")
    if node:
        entries.append(str(Path(node).resolve().parent))
    entries.extend(part for part in os.environ.get("PATH", "").split(os.pathsep) if part)
    entries.extend(("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    return os.pathsep.join(dict.fromkeys(entries))


def cmd_login_install(args) -> int:
    """Create an opt-in per-user LaunchAgent; it is never installed implicitly."""
    _reject_restricted_direct(args, "login-install")
    paths = _paths(args)
    plist_path = _launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = paths.state_path.parent / "hippomindd-launchd.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        os.chmod(log_path, 0o600)
    except OSError:
        pass
    package_root = Path(__file__).resolve().parents[1]
    config = {
        "Label": "com.hippomind.daemon",
        "ProgramArguments": [sys.executable, "-m", "hippomind.daemon", "--ledger", str(paths.ledger_path),
                             "--runtime-dir", str(paths.state_path.parent), "--port", str(_port(args, paths)),
                             "--watch-interval", str(args.watch_interval)],
        "RunAtLoad": True,
        "KeepAlive": {"Crashed": True},
        # Avoid a CPU-heavy restart loop when macOS privacy controls deny a
        # watched source during login. The watcher has per-source backoff too.
        "ThrottleInterval": 30,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "ProcessType": "Background",
        "WorkingDirectory": str(package_root),
        "EnvironmentVariables": {
            "PYTHONPATH": str(package_root),
            # launchd does not inherit an interactive shell PATH.  DSH's
            # zstd session decoder needs the resolved Node directory.
            "PATH": _launchd_path_with_node(),
        },
    }
    if args.archive:
        config["ProgramArguments"].extend(["--archive", args.archive])
    if args.no_watch:
        config["ProgramArguments"].append("--no-watch")
    with plist_path.open("wb") as handle:
        plistlib.dump(config, handle, sort_keys=True)
    os.chmod(plist_path, 0o600)
    output: Dict[str, Any] = {"installed": str(plist_path), "loaded": False}
    if not args.no_load:
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        output["loaded"] = result.returncode == 0
        if result.returncode:
            output["launchctl_error"] = (result.stderr or result.stdout).strip()[:800]
    _print(output)
    return 0


def cmd_login_remove(_args) -> int:
    plist_path = _launch_agent_path()
    if not plist_path.exists():
        _print({"removed": False, "message": "未安装 HippoMind LaunchAgent"})
        return 0
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    plist_path.unlink()
    _print({"removed": True, "path": str(plist_path)})
    return 0


def cmd_install_agents(args) -> int:
    _reject_restricted_direct(args, "install-agents")
    paths = _paths(args)
    package_root = Path(__file__).resolve().parents[1]
    home = Path(args.home).expanduser() if args.home else None
    agents = list(args.agent or [])
    discovery_report: Optional[Dict[str, Any]] = None
    if getattr(args, "auto", False):
        # Auto mode installs only for runtimes actually detected on this
        # machine; unknown agents stay report-only and are never touched.
        discovery = AgentDiscovery(paths.state_path.parent / "agent-discovery.json")
        discovery_report = discovery.maybe_discover(home=home, force=True)
        detected = [agent_id for agent_id, item in discovery_report["known"].items()
                    if item.get("detected")]
        if not agents or agents == ["all"]:
            agents = detected or list(discovery_report["known"].keys())
        else:
            agents = [agent for agent in agents if agent in detected] or detected
    result = install_agents(SQLiteLedger(paths.ledger_path), paths, agents or ["all"],
                            package_root=package_root, home=home,
                            scopes=args.scope or None, permissions=args.permission or None)
    payload: Dict[str, Any] = {"installed": result, "count": len(result),
                               "message": "各 Agent 仅在普通用户文本包含记忆：或记忆:时检索和注入"}
    if discovery_report is not None:
        payload["discovery"] = {
            "known_detected": sorted(name for name, item in discovery_report["known"].items()
                                     if item.get("detected")),
            "unknown_reported": discovery_report["unknown"],
            "unknown_policy": "report-only，不修改",
        }
    _print(payload)
    return 0


def cmd_agent_status(args) -> int:
    _reject_restricted_direct(args, "agent-status")
    paths = _paths(args)
    home = Path(args.home).expanduser() if args.home else None
    discovery = AgentDiscovery(paths.state_path.parent / "agent-discovery.json")
    discovery.maybe_discover(home=home)
    report = discovery.report()
    known = report.get("known", {})
    agents = agent_install_status(SQLiteLedger(paths.ledger_path), paths,
                                  args.agent or ["all"], home=home)
    for item in agents:
        entry = known.get(item["agent"], {})
        item["detected_on_machine"] = bool(entry.get("detected", True))
        item["integration_tier"] = integration_tier(item["agent"], report)
        if entry.get("evidence"):
            item["detection_evidence"] = entry["evidence"]
    _print({"agents": agents, "discovery": discovery.summary(),
            "unknown_agents": report.get("unknown", []),
            "unknown_policy": "report-only：未识别的 Agent 只报告，不修改"})
    return 0


def cmd_discover_agents(args) -> int:
    """Force one discovery pass and print the report without writing rules."""
    _reject_restricted_direct(args, "discover-agents")
    paths = _paths(args)
    home = Path(args.home).expanduser() if args.home else None
    discovery = AgentDiscovery(paths.state_path.parent / "agent-discovery.json")
    report = discovery.maybe_discover(home=home, force=True)
    for agent_id, item in report["known"].items():
        item["integration_tier"] = integration_tier(agent_id, report)
    _print({"scanned_at": report["scanned_at"], "known": report["known"],
            "unknown": report["unknown"],
            "unknown_policy": "report-only：未识别的 Agent 只报告，确认后可加入 provider manifest",
            "manifest": provider_manifest()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hippomind", description="HippoMind 本机 Memory Hub")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化 SQLite/WAL 账本，可选导入旧 JSON")
    _connection_arguments(init)
    init.add_argument("--import-json")
    init.add_argument("--source-agent", default="legacy-json")
    init.set_defaults(func=cmd_init)

    start = sub.add_parser("start", help="启动或复用本机 daemon")
    _connection_arguments(start)
    start.add_argument("--archive")
    start.add_argument("--watch-interval", type=float, default=30.0)
    start.add_argument("--no-watch", action="store_true")
    start.add_argument("--restart", action="store_true", help="停止并重新启动 daemon，用于代码或 schema 升级")
    start.add_argument("--read-only", action="store_true",
                       help="只读模式：所有凭据降级为共享只读视图（LAN 镜像/巡检副本）")
    start.add_argument("--foreground", action="store_true")
    start.set_defaults(func=cmd_start)

    for name, func, help_text in (("stop", cmd_stop, "停止本机 daemon"), ("status", cmd_status, "查看 daemon 与账本状态")):
        command = sub.add_parser(name, help=help_text)
        _connection_arguments(command)
        command.set_defaults(func=func)

    version_cmd = sub.add_parser("version", help="输出版本/schema/账本概要")
    _connection_arguments(version_cmd)
    version_cmd.set_defaults(func=cmd_version)

    doctor = sub.add_parser("doctor", help="以不泄露凭据的方式检查服务是否可用")
    _connection_arguments(doctor)
    doctor.add_argument("--deep", action="store_true", help="额外扫描 raw 证据中的敏感值（只输出聚合计数）")
    doctor.add_argument("--format", choices=("text", "json"), default="text")
    doctor.set_defaults(func=cmd_doctor)

    ingest = sub.add_parser("ingest", help="写入一条或一批带证据的记忆记录")
    _connection_arguments(ingest)
    ingest.add_argument("--content")
    ingest.add_argument("--kind", default="fact")
    ingest.add_argument("--agent", default="unknown")
    ingest.add_argument("--session", default="")
    ingest.add_argument("--task", default="")
    ingest.add_argument("--project", default="")
    ingest.add_argument("--role", default="assistant")
    ingest.add_argument("--scope", default="shared")
    ingest.add_argument("--evidence", action="append", default=[])
    ingest.add_argument("--json")
    ingest.add_argument("--file")
    ingest.set_defaults(func=cmd_ingest)

    context = sub.add_parser("context", help="检索严格、最小的 Context Pack")
    _connection_arguments(context)
    context.add_argument("query")
    context.add_argument("--task", default="")
    context.add_argument("--project", default="")
    context.add_argument("--source-agent", action="append", default=[])
    context.add_argument("--scope", default="shared")
    context.add_argument("--from-time", default="")
    context.add_argument("--to-time", default="")
    context.add_argument("--as-of", default="",
                         help="双时态历史切片：按该 ISO 时间点求值 valid_from/valid_until 窗口")
    context.add_argument("--max-items", type=int, default=8)
    context.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    context.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    context.add_argument("--raw-chat", action="store_true")
    context.add_argument("--allow-no-evidence", action="store_true")
    context.add_argument("--debug", action="store_true")
    context.add_argument("--enable-ngram-vector", action="store_true",
                         help="启用内置 n-gram 哈希向量重排通道（默认关闭）")
    context.add_argument("--text-only", action="store_true")
    context.add_argument("--offline", action="store_true")
    context.set_defaults(func=cmd_context)

    auto_context = sub.add_parser("auto-context", help="按当前要求构建最小 Context Pack（可使用显式记忆门控）")
    _connection_arguments(auto_context)
    auto_context.add_argument("request", nargs="?", default="",
                              help="本轮用户的完整自然语言要求；Agent 默认只响应记忆：或记忆:")
    auto_context.add_argument("--stdin", action="store_true",
                              help="从标准输入读取完整用户要求，适合 Agent 前置 hook")
    auto_context.add_argument("--cwd", default="",
                              help="当前任务工作目录，仅作为软检索线索")
    auto_context.add_argument("--agent", default="codex")
    auto_context.add_argument("--task", default="")
    auto_context.add_argument("--project", default="")
    auto_context.add_argument("--scope", default="shared")
    auto_context.add_argument("--max-items", type=int, default=AUTO_MAX_ITEMS)
    auto_context.add_argument("--max-chars", type=int, default=AUTO_MAX_CHARS)
    auto_context.add_argument("--trigger-mode", choices=AUTO_TRIGGER_MODES, default="always",
                              help="记忆注入门控：默认 Agent 使用 anywhere；CLI 直调用默认 always 以兼容旧脚本")
    auto_context.add_argument("--format", choices=("prompt", "text", "json"), default="prompt")
    auto_context.add_argument("--debug", action="store_true")
    auto_context.add_argument("--offline", action="store_true", help="不访问或启动 daemon，直接读本机 SQLite")
    auto_context.add_argument("--no-start", action="store_true", help="daemon 不在时仅离线读取")
    auto_context.add_argument("--archive")
    auto_context.add_argument("--watch-interval", type=float, default=30.0)
    auto_context.add_argument("--no-watch", action="store_true")
    auto_context.set_defaults(func=cmd_auto_context)

    inject = sub.add_parser("inject", help="开关自动 Context Pack 注入；采集和维护保持运行")
    _connection_arguments(inject)
    inject.add_argument("action", choices=("on", "off", "toggle", "status"), nargs="?", default="status")
    inject.add_argument("--agent", default="all", help="Agent 名称；all 表示默认开关")
    inject.set_defaults(func=cmd_inject)

    forget_cmd = sub.add_parser("forget", help="遗忘请求：移除记忆投影；--purge 连同事件内容清除")
    _connection_arguments(forget_cmd)
    forget_cmd.add_argument("--scope", default="", help="要遗忘的 scope")
    forget_cmd.add_argument("--before", default="",
                            help="只遗忘该 ISO 时间之前创建的记忆")
    forget_cmd.add_argument("--memory-id", action="append", default=[],
                            help="显式指定 memory_id，可重复")
    forget_cmd.add_argument("--purge", action="store_true",
                            help="连同 ledger_events 内容一并删除（真正擦除）")
    forget_cmd.add_argument("--source-agent", default="local-admin")
    forget_cmd.add_argument("--batch", default="",
                            help="按 import_batch 批次回滚导入")
    forget_cmd.add_argument("--dry-run", action="store_true",
                            help="只预览将被遗忘的记忆清单，不执行")
    forget_cmd.set_defaults(func=cmd_forget)

    facts_cmd = sub.add_parser("facts", help="事实投影：活跃 claim 的结构化视图")
    _connection_arguments(facts_cmd)
    facts_cmd.add_argument("--scope", default="shared")
    facts_cmd.add_argument("--subject", default="", help="按 subject 过滤")
    facts_cmd.add_argument("--limit", type=int, default=200)
    facts_cmd.add_argument("--format", choices=("text", "json"), default="text")
    facts_cmd.set_defaults(func=cmd_facts)

    skills_cmd = sub.add_parser("skills", help="技能投影：procedural 记忆的结构化视图")
    _connection_arguments(skills_cmd)
    skills_cmd.add_argument("--scope", default="shared")
    skills_cmd.add_argument("--limit", type=int, default=100)
    skills_cmd.add_argument("--format", choices=("text", "json"), default="text")
    skills_cmd.set_defaults(func=cmd_skills)

    resolve_cmd = sub.add_parser("resolve", help="人工裁决 Slot-Value 冲突：指定胜者记忆，败者让位（保留谱系）")
    _connection_arguments(resolve_cmd)
    resolve_cmd.add_argument("--claim", required=True, help="冲突组内任一 claim_id")
    resolve_cmd.add_argument("--winner", required=True, help="胜者 memory_id")
    resolve_cmd.add_argument("--source-agent", default="local-admin",
                             help="审计事件记录的裁决来源")
    resolve_cmd.set_defaults(func=cmd_resolve)
    install_auto = sub.add_parser("install-auto", help="一次性安装一个 Agent 的显式记忆 Context Pack 规则")
    install_auto.add_argument("--agents-file", help="目标 AGENTS.md；默认 ~/.codex/AGENTS.md")
    install_auto.add_argument("--agent", default="codex")
    install_auto.add_argument("--print-only", action="store_true", help="只输出规则，不写文件")
    install_auto.add_argument("--agent-config", help="规则引用的受限 Agent config")
    install_auto.set_defaults(func=cmd_install_auto)

    install_agents_command = sub.add_parser(
        "install-agents", help="为多个本机 Agent 安装显式记忆规则、受限凭据和 MCP profile"
    )
    _connection_arguments(install_agents_command)
    install_agents_command.add_argument("--agent", action="append", default=[],
                                        help="Agent 名称，可重复或使用 all")
    install_agents_command.add_argument("--auto", action="store_true",
                                        help="只为自动发现已检测到的运行时安装；未知 Agent 只报告不修改")
    install_agents_command.add_argument("--scope", action="append", default=[],
                                        help="允许的 scope，可重复；默认 shared 与该 Agent 私有 scope")
    install_agents_command.add_argument("--permission", action="append", default=[],
                                        choices=("read", "ingest", "feedback", "stats",
                                                 "shared_ingest", "shared_feedback", "sync"))
    install_agents_command.add_argument("--home", help="测试或自定义规则根目录，默认当前用户 home")
    install_agents_command.set_defaults(func=cmd_install_agents)

    agent_status = sub.add_parser("agent-status", help="查看 Agent 规则和凭据状态（不显示 token）")
    _connection_arguments(agent_status)
    agent_status.add_argument("--agent", action="append", default=[])
    agent_status.add_argument("--home")
    agent_status.set_defaults(func=cmd_agent_status)

    discover = sub.add_parser("discover-agents", help="扫描本机 Agent 运行时；未知 Agent 只报告不修改")
    _connection_arguments(discover)
    discover.add_argument("--home")
    discover.set_defaults(func=cmd_discover_agents)

    sync = sub.add_parser("sync", help="立即增量同步归档；daemon 不在时本地执行")
    _connection_arguments(sync)
    sync.add_argument("--archive")
    sync.add_argument("--watch-interval", type=float, default=30.0)
    sync.set_defaults(func=cmd_sync)

    watch = sub.add_parser("watch", help="以低频游标方式持续采集归档")
    _connection_arguments(watch)
    watch.add_argument("--archive")
    watch.add_argument("--watch-interval", type=float, default=30.0)
    watch.add_argument("--once", action="store_true")
    watch.set_defaults(func=cmd_watch)

    for name, func, help_text, argument in (
        ("export", cmd_export, "导出记忆为 JSONL（ingest 兼容）", "output"),
        ("import", cmd_import, "从 JSONL 导入（幂等；兼容旧版单对象 JSON）", "input"),
        ("backup", cmd_backup, "创建 SQLite 原子备份", "output"),
        ("pin", cmd_pin, "置顶/取消置顶记忆（--unpin 取消）", "memory-id"),
        ("restore", cmd_restore, "从 SQLite 备份恢复（daemon 必须停止）", "input"),
    ):
        command = sub.add_parser(name, help=help_text)
        _connection_arguments(command)
        command.add_argument(argument)
        if name == "import":
            command.add_argument("--source-agent", default="legacy-json")
        if name == "pin":
            command.add_argument("--unpin", action="store_true",
                                 help="取消置顶（默认执行置顶）")
        if name == "restore":
            command.add_argument("--sha256", default="",
                                 help="期望校验和；与实际不符时拒绝恢复")
        if name == "export":
            command.add_argument("--format", choices=("jsonl", "sql"),
                                 default="jsonl",
                                 help="sql=可回放审计脚本（同版本空库 sqlite3 < file）")
            command.add_argument("--since", default="",
                                 help="只导出该 ISO 时刻之后创建的记忆（增量导出）")
            command.add_argument("--scope", default="shared")
            command.add_argument("--include-raw-chat", action="store_true")
            command.add_argument("--limit", type=int, default=100_000)
            command.add_argument("--encrypt", action="store_true",
                                 help="口令加密导出（混淆级；配合 import 自动解密）")
            command.add_argument("--passphrase", default="")
        if name == "import":
            command.add_argument("--passphrase", default="",
                                 help="加密导出的口令（缺省时交互输入）")
        if name == "import":
            command.add_argument("--scope", default="")
        command.set_defaults(func=func)

    conflicts_cmd = sub.add_parser("conflicts", help="列出冲突/被取代 claim 谱系")
    _connection_arguments(conflicts_cmd)
    conflicts_cmd.add_argument("--scope", default="shared")
    conflicts_cmd.add_argument("--limit", type=int, default=100)
    conflicts_cmd.add_argument("--format", choices=("text", "json"), default="text")
    conflicts_cmd.set_defaults(func=cmd_conflicts)

    graph_cmd = sub.add_parser("graph", help="实体图谱：claim 主语邻域（因果/关联）")
    _connection_arguments(graph_cmd)
    graph_cmd.add_argument("subject", help="起始实体（claim subject）")
    graph_cmd.add_argument("--scope", default="shared")
    graph_cmd.add_argument("--hops", type=int, default=1, choices=(1, 2))
    graph_cmd.add_argument("--limit", type=int, default=200)
    graph_cmd.add_argument("--format", choices=("text", "json"), default="text")
    graph_cmd.set_defaults(func=cmd_graph)

    pins_cmd = sub.add_parser("pins", help="列出置顶记忆（--scope 过滤，--format json）")
    _connection_arguments(pins_cmd)
    pins_cmd.add_argument("--scope", default="shared")
    pins_cmd.add_argument("--limit", type=int, default=100)
    pins_cmd.add_argument("--format", choices=("text", "json"), default="text")
    pins_cmd.set_defaults(func=cmd_pins)

    monitor_cmd = sub.add_parser("monitor", help="只读巡检循环：周期打印健康与规模摘要（Ctrl-C 退出）")
    monitor_cmd.add_argument("--interval", type=float, default=5.0, metavar="SEC",
                             help="巡检间隔秒数（默认 5）")
    monitor_cmd.add_argument("--rounds", type=int, default=0,
                             help="巡检轮数上限（0=无限直到 Ctrl-C）")
    monitor_cmd.add_argument("--json", action="store_true",
                             help="输出单次 JSON 快照并立即退出（离线时退出码 1）")
    _connection_arguments(monitor_cmd)
    checkup_cmd = sub.add_parser("checkup", help="账本深度体检：完整性/FTS 同步/游标/声明一致性（默认只读）")
    _connection_arguments(checkup_cmd)
    checkup_cmd.add_argument("--format", choices=("json", "md", "junit"),
                             default="json",
                             help="md=markdown 报告；junit=CI 集成 XML")
    checkup_cmd.add_argument("--repair", action="store_true",
                             help="发现 FTS 失同步时自动重建索引")
    checkup_cmd.set_defaults(func=cmd_checkup)
    monitor_cmd.set_defaults(func=cmd_monitor)

    redact_raw = sub.add_parser("redact-raw", help="脱敏并收缩既有 raw 聊天证据（daemon 必须停止）")
    _connection_arguments(redact_raw)
    redact_raw.add_argument("--max-chars", type=int, default=4_000)
    redact_raw.add_argument("--batch-size", type=int, default=250)
    redact_raw.set_defaults(func=cmd_redact_raw)

    scan_raw = sub.add_parser("scan-raw", help="扫描既有 raw 证据中的敏感值（只输出聚合计数）")
    _connection_arguments(scan_raw)
    scan_raw.add_argument("--batch-size", type=int, default=250)
    scan_raw.set_defaults(func=cmd_scan_raw)

    maintain = sub.add_parser("maintain", help="执行 raw 保留期、容量与压缩归档维护")
    _connection_arguments(maintain)
    maintain.add_argument("--vacuum", action="store_true",
                    help="整理库文件（VACUUM）；先备份，大库耗时较长")
    maintain.add_argument("--prune-backups", type=int, default=0, metavar="N",
                          help="只保留最近 N 个登记在册的备份文件（sha256 校验后才删）")
    maintain.add_argument("--raw-retention-days", type=int, default=30)
    maintain.add_argument("--raw-ttl-days", type=int, default=0, metavar="N",
                          help="raw 证据物理删除期限（天）；0 表示按保留期+90 推导")
    maintain.add_argument("--purge-raw", action="store_true",
                          help="归档校验通过后物理删除过期 raw 证据（真正缩容）")
    maintain.add_argument("--dedupe", action="store_true",
                          help="折叠规范化后完全相同的重复行（保留最早一条与谱系）")
    maintain.add_argument("--dry-run", action="store_true",
                          help="只报告将发生什么，不做任何写入")
    maintain.add_argument("--raw-capacity", type=int, default=5_000)
    maintain.add_argument("--raw-archive-dir", default="")
    maintain.add_argument("--raw-archive-max-bytes", type=int, default=256 * 1024 * 1024)
    maintain.set_defaults(func=cmd_maintain)

    recovery_check = sub.add_parser("recovery-check", help="执行一次 SQLite/WAL 备份恢复演练（不替换在线账本）")
    _connection_arguments(recovery_check)
    recovery_check.set_defaults(func=cmd_recovery_check)

    feedback = sub.add_parser("feedback", help="以外部反馈调整工作记忆权重")
    _connection_arguments(feedback)
    feedback.add_argument("memory_id")
    feedback.add_argument("signal", type=float)
    feedback.add_argument("--note", default="")
    feedback.add_argument("--source-agent", default="")
    feedback.add_argument("--feedback-id", default="")
    feedback.set_defaults(func=cmd_feedback)

    login_install = sub.add_parser("login-install", help="显式安装可选 macOS 登录启动项")
    _connection_arguments(login_install)
    login_install.add_argument("--archive")
    login_install.add_argument("--watch-interval", type=float, default=30.0)
    login_install.add_argument("--no-watch", action="store_true")
    login_install.add_argument("--no-load", action="store_true", help="仅写入 plist，不立即加载")
    login_install.set_defaults(func=cmd_login_install)
    login_remove = sub.add_parser("login-remove", help="移除 HippoMind 的 macOS 登录启动项")
    login_remove.set_defaults(func=cmd_login_remove)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (LedgerError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
