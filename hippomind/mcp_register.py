"""Real user-scope MCP registration for agents with a standard MCP config.

Claude Code (``~/.claude.json``), Cursor (``~/.cursor/mcp.json``), Gemini CLI
(``~/.gemini/settings.json``), and Pi with its installed ``pi-mcp-adapter``
all have a real config location that the runtime reads.  Pi's adapter owns
``~/.pi/agent/mcp.json``; it is deliberately a Pi-owned override rather than
an invented key in ``settings.json``.  A managed ``~/.agents/mcp.json`` entry
also offers a standards-based discovery point for newly installed agents that
honour the portable user-global MCP convention.

Every write is additive: foreign keys are preserved verbatim and a file that
fails JSON parsing is never rewritten.  Registration entries reference the
0600 credential file path, never the token itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .ledger import LedgerError

MCP_SERVER_NAME = "hippomind"

# agent_id -> MCP config file (relative to the agent home) plus whether the
# file is exclusively an MCP config (safe to create from scratch).
_REGISTRATION_FILES: Dict[str, Tuple[str, bool]] = {
    # pi-mcp-adapter reads this native Pi-owned global override automatically.
    "pi": (".pi/agent/mcp.json", False),
    "claude": (".claude.json", False),
    "cursor": (".cursor/mcp.json", True),
    "gemini": (".gemini/settings.json", False),
    # Portable fallback for a newly installed MCP-aware agent.  It uses the
    # restricted ``local`` credential and never overwrites foreign servers.
    "local": (".agents/mcp.json", False),
}

_UNSUPPORTED_REASONS = {
    "codex": "Codex 使用 trusted native hook 接入，MCP 注册冗余",
    "deepseek": "DSH 使用 Cordis 原生插件接入，MCP 注册冗余",
}


def mcp_registration_path(agent_id: str, *, home: Optional[Path] = None) -> Optional[Path]:
    """Return the real MCP registration file for agents that have one."""
    entry = _REGISTRATION_FILES.get(str(agent_id or "").strip().lower())
    if entry is None:
        return None
    root = Path(home).expanduser() if home else Path.home()
    return root / entry[0]


def unsupported_reason(agent_id: str) -> str:
    agent = str(agent_id or "").strip().lower()
    return _UNSUPPORTED_REASONS.get(
        agent, "该 Agent 没有已确认的标准 MCP 注册接口，登记为待适配")


def server_entry(package_root: Path, ledger_path: Path, agent_config: Path) -> Dict[str, Any]:
    """The stdio server command registered into every editor config."""
    return {
        "command": sys.executable,
        "args": ["-m", "hippomind.mcp", "--ledger", str(ledger_path),
                 "--agent-token-file", str(agent_config)],
        "cwd": str(package_root),
    }


def _pi_adapter_status(home: Path) -> Dict[str, Any]:
    """Check the Pi-owned adapter declaration without executing Pi or a model.

    ``pi-mcp-adapter`` is a source-loaded Pi package.  Looking for its package
    entry gives a deterministic host-loader check and avoids starting a model
    merely to validate a configuration file.
    """
    settings_path = home / ".pi" / "agent" / "settings.json"
    status: Dict[str, Any] = {"settings_path": str(settings_path), "available": False}
    try:
        document = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        status["reason"] = "未找到 Pi settings.json"
        return status
    except (OSError, ValueError) as exc:
        status["reason"] = f"Pi settings.json 不可读: {exc}"
        return status
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, list):
        status["reason"] = "Pi settings.json 未声明 packages"
        return status
    for package in packages:
        if isinstance(package, str) and package.strip() == "npm:pi-mcp-adapter":
            status["available"] = True
            return status
        if isinstance(package, dict):
            values = [str(package.get(key, "")) for key in ("name", "source", "package")]
            if any(value == "npm:pi-mcp-adapter" or value == "pi-mcp-adapter" for value in values):
                status["available"] = True
                return status
    status["reason"] = "Pi 未安装 pi-mcp-adapter；保留规则文件回退"
    return status


def _load_registration_document(path: Path, mcp_only: bool) -> Tuple[Dict[str, Any], bool, str]:
    """Load (or create) the config document without ever losing foreign keys.

    Returns (document, created, error); error is non-empty when the existing
    file must not be touched.
    """
    if not path.exists():
        return ({}, True, "")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, False, f"配置文件不是合法 JSON，拒绝改写: {exc}"
    if not isinstance(document, dict):
        return {}, False, "配置文件根节点不是对象，拒绝改写"
    if mcp_only and document and "mcpServers" not in document:
        return {}, False, "专用 MCP 配置文件含有未知字段，拒绝改写"
    return document, False, ""


def _atomic_write(path: Path, document: Dict[str, Any], *, preserve_mode: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    if preserve_mode and path.exists():
        mode = path.stat().st_mode & 0o777
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp",
                                     dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode if mode is not None else 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _file_stamp(path: Path) -> Optional[Tuple[int, int]]:
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def register_mcp_server(agent_id: str, *, package_root: Path, ledger_path: Path,
                        agent_config: Path, home: Optional[Path] = None) -> Dict[str, Any]:
    """Add the HippoMind stdio server to the agent's real MCP config."""
    agent = str(agent_id or "").strip().lower()
    path = mcp_registration_path(agent, home=home)
    if path is None:
        return {"agent": agent, "supported": False, "registered": False,
                "reason": unsupported_reason(agent)}
    _, mcp_only = _REGISTRATION_FILES[agent]
    desired = server_entry(package_root, ledger_path, agent_config)
    # 读-改-写竞态防护：Claude Code 等宿主会高频整体重写自己的配置文件。
    # 写前比对 mtime/size，发现被并发修改就重新加载合并（有界重试），
    # 避免用旧快照覆盖掉宿主刚写入的键。
    for attempt in range(3):
        document, created, error = _load_registration_document(path, mcp_only)
        if error:
            return {"agent": agent, "supported": True, "registered": False,
                    "path": str(path), "error": error}
        stamp = None if created else _file_stamp(path)
        servers = document.get("mcpServers")
        if servers is None:
            servers = {}
            document["mcpServers"] = servers
        if not isinstance(servers, dict):
            return {"agent": agent, "supported": True, "registered": False,
                    "path": str(path), "error": "mcpServers 不是对象，拒绝改写"}
        changed = servers.get(MCP_SERVER_NAME) != desired
        servers[MCP_SERVER_NAME] = desired
        if not changed:
            return {"agent": agent, "supported": True, "registered": True,
                    "path": str(path), "changed": False, "created": created}
        if not created and stamp != _file_stamp(path) and attempt < 2:
            continue  # 宿主刚写过：重新加载合并
        try:
            _atomic_write(path, document, preserve_mode=not created)
        except OSError as exc:
            return {"agent": agent, "supported": True, "registered": False,
                    "path": str(path), "error": f"写入失败: {exc}"}
        return {"agent": agent, "supported": True, "registered": True,
                "path": str(path), "changed": changed, "created": created}
    return {"agent": agent, "supported": True, "registered": False,
            "path": str(path), "error": "配置文件并发写入冲突"}


def unregister_mcp_server(agent_id: str, *, home: Optional[Path] = None) -> Dict[str, Any]:
    """Remove only the HippoMind entry; every other key stays untouched."""
    agent = str(agent_id or "").strip().lower()
    path = mcp_registration_path(agent, home=home)
    if path is None:
        return {"agent": agent, "supported": False, "removed": False,
                "reason": unsupported_reason(agent)}
    _, mcp_only = _REGISTRATION_FILES[agent]
    document, _, error = _load_registration_document(path, mcp_only)
    if error:
        return {"agent": agent, "supported": True, "removed": False,
                "path": str(path), "error": error}
    servers = document.get("mcpServers")
    if not isinstance(servers, dict) or MCP_SERVER_NAME not in servers:
        return {"agent": agent, "supported": True, "removed": False,
                "path": str(path), "message": "未注册"}
    del servers[MCP_SERVER_NAME]
    try:
        _atomic_write(path, document, preserve_mode=True)
    except OSError as exc:
        return {"agent": agent, "supported": True, "removed": False,
                "path": str(path), "error": f"写入失败: {exc}"}
    return {"agent": agent, "supported": True, "removed": True, "path": str(path)}


def _runtime_handshake(entry: Dict[str, Any], *, timeout: float = 15.0) -> Dict[str, Any]:
    """Prove the *registered* command actually speaks MCP over stdio."""
    command = [str(entry.get("command", ""))] + [str(value) for value in entry.get("args", [])]
    if not command[0]:
        return {"checked": True, "ready": False, "error": "注册项缺少 command"}
    try:
        process = subprocess.Popen(
            command, cwd=str(entry.get("cwd") or "") or None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace")
    except OSError as exc:
        return {"checked": True, "ready": False, "error": f"启动失败: {exc}"}
    request = "".join(json.dumps(payload) + "\n" for payload in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "hippomind-verify", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ))
    try:
        stdout, stderr = process.communicate(request, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return {"checked": True, "ready": False, "error": f"初始化握手超时（>{timeout:.0f}s）"}
    ready = False
    tools = 0
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == 1 and isinstance(message.get("result"), dict):
            ready = True
        if message.get("id") == 2 and isinstance(message.get("result"), dict):
            listing = message["result"].get("tools")
            tools = len(listing) if isinstance(listing, list) else 0
    if not ready:
        return {"checked": True, "ready": False,
                "error": f"未收到 initialize 响应: {stderr.strip()[:200]}"}
    return {"checked": True, "ready": True, "tools": tools}


def mcp_registration_status(agent_id: str, *, home: Optional[Path] = None,
                            runtime_check: bool = False) -> Dict[str, Any]:
    """Non-secret view of the real registration, with optional live handshake."""
    agent = str(agent_id or "").strip().lower()
    root = Path(home).expanduser() if home else Path.home()
    path = mcp_registration_path(agent, home=root)
    if path is None:
        return {"supported": False, "registered": False, "reason": unsupported_reason(agent)}
    status: Dict[str, Any] = {"supported": True, "path": str(path), "registered": False}
    if not path.exists():
        status["message"] = "配置文件不存在"
        return status
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        status["error"] = f"配置文件不可读: {exc}"
        return status
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        status["message"] = "未注册 hippomind"
        return status
    args = [str(value) for value in entry.get("args", [])]
    status["registered"] = True
    status["command"] = str(entry.get("command", ""))
    status["uses_hippomind_mcp"] = any("hippomind.mcp" in value for value in args)
    if agent == "pi":
        # The registration is meaningful only when Pi's installed extension
        # actually loads MCP configs.  This is a local, no-model check.
        status["pi_adapter"] = _pi_adapter_status(root)
    if runtime_check:
        runtime = _runtime_handshake(entry)
        if agent == "pi":
            runtime["host_loader_ready"] = bool(status["pi_adapter"].get("available"))
            runtime["ready"] = bool(runtime.get("ready")) and runtime["host_loader_ready"]
            if not runtime["ready"] and not runtime.get("error"):
                runtime["error"] = str(status["pi_adapter"].get("reason", "Pi MCP adapter 不可用"))
        status["runtime"] = runtime
    return status


__all__ = ["MCP_SERVER_NAME", "LedgerError", "mcp_registration_path",
           "mcp_registration_status", "register_mcp_server", "server_entry",
           "unregister_mcp_server", "unsupported_reason"]
