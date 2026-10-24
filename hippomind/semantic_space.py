"""Ledger-native semantic space: LSA over the hub own claims and memories.

No model download and no GPU: this builds a term-document matrix from the
ledger itself (accepted memories and their claims), runs a truncated SVD with
numpy, and turns each term into a dense vector.  Terms that co-occur in the
same memories end up close, which is the part a character n-gram cannot do:
deploy and release share no characters but do share documents.

It is a small, honest semantic model: vocabulary and document count are bounded,
the model file records its own build parameters, and any missing term falls back
to the caller deterministic embedder.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from functools import lru_cache

from .textutil import tokenize_multilingual

DEFAULT_DIM = 128
MAX_TERMS = 4_000
MAX_DOCS = 3_000
MIN_TERM_DOCS = 2


def default_model_path() -> Path:
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "semantic_space.json"


def _documents(ledger) -> List[str]:
    docs: List[str] = []
    with ledger._reading() as conn:
        rows = conn.execute(
            "SELECT content FROM memories WHERE quality_state='accepted' "
            "AND lifecycle='active' AND content!='' "
            "ORDER BY created_at DESC LIMIT ?", (MAX_DOCS,)).fetchall()
        docs.extend(str(row["content"] or "") for row in rows)
        rows = conn.execute(
            "SELECT subject, predicate, value_json FROM memory_claims "
            "WHERE state='active' LIMIT ?", (MAX_DOCS,)).fetchall()
        for row in rows:
            try:
                value = json.loads(row["value_json"] or '""')
            except ValueError:
                value = row["value_json"]
            docs.append("%s %s %s" % (row["subject"], row["predicate"], value))
    return docs[:MAX_DOCS]


def build(ledger, *, dim: int = DEFAULT_DIM, max_terms: int = MAX_TERMS) -> Dict[str, Any]:
    """Fit the semantic space and return the model document."""
    import numpy as np

    docs = _documents(ledger)
    tokenised = [tokenize_multilingual(doc) for doc in docs]
    df: Dict[str, int] = {}
    for tokens in tokenised:
        for token in tokens:
            df[token] = df.get(token, 0) + 1
    terms = [term for term, count in sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))
             if count >= MIN_TERM_DOCS][:max_terms]
    if len(terms) < 32 or len(docs) < 8:
        return {"terms": [], "vectors": {}, "dim": 0, "docs": len(docs),
                "reason": "not enough corpus"}
    index = {term: i for i, term in enumerate(terms)}
    matrix = np.zeros((len(terms), len(docs)), dtype="float32")
    for column, tokens in enumerate(tokenised):
        counts: Dict[str, int] = {}
        for token in tokens:
            if token in index:
                counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            matrix[index[token], column] = 1.0 + math.log(count)
    idf = np.array([math.log((1 + len(docs)) / (1 + df[term])) + 1.0 for term in terms],
                   dtype="float32")
    matrix *= idf[:, None]
    dim = max(8, min(int(dim), min(matrix.shape) - 1))
    u, s, _vt = np.linalg.svd(matrix, full_matrices=False)
    u = u[:, :dim]
    norms = np.linalg.norm(u, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    u = u / norms
    vectors = {term: [round(float(value), 5) for value in u[index[term]]]
               for term in terms}
    return {"terms": terms, "vectors": vectors, "dim": int(dim),
            "docs": len(docs), "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "explained": [round(float(value), 4) for value in (s[:dim] ** 2).tolist()]}


class SemanticSpaceEmbedding:
    """EmbeddingAdapter backed by a fitted LSA model."""

    def __init__(self, model: Mapping[str, Any], fallback=None):
        self.dim = int(model.get("dim") or 0)
        self.vectors = model.get("vectors") or {}
        self.fallback = fallback

    @classmethod
    def load(cls, path: Optional[os.PathLike] = None, fallback=None):
        target = Path(path).expanduser() if path else default_model_path()
        try:
            model = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(model, dict) or not model.get("vectors"):
            return None
        return cls(model, fallback=fallback)

    @lru_cache(maxsize=4096)
    def _embed_cached(self, text: str) -> tuple:
        tokens = tokenize_multilingual(text)
        accumulator = [0.0] * self.dim
        used = 0
        for token in tokens:
            vector = self.vectors.get(token)
            if not vector or len(vector) != self.dim:
                continue
            used += 1
            for i, value in enumerate(vector):
                accumulator[i] += value
        if used == 0:
            fallback = self.fallback.embed(text) if self.fallback is not None else []
            return tuple(fallback)
        norm = math.sqrt(sum(value * value for value in accumulator)) or 1.0
        return tuple(value / norm for value in accumulator)

    def embed(self, text: str) -> List[float]:
        return list(self._embed_cached(str(text or "")))

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        return float(sum(x * y for x, y in zip(a, b)))


def save(model: Mapping[str, Any], path: Optional[os.PathLike] = None) -> Path:
    target = Path(path).expanduser() if path else default_model_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(model, ensure_ascii=False, separators=(",", ":")),
                      encoding="utf-8")
    return target


def load_embedder(path=None):
    """Return the fitted semantic embedder, or None when no model exists."""
    return SemanticSpaceEmbedding.load(path)
