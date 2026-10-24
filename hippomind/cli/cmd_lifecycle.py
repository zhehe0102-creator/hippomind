"""Process lifecycle commands: init, start, stop, status, version, doctor,
monitor and the optional macOS login item.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Mapping
from ..daemon import API_VERSION, SERVER_VERSION, load_runtime_state, run_daemon
from ..injection import InjectionState
from ..ledger import LedgerError, SCHEMA_VERSION, SQLiteLedger, utc_now
from . import (
    _agent_token, _health, _health_for_paths, _paths, _port, _request, _restart_daemon, _start_or_reuse_daemon,
)
from .common import _print, _reject_restricted_direct, _secure_local_file


def cmd_version(args) -> int:
    """输出版本/schema/环境信息（诊断与支持用）。"""
    try:
        from ..version import VERSION as pkg_version
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
    # 停机失败必须让调用方看出来：restore/backup/maintain 一类操作的前提就是
    # "账本没有并发写方"。此前无论 daemon 是否还在跑都返回 0，脚本里的
    # `hippomind stop && hippomind restore` 会在服务仍在写账本时继续执行。
    return 1 if result["running"] else 0


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

    launchd_ok, launchd_severity, launchd_detail = _launchd_check()
    if launchd_severity != "skipped":
        checks.append({"id": "launchd", "ok": launchd_ok,
                       "severity": launchd_severity, "detail": launchd_detail})

    warnings = [item["detail"] for item in checks if item["severity"] == "warning"]
    errors = [item["detail"] for item in checks if item["severity"] == "error"]
    payload = {
        "version": SERVER_VERSION, "ready": not errors,
        "checks": checks, "warnings": warnings, "errors": errors,
        "ledger": {"events": stats.get("events", 0), "active": stats.get("active", 0),
                    "raw_chat": stats.get("raw_chat", 0), "kinds": kinds},
        "deep_scan": deep_scan,
    }
    if args.format == "json":
        _print(payload)
    else:
        print(f"HippoMind doctor v{SERVER_VERSION}")
        for item in checks:
            marker = "OK" if item["severity"] == "ok" else "WARN" if item["severity"] == "warning" else "FAIL"
            print(f"[{marker}] {item['id']}: {item['detail']}")
        print("结论: " + ("READY，可正常使用" if payload["ready"] else "NEEDS ATTENTION，请先处理 FAIL 项"))
    # A readiness gate that cannot fail is not a gate.  This used to return 0
    # unconditionally, so `hippomind doctor && deploy` proceeded while doctor
    # printed `[FAIL] daemon: 不可达` — the same defect class as `stop`
    # reporting success while the daemon was alive (ff610e0).  Warnings stay
    # non-fatal: they are advisory, and the existing suite asserts 0 for a
    # healthy-but-warned temporary ledger.
    return 1 if errors else 0


def _launchd_check() -> tuple[bool, str, str]:
    """Report a stale macOS login item without ever mutating it.

    ``hippomind login-install`` writes the current package root into
    ``PYTHONPATH``/``WorkingDirectory``, but a plist installed by an older
    release keeps pointing at whatever path that release lived in.  After the
    workspace moved, launchd kept starting a daemon whose import path no longer
    exists — it fails at every login, silently.  Doctor is the command an
    operator pastes into a bug report, so it must surface that drift instead of
    reporting READY.

    The GUI job (``com.hippomind.gui``) is checked too: it is written by hand
    rather than by ``login-install``, so it is the one most likely to keep a
    dead path forever (it did: it still pointed at the pre-move
    ``pi-desktop/chat-workspace/hippocampus-agent`` directory).

    **External-volume login items are reported separately.**  When the package
    lives outside the boot volume (an external drive, a network mount), macOS
    privacy protection denies a launchd *background* job the right to read that
    path even though the same interpreter can read it from a terminal.  The job
    then dies at every login with ``PermissionError: [Errno 1] Operation not
    permitted`` inside ``_find_spec`` — measured, and reproduced with a
    minimal probe outside this project.  Rewriting the plist cannot fix it, so
    the check reports it as a distinct warning that names the real remedy
    instead of blaming the paths.

    Read-only by construction: it reads the plists and stats the paths they
    name.  It never creates, rewrites, bootstraps or bootouts a job.
    """
    if sys.platform != "darwin":
        return True, "skipped", "非 macOS，无 launchd 登录项"
    package_root = Path(__file__).resolve().parents[2]
    external_volume = _is_external_volume(package_root)
    labels = (("com.hippomind.daemon.plist", True), ("com.hippomind.gui.plist", False))
    problems: list = []
    found = 0
    deployed_pythonpaths: list = []
    for name, owned_by_installer in labels:
        plist_path = _launch_agent_path().parent / name
        try:
            with plist_path.open("rb") as handle:
                config = plistlib.load(handle)
        except FileNotFoundError:
            continue
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            problems.append(f"{name} 不可读：{type(exc).__name__}: {exc}"[:120])
            continue
        if not isinstance(config, Mapping):
            problems.append(f"{name} 格式异常（不是字典）")
            continue
        found += 1
        environment = config.get("EnvironmentVariables")
        environment = environment if isinstance(environment, Mapping) else {}
        declared_pythonpath = str(environment.get("PYTHONPATH", "") or "")
        if declared_pythonpath and _is_deployed_runtime(declared_pythonpath):
            deployed_pythonpaths.append(declared_pythonpath)
        if declared_pythonpath and not Path(declared_pythonpath).is_dir():
            problems.append(f"{name} PYTHONPATH 指向不存在的目录 {declared_pythonpath}")
        elif (declared_pythonpath and owned_by_installer
              and Path(declared_pythonpath).resolve() != package_root.resolve()
              and not _is_deployed_runtime(declared_pythonpath)):
            problems.append(f"{name} PYTHONPATH 指向 {declared_pythonpath}，"
                            f"当前包根是 {package_root}")
        working = str(config.get("WorkingDirectory", "") or "")
        if working and not Path(working).is_dir():
            problems.append(f"{name} WorkingDirectory 指向不存在的目录 {working}")
        program = config.get("ProgramArguments")
        if isinstance(program, (list, tuple)) and program:
            executable = str(program[0])
            if executable and not Path(executable).exists():
                problems.append(f"{name} 解释器不存在 {executable}")
    if problems:
        return False, "warning", ("登录项已过期：" + "；".join(problems) +
                                  "（daemon 项运行 login-install 重装；GUI 项需手工修正）")
    if not found:
        return True, "ok", "未安装登录自启项（按需运行 login-install）"
    if external_volume:
        # The repo lives on an external volume, but the job may legitimately
        # point at the internal-disk runtime copy that install-autostart.sh
        # deploys.  Only warn when it still points at the external path.
        deployed = [path for path in deployed_pythonpaths if _is_deployed_runtime(path)]
        if deployed:
            return True, "ok", (f"登录项使用内置盘运行副本（{deployed[0]}），"
                                "外置卷工作区不受 TCC 影响")
        return False, "warning", (
            f"登录项路径有效，但工作区在外置卷（{package_root}）：macOS 隐私保护会拒绝 "
            "launchd 后台任务读取该卷，登录自启会在每次登录时以 PermissionError 失败。"
            "开机自启不可用，请改用 `hippomind start`，或运行 "
            "scripts/install-autostart.sh 部署内置盘运行副本")
    return True, "ok", f"登录项存在且路径有效（{package_root}）"


def _is_deployed_runtime(path: str) -> bool:
    """True when ``path`` is an intentional internal-disk runtime copy.

    macOS TCC refuses to let a launchd background job read the external volume
    the repo lives on, so ``scripts/install-autostart.sh`` copies the package to
    ``~/Library/Application Support/HippoMind/runtime`` and points the job there.
    That is a deliberate deployment, not stale drift, so doctor must not report
    it as a broken login item.  The copy is only trusted when it actually
    contains this package.
    """
    try:
        candidate = Path(path).expanduser().resolve()
    except OSError:
        return False
    if not (candidate / "hippomind" / "daemon.py").is_file():
        return False
    # The whole point of the copy is to sit on the boot volume: a package that
    # still lives on the external volume is the un-deployed case, not a fix.
    return not _is_external_volume(candidate)


def _is_external_volume(path: Path) -> bool:
    """True when ``path`` lives on a volume other than the boot volume.

    Used only to explain a launchd failure mode that no plist edit can fix; the
    check itself never assumes it (it still verifies every path).
    """
    try:
        return os.stat(str(path)).st_dev != os.stat(os.sep).st_dev
    except OSError:
        return False


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
    package_root = Path(__file__).resolve().parents[2]
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
