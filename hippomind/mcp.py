"""Small dependency-free MCP stdio adapter for HippoMind Memory Hub.

It implements the MCP tools surface over JSON-RPC lines so editors/agents can
launch it directly.  The tool set is intentionally narrow and can be extended
without exposing the SQLite schema to callers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.parse import unquote, urlsplit

from .access import AccessDenied, AccessPolicy
from .agent_config import load_agent_config
from .context_pack import DEFAULT_MAX_CHARS, DEFAULT_MAX_TOKENS, ContextPackBuilder, ContextPackOptions
from .injection import InjectionState, normalise_agent
from .ledger import LedgerError, SQLiteLedger
from .watcher import ArchiveWatcher


SERVER_INFO = {"name": "hippomind-memory-hub", "version": "0.9.1"}
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")


def _tool(name: str, description: str, properties: Mapping[str, Any], required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": dict(properties), "required": list(required)}}


TOOLS = [
    _tool("hippomind_context_pack", "返回最小、带证据、默认不含原始聊天的任务上下文。", {
        "query": {"type": "string"}, "task_id": {"type": "string"}, "project": {"type": "string"},
        "scope": {"type": "string", "default": "shared"}, "max_items": {"type": "integer", "default": 8},
        "max_chars": {"type": "integer", "default": DEFAULT_MAX_CHARS},
        "max_tokens": {"type": "integer", "default": DEFAULT_MAX_TOKENS},
        "source_agents": {"type": "array", "items": {"type": "string"}},
        "debug": {"type": "boolean", "default": False},
    }, ("query",)),
    _tool("hippomind_ingest", "写入一条带 source_agent、scope 和 evidence 的结构化事实。", {
        "content": {"type": "string"}, "kind": {"type": "string", "default": "fact"},
        "source_agent": {"type": "string"}, "session_id": {"type": "string"}, "task_id": {"type": "string"},
        "project": {"type": "string"}, "scope": {"type": "string", "default": "shared"},
        "evidence": {"type": "array", "items": {"type": "string"}}, "context": {"type": "object"},
    }, ("content", "source_agent", "evidence")),
    _tool("hippomind_feedback", "用外部验证结果升权或降权一条工作记忆。", {
        "memory_id": {"type": "string"}, "signal": {"type": "number", "minimum": -1, "maximum": 1},
        "note": {"type": "string"}, "source_agent": {"type": "string"},
    }, ("memory_id", "signal")),
    _tool("hippomind_stats", "读取账本、归档和来源统计，不返回原始聊天。", {}, ()),
    _tool("hippomind_injection", "在当前 Agent 内开启、关闭或查询下一轮 Context Pack 注入。后台采集和记忆维护不受影响。", {
        "action": {"type": "string", "enum": ["on", "off", "toggle", "status"]},
        "agent": {"type": "string", "description": "管理员可指定 Agent 或 all；普通 Agent 只能操作自己。"},
    }, ("action",)),
    _tool("hippomind_sync", "立即从可访问的 Agent-Chat-Archive 做一次增量同步。", {
        "archive": {"type": "string"},
    }, ()),
    _tool("hippomind_recall", "关键词检索共享记忆（相关性排序，不含 raw chat）。", {
        "query": {"type": "string"}, "scope": {"type": "string", "default": "shared"},
        "limit": {"type": "integer", "default": 8, "minimum": 1, "maximum": 100},
        "offset": {"type": "integer", "default": 0, "minimum": 0},
    }, ("query",)),
    _tool("hippomind_pin", "置顶或取消置顶一条记忆（管理员/已安装 Agent）。", {
        "memory_id": {"type": "string"}, "unpin": {"type": "boolean", "default": False},
    }, ("memory_id",)),
    _tool("hippomind_forget", "遗忘记忆：按 memory_id 列表或 import_batch 批次回滚。", {
        "memory_ids": {"type": "array", "items": {"type": "string"}},
        "import_batch": {"type": "string"},
        "purge": {"type": "boolean", "default": False,
                  "description": "连同审计事件内容一并删除（真正擦除，慎用）"},
    }, ()),
    _tool("hippomind_conflicts", "列出冲突/被取代的 claim 谱系（人工裁决入口）。", {
        "scope": {"type": "string", "default": "shared"},
        "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
    }, ()),
    _tool("hippomind_graph", "实体图谱：以 claim 主语为节点、记忆关系为边的有界邻域（只读，不含正文）。", {
        "subject": {"type": "string"},
        "scope": {"type": "string", "default": "shared"},
        "hops": {"type": "integer", "default": 1, "minimum": 1, "maximum": 2},
        "limit": {"type": "integer", "default": 200, "minimum": 10, "maximum": 2000},
    }, ("subject",)),
]

RESOURCE_TEMPLATES = [{
    "uriTemplate": "hippomind://projects/{name}/active-context",
    "name": "project-active-context",
    "description": "读取指定项目的最新结构化 Context Pack；不返回 raw chat。",
    "mimeType": "text/plain",
}]


class MCPAdapter:
    def __init__(self, ledger_path: str = "", *, agent_token: str = "", agent_token_file: str = "",
                 unsafe_admin: bool = False):
        self.ledger = SQLiteLedger(ledger_path or None)
        self.contexts = ContextPackBuilder(self.ledger)
        self._configured_agent = False
        self.runtime_dir = Path(self.ledger.path).parent
        if agent_token_file and not agent_token:
            config = load_agent_config(agent_token_file)
            agent_token = config["token"]
            configured_runtime = str(config.get("runtime_dir", "")).strip()
            if configured_runtime:
                self.runtime_dir = Path(configured_runtime).expanduser()
        credential = self.ledger.resolve_agent_credential(agent_token) if agent_token else None
        if unsafe_admin:
            self.policy = AccessPolicy.administrator()
            self._configured_agent = True
        elif credential is not None:
            self.policy = AccessPolicy.from_credential(credential)
            self._configured_agent = True
        else:
            # A bare stdio server may be launched by an editor before its
            # installer runs. It can retrieve shared context but cannot write,
            # sync, or impersonate an agent without a scoped credential.
            self.policy = AccessPolicy.read_only_shared()
        self.injection = InjectionState(self.runtime_dir)

    def call(self, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        if name == "hippomind_context_pack":
            options = ContextPackOptions(
                max_items=arguments.get("max_items", 8), max_chars=arguments.get("max_chars", DEFAULT_MAX_CHARS),
                max_tokens=arguments.get("max_tokens", DEFAULT_MAX_TOKENS),
                raw_chat=False, require_evidence=True, scope=arguments.get("scope", "shared"),
                project=arguments.get("project", ""), task_id=arguments.get("task_id", ""),
                source_agents=arguments.get("source_agents", []) or [], debug=bool(arguments.get("debug", False)),
            )
            options.scope = self.policy.enforce_options(options.scope)
            return self.contexts.build(str(arguments.get("query", "")), options)
        if name == "hippomind_ingest":
            result = self.ledger.ingest(self.policy.enforce_ingest(arguments))
            return {"event_id": result.event_id, "memory_id": result.memory_id,
                    "inserted": result.inserted, "content_hash": result.content_hash}
        if name == "hippomind_feedback":
            memory = self.ledger.get_memory(str(arguments.get("memory_id", "")))
            if memory is None:
                raise LedgerError("未找到记忆")
            self.policy.enforce_feedback_scope(memory.get("scope", ""))
            feedback_source = self.policy.enforce_feedback_source(arguments.get("source_agent", ""))
            return {"updated": self.ledger.feedback(str(arguments.get("memory_id", "")),
                                                      arguments.get("signal", 0.0),
                                                      str(arguments.get("note", "")),
                                                      feedback_source)}
        if name == "hippomind_stats":
            self.policy.can_read_stats()
            if self.policy.admin:
                return self.ledger.stats()
            return {"access": "restricted", "scopes": list(self.policy.scopes), "raw_chat": 0}
        if name == "hippomind_injection":
            self.policy.require("read")
            if not self._configured_agent:
                raise AccessDenied("上下文开关需要已安装的 Agent 凭据")
            requested = normalise_agent(arguments.get("agent", "all" if self.policy.admin else self.policy.agent_id))
            if not self.policy.admin and requested != normalise_agent(self.policy.agent_id):
                raise AccessDenied(f"agent {self.policy.agent_id} 不能修改 {requested} 的上下文开关")
            return self.injection.update(str(arguments.get("action", "status")), requested)
        if name == "hippomind_sync":
            self.policy.require("sync")
            return ArchiveWatcher(self.ledger, arguments.get("archive") or None).sync_once()
        if name == "hippomind_recall":
            self.policy.require("read")
            scope = self.policy.enforce_options(arguments.get("scope", "shared"))
            hits = self.ledger.search(
                str(arguments.get("query", "")), scope=scope,
                limit=max(1, min(100, int(arguments.get("limit", 8)))),
                offset=max(0, int(arguments.get("offset", 0))))
            for hit in hits:
                hit.pop("raw_chat", None)
            return {"query": str(arguments.get("query", "")), "scope": scope,
                    "results": hits}
        if name == "hippomind_pin":
            self.policy.require("write")
            outcome = self.ledger.pin_memory(
                str(arguments.get("memory_id", "")),
                pinned=not bool(arguments.get("unpin", False)),
                source=self.policy.agent_id or "mcp")
            return {"pinned": bool(outcome.get("pinned",
                                               not arguments.get("unpin", False)))}
        if name == "hippomind_forget":
            self.policy.require("write")
            ids = [str(v) for v in (arguments.get("memory_ids") or []) if str(v)]
            batch = str(arguments.get("import_batch", "") or "")
            if not ids and not batch:
                raise LedgerError("需要 memory_ids 或 import_batch 之一")
            result = self.ledger.forget(
                memory_ids=ids, import_batch=batch,
                purge=bool(arguments.get("purge", False)),
                source_agent=self.policy.agent_id or "mcp")
            return {k: result.get(k) for k in ("forgotten", "purged",
                                               "purged_events", "impact")}
        if name == "hippomind_conflicts":
            self.policy.require("read")
            scope = self.policy.enforce_options(arguments.get("scope", "shared"))
            report = self.ledger.list_conflicts(
                scope=scope, limit=max(1, min(500, int(arguments.get("limit", 50)))))
            for item in report.get("items", []):
                item.pop("content", None)  # 谱系视图不外带正文
            return report
        if name == "hippomind_graph":
            self.policy.require("read")
            subject = str(arguments.get("subject") or "").strip()
            if not subject:
                raise LedgerError("hippomind_graph 需要 subject")
            scope = self.policy.enforce_options(
                arguments.get("scope", "shared"))
            hops = max(1, min(2, int(arguments.get("hops", 1))))
            limit = max(10, min(2000, int(arguments.get("limit", 200))))
            # 节点/边仅含主语、计数与 memory_id，不含记忆正文。
            return self.ledger.entity_graph(
                subject, scope=scope, hops=hops, limit=limit)
        raise LedgerError(f"未知 MCP 工具: {name}")

    def resource_templates(self) -> Dict[str, Any]:
        return {"resourceTemplates": RESOURCE_TEMPLATES}

    def read_resource(self, uri: str) -> Dict[str, Any]:
        parsed = urlsplit(str(uri or ""))
        if parsed.scheme != "hippomind" or parsed.netloc != "projects":
            raise LedgerError("不支持的 HippoMind resource URI")
        path = parsed.path.strip("/").split("/")
        if len(path) != 2 or path[1] != "active-context":
            raise LedgerError("resource URI 必须是 hippomind://projects/{name}/active-context")
        project = unquote(path[0]).strip()[:512]
        if not project or "/" in project:
            raise LedgerError("resource project 无效")
        scope = self.policy.enforce_options("shared")
        pack = self.contexts.build(project, ContextPackOptions(
            max_items=8, max_chars=DEFAULT_MAX_CHARS, max_tokens=DEFAULT_MAX_TOKENS,
            raw_chat=False, require_evidence=True, scope=scope, project=project,
        ))
        return {"contents": [{"uri": str(uri), "mimeType": "text/plain",
                               "text": str(pack.get("text", ""))}]}


def _result(request_id: Any, payload: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(adapter: MCPAdapter) -> None:
    for line in sys.stdin:
        request_id = None
        notification = False
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise LedgerError("JSON-RPC 请求必须是对象")
            request_id = request.get("id")
            notification = "id" not in request
            method = request.get("method", "")
            params = request.get("params", {})
            if not isinstance(params, Mapping):
                raise LedgerError("params 必须是对象")
            if method == "initialize":
                requested = str(params.get("protocolVersion", ""))
                protocol = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                response = _result(request_id, {"protocolVersion": protocol, "capabilities": {
                                                    "tools": {"listChanged": False},
                                                    "resources": {"subscribe": False, "listChanged": False}},
                                                "serverInfo": SERVER_INFO})
            elif method == "tools/list":
                response = _result(request_id, {"tools": TOOLS})
            elif method == "resources/list":
                response = _result(request_id, {"resources": []})
            elif method == "resources/templates/list":
                response = _result(request_id, adapter.resource_templates())
            elif method == "resources/read":
                params_uri = params.get("uri", "")
                response = _result(request_id, adapter.read_resource(str(params_uri)))
            elif method == "tools/call":
                name = str(params.get("name", ""))
                arguments = params.get("arguments", {})
                if not isinstance(arguments, Mapping):
                    raise LedgerError("工具参数必须是对象")
                try:
                    payload = adapter.call(name, arguments)
                    response = _result(request_id, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                                                    "structuredContent": payload})
                except LedgerError as exc:
                    response = _result(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
            elif method == "ping":
                response = _result(request_id, {})
            elif method.startswith("notifications/"):
                continue
            else:
                response = _error(request_id, -32601, "method not found")
        except LedgerError as exc:
            response = _error(request_id, -32602, str(exc))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            response = _error(request_id, -32700, str(exc))
        if not notification:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="HippoMind MCP stdio adapter")
    parser.add_argument("--ledger")
    parser.add_argument("--agent-token", help="受限 Agent 凭据；未提供时仅可读 shared Context Pack")
    parser.add_argument("--agent-token-file", help="包含受限凭据的 0600 JSON 文件")
    parser.add_argument("--unsafe-admin", action="store_true", help="仅供本机管理员诊断，不应放入 Agent 配置")
    args = parser.parse_args(argv)
    serve(MCPAdapter(args.ledger or "", agent_token=args.agent_token or "",
                     agent_token_file=args.agent_token_file or "", unsafe_admin=args.unsafe_admin))


if __name__ == "__main__":
    main()
