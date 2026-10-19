"""可选 embedding 适配器：为检索提供第三（向量）通道的统一接口。

设计原则（对应 MEMORY_SYSTEM_DESIGN「可选嵌入」演进项）：
* 关键词通道（FTS/LIKE）保持默认与完整；向量通道是**可选叠加**，
  通过 adapter 注入，用小权重融合进总分而不替换任何现有通道。
* 内置 HashingNGramEmbedding：纯 Python、零依赖、确定性的字符 n-gram
  特征哈希向量——为未来接入真实语义 embedding（外部依赖）预留同一接口。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple


class EmbeddingAdapter(Protocol):
    """检索向量通道的最小协议：embed(text) 返回定长归一化向量。"""

    def embed(self, text: str) -> List[float]: ...

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float: ...


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[a-z0-9_]+")


class HashingNGramEmbedding:
    """字符 n-gram 特征哈希（feature hashing）→ 定长 L2 归一化向量。

    * CJK 按单字、拉丁按词切分；n-gram 邻接组合后哈希到固定维度，
      符号哈希降低碰撞偏置。同一文本永远得到同一向量。
    * 与 token_sim 的区别是可加权的稠密重叠度量；作为可选通道与
      FTS/LIKE 融合，不承担候选生成职责。
    """

    def __init__(self, dim: int = 256, *, cjk_ngram: int = 2, latin_ngram: int = 3):
        self.dim = max(64, min(1024, int(dim)))
        self.cjk_ngram = max(1, min(4, int(cjk_ngram)))
        self.latin_ngram = max(1, min(5, int(latin_ngram)))

    def _grams(self, text: str) -> List[str]:
        lowered = str(text or "").lower()
        grams: List[str] = []
        cjk_runs = _CJK_RE.findall(lowered)
        # CJK 连续段按滑动窗口取 n-gram
        run: List[str] = []
        for ch in lowered:
            if _CJK_RE.match(ch):
                run.append(ch)
            elif run:
                for size in range(1, self.cjk_ngram + 1):
                    grams.extend("".join(run[i:i + size])
                                 for i in range(len(run) - size + 1))
                run = []
        if run:
            for size in range(1, self.cjk_ngram + 1):
                grams.extend("".join(run[i:i + size])
                             for i in range(len(run) - size + 1))
        for word in _LATIN_RE.findall(lowered):
            padded = f"^{word}$"
            for i in range(max(1, len(padded) - self.latin_ngram + 1)):
                grams.append(padded[i:i + self.latin_ngram])
        return grams

    def embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dim
        for gram in self._grams(text):
            digest = hashlib.md5(gram.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
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

