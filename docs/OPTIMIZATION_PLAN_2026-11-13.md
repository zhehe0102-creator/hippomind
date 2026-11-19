# HippoMind 记忆系统全面优化方案（2026-11-13）

> 来源：WorkBuddy 会话 `e71ca582`（HippoMind 全链路审计，基线 `fd8b3a6` round 12）。
> 本文件是实施用工作分解；最终报告见 `OPTIMIZATION_REPORT.md`。

## 0. 现状基线

- 工作区：`/Volumes/移动硬盘/记忆系统重构`（HEAD `fd8b3a6`，含未提交的 round-13 草稿改动）
- 目标解释器：`/usr/bin/python3` = Python 3.9.6，numpy 可用；ruff 未安装（lint.sh 会 SKIP）
- 回归基线：`Ran 1738 tests`，1 个既存失败 `test_forgetting_report.test_main_json_round_trips_and_honours_days_and_limit`
- 审计已复现（固定夹具，本轮必须逐条修掉）：
  1. 注入闭合标签逃逸：恶意 `</MEMORY_DATA_UNTRUSTED>` 渲染后出现 2 个闭合标签
  2. `--read-only` daemon 仍启动 watcher，空闲后执行 `maintain(archive_raw=True, purge_raw=True)`
  3. 单槽裁决把败者记忆的无关 claim 一并下线（`theme` 从 active → conflict）
  4. raw 提炼固定写 `scope="shared"`、无 `derived_from_raw_chat` 标记
  5. Codex 跨批次：第 2 条消息 `session_id=""`、`project=""`、证据行号从 1 重新计数
  6. raw 身份键误合并不同来源事件（两条不同 event_id 的“继续”合并为 1 条），流式半截/完整内容却并存
  7. Gemini 同时间戳记录永久漏采（3 条只入库 2 条）
  8. 历史切片（as_of）任务展开未传 as_of，混入当前进度
  9. 负反馈 `signal=-1` 后 confidence 0.50 → 0.54
  10. 向量检索与后台同步竞态：`IndexError: list index out of range`
  11. JSONL 迁移丢 claim / procedural，导入整批失败
  12. 会话摘要永久陈旧（新增消息后 pending 仍为 0）
  13. `/metrics` 匿名开放

## 1. 工作包（写作用域互斥）

### WP-A 注入边界安全（security）
- 文件：`integrations/dsh-plugin/lib/index.js`、`hippomind/llm_agent.py`、
  `tests/test_dsh_plugin.mjs`、新增 Python 回归测试
- 设计：
  - 服务端统一渲染契约：payload 内的 `<MEMORY_DATA_UNTRUSTED>` /
    `</MEMORY_DATA_UNTRUSTED>`（大小写、空白变体）与 `<MEMORY_DATA` 前缀一律转义
    （`<` → `&lt;`），保证渲染结果中恰好只有 1 个开标签与 1 个闭标签。
  - DSH 插件与 `llm_agent.DEFAULT_PROMPT` 使用同一契约；Python 侧新增
    `render_untrusted_block(text)`，在 `str.format` 前完成记忆正文转义。
  - 不改动 `context_pack.render_untrusted_payload` 的既有 nonce 契约。
- 验收：恶意输入闭合标签计数 = 1；开标签计数 = 1；恶意指令仍在块内；现有 dsh 插件测试通过。

### WP-B 只读模式 + 观测面（read-only / observability）
- 文件：`hippomind/ledger/base.py`、`hippomind/daemon.py`、`hippomind/injection_trace.py`、
  `tests/test_read_only_mode.py`、新增测试
- 设计：
  - `SQLiteLedger(path, read_only=True)`：连接用 `file:...?mode=ro` URI + `PRAGMA query_only=ON`；
    跳过 `_bootstrap` 迁移与 WAL 切换，仅校验 schema 版本；`_connect(write=True)`、
    `_transaction`、`maintain`、`maintenance_checkpoint` 在只读模式拒绝写/降级为空操作。
  - daemon `run_daemon` 在 `read_only` 时不启动：watcher、discovery（自动安装）、
    capture、distill、vector、sleep、metrics-trend、warmup；仅保留纯读 index 构建。
  - `/metrics` 纳入 `_require_auth()`（/health 继续免鉴权）。
  - `BaseMixin.close()` 关闭本进程所有线程的 reader 连接（线程注册表）。
  - `injection_trace` 改尾部读取（有界），trace 文件 0600 + 轮转上限；metrics.jsonl 同权限。
- 验收：只读服务后台零写入（SQLite 文件 mtime/size 不变、无 maintain 调用）；
  只读连接写入抛错；`/metrics` 无 token 401、有 token 200；相关旧测试更新后通过。

### WP-C 裁决爆炸半径 + 双时态（ledger temporal/conflict）
- 文件：`hippomind/ledger/lifecycle.py`、`hippomind/context_pack.py`、
  `tests/test_claim_resolve.py`、`tests/test_conflict_review.py`、`tests/test_claim_temporal.py`、
  `tests/test_as_of.py`、新增测试
- 设计：
  - `resolve_claim()` 委托 `settle_claim_slot()`：只让位同槽位 claim；败者记忆只有在
    不再持有任何 active claim 时才整体下线；返回兼容字段（`resolved/winner/losers`）。
  - `context_pack` 任务状态展开（task 补充检索）与因果祖先把 `as_of` 透传下去。
  - 同秒精度导致 `_supersede_window_end` 返回空串时，用“败者 valid_from 的下一秒”
    闭合窗口；保持“返回空串仅当无法构造非零窗口”不变式。
  - 负反馈：`feedback()` 中 confidence 不再加 `abs(signal)`；仅正向信号提升。
- 验收：单槽裁决后无关 claim 仍 active、记忆仍可召回；`resolve_claim` 既有测试
  语义（胜者/败者/翻案）保持；历史包不含 as_of 之后的进度；同秒 supersede 后历史
  as_of 不再双活；`signal=-1` confidence 不增。

### WP-D 采集与消化（capture / distill / digest）
- 文件：`hippomind/transcripts.py`、`hippomind/gemini_capture.py`、
  `hippomind/distill.py`、`hippomind/session_digest.py`、新增/相关测试
- 设计：
  - Codex/Claude 游标持久化 `offset + absolute_line + session_id + cwd`；批次从游标
    恢复上下文，证据行号使用绝对行号；文件截断时重置并记录。
  - Gemini 复合 keyset：`(last_modified_time, conversation_id)`，查询
    `WHERE last_modified_time > ? OR (last_modified_time = ? AND conversation_id > ?)`；
    游标存 JSON；兼容旧纯时间戳游标。
  - distill：提炼结果继承来源 raw 记忆的 scope（不固定 shared），metadata 写
    `derived_from_raw_chat=true`、`untrusted_source=true`；显式共享必须走独立提升 API。
  - session digest：游标记录消息数 + last_at + digest memory_id；会话新增消息后重新
    进入队列，新版摘要 supersede 旧摘要；不足 min_messages 不产摘要。
- 验收：Codex 跨 3 批 session/project/absolute line 正确；Gemini 3 条同时间戳全部入库；
  私有 raw 提炼后 scope 非 shared 且带标记；新增消息后 pending=1 且旧 digest superseded。

### WP-E 身份、迁移、并发与原子 IO
- 文件：`hippomind/ledger/identity.py`、`hippomind/ledger/clients.py`、`hippomind/schema.py`、
  `hippomind/vector_index.py`、`hippomind/semantic_space.py`、`hippomind/cursor_capture.py`、
  `hippomind/pi_capture.py`、`tests/test_identity_stable.py`、`tests/test_vector_index.py`、
  新增测试
- 设计：
  - identity v3：显式来源事件 ID（`event_id/external_id`）进入 raw 身份键；无事件 ID 时
    保持 v2 去重语义；v2 兼容探测保留。流式同事件多版本：同一事件 ID 内容变化时
    以 revision 链 supersede 旧行（不并存）。
  - `export_rows`：每条记忆输出完整 `claims` 列表、`procedural`、context、causal parents
    与必要反馈字段；导入端 `ingest_many` 可完整回灌；保持旧格式向后兼容。
  - 向量索引：search/sync 同一把 RLock（或快照拷贝），保存用唯一临时文件 + `os.replace`；
    `semantic_space` 保存同样原子化。
  - `cursor_capture/pi_capture` 的流式记录带稳定事件 ID 与 revision，供 identity v3 使用。
- 验收：不同 event_id 的同文本 raw 保留两条；同一 event_id 重放幂等；流式半截+完整
  只留一条可召回（旧行 superseded）；JSONL 导出→导入 claims 数一致且 procedural 可回导；
  向量并发竞态用例连续 100 次无异常；保存原子性测试通过。

## 2. 集成与验收（Lead）

1. 全部工作包完成后：`python3 -m unittest discover -s tests`（Python 3.9）
2. `bash scripts/lint.sh`（ruff 缺失时 SKIP，需保证无新增 E/F/W 债务）
3. `/usr/bin/python3 tests/drill_schema_migration.py --json`
4. `node --check integrations/dsh-plugin/lib/index.js` + `node tests/test_dsh_plugin.mjs`
5. 新增回归测试必须覆盖审计复现脚本中的每个失败形态。
6. 文档同步：`docs/ARCHITECTURE.md`、`docs/AUTOMATIC_MEMORY.md`、`docs/RELEASE.md`
   的失真陈述按现状改写；新增 `OPTIMIZATION_REPORT.md` 记录修复清单与验收证据。
7. Git 对象目录 AppleDouble（`._pack-*` 等）先备份到隔离区再移出 `.git/objects/pack`，
   最后 `git fsck --full`（可回滚，不做删除）。

## 3. 非目标（本轮不做）

- 重写 `commons.py` 双存储架构（仅在报告/backlog 记录，避免高风险大改）。
- 调 embedding 参数、检索算法大改（审计明确要求先修正确性）。
- 生产账本清理/迁移 --apply（本轮只做冻结副本演练）。


## 4. 第二轮增强（2026-11-13 晚，已全部实施并验收）

第一轮验收 13/13 之后，按“离满分还差什么”的清单继续做了 5 个可验证工作包：

| 任务 | 内容 | 结果 |
| --- | --- | --- |
| task-9 WP-H | causal_ancestors 支持 as_of；context_pack 透传，历史包不泄漏未来祖先 | tests/test_causal_as_of.py 9 项 + 端到端 1 项 |
| task-10 WP-I | commons 双存储对账 compare/repair + scripts/reconcile_commons.py | 26 项测试；真实 commons 只读跑通 |
| task-11 WP-J | CI 矩阵 3.9–3.13 + macOS + release-gate；scripts/release_check.sh | release_check 7/7 PASS；3.13 全量 OK |
| task-12 WP-K | HTTP /context-pack 随机 A/B + arm trace + 因果报告 | 7 项测试；默认关闭保持旧行为 |
| task-13 WP-L | scripts/governance_rehearsal.py 冻结副本治理演练 | 8 项测试；合成 5200 行 before 脏→after 全 0 |

最终数据：3.9 与 3.13 各 1915 tests OK；lint gate 90/90；审计验收 13/13；release_check 7/7；
仍受环境限制未闭环的项（生产部署、真实 A/B 样本、关系边时间窗、claim 级历史投影）见 OPTIMIZATION_REPORT §8.1。

## 5. 第三轮增强（2026-11-14，已全部实施并验收）

| 任务 | 内容 | 结果 |
| --- | --- | --- |
| task-14 WP-M | 会话感知 follow-up 锚定 + session_id 全链路透传 + 安全守卫 | 35 项测试；follow-up 命中 5/18→18/18；普通查询逐字节不变；锚点 p95 0.175ms |
| task-15 WP-N | `scripts/eval_benchmark.py` + 48 查询 benchmark + 阈值门禁 + CI/release 接入 | 20 项测试；recall 1.000 / MRR 0.9722 / update 1.000 / abstain 1.000；release_check 8/8 |
| task-16 WP-O | 事实卡原型（slot 当前值 + 历史 + as_of） | 26 项测试；渲染缩减 81.6%；未接入注入 |
| task-17 WP-P | trace 保留期（默认 30 天、原子清理、0600） | 15 项测试 |

最终数据：3.9 与 3.13 各 2012 tests OK；lint OK；审计验收 13/13；release_check 8/8。

## 6. 第四轮增强（2026-11-14，已全部实施并验收）

| 任务 | 内容 | 结果 |
| --- | --- | --- |
| task-18 WP-Q | 事实卡 opt-in 接入（memory_ids 过滤、有界卡块、去重、环境开关） | 35 项测试；默认关闭逐字节不变；82→141 字符、当前事实显式 |
| task-19 WP-R | `hippomind explain` 离线只读解释 CLI | 17 项测试；PARSER_SURFACE 44；人类+JSON 同源 |
| task-20 WP-S | distill/llm_client 成本与延迟指标 | 10 项测试 + 既有 130 项回归；行为不变 |
| Lead A4 | 备份/恢复/篡改拒绝演练 + release_check 第 9 步 + CI | 3 项测试；drill PASS；release_check 9/9 |

最终数据：3.9 与 3.13 各 2066 tests OK；lint OK；审计验收 13/13；release_check 9/9。
