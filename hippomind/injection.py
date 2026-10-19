"""Persistent local switch for automatic Context Pack injection."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator

from .ledger import LedgerError


STATE_FILE_NAME = "context-injection.json"
MAX_STATE_BYTES = 64 * 1024
_AGENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TRUE_STRINGS = {"1", "true", "yes", "on"}
_FALSE_STRINGS = {"0", "false", "no", "off"}


def _strict_bool(value: Any, field: str) -> bool:
    """Parse a persisted switch strictly; unknown shapes fail closed."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
    elif isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    raise LedgerError(f"上下文开关配置字段 {field} 必须是布尔值；为保护隐私已停止自动注入")


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Best-effort advisory lock serialising read-modify-write updates."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX fallback
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def normalise_agent(agent: str) -> str:
    value = str(agent or "all").strip().lower()
    if value in {"", "*", "default", "global"}:
        return "all"
    if not _AGENT_RE.fullmatch(value):
        raise LedgerError("Agent 名称只能包含字母、数字、点、下划线和连字符")
    return value


class InjectionState:
    """0600 JSON setting with a default and per-Agent overrides."""

    def __init__(self, runtime_dir: os.PathLike | str):
        self.path = Path(runtime_dir).expanduser() / STATE_FILE_NAME

    @staticmethod
    def _default() -> Dict[str, Any]:
        return {"version": 1, "default_enabled": True, "agents": {}}

    def _read(self) -> Dict[str, Any]:
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise LedgerError("上下文开关配置超过安全上限")
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._default()
        except (OSError, ValueError, TypeError) as exc:
            raise LedgerError("无法读取上下文开关配置；为保护隐私已停止自动注入") from exc
        if not isinstance(value, dict) or not isinstance(value.get("agents", {}), dict):
            raise LedgerError("上下文开关配置格式无效；为保护隐私已停止自动注入")
        return {
            "version": 1,
            "default_enabled": _strict_bool(value.get("default_enabled", True),
                                            "default_enabled"),
            "agents": {normalise_agent(name): _strict_bool(enabled, f"agents.{name}")
                       for name, enabled in value["agents"].items()
                       if isinstance(name, str) and _AGENT_RE.fullmatch(name)},
        }

    def status(self, agent: str = "all") -> Dict[str, Any]:
        selected = normalise_agent(agent)
        data = self._read()
        agents = data["agents"]
        overridden = selected != "all" and selected in agents
        enabled = bool(agents[selected]) if overridden else bool(data["default_enabled"])
        return {"agent": selected, "enabled": enabled, "overridden": overridden,
                "default_enabled": bool(data["default_enabled"]), "path": str(self.path)}

    def update(self, action: str, agent: str = "all") -> Dict[str, Any]:
        selected = normalise_agent(agent)
        value = str(action or "status").strip().lower()
        if value == "status":
            return self.status(selected)
        if value not in {"on", "off", "toggle"}:
            raise LedgerError("注入开关只支持 on、off、toggle 或 status")
        # 读-改-写全程持有排他锁：并发 Hook 同时切换开关时不会丢失更新。
        with _exclusive_lock(self.path):
            data = self._read()
            current = self.status(selected)["enabled"]
            enabled = {"on": True, "off": False, "toggle": not current}[value]
            if selected == "all":
                # ``all`` is the simple global switch.  Do not leave an old
                # per-Agent override silently bypassing the user's choice.
                data["default_enabled"] = enabled
                data["agents"] = {}
            else:
                data["agents"][selected] = enabled
            self._write(data)
        result = self.status(selected)
        result["changed"] = True
        return result

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="." + self.path.name + ".", suffix=".tmp",
                                                 dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
