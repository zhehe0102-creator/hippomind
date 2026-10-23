# HippoMind Atlas — GUI 全面重构报告

> 2026-10-23 · Lead + 4 个专项子智能体 · 工作区 `/Volumes/移动硬盘/记忆系统重构`
> 实现提交：`da6c2de`（feat(gui): rebuild the console as HippoMind Atlas）
> 设计/接口契约：`docs/GUI_ATLAS.md`

## 一、本轮性质

用户要求「继续重构整个记忆系统的 GUI，进行全面重新设计，抛弃之前的全部设计」。本轮因此
不是给旧界面换皮，而是**整体替换**：

- 删除上一版 Observatory Web GUI（深色玻璃、极光渐变、紫色霓虹、6 个视图）。
- 新建 **HippoMind Atlas（记忆图志）**：纸本档案馆视觉语言、10 个视图、完整的
  `/api/v1` 只读数据面、零构建 ES Modules、一次性令牌登录。
- 旧 matplotlib 工作台（`--gui` / `--network`）保留为「旧版演示」，不再作为文档
  主界面；一键启动新增 `--web`。

## 二、设计

### 2.1 视觉语言

- 浅色「纸本」为默认：`--paper #F2EEE4`、墨色 `#1A1916`、朱砂信号 `#C4471E`、
  墨蓝交互 `#1E43B2`；night 深色为可选主题。
- 衬线大标题（Songti SC/Georgia）+ 等宽数据（SF Mono/Menlo）+ 细线表格 + 索引编号；
  2–3px 圆角，禁止玻璃拟态、渐变霓虹、大圆角、emoji、外链字体。
- 侧栏 248px 编号导航（01–10）、顶部上下文栏、独立滚动内容区、底部 30px 状态栏；
  ≤1080px 收窄、≤760px 单列；prefers-reduced-motion 全关动效。

### 2.2 信息架构（10 视图）

| # | 视图 | 能力 |
|---|---|---|
| 01 | 脉搏 | 活跃记忆/事件/总量、30 天 sparkline、kind/agent 构成、最近入档、健康 flags |
| 02 | 记忆库 | 结构化记忆分页、kind/agent/lifecycle/project/时间/raw 过滤、排序、详情抽屉 |
| 03 | 检索 | FTS+LIKE 召回、过滤器、命中高亮、评分条、证据展开、分页 |
| 04 | 上下文 | Context Pack 表单、预算 meters、injection 状态、结构化/文本/XML 输出 |
| 05 | 图谱 | claim 主语索引、Canvas 力导向图、缩放/平移/选中、实体 claims 侧栏 |
| 06 | 事实 | 冲突/claims/事实投影/技能四页签、可复制的 resolve 命令 |
| 07 | 年轮 | 30/90/365 天、日/周堆叠柱、事件 sparkline、时段下钻 |
| 08 | 端点 | Agent 表、信任/凭据、游标表、发现/自动安装状态、刷新发现 |
| 09 | 体检 | READY verdict、8 项检查、存储 meters、按需 quick_check、维护命令 |
| 10 | 参考 | 接口目录、主题/刷新/分页设置、版本与快捷键 |

交互：`⌘K` 命令面板、1–0 切换、R 刷新、T 主题、Esc 关闭抽屉/面板；详情抽屉
支持置顶、反馈 ±1、复制 ID/CLI 命令；所有危险操作（遗忘/裁决）只提供可复制命令。

## 三、架构

### 3.1 前端

- `hippomind/web/`：index.html + 5 个 CSS + 24 个 ES Modules，无 npm、无 CDN、
  无图表库；图表为手写 SVG（sparkline/bar/donut/stacked bars），图谱为手写 Canvas
  力导向（同步 240 tick 布局，无残留动画循环）。
- 核心层：`js/core/{dom,format,api,store,router}.js`；组件层：`components.js`、
  `charts.js`、`icons.js`、`palette.js`、`connect.js`；视图层 10 个模块 +
  共用 `components/inspector.js` 与 `components/graph-canvas.js`。
- 所有接口字符串只经文本节点/`el()` 进入 DOM；无 `innerHTML` 拼接数据；XSS seed
  6 条全部按文本显示（QA 断言 `window.__XSS__` 未被设置）。

### 3.2 后端

- `hippomind/gui_api.py`：25 个端点目录，13 个 GET 读模型 + pin/feedback 两个审计写
  操作 + 一次性 UI 令牌。管理员令牌限定；分页/limit/offset 全部 clamp；内容截断
  （列表 600、详情 12000）；SQL 参数化。
- `GET /api/v1/overview` 首屏合并 stats/growth/recent/health flags；`memories` 支持
  全维度过滤 + facets；`memories/<id>` 返回来源事件、claims、父子关系、反馈。
- 静态服务：`/ui` → 302 `/ui/`；只读 `hippomind/web/` 内文件；CSP
  `default-src 'self'`、frame-ancestors none、nosniff、no-referrer、no-cache。
- CLI：`hippomind ui` 向 daemon 申请 120 秒单次 nonce，打开
  `/ui/#/pulse?boot=…`；页面通过 fragment 换 token 并清理 URL，token 不进命令行
  历史/服务端日志。`start-hippomind.command --web` 直接使用。

### 3.3 打包

- `pyproject.toml` 的 package-data 增加 `hippomind = ["web/**/*"]`；
  MANIFEST.in 增加 `recursive-include hippomind/web *`，避免 wheel 安装后 /ui 404。

## 四、交付与验证

### 4.1 测试

| 门 | 结果 |
|---|---|
| 全量单测 | **644 项 OK**（2026-10-23 实测，122s） |
| 新增 GUI 测试 | API 15 + serving 8 + CLI 4 + frontend 17 = 44 项 |
| CLI 表面守卫 | 38 个子命令（新增 ui）的 argparse 摘要/handler 全部锁定 |
| lint | gate **90/90**、legacy **254/254**，exit 0（ruff 0.16.3） |
| 前端静态检查 | 资源存在/无内联脚本/无外链/node 语法/模块图/组件导出/CSP 关键点 |

### 4.2 截图 QA

- seed 数据（`scripts/gui_seed.py`：720 memories、108 claims、4 conflicts、
  64 relations、40 skills、8 agents、90 天分布、6 条 XSS 载荷）：
  **20/20 PASS**（10 视图 × paper/night），0 console error、0 HTTP≥400、
  0 横向溢出、0 XSS 执行、0 卡骨架。截图：`output/gui/final/`。
- 交互状态：检索结果、Context Pack、记忆详情抽屉、命令面板各 1 张，
  0 error / 0 溢出。截图：`output/gui/interactions/`。
- 真实生产库（248,523 事件）：10 视图 paper **10/10 PASS**。
  截图：`output/gui/production/`。
- 一次性 nonce 浏览器全链路：无 localStorage 打开 `/ui#/pulse?boot=…`，
  自动换 token、fragment 清空、状态栏在线、无连接面板。

### 4.3 生产只读验证（2026-10-23）

- daemon 已用新代码重启：pid 70237，v1.0.0，watcher 开，ledger id 不变。
- 端点时延：`/api/v1/overview` **0.53s**、`/api/v1/health` **0.66s**、
  `/api/v1/agents` **0.15s**、`/api/v1/memories?limit=50` **8ms**；
  `quick_check` 1.8s（仅体检按钮按需）。
- `hippomind doctor`：READY（privacy 32,615 条 tool_output 与 launchd 外置卷为既有
  warning，不是本轮引入）。
- `/ui`：302 → `/ui/` 200，CSP/nosniff/frame DENY 齐全；HTML 无 token/路径。

## 五、已知边界（如实）

1. 生产账本当前 claims/relations 为 0（历史采集以 raw_chat/progress 为主），因此
   图谱/事实视图在真实库显示空态；seed 数据验证了 108 claims/64 relations/40 skills
   的非空路径。
2. GUI 仅接受管理员本机 token；scoped agent 凭据访问 `/api/v1/*` 返回 403（有意）。
3. 唯一写操作是置顶/反馈；遗忘/裁决不提供按钮执行。
4. 旧 matplotlib 工作台保留但非主界面；`--web` 是新入口。
5. 外置卷 launchd 开机自启仍受 macOS TCC 限制（既有平台限制，doctor 提示）。
6. `docs/GUI_ATLAS.md` §9 的 644 测试数是本轮实测；不写入 README，避免测试数漂移。

## 六、运行方式

```bash
# 推荐：启动并打开 Web 控制台（一次性令牌）
./start-hippomind.command --web

# 或由 CLI 打开；--no-open 只打印一次性 URL
python3 -m hippomind ui
python3 -m hippomind ui --no-open

# 旧版 matplotlib（保留）
./start-hippomind.command --gui
./start-hippomind.command --network
```

## 七、提交与回滚

- `03af121` feat(gui): Atlas admin read API, static console serving, hippomind ui
- `da6c2de` feat(gui): rebuild the console as HippoMind Atlas (paper archive)
- 回滚：`git revert da6c2de 03af121`（旧 Observatory 文件在本轮前未提交，回滚后
  /ui 将回到无前端资产状态；如需完全回到旧版，请从备份恢复并重新部署旧 web 目录）。
- 生产数据未迁移（schema 仍为 v8）；GUI 只读，pin/feedback 也走既有审计路径。
