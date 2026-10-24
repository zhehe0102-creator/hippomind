"""可选神经向量通道：ONNX Runtime + HuggingFace tokenizers（纯可选依赖）。

这个模块把"账本自拟合 LSA / 字符 n-gram"升级成真实的神经句向量，同时保持
embeddings.EmbeddingAdapter 的同一接口（embed / cosine），因此任何现有调用方
（ledger.search(embedder=...)、HybridRanker、ContextPack）不需要改动就能用上它。

设计契约（每条都有测试锁定）：

* **零硬依赖**：onnxruntime / tokenizers / 模型文件任一缺失时，
  load_onnx_embedder() 返回 None，**绝不抛异常**。检索路径不能因为可选组件
  缺失而失败——与 embeddings.LocalVectorEmbedding.load 同一契约。
* **路径从 HIPPOMIND_HOME 推导**：$HIPPOMIND_HOME/vendor（Python 依赖）与
  $HIPPOMIND_HOME/models/<name>（模型文件）。三者都可用环境变量覆盖：
  HIPPOMIND_VENDOR_DIR / HIPPOMIND_MODELS_DIR / HIPPOMIND_ONNX_MODEL。
  依赖只装在内置盘、不污染系统 Python，因此 vendor 目录会被按需加入 sys.path。
* **必须分批 encode**：一次性把 6k+ 文本交给 tokenizers 会构造
  (n, seq, hidden) 的中间张量（实测 6442 条吃掉 41GB RSS，把机器拖死）。
  encode_matrix() 强制按 batch_size（默认 64）切片。
* **空文本 → 零向量**：模型对空串输出的是 [CLS]/[SEP] 池化结果，语义上什么
  都不是，但它不是零向量，直接参与矩阵乘会污染 top-k。这里显式返回零行，
  由 hippomind.vector_index 在检索时屏蔽。
* **会话惰性创建**：InferenceSession 只在第一次真正 encode 时建立（daemon 启动
  不能被模型加载阻塞）；单次建会话实测 0.03s、峰值 RSS ~116MB。

模型选型（Lead 实测中文区分度 margin：bgezh 0.335 / minilm 0.276 / e5small
0.091）与"向量只做重排会掉分、参与候选生成才有正收益"的实测结论，见
docs/SEMANTIC.md 的「神经向量」章节。
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

DEFAULT_MODEL = "bgezh"
DEFAULT_MAX_LENGTH = 256
DEFAULT_BATCH_SIZE = 64
DEFAULT_THREADS = 2
DEFAULT_CACHE_SIZE = 2048

MODEL_FILENAME = "onnx_model_quantized.onnx"
TOKENIZER_FILENAME = "tokenizer.json"
CONFIG_FILENAME = "config.json"

ENV_HOME = "HIPPOMIND_HOME"
ENV_VENDOR = "HIPPOMIND_VENDOR_DIR"
ENV_MODELS = "HIPPOMIND_MODELS_DIR"
ENV_MODEL = "HIPPOMIND_ONNX_MODEL"


class OnnxUnavailable(RuntimeError):
    """可选 ONNX 栈或模型文件不可用（调用方应降级，而不是重试）。"""


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
# 与 ledger._helpers.default_hub_home 保持同一套规则（有测试断言两者一致）：
# 这里不 import 它，是为了让本模块在 hippomind.ledger 尚未初始化时也能安全
# 导入（vector_index 依赖本模块，ledger 又是热路径，避免循环导入风险）。

def hub_home() -> Path:
    """$HIPPOMIND_HOME 或平台默认的 HippoMind 数据目录。"""
    configured = os.environ.get(ENV_HOME, "").strip()
    if configured:
        return Path(configured).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "HippoMind"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata).expanduser() if appdata else home / "AppData" / "Roaming"
        return base / "HippoMind"
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state).expanduser() if state else home / ".local" / "state"
    return base / "hippomind"


def vendor_dir() -> Path:
    """内置依赖目录（onnxruntime / tokenizers 等）。"""
    configured = os.environ.get(ENV_VENDOR, "").strip()
    return Path(configured).expanduser() if configured else hub_home() / "vendor"


def models_dir() -> Path:
    """模型根目录，每个模型一个子目录。"""
    configured = os.environ.get(ENV_MODELS, "").strip()
    return Path(configured).expanduser() if configured else hub_home() / "models"


def default_model() -> str:
    """$HIPPOMIND_ONNX_MODEL 或默认模型名（bgezh 的中文区分度最好）。"""
    return os.environ.get(ENV_MODEL, "").strip() or DEFAULT_MODEL


def index_path() -> Path:
    """向量索引落盘位置（与模型/依赖分离，便于单独删除重建）。"""
    return hub_home() / "vector_index.npz"


# ---------------------------------------------------------------------------
# 可选依赖加载
# ---------------------------------------------------------------------------

_RUNTIME_LOCK = threading.Lock()
_RUNTIME: Dict[str, Any] = {}


def _import_runtime(vendor: Optional[Path]) -> Tuple[Any, Any]:
    """import onnxruntime + tokenizers；必要时把 vendor 目录加入 sys.path。"""
    errors: List[str] = []
    for attempt in (0, 1):
        try:
            import onnxruntime as ort  # noqa: F401
            from tokenizers import Tokenizer  # noqa: F401
            return ort, Tokenizer
        except Exception as exc:  # ImportError / OSError（缺 dylib）都算不可用
            errors.append("%s: %s" % (type(exc).__name__, exc))
            if attempt:
                break
            if vendor is None or not Path(vendor).is_dir():
                break
            if str(vendor) not in sys.path:
                sys.path.insert(0, str(vendor))
    raise OnnxUnavailable("onnxruntime/tokenizers 不可用（%s）" % "; ".join(errors))


def runtime(vendor: Optional[Path] = None) -> Tuple[Any, Any]:
    """返回 (onnxruntime, Tokenizer)；不可用时抛 OnnxUnavailable。

    结果按 vendor 路径缓存，失败也缓存——避免每次检索都重试 sys.path 探测。
    """
    key = str(vendor if vendor is not None else vendor_dir())
    with _RUNTIME_LOCK:
        cached = _RUNTIME.get(key)
        if cached is None:
            try:
                cached = _import_runtime(Path(vendor) if vendor is not None else vendor_dir())
            except OnnxUnavailable as exc:
                cached = exc
            _RUNTIME[key] = cached
    if isinstance(cached, OnnxUnavailable):
        raise cached
    return cached


def reset_runtime_cache() -> None:
    """测试用：清空依赖加载缓存（生产代码不应调用）。"""
    with _RUNTIME_LOCK:
        _RUNTIME.clear()


def available(vendor: Optional[Path] = None) -> bool:
    """依赖是否可导入（不代表模型文件存在）。"""
    try:
        runtime(vendor)
        return True
    except OnnxUnavailable:
        return False


def dependency_error(vendor: Optional[Path] = None) -> str:
    """不可用原因；可用时返回空串（供 doctor / 诊断输出）。"""
    try:
        runtime(vendor)
        return ""
    except OnnxUnavailable as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# 编码器：BERT 系 ONNX 模型 + tokenizers（mean pooling，不含归一化）
# ---------------------------------------------------------------------------

class TextEncoder(Protocol):
    """编码器最小协议：encode(texts) 返回 (n, dim) 未归一化矩阵。"""

    dim: int

    def encode(self, texts: Sequence[str]) -> Any: ...


def _model_id(model_dir: Path, onnx_path: Path, tokenizer_path: Path,
              dim: int, max_length: int) -> str:
    """模型指纹的一部分：文件大小 + config.json 内容摘要（刻意不用 mtime）。

    不用 mtime 的理由：模型在磁盘间复制（rsync / 备份恢复）会改变 mtime 却不
    改变内容，用它会让 13MB 索引白重建一次（实测全量重建 170s）。大小 + config
    摘要足以识别"换了一个模型"，同时对复制稳定。
    """
    parts = [model_dir.name, "dim=%d" % dim, "len=%d" % max_length, "pool=mean"]
    for path in (onnx_path, tokenizer_path):
        try:
            parts.append("%s:%d" % (path.name, path.stat().st_size))
        except OSError:
            parts.append("%s:missing" % path.name)
    try:
        digest = hashlib.sha256((model_dir / CONFIG_FILENAME).read_bytes()).hexdigest()[:12]
        parts.append("config:%s" % digest)
    except OSError:
        pass
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


class BertOnnxEncoder:
    """BERT 系 ONNX 模型的最小编码器（mean pooling over attention mask）。

    encode() 返回**未归一化**的 (n, dim) float32；归一化与空文本处理统一由
    OnnxEmbedding 负责，这样测试可以注入假编码器而完全不碰 onnxruntime。
    """

    def __init__(self, model_dir: os.PathLike, *, max_length: int = DEFAULT_MAX_LENGTH,
                 threads: int = DEFAULT_THREADS, vendor: Optional[os.PathLike] = None):
        directory = Path(model_dir).expanduser()
        onnx_path = directory / MODEL_FILENAME
        tokenizer_path = directory / TOKENIZER_FILENAME
        if not onnx_path.is_file():
            raise OnnxUnavailable("模型文件缺失: %s" % onnx_path)
        if not tokenizer_path.is_file():
            raise OnnxUnavailable("分词器文件缺失: %s" % tokenizer_path)
        ort, tokenizer_class = runtime(Path(vendor) if vendor is not None else vendor_dir())
        self.max_length = max(8, int(max_length))
        self.directory = directory
        try:
            self.tokenizer = tokenizer_class.from_file(str(tokenizer_path))
            self.tokenizer.enable_truncation(max_length=self.max_length)
            options = ort.SessionOptions()
            options.intra_op_num_threads = max(1, int(threads))
            # 静默 ORT 的 warning/info 噪声：会污染 CLI 输出与测试断言。
            options.log_severity_level = 3
            self.session = ort.InferenceSession(
                str(onnx_path), options, providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise OnnxUnavailable("ONNX 会话创建失败（%s）: %s" % (type(exc).__name__, exc))
        self.input_names = {item.name for item in self.session.get_inputs()}
        shape = self.session.get_outputs()[0].shape
        try:
            self.dim = int(shape[-1])
        except (TypeError, ValueError, IndexError):
            raise OnnxUnavailable("无法从模型输出形状推断维度: %r" % (shape,))
        self.model_id = _model_id(directory, onnx_path, tokenizer_path,
                                  self.dim, self.max_length)

    def encode(self, texts: Sequence[str]) -> Any:
        import numpy as np

        items = [str(text) if str(text or "").strip() else " " for text in texts]
        encodings = self.tokenizer.encode_batch(items)
        length = max((len(encoding.ids) for encoding in encodings), default=1) or 1
        ids = np.zeros((len(encodings), length), dtype=np.int64)
        mask = np.zeros((len(encodings), length), dtype=np.int64)
        for row, encoding in enumerate(encodings):
            span = min(length, len(encoding.ids))
            ids[row, :span] = encoding.ids[:span]
            mask[row, :span] = encoding.attention_mask[:span]
        feed: Dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
        # bge/bert 系模型的 token_type_ids 是**必填**输入：漏掉会抛
        # ValueError: Required inputs (['token_type_ids']) are missing.
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feed)[0]
        weights = mask[:, :, None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
        return np.asarray(pooled, dtype=np.float32)


# ---------------------------------------------------------------------------
# EmbeddingAdapter：归一化 + 分批 + 有界 LRU
# ---------------------------------------------------------------------------

class OnnxEmbedding:
    """ONNX 句向量适配器：embed / embed_many / encode_matrix / cosine。

    encoder 可注入（测试用假编码器即可，无需 onnxruntime 与模型文件）。
    """

    def __init__(self, encoder: TextEncoder, *, name: str = "",
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 cache_size: int = DEFAULT_CACHE_SIZE):
        self.encoder = encoder
        self.dim = int(getattr(encoder, "dim", 0) or 0)
        if self.dim <= 0:
            raise OnnxUnavailable("编码器未报告有效维度")
        self.name = name or "onnx"
        self.batch_size = max(1, int(batch_size))
        self.cache_size = max(0, int(cache_size))
        self._cache: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.RLock()
        # 诊断计数：测试用它证明"缓存命中时不会重复编码"。
        self.encode_calls = 0
        self.encode_items = 0

    # -- 缓存 -------------------------------------------------------------
    def _cache_get(self, text: str):
        if self.cache_size <= 0:
            return None
        with self._lock:
            vector = self._cache.get(text)
            if vector is None:
                return None
            self._cache.move_to_end(text)
            return vector

    def _cache_put(self, text: str, vector) -> None:
        if self.cache_size <= 0:
            return
        import numpy as np

        with self._lock:
            self._cache[text] = np.asarray(vector, dtype=np.float32).copy()
            self._cache.move_to_end(text)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    # -- 编码 -------------------------------------------------------------
    def _run_encoder(self, texts: Sequence[str]):
        import numpy as np

        raw = np.asarray(self.encoder.encode(list(texts)), dtype=np.float32)
        if raw.ndim == 1:
            raw = raw.reshape(1, -1)
        if raw.shape != (len(texts), self.dim):
            raise OnnxUnavailable(
                "编码器输出形状不符：期望 %r，实际 %r"
                % ((len(texts), self.dim), raw.shape))
        # 量化模型的极端输入可能产生 NaN/Inf；一个 NaN 就能让整行 top-k 失效。
        raw = np.nan_to_num(raw, nan=0.0, posinf=0.0, neginf=0.0)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        with self._lock:
            self.encode_calls += 1
            self.encode_items += len(texts)
        return (raw / norms).astype(np.float32)

    def encode_matrix(self, texts: Sequence[str]):
        """批量编码 → (n, dim) float32，行已 L2 归一化，空文本为零行。"""
        import numpy as np

        items = [str(text) if text is not None else "" for text in texts]
        matrix = np.zeros((len(items), self.dim), dtype=np.float32)
        if not items:
            return matrix
        pending: List[str] = []
        rows_of: Dict[str, List[int]] = {}
        for row, text in enumerate(items):
            if not text.strip():
                continue
            cached = self._cache_get(text)
            if cached is not None:
                matrix[row] = cached
                continue
            rows = rows_of.get(text)
            if rows is None:
                rows_of[text] = [row]
                pending.append(text)
            else:
                rows.append(row)
        # 强制分批：这是 41GB 事故的直接防线，不允许"反正只有几千条"。
        for start in range(0, len(pending), self.batch_size):
            chunk = pending[start:start + self.batch_size]
            vectors = self._run_encoder(chunk)
            for offset, text in enumerate(chunk):
                vector = vectors[offset]
                self._cache_put(text, vector)
                for row in rows_of[text]:
                    matrix[row] = vector
        return matrix

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        """批量接口：返回 Python list（跨进程 / JSON 友好）。大批量请用 encode_matrix。"""
        return [row.tolist() for row in self.encode_matrix(texts)]

    def embed(self, text: str) -> List[float]:
        return self.embed_many([text])[0]

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        import numpy as np

        try:
            left = np.asarray(a, dtype=np.float32).reshape(-1)
            right = np.asarray(b, dtype=np.float32).reshape(-1)
        except (TypeError, ValueError):
            return 0.0
        if left.size == 0 or left.size != right.size:
            return 0.0
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        if denominator <= 0.0 or not math.isfinite(denominator):
            return 0.0
        value = float(np.dot(left, right) / denominator)
        if not math.isfinite(value):
            return 0.0
        return max(-1.0, min(1.0, value))

    # -- 自描述 -----------------------------------------------------------
    def fingerprint(self) -> str:
        """索引指纹：模型变了 / 维度变了，旧索引必须重建。"""
        payload = "|".join([
            "onnx", self.name, "dim=%d" % self.dim,
            str(getattr(self.encoder, "model_id", "") or ""),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def params(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "dim": self.dim,
            "batch_size": self.batch_size,
            "cache_size": self.cache_size,
            "max_length": int(getattr(self.encoder, "max_length", 0) or 0),
            "pooling": "mean",
            "fingerprint": self.fingerprint(),
        }


# ---------------------------------------------------------------------------
# 工厂：任何失败都返回 None
# ---------------------------------------------------------------------------

def model_path(name: Optional[str] = None, directory: Optional[os.PathLike] = None) -> Path:
    root = Path(directory).expanduser() if directory is not None else models_dir()
    return root / (str(name or default_model()).strip() or DEFAULT_MODEL)


def model_available(name: Optional[str] = None,
                    directory: Optional[os.PathLike] = None) -> bool:
    """模型文件是否齐全（不建会话，不加载依赖）。"""
    target = model_path(name, directory)
    return (target / MODEL_FILENAME).is_file() and (target / TOKENIZER_FILENAME).is_file()


def load_onnx_embedder(model: Optional[str] = None, *,
                       directory: Optional[os.PathLike] = None,
                       vendor: Optional[os.PathLike] = None,
                       max_length: int = DEFAULT_MAX_LENGTH,
                       threads: int = DEFAULT_THREADS,
                       batch_size: int = DEFAULT_BATCH_SIZE,
                       cache_size: int = DEFAULT_CACHE_SIZE) -> Optional[OnnxEmbedding]:
    """构造 ONNX 向量通道；依赖 / 模型缺失或任何异常都返回 None。

    这是**唯一**推荐入口：调用方（daemon / 脚本）拿到 None 就退回纯词法检索，
    不需要 try/except，也不可能因为可选组件缺失而让检索失败。
    """
    try:
        name = str(model or default_model()).strip() or DEFAULT_MODEL
        encoder = BertOnnxEncoder(model_path(name, directory), max_length=max_length,
                                  threads=threads, vendor=vendor)
        return OnnxEmbedding(encoder, name=name, batch_size=batch_size,
                             cache_size=cache_size)
    except Exception:
        return None


def describe(model: Optional[str] = None,
             directory: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """诊断用：依赖 / 模型 / 路径状态（永不抛异常，供 doctor 打印）。"""
    name = str(model or default_model()).strip() or DEFAULT_MODEL
    target = model_path(name, directory)
    return {
        "model": name,
        "model_dir": str(target),
        "model_files": model_available(name, directory),
        "vendor_dir": str(vendor_dir()),
        "runtime_available": available(),
        "runtime_error": dependency_error(),
        "onnx_path": str(target / MODEL_FILENAME),
        "tokenizer_path": str(target / TOKENIZER_FILENAME),
    }
