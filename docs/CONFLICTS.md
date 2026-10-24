# 冲突裁决（CONFLICTS）：把 `state='conflict'` 从死队列变成可审计的闭环

Status: **active** — 本文件是 `scripts/conflict_review.py` 的策略说明与本次生产库
裁决记录。Owned files：`scripts/conflict_review.py`、`tests/test_conflict_review.py`、
本文件，以及 `hippomind/ledger/lifecycle.py` 里**只增不改**的槽位级方法。

## 结论先说（TL;DR）

生产库的 36 条 conflict claim **不是 36 个「谁对谁错」的争议**。按根因拆开：

| 根因 | 条数 | 含义 | 是否需要判断语义 |
| --- | --- | --- | --- |
| `duplicate` | 21 | 两造 `value_hash` 完全相同 | 否，纯属入库打分的产物 |
| `stale` | 8 | 对手方记忆早已 superseded/expired | 否，历史裁决已定 |
| `orphan` | 4 | 该 slot 只有一个 claim，没有对手 | 否，是连带下线 |
| `truncation` | 3 | 一条取值是另一条的截断前缀 | 否，保留完整那条 |
| `genuine` | 0 | 真·两种不同取值 | — |

一句话：**36 条里 0 条需要人来判断谁对谁错**。它们卡住不是因为难，而是因为
`quality_state='conflict'` 这个**记忆级**标志只有 `resolve_claim` 能清，而
`repair_claim_states` 又只在记忆已经 accepted+active 时才肯复活孤儿 claim —— 两边
互相等待，形成死锁。本次改动把这个环打断了。

## 为什么不能直接用 `hippomind resolve`

`resolve_claim` 是**记忆级**的：它会把败者记忆的**所有** claim 全部下线。实测生产库：

```
mem_9eb4f7328d  竞争 1 个槽位，另有 4 条无关 active claim
mem_b34d...     竞争 1 个槽位，另有 5 条无关 active claim
mem_e34d2ea5cf  竞争 4 个槽位，另有 5 条无关 active claim
```

也就是说逐条 `resolve` 会把「GitHub Radar 的技术栈」这类无关事实一起从召回里删掉。
所以 apply 走的是新增的**槽位级**方法，`resolve_claim` 与 `repair_claim_states`
的语义一个字没动。

## 三个可分离的阶段

### 1. inventory（只读）

```bash
python3 scripts/conflict_review.py            # 人读清单
python3 scripts/conflict_review.py --json     # 机器可读
```

按 `(scope, subject, predicate, slot_key)` 分组，每组展示两造的
`claim_id / memory_id / value / confidence / source_trust / asserted_at /
claim 状态 / 记忆状态 / 来源记忆摘要`，并给出 `cause`（上表根因）。

分组只装**活跃竞争**（`active` + `conflict`）；`superseded` 是历史，默认不展示
（`include_superseded=True` 可要）。只有一个活跃方的组**也会**返回 —— 它是孤儿，
需要被释放而不是被裁决。

### 2. adjudicate（决策器，绝不写库）

四种裁决：`supersede_a` / `supersede_b` / `keep_both` / `needs_human`。

**规则决策器（`--decider rules`，默认，离线）** 按根因分支，每个分支都在 reason 里
写明用了什么证据：

| cause | 裁决 | 依据 |
| --- | --- | --- |
| `duplicate` | `supersede_a` @0.95 | `value_hash` 相同 ⇒ 没有语义分歧，留证据更强的一方 |
| `truncation` | `supersede_a` @0.85 | 短的是长的前缀 ⇒ 留完整提取 |
| `stale` / `orphan` | `supersede_a` @0.90 | 只剩一方可召回 ⇒ 历史裁决的收敛 |
| `genuine` | `needs_human` @0.50 | 确定性代码不该猜，猜错等于删事实 |

**模型决策器（`--decider llm`）** 调本机 Claude Code CLI（与 `distill.py` 同一个），
只喂两条证据、不喂整库。**只有 `genuine` 组会调模型** —— 其余四种根因的证据已经
是结论性的，花一次限流调用只会增加延迟和新的出错方式。

### 3. apply（写库，必须显式 `--apply`）

```bash
python3 scripts/conflict_review.py --apply --decider rules
python3 scripts/conflict_review.py --apply --decider llm --limit 10
python3 scripts/conflict_review.py --apply --cause duplicate   # 只处理某类根因
```

| 裁决 | 动作 |
| --- | --- |
| `supersede_a/b` | `ledger.settle_claim_slot(claim, winner)` —— 只动这一个槽位 |
| `keep_both` | `ledger.release_claim_slot(claim)` —— 两侧都保留，谁都不删 |
| `needs_human` | **不动账本**，只留审计 |

`--dry-run` 是默认行为；`--apply` 与 `--dry-run` 互斥（同时给退出码 2）。

## 审计证据格式

每次裁决一行 `memory_claim_reviews`（首次 `--apply` 时 `CREATE TABLE IF NOT EXISTS`
自动建，**不**升 `SCHEMA_VERSION` —— 升版会让旧版本拒绝打开账本，而这张表只是可选元数据）：

| 列 | 内容 |
| --- | --- |
| `review_id` | `cr_` + sha256(槽位 + 两造 claim_id + 裁决 + 证据摘要) 前 32 位 |
| `scope/subject/predicate/slot_key` | 被裁决的槽位 |
| `cause` | 根因分类 |
| `claim_a/claim_b`、`memory_a/memory_b`、`value_hash_a/value_hash_b` | 两造身份 |
| `verdict` / `confidence` / `reason` | 判断、置信度、**理由（必填）** |
| `decider` / `model` | 谁说的（`rules` 或 `llm` + 模型名） |
| `evidence_digest` | 决策器**实际看到**的证据的 sha256 |
| `applied` / `outcome` | 是否落库、落库结果（失败也留痕，但 `applied=0`） |

`review_id` 是确定性的 + `INSERT OR REPLACE`，所以**重复 `--apply` 不会重复写行**。
账本自己还会写 `resolve` / `release` 审计事件，两条链可互相核对。

查看：`python3 scripts/conflict_review.py --show-reviews`。

**没有任何一条路径会删数据。** 败者的 claim 变 `superseded`（谱系保留，
`superseded_by` 指向胜者）；`keep_both` 连 supersede 都不做。

## 熔断与回退

`ClaudeDecider` 复用了 `distill.ClaudeExtractor` 的思路：

- 连续 `BREAKER_THRESHOLD=2` 次失败 ⇒ 停 `COOLDOWN_SECONDS=600`，本轮余下全部走规则；
- 单次调用有 `--timeout`（默认 60s）硬上限；
- 模型输出**必须**是四种裁决之一，否则算失败（不能把没见过的字符串强转成破坏性动作）；
- 模型给的 `supersede` 置信度低于 `--min-confidence`（默认 0.60）⇒ 自动降级为 `needs_human`。

本机实测：`claude -p` 在上游 API 不可达时会**无限挂起**（默认模型 `deepseek-v4.1-flash`
不在该版本模型目录里，触发 `[claude-code:unrecognized_model]`，随后 ECONNRESET）。
熔断 + timeout 就是为这个场景准备的。

## 新增的槽位级账本方法（`hippomind/ledger/lifecycle.py`，只增不改）

| 方法 | 作用 |
| --- | --- |
| `settle_claim_slot(claim_id, winner_memory_id='')` | 只在**该槽位**内定胜负；败者记忆仍有其它 active claim 时**不**整体下线 |
| `release_claim_slot(claim_id)` | keep-both：把该槽位两侧都放回 `active`，谁都不删 |
| `_converge_claim_memory(conn, memory_ids, now)` | 把记忆级 `quality_state` 标志收敛到不变量（见下） |

### 不变量与死锁

`claims_consistency_report` 检查的是：**active claim ⇒ 其记忆为 accepted**。

由于 `quality_state` 是记忆级的，一条记忆只要还有**任何**一个 conflict claim，
它的所有 claim 都不能是 active。这就产生了两难：

1. 想释放一个孤儿 claim，得先把记忆置回 `accepted`；
2. 想置回 `accepted`，得先没有 conflict claim。

`_converge_claim_memory` 用不动点收敛打破它，每条记忆两步：

1. 把**本槽位没有活跃对手**的 conflict claim 放回 active（它是连带下线，不是分歧）；
2. 再按剩下的情况重算标志 —— 还有冲突就保留标志且该记忆不得有 active claim；
   没了就清成 `accepted`。

`release_claim_slot` 还会主动**拒绝**释放「该记忆在别的槽位仍有冲突」的 claim，
因为那会制造 dangling active claim（`claims_consistency_report` 会报 warn）。

## 复现与验证

```bash
# 纯离线测试（fake LLM 注入，不 spawn 进程、不联网）
python3 -m unittest tests.test_conflict_review -v

# 全量 + lint
python3 -m unittest discover -s tests -p "test_*.py"
PATH="/Users/zps/Code/artemis/.venv/bin:$PATH" scripts/lint.sh

# 生产库：先看，再动
python3 scripts/conflict_review.py
python3 scripts/conflict_review.py --apply
python3 -m hippomind.cli doctor
python3 scripts/conflict_review.py --show-reviews
```

测试覆盖：四种裁决、`--dry-run` 不写库、幂等（重复 apply 不重复写）、
写失败不破坏状态、熔断、低置信度降级、未知裁决拒绝、
**败者其它槽位事实保留**、`keep_both` 不删任何一侧、以及 `resolve_claim` 旧语义未变。

## 本次生产库裁决结果

见下节「本次运行记录」（由实际 `--apply` 输出回填）。

## 本次运行记录（2026-10-24，生产账本）

备份：`output/backup-pre-conflict-review.sqlite`（909,922,304 bytes，
sha256 `53d973d8…d73b606`，走 SQLite online backup API，不是 `cp`）。

### 裁决前后

| 指标 | 前 | 后 |
| --- | --- | --- |
| `state='conflict'` 的 claim | **36** | **0** |
| `state='active'` 的 claim | 2323 | 2322 |
| `state='superseded'` 的 claim | 98 | 135 |
| `quality_state='conflict'` 的记忆 | 2 | **0** |
| 悬空 active claim（doctor/claims_consistency） | 0 | **0** |
| `memory_claim_reviews` 审计行 | — | 36 |

`doctor` 结论仍为 `READY`，privacy 那条 WARN 是既有的（32615 条 tool_output），与本次无关。

### 36 组的最终归属

| 根因 | 组数 | 裁决 | 说明 |
| --- | --- | --- | --- |
| `duplicate` | 21 | `supersede_a` @0.95 | 保留证据更强的一方 |
| `orphan` | 4 | `supersede_a` @0.90 | 唯一活跃方收回 |
| `truncation` | 3 | `supersede_a` @0.85 | 保留完整提取 |
| `stale` | 8 | `keep_both` @0.90 | 记忆早已 superseded，只清 claim 账，不动召回 |
| `genuine` | 0 | — | — |

**`needs_human` 剩 0 条。** 理由：36 组里没有任何一组是「两种都成立的不同取值」——
21 组两边 `value_hash` 完全相同，3 组是截断前缀，8 组挂在已死记忆上，4 组根本没有对手。
这不是「模型判对了」，而是**这些冲突本来就不是语义争议**，用确定性规则就能给出一致的、
可复核的归属。真正需要人来判断的语义分歧在这一批里一条都没有。

### 与最初判断的差异（如实记录）

初次分诊时我把 21 条写成「入库打分的产物」，这个说法**不够准确**。真实成因是
**记忆级标志的连带下线**：一条记忆在某个槽位输了，它的**其它** claim 被一起拉进
冲突队列，其中有些取值与外面的活跃 claim 完全一致 —— 于是看起来像「重复值冲突」。
测试里我按真实 `ingest` 路径复现了这个形状，而不是直接改库造数据。

另有一个分类 bug 也是在这批数据上暴露并修掉的：某个槽位有**三**个活跃方时，
最初只比较最强的前两方，而这两方恰好取值相同，于是把「两个一致 + 一个措辞略长」
误判成 `genuine`。现在比较的是**所有不同取值**，并有回归测试锁住。

### 两次 apply 的原因

第一次 `--apply` 处理掉 28 组（21 duplicate + 4 orphan + 3 truncation），剩 8 组 `stale`。
那 8 组的记忆早已 `superseded`，`release_claim_slot` 当时**正确地**拒绝把它们放回 `active`
（否则会制造 dangling claim），但也**没有**清账，所以队列里留着 8 条永远不会被处理的记录。

修法是让 `_converge_claim_memory` 在「记忆已不在 active 生命周期」时，把它的 conflict
claim 直接记为 `superseded`：这类 claim 本来就不可能被召回，清账**不改变任何召回决策**，
而且 claim 行是 UPDATE 不是 DELETE，谱系完整。第二次 apply 把这 8 组清完，冲突归零。

同时修正了报告：之前这 8 组会打印 `applied=0`（说「没做事」）但账本其实变了 ——
现在如实报告 `slot_cleared=True` + `stale_retired`。
