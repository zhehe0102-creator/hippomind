# HANDOFF —— HippoMind 自续进化引擎（会话轮换交接）

> 新会话接续时：先读本文件 + docs/BACKLOG-hippomind.md，重建 todo，从「当前任务」继续。
> 不要重新规划已验证的路线；共享目录 .opencode/ 会被其他会话移动，持久状态以本目录为准。

## 北极星

把 HippoMind 打磨成本机多 Agent 最好用的 AI 记忆系统（提取/凝练/注入/GUI/长跑稳定）。
滚动协议：实现 → 测试全绿 → 记录进度 → 更新 BACKLOG → 取下一项；队列 <10 自动补足 ≥15。

## 已验证基线（勿回退，改动后须重验；2026-08-21 迭代12 复验）

- 评测基准：40 例中文 QA 四类全 **1.0**（run_memory_qa_suite，tests/test_retrieval_eval.py）
- embedding 适配器：HashingNGramEmbedding 默认关，A/B 召回持平（词法通道已覆盖）

- 测试：`python3 -m unittest discover -s tests` → **188 OK**（含 test_review_fixes 24 项、test_upgrade_round 12 项）
- 学习基线：seed=42 headless 3000 步 → 觅食 **49** / 毒药 2 / +0.0310（惊喜度巩固后比文档基线 42 更高）
- 压力：`tests/memory_hub_stress.py --seconds 10 --writers 6` → ~2360 writes/s
- 浸泡：`tests/soak_simulation.py` → STABLE，finalRSS ~20MB（增量维护改造后 last10≈17ms）
- JS：`node --check integrations/dsh-plugin/lib/index.js` OK
- GUI：真实库 **1080 边 / 192 节点** Wazone 风格密集织网，截图 `/tmp/hippomind_gui_network.png`

## 本轮已完成（迭代 1，2026-08-21）

GUI 密度调优 + Wazone 风格改造：
- 共振边阈值 0.14→0.08、top-6；冷归档 top-2；同层互连 sim≥0.25 互惠去重
- 深空黑底 #04050a + 薰衣草节点 + 边缘辉光层（3.4×宽 α14%）+ 560 粒星尘
- 终端风等宽字体（MONO 列表）标题/HUD 盒式面板/图例点/[ 括号 ]按钮
- 修 HUD 标题重叠（等宽字宽 0.030/字符）与演化区行溢出（row_gap=0.052）
- network_visualizer max_edges 默认 1200→2000

## 已完成（迭代 2，2026-08-21）

中文记忆 QA 基准：40 例四类 **全部 1.0**（recall@8 / update / temporal / abstention）。
附带两个真实检索修复：① `_like_terms` CJK 碎片下探到 3 字（词序不同问句此前完全漏召回）；
② context_pack 纯事实兜底——无结构化字段的 fact/semantic 此前整组静默丢弃，现渲染要点行。
阈值回归测试在 tests/test_retrieval_eval.py（190/190 全绿）。

## 已完成（迭代 3，2026-08-21）

时间短语解析：time_parse.py 11 种短语（昨天/前天/今天/上周/本周/上月/本月/最近N天/一周/一月/半月）
→ build() 自动 from/to；任务或项目过滤存在时自动撤销防过窄；缓存键含有效时间。

## 已完成（迭代 4，2026-08-21）

claim 冲突裁决：ledger.resolve_claim() 按槽位组聚合（支持翻案重裁），
CLI `hippomind resolve --claim --winner`，审计事件落账；6 项新测试（207 全绿）。

## 已完成（迭代 5，2026-08-21）

导出/导入 CLI：export_rows() 幂等 JSONL 迁移、raw 隔离、claim 槽位随迁；4 项测试（211 绿）。

## 已完成（迭代 6–7，2026-08-21）

- 迭代6：HUD「采集状态」行——watcher 运行/停用/离线三态（采集中·轮询30s），离线回退正确。
- 迭代7：多 Agent 聚焦视图（点 commons 层 agent → 来源记忆紫描边+边提亮+计数）；
  workbench 快照键 G（PNG 存 output/）。新增 5 测试。

## 已完成（迭代 8–9，2026-08-21）

- 迭代8：热重载——reload()（强制发现+受控自动安装）/admin/reload/SIGHUP；集成测试。
- 迭代9：睡眠巩固——空闲>10min 触发深维护（巩固+raw归档+检查点）；**修 monotonic 近 0
  与 0.0 哨兵混用 bug**（Optional 语义）；新增 5 测试。

## 已完成（迭代 10，2026-08-21）

文档同步：README v0.9.3 清单 + 设计文档七点六节；启动器 --network 直启。

## 已完成（迭代 11，2026-08-21）

embedding 适配器：HashingNGramEmbedding（零依赖确定性哈希向量）+ search 可选重排
通道（默认关）+ CLI 开关。A/B 实测：评测集召回持平——词法通道已覆盖，接口为未来
真实语义 embedding 预留。5 项新测试（223 全绿）。

## 已完成（迭代 12，2026-08-21）全面回归验证轮

223 测试 OK · 浸泡 STABLE(finalRSS 19.8MB) · 压力 2349 writes/s · JS OK ·
seed42 觅食49/毒药2 · GUI 1080边/192节点。全部基线保持。

## 已完成（迭代 13，2026-08-21）

巩固规则效用回填：reinforce 正向 → 上游 episodes 权重信用（0.15 比例，纯权重不动
touch，负向不回传）。**新基线：seed42 觅食50 / 毒药2 / +0.0322**。2 项测试。

## 已验证基线更新（2026-08-21 迭代13 后）

- seed42 headless 3000 步：觅食 **50** / 毒药 **2** / 平均回报 **+0.0322**
- 评测基准 40 例四类全 1.0；全套 225 测试绿

## 断点与坑

- `.opencode/` 是多会话共享目录，GOAL.md 会被启动协议移走——持久状态用本目录
  （docs/BACKLOG-hippomind.md + 本文件），不要依赖共享 GOAL.md。
- 子代理 task 工具经常返回空输出——重试一次索取结果，仍为空就自己干。
- Agg 环境下 "PingFang SC not found" 警告无害（真实 GUI 正常解析）。
- hippocampus_memory.json 是用户真实演示库（gitignore）：跑 headless 演示务必加
  `--memory <tmp> --no-persist`，避免覆盖。


## 已完成（迭代 14–27 合并补记，2026-08-21）

> 勘误：此前多轮 HANDOFF 更新因替换文本失配静默未生效，本节一次性补齐。
> 权威队列见 .opencode/BACKLOG.md（逐项 [x] 带证据），交接快照见
> .opencode/handoffs/ses_fdbc4dcd5ffe3Ct5Wjm9s4DzDx.md。

- 迭代14：2 字词回退通道 + 可靠性门双词重叠分支（弃权基准保持 1.0）
- 迭代15/16：时间轴回放（T 键）+ 网络视图快照键 G + 回放进度百分比
- 迭代17：ContextPackOptions.state_fraction/max_block_chars 显式分层预算
- 迭代18：watcher 新 Agent 出现报告（new_agents 字段）
- 迭代19：隐私遗忘 forget（软/硬两级+审计）；迭代20：facts/skills 投影 CLI
- 迭代21：检索 trace filtered 条目附 score 快照；迭代22：跨语言改写 32 组词
- 迭代23：DSH 游标清理（list/delete_cursor + _prune_dsh_cursors）
- 迭代24：daemon HTTP 并发基准 95.5 RPS / p95=186.5ms / 0 错误
- 迭代25：DETAILED_GUIDE 5.5 节（界面/自治/CLI 指南）
- 迭代26：英文评测套件 run_english_qa_suite() 16 例全 1.0（_run_suite 双语内核）
- 迭代27：cProfile 剖析 → 线程本地读连接缓存 _reader()/close() →
  recall 185.8→231.6 qps（+25%）；报告 docs/benchmarks/recall-profile-2026-08-21.md

**当前测试基线：246 全绿。**

## 已完成（迭代 28，2026-08-21）

大库性能曲线 benchmark_scale.py：ingest 351/333/166 RPS；search p50
4.8/28.5/123.7ms（超线性）；报告 docs/benchmarks/scale-curve-2026-08-21.md。
新登记 P2「search O(n) 成分定位」入队。

## 已完成（迭代 29，2026-08-21）

FTS trigram 切换（v6 迁移+兼容回退）+ 中文滑窗 + LIKE 门控：
search p50 -17%/-25%、ingest +16%/+22%；双语 QA 全 1.0；249 测试绿。

## 已完成（迭代 30，2026-08-21）

context-pack 缓存命中率：cache_stats() 进 daemon /stats；重复查询
22.8ms→0.02ms 零成本证实；250 测试绿。

## 已完成（迭代 31，2026-08-21）

优雅停机：shutdown() 收尾 WAL 检查点+读连接关闭；SIGTERM 子进程集成测试
（exit 0/状态清理/WAL 归零）；251 测试绿。

## 已完成（迭代 32，2026-08-21）

backup 校验和：sha256+bytes 进输出；端到端恢复验证；252 测试绿。
（注：全套偶发 1 次时序失败后连续两轮绿——疑似停机集成测试负载抖动，观察。）

## 已完成（迭代 33，2026-08-21）

pin/unpin：v7 迁移 + 有界加权（仅重排相关候选契约）+ CLI + 审计；
257 测试绿；双语 QA 全 1.0。

## 已完成（迭代 34，2026-08-21）

checkup 体检命令（五项只读诊断+fail 退出码）；260 测试绿；BACKLOG 第四批补至 17 项。

## 已完成（迭代 35，2026-08-21）

restore --sha256 守卫：篡改拒绝+正常恢复验证；262 测试绿。

## 已完成（迭代 36，2026-08-21）

duplicate_candidates 合并建议进 maintain（数字保留+桶上限防误报/爆炸）；
264 测试绿。

## 已完成（迭代 37，2026-08-21）

scope 隔离矩阵：16 组合零泄漏+硬边界+pack 过滤+置顶不跨界。

## 已完成（迭代 38，2026-08-21）

WAL 告警：stats.wal_bytes + >64MiB warnings 字段；269 测试绿。

## 已完成（迭代 39，2026-08-21）

stats 剖析：0.05ms/次，间隔门控下无热点——评估后关闭。

## 已完成（迭代 40，2026-08-21）

深分页四契约（不重不漏/稳定/越界空/前缀一致）；273 测试绿。

## 已完成（迭代 41，2026-08-21）

并发 claim 收敛 + 取代序列契约；275 测试绿。

## 已完成（迭代 42，2026-08-21）

往返基准：导出 0.12s/导入 0.76s/重导全幂等；报告落盘。

## 已完成（迭代 43，2026-08-21）

README「运维工作流」节（八类命令示例）。

## 已完成（迭代 44，2026-08-21）

键位帮助行补全（T/G/S/L 缺口）+ 绑定⇒可见契约测试；277 测试绿。

## 已完成（迭代 45，2026-08-21）

PORTABILITY 跨机迁移节（双路径+实测引用）。

## 已完成（迭代 46，2026-08-21）

英文时间短语：_EN_PATTERNS + 评测 parse 模式；双语套件全 1.0；281 测试绿。

## 已完成（迭代 47，2026-08-21）

CJK 分词评估关闭：落地 like_terms 裁剪（救援 -43%）；报告落盘。

## 已完成（迭代 48，2026-08-21）

webhook 通知 + tasks 源 new_agents 补齐；283 测试绿。

## 已完成（迭代 49，2026-08-21）

激活分解面板（选中记忆显示 7 分量）；284 测试绿；BACKLOG 第五批补至 15 项。

## 已完成（迭代 50，2026-08-22）

/ingest 批量已存在（records≤128），实测 29.5× 提速——关闭。

## 已完成（迭代 51，2026-08-22）

checkup --repair：DROP+CREATE 结构性重建 FTS（实测 DELETE 式不清除外部
篡改损坏）；286 测试绿。

## 已完成（迭代 52，2026-08-22）

pins 列表命令（text/json）；288 测试绿。

## 已完成（迭代 53，2026-08-22）

search LRU（generation 失效+突变隔离）；291 测试绿。

## 已完成（迭代 54，2026-08-22）

审计事件孤儿归档（maintain events_keep=，FK 约束下的正确策略）；294 测试绿。

## 已完成（迭代 55，2026-08-22）

健康自监控 health 字段进 stats；296 测试绿。

## 已完成（迭代 56，2026-08-22）

import 批次戳 + **修复 export/import 双重定义遮蔽缺陷**（JSONL 版曾为死代码）；
README 勘误；299 测试绿。

## 已完成（迭代 57，2026-08-22）

run_all_benchmarks.py 汇总脚本（含质量门）；全部基准通过。

## 已完成（迭代 58，2026-08-22）

英文弃权 4→8 例（精度保持 1.0）；299 测试绿。

## 已完成（迭代 59，2026-08-22）

时间轴 +/- 调速与 . 暂停；303 测试绿。

## 已完成（迭代 60，2026-08-22）

watcher 多根轮转扫描；305 测试绿。

## 已完成（迭代 61，2026-08-22）

README mermaid 架构数据流图。

## 已完成（迭代 62，2026-08-22）

export --encrypt（PBKDF2+CTR+HMAC，混淆级定位）；308 测试绿。

## 已完成（迭代 63，2026-08-22）

v1.0 截图生成并接入 DETAILED_GUIDE。

## 已完成（迭代 64，2026-08-22）

会话级过滤确认关闭（已存在）；BACKLOG 第六批补至 19 项。

## 已完成（迭代 65，2026-08-22）

forget --batch 批次回滚；311 测试绿。

## 已完成（迭代 66，2026-08-22）

conflicts 冲突谱系命令；313 测试绿。

## 已完成（迭代 67，2026-08-22）

/metrics Prometheus 端点；315 测试绿。

## 已完成（迭代 68，2026-08-22）

metrics.jsonl 每小时趋势行；317 测试绿。

## 已完成（迭代 69，2026-08-22）

export --since 增量导出 + 死代码注册块清理；320 测试绿。

## 已完成（迭代 70，2026-08-22）

maintain --vacuum；323 测试绿。

## 已完成（迭代 71，2026-08-22）

FTS 体积审计；325 测试绿。

## 已完成（迭代 72，2026-08-22）

ingest 幂等契约锁定；328 测试绿。

## 已完成（迭代 73，2026-08-22）

token 预算校准文档+契约测试；332 测试绿。

## 已完成（迭代 74，2026-08-22）

hub rtt 进 HUD；334 测试绿。

## 已完成（迭代 75，2026-08-22）

英文 update 用例 4→8；334 测试绿。

## 已完成（迭代 76，2026-08-22）

快照命名带 seed/节点数；336 测试绿。

## 已完成（迭代 77，2026-08-22）

PORTABILITY 勘误+Windows 示例（示例命令实测通过）。

## 已完成（迭代 78，2026-08-22）

checkup --format md；338 测试绿。

## 已完成（迭代 79，2026-08-22）

cursor 分页评估+深分页空页缺陷修复；341 测试绿。

## 已完成（迭代 80，2026-08-22）

第七批补货至 18 项。

## 已完成（迭代 81，2026-08-22）

forget 影响面摘要；344 测试绿。

## 已完成（迭代 82，2026-08-22）

缓存失效精度确认关闭；347 测试绿。

## 已完成（迭代 83，2026-08-22）

并发 ingest 压测 11k RPS 零错误；347 测试绿。

## 已完成（迭代 84，2026-08-22）

export --format sql 审计导出；350 测试绿。

## 已完成（迭代 85，2026-08-22）

checkup --format junit；352 测试绿。

## 已完成（迭代 86，2026-08-22）

时间轴 GIF 导出；354 测试绿。

## 已完成（迭代 87，2026-08-22）

ledger 打开耗时剖析报告；354 测试绿。

## 已完成（迭代 88，2026-08-22）

英文 temporal 4→8；354 测试绿。

## 已完成（迭代 89，2026-08-22）

README FAQ 节（9 类症状速查，处理命令全部实测）；354 测试绿。

## 任务结束（2026-08-22）

用户验收通过，明确指示结束任务。最终状态：
- 354 测试全绿；双语 QA 套件四指标全 1.0（中 40 例/英 32 例）
- 三项基准全过（HTTP 并发 95.5 RPS / 并发写入 11,014 RPS 零错误 / 检索剖析）
- 真实库 checkup ok；schema v7；BACKLOG 剩余 17 项待办移交后续会话
- 全链路冒烟（ingest→search→pack→pin→feedback→forget 影响面）通过

## 下一步队列

权威队列：/Users/zps/Documents/Default Project/.opencode/BACKLOG.md
（第三批 15 项待办；下一项 P2 大库性能曲线）。

## 终审轮（2026-08-22，用户验收前深度审查）

- **修复迭代 71 审计误报**：health_overview.fts 分母从全表改为 _rebuild_fts
  不变量（排除 raw 且非 active）——生产库 689k 行实测 expected=14806 与实际
  完全吻合，误报消除；新增 raw 归档豁免测试。
- 新增 hippomind version 命令；游标自愈项确认已覆盖后关闭。
- 深度审查：6 线程混合写零错误、9 daemon 端点全 200+401 鉴权、35 命令
  --help 全过、GUI 渲染快照 OK、生产库只读验证通过。
- 最终：355 测试绿，基准全过。

## 前景轮（2026-08-22，打造最优软件）

- MCP 工具面 6→10：新增 recall/pin/forget/conflicts（读写权限随凭据分级，
  只读策略写操作 AccessDenied 实测）；5 项测试。
- GUI footer 仪表：账本计数 + Pack 缓存命中率（backlog 清一项）。
- 向量通道 A/B：双语套件分数/延迟与关闭完全持平 → 维持默认关闭（诚实决策），
  HashingNGramEmbedding 留作真实语义 embedding 接入前的接口验证。
- docs/ROADMAP.md：前景推测五条 + P1/P2/P3 演进主线 + 负空间清单。
- 修复 GUI 仪表对桩数据的 None 相加崩溃（缺键默认 0）；360 测试绿。

## 前景轮二（2026-08-22）

- **P2 实体图谱落地**：ledger.entity_graph()（claim 主语为节点、
  memory_relations 为边、1–2 跳有界 BFS、大小写不敏感）+ CLI `hippomind graph`
  （text/json）；4 项测试（364 绿）。生产库无活跃 claim → 空图属预期。
- 环境探测：无 ollama/onnxruntime/sentence-transformers——P1 神经 embedding
  与 LLM 蒸馏被外部依赖阻塞，已在 ROADMAP 标注前置条件。

## 前景轮三（2026-08-22）

- forget --dry-run（只读预览，不进事务不 bump generation）；4 测试。
- ingest/feedback 离线直写回退（daemon 不可达才触发；存活时强制 HTTP，
  并发安全不变）；3 测试。
- docs/benchmarks/README.md 索引页；README 五分钟教程（六步全实测，
  修正 --agent 与 feedback 位置参数两处文档错误）。
- 371 测试绿，基准全过。

## 前景轮四（2026-08-22）

- GUI 内嵌查询框：底部 TextBox（Enter 提交）→ hub context_pack → 结果
  进 Context Pack 面板；无 hub 时提示走 CLI；Agg 构造安全幂等；4 测试。
- 压测脚本参数化确认已存在后关闭（--seconds/--writers/--batch-size，
  实测 4863 wps）。
- 371→375 测试绿。

## 前景轮五（2026-08-22）

- schema 迁移演练脚本：真实生产库副本（2GB）全流程验证——行数零丢失、
  FTS 一致、探针检索可用；修正 schema_before 读法。

## 前景轮六（2026-08-22）

- 备份登记+轮转：backups.json 登记表 + maintain --prune-backups N
  （sha256 校验后才删，篡改文件跳过）；3 测试。
- daemon --read-only 模式：所有凭据降级只读共享视图（LAN 镜像使能器），
  CLI start --read-only 全链路透传；读放行/写全拒/库不变；3 测试。
- 381 测试绿，基准全过。

## 前景轮七收尾

- BACKLOG 清零后按规则补货 15 项（第四批，P2×2 + P3×13）
- MCP 十工具核实无 graph → hippomind_graph 入列 P2
- 冲突裁决 CLI 已存在（cmd_resolve），GUI 面板任务只需指向它

## 前景轮八（2026-08-22，GOAL 驱动）

- hippomind_graph MCP 工具（5 测试）
- GUI 实体图层：recent_subjects 索引 + 共现青色边 + 详情邻域行（4 测试）
- monitor --json（离线 exit 1）
- /metrics 增 conflict/pinned 计数（2 测试）
- export --since 验证后关闭（1 测试固化增量+幂等）
- 396 测试绿，基准全过。

## 前景轮九（2026-08-22，GOAL：五遍深度审查+优化+功能详解）

- 五遍审查（正确性/并发/错误处理/性能/一致性）→ docs/code-review-2026-08-22.md
- 修复 3 项（均带回归测试）：413 排空（客户端可读错误响应）、
  监听 backlog 5→128（8 客户端压测曾 reset 丢 128 条，修后 48000 行零丢失）、
  README 工具数漂移 10→11（防漂移测试锁定）
- 性能优化 2 项：search 缓存 deepcopy→定向拷贝（-33%）、
  查询侧分析提升出评分循环（累计 -43%，0.136s→0.077s）
- docs/FEATURES.md 功能详解（CLI 38 命令/MCP 11 工具/HTTP API/GUI/安全模型/性能画像）
- 测试计数口径说明：当前 discover=374 全绿（67 文件逐一点名核实，
  含本目标新增 16 项）；早前轮次报 396 系当时目录含临时探针文件所致

## 2026-08-24 GUI 观感与资源轮（用户投诉：线条乱/模糊/文字挤压 + 高 CPU）
- **模糊**：`network_visualizer._build_figure` dpi 100→150（2880×1620 背衬），全局字号 +1~2pt。
- **线条毛球**：`_edge_data` 新增每节点度数上限（association 3 / resonance 4 / source 3 /
  commons 4 / drive_memory 6 / domain_memory 10），按强度择优保留；max_edges 2000→900；
  辉光层 3.4×/0.14 → 2.4×/0.10；EDGE_WIDTH 0.68→0.55。
- **文字挤压**：HUD 面板行距/字号重排，按钮移入面板行下方（Evolution [E] / Prune [P]）；
  图例改上下两行；detail/context 两行移到查询框上方；键位帮助移到底部右缘（保留
  `self.keys_help` 属性满足 test_keys_help 契约）；commons 栏名称截 8 字符+省略号、
  计数固定右对齐 x=6.08；标题/副标题拉开。
- **高 CPU**：main.py 新增 `_attach_idle_tracking`/`_frame_pause`——交互 30FPS，
  空闲 30s 后 2FPS。实测：活跃 ~44% → 空闲 0.4~2.3%；GUI 连续运行 13.5h 无崩溃。
- **MCP 注册修复（重要）**：5 个 agent（claude/pi/cursor/gemini/local）的 MCP 注册指向
  已清空的临时目录 `/var/folders/.../tmpmnz8vjs5/hub.sqlite`（0 记忆、凭据文件丢失），
  已用 `install-agents --auto --ledger ~/Library/Application Support/HippoMind/hippomind.sqlite
  --runtime-dir ~/Library/Application Support/HippoMind` 全部重指生产账本；agent-status
  运行时握手 5/5 ready（各 11 工具）。
- **维护守卫修复**：`maintain --vacuum`/`redact-raw`/`restore` 原来只要 8765 端口有
  daemon 就拒绝（误伤无关账本）；新增 `_daemon_serves_ledger`（比对 ledger_id），
  仅同账本才要求先 stop。修复 9 个环境性测试失败。
- **时间测试修复**：test_time_parse 上周样本 now-8d 在周一运行会掉出日历周，改为
  固定上周三正午。
- **GUI 自启**：`~/Library/LaunchAgents/com.hippomind.gui.plist` 已创建但**未加载**——
  launchd 拉起的 matplotlib 窗口不映射到屏幕（cgWindowNotFound），GUI 暂用 nohup
  启动（日志 `~/Library/Application Support/HippoMind/hippomind-gui.log`）；daemon
  自启 plist 正常。待查：launchd 会话下 NSWindow 映射问题。
- **验收**：`python3 -m unittest discover -s tests` → Ran 376 tests OK（2026-08-24）。
