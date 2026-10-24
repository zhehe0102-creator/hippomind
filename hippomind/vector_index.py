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
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .schema import fts_indexed_predicate
from .semantic_onnx import hub_home

INDEX_FILENAME = "vector_index.npz"
DEFAULT_TOPN = 96
DEFAULT_SYNC_BATCH = 64
#: 索引文件格式版本：布局变化时递增，旧文件自动被当作"指纹不匹配"重建。
FORMAT_VERSION = 1


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
