"""Agent 注入面的自动检测与自动修复。

设计目标（2026-10-24 生产事故驱动）：

一次真实事故暴露了三类问题，本模块逐个解决：

1. **配置存在但从不工作**。Codex 的 UserPromptSubmit hook 写进了
   `~/.codex/hooks.json`，但命令里的 `--agent-config` 指向一个
   `/var/folders/.../tmpXXXX/` 临时目录——那是安装时的工作目录，重启即失效。
   状态命令只看"有没有这条 hook"，于是报告"已安装"，实际每轮都静默失败。

2. **指向错误的账本**。gemini / cursor 的 MCP profile 把 `--ledger` 指向
   一个 0 条记忆的临时测试库，而生产库有 26 万条。agent 拿到的是空记忆。

3. **cwd 在外置卷**。hook / MCP 的 `cwd` 指向可移动卷上的仓库，
   launchd 与后台进程会因 macOS TCC 被拒绝。

因此检测的判据不是"文件里有没有 hippomind 字样"，而是：
**这条注入路径现在能不能真的读到生产账本**。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: 一次注入配置被认为"健康"的必要条件。全部可机器判定，没有主观项。
HEALTHY = "healthy"
#: 配置在，但指向的路径已经不存在（典型：临时目录被清理）。
STALE_PATH = "stale_path"
#: 配置在，但指向的账本不是生产账本（典型：指向空的测试库）。
WRONG_LEDGER = "wrong_ledger"
#: 配置在，但工作目录在可移动卷上（launchd/后台进程会被 TCC 拒绝）。
VOLATILE_CWD = "volatile_cwd"
#: 完全没有这条接入。
MISSING = "missing"
#: 配置指向的**模块**在运行时副本里不存在（路径对、代码没同步）。
#: 这一条是 2026-10-25 新增的：claude hook 装好后命令、路径、账本全部
#: 正确，审计报 healthy，但真跑起来报 No module named hippomind.claude_hook ——
#: 因为内置盘 runtime 副本是用 rsync --delete 同步的，新模块没进去。
#: "路径能解析"和"代码在那儿"是两件事，只查前者会漏掉整类失败。
NOT_SYNCED = "not_synced"
#: 存在但无法解析（坏 JSON / 权限不足），不猜测。
UNREADABLE = "unreadable"

#: 可移动卷挂载前缀。macOS 的 TCC 对后台进程读取这些路径会拒绝。
VOLATILE_PREFIXES = ("/Volumes/",)

#: 例外：**挂在可移动卷上的磁盘映像**不受同一条 TCC 限制。
#: 实测（2026-10-26）launchd 后台任务直接读 /Volumes/<外置卷> 会被
#: Operation not permitted 拒绝，但对同一块盘上 .dmg 挂载出来的卷
#: 可以正常 open/read/write 甚至跑 SQLite。这使"数据放在外置盘"成为
#: 可行方案（见 docs/PORTABILITY.md 的 external disk 一节）。
#: 判据用 hdiutil info 的真实挂载表，不看路径字符串——把映像当成
#: "可移动卷"是假阳性，会把一个能正常工作的部署判成坏掉。
_IMAGE_MOUNT_CACHE = None
_IMAGE_CACHE_TTL = 5.0


def _image_mount_points():
    """当前由磁盘映像挂载出来的卷的挂载点集合（带 5s 缓存）。"""
    global _IMAGE_MOUNT_CACHE
    import subprocess
    import time
    now = time.monotonic()
    if _IMAGE_MOUNT_CACHE is not None and now - _IMAGE_MOUNT_CACHE[0] < _IMAGE_CACHE_TTL:
        return _IMAGE_MOUNT_CACHE[1]
    points = set()
    try:
        out = subprocess.run(["/usr/bin/hdiutil", "info"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.startswith("/dev/"):
                for part in line.split("	")[1:]:
                    part = part.strip()
                    if part.startswith("/Volumes/"):
                        points.add(part)
    except Exception:
        points = set()
    result = frozenset(points)
    _IMAGE_MOUNT_CACHE = (now, result)
    return result


def _is_image_mount(value):
    """这个路径是否落在**由磁盘映像挂载出来的卷**上。"""
    for point in _image_mount_points():
        if value == point or value.startswith(point + "/"):
            return True
    return False

#: 临时目录前缀。安装期的工作目录一旦落在这些位置，重启后必然失效。
TEMP_PREFIXES = ("/var/folders/", "/tmp/", "/private/var/folders/", "/private/tmp/")


@dataclass
class Finding:
    """一条注入路径的检测结论。"""

    agent: str
    surface: str
    path: str
    status: str
    detail: str = ""
    #: 修复时需要替换的旧值（例如旧 ledger 路径）。
    stale_values: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"agent": self.agent, "surface": self.surface, "path": self.path,
                "status": self.status, "detail": self.detail,
                "stale_values": list(self.stale_values)}


def _is_temp(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in TEMP_PREFIXES)


def _is_volatile(value: str) -> bool:
    if not any(value.startswith(prefix) for prefix in VOLATILE_PREFIXES):
        return False
    # 磁盘映像挂载出来的卷走的是另一条路径，TCC 不会拒绝（实测）。
    return not _is_image_mount(value)


def _iter_strings(node: Any) -> List[str]:
    """收集 JSON 结构里所有字符串，用于判断它指向了哪里。"""
    if isinstance(node, str):
        return [node]
    if isinstance(node, Mapping):
        out: List[str] = []
        for value in node.values():
            out.extend(_iter_strings(value))
        return out
    if isinstance(node, list):
        out = []
        for value in node:
            out.extend(_iter_strings(value))
        return out
    return []


#: 解释器路径不算"数据路径"。macOS 上 Python 可能装在可移动卷的 Xcode 里
#: （本机就是 /Volumes/XcodeDev/Xcode.app/...），那是**只读的解释器**，
#: 不是会被 TCC 拒绝的账本/工作目录。把它判成 volatile 是假阳性。
_INTERPRETER_MARKERS = ("/bin/python", "/bin/node", "/bin/bash", "/bin/sh",
                        ".app/Contents/", "/usr/bin/", "/bin/")


def _is_interpreter(value: str) -> bool:
    return any(marker in value for marker in _INTERPRETER_MARKERS)


def _classify_paths(strings: Sequence[str], *, ledger_path: Path,
                    require_ledger: bool) -> Tuple[str, str, List[str]]:
    """把一组路径字符串归类成 (status, detail, stale_values)。"""
    stale: List[str] = []
    for value in strings:
        if not (value.startswith("/") or value.startswith("~")):
            continue
        expanded = str(Path(value).expanduser())
        if _is_interpreter(expanded):
            # 解释器只要求存在；它是不是在可移动卷上不影响 TCC 对数据的判定。
            continue
        if _is_temp(expanded) and not Path(expanded).exists():
            stale.append(value)
        elif _is_volatile(expanded):
            stale.append(value)
    if stale:
        return STALE_PATH, "指向已失效或不可达的路径（临时目录/可移动卷）", stale

    # 账本是否指向生产库。
    if require_ledger:
        found_ledger = False
        wrong: List[str] = []
        for value in strings:
            if not value.endswith((".sqlite", ".db")):
                continue
            expanded = str(Path(value).expanduser())
            if expanded == str(ledger_path):
                found_ledger = True
            elif _is_temp(expanded):
                wrong.append(value)
        if wrong:
            return WRONG_LEDGER, "指向非生产账本（临时/测试库）", wrong
        if not found_ledger:
            return WRONG_LEDGER, "未指向生产账本 %s" % ledger_path, []
    return HEALTHY, "", []


def _read_json(path: Path) -> Tuple[Optional[Any], str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as exc:
        return None, "%s: %s" % (type(exc).__name__, str(exc)[:160])


def inspect_mcp_server(agent: str, path: Path, *, ledger_path: Path) -> Finding:
    """检查一个 agent 的 MCP 配置里 hippomind 那条是否可用。"""
    if not path.exists():
        return Finding(agent, "mcp", str(path), MISSING, "配置文件不存在")
    data, error = _read_json(path)
    if data is None:
        return Finding(agent, "mcp", str(path), UNREADABLE, error)
    servers = None
    if isinstance(data, Mapping):
        servers = data.get("mcpServers") or data.get("mcp")
    if not isinstance(servers, Mapping):
        return Finding(agent, "mcp", str(path), MISSING, "没有 mcpServers 段")
    entry = servers.get("hippomind")
    if entry is None:
        return Finding(agent, "mcp", str(path), MISSING, "未注册 hippomind")
    strings = _iter_strings(entry)
    status, detail, stale = _classify_paths(strings, ledger_path=ledger_path,
                                            require_ledger=True)
    return Finding(agent, "mcp", str(path), status, detail, stale)


def inspect_codex_hook(path: Path, *, ledger_path: Path) -> Finding:
    """Codex 原生 hook 的可用性检查（含命令里的路径）。"""
    if not path.exists():
        return Finding("codex", "hook", str(path), MISSING, "hooks.json 不存在")
    data, error = _read_json(path)
    if data is None:
        return Finding("codex", "hook", str(path), UNREADABLE, error)
    hooks = data.get("hooks") if isinstance(data, Mapping) else None
    commands: List[str] = []
    if isinstance(hooks, Mapping):
        for groups in hooks.values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, Mapping):
                    continue
                for handler in group.get("hooks") or []:
                    if isinstance(handler, Mapping):
                        command = handler.get("command")
                        if isinstance(command, str) and "hippomind" in command:
                            commands.append(command)
    if not commands:
        return Finding("codex", "hook", str(path), MISSING, "未安装 hippomind hook")

    # 命令里的绝对路径要能解析。
    # 关键：**必须引号感知**。macOS 的 "Application Support" 含空格，用
    # shlex.split 会把未加引号的路径切成两段（".../Application" + "Support/..."），
    # 半截路径当然不存在，于是一条完全正常的 hook 被误报成 stale_path。
    # 这里按 shell 引号先切出"带引号的整体"，再处理去掉引号段后的裸 token。
    referenced: List[str] = []
    for command in commands:
        quoted = re.findall(r"'([^']*)'", command)
        bare = re.sub(r"'[^']*'", " ", command).split()
        for token in list(quoted) + bare:
            if "=" in token and not token.startswith("/"):
                token = token.split("=", 1)[1]
            if not token.startswith("/") or _is_interpreter(token):
                continue
            referenced.append(token)

    status, detail, stale = _classify_paths(referenced, ledger_path=ledger_path,
                                            require_ledger=False)
    if status != HEALTHY:
        return Finding("codex", "hook", str(path), status, detail, stale)
    missing = [value for value in referenced
               if value.endswith(".json") and not Path(value).exists()]
    if missing:
        return Finding("codex", "hook", str(path), STALE_PATH,
                       "agent-config 不存在", missing)
    absent_root = _module_missing(commands[0], module="hippomind.codex_hook")
    if absent_root:
        return Finding("codex", "hook", str(path), NOT_SYNCED,
                       "运行时副本缺少 hippomind/codex_hook.py，需重新同步 runtime",
                       [absent_root])
    return Finding("codex", "hook", str(path), HEALTHY)


def _module_missing(command: str, *, module: str) -> str:
    """Return the package root the command runs from when module is absent.

    The check is deliberately filesystem-only: importing the module would
    execute it, and an audit must never run target code.  A hook command has
    the shape ``cd <root> && [PYTHONPATH=<root>] <python> -m <module> ...``, so
    "does <root>/<module path>.py exist" is exactly the question a real
    invocation would answer with ImportError.

    Returns "" when the module is present (or when the command does not pin a
    root we can check), otherwise the root that is missing it.
    """
    roots: List[str] = []
    for quoted in re.findall(r"'([^']*)'", command):
        roots.append(quoted)
    bare = re.sub(r"'[^']*'", " ", command).split()
    for index, token in enumerate(bare):
        if token == "cd" and index + 1 < len(bare):
            roots.append(bare[index + 1])
        if token.startswith("PYTHONPATH="):
            roots.append(token.split("=", 1)[1])
    relative = module.replace(".", "/") + ".py"
    for root in roots:
        if not root.startswith("/"):
            continue
        try:
            if not Path(root).is_dir():
                continue
        except OSError:
            continue
        if not (Path(root) / relative).is_file():
            return root
    return ""


def inspect_claude_hook(path: Path, *, ledger_path: Path) -> Finding:
    """Claude Code settings.json 里 hippomind UserPromptSubmit hook 的可用性。

    判据与 Codex hook 一致：命令里引用的绝对路径必须现在就能解析。
    区别在于文件形状——Claude 把 hook 放在 settings.json 的 hooks 段里，
    和用户自己的其它设置同处一个文件，所以这里只读不写，且只关心带
    hippomind.claude_hook 的那一条 handler。

    这条检查存在的理由：claude 是本机唯一每天都有真实回复的 agent，但它
    历史上只有 rules + mcp 两个**被动**面（拉取式），从来没有被注入过。
    没有这条检查，"claude 装了注入"就又是一句无法验证的声明。
    """
    if not path.exists():
        return Finding("claude", "hook", str(path), MISSING, "settings.json 不存在")
    data, error = _read_json(path)
    if data is None:
        return Finding("claude", "hook", str(path), UNREADABLE, error)
    hooks = data.get("hooks") if isinstance(data, Mapping) else None
    commands: List[str] = []
    if isinstance(hooks, Mapping):
        for groups in hooks.values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, Mapping):
                    continue
                for handler in group.get("hooks") or []:
                    if not isinstance(handler, Mapping):
                        continue
                    command = handler.get("command")
                    if isinstance(command, str) and "hippomind.claude_hook" in command:
                        commands.append(command)
    if not commands:
        return Finding("claude", "hook", str(path), MISSING,
                       "未安装 hippomind.claude_hook")
    if len(commands) > 1:
        return Finding("claude", "hook", str(path), STALE_PATH,
                       "存在 %d 条重复的 hippomind hook（历史上字符串修补叠加的产物）"
                       % len(commands), commands)

    # 与 codex hook 相同的引号感知切分：Application Support 含空格，
    # 用非空白字符类切分会把路径切成半截，把正常 hook 误报成 stale。
    referenced: List[str] = []
    for command in commands:
        quoted = re.findall(r"'([^']*)'", command)
        bare = re.sub(r"'[^']*'", " ", command).split()
        for token in list(quoted) + bare:
            if "=" in token and not token.startswith("/"):
                token = token.split("=", 1)[1]
            if not token.startswith("/") or _is_interpreter(token):
                continue
            referenced.append(token)

    status, detail, stale = _classify_paths(referenced, ledger_path=ledger_path,
                                            require_ledger=False)
    if status != HEALTHY:
        return Finding("claude", "hook", str(path), status, detail, stale)
    missing = [value for value in referenced
               if value.endswith(".json") and not Path(value).exists()]
    if missing:
        return Finding("claude", "hook", str(path), STALE_PATH,
                       "agent-config 不存在", missing)
    # 路径都在 ≠ 代码在。内置盘 runtime 是 rsync 出来的副本，新增模块若没同步，
    # hook 每轮都静默 ImportError，而审计会一直报 healthy。
    absent_root = _module_missing(commands[0], module="hippomind.claude_hook")
    if absent_root:
        return Finding("claude", "hook", str(path), NOT_SYNCED,
                       "运行时副本缺少 hippomind/claude_hook.py，需重新同步 runtime",
                       [absent_root])
    return Finding("claude", "hook", str(path), HEALTHY)


def _managed_section(text: str) -> Optional[str]:
    """返回托管段正文；没有托管段标记时返回 None。"""
    start = text.find(_RULES_BEGIN)
    if start < 0:
        return None
    end = text.find(_RULES_END, start)
    return text[start:end] if end > start else text[start:]


#: hippomind 写入规则文件时的托管段边界（与 auto_context.RULES_BEGIN/END 一致）。
_RULES_BEGIN = "<!-- HIPPO-MIND-AUTO-CONTEXT:BEGIN -->"
_RULES_END = "<!-- HIPPO-MIND-AUTO-CONTEXT:END -->"


def _managed_section(text: str) -> Optional[str]:
    """返回托管段正文；没有托管段标记时返回 None。"""
    start = text.find(_RULES_BEGIN)
    if start < 0:
        return None
    end = text.find(_RULES_END, start)
    return text[start:end] if end > start else text[start:]


def inspect_rule_file(agent: str, path: Path) -> Finding:
    """规则文件检查：只看 hippomind 自己的托管段，不看用户内容。

    规则文件是与用户共用的（用户在里面写了别的工具的路径），所以判据必须
    限定在托管段内。段内关心的是：cwd / 包路径有没有指向可移动卷或已经不
    存在的临时目录。解释器路径不算——它是只读的，TCC 不因它拒绝数据读取。
    """
    if not path.exists():
        return Finding(agent, "rules", str(path), MISSING, "规则文件不存在")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Finding(agent, "rules", str(path), UNREADABLE, str(exc)[:160])
    if "hippomind" not in text.lower():
        return Finding(agent, "rules", str(path), MISSING, "规则文件里没有 hippomind 段")
    managed = _managed_section(text)
    if managed is None:
        # 有 hippomind 字样但没有托管段边界：无法判定其内部路径，按健康处理，
        # 因为这不是"注入坏了"，只是"这段不是我们写的"。
        return Finding(agent, "rules", str(path), HEALTHY,
                       "未找到托管段标记，跳过路径检查")
    # 托管段内的路径：先按引号切出整体，再处理裸 token。
    # 不能用 [^\s]+ —— "Application Support" 含空格会被切成半截。
    quoted = re.findall(r"'([^']*)'", managed)
    bare = re.sub(r"'[^']*'", " ", managed).split()
    referenced = []
    for token in list(quoted) + bare:
        if "=" in token and not token.startswith("/"):
            token = token.split("=", 1)[1]
        if not token.startswith("/") or _is_interpreter(token):
            continue
        if _is_volatile(token):
            referenced.append(token)
        elif _is_temp(token) and not Path(token).exists():
            referenced.append(token)
    if referenced:
        return Finding(agent, "rules", str(path), VOLATILE_CWD,
                       "托管段引用了临时目录或可移动卷", referenced)
    return Finding(agent, "rules", str(path), HEALTHY)


def audit(agents: Sequence[str], *, ledger_path: Path, home: Optional[Path] = None,
          rule_path_of=None, mcp_path_of=None, codex_hooks: Optional[Path] = None
          ) -> List[Finding]:
    """对给定 agent 列表做完整审计，返回每个注入面的结论。"""
    root = Path(home).expanduser() if home else Path.home()
    findings: List[Finding] = []
    for agent in agents:
        if rule_path_of is not None:
            findings.append(inspect_rule_file(agent, rule_path_of(agent)))
        mcp_path = mcp_path_of(agent) if mcp_path_of is not None else None
        if mcp_path is not None:
            findings.append(inspect_mcp_server(agent, Path(mcp_path),
                                               ledger_path=ledger_path))
        if agent == "codex":
            findings.append(inspect_codex_hook(
                codex_hooks or (root / ".codex" / "hooks.json"),
                ledger_path=ledger_path))
        if agent == "claude":
            findings.append(inspect_claude_hook(
                root / ".claude" / "settings.json", ledger_path=ledger_path))
    return findings


def summarize(findings: Sequence[Finding]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for finding in findings:
        counts[finding.status] = counts.get(finding.status, 0) + 1
    broken = [f.as_dict() for f in findings if f.status != HEALTHY]
    return {"total": len(findings), "counts": counts, "healthy": counts.get(HEALTHY, 0),
            "broken": broken}
