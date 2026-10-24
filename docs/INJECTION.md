# 注入效果闭环（INJECTION）

自动注入有没有用？这份文档说明我们**怎么测**、**测到了什么**、以及
**哪些结论我们明确不敢下**。

一句话现状（2026-10-24 生产库）：**可用样本 0 条，因此没有任何效果结论。**

唯一一条记录了 `memory_ids` 的注入，其回复窗口内一条 assistant 消息都没有。
原因是**用户不再使用 Codex CLI**，不是采集故障（见 §8.1 的核实过程）。
要拿到结论，需要换一个真在用的 agent 来积累样本。

---

## 1. 数据从哪来

两条互不依赖的证据链：

| 来源 | 内容 | 局限 |
| --- | --- | --- |
| `~/Library/Application Support/HippoMind/injection.jsonl` | 每次自动注入一行：agent、来源、query 哈希、条目数、字符数、是否注入、`memory_ids` | 早期版本不写 `memory_ids`；只在真正注入时写，所以**没有"未注入"对照组** |
| 账本 `memories` 表 | 注入的 memory 原文、以及同一 agent 后续的 `raw_chat=1 AND role='assistant'` 消息 | 会话采集可能停止（本次就是） |

`scripts/injection_report.py` 用 **只读方式**（`mode=ro` + `PRAGMA query_only`）
打开生产账本，测量过程不可能改动生产数据。

---

## 2. 样本量（永远先看这个）

报告的第一节永远是样本账：

```
trace 总数            6
记录了 memory_ids 的  1
memory 仍存在         1
== 可用样本 ==        0   (阈值 30)
被跳过的原因：
  no_memory_ids       5  trace 未记录 memory_ids（早期版本不写该字段）
  no_replies          1  窗口内没有可读的 assistant 回复
```

一条 trace 只有**同时**满足下面三条才算"可用样本"：

1. 记录了非空 `memory_ids`；
2. 这些 memory 仍在账本里（没被遗忘/清除）；
3. 注入时刻之后 `window-minutes`（默认 30）内，**同一 agent** 至少有一条
   *可读的* assistant 消息。

"可读"会剔除三类噪声，它们不代表模型用到了记忆：

- harness 报错（`API Error: ... 429 ...`）；
- 结构化探针回复（`{"claims":[]}` —— 这是提炼器自己的输出）；
- 几乎没有信息量的短文本（`textutil.is_noise_text`）。

**阈值 `MIN_SAMPLES = 30`**。低于阈值时报告会打印
"样本量 N < 阈值 30，不足以支撑结论"，并且 `conclusive=False`。
30 是让比例估计的 95% 半宽落到 ~0.2 以下的常规下限，是**报告门槛，不是统计证明**。

---

## 3. 四个指标

分母永远是"可用样本数"，报告里每个比率旁边都印样本量。

### 3.1 overlap（词面复用）— 原代理指标

注入 memory 的 token 集合里，有多大比例在回复窗口内重新出现。
token 用 `textutil.tokenize_multilingual`（拉丁词 + CJK 单字/双字）。
`>= MIN_TERM_OVERLAP (0.15)` 记为"复用"。

保留它是为了和历史数据连续，但它的弱点很明显：中文双字重合几乎不说明任何问题。

### 3.2 citation（信号引用）— 主力指标

只统计**可机器核验的独特信号**，并按稀有度加权：

| 类型 | 例子 |
| --- | --- |
| `path` | `hippomind/ledger/base.py` |
| `file` | `injection_quality.py` |
| `code` | 反引号包起来的片段 |
| `url` | `https://...` |
| `version` | `v4.2.1` |
| `quoted` | `《双层 Context Pack》` |
| `ident` | `mem_abc123`、`SQLite`、`CamelCase` |
| `num` | `6000` |

普通散文**不算信号**：共享一个中文词说明不了什么，共享一个文件路径说明很多。

权重用 IDF：以账本里最近 `BACKGROUND_DOCS = 1200` 条 assistant 消息为背景语料，
算每个信号的出现文档数 `df`，权重 `log((N+1)/(df+1)) + 1`。
**越罕见的信号权重越高**——所以"每轮都出现的词"不会污染指标。

`citation_rate >= MIN_CITATION_RATE (0.20)` 记为"引用"。

### 3.3 paraphrase（语义复述）

注入 memory 与窗口内任一条回复之间的最大余弦相似度，使用账本自拟合的 LSA 模型
（`semantic_space.load_embedder()`）。没有模型文件时该指标自动关闭
（`enabled=false`），不影响其他指标。embedder 抛异常时按 0 处理，不打断报告。

### 3.4 correction（纠错）— **负向**信号

回复**同时**满足：(a) 命中注入内容的某个信号，(b) 含纠错标记
（"不对""已过时""改为""推翻""instead""deprecated"…），记一次纠错。

这不是"用得好"，而是"注入的内容可能是错的"。它单独统计，**不和复用率相加**。

---

## 4. 对照：能做的和不能做的

### 4.1 做不了的

生产 Hook **只在真正注入时写 trace**。所以"同一批查询有注入 vs 无注入"
这种自然实验**通常根本构造不出来**——没有"未注入"的记录可对照。
报告里的 `control.mode = "none"` 就是如实说这件事，而不是硬凑一个对照。

### 4.2 退一步做的

**匹配的未注入对照臂**（`control.mode = "matched_not_injected"`）：

对每条注入的 memory，从"同一时刻可检索、`kind` 相同、但**没有被注入**"的
memory 里，挑创建时间最接近的一条配对；用同样的 IDF 加权信号复用率算一遍，
两者之差就是 `lift`。

```
lift = 注入项的信号复用率 − 未注入同类项的信号复用率
```

关键实现细节（曾经踩过的坑）：对照池必须排除**所有** trace 里注入过的 memory，
不只是当前这条 trace 的。否则一条被注入的记忆会漏进对照臂，
把每个 lift 都悄悄压向 0。

`lift_mean > 0` 也只写"观察性关联"，绝不写因果。报告里
`control.observational = true`、`control.causal = false` 是硬编码的事实声明。

---

## 5. 结论分级（`verdict.code`）

| code | 含义 | `conclusive` |
| --- | --- | --- |
| `no_samples` | 可用样本 0，什么都不说 | false |
| `insufficient_sample` | 样本 < 30，比率只作排查线索 | false |
| `no_control` | 样本够但构造不出对照臂，只有单臂描述统计 | false |
| `no_lift` | 样本够，但注入项复用率**不高于**对照 | true（否定结论） |
| `observational_positive` | 样本够且 lift > 0，**观察性关联** | true（但非因果） |

任何情况下都会附 4 条 caveat，其中第一条固定是
"观察性证据：注入与复用之间的相关不等于因果"。

---

## 6. 诊断：样本为什么这么少

零样本只有可解释才有价值。而且**"采集坏了"和"这个 agent 没人用了"必须分开**——
前者要修 bug，后者只能换数据源，处置完全不同。

### 6.1 三种状态

报告第 5 节对每个出现在 trace 里的 agent 输出：

| verdict | 含义 | 该做什么 |
| --- | --- | --- |
| `replies_available` | 窗口内有可读回复，指标适用 | 正常看指标 |
| `capture_stalled` | 游标记录了 **last_error**，采集真的失败了 | **修 bug** |
| `source_idle` | 采集器在注入**之后**仍正常扫描，但磁盘上没有新消息 | **不是故障**，换一个在用的 agent |
| `capture_unverified` | 最近一次扫描**早于**注入，不能排除未读数据 | 先确认采集器是否在跑 |
| `capture_unknown` | 没有该 agent 的游标行 | 采集状态不可判定 |

判据来自 `source_cursors` 表，不需要读文件系统：

- `last_error` 非空 → `capture_stalled`（唯一能证明"坏了"的证据）；
- 否则取该 agent 所有 `transcript:<agent>:*` 游标的 `updated_at` 最大值：
  - 晚于注入 → `source_idle`（扫过了，确实没新内容）；
  - 早于注入 → `capture_unverified`。

### 6.2 一个必须小心的陷阱：`offset < size` 不是故障

游标里 `offset` 是已消费字节数，`size` 是扫描时看到的文件大小。
直觉上 `offset < size` 像是"没读完 = 有积压 = 坏了"——**这是错的**。

`transcripts.sync()` 每轮对每个文件只读最多 `max_per_file` 条消息，
所以一个大文件会**连续多轮**停在 `offset < size`，这是正常节流，不是故障。
生产库当前就有一个这样的游标（`transcript:codex:1dd92bc7603b0848`，
gap 约 55 MB），而它所属的采集器每轮都在正常推进。

因此实现里 `backlog` 只作为**信息性计数器**打印，**绝不参与 verdict 判定**。
（这条是初版实现写错过的地方，见 §8.2。）

### 6.3 当前输出

```
agent=codex
  最近注入            2026-10-24T11:02:05+00:00
  最近 assistant 消息  2026-10-16T08:49:52+00:00
  该 agent 累计回复    173804 条
  注入后 24h 内回复    0 条
  游标                 135 个 · 错误 0 · 未读完 1 · 最近扫描 2026-10-24T12:40:29Z
  结论                【不是故障】采集器在注入之后仍正常扫描，只是磁盘上没有新消息
                      —— 该 agent 近期没被使用。要积累样本只能换一个真在用的 agent。
  未读完提示           1 个文件尚未读完（采集器每轮限量读取，属正常节流，不是故障）
```

---

## 7. 怎么用

```bash
# 人类可读的分层报告（默认读生产账本，只读）
python3 scripts/injection_report.py

# 机器可读
python3 scripts/injection_report.py --json

# 换账本 / 换窗口 / 换阈值 / 关掉语义模型
python3 scripts/injection_report.py --ledger /tmp/hub.sqlite --window-minutes 60 \
    --min-samples 30 --no-embedder
```

代码里：

```python
from hippomind.injection_quality import ReadOnlyLedger, default_ledger_path, report

with ReadOnlyLedger(default_ledger_path()) as ledger:
    result = report(ledger, window_minutes=30)
print(result["sampled"], result["verdict"]["code"])
```

`report(..., traces=[...])` 可以直接喂入显式 trace 列表，测试与回放都用这个入口。

---

## 8. 当前结论（2026-10-24）

**没有效果结论，因为可用样本为 0。**

### 8.1 根因：用户不再用 Codex CLI（不是采集故障）

初版报告写的根因是"codex 的采集在 2026-10-16 就停了"。**这个结论是错的**，
经核实已修正。核实过的三条事实：

| 事实 | 数值 | 说明 |
| --- | --- | --- |
| `source_cursors` 中 `transcript:codex:*` 游标 | **135 个，全部当天更新，0 个 last_error** | 采集器每轮都在扫 |
| `~/.codex/sessions` 最新文件 | 2026-10-23 09:30，8268 字节，只有 1 行 `session_meta`、**0 条消息** | 采集器读了、游标推了、正确地没有产出记忆 |
| codex 记忆按天分布 | 10-16 有 8576 条，10-15 有 10395 条，10-14 有 10668 条，**10-17 之后为 0** | 那之后本机基本没再用 Codex CLI |

**所以是"没有新数据"，不是"采集坏了"。** 两者处置完全不同：
修 bug 修不出样本，只能换一个真在用的 agent。

### 8.2 旧结论是怎么推出来的（诚实记录推理链）

初版诊断只看账本里的 `MAX(created_at)`（codex 最后一条回复是 10-16），
就推断"采集在 10-16 停了"。**错在把"账本里没有新数据"直接等同于"采集停了"**——
中间漏掉了两种可能：采集器没在跑，或者跑了但磁盘上本来就没东西。

这个错误恰恰是本次要消除的混淆，所以我把诊断逻辑本身改掉了（§6.1）：
现在必须用游标证据区分 `capture_stalled` 与 `source_idle`，
而不是从回复表的沉默反推。**只看下游数据就下"上游坏了"的判断，是一种系统性的误诊模式。**

### 8.3 事实清单

| 事实 | 数值 | 来源 |
| --- | --- | --- |
| trace 总数 | 6 | `injection.jsonl` |
| 记录 `memory_ids` 的 trace | 1（2026-10-24T04:02:05-0700，8 条 memory） | 同上 |
| 可用样本 | **0** | 本报告 |
| codex 最近一条 assistant 消息 | 2026-10-16T08:49:52Z | 账本 |
| 该注入后 24h 内 codex 回复 | 0 条 | 账本 |
| codex 采集游标 | 135 个，0 错误，当天更新 | `source_cursors` |
| 对照臂 | 无法构造 | 本报告 |

所以：

- 不能说注入有效；
- 也不能说注入无效——**我们测不到**；
- 唯一能说的是：**度量链路已经建好，但数据供给断了，断在"没人用这个 agent"**。

### 8.4 下一步

1. **换数据源**：把注入与采集接到一个真在用的 agent 上（当前账本里 claude 每天有回复，
   但 claude 从未被注入过）。这是拿到样本的唯一途径。
2. 攒够 30 条可用样本后重跑报告。
3. 若届时 `lift_mean <= 0`，说明注入没有可测增益，应当收紧相关性门控
   （`HIPPOMIND_MIN_SCORE`）而不是继续加大注入量。
4. **不要**去"修 codex 采集"——它没坏。

---

## 9. 因果边界（必读）

这些指标全部是**观察性**的。具体来说：

1. **共同原因**：注入的 memory 是从同一批查询检索来的，回复本来就可能和查询同主题。
   信号重合可能只是"都在聊同一件事"。
2. **记忆的时序**：agent 可能在注入之前就知道这个文件路径/命令，注入只是恰好也提到了它。
3. **窗口污染**：30 分钟窗口里的其他工作会同时抬高或稀释所有指标。
4. **对照不是随机化**：匹配对照只在 `kind` 和时间上对齐，仍可能有选择偏差。
5. **选择性缺失**：trace 只在注入成功时写，失败/被门控拦截的轮次完全不可见。

要真正做因果，需要**随机化的 A/B**：同一批查询随机分流到"注入/不注入"，
两边都写 trace（含 `injected: false` 的行），再比较下游指标。
这需要改 `injection_trace` 的**写入时机**（不只是格式），属于后续工作，
本次刻意没有动它——现有 trace 不能破坏。

---

## 10. 相关文件

- `hippomind/injection_trace.py` — 写 trace（本次**未改动**，格式保持兼容）
- `hippomind/injection_quality.py` — 指标、对照、样本账、只读账本视图
- `scripts/injection_report.py` — 分层报告 CLI
- `tests/test_injection_quality.py` — 44 条纯离线测试（fake ledger + fake embedder + 合成游标）
- `docs/AUTOMATIC_MEMORY.md` §3 — 注入链路与 trace 字段