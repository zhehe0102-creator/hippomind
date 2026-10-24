"""内存神经向量索引：让向量通道真正参与**候选生成**，而不只是重排。

背景（实测见 docs/SEMANTIC.md 的「神经向量」章节）：只把向量塞进**重排**阶段，
所有 embedding（含账本自拟合 LSA）都让指标**变差**——46 条关键词查询 recall
1.000 → 0.85~0.91。原因是生产 HybridRanker 的 lexical 信号在 phrase 命中时
恒为 1.0，向量 0.2 权重压不过它。让向量**参与候选生成**（向量 top-N 与 FTS
top-N 做 RRF，k=60）才有正收益：15 条改写式查询 recall 0.333 → 0.400。

本模块提供那个"向量 top-N"来源：一份常驻内存的 float32 矩阵（6457 条 ×
512 维 = 13MB）加 numpy 矩阵乘 + argpartition。

设计契约（每条都有测试锁定）：

* **零硬依赖**：numpy 缺失 / embedder 不可用 / 索引文件损坏 → 工厂函数返回
  None，调用方（ledger.search）静默退回纯词法路径。**绝不抛异常**。
* **持久化优先**：全量重建实测 ~170s（6457 条，batch 64），所以索引落盘到
  HIPPOMIND_HOME/vector_index.npz（memory_ids + float32 矩阵 + 内容哈希 +
  模型指纹）。指纹不匹配（换模型 / 换维度 / 换 max_length）才重建。
* **增量更新**：sync() 只 encode 新增/内容变化的记忆，删除失效记忆；不重算
  整表。内容哈希用 blake2b 8 字节（8B/条），这样"同 id 但内容变了"也能被
  发现——账本虽然 append-only，投影行会被 maintenance 重写。
* **空文本不出零行**：构建时跳过空/纯空白 content（模型对空串会输出一个
  非零但无语义的 [CLS]/[SEP] 池化向量，直接进矩阵会污染 top-k）；加载时再
  用 validity mask 兜住历史文件里的零行/NaN 行。
* **确定性**：同一查询、同一矩阵 → 同一顺序（同分按矩阵行序打破平局）。

与 semantic_space（账本自拟合 LSA）的分工：LSA 是"零下载、词共现"的小模型，
本模块是"真神经句向量"的载体；两者都实现 EmbeddingAdapter，所以
ledger.search 不关心用的是哪个。

claim 级索引（2026-10-24，见 docs/SEMANTIC.md 的「claim 级索引」章节）
--------------------------------------------------------------------

真正的瓶颈是**索引粒度**，不是模型、不是查询改写。生产提炼记忆把 5-10 个互不
相关的事实塞进一条 ~350-550 字符的正文，整体 embedding 必然被稀释成一个"什么
都有点像"的中心点。改成按 claim（主语/谓语/值三元组）建索引后，实测（53 条
改写式 + 46 条关键词，bge-small-zh 512 维）：

| 粒度 | semantic recall@8 / MRR | keyword recall@8 / MRR |
| --- | --- | --- |
| memory（上面那个类） | 0.226 / 0.149 | 0.652 / 0.523 |
| **claim（ClaimVectorIndex）** | **0.415 / 0.312** | **0.826 / 0.763** |
| claim + memory RRF 融合 | 0.302 / 0.215 | 0.761 / 0.621 |

因此 **claim 级单独用，不做融合**（融合实测更差：两路各自的第一名互相稀释）。

* 两个类是**并列**的，不是替换：`VectorIndex` 的每个行为都被既有测试锁定，
  `ClaimVectorIndex` 是新增的独立能力，落在独立文件（vector_index_claims.npz）。
* 查询对外仍返回**记忆级**去重结果：同一记忆的多条 claim 命中只保留最好的名次，
  因为下游（ledger.search / context_pack）的一切过滤、证据门槛、去重都按
  memory_id 工作。条目是 3 元组 `(memory_id, cosine, claim_id)`，多出来的
  claim_id 只用于审计（score_parts.vec_claim），旧的两元组消费方不受影响。
* claim 索引只覆盖 `state='active'` 的 claim 且记忆 accepted+active——与 FTS
  通道覆盖同一个记忆集合（复用 schema.fts_indexed_predicate），否则两条候选
  生成路径会互相"看见"对方看不见的行。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import fts_indexed_predicate
from .semantic_onnx import hub_home

INDEX_FILENAME = "vector_index.npz"
CLAIM_INDEX_FILENAME = "vector_index_claims.npz"
DEFAULT_TOPN = 96
DEFAULT_SYNC_BATCH = 64
#: 索引文件格式版本：布局变化时递增，旧文件自动被当作"指纹不匹配"重建。
FORMAT_VERSION = 1
#: claim 级索引独立版本号（与 memory 级解耦：任何一个的布局变化都不该逼另一个重建）。
CLAIM_FORMAT_VERSION = 1

#: claim 检索时的超额取数倍数：先按 claim 取 topn*N 行再折叠成记忆级。
#: 实测记忆常有 8-24 条 claim，取 4 倍能把折叠后的记忆数补回接近 topn。
CLAIM_OVERSCAN = 4

#: 粒度选择：环境变量与常量。生产默认 claim（实测两套查询集都更好）。
GRANULARITY_CLAIM = "claim"
GRANULARITY_MEMORY = "memory"
GRANULARITIES = (GRANULARITY_CLAIM, GRANULARITY_MEMORY)
ENV_GRANULARITY = "HIPPOMIND_VECTOR_GRANULARITY"
DEFAULT_GRANULARITY = GRANULARITY_CLAIM


def configured_granularity(default: str = DEFAULT_GRANULARITY) -> str:
    """$HIPPOMIND_VECTOR_GRANULARITY 或默认粒度；非法值静默回落到 default。

    静默而不是报错：这是检索热路径上的可选项，一个拼错的环境变量不该让
    注入链路失败。
    """
    value = str(os.environ.get(ENV_GRANULARITY, "") or "").strip().lower()
    if value in GRANULARITIES:
        return value
    fallback = str(default or "").strip().lower()
    return fallback if fallback in GRANULARITIES else DEFAULT_GRANULARITY


def _numpy():
    """可选 numpy；缺失时返回 None（调用方降级，不报错）。"""
    try:
        import numpy as np
        return np
    except Exception:
        return None


def default_index_path() -> Path:
    """HIPPOMIND_HOME/vector_index.npz（与 semantic_onnx.index_path 一致）。"""
    return hub_home() / INDEX_FILENAME


def default_claim_index_path() -> Path:
    """HIPPOMIND_HOME/vector_index_claims.npz（与 memory 级索引分开落盘）。

    独立文件的理由：两个索引的生命周期完全不同（claim 级随提炼增删，
    memory 级随采集增删），共用一个文件会让任一方的 sync 重写另一方的矩阵。
    """
    return hub_home() / CLAIM_INDEX_FILENAME


def content_hash(text: str) -> int:
    """内容的稳定 64 位哈希（8 字节/条，用于增量发现"内容变了"）。"""
    digest = hashlib.blake2b(str(text or "").encode("utf-8", errors="replace"),
                            digest_size=8).digest()
    return int.from_bytes(digest, "little")


def corpus_text(row: Any) -> str:
    """索引文本：只用 content。

    刻意**不**拼 task_id/project/kind（ledger.search 的评分 corpus 会拼）：
    这些字段在向量空间里是噪声，而实测正收益就是在"纯 content 向量"上得到的。
    需要不同口径时传 text_of= 覆盖。
    """
    if isinstance(row, dict):
        return str(row.get("content") or "")
    try:
        return str(row["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def fingerprint_of(embedder: Any) -> str:
    """索引指纹：优先用 embedder 自报的 fingerprint()，否则退化到 name+dim。"""
    getter = getattr(embedder, "fingerprint", None)
    if callable(getter):
        try:
            value = str(getter() or "")
            if value:
                return value
        except Exception:
            pass
    payload = "|".join([
        str(getattr(embedder, "name", "") or type(embedder).__name__),
        "dim=%d" % int(getattr(embedder, "dim", 0) or 0),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _ledger_rows(ledger: Any) -> List[Tuple[str, str]]:
    """账本里应当被索引的行：accepted + active + 非空 content。"""
    # 判定必须复用 schema.FTS_INDEXED_PREDICATE（tests/test_fts_invariant.py
    # 的 test_only_schema_module_owns_the_predicate_literal 逐字扫描 hippomind/，
    # 字面量只允许出现在 schema.py）。语义上也必须一致：向量通道与 FTS 通道
    # 覆盖同一个集合，否则两条候选生成路径会互相"看见"对方看不见的行。
    query = ("SELECT memory_id, content FROM memories WHERE "
             + fts_indexed_predicate() +
             " AND content!='' ORDER BY memory_id")
    try:
        with ledger._reading() as conn:
            rows = conn.execute(query).fetchall()
    except Exception:
        return []
    out: List[Tuple[str, str]] = []
    for row in rows:
        try:
            memory_id = str(row["memory_id"])
            content = str(row["content"] or "")
        except (KeyError, IndexError, TypeError):
            continue
        if memory_id and content.strip():
            out.append((memory_id, content))
    return out


def claim_text(subject: Any, predicate: Any, value_json: Any) -> str:
    """claim 的索引文本：`subject + " " + predicate + " " + value`。

    value 在账本里是 JSON 文本（`value_json`），先解成 Python 值再转字符串：
    直接对 JSON 字面量做 embedding 会把引号、括号、转义反斜杠当语义信号。
    dict/list 用 `json.dumps(ensure_ascii=False)` 展平（中文保持原字面量）。
    解析失败时退回原始字符串——一条坏 claim 不该让整次构建失败。
    """
    try:
        value = json.loads(value_json) if isinstance(value_json, str) else value_json
    except (TypeError, ValueError):
        value = value_json
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    parts = [str(subject or "").strip(), str(predicate or "").strip(),
             str(value if value is not None else "").strip()]
    return " ".join(part for part in parts if part)


def _claim_rows(ledger: Any) -> List[Tuple[str, str, str]]:
    """应当被索引的 claim：state='active' 且所属记忆 accepted+active。

    返回 [(claim_id, memory_id, text)]，按 claim_id 排序（矩阵行序即稳定顺序，
    同分时按它打破平局）。判定复用 `schema.fts_indexed_predicate`：字面量只
    允许出现在 schema.py（tests/test_fts_invariant.py 逐字扫描 hippomind/），
    语义上也必须与 FTS 通道覆盖同一记忆集合。
    """
    query = ("SELECT c.claim_id, c.memory_id, c.subject, c.predicate, c.value_json "
             "FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id "
             "WHERE c.state='active' AND " + fts_indexed_predicate("m") +
             " ORDER BY c.claim_id")
    try:
        with ledger._reading() as conn:
            rows = conn.execute(query).fetchall()
    except Exception:
        return []
    out: List[Tuple[str, str, str]] = []
    for row in rows:
        try:
            claim_id = str(row["claim_id"])
            memory_id = str(row["memory_id"])
            text = claim_text(row["subject"], row["predicate"], row["value_json"])
        except (KeyError, IndexError, TypeError):
            continue
        if claim_id and memory_id and text.strip():
            out.append((claim_id, memory_id, text))
    return out


class VectorIndex:
    """常驻内存的 float32 向量矩阵 + memory_id 映射。

    行顺序就是矩阵行序；ids[i] 对应 matrix[i]。矩阵行已 L2 归一化，
    因此检索只需一次矩阵乘（余弦 = 点积）。
    """

    def __init__(self, embedder: Any, ids: Sequence[str], matrix: Any, *,
                 hashes: Any = None, fingerprint: str = "", built_at: float = 0.0,
                 model: str = "", path: Optional[os.PathLike] = None):
        np = _numpy()
        if np is None:
            raise RuntimeError("numpy 不可用，向量索引不可用")
        self.embedder = embedder
        self.ids: List[str] = [str(value) for value in ids]
        self.matrix = np.asarray(matrix, dtype=np.float32)
        if self.matrix.ndim != 2:
            raise ValueError("matrix 必须是二维 (n, dim)")
        if self.matrix.shape[0] != len(self.ids):
            raise ValueError("matrix 行数 %d 与 ids 数 %d 不一致"
                             % (self.matrix.shape[0], len(self.ids)))
        self.dim = int(self.matrix.shape[1])
        expected = int(getattr(embedder, "dim", 0) or 0)
        if expected and expected != self.dim:
            raise ValueError("矩阵维度 %d 与 embedder 维度 %d 不一致"
                             % (self.dim, expected))
        self.hashes = (np.asarray(hashes, dtype=np.uint64) if hashes is not None
                       else np.zeros(len(self.ids), dtype=np.uint64))
        if self.hashes.shape[0] != len(self.ids):
            self.hashes = np.zeros(len(self.ids), dtype=np.uint64)
        self.fingerprint = str(fingerprint or fingerprint_of(embedder))
        self.built_at = float(built_at or time.time())
        self.model = str(model or getattr(embedder, "name", "") or "")
        self.path = Path(path).expanduser() if path else None
        self._lock = threading.RLock()
        self._valid = self._compute_valid()

    # -- 内部 -------------------------------------------------------------
    def _compute_valid(self):
        """有效性掩码：零行 / 非有限行不参与 top-k（NaN 会污染整个排序）。"""
        np = _numpy()
        finite = np.isfinite(self.matrix).all(axis=1)
        if not finite.all():
            # 就地清洗：NaN/Inf 行置零，后续乘加不再产生 invalid value 警告。
            self.matrix[~finite] = 0.0
        norms = np.linalg.norm(self.matrix, axis=1)
        return norms > 0.0

    def __len__(self) -> int:
        return len(self.ids)

    # -- 检索 -------------------------------------------------------------
    def search(self, query_vec: Sequence[float],
               topn: int = DEFAULT_TOPN) -> List[Tuple[str, float]]:
        """向量 top-N：矩阵乘 + argpartition，返回 [(memory_id, cosine)]。"""
        np = _numpy()
        if np is None or not len(self.ids):
            return []
        try:
            vector = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        except (TypeError, ValueError):
            return []
        if vector.size != self.dim or not np.isfinite(vector).all():
            return []
        norm = float(np.linalg.norm(vector))
        if norm <= 0.0:
            return []
        matrix = self.matrix
        valid = self._valid
        if not len(matrix) or not bool(valid.any()):
            return []
        # np.dot 而非 @：numpy 2.0 的 @ 在这块 (6459, 512) 连续矩阵上会伪报
        # "divide by zero / overflow / invalid value encountered in matmul"，
        # 结果本身正确却会污染 CLI 输出与测试断言；dot 走同一 BLAS 且快约 2x
        # （实测 0.16ms -> 0.09ms），且不产生任何 FP 标志。
        similarities = np.dot(matrix, vector / norm)
        if not bool(valid.all()):
            # 无效行压到 -inf，保证它们不会挤进 top-k。
            similarities = np.where(valid, similarities, -np.inf)
        count = int(similarities.shape[0])
        k = max(1, min(int(topn or DEFAULT_TOPN), count))
        if k < count:
            picked = np.argpartition(-similarities, k - 1)[:k]
        else:
            picked = np.arange(count)
        # 确定性：同分按矩阵行序（即 memory_id 排序后的稳定顺序）打破平局。
        picked = picked[np.lexsort((picked, -similarities[picked]))]
        out: List[Tuple[str, float]] = []
        for index in picked:
            score = float(similarities[int(index)])
            # 有效性掩码外的行（零向量 / 非有限）被压成 -inf，这里必须显式
            # 丢弃而不是"排在最后"：一个零向量行不是弱匹配，它是没有信号，
            # 让它进候选会让 RRF 给一条毫无依据的记忆分配名次分。
            if not math.isfinite(score):
                continue
            out.append((self.ids[int(index)], score))
        return out

    def search_text(self, query: str, topn: int = DEFAULT_TOPN) -> List[Tuple[str, float]]:
        """先 embed 查询再检索；embedder 任何异常都退化为空结果。"""
        if not str(query or "").strip():
            return []
        try:
            vector = self.embedder.embed(query)
        except Exception:
            return []
        return self.search(vector, topn=topn)

    # -- 增量 -------------------------------------------------------------
    def _encode(self, texts: Sequence[str]) -> Any:
        np = _numpy()
        encoder = getattr(self.embedder, "encode_matrix", None)
        if callable(encoder):
            matrix = np.asarray(encoder(list(texts)), dtype=np.float32)
        else:
            matrix = np.asarray([self.embedder.embed(text) for text in texts],
                                dtype=np.float32)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.shape != (len(texts), self.dim):
            raise ValueError("编码结果形状 %r 与索引维度 %d 不一致"
                             % (matrix.shape, self.dim))
        matrix = np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return (matrix / norms).astype(np.float32)

    def add_many(self, items: Sequence[Tuple[str, str]]) -> int:
        """追加 (memory_id, text)；空文本跳过。返回实际追加条数。"""
        np = _numpy()
        if np is None:
            return 0
        pairs = [(str(mid), str(text)) for mid, text in items
                 if str(mid) and str(text or "").strip()]
        if not pairs:
            return 0
        matrix = self._encode([text for _mid, text in pairs])
        hashes = np.asarray([content_hash(text) for _mid, text in pairs], dtype=np.uint64)
        ids = [mid for mid, _text in pairs]
        with self._lock:
            self.ids = self.ids + ids
            self.matrix = (np.vstack([self.matrix, matrix]) if len(self.matrix)
                           else matrix)
            self.hashes = (np.concatenate([self.hashes, hashes]) if len(self.hashes)
                           else hashes)
            self._valid = self._compute_valid()
        return len(pairs)

    def remove(self, memory_ids: Iterable[str]) -> int:
        """按 id 删除行；返回实际删除条数。"""
        np = _numpy()
        if np is None:
            return 0
        drop = {str(value) for value in memory_ids}
        if not drop:
            return 0
        with self._lock:
            keep = [index for index, mid in enumerate(self.ids) if mid not in drop]
            removed = len(self.ids) - len(keep)
            if not removed:
                return 0
            self.ids = [self.ids[index] for index in keep]
            self.matrix = self.matrix[keep] if keep else self.matrix[:0]
            self.hashes = self.hashes[keep] if keep else self.hashes[:0]
            self._valid = self._compute_valid()
        return removed

    def sync(self, ledger: Any, *, text_of: Optional[Callable[[Any], str]] = None,
             on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """与账本对齐：删除失效行、重编码内容变化的行、追加新行。

        返回 {"added": n, "removed": n, "updated": n, "docs": n}。
        on_progress 用于长任务（170s 全量重建）打印进度。
        """
        rows = _ledger_rows(ledger)
        text_of = text_of or corpus_text
        current: Dict[str, str] = {}
        for row in rows:
            text = text_of({"memory_id": row[0], "content": row[1]})
            if str(text or "").strip():
                current[str(row[0])] = str(text)
        existing = set(self.ids)
        drop: List[str] = []
        pending: List[Tuple[str, str]] = []
        changed = 0
        for index, memory_id in enumerate(self.ids):
            text = current.get(memory_id)
            if text is None:
                drop.append(memory_id)
            elif content_hash(text) != int(self.hashes[index]):
                drop.append(memory_id)
                pending.append((memory_id, text))
                changed += 1
        for memory_id, text in current.items():
            if memory_id not in existing:
                pending.append((memory_id, text))
        removed = self.remove(drop)
        added = 0
        if pending:
            if on_progress is not None:
                try:
                    on_progress("encoding %d new/changed docs" % len(pending))
                except Exception:
                    pass
            added = self.add_many(pending)
        if removed or added:
            self.built_at = time.time()
        return {"added": added, "removed": removed, "updated": changed,
                "docs": len(self.ids)}

    # -- 持久化 -----------------------------------------------------------
    def save(self, path: Optional[os.PathLike] = None) -> Optional[Path]:
        """原子落盘（写临时文件再 os.replace），失败返回 None。"""
        np = _numpy()
        if np is None:
            return None
        target = Path(path).expanduser() if path else (self.path or default_index_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        try:
            with open(temporary, "wb") as handle:
                np.savez(
                    handle,
                    format_version=np.asarray([FORMAT_VERSION], dtype=np.int64),
                    memory_ids=np.asarray(self.ids, dtype="U64"),
                    matrix=self.matrix,
                    hashes=self.hashes,
                    fingerprint=np.asarray([self.fingerprint], dtype="U64"),
                    model=np.asarray([self.model], dtype="U64"),
                    built_at=np.asarray([float(self.built_at)], dtype=np.float64),
                )
            os.replace(temporary, target)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            return None
        self.path = target
        return target

    @classmethod
    def load(cls, embedder: Any, path: Optional[os.PathLike] = None
             ) -> Optional["VectorIndex"]:
        """读索引；文件缺失 / 损坏 / 指纹或维度不匹配时返回 None（调用方重建）。"""
        np = _numpy()
        if np is None or embedder is None:
            return None
        target = Path(path).expanduser() if path else default_index_path()
        try:
            with np.load(target, allow_pickle=False) as payload:
                version = int(np.asarray(payload["format_version"]).reshape(-1)[0])
                if version != FORMAT_VERSION:
                    return None
                ids = [str(value) for value in np.asarray(payload["memory_ids"]).tolist()]
                matrix = np.asarray(payload["matrix"], dtype=np.float32)
                hashes = np.asarray(payload["hashes"], dtype=np.uint64)
                fingerprint = str(np.asarray(payload["fingerprint"]).reshape(-1)[0])
                model = str(np.asarray(payload["model"]).reshape(-1)[0])
                built_at = float(np.asarray(payload["built_at"]).reshape(-1)[0])
        except Exception:
            return None
        if fingerprint != fingerprint_of(embedder):
            # 换模型 / 换维度 / 换 max_length：旧矩阵在新空间里毫无意义。
            return None
        if matrix.ndim != 2 or matrix.shape[0] != len(ids):
            return None
        expected = int(getattr(embedder, "dim", 0) or 0)
        if expected and matrix.shape[1] != expected:
            return None
        try:
            return cls(embedder, ids, matrix, hashes=hashes, fingerprint=fingerprint,
                       built_at=built_at, model=model, path=target)
        except (ValueError, RuntimeError):
            return None

    @classmethod
    def build(cls, ledger: Any, embedder: Any, *, path: Optional[os.PathLike] = None,
              text_of: Optional[Callable[[Any], str]] = None,
              on_progress: Optional[Callable[[str], None]] = None
              ) -> Optional["VectorIndex"]:
        """从账本全量构建；embedder 不可用 / numpy 缺失时返回 None。"""
        np = _numpy()
        if np is None or embedder is None:
            return None
        text_of = text_of or corpus_text
        pairs: List[Tuple[str, str]] = []
        for memory_id, content in _ledger_rows(ledger):
            text = text_of({"memory_id": memory_id, "content": content})
            if str(text or "").strip():
                pairs.append((memory_id, str(text)))
        dim = int(getattr(embedder, "dim", 0) or 0)
        if not pairs or dim <= 0:
            return None
        try:
            index = cls(embedder, [], np.zeros((0, dim), dtype=np.float32), path=path)
            if on_progress is not None:
                try:
                    on_progress("building index for %d docs" % len(pairs))
                except Exception:
                    pass
            index.add_many(pairs)
        except Exception:
            return None
        index.built_at = time.time()
        return index if len(index) else None

    @classmethod
    def load_or_build(cls, ledger: Any, embedder: Any, *,
                      path: Optional[os.PathLike] = None,
                      sync: bool = False,
                      on_progress: Optional[Callable[[str], None]] = None
                      ) -> Optional["VectorIndex"]:
        """先读盘（快），失败或 sync=True 时对齐/重建。"""
        target = Path(path).expanduser() if path else default_index_path()
        index = cls.load(embedder, target)
        if index is None:
            index = cls.build(ledger, embedder, path=target, on_progress=on_progress)
            if index is None:
                return None
            index.save(target)
            return index
        if sync:
            index.sync(ledger, on_progress=on_progress)
            index.save(target)
        return index

    # -- 自描述 -----------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """docs / dim / bytes / built_at / model（+ 有效性计数）。"""
        np = _numpy()
        valid = int(self._valid.sum()) if np is not None else len(self.ids)
        return {
            "docs": len(self.ids),
            "dim": self.dim,
            "bytes": int(self.matrix.nbytes) if np is not None else 0,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.localtime(self.built_at or time.time())),
            "model": self.model,
            "fingerprint": self.fingerprint,
            "valid_docs": valid,
            "path": str(self.path) if self.path else "",
        }


class ClaimVectorIndex:
    """claim 级向量索引：矩阵行是 claim，对外返回**记忆级**去重后的名次。

    为什么单独一个类而不是给 VectorIndex 加一个 `granularity` 参数：
    VectorIndex 的每个行为（ids 就是 memory_id、sync 按 content_hash 对齐、
    stats 的 docs 语义）都被既有测试逐条锁定。把 claim 语义塞进去，要么改写
    那些测试锁定的契约，要么在每个方法里加分支——两者都会让"memory 级行为
    不变"这句话失去保证。并列一个类，成本只是重复 3 个持久化方法。

    行布局：`claim_ids[i]` 是矩阵第 i 行的 claim，`memory_ids[i]` 是它的宿主
    记忆。同一记忆的多条 claim 各占一行（这正是"粒度变细"的全部意义）。
    """

    def __init__(self, embedder: Any, claim_ids: Sequence[str],
                 memory_ids: Sequence[str], matrix: Any, *,
                 hashes: Any = None, fingerprint: str = "",
                 built_at: float = 0.0, model: str = "",
                 path: Optional[os.PathLike] = None):
        np = _numpy()
        if np is None:
            raise RuntimeError("numpy 不可用，向量索引不可用")
        self.embedder = embedder
        self.claim_ids: List[str] = [str(value) for value in claim_ids]
        self.memory_ids: List[str] = [str(value) for value in memory_ids]
        self.matrix = np.asarray(matrix, dtype=np.float32)
        if self.matrix.ndim != 2:
            raise ValueError("matrix 必须是二维 (n, dim)")
        if self.matrix.shape[0] != len(self.claim_ids):
            raise ValueError("matrix 行数 %d 与 claim 数 %d 不一致"
                             % (self.matrix.shape[0], len(self.claim_ids)))
        if len(self.memory_ids) != len(self.claim_ids):
            raise ValueError("memory_ids 数 %d 与 claim 数 %d 不一致"
                             % (len(self.memory_ids), len(self.claim_ids)))
        self.dim = int(self.matrix.shape[1])
        expected = int(getattr(embedder, "dim", 0) or 0)
        if expected and expected != self.dim:
            raise ValueError("矩阵维度 %d 与 embedder 维度 %d 不一致"
                             % (self.dim, expected))
        self.hashes = (np.asarray(hashes, dtype=np.uint64) if hashes is not None
                       else np.zeros(len(self.claim_ids), dtype=np.uint64))
        if self.hashes.shape[0] != len(self.claim_ids):
            self.hashes = np.zeros(len(self.claim_ids), dtype=np.uint64)
        self.fingerprint = str(fingerprint or fingerprint_of(embedder))
        self.built_at = float(built_at or time.time())
        self.model = str(model or getattr(embedder, "name", "") or "")
        self.path = Path(path).expanduser() if path else None
        self._lock = threading.RLock()
        self._valid = self._compute_valid()

    # -- 内部 -------------------------------------------------------------
    def _compute_valid(self):
        np = _numpy()
        finite = np.isfinite(self.matrix).all(axis=1)
        if not finite.all():
            self.matrix[~finite] = 0.0
        norms = np.linalg.norm(self.matrix, axis=1)
        return norms > 0.0

    def __len__(self) -> int:
        return len(self.claim_ids)

    @property
    def granularity(self) -> str:
        return GRANULARITY_CLAIM

    # -- 检索 -------------------------------------------------------------
    def search(self, query_vec: Sequence[float],
               topn: int = DEFAULT_TOPN) -> List[Tuple[str, float, str]]:
        """向量 top-N，按**记忆级**去重：[(memory_id, cosine, claim_id)]。

        去重规则是"同一记忆的多条 claim 只保留最好的名次"——即第一次出现时
        保留，后续同 memory_id 的行丢弃。这样 topn 的语义仍然是"多少条记忆"，
        与 memory 级索引的 topn 可直接对比（也正因如此 RRF 融合才有意义）。

        取数上先按 claim 取 `topn * oversample` 行再折叠：一个记忆可能独占
        前若干名（实测 8 claim 的记忆不少），只取 topn 行会折叠出远少于 topn
        条记忆，让向量通道无谓变窄。`CLAIM_OVERSCAN` 控制这个倍数。
        """
        np = _numpy()
        if np is None or not len(self.claim_ids):
            return []
        try:
            vector = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        except (TypeError, ValueError):
            return []
        if vector.size != self.dim or not np.isfinite(vector).all():
            return []
        norm = float(np.linalg.norm(vector))
        if norm <= 0.0:
            return []
        matrix = self.matrix
        valid = self._valid
        if not len(matrix) or not bool(valid.any()):
            return []
        # np.dot 而非 @：见 VectorIndex.search 的注释（numpy 2.0 的 matmul
        # 会伪报 FP 警告，dot 走同一 BLAS、快约 2x 且不产生标志）。
        similarities = np.dot(matrix, vector / norm)
        if not bool(valid.all()):
            similarities = np.where(valid, similarities, -np.inf)
        count = int(similarities.shape[0])
        want = max(1, int(topn or DEFAULT_TOPN))
        k = max(1, min(want * CLAIM_OVERSCAN, count))
        if k < count:
            picked = np.argpartition(-similarities, k - 1)[:k]
        else:
            picked = np.arange(count)
        picked = picked[np.lexsort((picked, -similarities[picked]))]
        out: List[Tuple[str, float, str]] = []
        seen: Dict[str, int] = {}
        for index in picked:
            score = float(similarities[int(index)])
            # 零向量/非有限行不是弱匹配，是没有信号：丢弃而不是排最后。
            if not math.isfinite(score):
                continue
            memory_id = self.memory_ids[int(index)]
            if memory_id in seen:
                continue
            seen[memory_id] = len(out)
            out.append((memory_id, score, self.claim_ids[int(index)]))
            if len(out) >= want:
                break
        return out

    def search_text(self, query: str,
                    topn: int = DEFAULT_TOPN) -> List[Tuple[str, float, str]]:
        """先 embed 查询再检索；embedder 任何异常都退化为空结果。"""
        if not str(query or "").strip():
            return []
        try:
            vector = self.embedder.embed(query)
        except Exception:
            return []
        return self.search(vector, topn=topn)

    # -- 增量 -------------------------------------------------------------
    def _encode(self, texts: Sequence[str]) -> Any:
        np = _numpy()
        encoder = getattr(self.embedder, "encode_matrix", None)
        if callable(encoder):
            matrix = np.asarray(encoder(list(texts)), dtype=np.float32)
        else:
            matrix = np.asarray([self.embedder.embed(text) for text in texts],
                                dtype=np.float32)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.shape != (len(texts), self.dim):
            raise ValueError("编码结果形状 %r 与索引维度 %d 不一致"
                             % (matrix.shape, self.dim))
        matrix = np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return (matrix / norms).astype(np.float32)

    def add_many(self, items: Sequence[Tuple[str, str, str]]) -> int:
        """追加 (claim_id, memory_id, text)；空文本跳过。返回实际追加条数。"""
        np = _numpy()
        if np is None:
            return 0
        triples = [(str(cid), str(mid), str(text)) for cid, mid, text in items
                   if str(cid) and str(mid) and str(text or "").strip()]
        if not triples:
            return 0
        matrix = self._encode([text for _cid, _mid, text in triples])
        hashes = np.asarray([content_hash(text) for _cid, _mid, text in triples],
                            dtype=np.uint64)
        with self._lock:
            self.claim_ids = self.claim_ids + [cid for cid, _m, _t in triples]
            self.memory_ids = self.memory_ids + [mid for _c, mid, _t in triples]
            self.matrix = (np.vstack([self.matrix, matrix]) if len(self.matrix)
                           else matrix)
            self.hashes = (np.concatenate([self.hashes, hashes]) if len(self.hashes)
                           else hashes)
            self._valid = self._compute_valid()
        return len(triples)

    def remove(self, claim_ids: Iterable[str]) -> int:
        """按 claim_id 删除行；返回实际删除条数。"""
        np = _numpy()
        if np is None:
            return 0
        drop = {str(value) for value in claim_ids}
        if not drop:
            return 0
        with self._lock:
            keep = [index for index, cid in enumerate(self.claim_ids)
                    if cid not in drop]
            removed = len(self.claim_ids) - len(keep)
            if not removed:
                return 0
            self.claim_ids = [self.claim_ids[index] for index in keep]
            self.memory_ids = [self.memory_ids[index] for index in keep]
            self.matrix = self.matrix[keep] if keep else self.matrix[:0]
            self.hashes = self.hashes[keep] if keep else self.hashes[:0]
            self._valid = self._compute_valid()
        return removed

    def sync(self, ledger: Any,
             on_progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """与账本对齐：删除失效 claim、重编码文本变化的 claim、追加新 claim。

        返回 {"added", "removed", "updated", "docs", "memories"}。
        claim 的"内容变化"包含 subject/predicate/value 任一变化——账本里
        claim_id 是 (memory_id, subject, predicate, slot_key) 的哈希，value 变了
        id 不变，所以内容哈希这一层是必须的（与 memory 级同款理由）。
        """
        rows = _claim_rows(ledger)
        current: Dict[str, Tuple[str, str]] = {
            claim_id: (memory_id, text) for claim_id, memory_id, text in rows}
        existing = set(self.claim_ids)
        drop: List[str] = []
        pending: List[Tuple[str, str, str]] = []
        changed = 0
        for index, claim_id in enumerate(self.claim_ids):
            entry = current.get(claim_id)
            if entry is None:
                drop.append(claim_id)
                continue
            memory_id, text = entry
            if (content_hash(text) != int(self.hashes[index])
                    or self.memory_ids[index] != memory_id):
                drop.append(claim_id)
                pending.append((claim_id, memory_id, text))
                changed += 1
        for claim_id, (memory_id, text) in current.items():
            if claim_id not in existing:
                pending.append((claim_id, memory_id, text))
        removed = self.remove(drop)
        added = 0
        if pending:
            if on_progress is not None:
                try:
                    on_progress("encoding %d new/changed claims" % len(pending))
                except Exception:
                    pass
            added = self.add_many(pending)
        if removed or added:
            self.built_at = time.time()
        return {"added": added, "removed": removed, "updated": changed,
                "docs": len(self.claim_ids),
                "memories": len(set(self.memory_ids))}

    # -- 持久化 -----------------------------------------------------------
    def save(self, path: Optional[os.PathLike] = None) -> Optional[Path]:
        """原子落盘（写临时文件再 os.replace），失败返回 None。"""
        np = _numpy()
        if np is None:
            return None
        target = (Path(path).expanduser() if path
                  else (self.path or default_claim_index_path()))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        try:
            with open(temporary, "wb") as handle:
                np.savez(
                    handle,
                    format_version=np.asarray([CLAIM_FORMAT_VERSION], dtype=np.int64),
                    claim_ids=np.asarray(self.claim_ids, dtype="U64"),
                    memory_ids=np.asarray(self.memory_ids, dtype="U64"),
                    matrix=self.matrix,
                    hashes=self.hashes,
                    fingerprint=np.asarray([self.fingerprint], dtype="U64"),
                    model=np.asarray([self.model], dtype="U64"),
                    built_at=np.asarray([float(self.built_at)], dtype=np.float64),
                )
            os.replace(temporary, target)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            return None
        self.path = target
        return target

    @classmethod
    def load(cls, embedder: Any, path: Optional[os.PathLike] = None
             ) -> Optional["ClaimVectorIndex"]:
        """读索引；文件缺失 / 损坏 / 指纹或维度不匹配时返回 None（调用方重建）。"""
        np = _numpy()
        if np is None or embedder is None:
            return None
        target = Path(path).expanduser() if path else default_claim_index_path()
        try:
            with np.load(target, allow_pickle=False) as payload:
                version = int(np.asarray(payload["format_version"]).reshape(-1)[0])
                if version != CLAIM_FORMAT_VERSION:
                    return None
                claim_ids = [str(value) for value in
                             np.asarray(payload["claim_ids"]).tolist()]
                memory_ids = [str(value) for value in
                              np.asarray(payload["memory_ids"]).tolist()]
                matrix = np.asarray(payload["matrix"], dtype=np.float32)
                hashes = np.asarray(payload["hashes"], dtype=np.uint64)
                fingerprint = str(np.asarray(payload["fingerprint"]).reshape(-1)[0])
                model = str(np.asarray(payload["model"]).reshape(-1)[0])
                built_at = float(np.asarray(payload["built_at"]).reshape(-1)[0])
        except Exception:
            return None
        if fingerprint != fingerprint_of(embedder):
            return None
        if matrix.ndim != 2 or matrix.shape[0] != len(claim_ids):
            return None
        expected = int(getattr(embedder, "dim", 0) or 0)
        if expected and matrix.shape[1] != expected:
            return None
        try:
            return cls(embedder, claim_ids, memory_ids, matrix, hashes=hashes,
                       fingerprint=fingerprint, built_at=built_at, model=model,
                       path=target)
        except (ValueError, RuntimeError):
            return None

    @classmethod
    def build(cls, ledger: Any, embedder: Any, *,
              path: Optional[os.PathLike] = None,
              on_progress: Optional[Callable[[str], None]] = None
              ) -> Optional["ClaimVectorIndex"]:
        """从账本全量构建；embedder 不可用 / numpy 缺失 / 无 claim 时返回 None。"""
        np = _numpy()
        if np is None or embedder is None:
            return None
        rows = _claim_rows(ledger)
        dim = int(getattr(embedder, "dim", 0) or 0)
        if not rows or dim <= 0:
            return None
        try:
            index = cls(embedder, [], [], np.zeros((0, dim), dtype=np.float32),
                        path=path)
            if on_progress is not None:
                try:
                    on_progress("building claim index for %d claims" % len(rows))
                except Exception:
                    pass
            index.add_many(rows)
        except Exception:
            return None
        index.built_at = time.time()
        return index if len(index) else None

    @classmethod
    def load_or_build(cls, ledger: Any, embedder: Any, *,
                      path: Optional[os.PathLike] = None,
                      sync: bool = False,
                      on_progress: Optional[Callable[[str], None]] = None
                      ) -> Optional["ClaimVectorIndex"]:
        """先读盘（快），失败或 sync=True 时对齐/重建。"""
        target = Path(path).expanduser() if path else default_claim_index_path()
        index = cls.load(embedder, target)
        if index is None:
            index = cls.build(ledger, embedder, path=target, on_progress=on_progress)
            if index is None:
                return None
            index.save(target)
            return index
        if sync:
            index.sync(ledger, on_progress=on_progress)
            index.save(target)
        return index

    # -- 自描述 -----------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """claims / memories / dim / bytes / built_at / model / granularity。"""
        np = _numpy()
        valid = int(self._valid.sum()) if np is not None else len(self.claim_ids)
        return {
            "granularity": GRANULARITY_CLAIM,
            "docs": len(self.claim_ids),
            "claims": len(self.claim_ids),
            "memories": len(set(self.memory_ids)),
            "dim": self.dim,
            "bytes": int(self.matrix.nbytes) if np is not None else 0,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.localtime(self.built_at or time.time())),
            "model": self.model,
            "fingerprint": self.fingerprint,
            "valid_docs": valid,
            "path": str(self.path) if self.path else "",
        }


def load_vector_index(ledger: Any, embedder: Any, *,
                      path: Optional[os.PathLike] = None,
                      sync: bool = False,
                      on_progress: Optional[Callable[[str], None]] = None
                      ) -> Optional[VectorIndex]:
    """便捷入口：任何异常都返回 None（检索路径不得因向量索引失败而报错）。"""
    try:
        return VectorIndex.load_or_build(ledger, embedder, path=path, sync=sync,
                                         on_progress=on_progress)
    except Exception:
        return None


def load_claim_vector_index(ledger: Any, embedder: Any, *,
                            path: Optional[os.PathLike] = None,
                            sync: bool = False,
                            build: bool = True,
                            on_progress: Optional[Callable[[str], None]] = None
                            ) -> Optional[ClaimVectorIndex]:
    """claim 级便捷入口：任何异常都返回 None。

    `build=False` 只读盘、绝不重建。**请求路径必须用它**：全量构建实测
    56-86s，而注入路径的预算是 700ms；把构建放进请求里等于把一次冷启动
    变成一次超时。构建由 daemon 的后台循环负责。
    """
    try:
        if not build:
            target = Path(path).expanduser() if path else default_claim_index_path()
            return ClaimVectorIndex.load(embedder, target)
        return ClaimVectorIndex.load_or_build(ledger, embedder, path=path,
                                              sync=sync, on_progress=on_progress)
    except Exception:
        return None


def load_granular_index(ledger: Any, embedder: Any, *,
                        granularity: Optional[str] = None,
                        path: Optional[os.PathLike] = None,
                        sync: bool = False,
                        build: bool = True,
                        on_progress: Optional[Callable[[str], None]] = None
                        ) -> Optional[Any]:
    """按粒度加载索引；claim 优先、缺失时回退 memory 级、都失败返回 None。

    回退顺序是**有意的**：claim 级实测更好，但"claim 表为空 / claim 索引文件
    写不出来"（例如账本还没跑过提炼）不该等于"没有向量通道"。回退到 memory
    级至少不劣于改造前。

    `build=False` 时两个粒度都只读盘、不重建——请求路径（注入）必须用它，
    因为全量构建实测 56-86s，远超注入预算；构建归 daemon 后台循环。

    返回的对象都实现 `search_text(query, topn) -> [(memory_id, cosine, ...)]`
    与 `stats()`，ledger.search 不关心具体是哪个类。
    """
    chosen = str(granularity or configured_granularity()).strip().lower()
    if chosen not in GRANULARITIES:
        chosen = configured_granularity()
    if chosen == GRANULARITY_MEMORY:
        return _load_memory_index(ledger, embedder, path=path, sync=sync,
                                  build=build, on_progress=on_progress)
    claim_path = Path(path).expanduser() if path else None
    index = load_claim_vector_index(ledger, embedder, path=claim_path, sync=sync,
                                    build=build, on_progress=on_progress)
    if index is not None:
        return index
    return _load_memory_index(ledger, embedder, sync=sync, build=build,
                              on_progress=on_progress)


def _load_memory_index(ledger: Any, embedder: Any, *,
                       path: Optional[os.PathLike] = None,
                       sync: bool = False, build: bool = True,
                       on_progress: Optional[Callable[[str], None]] = None
                       ) -> Optional[VectorIndex]:
    """memory 级加载；`build=False` 时只读盘。"""
    try:
        if not build:
            target = Path(path).expanduser() if path else default_index_path()
            return VectorIndex.load(embedder, target)
        return VectorIndex.load_or_build(ledger, embedder, path=path, sync=sync,
                                         on_progress=on_progress)
    except Exception:
        return None
