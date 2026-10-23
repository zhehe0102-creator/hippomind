# HippoMind 本机 Memory Hub 使用与运维指南

## 产品形态

v0.8 将原有的 Grid World 海马体演示保留为独立实验，并新增长期运行的本机 Memory Hub：

```text
Codex / Pi / Claude / Cursor / Gemini / 本地 Agent
                  |
          CLI / localhost HTTP / MCP stdio
                  |
    Context Pack <- SQLite FTS5 + 结构化过滤 + MMR
                  |
      SQLite/WAL 事件账本 (事实来源，0600 权限)
                  |
Agent-Chat-Archive watcher (低频增量游标，原始聊天只作证据)
```

默认数据目录是 `~/Library/Application Support/HippoMind/`：

- `hippomind.sqlite`：SQLite/WAL 事实账本。
- `hippomindd.json`：本机 daemon 状态，不包含 token。
- `hippomindd.token`：0600 的 localhost API token。
- `hippomindd.log`：后台服务日志。

设置 `HIPPOMIND_HOME` 可将这些文件放到其他目录。旧的 `hippocampus_memory.json` 从不被启动程序覆盖；它只能由显式 `import` 读取，或由旧的实验 GUI 继续使用。

## 一键开启

在项目目录运行：

```bash
./start-hippomind.command
```

最简单的方式是双击工作区根目录的 **`HippoMind Atlas.app`**（或
`打开 HippoMind Atlas.command`）：脚本会启动/复用 daemon、申请一次性令牌并打开
Web 控制台。首次被 macOS 拦截时右键应用 →「打开」。

它会启动或复用 daemon 和 watcher，但不会打开 GUI。按需打开中文层状网络：

```bash
./start-hippomind.command --gui
```

推荐直接打开 Web 控制台（HippoMind Atlas）：

```bash
./start-hippomind.command --web
# 或
python3 -m hippomind ui
```

重复运行不会创建第二个服务。也可以直接使用 CLI：

```bash
python3 -m hippomind init
python3 -m hippomind start
python3 -m hippomind status
python3 -m hippomind stop
```

可选登录启动只在用户明确要求时安装：

```bash
python3 -m hippomind login-install
python3 -m hippomind login-remove
```

`login-install` 写入当前用户的 `~/Library/LaunchAgents/com.hippomind.daemon.plist`，不会修改系统级服务。

## 最小 Context Pack

### 显式按需接续

一次性安装所有本机已支持 Agent 的显式记忆接入：

```bash
python3 -m hippomind install-agents --agent all
```

macOS 下直接双击 `start-hippomind.command` 也会先启动或复用 daemon，再幂等刷新同一套接入。需要历史时，在普通用户文本中加入 `记忆：` 或 `记忆:`，例如“继续验收。记忆：检查 daemon API 的并发限制”。标记可在自然语言任意位置，查询取标记之后的内容。

安装器会为 Codex 注册并验证 UserPromptSubmit 原生 Hook，为 DeepSeek Harness（DSH）部署 Cordis 原生插件（见下节），并把 MCP server 注册到 Pi 的 `~/.pi/agent/mcp.json`、Claude、Cursor、Gemini 的实际用户配置；同时在 Pi、Claude、Cursor、Gemini、DeepSeek 与 local Agent 的规则位置插入并维护带标记的 HippoMind 区块，保留其他用户规则。`~/.agents/mcp.json` 还提供受管的通用 MCP 入口，遵循该标准的新 Agent 可在安装后直接发现 Hub。重复运行幂等。Codex Hook、DSH 插件和规则回退都会先检查本轮普通用户文本的标记；没有标记时，不读取账本、不启动 daemon、不注入历史。引用行、代码块、行内代码与空标记同样不触发。触发后才从标记之后的查询、任务线索和工作目录软线索构建最小 Context Pack。

首次安装或规则升级后，需要重载相应 Agent 的规则或新建任务；后台服务不能追溯改写一个已经开启的模型上下文。对于完全未知、未配置的 Agent 客户端，本机服务只生成适配报告，绝不强行注入。自定义 Agent 可使用 [AGENTS.md.template](integrations/AGENTS.md.template)、[pi-memory-hook.md](integrations/pi-memory-hook.md) 或 [AGENT_MEMORY_PROTOCOL.md](integrations/AGENT_MEMORY_PROTOCOL.md)。

当前发布契约：自动、MCP 与 DSH 注入均最多 8 项、默认 6000 token，另有 16000 字符运输上限；raw chat 仍默认不注入。旧版段落中的 5000 字符仅作历史说明。

需要人工查看预取结果时才运行：

```bash
printf '%s\n' "继续验收。记忆：检查 SQLite WAL 的并发事务" | python3 -m hippomind auto-context --stdin --trigger-mode anywhere --cwd "$PWD" --format text
```

自动路径的默认约束不可放宽：`max_items=8`、`max_chars=16000`（运输字符硬上限）、`max_tokens=6000`（默认 token 预算）、`raw_chat=false`、`require_evidence=true`。token 预算覆盖代码、英文和特殊符号的估算差异；当两者冲突时，16000 字符上限优先。先按 `task/project/source/time/scope` 精确过滤，再做关键词、FTS5（含结构化 context 字段）、文本相关性、时间、触碰、重要性、效用和置信度混合评分。之后按同一任务合并，再用 MMR 去重与多样性重排；同任务的最新状态字段先占用预算。v4 账本还记录 `valid_from/valid_until/asserted_at`，以 Slot-Value claims 处理事实替代，并以 `memory_relations` 保留有界因果父节点。`procedural` 记忆只保存声明式步骤与机器断言，不执行记忆正文中的命令。非空结果被包在固定兼容外层 `<MEMORY_DATA_UNTRUSTED>` 和动态 nonce 内层中，正文 XML 字符先转义；空结果的 `--debug` 给出候选、过滤原因和证据来源。MCP Resource 模板为 `hippomind://projects/{name}/active-context`。可用 `hippomind inject on|off|toggle|status --agent <agent>` 暂停或恢复注入，采集和维护不受影响。

若从较早版本升级，且旧账本可能已收录原始转录，请先停止 daemon，再运行 `python3 -m hippomind scan-raw` 检查聚合暴露计数，然后运行 `python3 -m hippomind redact-raw`。该维护命令会从原始聊天证据、事件投影和 FTS 索引中删除已识别的凭据与邮箱，并将每条原始证据限制为 4000 字符；完成后再启动 daemon。`python3 -m hippomind maintain --raw-retention-days 30 --raw-capacity 5000` 会将过期或冷 raw 证据脱敏后压缩为私有 JSONL 归档，并按归档字节上限清理最旧压缩件；`python3 -m hippomind recovery-check` 执行不替换在线账本的 WAL 备份/完整性恢复演练。16000 字符是按需注入给 Agent 的 Context Pack 运输上限，不会放宽原始归档的安全上限。

`--agent all` 是真正的全局注入开关，会清除旧的单 Agent 覆盖；需要例外时，先执行全局 `on`，再对指定 Agent 执行 `off`。关闭只影响聊天前的 Context Pack，不影响后台采集和维护。

跨电脑迁移请使用 `python3 -m hippomind export portable.json` 与目标机器上的 `python3 -m hippomind import portable.json`。portable export 仅含结构化证据事件，默认排除 raw/tool/system/developer、环境与命令输出事件、watcher 游标、daemon 状态、token 和 Agent 凭据；完整安装和恢复步骤见 [跨电脑部署](docs/PORTABILITY.md)。

### 使用建议：三种最省心的入口

- **请求内标记**：日常使用 `记忆：<要回忆的问题>`。它是默认建议，准确、可见、不会让普通请求出现意外历史。
- **MCP Resource 选择器**：支持 Resources 的宿主可挂载 `hippomind://projects/{name}/active-context`，适合先浏览项目当前状态、再决定是否带入对话。
- **诊断与开关**：`hippomind context <query> --debug` 显示命中、过滤与证据；`hippomind inject off --agent <agent>` 立即禁止下一轮注入。

`记忆：`比仅凭自动猜测更可靠：它把调用权留给用户，又保留在一句自然语言请求中完成检索的便利。

输出只有任务、目标、当前进度、关键决定、相关文件、阻塞、下一步、少量用户偏好和证据。没有可靠命中时返回空项目和“未找到有证据且相关的记忆”，不会用低相关旧聊天填满预算。解释选择与过滤原因：

```bash
python3 -m hippomind context "Memory Hub" --debug
```

## 写入结构化事实

所有长期可复用事实都带来源、作用域和证据：

```bash
python3 -m hippomind ingest \
  --content "SQLite/WAL 账本与 Context Pack 已完成" \
  --kind progress --agent codex --session run-42 \
  --task memory-hub --project hippocampus-agent \
  --evidence task:memory-hub
```

可选 `kind`：`task`、`progress`、`decision`、`file`、`blocker`、`next_step`、`preference`、`result`、`semantic`、`fact`。复杂输入可通过 `--json` 或 `--file` 传入，支持字段 `context.goal`、`progress`、`key_decisions`、`related_files`、`blockers`、`next_steps`、`preferences`。

账本把不可变 `ledger_events` 与可变 `memories` 投影分开保存。每条原始记录都有 `source_agent`、`session_id`、`task_id`、`project`、`role`、`occurred_at`、`scope`、`evidence`、`content` 和唯一 `content_hash`。重复采集同一记录只返回同一 id，不新增记忆。

## Agent 接入

常规接入使用 `python3 -m hippomind install-agents --agent all`；状态检查不会泄露 token：`python3 -m hippomind agent-status --agent all`。Codex 的 `native_context_hook.runtime` 应显示 `ready=true`、`trust_status=trusted` 和 `enabled=true`，此时不需要规则中的 shell 预取。Pi 的最小接入步骤见 [pi-memory-hook.md](integrations/pi-memory-hook.md)。其他 Agent 按 [AGENT_MEMORY_PROTOCOL.md](integrations/AGENT_MEMORY_PROTOCOL.md) 写入和读取。

### DeepSeek Harness（DSH）原生插件

`install-agents` 检测到 `~/.dsh` 时会把 Cordis 插件部署到 `~/.dsh/plugins/dsh-hippomind/`，并在 `~/.dsh/profiles/web/package.json` 中按官方 bundle 契约登记 `link:` 依赖、bundle 行与 `node_modules/@dsh-external/dsh-hippomind` 符号链接（首次修改前自动留 `package.json.bak.hippomind` 备份，其他插件条目不动）。插件源码位于 [integrations/dsh-plugin/](integrations/dsh-plugin/)，零第三方依赖，行为：

- **请求前注入**：`agent/pre-step` 用本轮真实用户要求检索 daemon `/context-pack`，把最多 8 项、6000 token、16000 字符运输上限的有证据结果以 `<MEMORY_DATA_UNTRUSTED>` 包裹追加在消息尾（两项都是按需上限；近距离、静态说明文本保持缓存命中）；子代理不注入，空结果不注入。注入开关关闭时只返回空包。
- **会话结束写回**：监听 `session/event` 采集 user/assistant 文本与事件序号；会话闲置超时（默认 120s）或插件卸载时，把增量写回为结构化 `progress` / `decision` / `result` 记录，evidence 指向 `dsh-session://<id>#seq=a-b`。
- **运行证明与凭据保护**：token 只从 0600 的 agent config 读取（patch 行只含路径引用），日志永不输出 token；配置权限过宽时插件自禁用。Cordis 真正调用 `apply()` 后会写一条只在 `agent:deepseek` scope 可见的健康探针，不进入共享召回；`agent-status --agent deepseek` 的 `runtime_probe.seen=true` 表示真实运行时已装载，而不只是文件部署完成。
- **失败降级**：daemon 不可达时进入 30s 静默退避，决策原样放行；插件任何内部异常都只降级为警告，不影响 DSH 启动与请求。`~/.dsh` 不存在或 profile 无法装配时，安装器自动降级为纯规则接入并在 `agent-status` 中说明原因。

DSH 重启（或经 super-injector 热注入）后插件生效。验证：`python3 -m hippomind agent-status --agent deepseek` 的 `native_dsh_plugin.installed=true` 且 `runtime_probe.seen=true`；后者证明 Cordis 宿主实际执行过插件。若 DSH 早于 daemon 启动，插件会在下一次成功预取后补发该私有探针。

### Provider Manifest 与自动发现

`hippomind/provider_manifest.py` 维护已知 Agent 的声明式清单（检测标记、接入分级顺序、规则路径）。接入分级：`native_hook`（Codex）> `native_plugin`（DSH）> `mcp`（Pi 的 `pi-mcp-adapter`、Claude、Cursor、Gemini 和通用 `~/.agents/mcp.json`）> `rules`（所有已支持 Agent 的兜底）。Pi 缺少 `pi-mcp-adapter` 时状态会明确报错并自动退回规则层。原生层级要求对应运行时已安装，否则降级为规则接入。

```bash
python3 -m hippomind discover-agents        # 立即扫描一次并输出报告
python3 -m hippomind install-agents --auto  # 只为本机检测到的运行时安装
```

daemon 内置低频发现循环（默认每小时最多一次，结果持久化在 runtime 目录的 `agent-discovery.json`，`/stats` 的 `agent_discovery` 字段可查）。**每次发现后 daemon 会执行受控自动安装**：只触碰 manifest 中已登记且本机检出证据齐全的 Agent——下载新 Agent 后无需手工操作，下一个发现周期即自动接入；安装后逐项核验（凭据、规则、原生 Hook/插件、真实 MCP 注册及 stdio 握手），失败自动修复一次，结果记录在 `/stats` 的 `auto_install` 字段。**未识别的 Agent 运行时只报告不修改**：发现结果会列出目录与指令文件签名（如 AGENTS.md/CLAUDE.md），确认后手工加入 manifest 才会接入。

Python 集成也可直接使用 `MemoryHubAgentAdapter`，统一执行 begin/checkpoint/decision/blocker/complete/feedback 生命周期；该适配器不会自行调用 LLM，也不会默认请求 raw chat。

MCP 接入分两层：**真实注册**与**便携 profile**。安装器会把 `hippomind` stdio 服务器直接写入 Pi adapter 的 `~/.pi/agent/mcp.json`、Claude（`~/.claude.json` 顶层 `mcpServers`）、Cursor（`~/.cursor/mcp.json`）、Gemini（`~/.gemini/settings.json`）以及可移植的 `~/.agents/mcp.json`。写入只增改我们自己的键，外部键原样保留，无法解析的文件拒绝改写；`agent-status` 会对注册命令做实际 initialize + tools/list 握手，返回 `runtime.ready` 与工具数，Pi 额外核验 adapter 包已启用。便携 profile 仍生成在 [integrations/hippomind-mcp.json](integrations/hippomind-mcp.json)，供不读取标准路径的客户端手工导入：

```bash
python3 -m hippomind.mcp
```

MCP 提供 `hippomind_context_pack`、`hippomind_ingest`、`hippomind_feedback`、`hippomind_stats`、`hippomind_injection` 和 `hippomind_sync`。支持 MCP 工具调用的 Agent 可在当前对话中调用 `hippomind_injection`，传入 `action=on|off|toggle|status` 控制下一轮上下文注入；普通 Agent 只能修改自己，管理员才可指定 `all` 或其他 Agent。切换不停止 daemon、watcher、归档、巩固和降权。它只走本机 SQLite，默认不会输出原始聊天。

## HTTP API

daemon 仅监听 `127.0.0.1:8765`。`/health` 可匿名检查；其余端点必须带本机 token：

```bash
TOKEN="$(cat "$HOME/Library/Application Support/HippoMind/hippomindd.token")"
curl -sS http://127.0.0.1:8765/health
curl -sS -X POST http://127.0.0.1:8765/context-pack \
  -H "Content-Type: application/json" -H "X-HippoMind-Token: $TOKEN" \
  -d '{"query":"继续 Memory Hub","options":{"max_items":8,"max_chars":16000,"max_tokens":6000}}'
```

端点：`/health`、`/ingest`、`/recall`、`/context-pack`、`/feedback`、`/sync`、`/stats`、`/shutdown`；控制台额外提供 `GET /ui`（静态、同源、无令牌）与 `GET /api/v1/*`（只读数据面，管理员令牌）及 `POST /api/v1/pin`、`/api/v1/feedback`、`/api/v1/ui-token`。请求上限 1 MiB，响应上限 512 KiB，并发上限 16。`/ingest` 每批最多 128 条，并在一个 SQLite 事务中提交。

## Watcher 与生命周期

watcher 默认每 30 秒读取可访问的 `Agent-Chat-Archive`：

- `index/index.sqlite` 的消息按 SQLite `rowid` 游标读取；user、assistant、tool 都可保存为原始证据。
- `tasks/tasks.jsonl` 增量导入任务、状态和最近 notes 作为结构化任务事实。
- 已知 `results.jsonl` 文件导入为 `result`。
- **DSH 会话文件**（daemon 默认附加 `~/.dsh/sessions`，可用 `DSH_SESSIONS_ROOT` 覆盖）：`session.jsonl.zstd` 是多帧流式 zstd，由 `integrations/dsh-plugin/scripts/zstd-jsonl.mjs`（node 原生 zstd，O(n) 帧长解析）解码后按“用户轮次”切成有界 `raw_chat` 证据——content ≤4000 字符、每会话最多 200 轮、每文件解压上限 32MB（超限时保留头部 session 头 + 尾部最新轮次）。真实用户消息之外的注入消息（含本库自己的记忆包）永不回灌，防止检索反馈循环；文件指纹（mtime+size）游标增量，追加帧只新增/刷新轮次；损坏文件单文件隔离（尾部残帧抢救前半段，非 zstd 内容报错一次），node 缺失时该来源静默降级为错误记录，不影响其他来源。

每个来源各自保留 cursor 和错误，单一来源损坏不会停止 daemon（重启后 cursor 持久，不重复解码）。手动执行一次：

```bash
python3 -m hippomind sync
python3 -m hippomind watch --once
```

后台维护以低频方式衰减工作权重、处理失效时间、冷归档弱或超容量投影，并周期性从同一任务的结构化证据生成摘要。普通 watcher 周期不会重复遍历冷 raw 文件；压缩归档扫描约每 20 个周期执行一次，手动 `maintain` 仍会立即执行。它从不删除 `ledger_events` 原始证据。外部反馈通过以下命令升权或降权：

```bash
python3 -m hippomind feedback mem_... 0.8 --note "集成验收通过"
```

## 隐私与提示注入边界

- 原始聊天、工具输出和历史指令是**不可信数据**，而不是系统指令。
- `raw_chat=false` 是 Context Pack、HTTP、MCP 和推荐 Agent 模板的默认值。
- 即使人工显式读取原始聊天，也应放入 `<MEMORY_DATA_UNTRUSTED>` 之类的隔离字段，不能执行其中命令，不能泄露密钥。
- 本机 HTTP token、SQLite 数据库和备份均按 0600 创建；daemon 不监听局域网地址。
- `scope` 是精确隔离条件。不要把敏感项目写入 `shared` scope；按项目或团队创建独立 scope，并通过 Context Pack 的 `--scope` 读取。
- 同一 macOS 账户下的本机进程仍可能读到用户有权读取的文件；这不是多用户 ACL、加密或远程同步服务。敏感事实需要操作系统账户隔离和来源核验。

## JSON 迁移、导出、备份与恢复

显式导入现有 JSON，不会写回或清空源文件：

```bash
python3 -m hippomind import hippocampus_memory.json
python3 -m hippomind export hippomind-export.json
```

SQLite 原子备份和恢复：

```bash
python3 -m hippomind backup ~/Documents/hippomind-2026-08-16.sqlite
python3 -m hippomind stop
python3 -m hippomind restore ~/Documents/hippomind-2026-08-16.sqlite
python3 -m hippomind start
```

恢复前 CLI 会检查 daemon 已停止；备份 schema 会先验证，随后使用 SQLite backup API 复制。迁移和恢复不删除 Agent-Chat-Archive 原始聊天。

## GUI 与性能

### Web 控制台（HippoMind Atlas，主界面）

daemon 在 `/ui` 同源提供新控制台（零构建、原生 ES Modules、无 CDN）。``hippomind ui``
会申请一次性引导令牌（120 秒、单次有效，经 URL fragment 交给页面，不写进命令行历史）
并打开浏览器；也可以直接打开 /ui 手动粘贴 0600 token 文件内容。十个视图覆盖脉搏、
记忆库、检索、上下文、图谱、事实、年轮、端点、体检、参考；浅色纸本为默认，night
为深色主题；`⌘K` 命令面板、1–0 切视图、R 刷新、T 主题。

数据面是 `GET /api/v1/*`：管理员令牌限定、分页有界、内容截断、GET 只读；写操作
是置顶/反馈，以及事实页的「记录事实」（经 `POST /ingest` 写入带证据 claim）。
v1.1 增加结构化活动流、运行环境卡片（Python/卷/磁盘/依赖/备份/登录项）、重复候选
只读扫描、置顶过滤与 CSV 导出、命令面板直接检索、主题跟随系统。静态服务只读 `hippomind/web/` 且带 CSP、frame-ancestors、nosniff 头，
页面不含 token 或账本路径。设计与接口契约见 [docs/GUI_ATLAS.md](docs/GUI_ATLAS.md)。

### 环境自检与依赖

`scripts/bootstrap-env.sh` 默认只检查 Python/sqlite 与是否已有 .venv；
`scripts/bootstrap-env.sh --create` 会在 exFAT 上用 --copies 创建 .venv 并安装
requirements.txt（需要网络）。`start-hippomind.command` 会优先使用 .venv/bin/python。

### 旧版 matplotlib 工作台

`--gui` 只启动一个独立 matplotlib 进程。全屏六层网络保留简体中文、稳定二次贝塞尔曲线、节点/边预算、边采样、缩放和视口裁剪。它每约 300 帧才检查一次 daemon 状态；按 `H` 显示最多 4 项、900 字符的 Context Pack。GUI 不参与 watcher、账本写入或 Grid World 学习路径，因此不会改变实验结果。

`--one-click` 默认不读取或写回旧的 `hippocampus_memory.json`；若确实要运行旧演示快照，才显式传入 `--persist-legacy-json`。这避免一次普通可视化检查覆盖用户已有 JSON。

空闲 daemon 只等待 HTTP 请求和低频 watcher tick。SQLite 使用 WAL、busy timeout 和批量 `BEGIN IMMEDIATE` 事务；FTS 候选集被严格限量，GUI 只在按需时加载 matplotlib。

## 验证

```bash
# 原有 18 项和 Memory Hub 新增测试
python3 -m unittest discover -s tests -v

# 语法检查
python3 -m py_compile hippomind/*.py

# 固定 Grid World 回归；指定临时 JSON，绝不写入用户快照
python3 -m hippomind.main --mode headless --steps 3000 --seed 42 \
  --memory /tmp/hippomind-seed42.json --no-persist

# 可重复的短时并发/WAL/Context Pack 压力等价测试
python3 tests/memory_hub_stress.py --seconds 10 --writers 6 --batch-size 32
```

将 `--seconds` 设为 `86400` 即可作为 24 小时 soak 脚本。它不读取真实归档或用户记忆，结束时会验证重开后的事件数、WAL 模式和 Context Pack 预算。

## 排障

| 症状 | 检查与处理 |
|---|---|
| `hippomindd 未运行或不可达` | 运行 `python3 -m hippomind status`，再运行 `start`；查看 `hippomindd.log`。 |
| token 失效或 401 | 确认使用同一 `--runtime-dir`；停止服务后删除仅该目录的 `hippomindd.token`，再 `start`。 |
| watcher 未导入内容 | `python3 -m hippomind sync --archive /path/to/Agent-Chat-Archive`；用 `/stats` 查看 source cursor/error。 |
| Context Pack 为空 | 先检查 `scope`、task/project/source 过滤和证据字段；使用 `--debug`，不要改用无关原始聊天。 |
| SQLite 忙 | 等待短暂重试；不要用多个写入器反复删除 `.wal` 或 `.shm` 文件。 |
| GUI 无法打开 | daemon 不受影响；安装 `requirements.txt` 中的 matplotlib，再运行 `start-hippomind.command --gui`。 |
| 需要回滚 | 停 daemon，使用最近 `backup`；JSON 导出可用于审计和可移植检查。 |

## 已知边界

- 标准库实现使用 FTS5、结构化过滤和现有文本相似度；不默认下载或运行向量模型。
- watcher 读取已知 Archive schema 和文件，其他 Agent 只要按通用协议写入 CLI/HTTP/MCP 即可接入。
- 账本保证本机 SQLite 事务一致性，不替代跨主机共识、端到端加密或人工事实核验。
