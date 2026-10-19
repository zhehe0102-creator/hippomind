# HippoMind 前景路线图（2026-08-22）

## 前景推测：为什么本机记忆枢纽会在未来三年变得关键

1. **本地 Agent 爆发**：Claude Code / Codex / Cursor 类工具让每个开发者同时运行
   多个长任务 Agent——它们共享同一台机器，却各自失忆。跨 Agent 记忆枢纽是
   刚需，且目前没有事实标准。
2. **隐私法规 + 本地模型**：数据不出机器从「偏好」变成「合规要求」。纯本地
   架构（单文件 SQLite、无外呼）是云方案的永久软肋。
3. **MCP 协议标准化**：编辑器/Agent 通过 MCP 挂工具成为默认姿势。谁的记忆
   服务器 MCP 工具面最完整，谁就是默认安装项。
4. **记忆质量 > 记忆数量**：689k 行 raw 归档证明「存」已解决，「蒸馏出可信
   事实」才是下一个战场。
5. **可审计成为企业门槛**：证据链、遗忘影响面、冲突谱系——这些我们已有的
   能力会从加分项变成准入项。

## 已就位的地基（本轮盘点确认）

- MCP stdio 服务器：10 工具全表面（context_pack/ingest/feedback/stats/
  injection/sync + **recall/pin/forget/conflicts 本轮新增**），读写权限随凭据分级
- 向量通道接口 + HashingNGramEmbedding 实现（A/B 实测：双语套件分数与延迟
  与关闭时完全持平 → 维持默认关闭，等真实语义 embedding 接入再启用）
- GUI：时间轴回放/调速/GIF、激活分解、快照命名、hub rtt、**Pack 缓存命中率
  与账本计数仪表（本轮新增）**

## 演进主线（按杠杆排序）

### P1 — 语义检索补全（决定上限）
- [ ] 内置本地 embedding：接入 bge-small-zh 量化版（llama.cpp/ONNX，离线），
  走现有 EmbeddingAdapter 协议；候选生成仍由 FTS 负责，向量只做重排融合
- [ ] 混合检索评测集：改写/同义/跨语言四类用例进 retrieval_eval，锁定收益

### P1 — 记忆蒸馏管线（把 674k raw 变成可用事实）
- [ ] watcher 接本地 LLM（Ollama/qwen）：归档→候选事实→quarantine 待审，
  人工 resolve 放行；蒸馏产物带 provenance 指向源事件
- [ ] 蒸馏吞吐基准：每小时可处理归档量 vs 归档增速

### P2 — 实体图谱（从 claim 长出来）
- [ ] subject/predicate 已结构化：补实体规范化（别名合并）+ 图遍历查询
  （`hippomind graph <entity>` 一跳/二跳邻居）
- [ ] GUI 叠加图层：选中实体高亮其 claim 网络

### P2 — 多机协作形态
- [ ] LAN 只读镜像模式：第二台机器挂载只读账本副本 + 定期 rsync 快照
- [ ] 冲突合并策略文档化（content_hash 幂等已保证安全重放）

### P3 — 体验打磨
- [x] GUI 缓存命中率/账本仪表（2026-08-22）
- [ ] GUI 内嵌查询框（免切终端跑 context）
- [ ] `hippomind tui` 只读巡检面板（checkup+stats+trend 曲线）

## 明确不做（负空间）

- 云同步/多租户：违背本地优先定位
- 自动删除类维护：遗忘必须留审计、可回滚
- 引入重型 Python 依赖：标准库红线不破（embedding 走子进程/ONNX runtime 隔离）
