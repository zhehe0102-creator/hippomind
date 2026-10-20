"""Command line interface for the long-running HippoMind Memory Hub.

The package keeps the exact face of the former single ``cli.py``: every
``hippomind.cli.<name>`` that resolved before still resolves here.  The
daemon-connection and auto-context helpers -- the ones
:mod:`hippomind.codex_hook` imports and the test suite patches -- stay
defined in this module; the argparse tree lives in
:mod:`hippomind.cli.parser` and the sub-commands in the ``cmd_*`` modules.
"""

from __future__ import annotations

import argparse as argparse
import contextlib
import json
import tempfile as tempfile
import getpass as getpass
import hashlib as hashlib
import os
import plistlib as plistlib
import shutil as shutil
import sqlite3 as sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from ..agent_config import load_agent_config as load_agent_config
from ..agent_install import agent_install_status as agent_install_status, install_agents as install_agents
from ..auto_context import (
    AUTO_MAX_CHARS, AUTO_MAX_ITEMS, PROMPT_CLOSE, PROMPT_OPEN, build_auto_context, extract_memory_request,
    render_untrusted_context,
)
from ..auto_context import (
    AUTO_TRIGGER_MODES as AUTO_TRIGGER_MODES, agent_auto_rules as agent_auto_rules,
    install_agent_rules as install_agent_rules,
)
from ..context_pack import (
    ContextPackBuilder as ContextPackBuilder, ContextPackOptions as ContextPackOptions,
    DEFAULT_MAX_CHARS as DEFAULT_MAX_CHARS, DEFAULT_MAX_TOKENS as DEFAULT_MAX_TOKENS,
)
from ..daemon import RuntimePaths, ledger_identity, load_runtime_state, read_local_token
from ..daemon import API_VERSION as API_VERSION, SERVER_VERSION as SERVER_VERSION, run_daemon as run_daemon
from ..injection import InjectionState
from ..ledger import LedgerError, SQLiteLedger
from ..ledger import SCHEMA_VERSION as SCHEMA_VERSION, utc_now as utc_now
from ..embeddings import HashingNGramEmbedding as HashingNGramEmbedding
from ..provider_manifest import (
    AgentDiscovery as AgentDiscovery, integration_tier as integration_tier,
    provider_manifest as provider_manifest,
)
from ..watcher import ArchiveWatcher as ArchiveWatcher, default_dsh_sessions_root as default_dsh_sessions_root
from .common import _agent_settings, _print


def _paths(args) -> RuntimePaths:
    config = _agent_settings(args)
    ledger = getattr(args, "ledger", None) or config.get("ledger") or None
    runtime_dir = getattr(args, "runtime_dir", None) or config.get("runtime_dir") or None
    return RuntimePaths.for_ledger(ledger, runtime_dir)


def _agent_token(args, paths: RuntimePaths) -> str:
    config = _agent_settings(args)
    return str(config.get("token", "")) if config else read_local_token(paths)


def _port(args, paths: RuntimePaths) -> int:
    state = load_runtime_state(paths)
    config = _agent_settings(args)
    return int(getattr(args, "port", None) or state.get("port") or config.get("port") or 8765)


def _request(paths: RuntimePaths, port: int, method: str, endpoint: str,
             body: Optional[Mapping[str, Any]] = None, auth: bool = True,
             token: Optional[str] = None) -> Dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8") if body is not None else None
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
    package_root = Path(__file__).resolve().parents[2]
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
                    verify_token: str = "",
                    read_only: bool = False) -> tuple[Dict[str, Any], bool, Path]:
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
                                              watch_interval=watch_interval,
                                              no_watch=no_watch, read_only=read_only)


def _verified_scoped_credential(args, paths: RuntimePaths) -> bool:
    """Only a locally valid scoped config may request the upgrade self-heal."""
    if not str(getattr(args, "agent_config", "") or "").strip():
        return False
    try:
        return SQLiteLedger(paths.ledger_path).resolve_agent_credential(_agent_token(args, paths)) is not None
    except LedgerError:
        return False


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

# ---------------------------------------------------------------------------
# Split off into submodules.  ``hippomind.cli.<name>`` keeps resolving to
# the same function objects it did while all of this lived in one cli.py.
# ---------------------------------------------------------------------------
from .common import (  # noqa: E402  (re-exports follow the helpers they feed)
    _connection_arguments as _connection_arguments, _reject_restricted_direct as _reject_restricted_direct,
    _secure_local_file as _secure_local_file,
)
from .cmd_lifecycle import (  # noqa: E402  (re-exports follow the helpers they feed)
    _launch_agent_path as _launch_agent_path, _launchd_path_with_node as _launchd_path_with_node,
    _monitor_snapshot as _monitor_snapshot, cmd_doctor as cmd_doctor, cmd_init as cmd_init,
    cmd_login_install as cmd_login_install, cmd_login_remove as cmd_login_remove, cmd_monitor as cmd_monitor,
    cmd_start as cmd_start, cmd_status as cmd_status, cmd_stop as cmd_stop, cmd_version as cmd_version,
)
from .cmd_write import (  # noqa: E402  (re-exports follow the helpers they feed)
    _ingest_record as _ingest_record, cmd_conflicts as cmd_conflicts, cmd_facts as cmd_facts,
    cmd_feedback as cmd_feedback, cmd_forget as cmd_forget, cmd_graph as cmd_graph, cmd_ingest as cmd_ingest,
    cmd_pin as cmd_pin, cmd_pins as cmd_pins, cmd_resolve as cmd_resolve, cmd_skills as cmd_skills,
)
from .cmd_read import (  # noqa: E402  (re-exports follow the helpers they feed)
    _pack_body as _pack_body, cmd_context as cmd_context, cmd_inject as cmd_inject, cmd_sync as cmd_sync,
    cmd_watch as cmd_watch,
)
from .cmd_data import cmd_export as cmd_export, cmd_import as cmd_import  # noqa: E402  (re-exports follow the helpers they feed)
from .cmd_maintain import (  # noqa: E402  (re-exports follow the helpers they feed)
    _backup_registry_path as _backup_registry_path, _load_backup_registry as _load_backup_registry,
    _render_checkup_junit as _render_checkup_junit, _render_checkup_md as _render_checkup_md,
    _save_backup_registry as _save_backup_registry, _sha256_of as _sha256_of, cmd_backup as cmd_backup,
    cmd_checkup as cmd_checkup, cmd_maintain as cmd_maintain, cmd_recovery_check as cmd_recovery_check,
    cmd_redact_raw as cmd_redact_raw, cmd_restore as cmd_restore, cmd_scan_raw as cmd_scan_raw,
)
from .cmd_integrate import (  # noqa: E402  (re-exports follow the helpers they feed)
    cmd_agent_status as cmd_agent_status, cmd_discover_agents as cmd_discover_agents,
    cmd_install_agents as cmd_install_agents, cmd_install_auto as cmd_install_auto,
)
from .parser import build_parser as build_parser  # noqa: E402  (re-exports follow the helpers they feed)
from .app import main as main  # noqa: E402  (re-exports follow the helpers they feed)
