# 数据卫生：无意义记忆的检出、处置与防再产生

> 首次执行：2026-10-24 · 账本 `~/Library/Application Support/HippoMind/hippomind.sqlite`
> 工具：`python3 scripts/data_hygiene.py`（默认 dry-run）

本文记录三类污染、它们对指标的**真实**影响、处置方式，以及为什么用降级而不是删除。

---

## 一、两类污染，作用机制完全不同

### 1.1 噪音空壳（noise）

| reason | 条数 | 例子 |
| --- | --- | --- |
| `self_prompt` | 332 | `你是 HippoMind 的记忆提炼器。只输出 JSON...` |
| `empty_payload` | 46 | `{"claims":[]}` |
| `noise` | 24 | `Run:` |

这些是采集/提炼把**自己的中间产物**当成会话内容存了下来。它们永远没用，
但它们**不是** duplicate_rate 失败的原因——原因见下一节。

### 1.2 重复冗余（duplicates）—— 真正的元凶

```
可召回行（accepted + active）   7,474
其中不同内容                    3,216
内容重复率                      57.0%
```

最极端的一行内容被存了 **141 份**，`evidence_json` 完全相同，
但 `metadata_json` 有 141 个不同版本。

`eval_memory.py` 的 `duplicate_rate` 度量的是**同一个查询的 top-k 里有多少重复内容**。
当一条记忆存在 N 份副本时，top-8 里可能挤进 5 条一模一样的文本，
重复率飙升，同时真正不同的结果被挤出榜单——所以 recall 也会被拖累。

---

## 二、一个必须记录的反直觉结论

最初的假设是「清掉那 476 条空壳，duplicate_rate 就会降下来」。**实测证伪：**

```
baseline                      recall=0.9348 mrr=0.8232 dup=0.1087
清理 380 条空壳（降级 cold）  recall=0.9348 mrr=0.8232 dup=0.1087   ← 完全没变
```

两个独立原因叠加：

1. **评测根本看不见大部分空壳。** `eval_memory.py` 调用 `search()` 时传
   `include_raw_chat=False`；而 308 条 prompt 污染里 286 条是 `raw_chat=1`。
   评测能看到的空壳只有 48 条（22 prompt + 24 `Run:`）。
2. **降级到 `cold` 不生效。** `search.py` 的 `include_cold` **默认 True**，
   `cold` 行照样被召回。要真正移出检索，必须用 `quality_state='quarantine'`
   或 `lifecycle='superseded'`。

这两个坑都很容易再踩一次，所以写进本文而不是只写在提交信息里。

---

## 三、处置方式：降级，不删除

### 3.1 为什么是 `quarantine` 而不是 `cold`

| 降级方式 | 是否移出检索 | 依据 |
| --- | --- | --- |
| `lifecycle='cold'` | **否** | `search.py` 的 `include_cold` 默认 True |
| `quality_state='quarantine'` | 是 | `search.py` 恒加 `quality_state='accepted'` |
| `lifecycle='superseded'` | 是 | 同上，且需要 `superseded_by` |

本工具用 `quarantine`：它不需要指定"被谁取代"（重复项之间没有语义胜负，
只是同一句话的多份拷贝），语义最贴切。

### 3.2 为什么绝不物理 DELETE

每一行都是 evidence-linked 的。物理删除会：
- 破坏 append-only 审计链（`ledger_events` 与 `memories` 脱节）；
- 让判断**不可撤销**——而"哪些是重复"本身是一个可能判错的启发式。

降级保留全部可审计性，且回滚只需一条 UPDATE。

### 3.3 回滚

`--apply` 会在**修改之前**先写两份文件到账本同目录：

```
data-hygiene-rollback-<UTC时间戳>.json          # 结构化清单（memory_id + 分类 + 原因 + 预览）
data-hygiene-rollback-<UTC时间戳>.rollback.sql  # 可直接执行的还原 SQL
```

回滚：

```bash
sqlite3 ~/Library/Application\ Support/HippoMind/hippomind.sqlite \
  < ~/Library/Application\ Support/HippoMind/data-hygiene-rollback-20261024T164806Z.rollback.sql
```

顺序是刻意的：**先写清单再改库**。如果进程在 apply 中途被杀，磁盘上的清单仍然
描述了所有可能被改动的行。

---

## 四、扫描范围：只看 `active`，绝不碰 `cold`

这是本工具最危险的一个参数，值得单独说明。

```
accepted + cold   253,522 行   ← 归档证据，天然高度重复，但不是积压
accepted + active   7,474 行   ← 真正在检索池里的
```

第一版扫描用的是 `quality_state='accepted'` 不加 lifecycle 过滤，dry-run 报了
**163,622 条待处理**——其中 162,662 条是归档行。如果直接 `--apply`，
会把 25 万条归档证据全部隔离掉。

> 是"数字大得离谱"救了它。所以 `--dry-run` 是默认值，且报告第一屏必须打印
> `live_rows` 和 `distinct_contents`，让口径可核对。

当前扫描谓词：`quality_state='accepted' AND lifecycle='active'`，
与 `DistillEngine._candidates`、`scripts/distill_status.py` 保持一致。

---

## 五、防再产生：入库质量门

存量清理是一次性的，**入库门才是长期解**。实现在 `hippomind/distill.py`：

```python
classify_unusable(content) -> str   # "" = 可用；否则返回原因码
```

`run()` 在去重之前先过这道门，并把结果计入 `summary["rejected"]`（按 reason 分组）。
**不做静默丢弃**——这正是当初 476 条能悄悄积累的原因：没人能区分
"我们过滤了" 和 "我们没采到"。

### 5.1 特征匹配：字面标记，不用宽泛词

`PROMPT_SELF_MARKERS` 是 11 条**提炼器 prompt 的原文片段**，例如：

```
"你是 HippoMind 的记忆提炼器"
"从下面的会话片段中提取稳定、可复核的事实"
"subject（实体/项目/主题）"
"slot_key（槽位，稳定小写）"
```

刻意**不用**「记忆」「JSON」「claims」这类词：真实的记忆完全可能讨论记忆系统和 JSON。
测试里有专门的"不误杀"用例：

```python
"这个 JSON 配置需要 slot_key 字段才能通过校验。"   # 保留
"记忆系统必须支持按时间回溯的查询。"               # 保留
"subject/predicate/value 三元组是 claims 的基本结构。"  # 保留
```

`noise` 一类复用已有的 `hippomind.textutil.is_noise_text`（banner / 裸命令回显），
不新造规则。

### 5.2 入库去重

`run()` 在写入前查一次指纹索引，同规范化内容已存在则**跳过写入但推进游标**
（否则同一行会被反复重试）。索引**每轮 run() 快照一次**，轮内用
`_remember_written` 自维护，**不用 ledger generation 做缓存键**——generation
每次写事务都自增，用它做键会让缓存被它要防的那些写入本身打掉。

验证（临时库，6 条相同内容）：

```
pass1 distilled=1 skipped_dup=5   → distill 行数 1
pass2 distilled=1 skipped_dup=3   → distill 行数 2
duplicate content groups: 0
cursors: 10 | still pending: 0
```

---

## 六、本次执行结果（2026-10-24）

```
检出：noise 402（self_prompt 332 / empty_payload 46 / noise 24）
      duplicates 4,141
      合计 4,543 → 全部 quality_state=quarantine

keyword 评测（生产账本实测）：
              recall@8     mrr      duplicate_rate
  清理前      0.9348     0.8269        0.1313
  清理后      0.9565     0.8504        0.0145
  变化        +0.0217    +0.0235       -0.1168

gate: PASS（阈值 max_duplicate_rate=0.10）
```

recall 和 MRR 上升是因为重复项不再挤占 top-8 名额，让真正不同的结果进来了。

---

## 七、遗留问题（超出本次写范围，未修）

### 7.1 根因：ledger identity key 含可变 metadata

这是**设计缺陷**，不只是数据问题。`hippomind/ledger/identity.py` 的结构化记录
identity 里包含 `valid_from`，而 raw 记录包含 `session_id`。同一句话在不同会话
被重新观察到，就会得到不同 `content_hash` → 新行。

```
141 份副本：evidence_json 完全相同（gemini:1922...）
            metadata_json 有 141 个不同版本
```

修它需要动 identity 逻辑（schema 级风险，会影响所有采集路径的去重语义），
**不在本任务写范围内**。本次用入库去重兜住，但这是止血不是根治：

- 入库去重只覆盖 `hippomind-distill` 自己的产物；
- 采集侧（gemini / pi / transcripts）仍会写出重复行，需要单独处理。

建议后续任务：把 identity key 收敛为「class + agent + 规范化内容」
（对结构化记录额外保留显式 `valid_from`，但不保留采集时的默认时间戳），
并在采集侧加一层同内容检测。

### 7.2 `distill_status.py` 与 `data_hygiene.py` 的口径必须保持同步

两者都用 `quality_state='accepted' AND lifecycle='active'`。
如果将来有人改了其中一个，另一个会静默给出不同的 backlog 数字。
目前靠测试和注释约束，没有共享常量。

### 7.3 清理是一次性快照

daemon 一直在跑，`--apply` 之后新产生的重复不会被自动清理。
建议把 `data_hygiene.py --apply` 挂进维护周期（例如每周一次），
或在 `lifecycle.maintain()` 里加一个同类检测。**本次未接线**。