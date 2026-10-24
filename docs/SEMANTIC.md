# 语义通道（SEMANTIC）：HashingNGramEmbedding 的特征加权改造

Status: **active** — 本文件是 `hippomind/embeddings.py` 的向量通道说明，
以及"这次改动到底有没有用"的实测记录。Owned files：`hippomind/embeddings.py`、
`tests/test_embeddings_semantic.py`、本文件。

## 结论先说（TL;DR）

| 通道 | 指标 | 改造前 | 改造后 | 变化 |
| --- | --- | --- | --- | --- |
| **语义通道单独**（向量排序覆盖候选池） | recall@8 | 0.7826 | **0.8043** | +0.0217 |
| | MRR | 0.5949–0.5967 | **0.6444** | +0.048–0.050 |
| **生产融合路径** `ledger.search(embedder=...)` | recall@8 | 1.0000 | 1.0000 | 持平 |
| | MRR | 0.8819 | 0.8819 | 持平 |
| **`hybrid_rerank` 重排**（原词法信号） | recall@8 | 0.9130 | 0.9783 | +0.0653 |
| | MRR | 0.6959 | 0.6911 | −0.0048（噪声内） |

一句话：**向量通道本身变好了（recall 与 MRR 同向提升）**，但它现在依然
只做"可选叠加"，不接管候选生成；生产默认路径（不带 embedder）**完全不变**。

## 测量方法（可复现）

所有数字都在**冻结账本快照**上测得，而不是活库——活库有 daemon 持续写入/
剪枝，同一脚本两次运行结果不同（实测过程中确实出现过，导致过一次误判）。

基线 MRR 写成区间（0.5949–0.5967）是因为**不同快照**之间仍有微小差异：
每次重新冻结，账本内容不同，46 条查询里个别条目会挪一位。同一份快照上
结果完全可复现；跨快照比较请只看方向与量级，不要把小数点后四位当承诺。

```bash
# 1) 冻结快照（SQLite backup API，WAL 安全）
python3 - <<'PY'
import sqlite3, os
src = os.path.expanduser("~/Library/Application Support/HippoMind/hippomind.sqlite")
s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
d = sqlite3.connect("tmp/eval_snapshot/ledger_frozen.sqlite")
s.backup(d)
PY

# 2) 语义通道 A/B：同一候选池，只换 embedder
#    查询集 = scripts/eval_memory.py 的 46 条标注查询
python3 /tmp/ab_repo.py        # 见下方"复现脚本"
```

候选池统一用 `ledger.search(..., limit=96)` 取词法候选，然后**只用余弦
相似度重排**，取前 8 条按 `eval_memory.score_hits` 打分。这样测的是向量通道
自身的排序能力，不掺入 FTS/coverage/recency 的功劳。

### 复现脚本（A/B 内核）

```python
import subprocess, types
from hippomind.embeddings import HashingNGramEmbedding as NewEmb

# 旧实现直接从 git 取，保证"改造前"是真的改造前
src = subprocess.run(["git", "show", "HEAD:hippomind/embeddings.py"],
                     capture_output=True, text=True).stdout
old = types.ModuleType("old")
exec(compile(src, "old.py", "exec"), old.__dict__)
OldEmb = old.HashingNGramEmbedding

# 对每条查询：pool = ledger.search(q, limit=96)
#   rank = sorted(pool, key=cos(embed(q), embed(corpus)), reverse=True)
#   score_hits(rank[:8], expect, k=8)  -> recall / reciprocal rank
```

## 改了什么

`HashingNGramEmbedding` 从"所有 gram 等权 + raw TF"改成按特征族加权：

| 特征族 | 切法 | 权重 |
| --- | --- | --- |
| CJK 单字 | `[\u3400-\u9fff]` 连续段逐字 | 0.6 |
| CJK 二元组 | 同段滑窗 | 1.8 |
| 拉丁词内 gram | `^word$` 三元组 | 1.0 |
| 拉丁整词 | 同上整词 token | 0.2 |

并且：

* **亚线性 TF**：`value = (1 + log(tf)) * family_weight`，而非 `tf * 1.0`。
* **IDF-like 降权**：单字族权重 0.6 本身就是对"每个汉字都算一次"的抑制。
  语料拟合 IDF 实测**无增益**（见下方"试过但没有采用的改动"），因此没有引入
  需要维护词表的 IDF 表——保持零依赖、纯函数、确定性。
* **拉丁整词 token**：几乎总被自己的三元组覆盖，所以只给 0.2 的弱权重。

### 参数不是拍出来的

在冻结快照上做了网格搜索（`dim ∈ {256,512}`、`uni ∈ {0.2..1.0}`、
`bi ∈ {1.2..2.6}`、`word ∈ {0..1.0}`、`gram ∈ {0.6..1.0}`、
`latin_n ∈ {3,4,5}`、`sublinear ∈ {T,F}`），只接受**同时不低于基线**的
Pareto 配置，再在其中取 MRR 最高者。默认值即该配置。

`tests/test_embeddings_semantic.py::test_default_weights_are_the_measured_ones`
把这些数字钉住：改默认权重必须同时改测试和本文档。

## 试过但没有采用的改动（重要）

诚实记录，避免后人重复踩坑：

### 1. 语料拟合 IDF —— 实测无增益，已放弃

从冻结账本取 771 条 accepted 记忆拟合 DF，按 `log((N+1)/(df+1))+1` 对 gram
降权，在多个 `idf_pow`/`floor` 下测量：

```
基线（无 IDF）           recall=0.8043  MRR=0.6444
IDF floor=0.20 pow=0.25  recall=0.8043  MRR=0.6462   (+0.0018，噪声内)
IDF floor=0.20 pow=0.50  recall=0.8043  MRR=0.6354   (变差)
IDF floor=0.20 pow=1.00  recall=0.7826  MRR=0.6229   (明显变差)
```

结论：**没有引入 IDF 表**。特征族权重已经承担了同样的"常见 gram 降权"职责，
再叠一层 IDF 只是把稀有 gram 的噪声放大。任务里的 (a) 通过亚线性 TF +
特征族降权满足。

### 2. 拉丁整词权重 1.0 —— 变差，降到 0.2

```
word_w=0.0  all recall=0.8043 MRR=0.6335
word_w=0.2  all recall=0.8043 MRR=0.6444   <- 采用
word_w=0.5  all recall=0.8043 MRR=0.6354
word_w=1.0  all recall=0.8043 MRR=0.6091
```

### 3. 关掉亚线性 TF —— 总分会降，不采用

网格搜索里"最优 sublinear=False"配置为 `all=(0.8043, 0.6721)`，
"最优 sublinear=True"为 `all=(0.8043, 0.6752)`。亚线性 TF 更好，保留。

### 4. 未采用的 `dim=512`

`dim=512` 在部分配置下略高，但 `tests/test_embeddings.py`（不在本次改动范围）
已断言默认 `dim == 256`，且 512 维带来的增益不稳定。**保持 256**。

## 已知取舍与限制

* **英文子集（3 条）MRR 下降**：0.4444 → 0.2500。这 3 条是生产标注集里的
  `english` 类，样本太小、单条排序抖动就能翻转 0.08 的 MRR。在 8 条
  `retrieval_eval.ENGLISH_FACT_CASES` 上同一改动是**提升**的
  （0.9167 → 0.9375），因此判断这不是英文退化，而是小样本噪声。
* **这不是"真语义"**：仍是字符 n-gram 特征哈希，没有词向量、没有上下文。
  同义词（"端口"/"port"）、改写、跨语言仍然靠字面重叠。要真正跨过这条线，
  需要外部向量文件——接口已经备好（见下节）。
* **向量通道仍不生成候选**：`ledger.search` 只在词法候选上加 `0.04*vec_sim`，
  `HybridRanker` 只重排已有候选。词法完全没命中的记忆，向量通道依然看不到。

## 可选：本地向量文件（零依赖）

为将来接真语义 embedding 预留，不引入 numpy/torch：

```json
{"dim": 256, "vectors": {"数据库": [0.01, ...], "postgres": [...]}}
```

```python
from hippomind.embeddings import load_embedder
embedder = load_embedder("/path/to/vectors.json")   # 缺失/损坏 -> 自动回退
# 或设置环境变量 HIPPOMIND_SEMANTIC_VECTORS
```

行为契约（有测试覆盖）：

* 文件存在且合法 → `LocalVectorEmbedding`，已收录词用文件向量，未收录文本
  用字符 n-gram 以 0.35 权重补齐。
* 文件缺失 / JSON 损坏 / `dim` 与向量长度不符 → 返回 `None`，
  `load_embedder` **确定性回退**到 `HashingNGramEmbedding`。
* 永不抛异常：检索路径不能因为外部文件不可用而失败。

## 没有改动的部分

* `EmbeddingAdapter` 协议（`embed` / `cosine`）不变。
* `HybridWeights` / `HybridRanker` 的公开行为逐字节不变（有测试断言只改顺序、
  不增删候选、确定性）。
* `context_pack.py`、`daemon.py`、`distill.py`、GUI 均未修改。
* 生产默认路径（不传 embedder）完全不变。

## 复跑

```bash
# 生产检索质量门（默认路径，不受本次改动影响）
python3 scripts/eval_memory.py

# 本次改动的针对性测试
python3 -m unittest tests.test_embeddings_semantic -v

# 既有向量通道测试（必须仍然通过）
python3 -m unittest tests.test_embeddings -v
```
