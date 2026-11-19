# HippoMind 记忆系统全面优化报告（round 13）

> 日期：2026-11-13 · 依据：WorkBuddy 会话 `e71ca582`（HippoMind 全链路审计，基线 `fd8b3a6` round 12）
> 实施方式：Lead + 5 名并行工程师按互斥写作用域完成；本文只记录已落地代码与实测证据。

## 一、结论摘要

- 审计列出的 **13 条可复现缺陷全部修复**，`scripts/audit_acceptance.py` 从 0/13 → **13/13 PASS**（exit 0）。
- 全量回归：**Python 3.9 与 3.13 各 Ran 2066 tests — OK**（基线 1738）；修掉了三个既存缺陷（test_forgetting_report 固定日期、3.13 random.seed、停机 WAL 竞态）。
- schema 迁移演练 `drill_schema_migration.py --json` → `ok=true`；`node --check` 通过，DSH 插件测试 **7/7**；`scripts/lint.sh` → **OK**（ruff 0.16.3：gate 90/90、legacy 249/254）；`release_check.sh` 9 步门禁 **9/9 PASS**（含检索质量门禁与灾备演练）。
- 全部验证在临时 `HIPPOMIND_HOME` 与临时账本中完成；**未读写生产账本，未修改真实 Agent 配置**。
- 第二轮增强（causal as_of、commons 对账、CI 跨版本/平台与发布门禁、HTTP 随机 A/B、冻结副本治理演练、3.13 兼容）见 **§八**。
- 第三轮增强（会话感知 follow-up 锚定、基准式检索质量门禁、事实卡原型、trace 保留期）见 **§九**。
- 第四轮增强（事实卡 opt-in 接入、hippomind explain、成本/延迟指标、灾备演练）见 **§十**。
- 附带修复审计 P2 的工程债：Git 对象目录 AppleDouble 隔离（`git fsck --full` 通过、`git status` 无报错）、`injection_trace` 尾部读取/0600/轮转、`semantic_space`/向量索引原子保存、`integrations/query.py` 破坏性重写、线程连接回收。

## 二、审计 13 条 → 修复与证据

| # | 审计项 | 修复 | 验收证据 |
| --- | --- | --- | --- |
| 1 | 注入闭合标签逃逸 | WP-A：DSH 插件与旧 LLM 统一服务端渲染，payload 内 `<` 转义为 `&lt;`，渲染结果恰好 1 开 1 闭 | `injection_dsh_boundary` / `injection_python_prompt` PASS；9 Python + 7 Node 用例 |
| 2 | `--read-only` 仍后台写库/清理 raw | WP-B：只读账本 `mode=ro`+`query_only`，后台写循环全部不启动，SIGHUP 不改 Agent 配置 | `read_only_daemon` PASS（后台事件空、写拒绝、账本 mtime/size 不变、`/metrics` 401→200） |
| 3 | 单槽裁决误伤无关事实 | WP-C：`resolve_claim` 委托 `settle_claim_slot`，先收敛孤儿 conflict 再判定整体下线 | `resolve_blast_radius` PASS（theme 保持 active、败者仍可召回） |
| 4 | raw 提炼越权写 shared、无派生标记 | WP-D：继承来源 scope，metadata 写 `derived_from_raw_chat`/`untrusted_source` | `distill_scope_isolation` PASS（`agent:claude` + 标记） |
| 5 | Codex 跨批次丢 session/project/行号 | WP-D：游标持久化 `offset+absolute_line+session_id+cwd` | `codex_cross_batch_context` PASS（三批 session-A/project-A/行 2,3,4） |
| 6 | raw 身份误合并 / 流式版本并存 | WP-E：identity v3 显式来源事件 ID 入键 + 同事件修订 supersede | `identity_source_events` PASS（不同 event_id 两条、重放幂等、流式只留完整版） |
| 7 | Gemini 同时间戳永久漏采 | WP-D：复合 keyset `(last_modified_time, conversation_id)` | `gemini_same_timestamp` PASS（3 条全部入库，重复 sync=0） |
| 8 | 双时态链路不成立 | WP-C：任务展开/关联扩展透传 `as_of`，同秒窗口 `+1s` 闭合 | `as_of_task_expansion` PASS；`test_claim_temporal` 新增窗口用例 |
| 9 | 负反馈反而提升置信度 | WP-C：仅 `signal>0` 提升 confidence | `negative_feedback` PASS（0.50 → 0.50，不再上升） |
| 10 | 向量检索与同步竞态 | WP-E：RLock + 快照，搜索不随 remove 重绑定数组 | `vector_concurrent_sync` PASS（原 IndexError 消失） |
| 11 | JSONL 迁移丢 claim/procedural | WP-E：导出完整 claims/procedural/context/causal parents，导入可回灌 | `export_import_roundtrip` PASS（claims=2、procedural=1、无异常） |
| 12 | 会话摘要永久陈旧 | WP-D：游标记录消息数+last_at，新消息重新排队并 supersede 旧摘要 | `session_digest_freshness` PASS（pending 0→1→0，旧摘要 superseded） |
| 13 | `/metrics` 匿名开放、注入对照缺失 | WP-B + WP-F：`/metrics` 鉴权；HTTP 注入路径记录 `injected=true/false`、memory_ids、延迟、空结果原因 | `read_only_daemon` + 14 项 trace 测试；`/stats` 提供有界窗口 |

## 三、分工作包设计要点

### WP-A 注入边界安全
- 服务端统一契约：`escape_untrusted_text()` / `escapeUntrusted()` 将不可信正文中的 `<` 全部转义，涵盖大小写与空白变体的 `</MEMORY_DATA_UNTRUSTED >`、`<MEMORY_DATA nonce>`；DSH 侧新增 `clipEscaped()` 处理转义后的长度扩张，严格守住 `maxChars`。
- 旧 LLM agent 的 `DEFAULT_PROMPT` 只保留占位符，记忆正文在 `str.format` 前由受信任函数渲染；恶意内容仍在块内可见，但无法产生第二个闭合标签。
- 残留契约：`task` 参数仍视为受信任调用方输入（与审计范围一致），未在记忆正文边界内。

### WP-B 只读模式与观测面
- `SQLiteLedger(read_only=True)`：文件必须已存在；连接使用 `file:...?mode=ro` URI + `PRAGMA query_only=ON`；跳过 `_bootstrap` 迁移/WAL 切换，仅校验 schema 版本；`_transaction` 与写连接直接拒绝；`maintenance_checkpoint` 返回同形状 no-op。
- `run_daemon(read_only=True)` 仅启动纯读 entity index；watcher、discovery 自动安装、warmup、capture、distill、vector、sleep、metrics-trend、SIGHUP reload 全部跳过并在 `health["background"]` 记账。
- `close()` 通过跨线程连接注册表 + epoch 回收本进程所有 reader，读写线程退出不再泄漏连接。
- `injection_trace`/`metrics.jsonl`：0600、8 MiB 轮转（保留 `.1`）、256 KiB 尾部有界读取，健康检查不再随文件增长变慢。
- 取舍：只读镜像要求已初始化账本快照；`/metrics` 鉴权后仍按管理员视图输出聚合计数（只读镜像能显示真实规模）；真正只读挂载的 `immutable=1` 未采用（stale WAL 风险）。

### WP-C 裁决爆炸半径与双时态
- `resolve_claim()` 委托 `settle_claim_slot()`：作用域严格限定 `(scope, subject, predicate, slot_key)`；先 `_converge_claim_memory()` 释放被连带打成 conflict 的孤儿 claim，再判定败者记忆是否整体下线；返回兼容字段 `resolved/winner/losers`。
- 任务状态展开、关联扩展、因果祖先都做 `as_of` 窗口过滤；`causal_ancestors` 本身无 `as_of`（未改 graph.py），由 context_pack 侧后置过滤兜底并注明限制。
- `_supersede_window_end()` 在同一秒精度无法构造非零窗口时使用 `loser_from + 1s`，保证窗口非零且可闭合；`feedback()` 负信号不再抬 confidence。

### WP-D 采集与消化
- 采集游标升级为 `{offset, line, session_id, cwd, size}`；Codex `session_meta` 可在任意批次出现；证据行号使用文件绝对行号；文件截断时全量重置。
- Gemini 复合 keyset 修复同时间戳漏采，并兼容旧纯时间戳游标。
- 提炼继承来源 scope，metadata 带 `derived_from_raw_chat`/`untrusted_source`/`source_scope`；私有 raw 不再因提炼变成 shared。
- 会话摘要游标记录消息数 + last_at + memory_id；长会话改为摘要最新 400 条；新消息触发新版本并 `supersede` 旧摘要；dry-run 不推进游标。

### WP-E 身份 v3、迁移与并发
- `IDENTITY_VERSION=3`：显式 `event_id/external_id` 进入 raw 身份键；无 id 时与 v2 逐字节一致；同时计算 v2/v1 兼容探测，升级首轮不会重灌历史库。
- 流式修订：同一 external_id 内容变化时事务内插入新投影并退役旧行（lifecycle/superseded_by/valid_until/FTS/claims），旧半截版本不再参与召回。
- external_id 投影在 `metadata_json`（不新增 schema 列，无迁移风险）；代价是跨库按 external_id 精确查询无索引，产能路径有 bucket 查询足够。
- `export_rows` 携带完整 claims/procedural/context/causal parents/窗口字段，旧平铺字段保留兼容；导回 claims 数一致、procedural 可读。
- 向量索引 search/sync 先快照 ids/matrix/valid 再计算，remove/add 不再造成 `IndexError`；保存使用唯一临时文件 + fsync + `os.replace`；`semantic_space` 同样原子化。
- Cursor/Pi 采集记录新增稳定 `external_id` 与 revision，供身份链使用。

### WP-F 注入统一可观测性
- 新增 `record_server_injection()`：HTTP `/context-pack`（GET 与 POST 共用一条路径）每次决策写一行 trace，包含 `injected`、`items/chars`、`memory_ids`、`empty_reason`、`latency_ms`、`scope/task_id`、`error`；空结果、注入关闭、构建异常都有对照组。
- 只落 query 哈希/长度/记忆 ID，**绝不写 query 原文或记忆正文**；`/stats` 提供尾部有界窗口（count/injected/empty/errors/recent），受限凭据只能看自己的 recent 行。
- 未鉴权请求在鉴权阶段返回 401，不写 trace，避免把探测混入质量窗口。

### WP-G P2 工程债
- `integrations/query.py`：任务持久化改为读-合并-唯一临时文件-`os.replace` 原子替换；进程内 RLock + `flock` 稳定 sidecar 锁；派生 SQLite 改为单事务 upsert + 差集删除；重复更新幂等且不写盘。
- `commons.py` 双存储漂移未做架构重写（风险高），已记录建议：ledger 为唯一权威、commons 降级为可重建缓存、新增 reconcile 对账命令。

## 四、第一轮验收命令与结果（第二轮最终数据见 §八）

```bash
# 1) 全量回归（临时 HOME + 临时账本）
TEST_HOME=$(mktemp -d /tmp/hippo-final.XXXXXX)
HIPPOMIND_HOME=$TEST_HOME PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD \
  /usr/bin/python3 -m unittest discover -s tests -p "test_*.py"
# → Ran 1864 tests ... OK

# 2) 审计验收：13 条复现项
PYTHONPATH=$PWD /usr/bin/python3 scripts/audit_acceptance.py --json
# → passed=13 failed=0 exit=0

# 3) schema 迁移演练 / DSH 插件 / lint
/usr/bin/python3 tests/drill_schema_migration.py --json   # → ok=true
node --check integrations/dsh-plugin/lib/index.js         # → OK
node tests/test_dsh_plugin.mjs                            # → 7/7 cases ok
PATH=/Users/zps/Code/artemis/.venv/bin:$PATH bash scripts/lint.sh  # → OK（gate 90/90）
```

## 五、行为变化与兼容性

- `resolve_claim()`：从“整条记忆赢者通吃”收窄为“只裁决槽位”；返回字段 `resolved/winner/losers` 保留，另附 `crowned/retired/demoted/loser_memories/settled`。依赖旧整条下线行为的调用方需要显式改用其他 API。
- `--read-only`：现在要求已初始化的账本快照；空账本会构造失败而不是静默建库；后台写循环与 SIGHUP 重载不再改变 Agent 配置。
- `/metrics` 需要令牌（`/health` 仍免鉴权）；新增 `GET /context-pack`（与 POST 同语义、同鉴权）。
- 注入 trace 增加 `injected=false` 对照组与延迟字段；旧字段不变，`record()` 签名未动。
- distill 产物继承来源 scope；会话摘要新增版本化与 supersede；identity v3 对旧行保留 v2/v1 探测，重放不会重复入库。
- 文档已同步：`docs/ARCHITECTURE.md`（请求路径/只读/身份）、`docs/AUTOMATIC_MEMORY.md`（采集游标/Distill 参数/隔离/trace/诚实清单）、`docs/INJECTION.md`（trace 对照）、`docs/RELEASE.md`（`version --json` 已可用）、`docs/FEATURES.md`（`/metrics` 与 `GET /context-pack`）。

## 六、已知限制与后续建议

1. `commons.py` 独立库与主 ledger 的双存储漂移仍在；建议后续把 ledger 定为唯一权威、commons 降级缓存并加 reconcile 对账。
2. `graph.causal_ancestors` 无 `as_of`；历史切片的祖先可能欠收（context_pack 侧已做窗口过滤，不会泄漏未来事实）。
3. 跨库导入 causal parent 只在目标库存在同 id 父记忆时连边；如需跨库重建关系，需要按 parent event/内容解析目标 memory_id。
4. external_id 用 metadata 投影，无专用索引；若未来需要全库按事件 ID 查询，可加列/表达式索引并走一次 schema 迁移。
5. 注入“是否真的改善结果”仍只有离线复用率报告；本轮补齐了 HTTP 侧有注入/未注入对照组，线上因果验证仍需随机 A/B。
6. CI 仍只覆盖 Python 3.9（本机 ruff 未安装，lint 为 SKIP）；建议 CI 增加 3.13 与 macOS 矩阵。
7. 外置 ExFAT 卷会持续生成 `._*` AppleDouble 附属文件（已被 `.gitignore` 忽略）；`.git/objects/pack` 下的历史 AppleDouble 已备份并移入 `.git/appledouble-quarantine-20261113/`，`git fsck --full` 通过。

## 七、改动清单（按目录）

- **核心**：`hippomind/ledger/base.py`、`lifecycle.py`、`identity.py`、`clients.py`、`hippomind/daemon.py`、`context_pack.py`、`distill.py`、`session_digest.py`、`transcripts.py`、`gemini_capture.py`、`cursor_capture.py`、`pi_capture.py`、`vector_index.py`、`semantic_space.py`、`injection_trace.py`、`llm_agent.py`、`gui_api.py`。
- **接入**：`integrations/dsh-plugin/lib/index.js`、`integrations/query.py`。
- **脚本/验收**：`scripts/audit_acceptance.py`（新增）、`scripts/eval_refusal.py`（只读连接签名兼容）。
- **测试**：新增 `tests/test_injection_surface_security.py`、`test_dsh_plugin.mjs`、`test_read_only_ledger.py`、`test_metrics_auth.py`、`test_conflict_blast_radius.py`、`test_codex_cursor.py`、`test_gemini_keyset.py`、`test_distill_scope.py`、`test_session_digest_freshness.py`、`test_identity_v3.py`、`test_export_roundtrip.py`、`test_query_rewrite.py`；更新对应的既有测试（claim/temporal/as_of/conflict/read_only/metrics/injection_trace/identity_stable/vector_index 等）。
- **文档**：`docs/OPTIMIZATION_PLAN_2026-11-13.md`（新增，实施分解）、本报告，以及上节列出的 5 篇同步文档。
- **数据面**：未改动任何生产账本/真实 Agent 配置；所有演练均在临时目录完成。

## 八、第二轮增强（round 13.5：把已知缺口逐项关闭）

> 第一轮把审计缺陷清零后，按“离满分还差什么”的清单继续做了六个可验证的增强。
> 全部实现仍遵守同一约束：不读写生产账本、不修改真实 Agent 配置、只用临时目录与临时账本。

| 工作包 | 内容 | 验收证据 |
| --- | --- | --- |
| WP-H 因果祖先 as_of | `ledger/graph.causal_ancestors()` 新增可选 `as_of`：祖先窗口按 as_of 求值，quality/lifecycle 采用与历史检索一致的历史可见性；`context_pack` 透传 `options.as_of`，历史包不再混入当时尚不存在的祖先 | 新增 `tests/test_causal_as_of.py`（9 项）+ `test_as_of.py` 端到端 1 项；过去只返回当时可见祖先、未来祖先不泄漏、无 as_of 时逐字节兼容 |
| WP-I commons 双存储对账 | `commons.compare_shared_with_ledger()` / `repair_shared_with_ledger()`：按 memory_id 分桶 missing_in_ledger / missing_in_commons / stale / window_mismatch；`scripts/reconcile_commons.py` 默认只读、`--repair` 仅同步窗口字段；`stats()` 暴露 last_reconcile | 新增 `tests/test_commons_reconcile.py`（26 项）：分桶精确、默认零写入、repair 幂等、缺库不抛异常；真实 commons 库（2017 行）只读跑通且源库 sha256/size/mtime 不变 |
| WP-J CI 跨版本/跨平台与发布门禁 | ubuntu 矩阵 `3.9/3.11/3.12/3.13` + 新增 macOS(3.13) job；lint/drill/benchmark 升到 3.13；新增 release-gate job（node + acceptance + drill）；新增 `scripts/release_check.sh` 一键 7 步门禁；pyproject classifiers 同步 | CI YAML 语义自检通过；`release_check.sh` 本地全量 **PASS 7/7**（含 1915 tests）；3.13 全量 **1915 tests OK** |
| WP-K HTTP 注入随机 A/B | `/context-pack`（GET/POST 共用路径）接入既有 `injection_ab` 分桶：实验开启时 control 臂仍检索但投递空包、trace 记 `arm=control / injected=false / empty_reason=ab_control`，treatment 正常注入；默认关闭时行为与旧版完全一致；`scripts/injection_ab_report.py` 按 arm 统计注入率/延迟/复用率并在样本不足时给出所需 prompt 数 | 新增 `tests/test_injection_ab_http.py`（7 项）；trace/stats 分桶、默认关闭回归、报告脚本子进程校验；不落 query 原文/正文 |
| WP-L 冻结副本治理演练 | `scripts/governance_rehearsal.py`：SQLite backup API 生成一致快照、源文件 sha256/mtime/size 前后校验、frozen 永不写、dry-run 在一次性副本上真实模拟、`--apply` 仅允许副本；步骤含 repair_claim_states、valid_until 回填、重复折叠、maintain、FTS 收敛、identity 审计、可选 commons 只读对账 | 新增 `tests/test_governance_rehearsal.py`（8 项）；本机合成 5200 行账本：before 悬空 claim=26、缺失窗口=26、重复 raw 20 组/60 行 → after 全 0，8 步 ok，源与冻结快照字节不变；二次 apply 全 delta=0 |
| Python 3.13 兼容与停机 WAL | `textutil.coerce_random_seed()` + `hippocampus/drives` 适配（3.11+ 不再接受任意对象作 seed）；daemon 停机顺序重写：信号处理器快速返回、join 后台线程、有界重试 TRUNCATE checkpoint、先回收 reader 再 checkpoint、幂等；跨 SQLite 版本的侧车断言改为只比较数据文件 | 3.13 全量 1915 tests OK；sigterm WAL 归零测试在 3.9/3.13 各连跑通过；`tests/test_commons_reconcile` 在 3.9（SQLite 3.54）与 3.13（SQLite 3.50）均通过 |

### 8.1 第二轮之后仍存在的限制（诚实清单）

1. **生产部署未执行**：本机当前没有生产主账本/daemon（`~/Library/Application Support/HippoMind` 只有启动器与日志），因此无法在本环境做 `install-autostart.sh` + kickstart 的真实上线验证；发布步骤仍以 RELEASE/HANDOFF 为准，由部署环境执行。
2. **A/B 尚无真实流量样本**：基础设施与报告已就位（随机分桶、控制臂、arm 分桶 trace、样本缺口估算），但“注入是否改善结果”的结论仍需要每个 arm ≥30 条可评估样本的线上数据；不能把离线复用率当因果证明。
3. **关系边的历史拓扑**：`causal_ancestors(as_of=...)` 过滤了祖先记忆的窗口，但 `memory_relations.created_at` 未参与 as_of 求值；如需要“当时的图拓扑”，需另给关系边加时间窗。
4. **claim 级历史投影**：`context_pack` 的关联 claim 扩展仍以当前 claim state 为准（调用侧做窗口后置过滤）；历史切片的关联项可能欠收，但不会泄漏未来事实。
5. **commons repair 的边界**：`--repair` 只收敛窗口字段（superseded_by/valid_until）；stale/missing 两类仍需人工或后续同步任务，工具不自动删除/重建行。
6. **3.9 仍是支持地板**：CI 覆盖 3.9–3.13，但 ruff target-version 仍为 py39；未来支持 3.14 时需要同步矩阵与 seed/兼容审计。

### 8.2 最终验收（第二轮完成后复跑）

```bash
# 全量回归（两个解释器都跑）
TEST_HOME=$(mktemp -d /tmp/hippo-final.XXXXXX)
HIPPOMIND_HOME=$TEST_HOME PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /usr/bin/python3 -m unittest discover -s tests -p "test_*.py"
# → Ran 1915 tests ... OK
HIPPOMIND_HOME=$TEST_HOME PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /Users/zps/.workbuddy/binaries/python/versions/3.13.12/bin/python3 -m unittest discover -s tests -p "test_*.py"
# → Ran 1915 tests ... OK

# 一键发布门禁（compileall / 全量测试 / lint / drill / node / DSH 插件 / 审计验收）
bash scripts/release_check.sh
# → release check summary: 7 run, 7 passed, 0 failed, 0 skipped
# → RELEASE CHECK: PASS

# 审计 13 条复现项
PYTHONPATH=$PWD /usr/bin/python3 scripts/audit_acceptance.py --json
# → passed=13 failed=0

# 冻结副本治理演练（默认合成 5200 行；有真实快照时传 --source）
TEST_HOME=$(mktemp -d /tmp/hippo-gov.XXXXXX) HIPPOMIND_HOME=$TEST_HOME \
  /usr/bin/python3 scripts/governance_rehearsal.py --json
# → before 悬空 claim=26/缺失窗口=26/重复 raw 20 组 → after 全 0；源文件指纹前后一致

# commons 对账（默认只读；--repair 需显式）
/usr/bin/python3 scripts/reconcile_commons.py --json

# HTTP A/B 报告（实验开启并累积 trace 后）
/usr/bin/python3 scripts/injection_ab_report.py --json
```

### 8.3 第二轮改动清单

- **核心**：`hippomind/ledger/graph.py`、`hippomind/context_pack.py`、`hippomind/commons.py`、`hippomind/daemon.py`、`hippomind/injection_trace.py`、`hippomind/textutil.py`、`hippomind/hippocampus.py`、`hippomind/drives.py`。
- **脚本**：`scripts/reconcile_commons.py`、`scripts/injection_ab_report.py`、`scripts/governance_rehearsal.py`、`scripts/release_check.sh`（均为新增）。
- **测试**：`tests/test_causal_as_of.py`、`test_commons_reconcile.py`、`test_injection_ab_http.py`、`test_governance_rehearsal.py`（新增）；`test_as_of.py`、`test_commons_reconcile.py` 的跨 SQLite 版本断言更新。
- **工程**：`.github/workflows/ci.yml`（3.13 + macOS + release-gate）、`pyproject.toml`（classifiers）。
- **数据面**：仍未改动任何生产账本/真实 Agent 配置；治理演练只在合成账本或显式给定的冻结快照上执行。


## 九、第三轮增强（round 13.6：补理解层与证明层）

> 第二轮之后仍缺两块：短追问（继续/同上）没有语义、release gate 不证明检索有效。
> 本轮把这两块做成可验证的闭环，并顺手补了两个 P1 原型。

| 工作包 | 内容 | 验收证据 |
| --- | --- | --- |
| WP-M 会话感知查询消歧 | 新增 `hippomind/query_context.py`：延续词/零内容词判定、同一会话最近 1–2 条真实用户消息锚定（默认 180 分钟窗口）；Context Pack 的初始检索/任务锚点重检索/加深/混合重排全用锚点查询；session_id 从 daemon/hooks/DSH/CLI 透传；安全守卫：短但有内容词的请求不锚定、原查询含 ≥2 内容词时候选必须命中原词否则整池回退、`as_of` 历史切片不锚定 | 28 条 follow-up 夹具：follow-up 类命中 **5/18 → 18/18**；safety/normal 类 7 条锚定 0 次、pack 变化 0 条；普通查询开/关下 pack JSON（含 debug）逐字节相等；锚点查询 p95 **0.175ms**；35 项新测试 + CLI 面同步 |
| WP-N 基准式检索质量门禁 | 新增 `scripts/eval_benchmark.py`：fixture（memories+queries）建临时账本按类别评测，指标 recall@8/MRR/duplicate/update_hit/abstain/p95；自建 `benchmark_zh_v1.json`（48 queries，5 类）；阈值文件 + `--update-baseline`；官方 LongMemEval 子集适配器；接入 `release_check.sh` 第 8 步与 CI release-gate | 默认实测 recall **1.000** / MRR **0.9722** / update_hit **1.000** / abstain **1.000** / p95 **2–6ms**；gate PASS；20 项新测试；`release_check.sh --quick` 8 步全绿 |
| WP-O 事实卡原型 | 新增 `hippomind/fact_cards.py`：按 (subject,predicate,slot_key) 聚合当前事实 + 有界变更历史，支持 as_of，`render_fact_cards` 有界渲染，`card_coverage_stats` 供 A/B；本轮不接入注入链路 | 26 项测试；同夹具渲染 **325 字符 vs 原始 claims compact JSON 1767 字符（缩减 81.6%）**；只读契约（账本 sha256/mtime/size 不变） |
| WP-P trace 保留期 | `injection_trace` 新增 `HIPPOMIND_TRACE_RETENTION_DAYS`（默认 30 天，0 关闭），每小时最多清理一次；剔除过期行、坏行/缺 at 行保留；唯一临时文件 + 原子替换 + 0600；`.1` 轮转历史不清理；summary 暴露 retention 状态 | 15 项测试（含并发追加检测、replace 失败旧文件完好、模式保持） |

### 9.1 第三轮之后仍存在的限制（诚实清单）

1. **生产部署与真实数据治理仍未执行**：本机没有生产主账本/daemon；离线/冻结副本演练工具已就绪，但真实上线、真实库治理、24h soak 必须在部署环境完成。
2. **线上 A/B 仍无样本**：随机分桶、控制臂、arm trace、因果报告已就绪；需每臂 ≥30 条可评估样本的真实流量才能给“注入是否有效”的因果结论。
3. **官方基准尚未跑**：`eval_benchmark.py` 支持 `--format longmemeval` 子集，但仓库不内置官方数据（许可/体积），因此目前只有自建维度对齐集的回归门禁，不能对外声称 LongMemEval/LoCoMo 分数。
4. **follow-up 锚定是启发式**：延续词表/短句阈值可能漏判罕见表述；无 session_id 的宿主会退化为同 agent+时间窗（可能混入近 180 分钟内其他会话）。
5. **事实卡尚未接入注入链路**：本轮只交付模块与量化；接入需要决定卡片排序、token 预算与与 claim/摘要的去重关系（下一轮 A/B）。
6. **历史图拓扑与 claim 级投影**：`causal_ancestors` 只过滤祖先记忆窗口，关系边 `created_at` 未参与 as_of；关联 claim 扩展仍以当前 state 为准（欠收不泄漏）。
7. **commons repair 边界**：`--repair` 只收敛窗口字段；stale/missing 仍需人工或后续同步任务。
8. **3.9 仍是支持地板**：CI 覆盖 3.9–3.13；支持 3.14 时需同步矩阵与兼容审计。

### 9.2 第三轮最终验收

``bash
# 全量回归（3.9 与 3.13 各跑一遍）
TEST_HOME=$(mktemp -d /tmp/hippo-final.XXXXXX)
HIPPOMIND_HOME=$TEST_HOME PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /usr/bin/python3 -m unittest discover -s tests -p "test_*.py"
# → Ran 2012 tests ... OK
HIPPOMIND_HOME=$TEST_HOME PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /Users/zps/.workbuddy/binaries/python/versions/3.13.12/bin/python3 -m unittest discover -s tests -p "test_*.py"
# → Ran 2012 tests ... OK

# 一键发布门禁（compileall / 全量 / lint / drill / node / DSH / acceptance / 检索质量）
bash scripts/release_check.sh
# → release check summary: 8 run, 8 passed, 0 failed, 0 skipped → PASS

# 检索质量门禁（独立运行，含分类数字）
PYTHONPATH=$PWD /usr/bin/python3 scripts/eval_benchmark.py --json

# follow-up 消歧验收（28 条夹具回放）
PYTHONPATH=$PWD /usr/bin/python3 -m unittest tests.test_query_context -v
``

### 9.3 第三轮改动清单

- **核心**：`hippomind/query_context.py`（新）、`hippomind/context_pack.py`、`hippomind/daemon.py`、`hippomind/auto_context.py`、`hippomind/cli/__init__.py`、`hippomind/cli/parser.py`、`hippomind/cli/cmd_read.py`、`hippomind/codex_hook.py`、`hippomind/claude_hook.py`、`hippomind/injection_trace.py`、`hippomind/fact_cards.py`（新）、`integrations/dsh-plugin/lib/index.js`。
- **脚本/门禁**：`scripts/eval_benchmark.py`（新）、`scripts/release_check.sh`、`.github/workflows/ci.yml`。
- **夹具/测试**：`tests/fixtures/retrieval/benchmark_zh_v1.json`、`benchmark_baseline.json`、`followup_zh.json`、`tests/test_query_context.py`、`tests/test_eval_benchmark.py`、`tests/test_fact_cards.py`、`tests/test_injection_trace_retention.py`（均新增）；`tests/test_as_of.py`、`tests/test_cli_package.py` 更新。
- **文档**：`docs/ARCHITECTURE.md`（follow-up 锚定契约）、`docs/AUTOMATIC_MEMORY.md`（3.1b 会话锚定 + trace 保留期）、`docs/EVAL.md`（benchmark 门禁）、本报告与 HANDOFF。
- **数据面**：仍未改动任何生产账本/真实 Agent 配置；所有基准与 follow-up 回放都在临时账本完成。


## 十、第四轮增强（round 13.7：可解释性、事实卡接线、成本观测、灾备演练）

> 第三轮之后，最贵的三件事是：出问题时“看不见为什么”、事实卡只有原型、发布门禁不演练灾备。
> 本轮把这三件补齐，并把解释能力做成可脚本的 CLI。

| 工作包 | 内容 | 验收证据 |
| --- | --- | --- |
| WP-Q 事实卡 opt-in 接入 | `fact_cards.build_fact_cards` 增加 memory_ids 过滤；`ContextPackOptions.fact_cards / max_fact_card_chars`；pack 文本前置有界“当前事实卡”块（与已选文本去重、不挤掉原文）；自动注入路径由 `HIPPOMIND_FACT_CARDS` 环境开关控制（默认关闭，行为逐字节不变），daemon body/GET 可覆盖 | 35 项测试（含默认关闭逐字节相等、去重、as_of、极小预算）；同夹具 82→141 字符、当前事实显式出现、去重命中 50%/专属夹具 100% |
| WP-R hippomind explain | 新增离线只读 CLI：components/candidates/score_parts/filtered reasons/selected/empty_reason/gate/fact_cards/--trace；人类输出与 `--json` 同源；exit 0（空包正常）/2（参数/账本错误） | 17 项测试；3.9/3.13 手动 CLI 验证；PARSER_SURFACE 同步（44 子命令）；账本 sha256/mtime 不变 |
| WP-S 成本/延迟指标 | `distill.run()` 新增 extractor 调用/失败/耗时/batch/items/fallback 计数与 `cost_stats()`；`llm_client` 新增 calls/success/failure/timeout/parse/ms 统计（返回值/重试/超时行为不变） | 10 项新测试 + 既有 130 项 distill/llm 回归 OK；固定输出逐字节不变 |
| A4 灾备演练 | 新增 `scripts/backup_restore_drill.sh`：合成账本 → backup（sha 侧车）→ 变更 → restore → checkup → 篡改快照拒绝且账本不变；接入 `release_check.sh` 第 9 步与 CI release-gate | 3 项测试 + 本地 drill PASS；`release_check.sh` **9/9**；CI release-gate 增加该步骤 |
| 文档同步 | FEATURES 子命令 44 + explain 行；RELEASE 灾备演练清单；USAGE_GUIDE 现代验证入口与 explain/benchmark/drill 命令 | `tests.test_doc_claims` 5 项全绿（计数/存在性守卫） |

### 10.1 第四轮之后仍存在的限制

1. **生产部署/真实数据治理仍未执行**：本机无生产主账本与 daemon；灾备演练是合成账本，真实恢复需在部署环境按 RELEASE 清单执行。
2. **线上 A/B 仍无样本**：分桶/控制臂/arm trace/报告就绪，但因果结论仍需真实流量。
3. **官方基准未跑**：`eval_benchmark.py` 支持 LongMemEval 子集适配，但仓库不内置官方数据；当前只有自建维度对齐集的回归门禁。
4. **事实卡仍是 opt-in**：默认关闭、按字面去重、未按 token 预算折算；开启前建议先跑一次 A/B（当前只是“当前事实显式、历史可辨”的增量，不宣称 token 净节省）。
5. **explain 复用了 auto_context 的私有组件加载 helper**：后续重构该模块时需同步；输出解释的是“检索路径”，不含 daemon 侧注入开关/限流等状态。
6. **成本/延迟指标尚未接到 /stats 面板**：`cost_stats()` 与 `llm_client.stats()` 已可用，接线是下一轮小项。
7. 历史图拓扑/claim 级投影、commons stale/missing 自动修复、Cursor 真机端到端仍未闭环（同前几轮）。

### 10.2 第四轮最终验收

``bash
# 全量回归（两个解释器）
TEST_HOME=$(mktemp -d /tmp/hippo-final.XXXXXX)
HIPPOMIND_HOME=$TEST_HOME PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /usr/bin/python3 -m unittest discover -s tests -p "test_*.py"
# → Ran 2066 tests ... OK
HIPPOMIND_HOME=$TEST_HOME PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD /Users/zps/.workbuddy/binaries/python/versions/3.13.12/bin/python3 -m unittest discover -s tests -p "test_*.py"
# → Ran 2066 tests ... OK

# 9 步发布门禁
bash scripts/release_check.sh
# → release check summary: 9 run, 9 passed, 0 failed, 0 skipped → PASS

# 解释一次检索（离线只读）
python3 -m hippomind explain "继续" --json

# 灾备演练
bash scripts/backup_restore_drill.sh --json
``

### 10.3 第四轮改动清单

- **核心**：`hippomind/fact_cards.py`、`hippomind/context_pack.py`、`hippomind/auto_context.py`、`hippomind/daemon.py`、`hippomind/distill.py`、`hippomind/llm_client.py`。
- **CLI**：`hippomind/cli/cmd_explain.py`（新）、`hippomind/cli/parser.py`、`hippomind/cli/__init__.py`。
- **脚本/门禁**：`scripts/backup_restore_drill.sh`（新）、`scripts/release_check.sh`、`.github/workflows/ci.yml`。
- **测试**：`tests/test_fact_card_injection.py`、`test_explain_cli.py`、`test_distill_cost_metrics.py`、`test_backup_restore_drill.py`（新）；`test_fact_cards.py`、`test_cli_package.py` 更新。
- **文档**：`docs/FEATURES.md`、`docs/RELEASE.md`、`USAGE_GUIDE.md`、本报告与 HANDOFF。
- **数据面**：未改动任何生产账本/真实 Agent 配置；explain 只读打开，灾备演练用合成账本。
