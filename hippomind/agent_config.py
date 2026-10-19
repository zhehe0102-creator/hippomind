"""Private per-agent connection files for HippoMind integrations.

The files contain a scoped bearer credential returned once by the ledger. They
are deliberately separate from editor configuration so a rule/MCP file can
refer to a path without embedding a token in a broadly readable project file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping

from .ledger import LedgerError


CONFIG_VERSION = 1


def safe_agent_name(value: object) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "", str(value or "").strip().lower())
    if not name:
        raise LedgerError("agent 名称不能为空")
    return name[:80]


def agent_config_path(runtime_dir: os.PathLike, agent_id: str) -> Path:
    return Path(runtime_dir).expanduser() / "agents" / (safe_agent_name(agent_id) + ".json")


def _secure_mode(path: Path) -> bool:
    try:
        return not bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def load_agent_config(path: os.PathLike, *, require_secure: bool = True) -> Dict[str, Any]:
    target = Path(path).expanduser()
    try:
        if require_secure and os.name != "nt" and not _secure_mode(target):
            raise LedgerError(f"agent config 权限过宽: {target}")
        if target.stat().st_size > 64 * 1024:
            raise LedgerError("agent config 超过 64 KiB 安全上限")
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LedgerError(f"agent config 不存在: {target}") from exc
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, LedgerError):
            raise
        raise LedgerError(f"agent config 无效: {target}") from exc
    if not isinstance(data, Mapping):
        raise LedgerError("agent config 根节点必须是对象")
    try:
        port = int(data.get("port", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise LedgerError("agent config.port 必须是整数") from exc
    token = str(data.get("token", "")).strip()
    agent_id = safe_agent_name(data.get("agent_id", ""))
    if len(token) < 24:
        raise LedgerError("agent config 缺少有效 credential")
    scopes = data.get("scopes", [])
    if isinstance(scopes, str):
        scopes = [scopes]
    if not isinstance(scopes, list):
        raise LedgerError("agent config.scopes 必须是列表")
    return {
        "version": int(data.get("version", CONFIG_VERSION)),
        "agent_id": agent_id,
        "token": token,
        "scopes": [str(value).strip()[:128] for value in scopes if str(value).strip()][:64],
        "ledger": str(data.get("ledger", "")).strip(),
        "runtime_dir": str(data.get("runtime_dir", "")).strip(),
        "port": port,
        "path": str(target),
    }


def write_agent_config(path: os.PathLike, data: Mapping[str, Any]) -> Path:
    target = Path(path).expanduser()
    payload = dict(data)
    payload["version"] = CONFIG_VERSION
    payload["agent_id"] = safe_agent_name(payload.get("agent_id", ""))
    token = str(payload.get("token", "")).strip()
    if len(token) < 24:
        raise LedgerError("agent credential 至少需要 24 个字符")
    payload["token"] = token
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
