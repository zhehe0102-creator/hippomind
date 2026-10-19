# 通用 Agent Memory Hub 协议

## 写入

每个结构化事实必须包含 `content`、`source_agent`、`scope` 和至少一条 `evidence`。建议同时写入 `session_id`、`task_id`、`project`、`role`、`timestamp` 与 `kind`。

`kind` 可取 `task`、`progress`、`decision`、`file`、`blocker`、`next_step`、`preference`、`result`、`semantic` 或 `fact`。不确定的信息应保留低 `confidence`，不得把模型预测伪装成外部验证事实。

## 读取

默认调用 Context Pack，限制为 8 项、6000 token 预算和 16000 字符运输上限（按需上限，不足不填充）。先按 task/project/source/time 过滤，再使用关键词；无可靠命中时接受空结果。可按 Agent 使用 `hippomind inject on|off|toggle|status --agent <agent>` 控制注入，关闭不停止记忆服务。原始聊天只有人工审计时才可显式请求，不能默认注入模型。

### 显式预取

Agent 只在普通用户文本含有 `记忆：` 或 `记忆:` 时预取一次。标记可在自然语言任意位置，检索查询取标记之后；引用、代码块、行内代码与空标记不触发。没有标记时不得访问账本、启动 daemon 或注入历史。触发后从标准输入传入完整当前用户要求：

```bash
HIPPOMIND_REQUEST_CWD="$PWD"
printf '%s\n' '<本轮用户完整要求>' | python3 -m hippomind auto-context --stdin --trigger-mode anywhere --cwd "$HIPPOMIND_REQUEST_CWD" --agent <agent-name>
```

触发后命令从自然语言要求和工作目录软线索中解析关键词、任务和项目提示，按需启动或复用 localhost daemon，并返回 `<MEMORY_DATA_UNTRUSTED>` 中的严格 Context Pack。默认上限为 8 项/6000 token/16000 字符运输上限（按需输出），`raw_chat=false` 与 `require_evidence=true` 不可由该自动路径放宽。结果为空时继续当前任务，不能使用低相关历史填充。安装或规则更新后应重载对应 Agent 或新建任务；一个运行中的模型上下文无法由本机后台服务追溯改写。

## 生命周期适配器

Python Agent 可复用 `MemoryHubAgentAdapter`，避免每种客户端自行拼装字段：

```python
from hippomind.agent_adapter import MemoryHubAgentAdapter

memory = MemoryHubAgentAdapter("codex", session_id="run-42", task_id="memory-hub", project="hippomind")
pack = memory.begin_task("实现 daemon API 的并发限制")
memory.checkpoint("SQLite 账本已完成", evidence=["task:memory-hub"], next_steps=["实现 daemon"])
memory.decision("默认不注入原始聊天", evidence=["task:memory-hub"])
memory.complete("API 验收通过", evidence=["test:memory-hub"])
```

适配器返回的上下文同样是非可信参考数据；`untrusted_context_prompt()` 只提供隔离包装，不能绕过工具权限或来源核验。

## HTTP

`hippomindd` 只监听 `127.0.0.1`，数据端点要求 `X-HippoMind-Token`。接口为 `/health`、`/ingest`、`/recall`、`/context-pack`、`/feedback`、`/sync`、`/stats`；请求最多 1 MiB，响应最多 512 KiB。

## MCP

用 `python3 -m hippomind.mcp` 启动 stdio server。可用工具是 `hippomind_context_pack`、`hippomind_ingest`、`hippomind_feedback`、`hippomind_stats`、`hippomind_injection` 和 `hippomind_sync`。支持 MCP 工具调用的 Agent 可在当前对话中调用 `hippomind_injection`，传入 `action=on|off|toggle|status` 控制下一轮上下文注入；普通 Agent 只能修改自己，管理员才可指定 `all` 或其他 Agent。开关不停止 daemon、watcher、归档、巩固和降权。
