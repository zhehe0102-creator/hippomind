# HippoMind 功能详解

> 面向使用者的完整功能手册。HippoMind 是一个**本机多 Agent 记忆中枢**：
> 把你电脑上各个 AI Agent（Claude Code、Codex、Cursor 等）的聊天与任务
> 归档成一本可检索的长期记忆账本，再以「最小、带证据、可审计」的方式
> 喂回给需要的 Agent。一句话：**让所有 AI 共享同一份会生长、会遗忘、
> 会自我修正的记忆**。

---

## 1. 它能解决什么问题

| 没有 HippoMind | 有了 HippoMind |
|---|---|
| 每个 Agent 各自失忆，跨会话经验清零 | 一次学到的部署参数/踩坑教训，所有 Agent 可检索 |
| 让 AI "记一下"= 塞进系统提示词，越堆越长 | 按当前任务动态组装最小 Context Pack（默认 ≤8 条/带证据） |
| 聊天记录散落各处无法查询 | 归档自动采集 → SQLite/WAL 单文件账本，FTS+LIKE 双通道检索 |
| AI 说错没人对账 | feedback 升降权 + claim 冲突检测 + 人工裁决 |

## 2. 总体架构

```
采集层   ArchiveWatcher（轮询归档目录，30s）→ 新会话自动入库
存储层   SQLiteLedger（SQLite/WAL，schema v7，事件溯源：ledger_events 只追加）
服务层   hippomindd daemon（localhost HTTP + token 鉴权）
消费层   CLI / MCP 工具 / HTTP API / GUI 工作台 / 自动注入钩子
```

- **事件溯源**：每次写入先落 `ledger_events`（不可变审计流），记忆投影
  （`memories` 表）可随时从事件重建——改错可回滚，历史不可篡改。
- **单文件部署**：整个记忆就是一个 `.sqlite` 文件（+wal/shm），备份=复制文件。

## 3. CLI 全部子命令（38 个）

### 3.1 生命周期
| 命令 | 干什么 | 典型用法 |
|---|---|---|
| `init` | 初始化账本 | `hippomind init` |
| `start` | 启动/复用 daemon；`--read-only` 只读模式（LAN 镜像）；`--restart` 升级后重启 | `hippomind start --restart` |
| `stop` | 优雅停机（WAL 检查点后退出） | `hippomind stop` |
| `status` | daemon 健康+账本规模 | `hippomind status` |
| `version` | 版本/schema 版本/概要 | `hippomind version` |
| `doctor` | 不泄露凭据的可用性自检 | 排障第一步 |
| `monitor` | 只读巡检循环；`--json` 单次快照供 cron | `hippomind monitor --json`（离线 exit 1） |

### 3.2 写入与反馈
| 命令 | 干什么 |
|---|---|
| `ingest` | 写入结构化事实（content/kind/source_agent/evidence 必填；支持 claims/causal_parents/scope/valid_from·until 双时态） |
| `feedback` | 外部验证结果升降权（signal -1..1，自动钳制），影响后续检索排序 |
| `pin` / `pins` | 置顶记忆（检索有界加权 0.06，只重排相关候选不强行带入无关结果） |
| `forget` | 按 memory_id 或 import_batch 回滚投影；`--dry-run` 先看影响面；`--purge` 连审计内容一起擦除 |

### 3.3 检索与消费
| 命令 | 干什么 |
|---|---|
| `context` | **核心命令**：按任务组装最小 Context Pack（分层预算、coverage 门控防误召回、缓存命中 ~0.02ms） |
| `auto-context` | 同上但解析"记忆："触发标记，适合挂到 Agent 钩子 |
| `inject` | 开关下一轮自动注入（采集维护不受影响） |
| `facts` | 结构化事实投影（活跃 claim 视图） |
| `skills` | 程序性记忆投影（怎么做的步骤） |
| `graph` | 实体图谱：claim 主语为节点、因果/关联为边，1–2 跳邻域；冲突实体可见并计数 |

### 3.4 数据进出
| 命令 | 干什么 |
|---|---|
| `export` | JSONL（ingest 兼容）或可回放 SQL；`--since` 增量；`--encrypt` 口令加密 |
| `import` | 幂等导入（content_hash 去重，重导零重复） |
| `backup` | 原子快照 + sha256；自动登记 backups.json |
| `restore` | 从备份恢复（daemon 必须停止；`--sha256` 校验） |
| `maintain` | VACUUM 整理 + `--prune-backups N` 轮转旧备份（sha256 吻合才删，篡改文件跳过） |

### 3.5 体检与安全
| 命令 | 干什么 |
|---|---|
| `checkup` | 五项只读体检：integrity/FTS 同步/游标/claim 悬挂/计数；exit 1 表示需处理 |
| `recovery-check` | 深度恢复前检查 |
| `conflicts` / `resolve` | 列出冲突 claim 谱系 → 人工裁决指定胜出方 |
| `redact-raw` | 一键清除 raw chat 证据（须先 stop） |
| `scan-raw` | 扫描 raw 证据占用 |
| `login-install` / `login-remove` | 本机登录态管理（安装/移除登录自启项） |
| `install-auto` / `install-agents` / `agent-status` / `discover-agents` | 给 Claude Code/Cursor 等写 MCP profile、查看接入状态 |
| `sync` / `watch` | 手动增量同步 / 前台观察采集 |

## 4. MCP 工具（11 个，给 AI Agent 用）

`hippomind_context_pack`（按任务取最小上下文）· `hippomind_recall`（关键词检索）
· `hippomind_ingest`(写入) · `hippomind_feedback`(验证反馈) · `hippomind_stats`
· `hippomind_injection`(开关注入) · `hippomind_sync` · `hippomind_pin`
· `hippomind_forget` · `hippomind_conflicts`(冲突谱系) · `hippomind_graph`(实体图谱)

安装：`hippomind install-agents --auto` 后重启编辑器即可。受限 Agent 凭据
只能读写自己 scope，管理员 token 全量。

## 5. HTTP API（daemon，默认 127.0.0.1:8765）

| 方法 | 端点 | 说明 |
|---|---|---|
| GET | `/health` | 存活+账本指纹（防 token 拿去操作另一本账） |
| GET | `/stats` | 账本/归档/来源/缓存命中率统计 |
| GET | `/graph?subject=&scope=&hops=&limit=` | 实体图谱（缺 subject→400，无 token→401） |
| GET | `/metrics` | Prometheus 文本（memories/events/claims_active/conflict/pinned/wal/cache） |
| POST | `/ingest` | 批量写入（≤预算，超长 413 且客户端必能读到错误响应） |
| POST | `/recall` · `/context-pack` · `/feedback` · `/sync` | 对应 CLI 能力 |
| POST | `/admin/reload` | 热重载归档扫描（SIGHUP 同效） |
| POST | `/shutdown` | 优雅停机 |

并发语义：HTTP/1.1 keep-alive、backlog 128、请求槽位信号量限流；
8 客户端压测 48,000 行零错误零丢失。

## 6. GUI 工作台（`python3 -m hippomind.main`）

- **网格世界+海马体模拟**：Agent 在网格觅食，情景→语义巩固全程可视
  （权重/效用/触碰/回忆次数分解面板）
- **记忆网络图**：域聚类布局，点击聚焦某 Agent 的证据网（紫描边）、
  点击记忆看激活分量；**实体图层**：选中文本命中账本主语时共现记忆青色边提亮
- **内嵌查询框**：底部输入 Enter 直跑 Context Pack，结果进面板
- **时间轴回放**：记忆生长过程逐帧回放；G 键快照 PNG
- **HUD**：采集状态/FPS/Generation/账本规模实时显示

## 7. daemon 内部机制

- **自动采集**：watcher 每 30s 扫描归档根（支持多根轮换），新 Agent 出现
  上报 `new_agents`，webhook 通知（尽力而为 3s）
- **睡眠巩固**：空闲 >10min 自动深维护（consolidate+raw 归档+WAL 检查点），
  每空闲期只跑一次
- **指标趋势**：后台周期采样进 /metrics 与 stats.trend
- **优雅停机**：SIGTERM→停采集→WAL TRUNCATE 检查点→状态文件清理

## 8. 安全模型（默认即安全）

1. **绑定 localhost**：daemon 拒绝绑定非回环地址
2. **token 鉴权**：0600 权限文件存本机 token；每请求 `X-HippoMind-Token`
3. **scope 隔离**：`shared` vs `agent:<id>`；受限凭据跨 scope 读→403
4. **--read-only**：所有凭据（含管理员）降级只读共享视图，写全拒 403——
   LAN 镜像机物理上不可能误写主库
5. **raw chat 三重隔离**：默认不入活跃池/不进 Context Pack/导出需显式
   include_raw_chat；`redact-raw` 可一键擦除
6. **账本指纹**：token 绑定 ledger_id，防止拿 A 库 token 操作 B 库
7. **加密导出**：`export --encrypt` 流式加密（混淆级），import 自动解密
8. **输入防线**：长度钳制（query≤4000 字符等）、非有限数值拒绝、
   超长 body 413、伪造 Content-Length 有排空上限

## 9. 性能画像（2026-08-22 实测）

| 场景 | 数字 |
|---|---|
| 并发写入吞吐 | ~9,500-10,000 RPS（8 线程×1000，零丢失） |
| 检索剖析（300 语料/60 查询） | 0.136s→**0.077s**（本轮优化 -43%） |
| context pack 缓存命中 | 22.8ms→0.02ms |
| 10k/50k 行 search p50 | ~28ms/~93ms（trigram+门控后） |
| schema 迁移演练 | 生产库副本 2GB：打开+校验 <6s，689,273 行零丢失 |

## 10. 五分钟上手

```bash
cd "/Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent"
hippomind init && hippomind start     # 起服务
hippomind ingest --content "生产库端口是 6543" --kind fact \
    --agent me --evidence "manual:1"  # 教它一件事
hippomind context --query "数据库端口" # 验证能想起来
hippomind install-agents --auto       # 接入你的 AI 编辑器
open -a Terminal python3 -m hippomind.main   # 打开可视化工作台
```

## 11. 已知边界（诚实清单）

- 纯中文 FTS 依赖 trigram（旧 SQLite 优雅回退 LIKE）；跨词边界查询走 LIKE 门控
- embedding 向量通道接口就绪但默认关闭（本机无模型运行时，见 ROADMAP P1）
- raw 蒸馏管线待 LLM 运行时
- 投影 API 返回形态不完全统一（list vs dict，消费方已适配）
