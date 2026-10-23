#!/usr/bin/env python3
"""HippoMind Atlas GUI QA -- deterministic seed ledger + isolated daemon.

Creates a disposable, reproducible ledger shaped for every Atlas view:
8 agents, 90 days, 8 kinds, claims/conflicts/superseded, facts, skills,
relations, feedback, pins, cursors, trust and credentials -- plus XSS payload
records that prove the UI escapes data instead of executing it.

The fixture lives under a temp root (/tmp/hippomind-atlas-qa by default) and
never touches the production ledger.  The daemon is bound to loopback with
token auth, so screenshots exercise the same auth path as production.

Usage:
    python3 scripts/gui_seed.py                  # seed only, print JSON
    python3 scripts/gui_seed.py --daemon         # seed + background daemon
    python3 scripts/gui_seed.py --serve          # seed + foreground daemon
    python3 scripts/gui_seed.py --clean          # stop daemon + remove fixture
    python3 scripts/gui_seed.py --print-token    # include the token in JSON

Exit codes: 0 ok, 1 seed/daemon failure, 2 usage/safety refusal.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger  # noqa: E402

DEFAULT_ROOT = Path(os.environ.get("HIPPOMIND_ATLAS_QA_ROOT", "").strip()
                    or (Path("/tmp") / "hippomind-atlas-qa"))
DEFAULT_MEMORIES = 720
SEED = 20261022
MANIFEST_NAME = "gui-seed.json"
RAW_KINDS = frozenset({"raw_chat", "tool_output", "system_message"})

AGENT_IDS = ["codex", "claude", "gemini", "dsh", "pi", "deepseek", "echobird", "local"]
AGENT_TRUST = {
    "codex": 0.78, "claude": 0.74, "gemini": 0.61, "dsh": 0.86,
    "pi": 0.52, "deepseek": 0.68, "echobird": 0.44, "local": 0.90,
}
KINDS = ["progress", "decision", "next_step", "semantic", "task",
         "raw_chat", "tool_output", "procedural"]
KIND_WEIGHTS = [24, 17, 12, 11, 10, 10, 8, 8]
PROJECTS = ["hippomind", "atlas-gui", "memory-hub", "infra", "research", "docs"]

#: The six claim families below are ingested in chronological order so the
#: ledger itself derives the required conflict / superseded states.
_CLAIM_ROTATION = [
    ("atlas", "phase", "current", "implement"),
    ("atlas", "owner", "maintainer", "atlas-qa"),
    ("daemon", "port", "default", 8765),
    ("daemon", "transport", "bind", "loopback-only"),
    ("ledger", "engine", "primary", "sqlite-wal"),
    ("ledger", "schema", "version", 8),
    ("gui", "theme", "default", "paper"),
    ("gui", "stack", "frontend", "vanilla-esm"),
    ("retrieval", "channel", "primary", "fts5-trigram"),
    ("retrieval", "channel", "fallback", "like-scan"),
    ("agent", "codex", "role", "chat-archive"),
    ("agent", "dsh", "role", "session-log"),
    ("context", "evidence", "policy", "required"),
    ("context", "raw_chat", "policy", "excluded"),
    ("backup", "cadence", "daily", "03:00"),
    ("tests", "suite", "framework", "unittest"),
    ("tests", "gui", "gate", "screenshot"),
    ("archive", "retention", "raw_days", 30),
    ("archive", "raw", "capacity", 5000),
    ("security", "token", "storage", "local-file"),
    ("security", "bind", "host", "loopback"),
    ("graph", "node", "limit", 200),
    ("timeline", "bucket", "default", "day"),
]

_DETAILS = [
    "把 /api/v1 的响应统一为同源 JSON，错误体只保留 error 字段",
    "Archive 视图改成细线表格，去掉旧版玻璃拟态卡片",
    "给 timeline 的堆叠柱状图补上 agent 图例与 hover tooltip",
    "把 claims 冲突投影进 facts 视图的「冲突」标签页",
    "让 Context Pack 默认要求 evidence，并默认排除 raw chat",
    "把 daemon 的 WAL 体积检查并入 health 的 storage 分区",
    "为 graph 力导向图补上滚轮缩放、拖拽平移与邻域高亮",
    "把 archive 的详情抽屉抽成 inspector 共享组件",
    "用 FTS5 trigram 承载中文检索，LIKE 只做兜底救援",
    "把设计 token 收敛进 tokens.css，主题用 data-theme 切换",
    "给命令面板补上 Cmd-K 与 1-0 键盘导航",
    "补齐 /ui 静态服务的 MIME 与安全响应头",
    "把 seed 数据固定为 90 天分布，覆盖 8 个 agent",
    "把刷新心跳收紧到 30 秒，页面隐藏时暂停轮询",
    "给 health 视图加 verdict 大字戳与检查项网格",
    "把 timeline 的日/周粒度切换做成 segmented 控件",
    "给 recall 结果补上 score 横条与命中词高亮",
    "把 compose 的预算仪表接到 max_tokens 与 max_chars",
]

_STATUS = [
    "当前通过 smoke 验证", "已合并到主分支", "还剩空态与错误态待补",
    "等待视图队友接入", "已通过 node --check", "截图基线已经跑通",
    "性能观测正常，响应低于 200ms", "已补 12 条单元测试",
]

_REASONS = [
    "零构建、零第三方依赖，daemon 能直接服务 ES Modules",
    "同源请求可以复用本机 token，不必为 GUI 打开 CORS",
    "论文式排版需要数据密度，卡片堆叠会浪费纵向空间",
    "证据优先原则：没有 evidence 的推断不进 Context Pack",
    "旧 Observatory 的视觉语言与档案馆定位冲突",
    "FTS trigram 对中文子串召回明显优于 unicode61",
]

_FILES = [
    "hippomind/web/js/app.js", "hippomind/web/js/views/archive.js",
    "hippomind/web/js/core/api.js", "hippomind/web/css/tokens.css",
    "hippomind/daemon.py", "hippomind/gui_api.py", "hippomind/ledger/graph.py",
    "hippomind/context_pack.py", "scripts/gui_seed.py", "tests/test_gui_api.py",
    "docs/GUI_ATLAS.md", "hippomind/web/js/components.js",
]

_COMMANDS = [
    "python3 -m unittest discover -s tests -p 'test_gui*.py'",
    "node --check hippomind/web/js/app.js",
    "scripts/lint.sh",
    "python3 -m hippomind.daemon --port 8765",
    "hippomind recall 'FTS trigram' --limit 5",
    "hippomind checkup",
]

_CRITERIA = [
    "10 个视图在 1440x900 下无横向溢出",
    "paper 与 night 两套 token 都能通过对比度检查",
    "node --check 全部通过且无 console error",
    "seed 数据下每个视图都有内容态",
    "401 时必须出现令牌引导层而不是空白页",
]

_STATEMENTS = [
    "账本事件是只追加的，记忆投影可以随时从事件重建。",
    "Claims 的 Slot-Value 冲突由 source trust 与 confidence 共同裁决。",
    "Context Pack 默认只收结构化记忆，原始聊天只作审计证据。",
    "FTS 索引集合的精确定义是 quality_state='accepted' 且 lifecycle='active'。",
    "UI 的所有动态文本必须走 el() 的文本子节点，禁止拼 innerHTML。",
    "daemon 只绑定 loopback，管理员令牌保存在 0600 的本地文件里。",
    "时间轴按 agent 堆叠，能看出哪个采集器在某天掉了链子。",
    "图谱的节点是 claim 主语，边来自 memory_relations 的因果链。",
]

_TASKS = [
    "为 archive 视图接通分页与 facets",
    "让 recall 结果支持键盘逐条浏览",
    "把 compose 的三种输出做成只读 textarea",
    "给 graph 画布加缩放与重置按钮",
    "为 facts 的冲突页补复制裁决命令",
    "把 health 的完整性检查做成按需触发",
    "给 reference 补快捷键表与版本信息",
    "实现 timeline 的 bucket 下钻表格",
]

_USER_MSGS = [
    "帮我看看 daemon 最近有没有 401 的请求",
    "把 archive 的详情抽屉做成可复用的组件",
    "这个页面的表格为什么横向溢出了",
    "帮我确认 claims 冲突是不是都还在 active",
    "Context Pack 为什么默认不含 raw chat",
    "截图基线跑完了吗，有几个视图是空的",
    "把 night 主题的对比度再核对一遍",
    "FTS 漂移检查现在是什么状态",
]

_ASSIST_MSGS = [
    "401 来自未携带 X-HippoMind-Token 的本地 curl，不是 GUI 回归。",
    "已抽出 inspector 组件，archive/recall/timeline 共用同一个抽屉。",
    "横向溢出是 archive 表格列宽没有收敛，已用 table-layout: fixed 修复。",
    "冲突 claim 的状态是 conflict，active 的只有胜出方。",
    "因为 evidence 策略默认要求证据，raw chat 只进入审计通道。",
    "10 个视图里 archive 仍是空态，因为 /api/v1/memories 还没合并。",
    "night 主题的 --ink-3 对比度偏低，建议提到 4.6:1 以上。",
    "FTS 漂移为 0，sts 行数与应索引集合一致。",
]

_TOOL_OUTPUTS = [
    "Ran 596 tests in 41.2s\n\nOK",
    "node --check OK (42 files)",
    "lint: OK -- 0 new findings",
    "PASS tests/test_gui_api.py 18 tests",
    "health: fts=ok claims=ok wal=0 bytes",
    "recall: 5 hits in 23.4 ms (fts+like)",
]

_SKILLS: List[Dict[str, Any]] = [
    {"name": "GUI 截图基线", "trigger": ["页面改动后", "需要视觉证据时"],
     "pre": ["seed daemon 已启动", "token 文件可读"],
     "pipeline": ["运行 scripts/gui_seed.py --daemon",
                  "运行 scripts/gui_shot.mjs --theme both",
                  "检查 REPORT.md 的失败行"],
     "verify": ["10 个视图 x 2 主题均生成 PNG", "无 console error 与 401"]},
    {"name": "前端静态检查", "trigger": ["提交前端改动前"],
     "pre": ["node 22 已安装", "工作区无未保存改动"],
     "pipeline": ["对 hippomind/web/js 全量跑 node --check",
                  "检查 index.html 无内联脚本与外链",
                  "检查视图模块导出 meta/render"],
     "verify": ["tests/test_gui_frontend.py 全绿"]},
    {"name": "冲突裁决", "trigger": ["facts 视图出现冲突 claim"],
     "pre": ["确认冲突槽位", "确认胜者 memory_id"],
     "pipeline": ["复制界面上的 hippomind resolve 命令", "在终端执行并观察 state 变化", "回到 facts 刷新"],
     "verify": ["冲突计数下降", "胜者 claim 状态为 active"]},
    {"name": "daemon 隔离启动", "trigger": ["需要 QA 环境时"],
     "pre": ["确认 8765 未被占用", "确认临时目录可写"],
     "pipeline": ["python3 scripts/gui_seed.py --daemon", "读取 JSON 的 daemon_url", "用 token 文件请求 /stats"],
     "verify": ["/health 返回 status=ok", "/stats 返回 200 且 ledger 计数匹配"]},
    {"name": "Context Pack 组装", "trigger": ["需要给 agent 注入上下文"],
     "pre": ["injection 开关已开启", "任务描述足够具体"],
     "pipeline": ["在 compose 填写任务描述", "设置 max_items 与预算", "复制 XML 输出"],
     "verify": ["items 均带 evidence", "token 使用率不超过预算"]},
    {"name": "WAL 体积检查", "trigger": ["health 报 storage warn"],
     "pre": ["备份目录可用"],
     "pipeline": ["hippomind checkup", "hippomind maintain --checkpoint", "重新打开 health"],
     "verify": ["wal_bytes 回落", "fts 无漂移"]},
    {"name": "主题 token 核对", "trigger": ["新增颜色用法"],
     "pre": ["tokens.css 已更新"],
     "pipeline": ["在 paper 下截图", "切换 night 再截图", "对比规则线/弱化文字对比度"],
     "verify": ["两种主题均可读", "无写死颜色"]},
    {"name": "令牌排障", "trigger": ["页面出现 401 引导层"],
     "pre": ["daemon 正在运行"],
     "pipeline": ["读取 runtime 目录的 token 文件", "在连接层粘贴令牌", "刷新当前视图"],
     "verify": ["状态栏恢复在线", "视图重新拉取数据"]},
    {"name": "视图空态验收", "trigger": ["新视图合入"],
     "pre": ["seed daemon 在线"],
     "pipeline": ["清空 query 触发空态", "断网触发错误态", "清 token 触发 401 态"],
     "verify": ["三种状态都有解释与下一步动作"]},
    {"name": "键盘可达性", "trigger": ["新增交互控件"],
     "pre": ["页面已加载"],
     "pipeline": ["Tab 遍历操作区", "按 1-0 切换视图", "按 Esc 关闭抽屉与面板"],
     "verify": ["焦点可见", "快捷键不与输入框冲突"]},
]

_XSS_PAYLOADS = [
    '<img src=x onerror="window.__XSS__=1">',
    '<script>window.__XSS__=2</script>',
    '<svg onload=window.__XSS__=3>',
    '<iframe srcdoc="&lt;b&gt;x&lt;/b&gt;">',
    '<img src=x onerror=window.__XSS__=1>',
    '<body onload=window.__XSS__=4>',
]

_FEEDBACK_NOTES = [
    "这条判断在排查时直接命中问题", "信息略旧，需要补充新结论",
    "证据链完整，保留", "结论方向对但范围过宽", "帮我省了一次重复排障",
]


# ---------------------------------------------------------------------------
# Paths, ports and tokens
# ---------------------------------------------------------------------------
def _paths(root: Path) -> Dict[str, Path]:
    runtime = root / "runtime"
    return {
        "root": root,
        "runtime_dir": runtime,
        "ledger_path": root / "hippomind-atlas.sqlite",
        "token_path": runtime / "hippomindd.token",
        "state_path": runtime / "hippomindd.json",
        "log_path": root / "daemon.log",
        "manifest_path": root / MANIFEST_NAME,
        "hub_home": root / "hub-home",
        "injection_path": runtime / "context-injection.json",
        "metrics_path": runtime / "metrics.jsonl",
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_token(path: Path) -> str:
    """Write a 0600 bearer token the daemon will reuse instead of minting one."""
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)
    return token


def _read_token(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_json(path: Path, data: Mapping[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
                         encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _read_manifest(root: Path) -> Dict[str, Any]:
    try:
        value = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _http_json(url: str, token: str = "", timeout: float = 1.5) -> Optional[Dict[str, Any]]:
    headers = {"X-HippoMind-Token": token} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _stop_daemon(manifest: Mapping[str, Any]) -> Optional[int]:
    """Stop only the daemon recorded in our manifest (never a foreign process)."""
    try:
        pid = int(manifest.get("daemon_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0 or not _pid_alive(pid):
        return None
    state_path = Path(str(manifest.get("state_path") or ""))
    recorded = 0
    try:
        recorded = int(json.loads(state_path.read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        recorded = 0
    if recorded != pid:
        sys.stderr.write("[gui_seed] manifest pid %s does not match daemon state; not killing\n" % pid)
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return None
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if not _pid_alive(pid):
            return pid
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return None
    return pid


# ---------------------------------------------------------------------------
# Seed content
# ---------------------------------------------------------------------------
def _claim(subject: str, predicate: str, slot: str, value: Any, confidence: float) -> Dict[str, Any]:
    return {"subject": subject, "predicate": predicate, "slot_key": slot,
            "value": value, "confidence": confidence}


def _special_records(rng: random.Random, anchor: datetime) -> List[Dict[str, Any]]:
    """Records that deliberately exercise conflict/superseded/XSS/edge states."""
    records: List[Dict[str, Any]] = []

    def add(age: int, agent: str, kind: str, content: str, *, claims: Optional[List[Dict[str, Any]]] = None,
            tags: Optional[List[str]] = None, confidence: float = 0.72, role: str = "",
            project: str = "atlas-gui", procedural: Optional[Dict[str, Any]] = None) -> None:
        stamp = anchor - timedelta(days=age)
        stamp = stamp.replace(hour=rng.randint(8, 22), minute=rng.randint(0, 59),
                              second=rng.randint(0, 59), microsecond=0)
        record: Dict[str, Any] = {
            "content": content, "kind": kind, "source_agent": agent,
            "session_id": "%s/special-%02d" % (agent, len(records) + 1),
            "timestamp": stamp.isoformat(), "scope": "shared",
            "project": project, "task_id": "task-000",
            "evidence": ["special:atlas-gui#%02d" % (len(records) + 1)],
            "tags": list(tags or ["atlas", "special"]),
            "confidence": confidence,
        }
        if role:
            record["role"] = role
        if claims:
            record["_claims"] = claims
        if procedural:
            record["procedural"] = procedural
        records.append(record)

    # -- conflict family: same slot, near-equal scores, disagreeing values
    add(89, "codex", "decision",
        "决策：Atlas 默认主题定为纸本（paper），朱砂作为唯一实时信号色。\n"
        "理由：纸本更贴近档案馆的气质，长时间阅读也不刺眼。",
        claims=[_claim("atlas", "theme", "default", "paper", 0.80)],
        tags=["design", "theme", "paper"], confidence=0.80)
    add(84, "claude", "decision",
        "评审后把默认主题切到夜档（night），理由是夜读场景更多。\n"
        "但纸本仍保留为可选主题，token 完全一致。",
        claims=[_claim("atlas", "theme", "default", "night", 0.82)],
        tags=["design", "theme", "night"], confidence=0.82)

    # -- superseded family: newer higher-confidence value wins
    add(87, "local", "semantic",
        "事实：存储引擎选用 sqlite + WAL，单文件便于本地备份与故障恢复。",
        claims=[_claim("storage", "engine", "primary", "sqlite", 0.50)],
        tags=["ledger", "storage"], confidence=0.50)
    add(66, "local", "semantic",
        "事实：压测后把主存储切到 postgres，sqlite 只保留为只读镜像。\n"
        "迁移脚本与回滚路径已记录在 infra 仓库。",
        claims=[_claim("storage", "engine", "primary", "postgres", 0.95)],
        tags=["ledger", "storage", "migration"], confidence=0.95)

    # -- second supersede family: deployment target upgrade
    add(78, "local", "semantic",
        "事实：部署主机最初选 mac-mini（16G 内存），够跑 daemon 与 watcher。",
        claims=[_claim("deploy", "target", "host", "mac-mini", 0.50)],
        tags=["infra", "deploy"], confidence=0.50)
    add(31, "local", "semantic",
        "事实：部署主机升级为 mac-studio（64G 内存），旧 mac-mini 退役。",
        claims=[_claim("deploy", "target", "host", "mac-studio", 0.95)],
        tags=["infra", "deploy", "upgrade"], confidence=0.95)

    # -- second conflict family
    add(83, "codex", "semantic",
        "当前 schema 版本是 7，还没有 pinned 列。",
        claims=[_claim("schema", "version", "current", 7, 0.90)],
        tags=["ledger", "schema"], confidence=0.90)
    add(21, "claude", "semantic",
        "schema 已经升到 8：新增 pinned 列与两条检索索引，迁移是幂等的。",
        claims=[_claim("schema", "version", "current", 8, 0.92)],
        tags=["ledger", "schema", "migration"], confidence=0.92)

    # -- healthy active claims
    add(55, "codex", "semantic",
        "事实：Atlas 的刷新心跳是 30 秒，页面隐藏时暂停轮询，避免后台耗电。",
        claims=[_claim("atlas", "refresh", "interval", 30, 0.75)],
        tags=["gui", "perf"], confidence=0.75)
    add(46, "claude", "semantic",
        "事实：记忆浏览器默认排除 raw chat，并在工具栏解释排除原因。",
        claims=[_claim("context", "raw_chat", "policy", "excluded", 0.85)],
        tags=["gui", "privacy"], confidence=0.85)
    add(1, "codex", "decision",
        "决策：Atlas 进入联调阶段，pulse / archive / facts 三个视图先接真实数据。\n"
        "维护者仍是 atlas-qa，验收口径见 docs/GUI_ATLAS.md 第 7 节。",
        claims=[_claim("atlas", "status", "phase", "shipping", 0.90),
                _claim("atlas", "owner", "maintainer", "atlas-qa", 0.85)],
        tags=["milestone", "release"], confidence=0.90)

    # -- XSS payload records (>=5) proving text-node escaping
    add(3, "codex", "semantic",
        "验收转义：%s 必须只显示为文本，不能执行。"
        % _XSS_PAYLOADS[0], tags=["xss", "escape"])
    add(8, "claude", "decision",
        "结果面板显示 %s 时也只能看到源码，CSP 会拦掉内联脚本。"
        % _XSS_PAYLOADS[1], tags=["xss", "csp"])
    add(15, "gemini", "semantic",
        "标签值 %s 需要按文本渲染，而不是写进 innerHTML。"
        % _XSS_PAYLOADS[2], tags=["xss", "dom"])
    add(27, "dsh", "raw_chat",
        "用户：把这个贴进详情抽屉试试 %s\n助手：它只会显示成文本。"
        % _XSS_PAYLOADS[3], tags=["xss", "raw"], role="user")
    add(33, "local", "tool_output",
        "工具输出：recall 查询 %s 返回 0 hits（payload 只作为审计证据保留）。"
        % _XSS_PAYLOADS[4],
        tags=["xss", "cli"], role="tool")
    add(40, "pi", "semantic",
        "冲突值 %s 出现在 facts 冲突表里，复制出去也不能触发脚本。"
        % _XSS_PAYLOADS[5],
        claims=[_claim("atlas", "xss", "payload", _XSS_PAYLOADS[0], 0.70)],
        tags=["xss", "claims"], confidence=0.70)

    # -- long content + tricky quoting + procedural with payload trigger
    long_text = "长篇验收记录：" + "；".join("第 %d 项确认转义与截断行为正确" % index
                                          for index in range(1, 90))
    add(12, "codex", "progress", long_text, tags=["long", "truncate"])
    add(5, "claude", "semantic",
        "他说：\"<b>不要相信</b>\"，-- 以及反斜杠 \\n 都只是普通文本。",
        tags=["quote", "escape"])
    add(18, "deepseek", "procedural",
        "流程「转义回归」：把 XSS payload 放进 trigger 字段，确认技能卡片仍按文本渲染。",
        tags=["procedural", "xss"],
        procedural={
            "trigger_pattern": ["遇到 %s 时" % _XSS_PAYLOADS[0], "详情抽屉渲染前"],
            "preconditions": ["CSP 已启用", "seed 数据已注入"],
            "execution_pipeline": ["打开 facts 技能页", "展开卡片", "确认无脚本执行"],
            "verification_assertion": ["window.__XSS__ 未定义"],
            "calibration_score": {"attempts": 12, "successes": 12, "rate": 1.0},
        })
    return records


def _content_for(rng: random.Random, kind: str, project: str, task_id: str,
                 agent: str, seq: int) -> str:
    detail = rng.choice(_DETAILS)
    if kind == "progress":
        return "推进「%s」：%s。已更新 %s，%d 条相关测试通过。" % (
            detail, rng.choice(_STATUS), rng.choice(_FILES), rng.randint(3, 120))
    if kind == "decision":
        return "决策：%s。\n理由：%s。" % (detail, rng.choice(_REASONS))
    if kind == "next_step":
        return "下一步：%s。\n验收标准：%s。" % (rng.choice(_TASKS), rng.choice(_CRITERIA))
    if kind == "semantic":
        return "%s\n补充事实：%s" % (rng.choice(_STATEMENTS), detail)
    if kind == "task":
        return "任务 %s：%s。\n项目：%s；负责人：%s；状态：%s。" % (
            task_id or "task-%03d" % (seq % 96), rng.choice(_TASKS), project or "hippomind",
            agent, rng.choice(_STATUS))
    if kind == "raw_chat":
        return "用户：%s\n助手：%s" % (rng.choice(_USER_MSGS), rng.choice(_ASSIST_MSGS))
    if kind == "tool_output":
        return "$ %s\n%s" % (rng.choice(_COMMANDS), rng.choice(_TOOL_OUTPUTS))
    if kind == "procedural":
        skill = _SKILLS[seq % len(_SKILLS)]
        return "流程「%s」：%s。\n前置：%s\n验证：%s" % (
            skill["name"], detail, "；".join(skill["pre"]), "；".join(skill["verify"]))
    return detail


def _procedural_for(seq: int) -> Dict[str, Any]:
    skill = _SKILLS[seq % len(_SKILLS)]
    attempts = 4 + (seq % 9)
    successes = max(1, attempts - (seq % 4))
    return {
        "trigger_pattern": skill["trigger"],
        "preconditions": skill["pre"],
        "execution_pipeline": skill["pipeline"],
        "verification_assertion": skill["verify"],
        "calibration_score": {"attempts": attempts, "successes": successes,
                              "rate": round(successes / attempts, 3)},
    }


def _random_record(rng: random.Random, anchor: datetime, age: int, seq: int) -> Dict[str, Any]:
    task_index = rng.randrange(96)
    project = PROJECTS[task_index % len(PROJECTS)]
    task_id = "task-%03d" % task_index
    agent = rng.choice(AGENT_IDS)
    kind = rng.choices(KINDS, weights=KIND_WEIGHTS, k=1)[0]
    scope = "shared" if rng.random() < 0.82 else "agent:" + agent
    stamp = anchor - timedelta(days=age)
    stamp = stamp.replace(hour=rng.randint(0, 23), minute=rng.randint(0, 59),
                          second=rng.randint(0, 59), microsecond=0)
    session_id = "%s/session-%04d" % (agent, seq)
    content = _content_for(rng, kind, project, task_id, agent, seq)
    evidence = ["%s:session/%s" % (agent, session_id)]
    if rng.random() < 0.7:
        evidence.append("archive/%s-%04d.jsonl" % (kind, seq))
    if rng.random() < 0.45:
        evidence.append("commit:%07x" % rng.getrandbits(28))
    tags = list(dict.fromkeys([rng.choice(["gui", "atlas", "ledger", "daemon", "retrieval",
                                           "claims", "timeline", "screenshot", "design", "api",
                                           "token", "fts", "pagination", "theme"]),
                               kind, project]))
    record: Dict[str, Any] = {
        "content": content, "kind": kind, "source_agent": agent,
        "session_id": session_id, "timestamp": stamp.isoformat(), "scope": scope,
        "project": project, "task_id": task_id, "evidence": evidence, "tags": tags,
        "weight": round(rng.uniform(0.22, 0.96), 3),
        "importance": rng.randint(2, 9),
        "confidence": round(rng.uniform(0.35, 0.95), 3),
        "utility": round(rng.uniform(-0.2, 0.5), 3),
        "metadata": {"surface": rng.choice(["cli", "chat", "tool", "session"]),
                     "turn": seq % 60},
    }
    if rng.random() < 0.08:
        record["project"] = ""
        record["task_id"] = ""
    mapping = {
        "progress": (["progress", "next_steps"], rng.choice(_TASKS)),
        "decision": (["key_decisions", "related_files"], rng.choice(_DETAILS)),
        "next_step": (["next_steps"], rng.choice(_TASKS)),
        "semantic": (["key_decisions"], rng.choice(_STATEMENTS)),
        "task": (["goal", "related_files"], rng.choice(_TASKS)),
    }
    fields, value = mapping.get(kind, ([], ""))
    context: Dict[str, List[str]] = {}
    for field in fields:
        context[field] = [value if field != "related_files" else rng.choice(_FILES)]
    if context:
        record["context"] = context
    if kind in RAW_KINDS:
        record["role"] = "tool" if kind == "tool_output" else rng.choice(["user", "assistant"])
    if kind == "procedural":
        record["procedural"] = _procedural_for(seq)
    return record


def _build_records(rng: random.Random, anchor: datetime, total: int) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = _special_records(rng, anchor)
    # Guarantee one memory for each of the last 30 days so the pulse sparkline
    # and the 30-day growth chart are never empty.
    for age in range(30):
        records.append(_random_record(rng, anchor, age, 1000 + age))
    remaining = max(0, total - len(records))
    weights = [1.6 - (age / 89.0) for age in range(90)]
    for offset in range(remaining):
        age = rng.choices(range(90), weights=weights, k=1)[0]
        records.append(_random_record(rng, anchor, age, 2000 + offset))
    records.sort(key=lambda item: item["timestamp"])

    structured_seen = 0
    for record in records:
        if "_claims" in record or record["kind"] in RAW_KINDS:
            continue
        structured_seen += 1
        if structured_seen % 6 == 0:
            slot = _CLAIM_ROTATION[(structured_seen // 6) % len(_CLAIM_ROTATION)]
            record["_claims"] = [_claim(slot[0], slot[1], slot[2], slot[3],
                                        round(rng.uniform(0.55, 0.95), 2))]
    return records


def _age_days(timestamp: str, anchor: datetime) -> int:
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((anchor - parsed.astimezone(timezone.utc)).total_seconds() // 86400))


# ---------------------------------------------------------------------------
# Ledger seeding
# ---------------------------------------------------------------------------
def _seed_ledger(ledger_path: Path, runtime_dir: Path, anchor: datetime,
                 total: int) -> Dict[str, Any]:
    rng = random.Random(SEED)
    ledger = SQLiteLedger(ledger_path)
    for agent, baseline in AGENT_TRUST.items():
        ledger.set_agent_trust(agent, baseline)

    records = _build_records(rng, anchor, total)
    rows: List[Dict[str, Any]] = []
    claim_history: List[Tuple[str, str]] = []
    for index, record in enumerate(records):
        claims = record.pop("_claims", None)
        subject = ""
        if claims:
            record["claims"] = claims
            subjects = [str(claim.get("subject", "")) for claim in claims]
            subject = subjects[0] if subjects else ""
            if claim_history:
                recent = claim_history[-12:]
                candidates = [entry for entry in recent if entry[1] != subject]
                pool = candidates or recent
                if rng.random() < 0.55:
                    parent_id, _ = rng.choice(pool)
                    record["causal_parents"] = [{
                        "memory_id": parent_id,
                        "relation_type": rng.choice(("caused_by", "related_to",
                                                     "elaborates", "supersedes")),
                        "confidence": round(rng.uniform(0.35, 0.95), 2),
                    }]
                elif index % 9 == 0:
                    atlas_parents = [entry for entry in claim_history if entry[1] == "atlas"]
                    if atlas_parents:
                        parent_id, _ = rng.choice(atlas_parents)
                        record["causal_parents"] = [{
                            "memory_id": parent_id, "relation_type": "related_to",
                            "confidence": 0.7}]
        result = ledger.ingest(record)
        kind = str(record.get("kind", ""))
        if not result.inserted:
            sys.stderr.write("[gui_seed] deduplicated a record while seeding (unexpected)\n")
        rows.append({"memory_id": result.memory_id, "kind": kind,
                     "raw_chat": kind in RAW_KINDS,
                     "source_agent": str(record.get("source_agent", "")),
                     "age_days": _age_days(str(record.get("timestamp", "")), anchor)})
        if claims:
            claim_history.append((result.memory_id, subject))

    claimed_ids = {memory_id for memory_id, _ in claim_history}
    structured = [row for row in rows if not row["raw_chat"]]

    # -- feedback: positive and negative signals on recent structured memories
    feedback_pool = [row["memory_id"] for row in structured if row["age_days"] <= 45]
    for position, memory_id in enumerate(rng.sample(feedback_pool[:400],
                                                    min(45, len(feedback_pool)))):
        signal = 1.0 if position % 3 != 2 else -1.0
        ledger.feedback(memory_id, signal, rng.choice(_FEEDBACK_NOTES),
                        rng.choice(AGENT_IDS))

    # -- touch counts / last_access
    ledger.touch([row["memory_id"] for row in rng.sample(structured, min(30, len(structured)))])

    # -- operator pins on recent working memory
    pinned_ids = [row["memory_id"] for row in structured
                  if row["age_days"] <= 10 and row["memory_id"] not in claimed_ids]
    for memory_id in pinned_ids[:7]:
        ledger.pin_memory(memory_id, True)

    # -- extra supersede lineage (public API; keeps claims untouched)
    supersede_pool = [row for row in structured
                      if row["age_days"] >= 50
                      and row["memory_id"] not in claimed_ids
                      and row["memory_id"] not in pinned_ids]
    supersede_pool.sort(key=lambda row: row["age_days"], reverse=True)
    for offset in range(0, min(6, len(supersede_pool) - 1), 2):
        try:
            ledger.supersede(supersede_pool[offset]["memory_id"],
                             supersede_pool[offset + 1]["memory_id"], "local-admin")
        except Exception as error:  # fixture nicety, not a hard requirement
            sys.stderr.write("[gui_seed] supersede skipped: %s\n" % error)

    # -- lifecycle mix via the ledger's transaction helper (deterministic rows)
    cold_candidates = [row["memory_id"] for row in structured
                       if row["age_days"] >= 62
                       and row["memory_id"] not in claimed_ids
                       and row["memory_id"] not in pinned_ids]
    expired_candidates = [row["memory_id"] for row in structured
                          if row["age_days"] >= 55
                          and row["memory_id"] not in claimed_ids
                          and row["memory_id"] not in pinned_ids
                          and row["memory_id"] not in cold_candidates[:70]]
    cold_ids = cold_candidates[:70]
    expired_ids = expired_candidates[:4]
    past = (anchor - timedelta(days=3)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    def _transition(conn: Any) -> None:
        for memory_id in cold_ids:
            conn.execute(
                "UPDATE memories SET lifecycle='cold', archived_at=?, superseded_by='' "
                "WHERE memory_id=? AND lifecycle='active'", (now_iso, memory_id))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        for memory_id in expired_ids:
            conn.execute(
                "UPDATE memories SET lifecycle='expired', valid_until=?, archived_at=? "
                "WHERE memory_id=? AND lifecycle='active'", (past, now_iso, memory_id))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))

    ledger._transaction(_transition)
    ledger.repair_fts()

    # -- collection cursors: successes, stale errors, one error-only source
    cursor_specs = [
        ("codex/rollout-2026-10-21T18-04-11-atlas", "line:12834", ""),
        ("claude/projects/atlas-gui/transcript", "byte:884211", ""),
        ("dsh/sessions/2026-10-21-atlas-qa", "offset:4096", ""),
        ("gemini/logs/atlas-gui", "line:220", ""),
        ("echobird/inbox/atlas", "cursor:4711", ""),
        ("pi/home/.pi/logs", "line:91", ""),
        ("codex/rollout-2026-10-19T09-11-02-legacy", "line:5533", ""),
        ("claude/projects/memory-hub/transcript", "byte:120033", ""),
    ]
    for source_id, cursor, _ in cursor_specs:
        ledger.set_cursor(source_id, cursor)
    for source_id, cursor, error in [
        ("codex/rollout-2026-10-20T22-18-40-broken", "line:77",
         "JSON decode failed at offset 8832 (truncated rollout header)"),
        ("echobird/inbox/atlas-drop", "cursor:1200",
         "permission denied: ~/.echobird/inbox/atlas-drop"),
    ]:
        ledger.set_cursor(source_id, cursor, error=error)
    ledger.set_cursor("gemini/logs/atlas-gui", "line:220", error="",
                      metadata={"path": "~/Library/Logs/gemini/atlas-gui.log"})

    # -- scoped credentials so the agents view has real rows; plaintext tokens
    # are deliberately never printed and the ledger stores only hashes.
    credential_ids: List[str] = []
    for agent in ("codex", "claude", "local"):
        credential, _raw_token = ledger.issue_agent_credential(
            agent, scopes=["shared", "agent:" + agent],
            permissions=["read", "ingest", "feedback", "stats"])
        credential_ids.append(credential.token_id)
    ledger.revoke_agent_credential(credential_ids[-1])

    # -- context injection enabled globally, two agent overrides off
    _write_json(runtime_dir / "context-injection.json", {
        "version": 1,
        "default_enabled": True,
        "agents": {"echobird": False, "pi": False},
    })

    # -- summary + invariants the screenshots rely on
    stats = ledger.stats()
    health = ledger.health_overview()
    fts = ledger.fts_sync_report()
    claims_report = ledger.claims_consistency_report()
    claim_counts = {"active": 0, "conflict": 0, "superseded": 0, "other": 0}
    for claim in ledger.list_claims(scope="shared", limit=2000):
        state = str(claim.get("state", "active"))
        claim_counts[state if state in claim_counts else "other"] += 1
    facts = ledger.list_facts(scope="shared", limit=500)
    skills = ledger.list_skills(scope="shared", limit=200)
    with ledger._reading() as conn:
        feedback_count = int(conn.execute("SELECT COUNT(*) FROM memory_feedback").fetchone()[0])
        lifecycle_rows = conn.execute(
            "SELECT lifecycle, COUNT(*) AS total FROM memories GROUP BY lifecycle").fetchall()
        xss_count = int(conn.execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE '%onerror%' "
            "OR content LIKE '%onload%' OR content LIKE '%<script>%' "
            "OR content LIKE '%<iframe%'").fetchone()[0])
    lifecycle = {str(row["lifecycle"]): int(row["total"]) for row in lifecycle_rows}
    ledger.close()

    summary: Dict[str, Any] = {
        "memories": int(stats["memories"]),
        "events": int(stats["events"]),
        "raw_chat": int(stats["raw_chat"]),
        "agents": stats["agents"],
        "kinds": stats["kinds"],
        "lifecycle": lifecycle,
        "claims": {"total": int(stats["claims"]), **claim_counts},
        "facts": len(facts),
        "skills": len(skills),
        "relations": int(stats["causal_relations"]),
        "feedback": feedback_count,
        "pinned": int(stats["pinned"]),
        "cursors": int(stats["cursors"]),
        "credentials": int(stats["agent_credentials"]),
        "xss_records": xss_count,
        "health": {"fts": fts["status"], "claims": claims_report["status"],
                   "quarantined": int(health["quarantined"]),
                   "fts_rows": int(fts["fts_rows"]),
                   "fts_expected": int(fts["projections"])},
    }
    _validate_summary(summary, total)
    return summary


def _validate_summary(summary: Mapping[str, Any], requested: int) -> None:
    problems: List[str] = []
    if int(summary["memories"]) < max(600, requested - 5):
        problems.append("memories=%s < 600" % summary["memories"])
    missing_agents = sorted(set(AGENT_IDS) - set(summary["agents"]))
    if missing_agents:
        problems.append("missing agents: %s" % ", ".join(missing_agents))
    missing_kinds = sorted(set(KINDS) - set(summary["kinds"]))
    if missing_kinds:
        problems.append("missing kinds: %s" % ", ".join(missing_kinds))
    claims = summary["claims"]
    if int(claims.get("active", 0)) < 50:
        problems.append("active claims < 50")
    if int(claims.get("conflict", 0)) < 2:
        problems.append("conflict claims < 2")
    if int(claims.get("superseded", 0)) < 2:
        problems.append("superseded claims < 2")
    if int(summary["facts"]) < 40:
        problems.append("facts < 40")
    if int(summary["skills"]) < 8:
        problems.append("skills < 8")
    if int(summary["relations"]) < 20:
        problems.append("relations < 20")
    if int(summary["feedback"]) < 30:
        problems.append("feedback < 30")
    if int(summary["pinned"]) < 5:
        problems.append("pinned < 5")
    if int(summary["cursors"]) < 6:
        problems.append("cursors < 6")
    if int(summary["credentials"]) < 2:
        problems.append("active credentials < 2")
    if int(summary["xss_records"]) < 5:
        problems.append("xss records < 5")
    lifecycle = summary["lifecycle"]
    if int(lifecycle.get("cold", 0)) < 20:
        problems.append("cold lifecycle < 20")
    if int(lifecycle.get("superseded", 0)) < 3:
        problems.append("superseded lifecycle < 3")
    if int(lifecycle.get("expired", 0)) < 2:
        problems.append("expired lifecycle < 2")
    if problems:
        raise RuntimeError("seed invariants failed: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# Workspace + daemon lifecycle
# ---------------------------------------------------------------------------
def _prepare_workspace(root: Path) -> Dict[str, Path]:
    manifest = _read_manifest(root)
    if manifest:
        stopped = _stop_daemon(manifest)
        if stopped:
            sys.stderr.write("[gui_seed] stopped previous fixture daemon pid=%s\n" % stopped)
    if root.exists():
        shutil.rmtree(root)
    paths = _paths(root)
    paths["runtime_dir"].mkdir(parents=True, exist_ok=True)
    paths["hub_home"].mkdir(parents=True, exist_ok=True)
    return paths


def _daemon_command(paths: Mapping[str, Path], port: int) -> List[str]:
    return [sys.executable, "-m", "hippomind.daemon",
            "--ledger", str(paths["ledger_path"]),
            "--runtime-dir", str(paths["runtime_dir"]),
            "--port", str(port),
            "--no-watch"]


def _daemon_env(paths: Mapping[str, Path]) -> Dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing if existing else "")
    env["HIPPOMIND_HOME"] = str(paths["hub_home"])
    return env


def _start_daemon(paths: Mapping[str, Path], port: int, token: str) -> Dict[str, Any]:
    log_path = paths["log_path"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            _daemon_command(paths, port), cwd=str(ROOT), env=_daemon_env(paths),
            stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        handle.close()
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 25.0
    health: Optional[Dict[str, Any]] = None
    while time.time() < deadline:
        if process.poll() is not None:
            break
        health = _http_json(base + "/health", timeout=0.8)
        if health:
            break
        time.sleep(0.15)
    if not health:
        tail = ""
        try:
            tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-25:])
        except OSError:
            pass
        try:
            process.terminate()
        except OSError:
            pass
        raise RuntimeError("fixture daemon did not become healthy\n" + tail)
    stats = _http_json(base + "/stats", token=token, timeout=3.0)
    if not stats:
        try:
            process.terminate()
        except OSError:
            pass
        raise RuntimeError("fixture daemon rejected the seeded token on /stats")
    return {"pid": int(process.pid), "health": health, "stats": stats,
            "base_url": base, "log_path": str(log_path)}


def _serve_foreground(paths: Mapping[str, Path], port: int) -> int:
    command = _daemon_command(paths, port)
    sys.stdout.flush()
    sys.stderr.flush()
    os.environ.update(_daemon_env(paths))
    try:
        os.execv(sys.executable, command)
    except OSError as error:
        sys.stderr.write("[gui_seed] failed to exec daemon: %s\n" % error)
        return 1
    return 0  # pragma: no cover - exec never returns


def _clean(root: Path, force: bool) -> int:
    resolved = root.resolve()
    if resolved == Path(resolved.anchor) or resolved == ROOT or ROOT in resolved.parents:
        sys.stderr.write("[gui_seed] refusing to clean unsafe path: %s\n" % resolved)
        return 2
    manifest = _read_manifest(root)
    if not resolved.exists():
        print(json.dumps({"ok": True, "cleaned": False, "reason": "absent",
                          "root": str(resolved)}, ensure_ascii=False))
        return 0
    if not manifest and not force:
        sys.stderr.write("[gui_seed] no %s in %s; refusing to delete (use --force)\n"
                         % (MANIFEST_NAME, resolved))
        return 2
    stopped = _stop_daemon(manifest) if manifest else None
    shutil.rmtree(resolved)
    print(json.dumps({"ok": True, "cleaned": True, "root": str(resolved),
                      "stopped_pid": stopped}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_anchor(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SystemExit("--anchor must be ISO-8601: %s" % error)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _payload(paths: Mapping[str, Path], port: int, summary: Mapping[str, Any],
             *, token: str = "", pid: Optional[int] = None,
             extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    base = "http://127.0.0.1:%d" % port
    payload: Dict[str, Any] = {
        "ok": True,
        "runtime_dir": str(paths["runtime_dir"]),
        "ledger_path": str(paths["ledger_path"]),
        "token_path": str(paths["token_path"]),
        "state_path": str(paths["state_path"]),
        "manifest_path": str(paths["manifest_path"]),
        "log_path": str(paths["log_path"]),
        "port": int(port),
        "daemon_url": base,
        "ui_url": base + "/ui/",
        "pid": pid,
        "counts": dict(summary),
    }
    if token:
        payload["token"] = token
    if extra:
        payload.update(dict(extra))
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a disposable HippoMind Atlas QA ledger and optionally run its daemon.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT),
                        help="fixture root directory (default: %s)" % DEFAULT_ROOT)
    parser.add_argument("--memories", type=int, default=DEFAULT_MEMORIES,
                        help="target memory count (default: %d)" % DEFAULT_MEMORIES)
    parser.add_argument("--anchor", default="",
                        help="ISO-8601 anchor for the 90-day distribution (default: now)")
    parser.add_argument("--port", type=int, default=0,
                        help="daemon port (default: pick a free loopback port)")
    parser.add_argument("--daemon", action="store_true",
                        help="start the fixture daemon in the background")
    parser.add_argument("--serve", action="store_true",
                        help="start the fixture daemon in the foreground")
    parser.add_argument("--clean", action="store_true",
                        help="stop the fixture daemon and delete the fixture root")
    parser.add_argument("--force", action="store_true",
                        help="allow --clean without a seed manifest")
    parser.add_argument("--print-token", action="store_true",
                        help="include the daemon bearer token in the JSON output")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if args.clean:
        return _clean(root, args.force)

    anchor = _parse_anchor(args.anchor)
    total = max(240, int(args.memories))
    if total < 600:
        sys.stderr.write("[gui_seed] note: --memories %d is below the 600-row QA contract\n" % total)

    paths = _prepare_workspace(root)
    token = _write_token(paths["token_path"])
    port = int(args.port) if args.port else _free_port()

    started = time.time()
    summary = _seed_ledger(paths["ledger_path"], paths["runtime_dir"], anchor, total)
    summary["seed_seconds"] = round(time.time() - started, 2)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "anchor": anchor.isoformat(),
        "root": str(paths["root"]),
        "runtime_dir": str(paths["runtime_dir"]),
        "ledger_path": str(paths["ledger_path"]),
        "token_path": str(paths["token_path"]),
        "state_path": str(paths["state_path"]),
        "port": port,
        "counts": summary,
    }
    _write_json(paths["manifest_path"], manifest)

    if args.serve:
        print(json.dumps(_payload(paths, port, summary,
                                  token=token if args.print_token else ""),
                         ensure_ascii=False, indent=2))
        return _serve_foreground(paths, port)

    pid: Optional[int] = None
    daemon_note: Optional[Dict[str, Any]] = None
    if args.daemon:
        runtime = _start_daemon(paths, port, token)
        pid = int(runtime["pid"])
        daemon_note = {"health": runtime["health"],
                       "ledger_memories": int(runtime["stats"].get("ledger", {}).get("memories", 0)
                                              or runtime["stats"].get("stats", {}).get("memories", 0)
                                              or 0)}
        manifest["daemon_pid"] = pid
        manifest["daemon_health"] = runtime["health"]
        _write_json(paths["manifest_path"], manifest)

    payload = _payload(paths, port, summary, token=token if args.print_token else "", pid=pid,
                       extra={"daemon": daemon_note} if daemon_note else None)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

