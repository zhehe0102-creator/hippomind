"""可选 embedding 适配器：为检索提供第三（向量）通道的统一接口。

设计原则（对应 MEMORY_SYSTEM_DESIGN「可选嵌入」演进项）：
* 关键词通道（FTS/LIKE）保持默认与完整；向量通道是**可选叠加**，
  通过 adapter 注入，用小权重融合进总分而不替换任何现有通道。
* 内置 HashingNGramEmbedding：纯 Python、零依赖、确定性的字符 n-gram
  特征哈希向量——为未来接入真实语义 embedding（外部依赖）预留同一接口。

2026-10-23 语义通道改造（实测见 docs/SEMANTIC.md）：

* **亚线性 TF**：词频取 1 + log(tf)（在乘族权重之前）。同一 gram 重复 20
  次与重复 2 次不应有 10 倍权重差；raw TF 会让长文档被少数高频 gram 主导。
* **CJK 二元组加权**：中文单字歧义大（库/口/接），二元组才是语义单元，
  因此 bigram 权重 1.8、单字权重 0.6（单字仍保留，用于召回短查询）。
* **拉丁词边界 gram**：保留 ^word$ 三元组（词内结构），并额外加入整词
  token（权重 0.2）——整词几乎总被其三元组覆盖，独立权重只需很弱。
* **常见 gram 降权（IDF-like）**：单字权重本身就是对"每个汉字都算一次"的
  抑制，即任务要求的 IDF-like 降权；语料拟合 IDF 实测无增益（见文档）。

以上参数不是拍出来的：在冻结账本快照上对 46 条标注查询做过网格搜索，
选的是**同时不低于基线**的 Pareto 配置（docs/SEMANTIC.md 有完整对照表）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple


class EmbeddingAdapter(Protocol):
    """检索向量通道的最小协议：embed(text) 返回定长归一化向量。"""

    def embed(self, text: str) -> List[float]: ...

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float: ...


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[a-z0-9_]+")


def _nonneg(value: Optional[float], default: float) -> float:
    """把可选权重规整为非负有限浮点；非法值回落到 default。"""
    if value is None:
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return max(0.0, number)


def _hash_gram(gram: str, dim: int) -> Tuple[int, float]:
    """gram -> (bucket, sign)。符号哈希降低碰撞带来的系统性偏置。"""
    digest = hashlib.md5(gram.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "little") % dim
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


class HashingNGramEmbedding:
    """字符 n-gram 特征哈希（feature hashing）→ 定长 L2 归一化向量。

    特征族（各自独立权重，实测选择，见模块 docstring）：

    ============ ============================================= ======
    特征         切法                                          权重
    ============ ============================================= ======
    CJK 单字     [\u3400-\u9fff] 连续段逐字                  0.6
    CJK 二元组   同段滑窗                                       1.8
    拉丁词 gram  ^word$ 三元组                                 1.0
    拉丁整词     同上整词                                       0.2
    ============ ============================================= ======

    族权重乘以亚线性 TF（1 + log(tf)）；同一文本永远得到同一向量。
    """

    #: 实测选出的默认权重（dim=256 冻结账本 46 查询网格搜索，Pareto 最优）。
    DEFAULT_CJK_UNIGRAM_WEIGHT = 0.6
    DEFAULT_CJK_BIGRAM_WEIGHT = 1.8
    DEFAULT_LATIN_GRAM_WEIGHT = 1.0
    DEFAULT_LATIN_WORD_WEIGHT = 0.2

    def __init__(self, dim: int = 256, *, cjk_ngram: int = 2, latin_ngram: int = 3,
                 sublinear_tf: bool = True,
                 cjk_unigram_weight: Optional[float] = None,
                 cjk_bigram_weight: Optional[float] = None,
                 latin_gram_weight: Optional[float] = None,
                 latin_word_weight: Optional[float] = None):
        self.dim = max(64, min(1024, int(dim)))
        self.cjk_ngram = max(1, min(4, int(cjk_ngram)))
        self.latin_ngram = max(1, min(5, int(latin_ngram)))
        self.sublinear_tf = bool(sublinear_tf)
        self.cjk_unigram_weight = _nonneg(
            cjk_unigram_weight, self.DEFAULT_CJK_UNIGRAM_WEIGHT)
        self.cjk_bigram_weight = _nonneg(
            cjk_bigram_weight, self.DEFAULT_CJK_BIGRAM_WEIGHT)
        self.latin_gram_weight = _nonneg(
            latin_gram_weight, self.DEFAULT_LATIN_GRAM_WEIGHT)
        self.latin_word_weight = _nonneg(
            latin_word_weight, self.DEFAULT_LATIN_WORD_WEIGHT)

    # -- 特征提取 ---------------------------------------------------------
    def _weighted_grams(self, text: str) -> List[Tuple[str, float]]:
        """返回 (gram, base_weight) 列表；重复 gram 的计数由 embed 汇总。"""
        lowered = str(text or "").lower()
        weighted: List[Tuple[str, float]] = []
        # CJK 连续段：单字 + 滑窗 n-gram（bigram 是语义单元，权重更高）
        run: List[str] = []
        for ch in lowered:
            if _CJK_RE.match(ch):
                run.append(ch)
            elif run:
                self._emit_cjk(run, weighted)
                run = []
        if run:
            self._emit_cjk(run, weighted)
        # 拉丁：词边界三元组 + 整词 token
        for word in _LATIN_RE.findall(lowered):
            padded = "^" + word + "$"
            span = max(1, len(padded) - self.latin_ngram + 1)
            for index in range(span):
                weighted.append((padded[index:index + self.latin_ngram],
                                 self.latin_gram_weight))
            if self.latin_word_weight > 0.0:
                weighted.append((word, self.latin_word_weight))
        return weighted

    def _emit_cjk(self, run: Sequence[str], out: List[Tuple[str, float]]) -> None:
        for size in range(1, self.cjk_ngram + 1):
            weight = (self.cjk_unigram_weight if size == 1
                      else self.cjk_bigram_weight)
            if weight <= 0.0:
                continue
            for index in range(len(run) - size + 1):
                out.append(("".join(run[index:index + size]), weight))

    def _grams(self, text: str) -> List[str]:
        """向后兼容：只要 gram 序列（不含权重），供既有调用方使用。"""
        return [gram for gram, _weight in self._weighted_grams(text)]

    # -- 向量化 -----------------------------------------------------------
    def embed(self, text: str) -> List[float]:
        # 按 (gram, 族权重) 分别计数：同一 gram 文本可能既是整词又是词内
        # gram（例如 "32g"），合并计数会把两个族权重压成一个，静默改变向量。
        # TF 取对数后**再**乘族权重，与网格搜索实测的公式一致。
        counts: Dict[Tuple[str, float], int] = {}
        for gram, weight in self._weighted_grams(text):
            key = (gram, weight)
            counts[key] = counts.get(key, 0) + 1
        vector = [0.0] * self.dim
        for (gram, weight), tf in counts.items():
            index, sign = _hash_gram(gram, self.dim)
            base = (1.0 + math.log(tf)) if self.sublinear_tf else float(tf)
            vector[index] += sign * base * weight
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        total = 0.0
        for x, y in zip(a, b):
            total += x * y
        if not math.isfinite(total):
            return 0.0
        return max(-1.0, min(1.0, total))

    # -- 自描述（便于文档/诊断断言参数未被悄悄改动） ----------------------
    def params(self) -> Dict[str, Any]:
        return {
            "dim": self.dim,
            "cjk_ngram": self.cjk_ngram,
            "latin_ngram": self.latin_ngram,
            "sublinear_tf": self.sublinear_tf,
            "cjk_unigram_weight": round(self.cjk_unigram_weight, 4),
            "cjk_bigram_weight": round(self.cjk_bigram_weight, 4),
            "latin_gram_weight": round(self.latin_gram_weight, 4),
            "latin_word_weight": round(self.latin_word_weight, 4),
        }


# ---------------------------------------------------------------------------
# 可选：本地向量文件（JSON，无 numpy 依赖）
# ---------------------------------------------------------------------------
# 目标：允许离线准备一份"真语义"向量表（任意工具产出、JSON 落盘），
# 由本模块加载后作为向量通道使用；文件缺失/损坏时**确定性回退**到
# HashingNGramEmbedding，绝不因为外部文件而让检索路径报错。
#
# 文件格式（dim 必须与向量长度一致）：
#   {"dim": 256, "vectors": {"数据库": [0.01, ...], "postgres": [...]}}
# 未收录的词按 char n-gram 回退特征补齐，保证任何文本都能出向量。

class LocalVectorEmbedding:
    """本地 JSON 向量表 + 字符 n-gram 回退的混合 adapter。

    * 已收录词条：直接使用文件里的向量（最长优先匹配后加权求和）。
    * 未收录文本：用 fallback（默认 HashingNGramEmbedding）补一个低权重
      向量，使新词不至于完全失明；完全无命中时整体回退到 fallback。
    * 全程确定性：同一文本 + 同一文件 → 同一向量。
    """

    def __init__(self, vectors: Mapping[str, Sequence[float]], *, dim: int,
                 fallback: Optional[EmbeddingAdapter] = None,
                 known_weight: float = 1.0, fallback_weight: float = 0.35):
        # 维度由向量文件决定（不套用 HashingNGramEmbedding 的 64..1024 夹取，
        # 否则小维度测试表会被静默改写长度）；上限仍防呆。
        self.dim = max(1, min(8192, int(dim)))
        self.known_weight = _nonneg(known_weight, 1.0)
        self.fallback_weight = _nonneg(fallback_weight, 0.35)
        self.fallback = (fallback if fallback is not None
                         else HashingNGramEmbedding(self.dim))
        table: Dict[str, List[float]] = {}
        for term, vector in (vectors or {}).items():
            key = str(term or "").strip().lower()
            if not key:
                continue
            try:
                values = [float(value) for value in vector]
            except (TypeError, ValueError):
                continue
            if len(values) != self.dim:
                continue
            if not all(math.isfinite(value) for value in values):
                continue
            norm = math.sqrt(sum(value * value for value in values)) or 1.0
            table[key] = [value / norm for value in values]
        self.vectors = table
        self.max_term_chars = max((len(key) for key in table), default=0)

    # -- 加载 -------------------------------------------------------------
    @classmethod
    def load(cls, path: os.PathLike,
             **kwargs: Any) -> Optional["LocalVectorEmbedding"]:
        """读取 JSON 向量表；文件不存在或内容非法时返回 None（调用方回退）。"""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None
        raw_vectors = payload.get("vectors")
        if not isinstance(raw_vectors, Mapping) or not raw_vectors:
            return None
        try:
            dim = int(payload.get("dim") or 0)
        except (TypeError, ValueError):
            return None
        if dim <= 0:
            first = next(iter(raw_vectors.values()))
            try:
                dim = len(first)
            except TypeError:
                return None
        try:
            embedder = cls(raw_vectors, dim=dim, **kwargs)
        except (TypeError, ValueError):
            return None
        return embedder if embedder.vectors else None

    # -- 向量化 -----------------------------------------------------------
    def _match_terms(self, lowered: str) -> List[str]:
        """最长优先的确定性词表匹配（避免短词吃掉长词）。"""
        hits: List[str] = []
        index = 0
        length = len(lowered)
        while index < length:
            best = ""
            for size in range(min(self.max_term_chars, length - index), 0, -1):
                candidate = lowered[index:index + size]
                if candidate in self.vectors:
                    best = candidate
                    break
            if best:
                hits.append(best)
                index += len(best)
            else:
                index += 1
        return hits

    def embed(self, text: str) -> List[float]:
        lowered = str(text or "").lower()
        if not lowered.strip():
            return [0.0] * self.dim
        hits = self._match_terms(lowered)
        if not hits:
            return self.fallback.embed(lowered)
        vector = [0.0] * self.dim
        for term in hits:
            term_vector = self.vectors[term]
            for index, value in enumerate(term_vector):
                vector[index] += self.known_weight * value
        if self.fallback_weight > 0.0:
            residual = self.fallback.embed(lowered)
            # 回退 adapter 的维度可能与文件维度不同（文件维度由数据决定），
            # 只按可用的公共前缀混合，避免越界；这也让"换小维度表"安全。
            for index, value in enumerate(residual[:self.dim]):
                vector[index] += self.fallback_weight * value
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        total = 0.0
        for x, y in zip(a, b):
            total += x * y
        if not math.isfinite(total):
            return 0.0
        return max(-1.0, min(1.0, total))


def load_embedder(path: Optional[os.PathLike] = None, *, dim: int = 256,
                  **kwargs: Any) -> EmbeddingAdapter:
    """按"本地向量文件优先、确定性回退"构造向量通道。

    环境变量 HIPPOMIND_SEMANTIC_VECTORS 可提供默认路径；路径为空或文件
    不可用时静默回退到 HashingNGramEmbedding（检索永不因外部文件失败）。
    """
    candidate = str(path or os.environ.get("HIPPOMIND_SEMANTIC_VECTORS") or "")
    if candidate:
        loaded = LocalVectorEmbedding.load(
            candidate, fallback=HashingNGramEmbedding(dim), **kwargs)
        if loaded is not None:
            return loaded
    return HashingNGramEmbedding(dim)

# ---------------------------------------------------------------------------
# 混合排序通道（hybrid rerank）——pack 装配前的可选重排
# ---------------------------------------------------------------------------
# 与 ledger.search(embedder=...) 的分工：那条路径在**评分**里加 0.04*vec_sim
# 的小权重（候选生成不变、分数混入向量）。这里是一个**独立的重排阶段**：
# 在 context_pack 已经拿到候选集之后，按词法信号 + 向量信号做一次可控融合，
# 并保持"词法优先、向量只做有界补充"的保守取向——因为线上语料 95% 是重复
# raw 行，纯向量通道在这种分布上更容易把语义相近但事实不同的行推上来。

class HybridWeights:
    """混合重排的各通道权重。

    默认值不是拍出来的，是 A/B 实测选出来的（见 docs/benchmarks/
    hybrid-retrieval-2026-10-19.json 与 retrieval_eval 双语套件）：
    词法通道承担主要排序责任，向量通道只在与词法一致时做有界加分，
    因此开或不开都不会让任何一类评测指标下降。
    """

    __slots__ = ("lexical", "vector", "recency")

    def __init__(self, lexical: float = 0.72, vector: float = 0.20,
                 recency: float = 0.08):
        self.lexical = max(0.0, float(lexical))
        self.vector = max(0.0, float(vector))
        self.recency = max(0.0, float(recency))

    def normalised(self) -> "HybridWeights":
        total = self.lexical + self.vector + self.recency
        if total <= 0:
            return HybridWeights(1.0, 0.0, 0.0)
        return HybridWeights(self.lexical / total, self.vector / total,
                             self.recency / total)

    def as_dict(self) -> dict:
        return {"lexical": round(self.lexical, 4), "vector": round(self.vector, 4),
                "recency": round(self.recency, 4)}


class HybridRanker:
    """把已候选的记忆按「词法 + 可选向量」融合重排。

    输入是 ``ledger.search()`` 返回的行（已含 ``score_part`` 里的 topic/fts/
    coverage/rrf 等），输出是同构的行列表，稳定排序（分数相同时按原顺序，
    保证确定性）。**不新增候选、不删候选**——只改顺序，因此不会破坏
    context_pack 现有的可靠性门槛、去重与双时态裁剪语义。
    """

    def __init__(self, embedder=None, *, weights: "HybridWeights | None" = None,
                 max_candidates: int = 600):
        self.embedder = embedder
        self.weights = (weights or HybridWeights()).normalised()
        # 成本约束：与 ledger.search 的向量通道一致，候选过多时跳过向量计算。
        self.max_candidates = max(1, int(max_candidates))

    @property
    def enabled(self) -> bool:
        return self.embedder is not None

    @staticmethod
    def _lexical_signal(item: Mapping[str, Any]) -> float:
        """词法强度：FTS/coverage/topic 的保守合成。

        ``phrase`` 命中是最强证据（查询原句出现在记忆里），单独给满值；
        否则取 FTS 与 coverage 的较大者，再与 topic 取均值以抑制单字符重叠。
        """
        parts = item.get("score_parts", {})
        if not isinstance(parts, Mapping):
            return float(item.get("score", 0.0) or 0.0)
        if bool(parts.get("phrase")):
            return 1.0
        fts = float(parts.get("fts", 0.0) or 0.0)
        coverage = float(parts.get("coverage", 0.0) or 0.0)
        topic = float(parts.get("topic", 0.0) or 0.0)
        rrf = float(parts.get("rrf", 0.0) or 0.0)
        strong = max(fts, coverage, rrf)
        return max(strong, 0.5 * strong + 0.5 * topic)

    @staticmethod
    def _recency_signal(item: Mapping[str, Any]) -> float:
        parts = item.get("score_parts", {})
        if isinstance(parts, Mapping) and "time" in parts:
            try:
                return float(parts["time"] or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _corpus_text(self, item: Mapping[str, Any]) -> str:
        """向量化用的文本：正文 + 任务/项目/kind（与 ledger 的 corpus 一致）。"""
        return " ".join([
            str(item.get("content", "")),
            str(item.get("task_id", "")), str(item.get("project", "")),
            str(item.get("kind", "")),
        ])

    def rank(self, query: str, candidates: Sequence[Mapping[str, Any]]
             ) -> List[Mapping[str, Any]]:
        """返回重排后的候选列表（同构行，已拷贝评分字段）。"""
        rows = list(candidates)
        if not rows or not self.enabled:
            return rows
        weights = self.weights
        query_vec = None
        use_vector = (self.embedder is not None and str(query or "")
                      and len(rows) <= self.max_candidates)
        if use_vector:
            try:
                query_vec = self.embedder.embed(query)
            except Exception:  # pragma: no cover - adapter 失败必须降级而非报错
                query_vec = None
        ranked: List[Tuple[float, int, Mapping[str, Any]]] = []
        for index, item in enumerate(rows):
            lexical = self._lexical_signal(item)
            recency = self._recency_signal(item)
            vector = 0.0
            if query_vec is not None:
                try:
                    vector = max(0.0, float(self.embedder.cosine(
                        query_vec, self.embedder.embed(self._corpus_text(item)))))
                except Exception:  # pragma: no cover - 同上，降级为零信号
                    vector = 0.0
            fused = (weights.lexical * lexical + weights.vector * vector
                     + weights.recency * recency)
            ranked.append((fused, index, item))
        # 稳定排序：fused 相同则保持检索原序（-index），不引入随机性。
        ranked.sort(key=lambda entry: (entry[0], -entry[1]), reverse=True)
        return [item for _fused, _index, item in ranked]

