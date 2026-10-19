# BACKLOG —— HippoMind 持续进化任务队列

> 规则：按优先级从上往下取任务；完成后标记 `[x] 日期 证据`；剩余 <10 项时必须补足 ≥15。
> 补充来源：a 缺陷 b 调研差距 c 文档承诺 d 性能瓶颈 e 多 Agent 体验。

## 进行中

- [ ] **P0 GUI 密度调优**：共振边阈值 0.14→0.08、每规则 top-4→top-6，加同层高相似互连
  （sim≥0.25 top-1），目标：演示库可见曲线 ≥400；重截图对比 Wazone 参考效果。

## 待办（P0 高优）

- [ ] **P0 检索评测基准**：构建中文记忆评测集（≥40 QA：事实召回/知识更新/时间推理/弃权），
  `tests/test_retrieval_eval.py` 扩展为可跑分回归；输出 recall@8 / 弃权正确率 / 更新命中率基线数字。
- [ ] **P0 时间短语解析**：query 中"昨天/上周/最近N天"解析为 from_time/to_time 过滤，
  接入 context_pack.parse_entities；附测试。
- [ ] **P1 claim 冲突手动裁决 CLI**：`hippomind resolve --claim <id> --winner <memory_id>`，
  裁决后胜者恢复 accepted、败者 superseded；附测试。
- [ ] **P1 可选 embedding 通道**：无依赖的字符 n-gram 哈希向量（dim=256）作为第三召回通道
  进 RRF 融合；开关默认关（`--enable-ngram-vector`）；附召回质量对比测试。

## 待办（P1 功能）

- [ ] **P1 daemon 热重载规则**：SIGHUP / `/admin/reload` 触发 provider manifest 与注入规则重载。
- [ ] **P1 记忆导出/导入 CLI**：`hippomind export --scope shared --format jsonl` 与 `import`，
  支持跨机迁移；校验 evidence 必带。
- [ ] **P2 GUI Hub 实时面板**：network HUD 增加 daemon 在线状态、账本事件数、watcher 游标延迟
  （轮询 /stats，30s 节流）。
- [ ] **P2 workbench 快照键**：visualizer 按 G 保存当前帧 PNG 到 output/。
- [ ] **P2 多 Agent 权重隔离视图**：network commons 层点击某 agent 高亮其全部来源记忆边。

## 待办（P2 质量/性能）

- [ ] **P2 ledger 读路径连接池**：_reading() 复用线程本地连接，减少高频 recall 的打开开销；
  基准：100 次 search 耗时对比记录进进度区。
- [ ] **P2 FTS 前缀查询优化**：CJK 长词改前缀 MATCH（`term*`）评估召回/性能，附数据。
- [ ] **P3 文档同步**：README/MEMORY_SYSTEM_DESIGN 增补本轮全部升级（RRF/共振边/增量维护/WAL 检查点/GUI 重构）。
- [ ] **P3 start-hippomind.command 支持 --network 直启全屏网络模式。**
- [ ] **P3 睡眠巩固调度**：daemon 检测系统空闲（无请求 + 无 watcher 增量 >10min）时自动跑
  consolidate + archive_raw，空闲日志一条。

## 补充候选池（生成新任务时的灵感源）

- 查询改写：英文↔中文关键词映射表提升跨语言召回
- memory timeline 视图（GUI 按时间轴回放记忆生长）
- watcher 对新归档 agent 类型的自动发现与接入报告
- context pack 分层预算（结构化优先/raw 截断先牺牲）显式化配置
- 巩固规则效用回填：semantic 被 recall 且行动成功时 reinforce 上游 episodes
