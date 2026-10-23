# GUI 全面重构：HippoMind Atlas（记忆图志）

> 状态：实现 v1.0（已验收）· 2026-10-23
> 本文件是团队协作的唯一契约。旧 GUI（hippomind/web 的 Observatory 设计与
> matplotlib 工作台）的视觉与信息架构被废弃；旧实现只保留可直接复用的能力。

## 0. 一句话

把 HippoMind 的记忆账本做成一座可读、可查、可核对的档案馆：默认浅色纸本、
编辑式排版、单一朱砂信号色、数据密度高但不拥挤；键盘优先；十个功能区覆盖
「脉搏 → 记忆库 → 检索 → 上下文 → 图谱 → 事实 → 年轮 → 端点 → 体检 → 参考」。

## 1. 目标与非目标

目标：

1. 新的 Web 控制台由 daemon 直接服务（/ui，同源、零构建、零第三方依赖、ES Modules）。
2. 覆盖记忆系统全部可读能力：总览、记忆浏览/详情、检索、上下文包、实体图谱、
   claims/冲突/事实/技能、时间轴、Agent/游标/接入、健康体检、API 参考与设置。
3. 视觉语言与旧设计无任何延续：无玻璃拟态、无极光渐变、无紫色霓虹、无大圆角卡片。
   新语言 = 纸本 + 墨色 + 朱砂 + 索引编号 + 细线表格 + 衬线标题 + 等宽数据。
4. 浅色为默认（paper），深色为可选（night），同一套 token 切换。
5. 所有视图在 1440×900 与 1280×800 下完整可用；≤1080px 收窄导航；≤760px 单列。
6. 可验证：每个视图都有 seed 数据截图；前端无 console error；Python 测试覆盖
   API/静态服务；文档同步。

非目标（本轮）：

- 不做写操作大扩展。仅允许：置顶/取消置顶、反馈（+/-）。遗忘/裁决等危险操作
  只展示可复制的 CLI 命令，不提供按钮直接执行。
- 不改动 daemon 的 loopback-only/token/ACL 信任模型。GUI 使用管理员令牌（本机 token）。
- 不引入前端构建链（无 npm/bundler）、不引入图表库、不引入 Web 字体下载。
- 不删除旧 matplotlib 演示 GUI（--gui/--network 保留），但新 Web 控制台是
  文档里的主界面；start-hippomind.command 增加 --web 入口。

## 2. 视觉语言（精确 token）

### 2.1 色板

浅色（默认，data-theme="paper"）：

| token | 值 | 用途 |
|---|---|---|
| --paper | #F2EEE4 | 页面底色（暖纸） |
| --paper-2 | #E9E3D5 | 侧栏/次级底 |
| --surface | #FBF8F0 | 卡片/表面 |
| --surface-2 | #F5F1E6 | hover/次级表面 |
| --ink | #1A1916 | 主文字 |
| --ink-2 | #4D483D | 次级文字 |
| --ink-3 | #7C7466 | 弱化文字 |
| --ink-4 | #A89F8C | 极弱/占位 |
| --rule | #D9D1BF | 细线 |
| --rule-strong | #BCB29C | 强调线 |
| --accent | #C4471E | 朱砂：实时/关键信号/选中 |
| --accent-soft | #F0D9CC | 朱砂浅底 |
| --link | #1E43B2 | 墨蓝：链接/交互 |
| --link-soft | #D9E0F5 | 墨蓝浅底 |
| --ok | #2E6A4D | 通过/健康 |
| --warn | #9A640F | 警告 |
| --danger | #9E2626 | 错误/失败 |
| --highlight | #F1DEA6 | 搜索命中高亮 |
| --shadow | 26 25 22 | 阴影基色（用 rgb(var(--shadow) / .08)） |

深色（data-theme="night"）：

| token | 值 |
|---|---|
| --paper | #14120E |
| --paper-2 | #1B1812 |
| --surface | #1F1B14 |
| --surface-2 | #26211A |
| --ink | #F2EBD9 |
| --ink-2 | #CCC2AC |
| --ink-3 | #968B74 |
| --ink-4 | #6D6452 |
| --rule | #332D22 |
| --rule-strong | #4D4433 |
| --accent | #F06B3A |
| --accent-soft | #3A2418 |
| --link | #8FA8FF |
| --link-soft | #222B4A |
| --ok | #6FBF97 |
| --warn | #E0A74C |
| --danger | #E07070 |
| --highlight | #4A3A17 |
| --shadow | 0 0 0 |

语义色只用于状态/数据，不做装饰大色块。

### 2.2 字体

    --font-serif: "Songti SC","STSong","Noto Serif SC","Iowan Old Style",Georgia,"Times New Roman",serif;
    --font-sans: -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Helvetica Neue",Arial,sans-serif;
    --font-mono: "SF Mono","SFMono-Regular",Menlo,"JetBrains Mono",ui-monospace,monospace;

- 视图大标题：serif 34–40px / 1.15；卡片标题：serif 20px；正文：sans 14px/1.6。
- 数字与 ID：mono 12–13px，font-variant-numeric: tabular-nums。
- 小标签（eyebrow）：sans 10px，letter-spacing .16em，大写，--ink-3。
- 中文正文不使用 300 以下字重；衬线标题 500–600。

### 2.3 几何 / 节奏

- 间距基数 4：4/8/12/16/20/24/32/40/56/72。
- 圆角只允许 2px（控件）与 3px（卡片），禁止大圆角。
- 描边 1px --rule；分区用细线 + 留白，不用阴影卡片堆叠。
- 阴影仅用于：抽屉、命令面板、toast。
- 内容最大宽度 1560px，左右留白 40px（窄屏 24px）。
- 页面结构：左侧索引栏 248px（固定），顶部上下文栏 88px（sticky），
  底部状态栏 30px（固定），内容区独立滚动。
- 图标：内联 SVG 24×24 viewBox，stroke 1.5，currentColor；禁止 emoji。
- 动效：120–200ms，cubic-bezier(.2,.7,.3,1)；只做 opacity/transform；
  prefers-reduced-motion 时全部关闭。

### 2.4 品牌

- 名称：HippoMind Atlas；中文副题：记忆图志。
- 侧栏顶部标志：内联 SVG「海马回纹」——同心弧线 + 圆点，朱砂点。
- favicon.svg 同标志。

## 3. 信息架构（10 个视图）

导航顺序固定，编号即键盘快捷键（1–9，0=参考）：

| # | id | 中文 | 英文 eyebrow | 数据来源 |
|---|---|---|---|---|
| 01 | pulse | 脉搏 | PULSE | GET /api/v1/overview |
| 02 | archive | 记忆库 | ARCHIVE | GET /api/v1/memories |
| 03 | recall | 检索 | RECALL | POST /recall |
| 04 | compose | 上下文 | COMPOSE | POST /context-pack |
| 05 | graph | 图谱 | GRAPH | GET /api/v1/subjects + /api/v1/graph |
| 06 | facts | 事实 | FACTS | GET /api/v1/claims /conflicts /facts /skills |
| 07 | timeline | 年轮 | TIMELINE | GET /api/v1/timeline |
| 08 | agents | 端点 | AGENTS | GET /api/v1/agents |
| 09 | health | 体检 | HEALTH | GET /api/v1/health |
| 10 | reference | 参考 | REFERENCE | GET /api/v1/meta + 本地设置 |

每个视图必须实现：加载骨架、空态（有解释与下一步动作）、错误态（可重试、
401 时引导令牌）、正常态。数据刷新不整页闪烁。

### 3.1 pulse 脉搏

- 头条：当前活跃记忆大数字（serif 64px）+ 近 30 天增长 sparkline + 事件总数。
- 状态戳：daemon 在线、watcher、注入、FTS 漂移、WAL 体积。
- 左侧「构成」：kind 横向条（含 raw 占比说明）；「来源」：agent 横向条。
- 「最近入档」：6 条 catalog slip（kind/agent/时间/摘要），点击进 archive 详情。
- 「体检摘要」：三色 flags → 点击进入 health。
- 快捷动作：检索、组装上下文、复制 API 地址。

### 3.2 archive 记忆库

- 工具栏：关键词过滤（客户端提交 q）、kind/agent/lifecycle 下拉、项目选择、
  时间范围、raw 开关（默认排除 raw 证据并说明原因）、每页 25/50/100。
- 主表：时间、摘要（截断 2 行，保持原始空白）、kind、agent、lifecycle、weight、
  置顶标记。行点击 → 右侧详情抽屉。
- 详情抽屉：完整内容（可复制）、ID/来源/scope/project/task/session、时间线
  （created/updated/last_access/valid_until）、质量/权重/触碰、tags/evidence/metadata、
  claims、关系（父/子）、反馈记录；动作：置顶/取消、反馈 ±1、复制 ID、复制 CLI 命令。
- 底部分页 + 总数。

### 3.3 recall 检索

- 大号 serif 查询输入（Enter 检索）+ 高级选项（agent、kind、项目、时间、raw、
  require_evidence、limit）。
- 结果：序号、score 横条、kind/agent/lifecycle 徽标、摘要（高亮命中词）、
  证据数、时间；点击打开同一详情抽屉（与 archive 共用组件）。
- 展示耗时（前端计时）与「FTS + LIKE 双通道」说明；空态给建议词。
- 分页 offset/limit。

### 3.4 compose 上下文

- 左栏表单：任务描述 textarea；options：agent、scope、project、task_id、max_items
  （1–20）、max_tokens、max_chars、raw_chat 开关、require_evidence、as_of。
- 右栏：预算仪表（tokens/chars 使用率）、injection 状态、items 列表（task、结构化
  字段、证据 tags）、三种输出：结构化 / 纯文本 / XML（只读 textarea + 复制/下载）。
- debug 开关 → 显示 diagnostics（若接口返回）。
- 空态解释「Context Pack 默认要求证据、默认不含原始聊天」。

### 3.5 graph 图谱

- 左：主语检索输入 + recent subjects 列表（claims/conflicts 计数）。
- 中：Canvas 力导向图（自绘，无依赖）。节点=实体/记忆，边=关系；节点大小按
  claims/连接数；冲突实体朱砂描边；支持滚轮缩放、拖拽平移、点击选中、hover
  高亮邻域；右上角缩放/重置；图例。
- 右：选中实体详情（subject、claims 表、关联 memory 摘要列表，点击进详情）。
- 空态：说明需要 claims 数据，并给出 CLI 示例。

### 3.6 facts 事实

- Tabs：冲突 / Claims / 事实投影 / 技能。
- 冲突：state=conflict 的 claim 列表，显示 subject/predicate/slot/value、置信度、
  两个候选 memory 摘要，可复制 hippomind resolve 命令。
- Claims：全部状态表格，按 state 过滤，置信度条。
- 事实：subject/predicate/slot/value/confidence/source/asserted_at。
- 技能：procedural 卡片（trigger/preconditions/pipeline/verification），折叠展开。
- 每 tab 有计数徽标。

### 3.7 timeline 年轮

- 控制：30/90/365 天、按日/周、agent 过滤、kind 过滤、raw 开关。
- 主图：stacked bars（按 agent 分层），x=时间，y=入档数；hover tooltip；
  点击 bucket 下钻为「该时段记忆」表格（可跳 archive 带时间参数）。
- 副图：events/日 sparkline；总量/日均/峰值数字。
- 图例可点击显隐 agent 序列（至少实现 hover/点击选中）。

### 3.8 agents 端点

- 卡片区：watcher 状态、注入状态、发现扫描时间、自动安装结果、凭据数。
- 主表：agent、memories、active、events、last_seen、trust、credentials、cursors。
- 游标表：source_id、last_success、last_error（脱敏截断）、更新时间。
- 发现/安装状态 chips：detected / absent / already_installed / restricted。
- 「刷新发现」按钮 → POST /admin/reload（确认后执行，admin token）。

### 3.9 health 体检

- verdict 大字戳：READY / NEEDS ATTENTION（由 errors 数量）。
- 检查项网格：daemon、ledger、token、injection、privacy、FTS 同步、claims 一致性、
  storage/WAL、archive。每项 severity + detail + 建议命令（可复制）。
- 存储：DB bytes、WAL、freelist、reclaimable、archive files/bytes（meter）。
- 「运行完整性检查」按钮 → GET /api/v1/health?integrity=1，显示结果与耗时。
- 命令清单：checkup / maintain / doctor / backup（说明只读性）。

### 3.10 reference 参考

- Tab 接口：来自 /api/v1/meta 的 endpoints 表（method/path/说明/auth）。
- Tab 设置：主题、刷新间隔、每页条数、清空令牌、当前 ledger_id、API 地址复制。
- Tab 关于：版本、schema、启动时间、uptime、快捷键表、设计说明（指向 docs）。

## 4. 后端 API 契约（新增，全部要求管理员本地令牌）

统一：同源 JSON；请求头 X-HippoMind-Token；错误 {"error": "..."}；
所有 GET 只读；响应 < 512KB；limit 参数一律 clamp；所有字符串输出必须由前端转义。
新增路径前缀 /api/v1。现有 /recall、/context-pack、/feedback、/admin/reload、
/stats 保持兼容，GUI 直接复用（带 token）。

### 4.1 GET /api/v1/meta

    {"server_version":"1.0.0","api_version":"v1","schema_version":8,
     "ledger_id":"...","started_at":"...","uptime_s":123.4,"pid":77742,
     "limits":{"memories_page":[1,100],"content_preview":600,"graph_nodes":200},
     "endpoints":[{"method":"GET","path":"/api/v1/overview","summary":"...","auth":"token"}],
     "ui":{"name":"HippoMind Atlas","version":"1.0"}}

### 4.2 GET /api/v1/overview?days=30

    {"server":{"status":"ok","server_version":"1.0.0","watch_enabled":true,
      "ledger_id":"...","pid":77742,"started_at":"...","uptime_s":123.4},
     "ledger":{ ...原 /stats 的 ledger 字段... },
     "watcher":{...},"agent_discovery":{...},"auto_install":{...},
     "context_cache":{...},"daemon_health":{...},"wal_bytes":0,
     "growth":{"days":30,"buckets":[{"date":"2026-10-01","memories":12,"events":40}],
               "total_memories":123,"total_events":456},
     "recent":[PUBLIC_MEMORY_PREVIEW...],
     "health_flags":[{"id":"fts","label":"FTS 同步","severity":"ok|warn|error","detail":"..."}]}

PUBLIC_MEMORY_PREVIEW（列表统一形）：

    {"memory_id":"mem-...","kind":"progress","content":"≤600 字符",
     "source_agent":"codex","session_id":"","task_id":"","project":"","scope":"shared",
     "role":"","created_at":"ISO","updated_at":"ISO","lifecycle":"active",
     "quality_state":"accepted","raw_chat":false,"weight":0.5,"importance":5,
     "confidence":0.5,"utility":0.0,"touch_count":0,"pinned":false,"has_claims":false}

### 4.3 GET /api/v1/memories

参数：limit(1–100, 默认 50)、offset(0–100000)、kind、agent、lifecycle、project、
scope(默认 shared)、q(≤200 字符，LIKE)、pinned=1（只看置顶）、sort=updated|created|weight（默认 updated）、
order=desc|asc、from/to(ISO 前缀)、raw=exclude|include|only（默认 exclude）、
facets=1 时返回 facets。

    {"total":123,"offset":0,"limit":50,"items":[PUBLIC_MEMORY_PREVIEW...],
     "facets":{"kinds":[{"value":"progress","count":4663}],
               "agents":[{"value":"codex","count":...}],
               "lifecycles":[{"value":"active","count":...}],
               "projects":[{"value":"","count":...}]}}

### 4.4 GET /api/v1/memories/{memory_id}

    {"memory":{...完整行，evidence/tags/metadata 已解码，content ≤12000},
     "origin_event":{"event_id":"","event_type":"","occurred_at":"","ingested_at":"",
       "source_agent":"","session_id":"","task_id":"","project":"","role":"","scope":"",
       "content_preview":"≤400","metadata":{}},
     "claims":[{...}],
     "relations":{"parents":[{"relation_type":"caused_by","confidence":0.6,
        "memory":{...preview...}}],"children":[...]},
     "feedback":[{"feedback_id":"","signal":1,"note":"","created_at":"","source_agent":""}]}

### 4.5 GET /api/v1/timeline

参数：days(1–365, 默认 90)、bucket=day|week、agent、kind、raw=exclude|include（默认 exclude）。

    {"days":90,"bucket":"day","totals":{"memories":123,"events":456,"peak":12,
      "average":3.1},
     "buckets":[{"date":"2026-10-01","memories":12,"events":40,
       "agents":{"codex":7,"claude":5},"kinds":{"progress":8}}],
     "agents":[{"agent":"codex","total":70}]}

### 4.6 GET /api/v1/claims

参数：state=all|active|conflict|superseded（默认 all）、subject（可选，精确匹配）、limit(1–200)、
scope(默认 shared)。附 counts:{active,conflict,superseded,total}，items 中每条附
memory_preview（≤400，若无则空）。按 asserted_at 倒序。

### 4.7 GET /api/v1/conflicts

参数：limit(1–200)、scope。返回 {"scope","total","items":[...]}，
item 含 claim 字段 + memory content ≤120（沿用现有 /conflicts 语义）。

### 4.8 GET /api/v1/facts

参数：subject、limit(1–500)、scope；返回 {"items":[list_facts...]}。

### 4.9 GET /api/v1/skills

参数：limit(1–200)、scope；返回 {"items":[list_skills...]}。

### 4.10 GET /api/v1/subjects

参数：limit(1–500)、scope；返回 {"items":[{"subject","claims","conflicts"}]}。

### 4.11 GET /api/v1/graph

参数：subject(必填)、hops=1|2、limit(10–2000)、scope；
返回 ledger.entity_graph 原样（nodes/edges/subject）。

### 4.12 GET /api/v1/agents

    {"agents":[{"agent":"codex","memories":205945,"active":9000,"events":205945,
      "last_seen":"ISO","trust":0.5,"credentials":0}],
     "cursors":[{"source_id":"...","updated_at":"","last_success_at":"",
       "last_error":"","cursor_length":42}],
     "discovery":{...},"auto_install":{...},"watcher":{...},
     "injection":{"enabled":true,"agent":"all"},"credentials":7}

### 4.13 GET /api/v1/health

参数：integrity=0|1（默认 0；1 时执行 PRAGMA quick_check，可能耗时）。

    {"generated_at":"ISO","overview":{...health_overview...},
     "fts":{...fts_sync_report...},"claims":{...claims_consistency_report...},
     "storage":{"db_bytes":0,"wal_bytes":0,"freelist":0,"reclaimable_bytes":0,
       "archive_files":0,"archive_bytes":0,"retention_days":30},
     "daemon":{"status":"ok","uptime_s":0,"pid":0,"version":"","watch_enabled":true},
     "checks":[{"id":"ledger","label":"账本","severity":"ok|warn|error",
       "detail":"...","fix":{"label":"","command":""}}],
     "integrity":{"ran":false,"ok":null,"elapsed_ms":null}}

### 4.17 GET /api/v1/environment

运行环境只读快照：platform（python/executable/package_root）、workspace（卷类型/磁盘）、
daemon（host/port/ui_url/uptime）、ledger（大小/schema/journal/计数）、backups
（registry 路径/最近备份/存在性/年龄）、launchd（plist 路径与问题）、dependencies
（matplotlib/sqlite）、home（HIPPOMIND_HOME/runtime_dir）、checks（python/磁盘/备份/
登录项/日志模式等 severity + fix 命令）。用于体检页「运行环境」卡片。

### 4.18 GET /api/v1/events

结构化事件流：默认排除 raw_chat/tool_output/system_message/developer_message/
environment/command_output/approval；参数 limit(1–200)、type、agent、since。
返回 items（event_id/type/occurred_at/source_agent/session/task/project/role/scope/
content_preview≤240/metadata）与 counts 按类型聚合。用于脉搏页「最近活动」。

### 4.19 GET /api/v1/pins

参数 scope(默认 shared)、limit(1–500)；返回 total + items（PUBLIC_MEMORY_PREVIEW）。
记忆库的 pinned=1 过滤也复用同一置顶语义。

### 4.20 GET /api/v1/duplicates

参数 limit(1–100)、sample(200–20000)、min_similarity(0.5–1.0，默认 0.85)；
返回 duplicate_candidates 的 pairs/scanned/suggestions（只读，不合并不删除）。
体检页「重复候选」按钮按需调用。

### 4.14 POST /api/v1/pin

body {"memory_id":"...","pinned":true} → 200 {"memory_id","pinned","updated_at"}；
不存在 → 400；需管理员。

### 4.15 POST /api/v1/feedback

body {"memory_id":"...","signal":-1|0|1,"note":"≤400"} → 200 {"updated":true}。

### 4.16 UI 会话令牌（可选增强，P1）

- POST /api/v1/ui-token（token 认证）→ {"nonce":"...","expires_at":"ISO","url":"/ui/#/pulse?boot=..."}
- POST /api/v1/ui-token/exchange（无需 token）body {"nonce":"..."} → {"token":"..."}。
  nonce 一次性、60s TTL、最多 8 个待用、只存内存。
- CLI hippomind ui [--no-open]：读本地 token → mint nonce → 打开浏览器 → 打印 URL；
  daemon 不可达时报错并提示 hippomind start。

## 5. 静态服务（/ui）契约

- /ui → 302 到 /ui/（相对资源 URL 需要规范根）；/ui/ → hippomind/web/index.html；
  /ui/<path> → 根内文件；越界 403、缺失 404、目录 404。
- 允许的 MIME 由 mimetypes 推断；text/js/json/svg 追加 ; charset=utf-8。
- 响应头：Cache-Control: no-cache、X-Content-Type-Options: nosniff、
  Referrer-Policy: no-referrer、X-Frame-Options: DENY、
  Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
  img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none';
  form-action 'none'; frame-ancestors 'none'。
- HTML 中不得出现令牌/ledger 路径；页面只通过用户输入的令牌访问数据接口。
- 未知 /ui/* 资源不得泄漏目录列表。

## 6. 前端架构（零构建）

    hippomind/web/
      index.html                  # 壳骨架（首屏静态可读）
      favicon.svg
      css/tokens.css              # 色板/字体/间距/基础元素（shell）
      css/shell.css               # 布局：索引栏/上下文栏/状态栏/抽屉/命令面板
      css/components.css          # 按钮/表格/徽标/卡片/表单/meter/tabs/空态/骨架
      css/views-a.css             # views-a 的专属布局（views-a 队友）
      css/views-b.css             # views-b 的专属布局（views-b 队友）
      js/core/dom.js              # el/$/$$/delegate/clear/escape（Lead）
      js/core/format.js           # 数字/字节/时间/百分比/截断（Lead）
      js/core/api.js              # fetch 封装、token、错误（Lead）
      js/core/store.js            # 轻量状态+订阅+localStorage（Lead）
      js/core/router.js           # hash 路由 + query 解析（Lead）
      js/components.js            # 通用组件工厂（shell）
      js/charts.js                # SVG sparkline/bars/donut/stacked bars（shell）
      js/icons.js                 # 内联 SVG 图标集（shell）
      js/palette.js               # 命令面板（shell）
      js/connect.js               # token 引导覆盖层（shell）
      js/app.js                   # 引导/注册/轮询/状态栏（Lead）
      js/views/registry.js        # 视图清单（route id → import）（Lead）
      js/views/pulse.js           # views-a
      js/views/timeline.js        # views-a
      js/views/agents.js          # views-a
      js/views/health.js          # views-a
      js/views/reference.js       # views-a
      js/views/archive.js         # views-b
      js/views/recall.js          # views-b
      js/views/compose.js         # views-b
      js/views/graph.js           # views-b
      js/views/facts.js           # views-b
      js/components/inspector.js  # 记忆详情抽屉内容（views-b，archive/recall/timeline 共用）
      js/components/graph-canvas.js # 自绘力导向图（views-b）

### 6.1 视图模块契约

    export const meta = {
      id: "archive", index: "02", title: "记忆库", eyebrow: "ARCHIVE",
      subtitle: "账本中的全部结构化记忆",
    };

    // 由 app.js 在挂载时调用；返回元素或 Promise<元素>。
    // context: {
    //   container: HTMLElement,      // 视图容器（已清空），可直接填充
    //   params: URLSearchParams,     // 路由 query
    //   navigate(path),              // 切换视图（hash 路由）
    //   setQuery(obj),               // 改写当前视图 query（不重渲染）
    //   interval(fn, ms),            // 视图存活期间的定时器，卸载自动清理
    //   onCleanup(fn),
    //   signal: AbortSignal,         // 视图卸载时 abort，用于 fetch
    //   toast(msg, tone), confirm(opts) -> Promise<bool>, drawer: {...},
    //   refresh(),                   // 触发本视图重新加载（模块自定）
    // }
    export async function render(context) { /* ... */ }

视图不得修改 document.body 结构或全局监听（除 context 清理器）。所有动态文本
必须经 el() 的文本子节点或 esc()；禁止把接口字符串拼进 innerHTML。

### 6.2 核心模块 API

dom.js

- el(tag, attrs = {}, ...children) -> Element：attrs 支持 class/id/text/dataset/aria-*/style 对象、
  on:{event: fn}；children 支持节点、字符串、数组、null。
- $(sel, root?)、$$(sel, root?)、clear(node)、esc(str)、delegate(root, selector, type, fn)。

format.js

- fmtInt(n)、fmtCompact(n)（12.3k / 24.8万）、fmtBytes(n)、fmtPct(x, digits=1)、
  fmtMs(n)、relTime(iso)、absTime(iso, {seconds?})、shortId(id, head=8, tail=4)、
  truncate(text, n)、titleCase(s)、duration(seconds)。

api.js

- api.setToken(token)、api.getToken()、api.clearToken()、api.hasToken()
- api.get(path, params)、api.post(path, body)、api.text(path)
- class ApiError { status, detail, path }；网络错误 status=0；401 自动派发
  hippomind:unauthorized 事件（connect.js 监听）。
- token 键：localStorage hippomind.atlas.token。

store.js

- store.get(key)、store.set(key,value)、store.patch(obj)、store.subscribe(fn)，
  持久化键 hippomind.atlas.settings（主题/刷新间隔/每页条数）。

router.js

- router.start({views, mount, onChange})；hash 形如 #/archive?kind=decision；
  router.navigate(path, {replace})、router.parse(path) -> {id, params}、
  router.query(params) -> string。

### 6.3 通用组件 API（shell 提供，views 只依赖这些）

components.js（全部返回 Element，禁止 innerHTML 直插数据）：

- badge(text, {tone}) tone: neutral|accent|ok|warn|danger|link
- chip(text, {active, onClick, count})
- metric({label, value, unit, hint, tone, delta})
- card({eyebrow, title, actions, body, className})
- dataTable({columns, rows, empty, onRowClick, rowKey, dense})；
  column: {key, label, align, width, mono, format(row), render(row)}
- pager({offset, limit, total, onChange})
- emptyState({title, hint, action})
- skeleton(rows=3)、errorState({title, error, onRetry})
- meter({value, max, tone, label, valueText})
- copyButton(text, {label})（复制成功 toast）
- toast(message, {tone, ms})、confirm({title, body, confirmLabel, danger})、
  drawer.open({title, eyebrow, subtitle, content, actions})、drawer.close()
- tabs({items:[{id,label,count}], active, onChange})
- textField({label, value, placeholder, onInput, monospace})、selectField({label, options, value, onChange})
- toggleField({label, checked, onChange})、segmented({options, value, onChange})
- statRow([{label, value}])、rule()

charts.js：

- sparkline({values, width=220, height=44, tone, fill=true})
- barList({items:[{label, value}], total, tone, maxBars=10})
- donut({items:[{label,value,tone}], size=160, centerLabel, centerValue})
- stackedBars({buckets, series, width, height, onSelect, selected})
  buckets: [{key,label,total,parts:{seriesId:value}}]；返回带 hover tooltip 的 Element。
- legend({items:[{id,label,tone,active}], onToggle})

icons.js：icon(name, {size=16, title}) -> SVGElement；名称集合：
pulse, archive, recall, compose, graph, facts, timeline, agents, health, reference,
search, refresh, theme, copy, check, close, chevron-left, chevron-right, arrow-up,
arrow-down, pin, feedback-up, feedback-down, external, filter, plus, minus, warning,
error, info, menu。

### 6.4 壳层布局（index.html 固定 id）

    <div id="app" class="app">
      <aside id="rail" class="rail">...brand + <nav id="nav"> + footer...</aside>
      <div class="workspace">
        <header id="topbar" class="topbar">
          <div class="topbar-title"><div id="view-eyebrow"></div><h1 id="view-title"></h1>
            <p id="view-subtitle"></p></div>
          <div class="topbar-actions">
            <button id="search-trigger">搜索 ⌘K</button>
            <button id="refresh-btn">刷新 R</button>
            <button id="theme-btn">主题 T</button>
          </div>
        </header>
        <main id="view-root" class="view-root" tabindex="-1"></main>
        <footer id="statusbar" class="statusbar">…</footer>
      </div>
    </div>
    <div id="drawer-root"></div>
    <div id="palette-root"></div>
    <div id="toast-root"></div>
    <div id="connect-root"></div>

- 首屏在 JS 加载前也要有静态品牌/骨架，不闪白。
- 导航由 app.js 用 registry 生成（index.html 中 nav 为空占位）。
- 状态栏：在线点 + daemon 版本/pid + ledger 短 id + schema + 最近刷新时间 +
  快捷键提示；离线时朱砂点 + 「离线，点击重试」。

## 7. 质量门槛（验收）

1. python3 -m unittest discover -s tests -p 'test_gui*.py' 全绿（新增测试）。
2. python3 -m unittest discover -s tests 全绿（2026-10-23 实测 648 项）。
3. scripts/lint.sh exit 0。
4. 前端静态检查：index.html 引用的每个资源存在；每个 ES 模块可 node --check；
   无外链 http(s) 资源；无内联 <script>；无 innerHTML = <接口数据> 模式。
5. 截图 QA：seed 数据下 10 个视图 × paper/night 共 20 张 1440×900 PNG（output/gui/final/），
   无 console error、无 401、无布局溢出（documentElement.scrollWidth ≤ innerWidth）。
6. 真实生产库只读验证：新端点对 248k 生产账本实测 overview 0.53s / health 0.66s / agents 0.15s / memories 8ms（除 integrity=1），
   不写库、不触发维护；PRAGMA quick_check 保持 ok。
7. 安全：越界路径 403；/ui HTML 无 token/路径；所有写端点需管理员；XSS 用
   el()/文本节点（QA 用 <img src=x onerror=...> 种子内容验证不执行）。
8. 文档：README/MEMORY_HUB_GUIDE/FEATURES 增加 Web 控制台章节；FEATURES 子命令
   计数随 hippomind ui 更新；新增 docs/GUI_ATLAS.md（设计+API）。

## 8. 文件 Ownership（防写冲突）

| 范围 | 负责人 |
|---|---|
| docs/GUI_ATLAS.md、js/core/*、js/app.js、js/views/registry.js、daemon.py、gui_api.py、tests/test_gui_api.py、tests/test_gui_serving.py、cli 新命令、最终文档 | lead |
| index.html、favicon.svg、css/{tokens,shell,components}.css、js/components.js、js/charts.js、js/icons.js、js/palette.js、js/connect.js | shell 队友 |
| js/views/{pulse,timeline,agents,health,reference}.js、css/views-a.css | views-a 队友 |
| js/views/{archive,recall,compose,graph,facts}.js、js/components/{inspector,graph-canvas}.js、css/views-b.css | views-b 队友 |
| scripts/gui_seed.py、scripts/gui_shot.mjs、tests/test_gui_frontend.py、截图与 QA 报告 | qa 队友 |

约定：任何人不得修改他人范围内文件；跨范围需求发消息给 owner。集成由 lead 负责。


## 9. 实现与验收记录（2026-10-23）

### 9.1 交付物

- 后端：`hippomind/gui_api.py`（25 个端点目录、13 个 GET 读模型、pin/feedback 写操作、
  一次性 UI 引导令牌）；`hippomind/daemon.py` 的 /api/v1 路由、/ui 静态服务、
  /ui → /ui/ 规范跳转与 CSP/frame-ancestors/nosniff/referrer 头。
- 前端：`hippomind/web/`（index.html + 5 个 CSS + 24 个 ES Modules；零构建、零依赖）。
  十个视图全部实现：pulse / archive / recall / compose / graph / facts / timeline /
  agents / health / reference；记忆详情抽屉由 archive/recall 共用。
- CLI：`hippomind ui`（一次性 nonce，120s、单次有效，URL 经 fragment 传递）；
  `start-hippomind.command --web`。
- 文档：本文件、README、MEMORY_HUB_GUIDE、USAGE_GUIDE、FEATURES、ARCHITECTURE。
- QA：`scripts/gui_seed.py`（隔离 seed）、`scripts/gui_shot.mjs`（Chrome CDP 截图）、
  `scripts/gui_dev.py`（一键 seed→截图→清理）、`tests/test_gui_frontend.py`。

### 9.2 实测证据

| 门 | 结果 |
|---|---|
| 全量单测 | 648 项 OK（2026-10-23，含 GUI API 19 / serving 8 / CLI 4 / frontend 17） |
| lint | gate 90/90、legacy 254/254，exit 0（ruff 0.16.3） |
| 截图 | seed 720 条 × 10 视图 × 2 主题 = 20/20 PASS，0 console error / 0 HTTP≥400 / 0 溢出 / 0 XSS |
| 生产库截图 | 真实 248,523 事件 × 10 视图 = 10/10 PASS |
| 生产端点时延 | overview 0.50s、environment 0.14s、events 0.32s、pins 0.22s、duplicates 9ms、memories 8ms |
| 引导令牌 | 无 localStorage 打开 /ui#/pulse?boot=…：自动换 token、fragment 清理、无连接面板、数据正常 |
| 安全 | /ui HTML 无 token/路径；越界路径 400/403；CSP/frame DENY/nosniff；XSS seed 6 条全部按文本渲染 |
| 生产体检 | `hippomind doctor` READY（privacy/launchd 为已知 warning），daemon v1.0.0 pid 80795 |

### 9.3 已知边界

1. 生产账本当前 claims/relations 为空（历史采集主要写 raw_chat/progress），因此 graph/facts
   在真实库上展示空态；seed 数据（108 claims/64 relations/40 skills）用于证明非空路径。
2. GUI 只接受管理员本机 token；scoped agent 凭据访问 /api/v1/* 返回 403（有意）。
3. 唯一写操作是置顶/反馈；遗忘/裁决等仍只给可复制的 CLI 命令。
4. 旧 matplotlib 工作台保留（`--gui` / `--network`），但文档主界面已切换为 Web 控制台。
5. 工作区在外置卷时 launchd 开机自启仍受 macOS TCC 限制（既有平台限制，doctor 会提示）。


## 10. v1.1 增强（2026-10-23）

在 v1.0 的十视图基础上，本轮补的是「可运维 + 可写入 + 可发现」：

- **活动流**：新增 `GET /api/v1/events`（隐私过滤后的结构化事件），脉搏页新增
  「最近活动」卡片，按事件类型/来源展示最近 8 条并聚合类型计数。
- **运行环境**：新增 `GET /api/v1/environment`，体检页新增「运行环境」卡片
  （Python/平台/工作区卷/磁盘/依赖/最近备份/登录项/daemon URL/运行目录）与
  「重复候选」按需扫描（`/api/v1/duplicates`）。
- **置顶与导出**：新增 `GET /api/v1/pins`，记忆库支持 pinned=1 过滤；记忆库与
  事实页支持当前页 CSV 导出（客户端生成，不经服务端）。
- **事实写入**：事实页新增「记录事实」表单（subject/predicate/slot_key/value/
  evidence/confidence/content），通过既有 `POST /ingest` 写入一条带证据的 claim，
  成功后立即出现在事实/图谱视图。这是控制台唯一可直接执行的写操作；source_agent
  固定为 `atlas-gui`，scope 为 shared，evidence 必填。
- **命令面板**：输入任意关键词时追加「在记忆中搜索…」「在记忆库中过滤…」两个动态
  命令；新增 `?` 帮助抽屉（快捷键/设计说明）；命令面板新增「快捷键与帮助」。
- **主题**：参考页新增「跟随系统」，app.js 监听 `prefers-color-scheme` 实时切换；
  T 键仍在纸本/夜档间切换。
- **系统环境**：新增 `scripts/bootstrap-env.sh`（默认只检查；`--create` 在
  exFAT 上以 --copies 创建 .venv 并安装 requirements），start 脚本优先使用 .venv。

验收证据（2026-10-23 第二轮）：全量单测 648 项 OK；lint gate 90/90；seed 数据
20/20 截图 PASS；生产库 10/10 截图 PASS；新增 API 测试 4 项、E2E 写入表单验证
（AtlasE2E claim 写入后 `/api/v1/claims?subject=AtlasE2E` 返回 1 条 active）；
生产 environment 端点 < 0.3s、events < 5ms、pins/duplicates < 5ms。
