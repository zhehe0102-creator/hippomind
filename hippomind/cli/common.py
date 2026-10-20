"""Leaf helpers shared by every CLI module.

Nothing here imports another ``hippomind.cli`` module, which makes this the
first layer of the package and keeps the import graph acyclic.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict
from ..agent_config import load_agent_config
from ..ledger import LedgerError

def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _agent_settings(args) -> Dict[str, Any]:
    path = str(getattr(args, "agent_config", "") or "").strip()
    return load_agent_config(path) if path else {}


def _reject_restricted_direct(args, operation: str) -> None:
    if str(getattr(args, "agent_config", "") or "").strip():
        raise LedgerError(f"受限 Agent 不能绕过 daemon 直接执行 {operation}")


def _connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ledger", help="SQLite/WAL 账本路径")
    parser.add_argument("--runtime-dir", help="daemon 状态/token 目录")
    parser.add_argument("--port", type=int, help="daemon HTTP 端口，默认 8765")
    parser.add_argument("--agent-config", help="受限 Agent 的 0600 JSON 凭据文件")


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
