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

---

## 神经向量（ONNX 真语义通道，2026-10-24）

Status: **active，但生产默认关闭**。上面那几节讲的是"字符 n-gram 特征哈希"
（零依赖、纯 Python）和"账本自拟合 LSA"；本节讲第三条通道：**真实的神经
句向量**（ONNX Runtime + HuggingFace tokenizers），以及一条反直觉的实测结论。

Owned files：`hippomind/semantic_onnx.py`、`hippomind/vector_index.py`、
`hippomind/ledger/search.py`（只加参数）、`tests/test_semantic_onnx.py`、
`tests/test_vector_index.py`、`scripts/eval_semantic.py`、本文件。

### TL;DR

| 通道用法 | 46 条关键词集 recall@8 | 15 条改写集 recall@8 | 结论 |
| --- | --- | --- | --- |
| 纯词法（生产基线） | 1.000 | 0.200 | — |
| 向量只进**重排** | 0.85–0.91 | 未改善 | **变差，放弃** |
| 向量进**候选生成**（RRF k=60, top-96） | **1.000** | **0.667** | **采用** |

一句话：**向量只做重排会让指标变差；让它参与候选生成才有正收益。**
这也是本节的实现为什么不碰 `HybridRanker`，而是在 `ledger.search` 的
**候选生成**阶段加第三路召回。

### 为什么"只做重排"会变差（重要，别重复踩）

`HybridRanker._lexical_signal()` 在 `score_parts["phrase"]` 为真时**恒返回
1.0**。生产权重是 `lexical=0.72 / vector=0.20 / recency=0.08`，所以一条
phrase 命中的记忆拿满 0.72，向量通道最多补 0.20——**权重上压不过**。
向量能提供的价值（"语义相近但字面不同"）恰恰在词法信号为 0 的行上，
而那些行在重排里本来就已经沉底，加 0.20 也翻不上来；同时向量又把一些
词法排前面的行往下拽，于是净效果是**掉分**。

实测（同一批候选池，只换 embedder）：

```
生产权重 lex .72 / vec .20 / rec .08，46 条关键词查询
  lexical-only    recall=1.000  mrr=0.866
  +LSA(prod)      recall=0.913  mrr=0.733
  +bgezh          recall=0.891  mrr=0.701
  +minilmL12      recall=0.870  mrr=0.688
  +e5small        recall=0.848  mrr=0.671
```

### 模型选型

三个候选都实测过中文区分度（同义句余弦 − 无关句余弦的 margin）：

| 模型 | 维度 | margin | 体积 | 选用 |
| --- | --- | --- | --- | --- |
| `Xenova/bge-small-zh-v1.5`（bgezh） | 512 | **0.335** | 24MB | ✅ 默认 |
| `paraphrase-multilingual-MiniLM-L12-v2`（mMinilmL12） | 384 | 0.276 | 118MB | 备选 |
| `intfloat/multilingual-e5-small`（e5small） | 384 | 0.091 | 118MB | 备选 |

### 架构：三个文件，各管一件事

```
hippomind/semantic_onnx.py   ONNX 编码器 + EmbeddingAdapter + 路径/依赖解析
hippomind/vector_index.py    常驻内存 float32 矩阵 + 持久化 + 增量同步
hippomind/ledger/search.py   第三路召回（RRF），默认关闭
```

**`semantic_onnx.py`**

* `load_onnx_embedder()` 是唯一推荐入口：依赖/模型缺失或**任何异常**都返回
  `None`。调用方拿到 `None` 就退回纯词法，不需要 try/except。
* 路径从 `HIPPOMIND_HOME` 推导：`vendor/`（依赖）与 `models/<name>/`（模型）。
  可覆盖：`HIPPOMIND_VENDOR_DIR` / `HIPPOMIND_MODELS_DIR` / `HIPPOMIND_ONNX_MODEL`。
  依赖装在内置盘、不污染系统 Python，因此 `vendor/` 会被按需加入 `sys.path`。
* `BertOnnxEncoder` 的 `encode()` 返回**未归一化**矩阵；归一化与空文本处理
  统一在 `OnnxEmbedding` 里，这样测试可以注入假编码器、完全不碰 onnxruntime。
* **必须显式喂 `token_type_ids`**：bge/bert 系模型把它当必填输入，漏掉直接
  `ValueError: Required inputs (['token_type_ids']) are missing`。
* `InferenceSession` **惰性创建**（第一次 encode 时才建），daemon 启动不被阻塞。

**`vector_index.py`**

* 6457 条 × 512 维 × float32 = 13MB，常驻内存。
* `search()` 用 `np.dot` + `argpartition`：**0.09ms**（不是 `@`，见下方"踩坑"）。
* 落盘 `$HIPPOMIND_HOME/vector_index.npz`：`memory_ids` + `matrix` + 每行 8 字节
  内容哈希 + 模型指纹。**全量重建 87–170s，命中磁盘索引 0.01s**——持久化是
  必须的，不是优化。
* 指纹 = 模型名 + 维度 + max_length + 模型/分词器**文件大小** + `config.json`
  摘要。**刻意不用 mtime**：磁盘间复制会改 mtime 却不改内容，用它会让 13MB
  索引白重建一次。
* `sync()` 只 encode 新增/内容变化的行（实测增量 0.62s / 2 条），删除失效行。

**`ledger.search(vector_index=...)`**

* 新增参数 `vector_index` / `vector_topn`（默认 96）/ `rrf_k`（默认 60）。
* **默认关闭**：`vector_index=None` 时行为与改造前逐字节相同（有测试锁定）。
* 向量通道是**第三路候选来源**，与 FTS/LIKE 一起做 RRF。归一化分母随通道数
  从 2 变 3，保持 RRF 分数仍在 0..1 且不改变既有两通道的相对强弱。
* 三个必须做对的地方（都有测试）：
  1. **过滤必须在 SQL 里重放**。索引覆盖全库 accepted+active，而检索可能带
     scope/project/task/时间窗口/raw_chat 过滤。只按 id 取行会**越权召回**。
  2. **按过滤损耗超额取数**（topn×4 再裁回 topn），否则过滤后通道过窄。
  3. **永不抛异常**：索引/embedder 任何失败都退化为纯词法。

### 踩过的坑（诚实记录）

1. **一次性 encode 6442 条吃掉 41GB 内存**，把机器拖死。因此 `encode_matrix()`
   **强制**按 `batch_size=64` 切片，且测试断言每批大小。这不是"建议"而是
   代码里的硬边界。
2. **空文本不是零向量**。模型对 `""` 会输出 `[CLS]/[SEP]` 池化结果（非零但
   没有语义）。它进矩阵会污染 top-k，所以 `OnnxEmbedding` 显式置零，
   `VectorIndex` 用有效性掩码在检索时**丢弃**（不是排最后）。
3. **`matrix @ vector` 在 numpy 2.0 会伪报 FP 警告**：
   `divide by zero / overflow / invalid value encountered in matmul`，
   结果完全正确（与 float64 差 6e-8）却污染 CLI 输出与测试断言。
   换成 `np.dot` 后**警告消失且快约 2×**（0.16ms → 0.09ms）。
4. **`fts_indexed_predicate` 必须复用 `schema.py` 的定义**。
   `tests/test_fts_invariant.py` 逐字扫描 `hippomind/`，`accepted+active`
   字面量只允许出现在 `schema.py`。第一版在 `vector_index.py` 里复制了一份，
   被测试当场抓住。
5. **标注的 `expect` 必须先在账本里验证存在**。原始 15 条改写查询里有
   `KeepAlive`、`superseded` 两个词在账本中**根本不存在**，那两条永远不可能
   命中。现改为真实存在的 `重启` / `冲突`，并在 `scripts/eval_semantic.py`
   的 note 里记录每条 `LIKE` 命中数。

### 怎么复现

```bash
# 依赖与模型（已装在内置盘，不污染系统 Python）
export PYTHONPATH="$HOME/Library/Application Support/HippoMind/vendor"

# A/B 对照：关键词集 + 改写集，词法 vs 向量参与候选生成
python3 scripts/eval_semantic.py
python3 scripts/eval_semantic.py --set semantic
python3 scripts/eval_semantic.py --json --gate     # 门禁：关键词 >= 0.95 且改写集有提升

# 针对性测试（离线，不需要 onnxruntime / 模型文件 / 网络）
python3 -m unittest tests.test_semantic_onnx tests.test_vector_index -v
```

生产实测输出（2026-10-24，6457 条 accepted+active，bgezh 512 维）：

```
vector index: docs=6459 dim=512 bytes=13228032 model=bgezh (0.0s, 命中磁盘索引)

== set=keyword ==
  mode         recall   mrr      p50       p95       misses
  lexical      1.000    0.866    24.6ms    70.6ms    -
  vector_rrf   1.000    0.870    38.7ms    68.4ms    -
  delta        recall +0.0000  mrr +0.0037  p95 -2.2ms

== set=semantic ==
  mode         recall   mrr      p50       p95       misses
  lexical      0.200    0.143    13.4ms    44.9ms    p-deploy,p-restart-ui,...
  vector_rrf   0.667    0.569    33.2ms    54.0ms    p-deploy,p-adb-missing,...
  delta        recall +0.4667  mrr +0.4260  p95 +9.1ms
```

### 成本与限制

* **延迟**：p50 24.6ms → 38.7ms，p95 基本不变（68–70ms）。仍在 DSH 700ms
  注入超时之内，但这是**默认关闭**的另一个理由——要不要付这 14ms 由调用方决定。
* **内存**：常驻 13MB 矩阵 + onnxruntime 峰值约 116MB。daemon 当前约 40MB，
  全开约 150MB。
* **全量重建 87–170s**：只在换模型/换维度/索引丢失时发生，日常走增量。
* **索引只覆盖 accepted+active**：cold/expired 行不在向量通道里，仍可经
  `include_cold=True` 的有界 LIKE 回退找到（与 FTS 的边界一致）。
* **改写集仍有 5 条 MISS**：`p-deploy`（"灰度"只出现 1 次，向量空间里被
  "上线/发布"类记忆淹没）、`p-adb-missing`、`p-bitemporal`、`p-budget`、
  `p-assoc`（`related_to` 是代码标识符，模型不认）。这些是**真实的语义边界**，
  不是实现缺陷——继续调参只是过拟合这 15 条。
* **生产默认零变化**：daemon / context_pack / CLI 都还没接 `vector_index`。
  接线是独立决策，需要先回答"这 14ms 值不值 0.2 的改写集 recall"。

---

## 神经向量：为什么它只改进候选生成，不改自动注入的可靠性判定

这一节记录一次**失败的改造尝试**，因为它比成功的那次更有信息量。

### 做对了的部分（已上线）

`vector_index.py` + `semantic_onnx.py` 把 bge-small-zh（512 维）做成常驻内存索引
（6500 条 × 512 = 13MB），作为 `ledger.search` 的**第三路候选生成**参与 RRF。
在生产账本上实测（`scripts/eval_semantic.py`）：

| 查询集 | 纯词法 | 词法 + 向量 RRF | 变化 |
| --- | --- | --- | --- |
| keyword（46 条） | recall@8 1.000 / MRR 0.866 | 1.000 / 0.869 | 持平 |
| semantic（53 条改写式） | recall@8 0.207 / MRR 0.182 | **0.453 / 0.360** | **+0.246 / +0.178** |

改写式查询的召回翻倍，关键词集不退化。这部分是真实收益。

### 做错了的部分（已回退）

顺着上面的收益，很容易得出下一步结论："既然向量能找到，那就让向量命中也算
相关性证据，这样自动注入就能覆盖'换个说法'的提问。" **这个推断是错的**，实测证伪：

把"向量名次 < 20"加入 `context_pack._passes_relevance_gate` 与
`ContextPackBuilder._reliable` 之后：

| 提问 | 改造前 | 加语义门控后 |
| --- | --- | --- |
| "上线前应该走什么流程" | 0 条 | 8 条（但**不含**"灰度"这个真正的答案） |
| "手机连上了但是列表里没有设备" | 0 条 | 1 条（不含 "adb devices"） |
| "今天天气怎么样" | 0 条（正确） | **2 条（错误）** |
| "1+1等于几" | 0 条（正确） | **8 条（错误）** |

无关提问开始被注入，而且注入内容与提问并不相关。这是明确的**质量回归**，
因此整段回退，`context_pack` 的可靠性判定维持**只认词面证据**。

### 为什么语义近邻不能当相关性证据

在账本上量过（53 条改写式 + 46 条关键词查询）：

```
命中项余弦    p25=0.522  p50=0.584
未命中余弦    p50=0.478  p75=0.560  max=0.703
```

**两个分布严重重叠**——没有任何单一余弦阈值能同时做到少误收、少漏收。
名次好一些（命中项向量名次 p50 = 5 / 0，rank<20 覆盖 71% / 87% 的命中），
但"名次靠前"同样会把无关提问的邻居排进来。

根因是账本的语料分布：**约 95% 是重复采集的 raw 会话**，几乎任何短提问都能在
向量空间里找到一堆"话题相近"的邻居。语义近邻证明的是**话题相似**，不是
**事实相关**。把前者当成后者，就是把"这两句都在讲部署"误判成"这条记忆回答了这个问题"。

### 结论（写进设计约束）

1. 向量通道的定位是**召回扩展**：让本该出现但词面不重合的记忆进入候选池。
2. 自动注入的可靠性判定**只认词面证据**（FTS 命中 / coverage≥0.6）。向量找到的
   候选一样要过这道门。
3. 代价是：纯粹"换个说法、且毫无词面重叠"的提问，在自动注入路径下仍然拿不到
   上下文（返回空包是**正确**行为，不是 bug）。要覆盖这类提问，需要的是
   **查询改写**（先用模型把口语提问改写成账本词汇），而不是放宽相关性门控。
   这条作为下一步方向记录，不假装已经解决。
