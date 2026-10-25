# HippoMind 全面体检与完善方案

> 体检时间：2026-10-24 22:30 PDT
> 账本：267,914 记忆 / 2,989 active / 4,486 active claims / 882 MB
> 对照基准：mem0 v3（2026-04）、Zep/Graphiti、LongMemEval、LoCoMo

---

## 一、先说结论

系统**主干是健康的**：检索/注入/采集/提炼/自愈都跑得通，1226 测试全绿，
注入面 23/23 健康。但有 **1 个必须立刻修的真 bug**、**3 个结构性缺口**、
**2 个无法自证的能力**。

| 类别 | 项 | 严重度 |
| --- | --- | --- |
| 🔴 真 bug | 1,535 条 claim 挂在已隔离的记忆上（悬空） | 高 |
| 🟠 结构缺口 | 时间推理只有 `valid_from`、`valid_until` 全空 | 高 |
| 🟠 结构缺口 | 实体链接只有 `related_to` 一种边，覆盖 45% | 中 |
| 🟠 结构缺口 | 没有可解释的遗忘策略（`utility<1` 是 100%） | 中 |
| 🟡 无法自证 | 没有公开基准（LongMemEval/LoCoMo）的可比数字 | 中 |
| 🟡 无法自证 | 注入效果可用样本 = 0 | 中 |

---

## 二、🔴 必须立刻修的 bug：1,535 条悬空 claim

### 现象

```
active claims              4,486
挂在 quarantine 记忆上      1,535   ← round 10 数据清理的遗留
挂在非 active 记忆上        1,671
health 警告："活跃 claim 4486 条，悬空 1535 条"
```

### 根因

round 10 的 `data_hygiene.py --apply` 把 4,543 条重复记忆降级为
`quality_state='quarantine'`，**但没有同步处理它们的 claim**。
claim 的 `state` 仍是 `active`，于是：

* `repair_claim_states()` 认为它们有效（只看 claim 自己的 state）
* 检索时它们不会被召回（记忆已 quarantine）
* 但冲突检测、slot 占用、claim 统计全部把它们算进去

**危害**：一个已隔离记忆的 claim 会继续占着 slot，导致新记忆的 claim
被误判成「与它冲突」而进 conflict 队列。这是**隐性数据污染**。

### 修复方案

在 `lifecycle.repair_claim_states()` 增加一条不变式：

```python
# claim 的 state 必须与它所属记忆的可召回性一致。
# 记忆被隔离/下线后，它的 claim 不可能再被召回，继续标 active
# 只会占 slot、制造假冲突。这里统一收敛。
UPDATE memory_claims SET state='superseded'
 WHERE state='active'
   AND memory_id IN (SELECT memory_id FROM memories
                    WHERE quality_state!='accepted' OR lifecycle!='active')
```

并把这个不变式做成 **health 检查项**（现在只是 warning 文案），
以及 `data_hygiene.py` 清理记忆时**同步收敛 claim**。

验收：`health` 的悬空数归 0，`conflict` 不增加（证明没有误伤）。

---

## 三、🟠 结构性缺口 1：时间推理几乎没实现

### 现状（实测）

```
claims 总数              4,771
valid_from 非空          4,771  (100%)
valid_until 非空             0  (0%)    ← 关键
```

所有 claim 的 `valid_until` 都是空。这意味着系统**能回答「什么时候开始的」，
不能回答「什么时候失效的」**。

### 对照 Zep/Graphiti

Zep 的核心设计是每条事实都有 **validity window**：

> "each fact in a context graph has a validity window: when it became true,
> and when (if ever) it was superseded"

它的论文（arXiv 2501.13956）明确把「temporally-aware knowledge graph」
作为相对 RAG 的核心优势。

### 为什么这很重要

没有 `valid_until`，`as_of` 双时态查询就是**假的**——
`ledger.search(as_of=...)` 只能按 `valid_from` 过滤，「当时这条还成立吗」无法回答。

### 修复方案

1. **claim 被 supersede 时写入 `valid_until`**（现在只改 state）：
   ```python
   # settle_claim_slot / resolve_claim 里，败者下线时补上失效时间。
   # 没有它，双时态查询只能答「何时开始」，答不了「何时失效」。
   UPDATE memory_claims SET valid_until = <now> WHERE claim_id = ?
   ```
2. **回填历史**：对已有 `state='superseded'` 的 claim，用它的
   `superseded_by` 指向的胜者的 `valid_from` 作为近似 `valid_until`。
3. **评测**：加一条 `as_of` 查询门禁——「问 2026-10-20 时 X 是什么状态」
   必须返回当时的 claim 而不是现在的。

---

## 四、🟠 结构性缺口 2：实体链接太弱

### 现状（实测）

```
memory_relations 总数      2,020
关系类型                   只有 related_to 一种
有关系的 active 记忆        1,347 / 2,989 = 45.1%
```

### 对照 mem0 v3

mem0 2026-04 的新算法把 **entity linking 作为第三路检索信号**：

> "Entity linking -- entities are extracted, embedded, and linked across
> memories for retrieval boosting."
> "Multi-signal retrieval -- semantic, BM25 keyword, and entity matching
> scored in parallel and fused."

也就是说 mem0 的检索是 **3 路融合**（语义 + BM25 + 实体），
HippoMind 现在是 **2 路**（词法 + 向量），实体只用于事后联想扩展。

### 修复方案

1. **把实体做成检索的第三路信号**，而不是只做图扩展：
   - 从 claim 的 `subject` + `slot_key` 抽实体（已有数据，不需要新模型）
   - 建 `entity → memory_ids` 倒排
   - 查询时抽实体，命中的记忆参与 RRF 融合
2. **关系类型细化**：现在只有 `related_to`。至少区分
   `same_subject` / `supersedes` / `derived_from` / `same_task`。
   这直接影响「联想扩展」的质量——现在的扩展是无差别的。

预期收益：实体信号对**专有名词类查询**（项目名、设备代号、版本号）
帮助最大，而这正是当前 semantic 集最弱的一类（hippomind 类 recall 0.06-0.13）。

---

## 五、🟠 结构性缺口 3：没有可解释的遗忘

### 现状（实测）

```
active 记忆 utility<1.0     2,989 / 2,989 = 100%   ← 全部被衰减过
cold 归档                   255,817
```

`utility` 全部 < 1，说明衰减逻辑在跑，但**没有任何可解释的策略**：
没有「为什么这条被降权」「为什么这条被归档」的可查记录。

### 对照

mem0 的做法相反——**ADD-only，永不覆盖**：

> "Single-pass ADD-only extraction -- one LLM call, no UPDATE/DELETE.
> Memories accumulate; nothing is overwritten."

它把「遗忘」交给检索排序，而不是删除。这个取舍值得借鉴：
**删除是不可逆的，降权是可逆的**。

### 修复方案

1. **不删除，只降权**：现有 cold 机制已经是对的，但需要
   **可解释性**——每条记忆的 `utility` 变化要能追溯原因。
2. **加遗忘审计**：`scripts/forgetting_report.py` 输出
   「本周哪些记忆被降权/归档，依据是什么」。
3. **保留恢复路径**：cold 记忆应能被一次显式查询召回（现在 `include_cold`
   默认 True，这点是对的）。

---

## 六、🟡 无法自证 1：没有公开基准的可比数字

### 现状

只有自建的 46 条 keyword + 53 条 semantic 查询。这些是**好工具**
（它们抓出过真实回归），但**无法与外部系统比较**。

### 对照

| 基准 | 测什么 | mem0 v3 得分 |
| --- | --- | --- |
| LoCoMo | 长对话记忆 | 92.5 |
| LongMemEval | 5 项长期记忆能力 | 94.4 |
| BEAM (1M) | 生产规模 | 64.1 |

LongMemEval（arXiv 2410.10813）明确测 5 项能力：
**信息抽取、多会话推理、时间推理、知识更新、拒答**。
后两项（知识更新、拒答）HippoMind **完全没有测过**。

### 修复方案

不一定要跑完整基准（成本高、需要英文语料），但**至少要补两项自测**：

1. **知识更新测试**：同一 slot 先写 A 后写 B，问「现在是什么」必须答 B，
   问「以前是什么」必须答 A。这直接测 `valid_until` 是否工作。
2. **拒答测试**：问账本里根本没有的东西，必须返回空而不是硬凑。
   现在只有 3 条手测（「今天天气」「1+1」），需要扩充到 20+ 条并进门禁。

---

## 七、🟡 无法自证 2：注入效果样本 = 0

这是**最诚实也最尴尬**的一条：机制建好了，但至今没有任何证据表明
「注入让 agent 表现更好」。

### 现状

```
最近 7 次注入 · 7 次实际注入
可用样本（有 memory_ids 且有后续回复）= 0
```

### 根因（已定位）

注入发生在 codex，而 codex 的 assistant 采集在 10-16 就停了（用户不再用）。
claude 每天有回复，但从未被注入过。两条证据链在时间上完全错开。

### 修复方案

1. **让 claude 走注入路径**——它是唯一每天有真实回复的 agent。
   现在 claude 装的是 MCP（被动查询），不是主动注入。
2. **随机化 A/B**：同一批查询随机分流注入/不注入，两边都写 trace
   （含 `injected:false`）。这需要改 `injection_trace` 的**写入时机**，
   不是格式。这是唯一能把「观察性」升级成「因果」的路径。

---

## 八、执行优先级

| 序 | 任务 | 预估 | 价值 |
| --- | --- | --- | --- |
| **1** | 修 1,535 条悬空 claim + 加不变式与 health 检查 | 小 | 消除隐性污染 |
| **2** | `valid_until` 写入 + 历史回填 + `as_of` 门禁 | 中 | 双时态从假变真 |
| **3** | 拒答测试扩到 20+ 条并进门禁 | 小 | 防硬凑，可度量 |
| **4** | 知识更新测试（A→B→问现在/问以前） | 小 | 直接验证时间推理 |
| **5** | 实体作为第三路检索信号 | 中 | 专有名词类查询提升 |
| **6** | 关系类型细化（4 类） | 中 | 联想扩展从无差别变有针对性 |
| **7** | 遗忘审计报告 | 小 | 可解释性 |
| **8** | claude 注入 + 随机化 A/B | 大 | 唯一能证明注入有效 |

**建议先做 1-4**（都是小改动、可立即验证），再做 5-6（结构性改进），
7-8 需要真实使用数据积累。

---

## 九、需要承认的边界

1. **mem0 的数字不可直接比较**：它的 92.5/94.4 跑在英文语料 + 托管平台
   + 生产级模型栈上。HippoMind 是中文个人助手 + 本地小模型。
   **借鉴设计，不要抄数字。**
2. **LongMemEval 需要 500 条英文问答**，与当前中文语料不匹配。
   建议只借鉴它的**能力维度划分**（抽取/多会话/时间/更新/拒答），自建中文集。
3. **实体链接需要 NER**。本机没有 NER 模型，但 claim 的 `subject` 已经是
   人工/模型抽好的实体名，可以直接用——这是本系统相对 mem0 的**结构优势**。
