# HippoMind 记忆系统全面重构报告

> 2026-10-19 · Lead + 8 个专项子智能体 · 基线 schema v8 → 当前 v8（v9 待用）
> 工作区：`/Volumes/移动硬盘/记忆系统重构`（git 版本化，20+ 原子提交）
> 真实数据备份：`backups/hippomind-pre-compaction-20261019T141216Z.sqlite`
> （sha256 `948002887dd4…`，quick_check ok，1,536,641 行）

## 一、重构前的问题（全部实测，非推测）

| # | 缺陷 | 证据 | 级别 |
|---|---|---|---|
| 1 | raw 采集去重风暴：去重键混入 occurred_at，重抓即新行 | 单会话 70,508 行仅 3,573 种内容；1,475,584 行 content_hash 全互异 | P0 |
| 2 | 保留期只降级不删除，库无界增长 | 4.48GB；`archive_raw_evidence` 明写 "Rows stay in SQLite" | P0 |
| 3 | 巩固自噬：summary 哈希含易变字段，每轮空闲都新增 | 56,010 条 semantic 仅 142 种内容，单条最多重复 5,423 次 | P0 |
| 4 | 归档源码（v5）读不动线上库（v8），源码断档 | 「拒绝降级写入」实测复现 | P0 |
| 5 | claims / relations / feedback / procedural **全部 0 行**：卖点能力是死代码 | 生产库逐表计数 | P1 |
| 6 | 原始聊天横幅/口水话按满分入库，占爆 8 项 Context Pack 预算 | 质量门 raw 一律 1.0 | P1 |
| 7 | `<USER_REQUEST>` 包裹 + 每次变化的时间戳使同句无法折叠 | 键归一缺陷三处 | P1 |
| 8 | `fts_sync_report` 二次复杂度 >14min 不收敛；cold 行误判永久 warn | 实测 | P1 |
| 9 | 演示包与 Hub 反向依赖：import 存储层拖起 1.4k 行网格世界 | `ledger.py: from .hippocampus import token_sim` | P2 |
| 10 | 巨型模块：ledger.py 3,810→4,129 行单类、cli.py 2,004 行 | wc -l | P2 |
| 11 | 无自动缩容、无 lint/迁移 CI、版本号三处手抄漂移 | 0.9.2 vs 1.0.0 | P2 |

## 二、重构后的能力（逐项带验收）

**存储与采集治本**
- 稳定身份键：raw 证据按 `source_agent|session_id|role|规范化内容` 判重（剥离 harness 包裹与时间戳）；结构化记录保留双时态（显式 valid_from/occurred_at 才算新观测）→ 重抓幂等、历史合法并存。
- `purge_expired_raw()`：归档→fsync→回读校验→才删除，严格 fail-closed；API 默认 `cold 可审计`旧契约，daemon 显式开启 TTL 删除；`raw_ttl_days` 可配。
- `collapse_duplicate_rows()`：按会话内组折叠历史重复，最早行幸存，删除前行行入验证过的 gzip 归档。
- `consolidate()` 幂等：来源集合指纹做证据标记，100 轮不再增长。
- 自动缩容：删除后按 freelist 阈值增量/有界 VACUUM，指标入 `/stats` + Prometheus。

**检索质量**
- 噪音分类（`textutil.is_noise_text/informative_ratio`）：横幅、`/command` 回显、工具横幅、单字应答隔离在 0.15，正常散文按信息密度 0.55~1.0 —— 结构化记录契约不变。
- 键统一：`<USER_REQUEST>` 是容器（去壳留肉），`<ADDITIONAL_METADATA>` 等是易变块（连壳去）；wrapped/unwrapped、全半角、连字符变体同键；普通文本键零漂移。
- 混合检索：HybridRanker 词法主导+向量有界加成的独立重排通道（双语评测证明不劣后才可开默认）。
- 质量门固化：中文 40 例 + 英文 32 例 × 四类（recall@8/update/temporal/abstention）**全 1.0** 回归测试。

**未兑现能力激活**（生产 0 行 → 有写入路径、有测试）
- claims：decision/preference 保守自动派生（多值/超长/低置信/不可信来源/无 task 不派生），冲突-裁决-双时态链路真实可达。
- relations：adapter 顺序写自动串联因果链，`causal_ancestors` 可走全链。
- feedback：`complete(outcome=success|failure)` → 对因果祖先做 ±0.3 有界信用（24h、同 task、≤8 条、稳定 feedback_id 幂等；召回永不等于信用）。
- procedural：`skill()` 显式技能卡 + `distill_skill()` 从成功结果+因果链自动提炼（失败/非 result/无祖先一律拒绝；步骤只存不执行）。

**架构与工程化**
- `textutil.py` 抽出 → Hub 不再 import 演示包；`__init__` 全惰性：`import hippomind.ledger` 不再加载网格世界/matplotlib；**AST 边界测试永久封禁回退**。
- `schema.py` 抽出迁移链（v1..v8 集中可审）。
- SCHEMA_VERSION/`__version__` 单一来源（daemon 从包取，杜绝 v5-v8 式漂移重演）。
- CI 四门：单测 + **fixtures 迁移演练** + lint 增量棘轮 + 基准冒烟；`scripts/lint.sh`（无 ruff 优雅跳过）、`backup.sh`/`restore.sh`（SQLite online backup API + sha256，实测 cp 会漏 20 行 WAL 数据）。
- `tests/drill_schema_migration.py`：拒绝未来库必须**字节级不变**（SHA-256），并修掉了「假绿」与「-1 哨兵」两个探针缺陷。
- `hippomind version --json`；daemon 收敛性指标；`fts_sync_report` 线性化 + 语义修正（0.29s/136K 行）。
- **ledger.py → `hippomind/ledger/` 包**（9 个 mixin，最大文件 888 行；AST 自由名分析机械搬家，成员逐字保留；旧导入面全量 re-export，487 测试零改动通过；ruff --fix 清掉 6 个拆包后才显形的重复 `import re`，基线按精确计数 rebase：17→14 净降 3）。
- **cli.py → `hippomind/cli/` 包**（11 个文件：app/parser/common + 6 组子命令；零行为/参数/输出变化；`hippomind.cli:main` 入口与 `python3 -m hippomind` 均验证通过）。

**数据收口**（先备份后动手，全程可回滚）
- 4,483.3MB → **406.8MB（-91%）**；1,536,641 → 136,459 行；freelist 归零；quick_check ok。
- 删除内容 100% 在 gzip 归档（199MB raw + 1.8MB semantic）；逐行外部溯源指针以备份为权威（报告已注明边界）。
- TTL=90 删除 0 行——最老数据仅 71.2 天，**没有为凑数字降 TTL**；此后 daemon 自动收敛。

## 三、增删功能清单
- **增**：稳定去重键、fail-closed TTL 真删、历史折叠（raw+semantic）、自动缩容、噪音质量门、统一内容键、混合重排通道、claim 自动派生、因果链自动写入、结果信用分配、技能卡与自动提炼、迁移演练 fixtures、lint 棘轮、备份/恢复脚本、收敛性指标、架构边界测试、压缩工具链（`tools/migration/`）。
- **删/收敛**：演示包不再被 Hub 依赖（未物理删除：Grid World 仍是教学/演示资产，由 `start-hippomind.command --gui` 按需加载）；删除 140 万冗余行而非删除功能；废弃三处手抄版本号与「warn 永久化」的旧健康检查语义。
- **未动**（明确决策）：Context Pack 8 项/6000 token/16000 字符预算契约；「记忆：」显式门控；loopback-only daemon 与 token 权限模型；raw 冷数据的 LIKE 审计通道。

## 四、验证状态
- 全量单测：基线 384 → 483（+99）全绿；双语评测四类全 1.0。
- `drill_schema_migration.py` 21 项 PASS；`scripts/lint.sh` gate 90/90、legacy 259/259。
- 真实库：`PRAGMA quick_check = ok`，search/FTS/无孤儿行复核通过。

## 五、遗留风险（如实）
1. semantic 折叠后 daemon 的「空闲巩固 +10」日志会停增，但若有人回退 identity 键会复发——已有 20+ 回归测试锁死。
2. 冲突双下线的语义未变：连续两条 decision 会让旧那条进入 conflict（知识更新收敛），运营需知晓 `conflicts/resolve` 工作流。
3. 归档不含 metadata_json（溯源以备份为权威，已写入压缩报告）。
4. distill 的因果深度按设计取 2；超深链需显式 max_depth。
5. ledger/cli 包化已完成并通过全部门禁；但 launchd 自启项仍指向 PiDeck 旧路径（`~/Library/LaunchAgents/com.hippomind.*.plist` 的 PYTHONPATH 写死 `pi-desktop/...`，该目录已不存在）——启用重构版前需先 `login-install` 重装自启项指向新工作区。
6. `raw-archive` 与压缩归档合计约 201MB 保留在库旁目录；如需异地留存请纳入备份策略。
