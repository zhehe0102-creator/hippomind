"""Idempotent integration installer for local coding agents.

Rules are written only into each agent's documented/local instruction location.
The secret itself lives in a separate mode-0600 HippoMind config file and is
never embedded in a rule, MCP profile, or command output.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .agent_config import agent_config_path, load_agent_config, safe_agent_name, write_agent_config
from .auto_context import RULES_BEGIN, RULES_END, install_agent_rules
from .daemon import RuntimePaths
from . import __version__ as _PACKAGE_VERSION
from .dsh_install import dsh_integration_status, install_dsh_integration
from .ledger import AGENT_PERMISSIONS, AgentCredential, LedgerError, SQLiteLedger
from .mcp_register import mcp_registration_path, mcp_registration_status, register_mcp_server


SUPPORTED_AGENTS: Tuple[str, ...] = ("codex", "pi", "claude", "cursor", "gemini", "deepseek", "local")
# Shared collaboration is the default installed workflow.  The separate
# capabilities remain enforceable for deliberately restricted credentials.
DEFAULT_PERMISSIONS: Tuple[str, ...] = tuple(sorted(AGENT_PERMISSIONS))
CODEX_HOOK_EVENT = "UserPromptSubmit"
CODEX_HOOK_EVENT_KEY = "user_prompt_submit"
CODEX_HOOK_MODULE = "hippomind.codex_hook"
CODEX_HOOK_MARKER = "--managed"
CODEX_HOOK_TIMEOUT_SECONDS = 12
CODEX_APP_SERVER_TIMEOUT_SECONDS = 8.0


#: Self-reported identity for the Codex app-server handshake.  This used to be
#: a frozen v0.7 literal that matched no released version; the package version
#: is now the single source (pinned by tests/test_self_reported_versions.py).
INSTALLER_CLIENT_INFO = {"name": "hippomind-hook-installer",
                         "version": _PACKAGE_VERSION}


def codex_hooks_path(*, home: Optional[Path] = None) -> Path:
    root = Path(home).expanduser() if home else Path.home()
    return root / ".codex" / "hooks.json"


def _load_codex_hooks(path: Path) -> Tuple[Dict[str, Any], str]:
    """Load the user-owned Codex hook document without normalizing it yet."""
    try:
        if path.stat().st_size > 1_000_000:
            raise LedgerError("Codex hooks.json 超过 1 MiB 安全上限")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"description": "HippoMind managed Codex context hook", "hooks": {}}, ""
    except OSError as exc:
        raise LedgerError(f"无法读取 Codex hooks.json: {path}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LedgerError(f"Codex hooks.json 不是有效 JSON: {path}") from exc
    if not isinstance(data, dict):
        raise LedgerError("Codex hooks.json 根节点必须是对象")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise LedgerError("Codex hooks.json.hooks 必须是对象")
    return data, raw


def _managed_codex_hook_command(package_root: Path, agent_config: Path) -> str:
    root = shlex.quote(str(Path(package_root).expanduser().resolve()))
    interpreter = shlex.quote(str(Path(sys.executable).resolve()))
    config = shlex.quote(str(Path(agent_config).expanduser().resolve()))
    return (f"cd {root} && {interpreter} -m {CODEX_HOOK_MODULE} "
            f"{CODEX_HOOK_MARKER} --trigger-mode always --agent-config {config}")


def _is_managed_codex_hook(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    command = value.get("command")
    return (value.get("type") == "command" and isinstance(command, str)
            and CODEX_HOOK_MODULE in command and CODEX_HOOK_MARKER in command)


def _managed_codex_hook_key(path: Path, groups: Sequence[Mapping[str, Any]]) -> str:
    """Return Codex's stable state key for the one managed handler, if present."""
    try:
        source = str(path.expanduser().resolve())
    except OSError:
        source = str(path.expanduser())
    for group_index, group in enumerate(groups):
        handlers = group.get("hooks", []) if isinstance(group, Mapping) else []
        if not isinstance(handlers, list):
            continue
        for handler_index, handler in enumerate(handlers):
            if _is_managed_codex_hook(handler):
                return f"{source}:{CODEX_HOOK_EVENT_KEY}:{group_index}:{handler_index}"
    return ""


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return os.path.abspath(str(left.expanduser())) == os.path.abspath(str(right.expanduser()))


def _uses_current_codex_home(home: Optional[Path]) -> bool:
    """Only the active user's Codex config may be auto-trusted."""
    return home is None or _same_path(Path(home), Path.home())


def _codex_app_server_request(process: Any, request_id: int, method: str,
                              params: Mapping[str, Any], *, timeout: float) -> Mapping[str, Any]:
    """Issue one bounded JSON-RPC request to a local Codex app-server."""
    stdin = getattr(process, "stdin", None)
    stdout = getattr(process, "stdout", None)
    if stdin is None or stdout is None:
        raise LedgerError("Codex app-server 标准输入输出不可用")
    try:
        stdin.write(json.dumps({"id": request_id, "method": method, "params": dict(params)},
                               ensure_ascii=False, separators=(",", ":")) + "\n")
        stdin.flush()
    except OSError as exc:
        raise LedgerError("无法向 Codex app-server 发送请求") from exc
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        try:
            readable, _, _ = select.select([stdout], [], [], min(0.25, deadline - time.monotonic()))
        except (OSError, ValueError) as exc:
            raise LedgerError("无法读取 Codex app-server 响应") from exc
        if not readable:
            if process.poll() is not None:
                break
            continue
        line = stdout.readline()
        if not line:
            break
        try:
            response = json.loads(line)
        except ValueError:
            continue
        if not isinstance(response, Mapping) or response.get("id") != request_id:
            continue
        error = response.get("error")
        if isinstance(error, Mapping):
            raise LedgerError(f"Codex app-server {method} 失败: {str(error.get('message', '未知错误'))[:300]}")
        result = response.get("result")
        return result if isinstance(result, Mapping) else {}
    raise LedgerError(f"Codex app-server {method} 超时或提前退出")


def _stop_codex_app_server(process: Optional[Any]) -> None:
    if process is None:
        return
    try:
        if process.stdin is not None:
            process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)


def _codex_app_server(package_root: Path) -> Any:
    executable = shutil.which("codex")
    if not executable:
        raise LedgerError("未找到 codex CLI")
    return subprocess.Popen(
        [executable, "app-server", "--stdio"], cwd=str(package_root),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )


def _inspect_codex_user_prompt_hook(package_root: Path, hook_path: Path, *,
                                    expected_command: str = "") -> Dict[str, Any]:
    """Read the actual runtime state of HippoMind's own Codex hook."""
    process: Optional[Any] = None
    try:
        root = Path(package_root).expanduser().resolve()
        process = _codex_app_server(root)
        _codex_app_server_request(
            process, 1, "initialize",
            {"clientInfo": INSTALLER_CLIENT_INFO,
             "capabilities": {"experimentalApi": True}},
            timeout=CODEX_APP_SERVER_TIMEOUT_SECONDS,
        )
        listing = _codex_app_server_request(
            process, 2, "hooks/list", {"cwds": [str(root)]},
            timeout=CODEX_APP_SERVER_TIMEOUT_SECONDS,
        )
        for entry in listing.get("data", []):
            if not isinstance(entry, Mapping):
                continue
            for hook in entry.get("hooks", []):
                if not isinstance(hook, Mapping):
                    continue
                command = str(hook.get("command") or "")
                source_path = Path(str(hook.get("sourcePath") or "."))
                if (CODEX_HOOK_MODULE not in command or CODEX_HOOK_MARKER not in command
                        or (expected_command and command != expected_command)
                        or not _same_path(source_path, hook_path)):
                    continue
                trust_status = str(hook.get("trustStatus") or "unknown")
                enabled = bool(hook.get("enabled"))
                return {
                    "checked": True, "found": True, "key": str(hook.get("key") or ""),
                    "current_hash": str(hook.get("currentHash") or ""),
                    "trust_status": trust_status, "enabled": enabled,
                    "timeout_seconds": hook.get("timeoutSec"),
                    "ready": enabled and trust_status in {"trusted", "managed"},
                }
        return {"checked": True, "found": False, "ready": False,
                "reason": "Codex 未发现 HippoMind UserPromptSubmit Hook"}
    except (LedgerError, OSError) as exc:
        return {"checked": False, "found": False, "ready": False, "reason": str(exc)[:300]}
    finally:
        _stop_codex_app_server(process)


def _write_codex_hook_trust(package_root: Path, hook_path: Path, hook_key: str,
                            current_hash: str) -> Dict[str, Any]:
    """Persist the exact hash calculated by Codex for HippoMind's handler."""
    if not hook_key or not current_hash:
        return {"written": False, "reason": "缺少 Codex Hook 运行时标识"}
    process: Optional[Any] = None
    try:
        root = Path(package_root).expanduser().resolve()
        process = _codex_app_server(root)
        _codex_app_server_request(
            process, 1, "initialize",
            {"clientInfo": INSTALLER_CLIENT_INFO,
             "capabilities": {"experimentalApi": True}},
            timeout=CODEX_APP_SERVER_TIMEOUT_SECONDS,
        )
        key_path = f"hooks.state.{json.dumps(hook_key, ensure_ascii=False)}.trusted_hash"
        _codex_app_server_request(
            process, 2, "config/value/write",
            {"filePath": str(hook_path.parent / "config.toml"), "keyPath": key_path,
             "value": current_hash, "mergeStrategy": "replace", "expectedVersion": None},
            timeout=CODEX_APP_SERVER_TIMEOUT_SECONDS,
        )
        return {"written": True}
    except (LedgerError, OSError) as exc:
        return {"written": False, "reason": str(exc)[:300]}
    finally:
        _stop_codex_app_server(process)


def ensure_codex_user_prompt_hook_trusted(package_root: Path, hook_path: Path, *,
                                           expected_command: str = "") -> Dict[str, Any]:
    """Verify and, when needed, trust the current HippoMind hook hash."""
    before = _inspect_codex_user_prompt_hook(
        package_root, hook_path, expected_command=expected_command,
    )
    if before.get("ready") or not before.get("checked") or not before.get("found"):
        return before
    write = _write_codex_hook_trust(
        package_root, hook_path, str(before.get("key") or ""),
        str(before.get("current_hash") or ""),
    )
    after = _inspect_codex_user_prompt_hook(
        package_root, hook_path, expected_command=expected_command,
    )
    after["trust_written"] = bool(write.get("written"))
    if not write.get("written") and write.get("reason"):
        after.setdefault("reason", write["reason"])
    return after


def install_codex_user_prompt_hook(package_root: Path, agent_config: Path, *,
                                   home: Optional[Path] = None) -> Dict[str, Any]:
    """Insert exactly one HippoMind UserPromptSubmit hook without touching others."""
    path = codex_hooks_path(home=home)
    document, raw = _load_codex_hooks(path)
    hooks = dict(document.get("hooks", {}))
    groups = hooks.get(CODEX_HOOK_EVENT, [])
    if not isinstance(groups, list):
        raise LedgerError("Codex hooks.json.UserPromptSubmit 必须是列表")
    managed = {
        "type": "command",
        "command": _managed_codex_hook_command(package_root, agent_config),
        "timeout": CODEX_HOOK_TIMEOUT_SECONDS,
        "additionalContextLimit": 0,
    }
    updated_groups: List[Dict[str, Any]] = []
    installed = False
    removed_duplicates = 0
    for raw_group in groups:
        if not isinstance(raw_group, Mapping):
            raise LedgerError("Codex hooks.json 的 UserPromptSubmit 项必须是对象")
        group = dict(raw_group)
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            raise LedgerError("Codex UserPromptSubmit 的 hooks 必须是列表")
        updated_handlers: List[Any] = []
        for handler in handlers:
            if _is_managed_codex_hook(handler):
                if not installed:
                    updated_handlers.append(managed)
                    installed = True
                else:
                    removed_duplicates += 1
            else:
                updated_handlers.append(handler)
        group["hooks"] = updated_handlers
        updated_groups.append(group)
    if not installed:
        updated_groups.append({"matcher": "*", "hooks": [managed]})
    hooks[CODEX_HOOK_EVENT] = updated_groups
    document["hooks"] = hooks
    rendered = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    changed = rendered != raw
    if changed:
        _atomic_text(path, rendered)
    result: Dict[str, Any] = {
        "path": str(path),
        "changed": changed,
        "installed": True,
        "removed_duplicates": removed_duplicates,
        "state_key": _managed_codex_hook_key(path, updated_groups),
    }
    if _uses_current_codex_home(home):
        runtime = ensure_codex_user_prompt_hook_trusted(
            package_root, path, expected_command=managed["command"],
        )
        result["runtime"] = {
            key: runtime[key] for key in ("checked", "found", "ready", "trust_status",
                                           "enabled", "timeout_seconds", "trust_written", "reason")
            if key in runtime
        }
    else:
        result["runtime"] = {"checked": False, "ready": False,
                             "reason": "隔离 home 不修改当前用户的 Codex 信任状态"}
    return result


def codex_user_prompt_hook_status(*, home: Optional[Path] = None,
                                  package_root: Optional[Path] = None,
                                  expected_command: str = "") -> Dict[str, Any]:
    """Inspect only HippoMind-owned hook entries; never reveal credentials."""
    path = codex_hooks_path(home=home)
    try:
        document, _raw = _load_codex_hooks(path)
        groups = document.get("hooks", {}).get(CODEX_HOOK_EVENT, [])
        count = sum(
            1 for group in groups if isinstance(group, Mapping)
            for handler in group.get("hooks", []) if _is_managed_codex_hook(handler)
        ) if isinstance(groups, list) else 0
        result: Dict[str, Any] = {"path": str(path), "installed": count == 1, "count": count}
        if package_root is not None and _uses_current_codex_home(home):
            runtime = _inspect_codex_user_prompt_hook(
                package_root, path, expected_command=expected_command,
            )
            result["runtime"] = {
                key: runtime[key] for key in ("checked", "found", "ready", "trust_status",
                                               "enabled", "timeout_seconds", "reason")
                if key in runtime
            }
        return result
    except LedgerError as exc:
        return {"path": str(path), "installed": False, "count": 0, "error": str(exc)}


def agent_rule_path(agent_id: str, *, home: Optional[Path] = None) -> Path:
    """Return the global instruction location used by each supported runtime."""
    home = Path(home).expanduser() if home else Path.home()
    agent = safe_agent_name(agent_id)
    locations = {
        "codex": home / ".codex" / "AGENTS.md",
        "pi": home / ".pi" / "agent" / "AGENTS.md",
        "claude": home / ".claude" / "CLAUDE.md",
        "cursor": home / ".cursor" / "rules" / "hippomind-memory-hub.mdc",
        "gemini": home / ".gemini" / "GEMINI.md",
        "deepseek": home / ".deepseek" / "AGENTS.md",
        "local": home / ".local-agent" / "AGENTS.md",
    }
    try:
        return locations[agent]
    except KeyError as exc:
        raise LedgerError("不支持的 Agent: " + agent) from exc


def parse_agents(values: Iterable[str]) -> Tuple[str, ...]:
    selected: List[str] = []
    for value in values:
        for raw in str(value or "").split(","):
            agent = safe_agent_name(raw)
            if agent == "all":
                for name in SUPPORTED_AGENTS:
                    if name not in selected:
                        selected.append(name)
            elif agent not in SUPPORTED_AGENTS:
                raise LedgerError("不支持的 Agent: " + agent)
            elif agent not in selected:
                selected.append(agent)
    return tuple(selected or SUPPORTED_AGENTS)


def _atomic_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _prepare_cursor_rule(path: Path) -> None:
    """Cursor rules need front matter when the managed rule is a new file."""
    if path.exists():
        return
    _atomic_text(path, "---\ndescription: HippoMind Memory Hub automatic context preflight\nalwaysApply: true\n---\n\n")


def _mcp_profile_path(config_path: Path) -> Path:
    return config_path.with_name(config_path.stem + "-mcp.json")


def _write_mcp_profile(path: Path, *, package_root: Path, ledger_path: Path,
                       agent_config: Path) -> Path:
    # This portable profile contains a path to a 0600 credential file, never
    # the bearer token. Each editor can import the profile using its own UI.
    profile = {
        "mcpServers": {
            "hippomind": {
                "command": sys.executable,
                "args": ["-m", "hippomind.mcp", "--ledger", str(ledger_path),
                         "--agent-token-file", str(agent_config)],
                "cwd": str(package_root),
            }
        }
    }
    _atomic_text(path, json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
    return path


def _snapshot_managed_file(path: Path) -> Tuple[bool, bytes, int]:
    """Capture one installer-owned file before a credential replacement."""
    try:
        if path.is_symlink() or not path.is_file():
            return False, b"", 0o600
        return True, path.read_bytes(), path.stat().st_mode & 0o777
    except FileNotFoundError:
        return False, b"", 0o600
    except OSError as exc:
        raise LedgerError(f"无法备份 Agent 配置: {path}") from exc


def _restore_managed_file(path: Path, snapshot: Tuple[bool, bytes, int]) -> None:
    """Restore a config atomically, or remove a file created by a failed install."""
    existed, payload, mode = snapshot
    if not existed:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".rollback.",
                                      suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, mode or 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _rollback_managed_files(
    snapshots: Sequence[Tuple[Path, Tuple[bool, bytes, int]]],
) -> None:
    """Restore installer-owned files in reverse write order."""
    first_error: Optional[OSError] = None
    for path, snapshot in reversed(snapshots):
        try:
            _restore_managed_file(path, snapshot)
        except OSError as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise LedgerError("Agent 安装失败且文件回滚不完整") from first_error


def _matching_credential(ledger: SQLiteLedger, config_path: Path, agent: str,
                         scopes: Sequence[str], permissions: Sequence[str]) -> Optional[Tuple[AgentCredential, Dict[str, Any]]]:
    try:
        config = load_agent_config(config_path)
        credential = ledger.resolve_agent_credential(config["token"])
    except LedgerError:
        return None
    if credential is None or credential.agent_id != agent:
        return None
    if tuple(credential.scopes) != tuple(scopes) or tuple(credential.permissions) != tuple(permissions):
        return None
    return credential, config


def install_agent(ledger: SQLiteLedger, paths: RuntimePaths, agent_id: str, *,
                  package_root: Path, home: Optional[Path] = None,
                  scopes: Optional[Sequence[str]] = None,
                  permissions: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Install one rule/config/profile trio without printing a secret."""
    agent = safe_agent_name(agent_id)
    if agent not in SUPPORTED_AGENTS:
        raise LedgerError("不支持的 Agent: " + agent)
    selected_scopes = tuple(dict.fromkeys(str(value).strip()[:128] for value in
                                          (scopes or ("shared", "agent:" + agent)) if str(value).strip()))
    selected_permissions = tuple(dict.fromkeys(str(value).strip() for value in
                                               (permissions or DEFAULT_PERMISSIONS) if str(value).strip()))
    config_path = agent_config_path(paths.state_path.parent, agent)
    matched = _matching_credential(ledger, config_path, agent, selected_scopes, selected_permissions)
    old_credential: Optional[AgentCredential] = None
    if matched is not None:
        credential, existing = matched
        token = str(existing["token"])
        credential_reused = True
    else:
        try:
            old = load_agent_config(config_path)
            old_credential = ledger.resolve_agent_credential(old["token"])
        except LedgerError:
            old_credential = None
        credential, token = ledger.issue_agent_credential(agent, scopes=selected_scopes,
                                                            permissions=selected_permissions)
        credential_reused = False
    rule_path = agent_rule_path(agent, home=home)
    mcp_profile_path = _mcp_profile_path(config_path)
    managed_paths = [config_path, rule_path, mcp_profile_path]
    if agent == "codex":
        managed_paths.append(codex_hooks_path(home=home))
    managed_snapshots = [(path, _snapshot_managed_file(path)) for path in managed_paths]
    try:
        write_agent_config(config_path, {
            "agent_id": agent,
            "token": token,
            "scopes": list(credential.scopes),
            "ledger": str(paths.ledger_path),
            "runtime_dir": str(paths.state_path.parent),
            "port": 0,
        })
        native_context_hook: Optional[Dict[str, Any]] = None
        if agent == "codex":
            native_context_hook = install_codex_user_prompt_hook(package_root, config_path, home=home)
        native_dsh_plugin: Optional[Dict[str, Any]] = None
        if agent == "deepseek":
            # The Cordis plugin may be unavailable (DSH not installed, profile not
            # assembled); every expected failure keeps the plain rule-file integration.
            native_dsh_plugin = install_dsh_integration(package_root, config_path, home=home)
        if agent == "cursor":
            _prepare_cursor_rule(rule_path)
        native_hook_runtime = native_context_hook.get("runtime", {}) if native_context_hook else {}
        native_codex_hook = bool(
            isinstance(native_hook_runtime, Mapping) and native_hook_runtime.get("ready")
        )
        native_deepseek_plugin = bool(
            isinstance(native_dsh_plugin, Mapping) and native_dsh_plugin.get("installed")
        )
        rule_changed, _ = install_agent_rules(
            rule_path, package_root, agent=agent, agent_config=config_path,
            native_codex_hook=native_codex_hook,
            native_deepseek_plugin=native_deepseek_plugin,
        )
        mcp_profile = _write_mcp_profile(
            mcp_profile_path, package_root=package_root,
            ledger_path=paths.ledger_path, agent_config=config_path,
        )
        # Real user-scope registration for standard MCP hosts (including Pi's
        # installed pi-mcp-adapter and the portable ~/.agents convention).
        # Failures degrade to the portable profile and never abort installation.
        try:
            mcp_registration = register_mcp_server(
                agent, package_root=package_root, ledger_path=paths.ledger_path,
                agent_config=config_path, home=home)
        except (OSError, LedgerError) as exc:
            mcp_registration = {"agent": agent, "supported": True, "registered": False,
                                "error": str(exc)[:300]}
    except Exception:
        if not credential_reused:
            ledger.revoke_agent_credential(credential.token_id)
        _rollback_managed_files(managed_snapshots)
        raise
    result = {
        "agent": agent,
        "rules": str(rule_path),
        "rules_changed": rule_changed,
        "config": str(config_path),
        "credential_id": credential.token_id,
        "credential_reused": credential_reused,
        "scopes": list(credential.scopes),
        "permissions": list(credential.permissions),
        "mcp_profile": str(mcp_profile),
        "mcp_registration": mcp_registration,
    }
    if native_context_hook is not None:
        result["native_context_hook"] = native_context_hook
    if native_dsh_plugin is not None:
        result["native_dsh_plugin"] = native_dsh_plugin
    # Do not invalidate a working integration until every replacement file and
    # native registration path has completed. A failed install can therefore be
    # retried without stranding the previous credential.
    if old_credential is not None and old_credential.token_id != credential.token_id:
        ledger.revoke_agent_credential(old_credential.token_id)
    return result


def install_agents(ledger: SQLiteLedger, paths: RuntimePaths, agents: Iterable[str], *,
                   package_root: Path, home: Optional[Path] = None,
                   scopes: Optional[Sequence[str]] = None,
                   permissions: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    return [install_agent(ledger, paths, agent, package_root=package_root, home=home,
                          scopes=scopes, permissions=permissions)
            for agent in parse_agents(agents)]


def agent_install_status(ledger: SQLiteLedger, paths: RuntimePaths, agents: Iterable[str], *,
                         home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return a non-secret diagnostic view of real rules and credentials."""
    result: List[Dict[str, Any]] = []
    package_root = Path(__file__).resolve().parents[1]
    for agent in parse_agents(agents):
        rule = agent_rule_path(agent, home=home)
        config_path = agent_config_path(paths.state_path.parent, agent)
        item: Dict[str, Any] = {"agent": agent, "rules": str(rule), "rules_installed": False,
                                "config": str(config_path), "credential_valid": False}
        if agent == "codex":
            expected_command = _managed_codex_hook_command(package_root, config_path)
            item["native_context_hook"] = codex_user_prompt_hook_status(
                home=home, package_root=package_root, expected_command=expected_command,
            )
        if agent == "deepseek":
            item["native_dsh_plugin"] = dsh_integration_status(home=home,
                                                               package_root=package_root)
            # A deployment-only check is insufficient: the Cordis plugin
            # emits this private health record only from apply().  Keep the
            # output non-secret and never surface unrelated chat content.
            try:
                probes = ledger.search("HippoMind DSH Cordis 插件 运行时装载",
                                       scope="agent:deepseek", limit=12,
                                       source_agents=["deepseek"])
                probe = next((row for row in probes
                              if isinstance(row.get("metadata"), Mapping)
                              and row["metadata"].get("runtime_probe") is True), None)
                item["native_dsh_plugin"]["runtime_probe"] = {
                    "seen": probe is not None,
                    **({"updated_at": probe.get("updated_at", ""),
                        "evidence": list(probe.get("evidence", []))[:1]} if probe is not None else {}),
                }
            except (LedgerError, OSError, ValueError):
                item["native_dsh_plugin"]["runtime_probe"] = {"seen": False}
        if mcp_registration_path(agent, home=home) is not None:
            # Live stdio handshake proves the entry an editor would actually
            # load; a broken registration shows up here instead of at runtime.
            item["mcp_registration"] = mcp_registration_status(agent, home=home,
                                                               runtime_check=True)
        try:
            item["rules_installed"] = RULES_BEGIN in rule.read_text("utf-8") and RULES_END in rule.read_text("utf-8")
        except OSError:
            pass
        try:
            config = load_agent_config(config_path)
            credential = ledger.resolve_agent_credential(config["token"])
            if credential is not None and credential.agent_id == agent:
                item.update({"credential_valid": True, "credential_id": credential.token_id,
                             "scopes": list(credential.scopes), "permissions": list(credential.permissions)})
        except LedgerError as exc:
            item["config_error"] = str(exc)
        result.append(item)
    return result
