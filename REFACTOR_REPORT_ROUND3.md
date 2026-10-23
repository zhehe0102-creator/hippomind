# 第三轮：记忆系统能力、运行环境与 GUI 继续优化

> 2026-10-23 · Lead + 4 个专项子智能体 · 工作区 `/Volumes/移动硬盘/记忆系统重构`
> 实现提交：`6c6fd60`（feat(atlas): environment panel, activity feed, pins, claim entry and GUI polish）
> 前置：`da6c2de`（Atlas v1.0）、`d57fc04`（验收报告）

## 一、本轮目标

用户要求：继续优化记忆系统、增强能力、完善电脑上的使用环境，然后继续把 GUI 做得更好。
因此本轮分三条线：

1. **系统环境**：把 Python/依赖/磁盘/备份/登录项/daemon 地址变成可见、可检查、可复现；
2. **记忆能力**：补上活动流、置顶、重复候选与「写入一条事实」的完整闭环；
3. **GUI v1.1**：活动流、环境面板、事实录入、置顶过滤、导出、命令面板搜索、
   帮助抽屉、跟随系统主题。

## 二、环境现状（实测，来自 `/api/v1/environment`）

| 项 | 值 |
|---|---|
| Python | 3.9.6（当前解释器以 environment 端点实时返回；工作区/解释器可在外置卷） |
| 依赖 | matplotlib 3.9.4、SQLite 3.54.0 |
| 工作区 | `/Volumes/移动硬盘/记忆系统重构`，外置卷 |
| 磁盘 | 1.9 TB 总量，745 GB 可用 |
| 账本 | 248,523 事件，schema v8，WAL；daemon v1.0.0 pid 80795 |
| 最近备份 | `backups/hippomind-20261021T045936Z.sqlite`，426 MB，约 2.4 天前，sha256 已登记 |
| 登录项 | `com.hippomind.daemon` / `com.hippomind.gui` plist 存在且路径有效；外置卷受 macOS TCC 限制，登录自启仍不可用 |
| 体检 | `hippomind doctor` READY（privacy tool_output 与 launchd 外置卷为既有 warning） |

结论：运行环境本身可用；限制来自平台（外置卷 + launchd），本轮把它变成 GUI 里可见的
事实，并提供手动启动/环境创建脚本，而不是假装可修复。

## 三、系统环境增强

- 新增 `GET /api/v1/environment`：platform/python/executable、workspace 卷类型与磁盘、
  daemon host/port/ui_url/uptime、ledger 大小/schema/journal、backups registry 与最近
  备份（存在性/年龄）、launchd plist 路径与问题、dependencies、home，以及
  python/磁盘/备份/登录项/日志模式等 checks（带可复制的修复命令）。
- 体检页新增「运行环境」卡片：Python、平台、工作区卷、磁盘可用、依赖、最近备份、
  登录自启、daemon UI 地址、运行目录；提供「复制启动命令 / 复制打开命令 / 复制 UI 地址」。
- 新增 `scripts/bootstrap-env.sh`：默认只检查 Python/sqlite/是否已有 .venv；
  `--create` 在 exFAT 上用 `--copies` 创建 .venv 并安装 requirements（需要网络）。
  `start-hippomind.command` 本来就优先使用 .venv/bin/python。
- 文档同步：README、MEMORY_HUB_GUIDE、USAGE_GUIDE 增加环境自检、备份、登录项与
  启动方式说明。

## 四、记忆能力增强

### 4.1 活动流

- `GET /api/v1/events`：只返回结构化事件（默认排除 raw_chat/tool_output/
  system_message/developer_message/environment/command_output/approval），支持
  limit/type/agent/since，返回 content_preview（≤240）与按类型计数。
- 脉搏页新增「最近活动」：最近 8 条事件 + 类型聚合，raw/工具输出不会出现在界面。

### 4.2 置顶

- `GET /api/v1/pins`；记忆库新增「置顶」过滤（`pinned=1`），列表 pin 列不再显示
  无意义的破折号。置顶/取消置顶仍走既有审计操作。

### 4.3 重复候选

- `GET /api/v1/duplicates`：只读调用账本指纹分桶 + 字符 Jaccard，返回
  pairs/scanned/suggestions；体检页新增「重复候选」按钮，按需扫描最近样本，
  展示相似度与预览。不合并不删除。

### 4.4 事实写入（唯一直接写操作）

- 事实页新增「记录事实」表单：subject / predicate / slot_key / value / evidence /
  confidence / content；提交走既有 `POST /ingest`，写入一条带证据的 claim，
  source_agent 固定 `atlas-gui`，scope 为 shared，evidence 必填。
- 写入后事实页自动刷新；图谱空态提供「记录一条事实」入口。
- E2E 实测（隔离 seed）：填写 AtlasE2E/records/storage/SQLite 后，
  `/api/v1/claims?subject=AtlasE2E` 返回 1 条 active claim，界面无 console error。
- 生产账本没有被写入（E2E 只在隔离 seed 上执行）。

## 五、GUI v1.1 优化

- **脉搏**：最近活动卡片；ACTIVE MEMORIES 标签从「近 30 天」改为「当前」；大数
  紧凑格式从 25 万级修正为 24.9 万级。
- **记忆库**：置顶过滤、当前页 CSV/JSON 导出（客户端 Blob，无服务端写）、pin 列
  空值不再渲染破折号。
- **事实**：记录事实表单、CSV 导出、空态/写入说明更准确；tab 切换不再整页重挂载。
- **图谱**：侧栏标题去重；空态引导到事实录入。
- **体检**：运行环境、重复候选、备份/登录项可见；存储与检查项保持不变。
- **参考**：主题增加「跟随系统」；设置项文案更新。
- **全局**：命令面板输入关键词时追加「在记忆中搜索…」「在记忆库中过滤…」动态命令；
  `?` 打开快捷键/帮助抽屉；T 键在纸本/夜档间切换；跟随系统时监听
  `prefers-color-scheme`；compose 的 as_of 占位符缩短避免截断。
- **样式**：新增活动/帮助/环境/重复候选/导出/表单的细线布局；沿用纸本 token，
  night 下同步验证。

## 六、验证

| 门 | 结果 |
|---|---|
| 全量单测 | **648 项 OK**（新增 API 测试 4 项：environment/events/pins/duplicates） |
| lint | gate **90/90**、legacy **254/254**，exit 0（ruff 0.16.3） |
| seed 截图 | **20/20 PASS**（10 视图 × paper/night，0 console error/0 HTTP≥400/0 溢出/0 XSS） |
| 生产截图 | **10/10 PASS**（真实 248,523 事件） |
| 交互 E2E | 事实表单写入 + 回读 active；置顶过滤（6/6 行 pinned）；命令面板动态搜索；重复候选扫描 |
| 生产端点 | environment 0.14s、events 0.32s、pins 0.22s、duplicates 9ms、overview 0.50s、memories 8ms |
| 生产体检 | doctor READY；daemon 已重启为 v1.0.0 pid 80795 |
| 前端静态 | 17 项：资源/模块图/node 语法/组件导出/无外链/无内联脚本/CSP 关键点 |

截图目录：`output/gui/r2-final/`（seed 20 张）、`output/gui/production-v11/`（生产 10 张）、
`output/gui/r2-interact/`（交互状态）、`output/gui/r2-claim/`（事实写入 E2E）。

## 七、已知边界（如实）

1. 生产账本 claims/relations 仍为 0；事实/图谱在真实库显示空态，直到通过「记录事实」
   或 CLI ingest 写入 claim。seed 数据（108 claims/64 relations/40 skills）验证了非空路径。
2. GUI 仅接受管理员本机 token；scoped agent 凭据访问 `/api/v1/*` 返回 403。
3. 唯一直接写操作是「记录事实」（以及置顶/反馈）；遗忘/裁决仍只给 CLI 命令。
4. 外置卷 + launchd 登录自启受 macOS TCC 限制，无法通过改 plist 修复；环境面板与
   doctor 会持续提示，建议用 `hippomind start` 手动启动。
5. 重复候选只扫描最近样本（GUI 默认 2000 条，可调），不是全库证明。
6. 环境创建脚本 `--create` 需要网络安装 requirements；当前 daemon 继续使用系统 Python。

## 八、使用方式

```bash
# 打开 Web 控制台（一次性登录）
./start-hippomind.command --web
# 或
python3 -m hippomind ui

# 检查/创建可复现环境
scripts/bootstrap-env.sh
scripts/bootstrap-env.sh --create

# 事实录入：控制台 → 事实 → 记录事实
# 环境面板：控制台 → 体检 → 运行环境 / 重复候选
```

## 九、提交

- `6c6fd60` feat(atlas): environment panel, activity feed, pins, claim entry and GUI polish
- `da6c2de` feat(gui): rebuild the console as HippoMind Atlas
- `d57fc04` docs(gui): record the Atlas redesign and its measured acceptance

回滚：`git revert 6c6fd60` 回到 v1.0；数据无迁移（schema 仍为 v8）。


## 十、一键打开（本轮新增）

用户反馈「打不开」，因此本轮把启动路径做成真正的一键：

- 工作区根目录新增 **`HippoMind Atlas.app`**：双击后自动确保 daemon 运行、
  申请一次性令牌并打开浏览器控制台；失败时弹出原生对话框并指向日志
  `~/Library/Logs/HippoMind-Atlas.log`。应用有 AppIcon、Info.plist，并做了
  ad-hoc codesign。
- 新增 **`打开 HippoMind Atlas.command`**：Finder 双击即可（会打开 Terminal 执行同一
  脚本），适合不想用 .app 或 macOS 拦截应用时使用。
- 共享逻辑在 `scripts/launch-atlas.sh`：优先 .venv/bin/python，回退 /usr/bin/python3；
  先检查/启动 daemon，再用 `hippomind ui --no-open` 拿一次性 URL，最后 `open`；
  支持 `HIPPOMIND_LAUNCH_DRY_RUN=1` 做无副作用自检。
- 为什么 .app 通过 Terminal 启动：本工作区在外置卷，macOS 会阻止直接放在可移动卷上的
  应用执行卷内脚本（实测 LaunchServices 以 exit 126 退出）；把 .command 交给 Terminal
  是用户已授权、且不依赖后台任务读取外置卷的可靠路径。

实测：`open "HippoMind Atlas.app"` 后日志出现 app bundle → Terminal → launch 记录，
一次性 URL 生成成功；`open "打开 HippoMind Atlas.command"` 同样成功；两者都只读打开
控制台，不写账本。
