# 本地 Agent 采集覆盖：Cursor / Pi / local-agent（2026-10-24 修订）

本文件回答一个具体问题：**除了 Claude Code、Codex、Gemini/Antigravity、DSH 之外，
本机还有哪些 Agent 留下了可增量采集的持久会话存储？** 结论先行：

| Agent | 本机持久存储 | 采集状态 |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` | 已覆盖（`hippomind/transcripts.py`） |
| Codex CLI | `~/.codex/sessions/**/*.jsonl` | 已覆盖（`hippomind/transcripts.py`） |
| Gemini / Antigravity | `~/.gemini/antigravity*/conversation_summaries.db` | 已覆盖（`hippomind/gemini_capture.py`） |
| Pi（pi / PiDeck） | `~/.pi/agent/sessions/**/*.jsonl` | 已覆盖（`hippomind/pi_capture.py`） |
| **Cursor** | **本机未安装（无任何数据目录）** | **条件就绪：`hippomind/cursor_capture.py`（合成 fixture 验证，未做端到端实测）** |
| `~/.local-agent` | 只有 `AGENTS.md`（HippoMind 注入块） | 无会话存储，无需采集 |
| Agent Bridge | `~/.agent-bridge/transcripts/sess_*.jsonl` | 仅 agy/Antigravity 会话，非 Cursor/Pi；可作未来来源 |

> **本机未安装 Cursor，未做端到端实测。** 本文件的 Cursor 章节是**基于公开来源的
> 证据调查** + **合成 fixture 验证的采集逻辑**，不是对真实 Cursor 的实测结论。
> 每一条 schema 结论都标注了来源 URL；查不到的写明「查不到」。

---

## 1. Pi：已实现原生增量采集

### 1.1 存储证据

`~/.pi/agent/sessions/<编码 cwd>/<ISO 时间戳>_<uuid>.jsonl`，每行一条 JSON 记录
（append-only，会话内重写/截断通过 `deleted` 墓碑表达）。2026-10-23 实测：
**136 个文件、342,137,116 字节**。全库记录类型统计：

| 记录类型 | 数量 | 处理方式 |
| --- | --- | --- |
| `session` | 134 | 会话头：id / cwd / parentSession |
| `message` | 35,843 | user 1,252、assistant 18,491、toolResult 16,052、system 48 |
| `compaction` | 93 | 会话压缩摘要（中位 13,269 字符，最大 23,899），按 task 记忆采集 |
| `session_info` | 40 | 会话改名，写入后续记录 metadata |
| `deleted` | 222 | resend-truncate 墓碑，用于防御性跳过 |
| `custom` / `custom_message` | 3,213 / 1,106 | 注入上下文/统计事件，跳过 |
| `context_edit` / `model_change` / `thinking_level_change` | 268 / 375 / 260 | 跳过 |

`message.content` 是 block 列表：`text` 21,814、`thinking` 10,023、
`toolCall` 16,080、`image` 12。采集只取 `text` block，其余一律不入库。

### 1.2 采集语义

- **user/assistant 文本** → `kind=raw_chat`、`source_agent=pi`、`scope=agent:pi`、
  `raw_chat=1`；`session_id` 来自会话头，`project` 取 cwd 目录名；
  evidence 为 `pi:<文件名>:<行号>`；metadata 含 transcript_path / line / cwd /
  session_name / parent_session / entry_id / format。
- **compaction 摘要** → `kind=task`、`scope=shared`、`task_id=<session_id>`、
  evidence `pi:compaction:<session_id>:<record_id>`，metadata `source=pi-compaction`。
- **跳过**：`thinking` / `toolCall` / `image` block、`toolResult` 与 `system` 角色、
  `custom` / `custom_message`、`context_edit`、`model_change`、
  `thinking_level_change`、HippoMind 提炼器提示词。
- **删除**：同一读取窗口内出现 `deleted(originalEntryId=<message id>)` 时跳过该消息。
  实测本机 222 条墓碑全部指向**已从文件物理删除**的记录（交集为 0），所以这是防御性逻辑；
  跨窗口「先入库、后删除」无法回撤（append-only 账本），由 raw 保留期/容量维护自然淘汰。
- **中断安全**：只消费以 `\n` 结尾的完整行；写者半行时游标不前进，下一轮重读。
- **超大行**：单行 > 8 MiB 无法解析，跳过该行并继续后续记录；> 64 MiB 的病理行置 `resync` 标记。

### 1.3 增量状态与边界

- `source_cursors` 键：`transcript:pi:<sha1(path)[:16]>`，值为 JSON：
  `{offset, line, size, session_id, cwd, session_name, parent_session, updated_at, resync?}`。
- 边界：每文件每轮最多读 2 MB、最多 40 行/40 条记录；每轮全局最多 200 条；
  单条消息截断 4,000 字符，摘要截断 8,000 字符；每批 ingest ≤ 1,000 条。
- 文件按 mtime **从新到旧**扫描；文件被截断/替换（size < offset）时从头重扫。
- 根目录可用 `HIPPOMIND_PI_SESSION_ROOTS`（`os.pathsep` 分隔）覆盖。

### 1.4 验证

- `python3 -m unittest tests.test_pi_capture -v` → **12 项 OK**。
- 真实数据只读验证（临时账本、不改 `~/.pi`）：691 KB 会话 → 34 raw + 1 task；
  43 KB 含 7 条墓碑 → 0 泄漏；2.3 MB 含超大行 → 跳过 1 条后仍采到 2 条；
  全库冒烟 136 文件、5 轮、197 条入库、0 错误。

---

## 2. Cursor：证据调查（本机未安装，全部结论带来源）

### 2.1 本机现状（2026-10-24 复核，可复现）

| 检查项 | 结果 |
| --- | --- |
| `/Applications/Cursor.app` | 不存在 |
| `~/Library/Application Support/Cursor` | 不存在 |
| `~/.config/Cursor` | 不存在 |
| `cursor` / `cursor-agent` CLI | 不在 PATH |
| `mdfind com.todesktop.230313mzl4w4u92` | 无结果 |
| `~/.cursor` | 存在，但**只有 HippoMind 自己写的** `mcp.json` / `rules/` / `skills/` |
| `~/.cursor/projects` / `chats` / `acp-sessions` | 均不存在 |
| 家目录 `state.vscdb` | 4 个，全部属于 VS Code 系（`.vscode-shared`、Antigravity IDE、Code、CodeBuddy CN），**表只有 `ItemTable`，无 `cursorDiskKV`** |

**关键教训**：`~/.cursor` 存在**不能**当作「Cursor 已安装」的证据——HippoMind 的
`install-agents` 会主动写 `~/.cursor/mcp.json`。探测必须看 Cursor 专属子目录
（`projects/`、`chats/`、`acp-sessions/`）或 Cursor 专属表（`cursorDiskKV`）。
这条已写进 `probe()` 并被 `test_dot_cursor_without_cursor_subdirs_is_not_installed` 锁定。

### 2.2 Cursor 有三套互不相同的本地存储（带来源）

Cursor 是 VS Code / Electron 派生，但**不是**简单沿用 VS Code 的聊天存储。公开资料显示
它至少有三套并列的会话表示：

| # | 形态 | 位置 | 来源 |
| --- | --- | --- | --- |
| 1 | **Composer（IDE，SQLite）** | `<user-data>/globalStorage/state.vscdb` 的 `cursorDiskKV` 表；`<user-data>/workspaceStorage/<hash>/state.vscdb` 的 `ItemTable` | [cursor-history README「Where Cursor Stores Data」](https://github.com/S2thend/cursor-history)、[Xuzhen-Li/cursor-state-vscdb AGENT_CONTEXT.md](https://github.com/Xuzhen-Li/cursor-state-vscdb/blob/main/docs/AGENT_CONTEXT.md) |
| 2 | **Store / Agent（CLI）** | `~/.cursor/projects/**/agent-transcripts/**/*.jsonl` 与 `~/.cursor/chats/**/store.db`、`~/.cursor/acp-sessions/**/store.db` | [cursor-history README 源表](https://github.com/S2thend/cursor-history)、[cursor-history paths.ts](https://github.com/S2thend/cursor-history/blob/main/src/core/store-stack/paths.ts) |
| 3 | **Legacy ItemTable 聊天 JSON** | `ItemTable` 键 `workbench.panel.aichat.view.aichat.chatdata` / `workbench.panel.aichat.chatdata` | [cursor-history storage.ts CHAT_DATA_KEYS](https://github.com/S2thend/cursor-history/blob/main/src/core/storage.ts)、[assistant-memory cursor.ts](https://github.com/dishangyijiao-labs/assistant-memory/blob/main/server/ingest/cursor.ts) |

平台路径（[cursor-history README](https://github.com/S2thend/cursor-history)）：

| 平台 | Composer stack | Store stack |
| --- | --- | --- |
| macOS | `~/Library/Application Support/Cursor/User/` | `~/.cursor/` |
| Windows | `%APPDATA%/Cursor/User/` | `%USERPROFILE%\.cursor\` |
| Linux / WSL | `~/.config/Cursor/User/` | `~/.cursor/` |

Linux 路径 `~/.config/Cursor/User/globalStorage/state.vscdb` 另有独立实测佐证：
[lgsn.dev「Cursor globalStorage bloat」](https://lgsn.dev/cursordiskkv) 给出
`sqlite> .tables` → `ItemTable  cursorDiskKV`，`SELECT COUNT(*) FROM cursorDiskKV` → 50023，
并明确「`cursorDiskKV` seems to hold cursor chat logs」。

### 2.3 Composer stack 的 schema（`cursorDiskKV`）

**表结构**：`cursorDiskKV(key TEXT, value BLOB)`，外加
`ItemTable`（VS Code 设置）与较新版本才有的 `composerHeaders`。
来源：[cursor-state-vscdb AGENT_CONTEXT.md](https://github.com/Xuzhen-Li/cursor-state-vscdb/blob/main/docs/AGENT_CONTEXT.md)
（原文：`SQLite tables: ItemTable, cursorDiskKV, composerHeaders`；`cursorDiskKV: key TEXT, value BLOB`）、
[cursor-history composer-sqlite.ts](https://github.com/S2thend/cursor-history/blob/main/src/core/composer-sqlite.ts)
（`type ComposerKeyValueTable = 'ItemTable' | 'cursorDiskKV'`）。

**键前缀**（[cursor-history 数据报告](https://github.com/S2thend/cursor-history/blob/main/docs/cursor_history_message_data_structure_analysis_report.md)）：

| 前缀 | 用途 | 本适配器 |
| --- | --- | --- |
| `bubbleId:<composerId>:<bubbleId>` | 单条消息内容 | **采集** |
| `composerData:<composerId>` | 会话元数据（name/createdAt/workspaceIdentifier） | **只取元数据**，不取正文 |
| `checkpointId:<composerId>:<checkpointId>` | 文件快照（撤销/重做） | 跳过 |
| `agentKv:blob:<hash>` | Agent 上下文 blob | 跳过 |
| `codeBlockDiff:*` / `inlineDiffs-*` | 代码 diff 状态 | 跳过 |

**bubble 正文**（[同报告](https://github.com/S2thend/cursor-history/blob/main/docs/cursor_history_message_data_structure_analysis_report.md)）：

```json
{"_v": 3, "bubbleId": "a05c2488-...", "type": 1, "text": "User message content...",
 "richText": "{...}", "isAgentic": true, "tokenCount": {...}, "checkpointId": "...",
 "toolFormerData": {...}}
```

- `type`：**1 = user，2 = assistant**。来源：报告「Conversation Header Items」表 +
  [cursor-history storage.ts `mapBubbleToMessage`](https://github.com/S2thend/cursor-history/blob/main/src/core/storage.ts)
  （`role: bubbleType === 2 ? 'assistant' : 'user'`）+ [掘金「Cursor 的 state.vscdb 解析踩坑记」](https://juejin.cn/post/7640063917021167625)
  （`type: 1, text: "你好"` / `type: 2, text: "你好！有什么..."`）。
- `_v`：schema 版本，目前主流 `3`；**未知版本只警告不崩溃**（掘金文同节）。
- 空 `text` 的 assistant bubble 是**流式中间态**：「Cursor 在 AI 回复过程中会不断创建新的
  bubble，先把空壳写进去，再逐步填充内容」（掘金文「坑 5」）。→ 这直接决定了游标设计，见 §2.6。
- `text` 是纯文本正文；`richText` / `toolFormerData` / `allThinkingBlocks` 不是记忆。
- `allThinkingBlocks` 每块字段不统一（有的用 `thinking`，有的用 `text`，旧版还有顶层 `thinking`）（掘金文「坑 7」）→ 一律不采。

**workspace 侧**：`ItemTable` 里 `composer.composerData` → `{"allComposers":[{composerId, createdAt, ...}]}`，
只提供「这个项目有哪些会话」，不含正文（掘金文「坑 1」+ [cursor-history storage.ts](https://github.com/S2thend/cursor-history/blob/main/src/core/storage.ts)）。
本项目**不采**它：正文已经在 global 库里，多读一个库只会多一份不一致风险。

**时间戳**：版本相关。新版（≥2025-09）每条 bubble 有 `createdAt`（ISO 字符串）；
旧版只有 assistant 消息有 `timingInfo.clientRpcSendTime`（Unix 毫秒），user 消息无时间戳。
来源：[cursor-history 兼容性报告「timestamp」节](https://github.com/S2thend/cursor-history/blob/main/docs/cursor_unified_data_model_compatibility_report.md)
（实测 4,592 条 bubble：`createdAt` 61.7%、`timingInfo` 16.6%、任一可用 73.1%）。
本适配器两者都读，读不到就用 `utc_now()` 兜底（与 Gemini/Pi 一致）。

**性能警示**：真实 `cursorDiskKV` 可以有 **273 万行 / 52 GB**，其中 74.29% 是 `bubbleId`。
来源：[cursor-state-vscdb 案例](https://github.com/Xuzhen-Li/cursor-state-vscdb)。
→ 本适配器每轮有界（`max_rows`/`max_messages`），绝不 `SELECT *` 全表。

### 2.4 Store stack（Agent / CLI）

- **transcript JSONL**：`~/.cursor/projects/<project>/agent-transcripts/<uuid>/<uuid>.jsonl`，
  每行一个对象，形如
  `{"role":"user","message":{"content":[{"type":"text","text":"..."}]}}`，
  assistant 行可含 `{"type":"tool_use","name":"Read","input":{...}}`。
  来源：[cursor-history 测试 fixture](https://github.com/S2thend/cursor-history/blob/main/tests/fixtures/store-root/projects/d-1-yuyu-proj-cursor-history/agent-transcripts/aaaaaaaa-0000-0000-0000-000000000001/aaaaaaaa-0000-0000-0000-000000000001.jsonl)
  + [transcript.ts `mapLine`](https://github.com/S2thend/cursor-history/blob/main/src/core/store-stack/transcript.ts)
  （`role` 必须是 `user`/`assistant`，`message.content` 必须是数组，只投影 `type==='text'`，`tool_use` 单独归类）。
  → **本适配器采集这一套**，与 `transcripts.py` 的 Claude/Codex 读取逻辑同构。
- **`store.db`（protobuf DAG）**：`~/.cursor/chats/<md5(cwd)>/<uuid>/store.db`，内含
  `meta`/`blobs` 两表，正文是 protobuf 叶子节点（`0x0a` = message leaf，`0x12` = subtree），
  需要 Merkle 递归解码。来源：[paths.ts](https://github.com/S2thend/cursor-history/blob/main/src/core/store-stack/paths.ts)、
  [store-db.ts](https://github.com/S2thend/cursor-history/blob/main/src/core/store-stack/store-db.ts)。
  → **本适配器不解析**，只在 probe 里如实报告 `protobuf_store_db_not_parsed`。
  写一个 protobuf DAG 解码器却没有任何真实样本可验证，正是「猜测式解析」——
  宁可显式报告不支持，也不把解码失败伪装成 0 条。

### 2.5 查不到 / 不能声称的部分（诚实清单）

以下内容**没有**找到可靠公开来源，因此**没有**实现，也没有写进代码：

1. **Cursor 官方文档**从未公开本地聊天库的 schema；Cursor 官方文档站只在
   「Export chat」等 UI 层面提及历史，不描述 `state.vscdb`。→ 上表全部是社区逆向，非官方。
2. `cursorDiskKV` 是否有**加密**形态：未找到任何来源说明。→ 适配器把无法解码的值
   计为 `corrupt` 而不是崩溃（`test_corrupt_and_null_payloads_are_counted_not_fatal`）。
3. `composerData` 中项目路径的**稳定字段名**：不同来源给出 `workspaceIdentifier.uri.fsPath`
   与 `workspaceUri` 两种（[storage.ts](https://github.com/S2thend/cursor-history/blob/main/src/core/storage.ts) 里两种都出现过），
   本适配器**两个都试**，都取不到就把 `project` 留空——不猜。
4. 新版 Cursor 是否已把部分会话**只放服务端**：社区提到「部分模式历史只在服务端」，
   但没找到可引用的官方说明。→ 文档不把它当结论，只当**风险**：若本地采不到，
   `capture-status` 会显示 `detected` 但 `inserted` 长期为 0，属可观测现象。

### 2.6 已实现的适配器（`hippomind/cursor_capture.py`）

接口与 `gemini_capture.py` / `pi_capture.py` 一致（`Scanner.sync(...) -> dict`），并额外
提供 daemon 用的单一入口 `collect(ledger, ...)` 与只读自检 `probe()` / `status()`。

**三态契约（核心）**：

| state | 含义 | 触发条件 |
| --- | --- | --- |
| `detected` | 找到已知 schema 的源 | `cursorDiskKV` 可读且有 `rowid`，或 transcript JSONL 可识别 |
| `not_installed` | 没有任何 Cursor 数据目录 | 三个平台的 `User` 目录 + `~/.cursor/{projects,chats,acp-sessions}` 全不存在 |
| `unrecognized` | **有 Cursor 数据，但没有已知 schema 匹配** | 目录存在、可读，但表/键/行格式都不认识 |

`unrecognized` **不是错误也不是 0 条**：它会出现在 `present_but_unrecognized` 里并附
具体原因（`no_cursorDiskKV_table` / `no_rowid_on_cursorDiskKV` / `no_composer_keys` …），
并由 `capture-status` 打印。这正是任务要求的「不能静默返回 0 条冒充成功」。

**采集语义**：

- bubble → `kind=raw_chat`、`source_agent=cursor`、`scope=agent:cursor`、`raw_chat=1`；
  `session_id` = `composerId`；`project` = `composerData` 的 workspace 目录名；
  evidence `cursor:bubble:<composerId>:<bubbleId>`；metadata 含 database / composer_id /
  bubble_id / bubble_type / bubble_schema_version(`_v`) / rowid / session_name / format。
- transcript 行 → 同上，evidence `cursor:transcript:<uuid>:<行号>`，`session_id` = transcript 目录 UUID，
  `project` = `projects/` 下那一级目录名。只取 `type=="text"` 的 block。
- **跳过**：`type` 非 1/2 的 bubble、`thinking`/`tool_use` block、空 assistant 壳（见下）、
  `checkpointId`/`agentKv`/`codeBlockDiff` 行、HippoMind 提炼器提示词。
- **流式空壳 + 游标**：因为 assistant bubble 是「先写空壳、原地填充」，纯 `rowid > high-water`
  会只采到空壳。所以每轮在 watermark 之后**额外重读最后 `RECENT_RESCAN_ROWS`=200 行**，
  填充后即可采到；重复部分靠账本的 content_hash 去重（免费）。重读量单独计为 `rescan`，
  **不计入 `messages`**，避免出现幻觉式消息计数。
- **幂等**：游标 `transcript:cursor:<sha1(db)[:16]>`（composer，JSON `{rowid,updated_at,database}`）
  与 `transcript:cursor:jsonl:<sha1(path)[:16]>`（transcript，JSON `{offset,line,updated_at,path}`）。
- **坏数据**：非 JSON / NULL 值 → 计入 `corrupt`；超 `MAX_VALUE_BYTES`(2 MB) 的 value → 计入
  `oversized`；非普通文件/符号链接/只读打开失败 → 计入 `errors`；单库异常不阻塞其它库。
- **只读**：`sqlite3.connect("file:...?mode=ro", uri=True)`，从不 `CREATE/UPDATE/VACUUM`。
- **有界**：每轮 `max_rows`=400 行 / `max_messages`=200 条；transcript 每文件 2 MB、40 条；
  每批 ingest ≤ 1,000 条。
- **根目录可覆盖**：`HIPPOMIND_CURSOR_ROOTS`（Composer `User` 目录）与
  `HIPPOMIND_CURSOR_STORE_ROOTS`（`~/.cursor`），`os.pathsep` 分隔，便于测试/多机。

### 2.7 验证（合成 fixture，非真实 Cursor）

`python3 -m unittest tests.test_cursor_capture -v` → **24 项 OK**。fixture 由测试自己用
`sqlite3` 建表灌数据，**不需要真装 Cursor**：

| 覆盖点 | 用例 |
| --- | --- |
| 三态：未安装 | `test_not_installed_when_no_cursor_directory_exists`、`test_collect_reports_not_installed_without_writing` |
| 三态：未识别（≠0 条） | `test_unrecognized_when_cursor_data_has_no_known_schema`、`test_unrecognized_schema_reports_unrecognized_not_zero`、`test_collect_reports_unrecognized_and_never_claims_success` |
| 三态：已识别 | `test_detected_when_cursordiskkv_and_bubbles_exist`、`test_probe_detects_transcript_schema` |
| `~/.cursor` 只有 mcp.json 不算装了 | `test_dot_cursor_without_cursor_subdirs_is_not_installed` |
| 坏库不崩 | `test_locked_database_is_reported_not_crashed` |
| user/assistant 映射 + 跳过 tool/checkpoint/agentKv | `test_captures_user_and_assistant_text` |
| 时间戳双来源 | `test_timestamps_come_from_createdat_and_timing_info` |
| 流式空壳填充后能采到 | `test_streaming_empty_bubble_is_captured_after_it_is_filled` |
| 幂等（连跑两次不重复） | `test_second_sync_does_not_duplicate`、`test_collect_captures_both_stacks_and_is_idempotent` |
| 追加消息续采 | `test_appended_bubbles_are_picked_up` |
| 坏值/空值计数不崩 | `test_corrupt_and_null_payloads_are_counted_not_fatal` |
| 每轮上限不丢数据 | `test_max_messages_bound_stops_the_cycle` |
| **积压必须能被逐轮排空**（见下） | `test_backlog_drains_completely_across_cycles` |
| transcript 只取 text block | `test_captures_only_text_blocks` |
| transcript 半行等待 | `test_partial_trailing_line_waits_for_completion` |
| transcript 坏行跳过 | `test_bad_lines_are_skipped_and_counted_as_absence` |
| 自检命令 | `test_status_reports_probe_and_cursors_without_writing` |

**实现过程中被合成测试抓到的真实缺陷（已修复并锁定）**：

初版游标把「本轮读到的最大 rowid」当作水位。当积压大于每轮消息预算（`max_messages`）时，超出预算的 bubble 会被水位直接跳过、**永久漏采**；表面上靠重叠窗口重读看起来「还有产出」，实际是在反复重读同一段尾巴而不是排空队列。修复为**只按本轮真正消费掉的行推进水位**，并新增 `test_backlog_drains_completely_across_cycles`（120 条积压 / 每轮 25 条 → 必须 120 条全部入库且无重复）。这正是「合成 fixture 证明采集逻辑本身是对的」的价值：它抓到了一个用真实数据也可能很难察觉的静默丢数据缺陷。

**未验证的限制（必须原样理解）**：

1. **本机未安装 Cursor，未做端到端实测**——没有一条真实 Cursor 数据被读过。
2. 真实 `cursorDiskKV` 的 `value` 究竟是 `TEXT` 还是 `BLOB`、是否压缩/加密、
   `bubbleId` 是否一定带 `composerId` 前缀，全部来自社区资料，未经本机证实。
3. `_v` 版本演进（3 → 4+）后字段是否改名未知；适配器遇到不认识的 `type` 只会计入
   `skipped`，不会误入库，但也可能因此漏采。
4. `store.db`（protobuf DAG）**未实现**，CLI/Agent 侧的这部分会话采不到。
5. 服务端-only 会话本地天然不可达（§2.5 第 4 条）。

### 2.8 自检命令（装好 Cursor 后怎么确认）

```bash
python3 -m hippomind capture-status           # 人类可读
python3 -m hippomind capture-status --format json
```

当前本机输出（节选，2026-10-24）：

```
claude+codex   detected
               files: {'claude': 432, 'codex': 135}
gemini         detected
pi             detected
               files: 136
cursor         not_installed
               note: 未发现 Cursor 数据目录（应用未安装，或安装后从未运行过）
```

装好 Cursor 并跑过一次对话后再执行：出现 `cursor  detected` 且 `recognized ≥ 1` 即表示
schema 被识别，daemon 下一轮采集会自动生效；若显示 `unrecognized`，则按
`present_but_unrecognized` 里的 `reason` 回来更新本文件的 §2.3/§2.4。

---

## 3. 剩余缺口

1. **Cursor 未做端到端实测**（本机无安装）。合成 fixture 只能证明「按文档 schema 写的解析是对的」，
   不能证明「真实 Cursor 就是那个 schema」。装好后用 `capture-status` 复核。
2. **Cursor `store.db`（protobuf DAG）未实现**：CLI/Agent 侧会话采不到，probe 如实报告。
3. **Pi 跨窗口删除不可回撤**：已入库 raw 行不会因后续墓碑删除；append-only 语义下只能靠
   raw 保留期/容量维护淘汰。
4. **Pi 历史回填速度**：默认每轮 40 行/文件、2 MB/文件、200 条全局；342 MB 历史会在多轮中渐进回填。
5. **Compaction 摘要截断 8,000 字符**（原文中位 13,269）；需要全文时可回读源 JSONL。
6. **daemon 接入由 Lead 完成**（本任务约束）：见 §4。

---

## 4. daemon 接入说明（给 Lead，照此改动即可）

本任务约定**不修改** `hippomind/daemon.py`（生产 daemon 正在跑它）。接入只需三处：

1. **导入**（与 `gemini_capture`/`pi_capture` 并列，约 `daemon.py:38-39`）：
   ```python
   from .cursor_capture import collect as collect_cursor
   ```
2. **采集循环**（`start_capture_loop`，在 `summary["pi"] = ...` 那段之后、`self.health["capture"] = summary` 之前）：
   ```python
   try:
       summary["cursor"] = collect_cursor(self.ledger, max_messages=100)
   except Exception as cursor_exc:
       summary["cursor"] = {"error": "%s: %s" % (type(cursor_exc).__name__, str(cursor_exc)[:200])}
   ```
   （`collect()` 自带 probe + 三态 + 内部异常隔离，即使不加 try 也不会抛，但外层 try 与本文件风格一致。）
3. **health 字段**：无需新增结构——`/stats.health.capture.cursor` 即上一步的 `summary["cursor"]`，
   其 `state` 字段严格取 `detected` / `not_installed` / `unrecognized` 之一，
   另有 `composer` / `transcripts` 两个子块和 `messages/inserted/duplicate/errors` 汇总。

本机当前预期：`state == "not_installed"`、`messages == 0`、`errors == 0`、每轮 ~0 ms
（只做一次 `isdir` 探测），对现有采集无影响。
