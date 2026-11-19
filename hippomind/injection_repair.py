"""注入面的自动修复：把审计出的坏配置改成指向生产路径。

修复原则（与审计同样保守）：

* **只改 hippomind 自己的条目**。别人的 hook / MCP server 一个字节都不动。
* **先备份再写**。每个被修改的文件在改动前落一份 .bak-hippomind-<ts>。
* **可 dry-run**。默认只报告将要改什么，--apply 才落盘。
* **失败不留半成品**。写临时文件 + os.replace 原子替换。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .injection_audit import _read_json

MARKER = "hippomind"


def _backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = path.with_name(path.name + ".bak-hippomind-" + stamp)
    shutil.copy2(path, target)
    return target


def _atomic_text_public(path: Path, text: str) -> None:
    """原子写文本（供规则文件复用；与 _atomic_json 同样的临时文件+replace 语义）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".hippomind.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".hippomind.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def _rewrite_strings(node: Any, replacements: Mapping[str, str]) -> Any:
    """递归替换 JSON 结构里的字符串；未命中的原样返回。"""
    if isinstance(node, str):
        return replacements.get(node, node)
    if isinstance(node, Mapping):
        return {key: _rewrite_strings(value, replacements) for key, value in node.items()}
    if isinstance(node, list):
        return [_rewrite_strings(value, replacements) for value in node]
    return node


#: 内置盘上的运行时副本。后台进程（launchd / agent hook / MCP 子进程）**必须**
#: 从这里启动：macOS 的 TCC 会拒绝后台进程读取可移动卷，把 cwd 或包路径指向
#: /Volumes/ 会让 hook 静默失败——失败是静默的，看起来像"配置没生效"。
def stable_runtime_root(ledger_path: Path) -> Path:
    """返回内置盘上的运行时根目录（与 install-autostart.sh 同步的那份）。"""
    return ledger_path.parent / "runtime"


def _runtime_package_root(ledger_path: Path, fallback: Path) -> Path:
    """优先用内置盘 runtime；不存在时退回调用方给的路径（仅用于报告）。"""
    runtime = stable_runtime_root(ledger_path)
    return runtime if (runtime / "hippomind").is_dir() else fallback


def repair_mcp_server(path: Path, *, ledger_path: Path, agent_config: Path,
                      package_root: Path, python: str) -> Dict[str, Any]:
    """把某个 agent 的 hippomind MCP 条目重写为指向生产账本。"""
    data, error = _read_json(path)
    if data is None and error != "missing":
        return {"path": str(path), "changed": False, "reason": "unreadable: " + error}
    if not isinstance(data, Mapping):
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = data.get("mcp") if isinstance(data.get("mcp"), dict) else {}
    # cwd 必须是内置盘 runtime：MCP server 由 agent 以子进程启动，
    # 若 cwd 在 /Volumes/ 上，TCC 会让它读不到账本并静默退出。
    runtime_root = _runtime_package_root(ledger_path, package_root)
    entry = {
        "command": python,
        "args": ["-m", "hippomind.mcp", "--ledger", str(ledger_path),
                 "--agent-token-file", str(agent_config)],
        "cwd": str(runtime_root),
    }
    servers = dict(servers)
    servers[MARKER] = entry
    updated = dict(data)
    if "mcp" in updated and not isinstance(updated.get("mcpServers"), dict):
        updated["mcp"] = servers
    else:
        updated["mcpServers"] = servers
    # 内容完全相同时不标记为 changed：否则每次运行都会重写一遍文件
    # （会不断产生新备份，也会让 --apply 看起来永远有改动）。
    changed = updated != data
    return {"path": str(path), "changed": changed, "entry": entry,
            "reason": "" if changed else "already current", "_data": updated}


def repair_codex_hook(path: Path, *, package_root: Path, agent_config: Path,
                      python: str, ledger_path: Path) -> Dict[str, Any]:
    """重写 Codex hook 命令，把临时路径换成内置盘上的稳定路径。"""
    data, error = _read_json(path)
    if data is None:
        return {"path": str(path), "changed": False, "reason": error or "missing"}
    if not isinstance(data, Mapping):
        return {"path": str(path), "changed": False, "reason": "root not object"}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {"path": str(path), "changed": False, "reason": "no hooks"}

    # 稳定的命令：cwd 与 PYTHONPATH 都指向内置盘 runtime。
    # 不依赖 Codex 继承的环境变量，也不依赖外置卷（TCC 会拒绝）。
    runtime_root = _runtime_package_root(ledger_path, package_root)
    # **必须给每个路径加引号**：本机的运行时与配置都在
    # "Application Support" 下，含空格。不加引号时 shell 会把路径拆成两段，
    # hook 拿到一个不存在的 --agent-config 并静默失败——这正是这次事故的形态。
    command = (
        "cd %s && PYTHONPATH=%s %s -m hippomind.codex_hook --managed "
        "--trigger-mode always --agent-config %s"
        % (shlex.quote(str(runtime_root)), shlex.quote(str(runtime_root)),
           shlex.quote(str(python)), shlex.quote(str(agent_config)))
    )
    changed = False
    updated_hooks = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            updated_hooks[event] = groups
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, Mapping):
                new_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                new_groups.append(group)
                continue
            new_handlers = []
            for handler in handlers:
                if (isinstance(handler, Mapping)
                        and isinstance(handler.get("command"), str)
                        and MARKER in handler["command"]):
                    replacement = dict(handler)
                    replacement["command"] = command
                    new_handlers.append(replacement)
                    changed = True
                else:
                    new_handlers.append(handler)
            new_group = dict(group)
            new_group["hooks"] = new_handlers
            new_groups.append(new_group)
        updated_hooks[event] = new_groups
    updated = dict(data)
    updated["hooks"] = updated_hooks
    # 内容相同就不算 changed，保证幂等（否则每次都重写 + 新建备份）。
    if changed and updated == data:
        changed = False
    return {"path": str(path), "changed": changed, "command": command,
            "reason": "" if changed else "already current", "_data": updated}


def repair_claude_hook(path: Path, *, package_root: Path, agent_config: Path,
                      python: str, ledger_path: Path) -> Dict[str, Any]:
    """重写 Claude settings.json 里的 hippomind hook 命令。

    与 repair_codex_hook 的差别只有两处，但两处都很关键：

    1. **文件形状不同**。Claude 把 hook 放在 settings.json，这个文件里还有
       用户自己的 model / env / permissions——只能就地替换带
       hippomind.claude_hook 的那条 handler，其它键一个字节都不能动。
    2. **必须收敛重复**。历史上的正则修补每跑一次就多叠一层坏命令，所以这里
       保留第一条、删除其余，而不是逐条修补（逐条修只会得到 N 条一样的坏命令）。
    """
    data, error = _read_json(path)
    if data is None:
        return {"path": str(path), "changed": False, "reason": error or "missing"}
    if not isinstance(data, Mapping):
        return {"path": str(path), "changed": False, "reason": "root not object"}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {"path": str(path), "changed": False, "reason": "no hooks"}

    runtime_root = _runtime_package_root(ledger_path, package_root)
    # 命令形态由 hook 模块自己定义：安装器与修复器必须产出同一个字符串，
    # 否则每次审计都会把"已经正确"的配置判成需要修复。
    from .claude_hook import managed_command
    command = managed_command(package_root=runtime_root, agent_config=agent_config,
                              python=python)
    changed = False
    removed = 0
    updated_hooks = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            updated_hooks[event] = groups
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, Mapping):
                new_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                new_groups.append(group)
                continue
            new_handlers = []
            kept = False
            for handler in handlers:
                managed = (isinstance(handler, Mapping)
                           and isinstance(handler.get("command"), str)
                           and "hippomind.claude_hook" in handler["command"])
                if not managed:
                    new_handlers.append(handler)
                    continue
                if kept:
                    removed += 1
                    changed = True
                    continue
                replacement = dict(handler)
                replacement["command"] = command
                replacement.setdefault("timeout", 12)
                new_handlers.append(replacement)
                kept = True
                if handler.get("command") != command:
                    changed = True
            new_group = dict(group)
            new_group["hooks"] = new_handlers
            new_groups.append(new_group)
        updated_hooks[event] = new_groups
    updated = dict(data)
    updated["hooks"] = updated_hooks
    if changed and updated == data:
        changed = False
    return {"path": str(path), "changed": changed, "command": command,
            "removed_duplicates": removed,
            "reason": "" if changed else "already current", "_data": updated}


def apply_repairs(repairs: List[Dict[str, Any]], *, dry_run: bool = True
                  ) -> List[Dict[str, Any]]:
    """执行修复。dry_run 时只报告，不写盘。"""
    results: List[Dict[str, Any]] = []
    for repair in repairs:
        path = Path(repair["path"])
        record = {"path": str(path), "changed": bool(repair.get("changed")),
                  "reason": repair.get("reason", "")}
        if not repair.get("changed"):
            record["applied"] = False
            results.append(record)
            continue
        if dry_run:
            # dry-run 也要如实标出"这条会被改"，否则预览看起来像什么都没做。
            record["would_change"] = True
            record["applied"] = False
            results.append(record)
            continue
        try:
            record["backup"] = str(_backup(path) or "")
            _atomic_json(path, repair["_data"])
            record["applied"] = True
        except OSError as exc:
            record["applied"] = False
            record["reason"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        results.append(record)
    return results
