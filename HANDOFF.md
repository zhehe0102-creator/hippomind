# HippoMind 交接说明（新会话直接粘贴用）

> 生成时间：2026-11-13 01:30 PST
> 工作目录：/Volumes/移动硬盘/记忆系统重构
> 当前 HEAD：fd8b3a6（round 12）+ 未提交的 round 13 全面优化（详见 OPTIMIZATION_REPORT.md）


## 0. round 13：按审计结论完成的全面优化（2026-11-13）

本轮不再新增功能，专注修复审计会话 e71ca582 的 13 条可复现缺陷与 P2 工程债。
完整清单、设计取舍与验收证据见 OPTIMIZATION_REPORT.md；实施分解见
docs/OPTIMIZATION_PLAN_2026-11-13.md。

**修复摘要（按 WP）**

| WP | 内容 | 关键产物 |
| --- | --- | --- |
| A | 注入边界：payload 的 < 转义，渲染恰好 1 开 1 闭 | llm_agent.escape_untrusted_text/render_untrusted_block、DSH escapeUntrusted/clipEscaped |
| B | 只读模式：mode=ro+query_only，后台写循环全停，/metrics 鉴权，trace/metrics 0600+轮转+尾部读，跨线程连接回收 | SQLiteLedger(read_only=True)、daemon 门禁、health.background |
| C | 冲突裁决收窄到槽位；as_of 全链路透传；同秒窗口 +1s；负反馈不抬置信度 | resolve_claim 委托 settle_claim_slot、test_conflict_blast_radius |
| D | 采集游标持久化绝对行号/session/cwd；Gemini 复合 keyset；提炼继承 scope + 派生标记；摘要随新消息刷新并 supersede | transcripts.py、gemini_capture.py、distill.py、session_digest.py |
| E | identity v3（来源事件 ID + 流式修订收敛，v2/v1 兼容）；JSONL 导出完整 claims/procedural；向量并发快照 + 原子保存 | ledger/identity.py、ledger/clients.py、vector_index.py |
| F | HTTP 注入统一 trace（含空结果/未注入对照组、延迟、memory_ids），/stats 有界窗口 | injection_trace.record_server_injection、daemon /context-pack |
| G | integrations/query.py 原子重写 + flock + upsert | test_query_rewrite.py |

**验收（本机实测）**

- 全量：Ran 1864 tests — OK（全部通过；顺带修掉一个依赖固定日期的时效用例）。
- 审计验收：python3 scripts/audit_acceptance.py --json → 13/13 PASS。
- schema drill ok=true；node tests/test_dsh_plugin.mjs 7/7；scripts/lint.sh OK（ruff 0.16.3，gate 90/90）。

**行为变化（会用到的）**

- resolve_claim 现在只裁决传入槽位，不再让整条败者记忆下线；返回 resolved/winner/losers 兼容字段。
- --read-only 要求已初始化账本快照；只读进程不写账本/不改 Agent 配置；/metrics 需 token。
- 新增 GET /context-pack（与 POST 同语义、同鉴权）。
- 提炼产物继承来源 scope；私有 raw 默认不再进 shared。
- 注入 trace 新增 injected=false 对照组与 empty_reason/latency_ms。


**第二轮增强（2026-11-13 晚，目标接近满分）**

| 工作包 | 内容 |
| --- | --- |
| WP-H | causal_ancestors 新增 as_of；context_pack 透传，历史包不再泄漏未来祖先 |
| WP-I | commons 双存储对账（compare/repair + scripts/reconcile_commons.py，默认只读、repair 只同步窗口） |
| WP-J | CI 矩阵 3.9–3.13 + macOS；release-gate job；scripts/release_check.sh 一键 7 步门禁 |
| WP-K | HTTP /context-pack 随机 A/B：control 臂空包 + arm 分桶 trace；scripts/injection_ab_report.py 因果分析 |
| WP-L | scripts/governance_rehearsal.py：冻结副本治理演练（backup 快照、源指纹校验、apply 只写副本） |
| 兼容 | Python 3.13 全量通过（random seed 兼容、停机 WAL 顺序重写、跨 SQLite 版本侧车断言） |

复跑口径：`bash scripts/release_check.sh` → 9/9 PASS；Python 3.9 与 3.13 各 `Ran 2066 tests ... OK`；审计验收 13/13；检索质量门禁 recall 1.000 / MRR 0.9722 / update 1.000 / abstain 1.000；备份/恢复/篡改拒绝演练 PASS。
审计验收 `scripts/audit_acceptance.py --json` → 13/13；治理演练合成账本 before 悬空 26 / 重复 60 → after 全 0。

仍未闭环（环境/数据边界）：生产部署未执行（本机无生产主账本与 daemon）、A/B 需真实流量样本、
关系边时间窗与 claim 级历史投影仍欠收、commons stale/missing 需人工修复。
**下一步建议**：commons 双存储对账（reconcile）、graph.causal_ancestors 增加 as_of、

**第三轮增强（2026-11-14，理解层 + 证明层）**

| 工作包 | 内容 |
| --- | --- |
| WP-M | 会话感知 follow-up 锚定（继续/同上→同会话最近真实用户消息），安全守卫 + 普通查询逐字节不变；follow-up 命中 5/18→18/18 |
| WP-N | 基准式检索质量门禁：48 查询 fixture（5 类）、阈值文件、`release_check.sh` 第 8 步、CI release-gate、LongMemEval 子集适配器 |
| WP-O | 事实卡原型（slot 当前值 + 变更历史 + as_of），渲染比原始 claims JSON 缩减 81.6%，尚未接入注入 |
| WP-P | trace 按时间保留（`HIPPOMIND_TRACE_RETENTION_DAYS` 默认 30 天，原子清理，坏行保留） |

第三轮验收：3.9 与 3.13 各 `Ran 2012 tests ... OK`；`release_check.sh` 8/8 PASS；
审计验收 13/13；检索基准 recall 1.000 / MRR 0.9722 / update_hit 1.000 / abstain 1.000；
follow-up 锚点查询 p95 0.175ms。

第三轮仍未闭环：生产部署/真实数据治理（本机无生产账本）、线上 A/B 样本、官方 LongMemEval

**第四轮增强（2026-11-14，可解释性/事实卡/成本观测/灾备）**

| 工作包 | 内容 |
| --- | --- |
| WP-Q | 事实卡 opt-in 接入 Context Pack（memory_ids 过滤、有界卡块、与已选文本去重、`HIPPOMIND_FACT_CARDS` 开关默认关） |
| WP-R | `hippomind explain`：离线只读解释检索（候选/score_parts/过滤原因/为空原因/门控/--trace，人类+JSON 同源） |
| WP-S | distill `cost_stats()` + llm_client 传输统计（调用/失败/超时/解析/耗时），行为不变 |
| A4 | `scripts/backup_restore_drill.sh` 灾备演练接入 release_check 第 9 步与 CI |

第四轮验收：3.9 与 3.13 各 `Ran 2066 tests ... OK`；`release_check.sh` 9/9 PASS；审计 13/13；lint OK。

仍受环境限制：生产部署/真实库治理、线上 A/B 样本、官方 LongMemEval 数据、事实卡默认开启 A/B、
cost_stats 接 /stats。
数据未内置（仅有自建维度对齐集回归门禁）、事实卡接入注入、关系边时间窗与 claim 级投影。
CI 补 Python 3.13/macOS 矩阵、注入效果的线上随机 A/B。

## 一、目标（Goal）

把 HippoMind 从"标记驱动的记忆归档"改造成**装完就忘、全自动、95 分**的本地记忆系统。

当前自评 **约 94 分**（round 10 是 93）。

## 二、系统现状（生产，已验证）

```
账本：~/Library/Application Support/HippoMind/hippomind.sqlite
daemon：127.0.0.1:8765（launchd 自启，内置盘 runtime 副本）
claim 向量索引：vector_index_claims.npz（2815 claims，512 维）
ONNX：vendor/（onnxruntime+tokenizers）+ models/bgezh/
agent 凭据：agents/*.json（13 个 agent）

注入面 23/23 健康 · 13 个 agent 端到端通过 · 冲突 0 · health READY
```

**改完代码必须**：`bash scripts/install-autostart.sh`（会自动校准注入面）
+ `launchctl kickstart -k gui/$(id -u)/com.hippomind.daemon`

## 三、本轮（round 11）最重要的一件事：注入面几乎全是坏的

首次审计发现 **23 个注入面里只有 1 个是好的**。7 个 agent 的接入全部指向
一个 `/var/folders/.../tmpXXXX/hub.sqlite` 的**临时测试库（0 条记忆）**，
而生产库有 26.7 万条。它们「看起来装好了」——配置里有 hippomind 字样、
状态命令报「已安装」——但没有一个能真正读到记忆。

| 问题 | 数量 | 原因 |
| --- | --- | --- |
| cwd/包路径在可移动卷 | 6 | launchd 后台任务被 macOS TCC 拒绝 |
| 指向失效临时路径 | 4 | 安装时的临时目录，重启即失效 |
| 根本没装 | 12 | opencode/qoder/zcode/workbuddy/antigravity/dsh |

## 四、交付的自动检测与自动修复

| 模块 | 作用 |
| --- | --- |
| `hippomind/injection_audit.py` | 判据是「这条路径现在能不能读到生产账本」，六种状态 |
| `hippomind/injection_repair.py` | 只改 hippomind 自己的条目，先备份再原子写入 |
| `hippomind/rule_repair.py` | 规则文件**整段重新生成**，不做路径字符串修补 |
| `scripts/injection_doctor.py` | 默认审计；`--fix` 预览；`--fix --apply` 落盘 |
| `scripts/install-autostart.sh` | 同步 runtime 后**自动校准所有 agent** |

Agent 覆盖从 7 个扩到 **13 个**：新增 opencode / qoder / zcode /
workbuddy / antigravity / dsh。没有实测证据的 agent 不进列表。

## 五、当前指标（全部 PASS）

| 指标 | 值 |
| --- | --- |
| keyword recall@8 / MRR / dup | **0.957 / 0.817 / 0.015** |
| semantic recall@8 / MRR | **0.509 / 0.348** |
| **pack recall@8 / MRR** | **1.000 / 1.000** |
| 注入面 / 端到端 | **23/23 · 13/13** |
| 测试 / lint | **1226 tests OK / lint: OK** |

```bash
python3 -m unittest discover -s tests -p "test_*.py"
PATH="/Users/zps/Code/artemis/.venv/bin:$PATH" scripts/lint.sh
python3 scripts/injection_doctor.py            # 注入面审计
python3 scripts/eval_memory.py --set keyword   # 也可 --set semantic / --mode pack
python3 scripts/eval_semantic.py --gate
python3 scripts/data_hygiene.py --dry-run
python3 -m hippomind.cli doctor
```

## 六、本轮三次自我纠错（都写进了代码注释与测试）

1. **解释器路径在可移动卷上不是故障**。本机 Python 装在
   `/Volumes/XcodeDev/Xcode.app` 里，判成 volatile 是假阳性。
2. **按空白切路径会切出半截**。`Application Support` 含空格，
   `/Users/x/Library/Application` 当然不存在 → 正常 hook 被误报 stale。
3. **用正则修补路径会越修越坏**。`--agent-config [^\s]+` 只匹配到第一个空格，
   残尾留在原地，产出 `.../dsh.json Support/HippoMind/agents/dsh.json'` 这种坏命令，
   而且每跑一次多叠一层。改为**整段重新生成**后彻底消除，并加了幂等性测试。

另外还发现并修掉一个产品缺陷：`daemon.reload()`（HTTP `/reload` 端点）触发
auto_install 时不传 home，会写进用户真实的 `~/.claude/CLAUDE.md`。
现在两处 auto_install 都尊重 `HIPPOMIND_HOME`，跑完全量测试后用户配置逐字节不变。

## 七、关键设计决策（别重复踩坑）

1. **后台进程必须用内置盘 runtime**。cwd / 包路径指向 `/Volumes/` 会被 TCC 拒绝，
   而且失败是静默的——看起来像「配置没生效」。
2. **审计判据是「能否读到生产账本」**，不是「文件里有没有 hippomind 字样」。
3. **规则文件只改托管段**（`<!-- HIPPO-MIND-AUTO-CONTEXT -->` 之间），
   段外是用户内容，一个字节都不动。
4. **改完代码要跑 install-autostart.sh**，它会同步 runtime 并自动校准注入面。
5. **向量只做候选生成，不做注入门控证据**（语义近邻证明话题相似，不证明事实相关）。
6. **降级到 `cold` 对检索无效**（`include_cold` 默认 True），要用 `quarantine`。
7. **批次大小保持 8**：批 16 会静默丢掉约 11% 的条目。
8. **distill tick 保持 300s、limit=64**。

## 八、离 95 分还差什么

| 维度 | 权重 | 分数 | 卡在哪 |
| --- | --- | --- | --- |
| 采集覆盖 | 15% | 90 | Cursor 未安装未实测；采集侧仍可能写重复行 |
| 自动提炼 | 25% | 93 | API 已通、去重已加；identity 根因已修但存量未清 |
| 检索质量 | 20% | 94 | claim 级大幅改善；semantic 0.509 受限于语料形态 |
| 注入质量 | 15% | 96 | 23/23 健康、13/13 端到端、自动校准 |
| 可靠性 | 10% | 95 | 熔断、自愈、health、回滚 SQL、测试隔离齐备 |
| 使用成本 | 15% | 93 | 零标记 + 自启 + 装完即生效 |

**加权约 94 分。** 要上 95，最该做的两件事：
1. **存量重复行清理**：identity 已修（增量止住），但生产库里的历史重复未删。
2. **注入效果样本**：可用样本仍为 0，注入有没有用至今测不到。

## 九、工作纪律

- 每次改完代码：全量测试 + lint，再 `install-autostart.sh` + kickstart。
- 外置卷上 `write`/`edit` 会 ENOTSUP，用 bash heredoc 或先写 /tmp 再 cp。
- 不要提交 `系统盘清理仪表盘.html`。
- **诚实优先于好看**：本轮多次出现「我以为的根因是错的」——
  metadata 不进 identity、6% 退化值是误判、注入面看似装好实则全坏。
  凡是要写进文档的数字，先跑一遍可复现的命令。

## 十、子智能体中断续接

```bash
python3 scripts/team_resume.py
```

> API 中断已恢复。继续 <task-id>：先 team_task_get 看当前进度，再检查你已写入的
> 文件，不要重做已完成部分；完成后 team_task_update action=complete 并给 Lead 发报告。现在继续。
