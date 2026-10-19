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
from typing import List, Protocol, Sequence


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
