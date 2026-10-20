"""The argparse tree for the ``hippomind`` command line.

Every sub-command binds ``func=`` to an implementation elsewhere in this
package; the argument surface itself is unchanged by the module split.
"""

from __future__ import annotations

import argparse
from ..auto_context import AUTO_MAX_CHARS, AUTO_MAX_ITEMS, AUTO_TRIGGER_MODES
from ..context_pack import DEFAULT_MAX_CHARS, DEFAULT_MAX_TOKENS
from . import cmd_auto_context
from .cmd_data import cmd_export, cmd_import
from .cmd_integrate import cmd_agent_status, cmd_discover_agents, cmd_install_agents, cmd_install_auto
from .cmd_lifecycle import (
    cmd_doctor, cmd_init, cmd_login_install, cmd_login_remove, cmd_monitor, cmd_start, cmd_status, cmd_stop,
    cmd_version,
)
from .cmd_maintain import (
    cmd_backup, cmd_checkup, cmd_maintain, cmd_recovery_check, cmd_redact_raw, cmd_restore, cmd_scan_raw,
)
from .cmd_read import cmd_context, cmd_inject, cmd_sync, cmd_watch
from .cmd_write import (
    cmd_conflicts, cmd_facts, cmd_feedback, cmd_forget, cmd_graph, cmd_ingest, cmd_pin, cmd_pins, cmd_resolve,
    cmd_skills,
)
from .common import _connection_arguments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hippomind", description="HippoMind 本机 Memory Hub")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化 SQLite/WAL 账本，可选导入旧 JSON")
    _connection_arguments(init)
    init.add_argument("--import-json")
    init.add_argument("--source-agent", default="legacy-json")
    init.set_defaults(func=cmd_init)

    start = sub.add_parser("start", help="启动或复用本机 daemon")
    _connection_arguments(start)
    start.add_argument("--archive")
    start.add_argument("--watch-interval", type=float, default=30.0)
    start.add_argument("--no-watch", action="store_true")
    start.add_argument("--restart", action="store_true", help="停止并重新启动 daemon，用于代码或 schema 升级")
    start.add_argument("--read-only", action="store_true",
                       help="只读模式：所有凭据降级为共享只读视图（LAN 镜像/巡检副本）")
    start.add_argument("--foreground", action="store_true")
    start.set_defaults(func=cmd_start)

    for name, func, help_text in (("stop", cmd_stop, "停止本机 daemon"),
                                                                     ("status", cmd_status, "查看 daemon 与账本状态")):
        command = sub.add_parser(name, help=help_text)
        _connection_arguments(command)
        command.set_defaults(func=func)

    version_cmd = sub.add_parser("version", help="输出版本/schema/账本概要")
    _connection_arguments(version_cmd)
    # ``version`` already prints JSON; the flag exists so release automation and
    # ``tests/test_schema_contract.py`` can ask for machine-readable output
    # explicitly instead of relying on the default being JSON forever.
    version_cmd.add_argument("--json", action="store_true",
                             help="以 JSON 输出（默认已是 JSON；保留该开关供发布脚本显式声明）")
    version_cmd.set_defaults(func=cmd_version)

    doctor = sub.add_parser("doctor", help="以不泄露凭据的方式检查服务是否可用")
    _connection_arguments(doctor)
    doctor.add_argument("--deep", action="store_true", help="额外扫描 raw 证据中的敏感值（只输出聚合计数）")
    doctor.add_argument("--format", choices=("text", "json"), default="text")
    doctor.set_defaults(func=cmd_doctor)

    ingest = sub.add_parser("ingest", help="写入一条或一批带证据的记忆记录")
    _connection_arguments(ingest)
    ingest.add_argument("--content")
    ingest.add_argument("--kind", default="fact")
    ingest.add_argument("--agent", default="unknown")
    ingest.add_argument("--session", default="")
    ingest.add_argument("--task", default="")
    ingest.add_argument("--project", default="")
    ingest.add_argument("--role", default="assistant")
    ingest.add_argument("--scope", default="shared")
    ingest.add_argument("--evidence", action="append", default=[])
    ingest.add_argument("--json")
    ingest.add_argument("--file")
    ingest.set_defaults(func=cmd_ingest)

    context = sub.add_parser("context", help="检索严格、最小的 Context Pack")
    _connection_arguments(context)
    context.add_argument("query")
    context.add_argument("--task", default="")
    context.add_argument("--project", default="")
    context.add_argument("--source-agent", action="append", default=[])
    context.add_argument("--scope", default="shared")
    context.add_argument("--from-time", default="")
    context.add_argument("--to-time", default="")
    context.add_argument("--as-of", default="",
                         help="双时态历史切片：按该 ISO 时间点求值 valid_from/valid_until 窗口")
    context.add_argument("--max-items", type=int, default=8)
    context.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    context.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    context.add_argument("--raw-chat", action="store_true")
    context.add_argument("--allow-no-evidence", action="store_true")
    context.add_argument("--debug", action="store_true")
    context.add_argument("--enable-ngram-vector", action="store_true",
                         help="启用内置 n-gram 哈希向量重排通道（默认关闭）")
    context.add_argument("--text-only", action="store_true")
    context.add_argument("--offline", action="store_true")
    context.set_defaults(func=cmd_context)

    auto_context = sub.add_parser("auto-context", help="按当前要求构建最小 Context Pack（可使用显式记忆门控）")
    _connection_arguments(auto_context)
    auto_context.add_argument("request", nargs="?", default="",
                              help="本轮用户的完整自然语言要求；Agent 默认只响应记忆：或记忆:")
    auto_context.add_argument("--stdin", action="store_true",
                              help="从标准输入读取完整用户要求，适合 Agent 前置 hook")
    auto_context.add_argument("--cwd", default="",
                              help="当前任务工作目录，仅作为软检索线索")
    auto_context.add_argument("--agent", default="codex")
    auto_context.add_argument("--task", default="")
    auto_context.add_argument("--project", default="")
    auto_context.add_argument("--scope", default="shared")
    auto_context.add_argument("--max-items", type=int, default=AUTO_MAX_ITEMS)
    auto_context.add_argument("--max-chars", type=int, default=AUTO_MAX_CHARS)
    auto_context.add_argument("--trigger-mode", choices=AUTO_TRIGGER_MODES, default="always",
                              help="记忆注入门控：默认 Agent 使用 anywhere；CLI 直调用默认 always 以兼容旧脚本")
    auto_context.add_argument("--format", choices=("prompt", "text", "json"), default="prompt")
    auto_context.add_argument("--debug", action="store_true")
    auto_context.add_argument("--offline", action="store_true", help="不访问或启动 daemon，直接读本机 SQLite")
    auto_context.add_argument("--no-start", action="store_true", help="daemon 不在时仅离线读取")
    auto_context.add_argument("--archive")
    auto_context.add_argument("--watch-interval", type=float, default=30.0)
    auto_context.add_argument("--no-watch", action="store_true")
    auto_context.set_defaults(func=cmd_auto_context)

    inject = sub.add_parser("inject", help="开关自动 Context Pack 注入；采集和维护保持运行")
    _connection_arguments(inject)
    inject.add_argument("action", choices=("on", "off", "toggle", "status"), nargs="?", default="status")
    inject.add_argument("--agent", default="all", help="Agent 名称；all 表示默认开关")
    inject.set_defaults(func=cmd_inject)

    forget_cmd = sub.add_parser("forget", help="遗忘请求：移除记忆投影；--purge 连同事件内容清除")
    _connection_arguments(forget_cmd)
    forget_cmd.add_argument("--scope", default="", help="要遗忘的 scope")
    forget_cmd.add_argument("--before", default="",
                            help="只遗忘该 ISO 时间之前创建的记忆")
    forget_cmd.add_argument("--memory-id", action="append", default=[],
                            help="显式指定 memory_id，可重复")
    forget_cmd.add_argument("--purge", action="store_true",
                            help="连同 ledger_events 内容一并删除（真正擦除）")
    forget_cmd.add_argument("--source-agent", default="local-admin")
    forget_cmd.add_argument("--batch", default="",
                            help="按 import_batch 批次回滚导入")
    forget_cmd.add_argument("--dry-run", action="store_true",
                            help="只预览将被遗忘的记忆清单，不执行")
    forget_cmd.set_defaults(func=cmd_forget)

    facts_cmd = sub.add_parser("facts", help="事实投影：活跃 claim 的结构化视图")
    _connection_arguments(facts_cmd)
    facts_cmd.add_argument("--scope", default="shared")
    facts_cmd.add_argument("--subject", default="", help="按 subject 过滤")
    facts_cmd.add_argument("--limit", type=int, default=200)
    facts_cmd.add_argument("--format", choices=("text", "json"), default="text")
    facts_cmd.set_defaults(func=cmd_facts)

    skills_cmd = sub.add_parser("skills", help="技能投影：procedural 记忆的结构化视图")
    _connection_arguments(skills_cmd)
    skills_cmd.add_argument("--scope", default="shared")
    skills_cmd.add_argument("--limit", type=int, default=100)
    skills_cmd.add_argument("--format", choices=("text", "json"), default="text")
    skills_cmd.set_defaults(func=cmd_skills)

    resolve_cmd = sub.add_parser("resolve", help="人工裁决 Slot-Value 冲突：指定胜者记忆，败者让位（保留谱系）")
    _connection_arguments(resolve_cmd)
    resolve_cmd.add_argument("--claim", required=True, help="冲突组内任一 claim_id")
    resolve_cmd.add_argument("--winner", required=True, help="胜者 memory_id")
    resolve_cmd.add_argument("--source-agent", default="local-admin",
                             help="审计事件记录的裁决来源")
    resolve_cmd.set_defaults(func=cmd_resolve)
    install_auto = sub.add_parser("install-auto", help="一次性安装一个 Agent 的显式记忆 Context Pack 规则")
    install_auto.add_argument("--agents-file", help="目标 AGENTS.md；默认 ~/.codex/AGENTS.md")
    install_auto.add_argument("--agent", default="codex")
    install_auto.add_argument("--print-only", action="store_true", help="只输出规则，不写文件")
    install_auto.add_argument("--agent-config", help="规则引用的受限 Agent config")
    install_auto.set_defaults(func=cmd_install_auto)

    install_agents_command = sub.add_parser(
        "install-agents", help="为多个本机 Agent 安装显式记忆规则、受限凭据和 MCP profile"
    )
    _connection_arguments(install_agents_command)
    install_agents_command.add_argument("--agent", action="append", default=[],
                                        help="Agent 名称，可重复或使用 all")
    install_agents_command.add_argument("--auto", action="store_true",
                                        help="只为自动发现已检测到的运行时安装；未知 Agent 只报告不修改")
    install_agents_command.add_argument("--scope", action="append", default=[],
                                        help="允许的 scope，可重复；默认 shared 与该 Agent 私有 scope")
    install_agents_command.add_argument("--permission", action="append", default=[],
                                        choices=("read", "ingest", "feedback", "stats",
                                                 "shared_ingest", "shared_feedback", "sync"))
    install_agents_command.add_argument("--home", help="测试或自定义规则根目录，默认当前用户 home")
    install_agents_command.set_defaults(func=cmd_install_agents)

    agent_status = sub.add_parser("agent-status", help="查看 Agent 规则和凭据状态（不显示 token）")
    _connection_arguments(agent_status)
    agent_status.add_argument("--agent", action="append", default=[])
    agent_status.add_argument("--home")
    agent_status.set_defaults(func=cmd_agent_status)

    discover = sub.add_parser("discover-agents", help="扫描本机 Agent 运行时；未知 Agent 只报告不修改")
    _connection_arguments(discover)
    discover.add_argument("--home")
    discover.set_defaults(func=cmd_discover_agents)

    sync = sub.add_parser("sync", help="立即增量同步归档；daemon 不在时本地执行")
    _connection_arguments(sync)
    sync.add_argument("--archive")
    sync.add_argument("--watch-interval", type=float, default=30.0)
    sync.set_defaults(func=cmd_sync)

    watch = sub.add_parser("watch", help="以低频游标方式持续采集归档")
    _connection_arguments(watch)
    watch.add_argument("--archive")
    watch.add_argument("--watch-interval", type=float, default=30.0)
    watch.add_argument("--once", action="store_true")
    watch.set_defaults(func=cmd_watch)

    for name, func, help_text, argument in (
        ("export", cmd_export, "导出记忆为 JSONL（ingest 兼容）", "output"),
        ("import", cmd_import, "从 JSONL 导入（幂等；兼容旧版单对象 JSON）", "input"),
        ("backup", cmd_backup, "创建 SQLite 原子备份", "output"),
        ("pin", cmd_pin, "置顶/取消置顶记忆（--unpin 取消）", "memory-id"),
        ("restore", cmd_restore, "从 SQLite 备份恢复（daemon 必须停止）", "input"),
    ):
        command = sub.add_parser(name, help=help_text)
        _connection_arguments(command)
        command.add_argument(argument)
        if name == "import":
            command.add_argument("--source-agent", default="legacy-json")
        if name == "pin":
            command.add_argument("--unpin", action="store_true",
                                 help="取消置顶（默认执行置顶）")
        if name == "restore":
            command.add_argument("--sha256", default="",
                                 help="期望校验和；与实际不符时拒绝恢复")
        if name == "export":
            command.add_argument("--format", choices=("jsonl", "sql"),
                                 default="jsonl",
                                 help="sql=可回放审计脚本（同版本空库 sqlite3 < file）")
            command.add_argument("--since", default="",
                                 help="只导出该 ISO 时刻之后创建的记忆（增量导出）")
            command.add_argument("--scope", default="shared")
            command.add_argument("--include-raw-chat", action="store_true")
            command.add_argument("--limit", type=int, default=100_000)
            command.add_argument("--encrypt", action="store_true",
                                 help="口令加密导出（混淆级；配合 import 自动解密）")
            command.add_argument("--passphrase", default="")
        if name == "import":
            command.add_argument("--passphrase", default="",
                                 help="加密导出的口令（缺省时交互输入）")
        if name == "import":
            command.add_argument("--scope", default="")
        command.set_defaults(func=func)

    conflicts_cmd = sub.add_parser("conflicts", help="列出冲突/被取代 claim 谱系")
    _connection_arguments(conflicts_cmd)
    conflicts_cmd.add_argument("--scope", default="shared")
    conflicts_cmd.add_argument("--limit", type=int, default=100)
    conflicts_cmd.add_argument("--format", choices=("text", "json"), default="text")
    conflicts_cmd.set_defaults(func=cmd_conflicts)

    graph_cmd = sub.add_parser("graph", help="实体图谱：claim 主语邻域（因果/关联）")
    _connection_arguments(graph_cmd)
    graph_cmd.add_argument("subject", help="起始实体（claim subject）")
    graph_cmd.add_argument("--scope", default="shared")
    graph_cmd.add_argument("--hops", type=int, default=1, choices=(1, 2))
    graph_cmd.add_argument("--limit", type=int, default=200)
    graph_cmd.add_argument("--format", choices=("text", "json"), default="text")
    graph_cmd.set_defaults(func=cmd_graph)

    pins_cmd = sub.add_parser("pins", help="列出置顶记忆（--scope 过滤，--format json）")
    _connection_arguments(pins_cmd)
    pins_cmd.add_argument("--scope", default="shared")
    pins_cmd.add_argument("--limit", type=int, default=100)
    pins_cmd.add_argument("--format", choices=("text", "json"), default="text")
    pins_cmd.set_defaults(func=cmd_pins)

    monitor_cmd = sub.add_parser("monitor", help="只读巡检循环：周期打印健康与规模摘要（Ctrl-C 退出）")
    monitor_cmd.add_argument("--interval", type=float, default=5.0, metavar="SEC",
                             help="巡检间隔秒数（默认 5）")
    monitor_cmd.add_argument("--rounds", type=int, default=0,
                             help="巡检轮数上限（0=无限直到 Ctrl-C）")
    monitor_cmd.add_argument("--json", action="store_true",
                             help="输出单次 JSON 快照并立即退出（离线时退出码 1）")
    _connection_arguments(monitor_cmd)
    checkup_cmd = sub.add_parser("checkup", help="账本深度体检：完整性/FTS 同步/游标/声明一致性（默认只读）")
    _connection_arguments(checkup_cmd)
    checkup_cmd.add_argument("--format", choices=("json", "md", "junit"),
                             default="json",
                             help="md=markdown 报告；junit=CI 集成 XML")
    checkup_cmd.add_argument("--repair", action="store_true",
                             help="发现 FTS 失同步时自动重建索引")
    checkup_cmd.set_defaults(func=cmd_checkup)
    monitor_cmd.set_defaults(func=cmd_monitor)

    redact_raw = sub.add_parser("redact-raw", help="脱敏并收缩既有 raw 聊天证据（daemon 必须停止）")
    _connection_arguments(redact_raw)
    redact_raw.add_argument("--max-chars", type=int, default=4_000)
    redact_raw.add_argument("--batch-size", type=int, default=250)
    redact_raw.set_defaults(func=cmd_redact_raw)

    scan_raw = sub.add_parser("scan-raw", help="扫描既有 raw 证据中的敏感值（只输出聚合计数）")
    _connection_arguments(scan_raw)
    scan_raw.add_argument("--batch-size", type=int, default=250)
    scan_raw.set_defaults(func=cmd_scan_raw)

    maintain = sub.add_parser("maintain", help="执行 raw 保留期、容量与压缩归档维护")
    _connection_arguments(maintain)
    maintain.add_argument("--vacuum", action="store_true",
                    help="整理库文件（VACUUM）；先备份，大库耗时较长")
    maintain.add_argument("--prune-backups", type=int, default=0, metavar="N",
                          help="只保留最近 N 个登记在册的备份文件（sha256 校验后才删）")
    maintain.add_argument("--raw-retention-days", type=int, default=30)
    maintain.add_argument("--raw-ttl-days", type=int, default=0, metavar="N",
                          help="raw 证据物理删除期限（天）；0 表示按保留期+90 推导")
    maintain.add_argument("--purge-raw", action="store_true",
                          help="归档校验通过后物理删除过期 raw 证据（真正缩容）")
    maintain.add_argument("--dedupe", action="store_true",
                          help="折叠规范化后完全相同的重复行（保留最早一条与谱系）")
    maintain.add_argument("--dry-run", action="store_true",
                          help="只报告将发生什么，不做任何写入")
    maintain.add_argument("--raw-capacity", type=int, default=5_000)
    maintain.add_argument("--raw-archive-dir", default="")
    maintain.add_argument("--raw-archive-max-bytes", type=int, default=256 * 1024 * 1024)
    maintain.set_defaults(func=cmd_maintain)

    recovery_check = sub.add_parser("recovery-check", help="执行一次 SQLite/WAL 备份恢复演练（不替换在线账本）")
    _connection_arguments(recovery_check)
    recovery_check.set_defaults(func=cmd_recovery_check)

    feedback = sub.add_parser("feedback", help="以外部反馈调整工作记忆权重")
    _connection_arguments(feedback)
    feedback.add_argument("memory_id")
    feedback.add_argument("signal", type=float)
    feedback.add_argument("--note", default="")
    feedback.add_argument("--source-agent", default="")
    feedback.add_argument("--feedback-id", default="")
    feedback.set_defaults(func=cmd_feedback)

    login_install = sub.add_parser("login-install", help="显式安装可选 macOS 登录启动项")
    _connection_arguments(login_install)
    login_install.add_argument("--archive")
    login_install.add_argument("--watch-interval", type=float, default=30.0)
    login_install.add_argument("--no-watch", action="store_true")
    login_install.add_argument("--no-load", action="store_true", help="仅写入 plist，不立即加载")
    login_install.set_defaults(func=cmd_login_install)
    login_remove = sub.add_parser("login-remove", help="移除 HippoMind 的 macOS 登录启动项")
    login_remove.set_defaults(func=cmd_login_remove)
    return parser
