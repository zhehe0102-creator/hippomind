# 本地 Agent 采集覆盖：Cursor / Pi / local-agent（2026-10-23）

本文件回答一个具体问题：**除了 Claude Code、Codex、Gemini/Antigravity、DSH 之外，
本机还有哪些 Agent 留下了可增量采集的持久会话存储？** 结论先行：

| Agent | 本机持久存储 | 采集状态 |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` | 已覆盖（`hippomind/transcripts.py`） |
| Codex CLI | `~/.codex/sessions/**/*.jsonl` | 已覆盖（`hippomind/transcripts.py`） |
| Gemini / Antigravity | `~/.gemini/antigravity*/conversation_summaries.db` | 已覆盖（`hippomind/gemini_capture.py`） |
| **Pi（pi / PiDeck）** | **`~/.pi/agent/sessions/**/*.jsonl`** | **本次新增 `hippomind/pi_capture.py`** |
| **Cursor** | **不存在（本机未安装）** | **无适配器；见下方证据与补齐条件** |
| `~/.local-agent` | 只有 `AGENTS.md`（HippoMind 注入块） | 无会话存储，无需采集 |
| Agent Bridge | `~/.agent-bridge/transcripts/sess_*.jsonl` | 仅 agy/Antigravity 会话，非 Cursor/Pi；可作未来来源 |

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
  这是 Pi 最接近“会话级摘要”的东西，与 Gemini summary 的用法一致。
- **跳过**：`thinking` / `toolCall` / `image` block、`toolResult` 与 `system` 角色、
  `custom` / `custom_message`（todo 计划、后台通知等 harness 注入）、
  `context_edit`、`model_change`、`thinking_level_change`、HippoMind 提炼器提示词。
- **删除**：同一读取窗口内出现 `deleted(originalEntryId=<message id>)` 时跳过该消息。
  实测本机 222 条墓碑全部指向**已从文件物理删除**的记录（墓碑 id 与现存消息 id 交集为 0），
  所以这是防御性逻辑；跨窗口“先入库、后删除”无法回撤（append-only 账本），
  由 raw 保留期/容量维护自然淘汰，见剩余缺口。
- **中断安全**：只消费以 `\n` 结尾的完整行；写者半行时游标不前进，下一轮重读。
- **超大行**：单行 > 8 MiB 无法解析，跳过该行并继续后续记录（不整文件放弃）；
  > 64 MiB 的病理行会置 `resync` 标记，下一轮先丢弃到行尾再继续。
- **损坏行**：单行 JSON 解析失败只跳过该行。

### 1.3 增量状态与边界

- `source_cursors` 键：`transcript:pi:<sha1(path)[:16]>`，值为 JSON：
  `{offset, line, size, session_id, cwd, session_name, parent_session, updated_at, resync?}`。
  会话头在更早的 chunk 时，session 身份从游标恢复；游标缺身份时用 `_head_context`
  只读文件首行补齐。
- 边界：每文件每轮最多读 2 MB、最多 40 行/40 条记录；每轮全局最多 200 条；
  单条消息截断 4,000 字符，摘要截断 8,000 字符；每批 ingest ≤ 1,000 条
  （与 `SQLiteLedger.ingest_many` 上限一致）。
- 文件按 mtime **从新到旧**扫描：活跃会话先采，历史 342 MB 积压在后台渐进回填。
- 文件被截断/替换（size < offset）时从头重扫；符号链接与非普通文件忽略。
- 根目录可用 `HIPPOMIND_PI_SESSION_ROOTS`（`os.pathsep` 分隔）覆盖，便于测试/多机。

### 1.4 验证

- `python3 -m unittest tests.test_pi_capture -v` → **12 项 OK**：采集/跳过、
  墓碑删除、增量、追加消息保持会话身份、半行等待、坏行、批量上限续采、
  超大行跳过、多轮 resync 超大行、无 session 游标时头部恢复、新文件优先、status、环境变量覆盖。
- 真实数据只读验证（临时账本、不改 `~/.pi`）：
  - 691 KB 含 compaction 的会话 → 34 条 raw + 1 条 task 摘要，0 错误；
  - 43 KB 含 7 条墓碑的会话 → 正常采集，0 泄漏；
  - 2.3 MB 含超大行的会话（临时把阈值降到 1 MB）→ 跳过 1 条超大行后仍采到 2 条 raw，0 错误；
  - 全库冒烟：136 文件、5 轮、197 条入库、0 错误，约 35 ms/轮（每轮 40 条）。
- 未修改 `hippomind/daemon.py`、`hippomind/transcripts.py`、`hippomind/gemini_capture.py`；
  daemon 接入由 Lead 完成（建议与 `GeminiSummaryScanner` 并列实例化，
  在 capture loop 调 `self.pi_capture.sync(max_messages=100)`，并把结果放进
  `health.capture["pi"]`）。

## 2. Cursor：本机不存在持久会话存储

### 2.1 证据（2026-10-23）

- `/Applications` 无 Cursor.app（全量列出：Antigravity/CodeBuddy/Qoder/OpenCode/
  DeepSeek Harness 等，无 Cursor）。
- `~/.cursor` 只有 3 个文件，全部是 HippoMind 自己写入的接入配置，无聊天数据：
  - `mcp.json`（`agent_install` 注册的 hippomind MCP server）
  - `rules/hippomind-memory-hub.mdc`
  - `skills/agent-bridge/SKILL.md`
- `~/Library/Application Support` 下无 `Cursor` 目录；`mdfind` 查询 Cursor 的
  bundle id（`com.todesktop.230313mzl4w4u92`）无结果；家目录内无 `state.vscdb`
  属于 Cursor（唯一 `state.vscdb` 在 `~/.vscode-shared/`，属 VS Code 共享存储）。
- 无 `cursor` / `cursor-agent` CLI（PATH 与 `~/.local/bin` 均无），无
  `~/.cursor-server`、`~/.cursor-agent`、`~/.local/share/cursor-agent`。
- `~/.local-agent` 只有 `AGENTS.md`（HippoMind auto-context 注入块），无会话存储。

结论：**本机没有可采集的 Cursor 持久会话数据，因此不发布未经真实数据验证的
cursor_capture.py。** 对未知 JSON schema 做猜测式解析会把垃圾写进共享账本，
比不采集更糟。

### 2.2 若日后安装/使用 Cursor，需要什么

Cursor 的本地聊天记录属于 VS Code 系存储（不同版本 schema 会变，必须在真实安装上
先探测再实现）：

1. 候选位置：
   - `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
   - `~/Library/Application Support/Cursor/User/workspaceStorage/<hash>/state.vscdb`
   - Linux：`~/.config/Cursor/User/...`；Windows：`%APPDATA%/Cursor/User/...`
2. 候选表/键：`ItemTable(key,value)`（旧版 chatdata JSON）与
   `cursorDiskKV(key,value)`（新版 `bubbleId:<composerId>:<bubbleId>`、
   `composerData:<composerId>`）。这些是社区已知形态，**本机无法验证**，
   需要先写只读 probe：确认表、键前缀、bubble 的 type/text 字段与 composer 元数据。
3. 实现约束（与 Pi/Gemini adapter 对齐）：只读 SQLite URI；每库游标
   （rowid/key + 内容指纹，避免 Cursor 原地更新 bubble 时漏采或重复）；
   只取 user/assistant 文本；单轮有界；`source_cursors` 记录
   `transcript:cursor:<sha1(db)>[:16]`；解析失败只记 error 不阻塞其它源。
4. 注意：Cursor 部分模式的对话历史只在服务端，本地无副本；这种情况本地采集
   天然不可达，只能依赖 MCP/规则注入（现有 `provider_manifest` 已覆盖）。

## 3. 剩余缺口

1. **Cursor adapter 未实现**：阻塞于本机无 Cursor 安装/无真实数据；补齐条件见 §2.2。
2. **Pi 跨窗口删除不可回撤**：已入库 raw 行不会因后续墓碑删除；append-only 语义下
   只能靠 raw 保留期/容量维护淘汰（与 Claude/Codex 现状一致）。
3. **Pi 历史回填速度**：默认每轮 40 行/文件、2 MB/文件、200 条全局；342 MB 历史
   会在多轮中渐进回填。若需一次性回填，可另写离线脚本（本次未发布，避免误伤 daemon）。
4. **Compaction 摘要截断 8,000 字符**（原文中位 13,269）；超出部分不进记忆，
   需要全文时可回读源 JSONL。
5. **daemon 接入未做**（按任务约束由 Lead 完成）。
