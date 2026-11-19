"""Hippocampus —— 给 Agent 搭建的海马体外部记忆系统（融合心潮·念/Ombre Brain 概念）。

核心机制：
  1. 升权（Upweight）：回忆/复用/成功 → 记忆强度上升（再巩固效应）
  2. 降权（Downweight）：遗忘曲线（Ebbinghaus）随不使用指数衰减；
     重复经历被巩固成语义知识后，原情景记忆自然"退居二线"
  3. 四维检索评分（Ombre Brain breath）：
       文本相关(topic) + 情感共鸣(emotion, Russell 环形)
       + 时间亲近(time) + 触碰频率(touch)，再乘显著性×重要性
     关键细节：回忆只增加 touch，不重置 last_active（防旧记忆"被回忆就变新"）
  4. 巩固（Consolidation）：重复情景提炼成语义规则（情景 → 皮层固化）
  5. 梦境消化（Dream）：把最近变动的记忆变成温和的"梦"，不冒充真实记忆、不自噬
  6. 联想（Association）：记忆按相似度建连接，回忆时联想扩散（spreading activation）
  7. 自适应：弱记忆/弱连接被剪枝（prune）
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import tempfile
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .memory import (EpisodicMemory, SemanticMemory, Association,
                     memory_to_dict, memory_from_dict)

# Persisted memories may contain private cross-agent context. Keep individual
# fields bounded so a malformed import cannot turn one entry into the whole
# active memory budget or make a later JSON save fail.
MAX_MEMORY_TEXT_CHARS = 12_000
MAX_TAGS = 32
MAX_TAG_CHARS = 96
MAX_FEATURES = 256
MAX_METADATA_CHARS = 32_000
MAX_PERSIST_BYTES = 64 * 1024 * 1024


def _bounded_float(value, default: float = 0.0, lower: Optional[float] = None,
                   upper: Optional[float] = None) -> float:
    """Convert an external numeric value while rejecting NaN/Infinity."""
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    if lower is not None:
        number = max(lower, number)
    if upper is not None:
        number = min(upper, number)
    return number


def _bounded_int(value, default: int = 0, lower: Optional[int] = None,
                 upper: Optional[int] = None) -> int:
    """Integer companion to _bounded_float for JSON/configuration values."""
    try:
        if isinstance(value, bool):
            raise TypeError
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = int(default)
    if lower is not None:
        number = max(lower, number)
    if upper is not None:
        number = min(upper, number)
    return number


def _short_text(value, limit: int = MAX_MEMORY_TEXT_CHARS) -> str:
    """Convert persisted/user data to a bounded text field."""
    if value is None:
        return ""
    try:
        return str(value).strip()[:limit]
    except Exception:
        return ""


def _normalise_tags(tags) -> List[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [tags]
    try:
        values = list(tags)
    except TypeError:
        return []
    clean = {_short_text(item, MAX_TAG_CHARS) for item in values}
    return sorted(tag for tag in clean if tag)[:MAX_TAGS]


def _normalise_features(features) -> Optional[List[float]]:
    if features is None or isinstance(features, (str, bytes)):
        return None
    try:
        values = list(features)
    except TypeError:
        return None
    if len(values) > MAX_FEATURES:
        values = values[:MAX_FEATURES]
    clean = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        clean.append(number)
    return clean or None


def _normalise_metadata(metadata) -> Dict:
    """Keep metadata JSON serialisable and bounded without executing it."""
    if not isinstance(metadata, dict):
        return {}
    try:
        encoded = json.dumps(metadata, ensure_ascii=False, default=str,
                             allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return {"_invalid_metadata": True}
    if len(encoded) > MAX_METADATA_CHARS:
        return {"_truncated_metadata": True}
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError):
        return {"_invalid_metadata": True}
    return value if isinstance(value, dict) else {}


def _optional_time(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value) if isinstance(value, (int, float)) else False


def _atomic_json_write(path: str, data: Dict) -> None:
    """Durably replace a JSON file without a fixed, collision-prone temp name."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                    dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        # fsync the directory where supported, so the rename survives a crash.
        try:
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _read_json(path: str) -> Dict:
    target = Path(path).expanduser()
    try:
        size = target.stat().st_size
    except OSError:
        raise
    if size > MAX_PERSIST_BYTES:
        raise ValueError(f"记忆文件超过 {MAX_PERSIST_BYTES // (1024 * 1024)} MiB 安全上限")
    with target.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("记忆文件根节点必须是 JSON 对象")
    return data


# ---------------------------------------------------------------------------
# 相似度函数
# ---------------------------------------------------------------------------


def gaussian_sim(a: List[float], b: List[float], sigma: float = 1.2) -> float:
    """结构化特征向量的高斯相似度：完全一致=1.0，越远越小。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    width = _bounded_float(sigma, 1.2, lower=1e-9)
    try:
        d2 = sum((float(x) - float(y)) ** 2 for x, y in zip(a, b))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(d2):
        return 0.0
    return math.exp(-d2 / (width * width))


# ``token_sim`` is defined once in :mod:`hippomind.textutil` so the storage layer
# no longer imports this demonstration module.  Re-exported here for the public
# API and for backwards compatibility with existing callers and tests.
from .textutil import coerce_random_seed, token_sim  # noqa: E402


EMOTION_MAX_DIST = math.sqrt(2.0)  # Russell 环形最大欧氏距离


def emotion_sim(q_emotion, m) -> float:
    """情感共鸣度（0~1，越近越高）。查询无情感坐标时给中性分 0.5（不影响排序）。"""
    if not q_emotion:
        return 0.5
    try:
        qv, qa = q_emotion
    except (TypeError, ValueError):
        return 0.5
    qv = _bounded_float(qv, 0.0, -1.0, 1.0)
    qa = _bounded_float(qa, 0.3, 0.0, 1.0)
    mv = _bounded_float(getattr(m, "valence", 0.0), 0.0, -1.0, 1.0)
    ma = _bounded_float(getattr(m, "arousal", 0.3), 0.3, 0.0, 1.0)
    d = math.sqrt((qv - mv) ** 2 + (qa - ma) ** 2)
    return max(0.0, 1.0 - d / EMOTION_MAX_DIST)


# ---------------------------------------------------------------------------
# 海马体
# ---------------------------------------------------------------------------


class Hippocampus:
    """外部记忆核心。线程不保证安全；单 Agent 演示足够。"""

    DEFAULT_CFG = {
        # --- 降权（遗忘） ---
        "decay_episodic": 0.006,       # 情景记忆遗忘速率（每秒指数衰减系数）
        "decay_semantic": 0.0002,      # 语义知识稳定得多（皮层固化后不易忘）
        "assoc_decay": 0.001,          # 联想连接的自然衰减
        "consolidated_decay_mult": 2.0,# 已提炼的情景可更快退居冷存储
        # --- 升权（强化） ---
        "recall_boost": 0.018,         # 只观察到的记忆仅轻微再巩固
        "recall_boost_semantic": 0.008,# 语义记忆更不应因热度自我垄断
        "reinforce_up": 0.18,          # 真正帮助行动后的效用升权
        "reinforce_down": 0.22,        # 误导行动时的抑制略强
        "assoc_boost": 0.08,           # 共激活联想强化
        "max_weight": 1.0,
        "min_weight": 0.08,            # 低于此值进入冷归档
        # --- 四维检索（Ombre Brain breath） ---
        "w_topic": 0.50,               # 文本/特征相关性权重
        "w_emotion": 0.25,             # 情感共鸣权重
        "w_time": 0.15,                # 时间亲近权重
        "w_touch": 0.10,               # 触碰频率权重
        "time_decay": 0.02,            # 时间亲近衰减 λ（e^{-λ·Δt}）
        "touch_cap": 10.0,             # touch 归一化上限
        # --- 去重/合并 ---
        "dedup_threshold": 0.92,       # 几乎相同的情景 → 合并并升权（重复即强化）
        "link_threshold": 0.55,        # 新记忆与已有记忆相似度超过此值 → 建联想
        "link_top_n": 4,               # 每条新记忆只连最相关的少数邻居
        "max_assoc_degree": 8,         # 图稀疏化：单节点最大联想度
        "assoc_prune": 0.12,           # 低于此强度的边可被清理
        "coactivate_threshold": 0.72,  # 只强化本来就有语义关系的共激活记忆
        # --- 巩固 ---
        "consolidate_every": 25.0,     # 每多少秒做一次巩固回放
        "consolidate_min_support": 3,  # 至少多少条相似经历才能形成规则
        "consolidate_sim": 0.72,       # 巩固聚类的相似度阈值
        "consolidate_min_weight": 0.30,# 聚类平均权重门槛
        "consolidate_seeds": 24,       # 每轮巩固的惊喜度种子预算（有界工作量）
        "consolidation_backfill": 0.15,# 规则强化回传上游情景的信用比例
        # --- 梦境消化（dream） ---
        "dream_window": 120.0,         # 最近多少时间内的变动记忆可入梦
        "dream_max": 5,                # 每次最多消化几条
        "dream_every": 40.0,           # 每多少秒做一次梦境消化
        # --- 联想扩散 ---
        "spread_steps": 2,             # 联想扩散步数
        "spread_decay": 0.6,           # 每步衰减
        # --- 检索 ---
        "feature_sigma": 1.2,          # 特征相似度宽度
        "recency_scale": 0.5,          # 新近度在检索分数中的权重
        "recall_k": 5,                 # 默认返回条数
        "max_per_action": 2,           # 为决策保留行动多样性，防单一动作刷屏
        # --- 容量与维护 ---
        "max_episodic": 180,           # 活跃情景记忆硬预算
        "max_semantic": 96,            # 活跃语义规则预算
        "max_archive": 600,            # 冷归档保留上限，最旧的先淘汰
        "auto_prune_every": 20.0,      # 自动容量维护周期
        # --- 其他 ---
        "init_weight": 0.5,
        "init_importance": 5,          # 默认重要性分级 1-10
    }

    _INT_CFG = {
        "link_top_n", "max_assoc_degree", "consolidate_min_support", "dream_max",
        "spread_steps", "recall_k", "max_per_action", "max_episodic",
        "max_semantic", "max_archive", "init_importance", "consolidate_seeds",
    }
    _UNIT_CFG = {
        "recall_boost", "recall_boost_semantic", "reinforce_up", "reinforce_down",
        "assoc_boost", "max_weight", "min_weight", "w_topic", "w_emotion",
        "w_time", "w_touch", "link_threshold", "assoc_prune",
        "coactivate_threshold", "consolidate_sim", "consolidate_min_weight",
        "spread_decay", "recency_scale", "init_weight", "consolidation_backfill",
    }
    _POSITIVE_CFG = {
        "touch_cap", "consolidate_every", "dream_window", "dream_every",
        "feature_sigma", "auto_prune_every",
    }

    @classmethod
    def _sanitize_cfg(cls, values: Dict) -> Dict:
        """Accept only finite, known configuration values from code or JSON."""
        raw = values if isinstance(values, dict) else {}
        cfg = dict(cls.DEFAULT_CFG)
        for key, default in cls.DEFAULT_CFG.items():
            if key not in raw:
                continue
            value = raw[key]
            if key in cls._INT_CFG:
                lower = 1
                if key in {"link_top_n", "max_assoc_degree", "dream_max",
                           "spread_steps", "max_episodic", "max_semantic",
                           "max_archive"}:
                    lower = 0
                upper = 10 if key == "init_importance" else 100_000
                cfg[key] = _bounded_int(value, int(default), lower, upper)
            elif key in cls._UNIT_CFG:
                cfg[key] = _bounded_float(value, float(default), 0.0, 1.0)
            elif key == "dedup_threshold":
                # Values over 1 are useful in tests to intentionally disable merge.
                cfg[key] = _bounded_float(value, float(default), 0.0, 2.0)
            elif key in cls._POSITIVE_CFG:
                cfg[key] = _bounded_float(value, float(default), 1e-9, 100_000.0)
            else:
                cfg[key] = _bounded_float(value, float(default), 0.0, 100_000.0)
        cfg["min_weight"] = min(cfg["min_weight"], cfg["max_weight"])
        return cfg

    def __init__(self, seed: Optional[int] = None, **cfg):
        self.rng = random.Random(coerce_random_seed(seed))
        self.cfg = self._sanitize_cfg({**self.DEFAULT_CFG, **cfg})

        self.time = 0.0                       # 记忆系统内部时钟（秒）
        self.episodic: Dict[int, EpisodicMemory] = {}
        self.semantic: Dict[int, SemanticMemory] = {}
        self.archive: Dict[int, EpisodicMemory] = {}
        self.assoc: Dict[Tuple[int, int], Association] = {}
        self._assoc_index: Dict[int, set] = defaultdict(set)
        self.dreams_history: deque = deque(maxlen=20)   # 梦境记录（只展示，不入检索池）
        self.next_id = 1

        self.learning_enabled = True          # 总开关（GUI 里 Space 切换）
        self.stats = {
            "stored": 0, "merged": 0, "pruned": 0,
            "archived": 0, "restored": 0, "forgotten": 0,
            "assoc_pruned": 0, "semantic_pruned": 0, "consolidated": 0,
            "reinforced": 0, "dreams": 0,
        }
        self._consolidation_timer = 0.0
        self._dream_timer = 0.0
        self._maintenance_timer = 0.0
        # 最近回报与事件（用于显示 return / food / poison 等可解释指标）
        self._recent_outcomes: deque = deque(maxlen=300)
        self._recent_events: deque = deque(maxlen=300)
        self._outcome_events: Counter = Counter()
        self._total_return = 0.0

    # ------------------------------------------------------------------
    # 基础存取
    # ------------------------------------------------------------------

    def _get(self, mid: int):
        return self.episodic.get(mid) or self.semantic.get(mid) or self.archive.get(mid)

    def all_memories(self) -> List:
        return list(self.episodic.values()) + list(self.semantic.values())

    def tick(self, dt: float = 1.0) -> None:
        """推进记忆时钟：遗忘曲线 + 周期性巩固 + 周期性梦境消化。"""
        if not self.learning_enabled:
            return
        try:
            dt = float(dt)
        except (TypeError, ValueError):
            raise ValueError("dt 必须是非负有限数")
        if not math.isfinite(dt) or dt < 0:
            raise ValueError("dt 必须是非负有限数")
        if dt == 0:
            return
        self.time += dt

        # 情景记忆遗忘（快）。不在剪枝阈值处钳位，否则永远无法进入冷归档。
        de = self.cfg["decay_episodic"] * dt
        for m in self.episodic.values():
            retention = 0.65 + 0.04 * max(1, min(10, m.importance)) + 0.25 * m.arousal
            rate = de / retention
            if m.consolidated:
                rate *= self.cfg["consolidated_decay_mult"]
            m.weight = max(0.0, m.weight * math.exp(-rate))
        # 语义记忆遗忘（慢）
        ds = self.cfg["decay_semantic"] * dt
        for m in self.semantic.values():
            m.weight = max(0.0, m.weight * math.exp(-ds))
        # 联想衰减
        da = self.cfg["assoc_decay"] * dt
        for a in self.assoc.values():
            a.strength = max(0.0, a.strength - da)

        # 周期性巩固（海马体回放）
        self._consolidation_timer += dt
        if self._consolidation_timer >= self.cfg["consolidate_every"]:
            self._consolidation_timer = 0.0
            self.consolidate()
        # 周期性梦境消化
        self._dream_timer += dt
        if self._dream_timer >= self.cfg["dream_every"]:
            self._dream_timer = 0.0
            self.dream()
        self._maintenance_timer += dt
        if self._maintenance_timer >= self.cfg["auto_prune_every"]:
            self._maintenance_timer = 0.0
            self.prune()

    # ------------------------------------------------------------------
    # 存入（情景记忆）
    # ------------------------------------------------------------------

    def store(self, situation: str, action: str = "", outcome: str = "",
              valence: float = 0.0, arousal: float = 0.3,
              lesson: str = "", tags: Optional[List[str]] = None,
              features: Optional[List[float]] = None,
              importance: Optional[int] = None, source: str = "experience",
              scope: str = "agent", pinned: bool = False,
              valid_until: Optional[float] = None,
              metadata: Optional[Dict] = None) -> int:
        """存入一条情景记忆。若与已有记忆几乎相同，则合并并升权（重复即强化）。

        返回记忆 id（-1 表示学习被关闭）。
        """
        if not self.learning_enabled:
            return -1
        situation = _short_text(situation)
        action = _short_text(action, 512)
        outcome = _short_text(outcome)
        lesson = _short_text(lesson, 4_000)
        tags = _normalise_tags(tags)
        features = _normalise_features(features)
        valence = _bounded_float(valence, 0.0, -1.0, 1.0)
        arousal = _bounded_float(arousal, 0.3, 0.0, 1.0)
        importance = _bounded_int(
            importance, self.cfg["init_importance"], 1, 10)
        source = _short_text(source, 128) or "experience"
        scope = _short_text(scope, 128) or "agent"
        valid_until = _optional_time(valid_until)
        metadata = _normalise_metadata(metadata)

        # 去重单位是「情境 + 行动 + 隔离域」。不同行动的结果绝不能相互覆盖。
        sim, existing = self._most_similar(
            situation, features, store="episodic", action=action, scope=scope)
        if existing is not None and sim >= self.cfg["dedup_threshold"]:
            n = max(1, existing.observation_count)
            existing.observation_count = n + 1
            existing.weight = min(
                self.cfg["max_weight"],
                existing.weight + self.cfg["recall_boost"] * (1.0 - existing.weight),
            )
            existing.last_active = self.time
            existing.touch_count += 1
            existing.valence = (existing.valence * n + valence) / (n + 1)
            existing.arousal = (existing.arousal * n + arousal) / (n + 1)
            existing.confidence = min(0.98, 1.0 - 1.0 / (n + 2))
            existing.importance = max(
                existing.importance,
                importance,
            )
            existing.tags = sorted(set(existing.tags) | set(tags))
            existing.digested = False
            if outcome:
                existing.outcome = outcome
            if lesson:
                existing.lesson = lesson
            if metadata:
                existing.metadata.update(metadata)
            self.stats["merged"] += 1
            return existing.id

        m = EpisodicMemory(
            id=self.next_id,
            situation=situation, action=action, outcome=outcome,
            valence=valence, arousal=arousal, lesson=lesson,
            tags=tags, features=features,
            created=self.time, last_active=self.time, last_access=self.time,
            access_count=1,
            weight=self.cfg["init_weight"],
            importance=importance,
            source=source, scope=scope, pinned=_as_bool(pinned),
            valid_until=valid_until, metadata=metadata,
        )
        self.episodic[m.id] = m
        self.next_id += 1
        self.stats["stored"] += 1
        self._link_similar(m)
        return m.id

    # ------------------------------------------------------------------
    # 回忆（四维加权检索 + 联想扩散 + 升权）
    # ------------------------------------------------------------------

    def recall(self, query: Optional[str] = None, features: Optional[List[float]] = None,
               k: Optional[int] = None, tags: Optional[List[str]] = None,
               include_semantic: bool = True,
               emotion: Optional[Tuple[float, float]] = None,
               drive_boost: Optional[Callable[[object], float]] = None,
               scope: Optional[str] = None,
               record_access: bool = True) -> List[Tuple[float, object]]:
        """四维加权检索：文本+情感+时间+触碰，乘显著性×重要性，再做联想扩散。

        - emotion=(valence, arousal)：当前情境的情感坐标，用于情感共鸣维度
        - drive_boost：驱力偏置回调，返回一个乘法系数（心潮融合用）
        被回忆的记忆升权（再巩固）并增加 touch（但不重置 last_active）。
        返回 [(score, memory), ...] 按分数降序。
        """
        if not self.learning_enabled:
            return []
        k = _bounded_int(k, self.cfg["recall_k"], 1, 100)
        tags = set(_normalise_tags(tags)) if tags else None
        features = _normalise_features(features)

        # 1) 全库四维打分
        scored: List[Tuple[float, object]] = []
        pool: List[object] = list(self.episodic.values())
        if include_semantic:
            pool += list(self.semantic.values())
        for m in pool:
            if getattr(m, "superseded_by", None) is not None:
                continue
            valid_until = getattr(m, "valid_until", None)
            if valid_until is not None and self.time >= valid_until:
                continue
            if scope is not None and getattr(m, "scope", "agent") != scope:
                continue
            if tags is not None and not (tags & set(m.tags)):
                continue
            topic = self._similarity(m, query, features)
            if topic <= 0:
                continue
            emo = emotion_sim(emotion, m)
            age_active = max(0.0, self.time - m.last_active)
            t = math.exp(-self.cfg["time_decay"] * age_active)
            touch_n = min(self.cfg["touch_cap"], max(0, m.touch_count))
            touch = math.log1p(touch_n) / math.log1p(self.cfg["touch_cap"])
            # 四维基础分（文本相似主导，弱化显著性对检索的压制）
            score = (topic * self.cfg["w_topic"] + emo * self.cfg["w_emotion"]
                     + t * self.cfg["w_time"] + touch * self.cfg["w_touch"])
            # 显著性、证据置信和实际效用只温和调制相关性。
            confidence = _bounded_float(getattr(m, "confidence", 0.5), 0.5, 0.0, 1.0)
            weight = _bounded_float(getattr(m, "weight", 0.5), 0.5, 0.0, 1.0)
            reliability = 0.55 + 0.30 * weight + 0.15 * confidence
            utility = _bounded_float(getattr(m, "utility", 0.0), 0.0, -1.0, 1.0)
            score *= reliability * (1.0 + 0.15 * utility)
            imp = int(getattr(m, "importance", 5))
            score *= (0.5 + 0.5 * min(10, max(1, imp)) / 10)
            if drive_boost is not None:
                try:
                    raw_boost = drive_boost(m)
                except (TypeError, ValueError, ArithmeticError):
                    raw_boost = 0.0
                boost = _bounded_float(raw_boost, 0.0, -0.95, 10.0)
                score *= (1.0 + boost)
            if score > 0:
                scored.append((score, m))

        scored.sort(reverse=True, key=lambda t: t[0])
        # 行动多样性：不让某一动作的近似记忆占满 top-k。
        top: List[Tuple[float, object]] = []
        overflow: List[Tuple[float, object]] = []
        per_action = Counter()
        for item in scored:
            action = getattr(item[1], "action", "")
            if action and per_action[action] >= self.cfg["max_per_action"]:
                overflow.append(item)
                continue
            top.append(item)
            if action:
                per_action[action] += 1
            if len(top) >= k:
                break
        if len(top) < k:
            chosen = {m.id for _, m in top}
            top.extend(item for item in overflow if item[1].id not in chosen)
            top = top[:k]

        # 2) 被回忆者升权（再巩固）+ 增加 touch（但不重置 last_active）
        if record_access and top:
            peak = top[0][0]
            for score, m in top:
                boost = (self.cfg["recall_boost_semantic"]
                         if isinstance(m, SemanticMemory) else self.cfg["recall_boost"])
                relative = score / peak if peak > 0 else 0.0
                m.weight = min(
                    self.cfg["max_weight"],
                    m.weight + boost * relative * (1.0 - m.weight),
                )
                m.access_count += 1
                m.touch_count += 1
                m.last_access = self.time

        # 3) 联想扩散：从被回忆的记忆出发，激活邻居
        seeds = [m for _, m in top]
        activated = self._spread(seeds)
        if record_access:
            self._coactivate(seeds)

        # 4) 合并结果（top 优先，联想激活者补充）
        seen = {id(m) for _, m in top}
        results = list(top)
        direct_scores = {m.id: score for score, m in scored}
        peak = top[0][0] if top else 0.0
        for act, m in activated:
            if id(m) not in seen:
                if getattr(m, "superseded_by", None) is not None:
                    continue
                valid_until = getattr(m, "valid_until", None)
                if valid_until is not None and valid_until <= self.time:
                    continue
                direct = direct_scores.get(m.id, 0.0)
                if direct <= 0 and act < self.cfg.get("min_spread_act", 0.25):
                    continue
                results.append((0.75 * direct + 0.25 * min(1.0, act) * peak, m))
                seen.add(id(m))
        results.sort(reverse=True, key=lambda t: t[0])
        return results[:k]

    def reinforce(self, memory_id: int, delta: float, note: str = "") -> None:
        """直接对某条记忆升权（delta>0）或降权（delta<0）。属于修改，会刷新 last_active。"""
        m = self._get(memory_id)
        if m is None:
            return
        signal = _bounded_float(delta, 0.0, -1.0, 1.0)
        m.utility = (0.80 * _bounded_float(getattr(m, "utility", 0.0), 0.0, -1.0, 1.0)
                     + 0.20 * signal)
        if signal >= 0:
            gain = self.cfg["reinforce_up"] * signal * (1.0 - m.weight)
            m.weight = min(self.cfg["max_weight"], m.weight + gain)
            m.success_count = int(getattr(m, "success_count", 0)) + 1
            if signal >= 0.5:
                m.importance = min(10, m.importance + 1)
        else:
            loss = self.cfg["reinforce_down"] * min(1.0, abs(signal))
            m.weight = max(0.0, m.weight - loss)
            m.failure_count = int(getattr(m, "failure_count", 0)) + 1
            if signal <= -0.5:
                m.importance = min(10, m.importance + 1)  # 危险也是重要记忆
        m.last_access = self.time
        m.last_active = self.time          # 修改刷新激活时间
        m.access_count += 1
        m.touch_count += 1
        self.stats["reinforced"] += 1
        if note:
            m.lesson = note
        # 巩固规则效用回填：语义规则被真实结果强化时，把有界比例的信用回传给
        # 提炼它的上游情景——「只有真正参与决策的记忆获得信用」闭环的最后一段。
        # 仅正向回传（规则的失败可能源于泛化误差，不应惩罚原始正确经历），
        # 且只触达仍在活跃池的上游、数量有界，防止长链放大。
        if (signal > 0 and isinstance(m, SemanticMemory)
                and getattr(m, "source_episodes", None)):
            backfill_gain = self.cfg["reinforce_up"] * signal * self.cfg.get(
                "consolidation_backfill", 0.25)
            for episode_id in list(m.source_episodes)[:8]:
                episode = self.episodic.get(episode_id)
                if episode is None or episode.pinned:
                    continue
                # 只做权重信用，不增加 touch：touch 会提高召回频率，
                # 实测会把 Agent 引回高风险情境（毒药次数上升）。
                episode.weight = min(self.cfg["max_weight"],
                                     episode.weight + backfill_gain
                                     * (1.0 - episode.weight))
                episode.last_access = self.time

    def note_outcome(self, valence: float, event: str = "move") -> None:
        """记录环境结果；事件类型与塑形奖励分开统计。"""
        # Environment rewards can exceed the valence range (the demo's food is
        # +2). Only reject non-finite values here; do not rewrite experiment data.
        signal = _bounded_float(valence, 0.0, -1_000_000.0, 1_000_000.0)
        self._recent_outcomes.append(signal)
        event = _short_text(event, 64) or "move"
        self._recent_events.append(event)
        self._outcome_events[event] += 1
        self._total_return += signal

    @property
    def accuracy(self) -> float:
        """最近一段时间内结果非负（不亏）的比例。"""
        if not self._recent_outcomes:
            return 0.0
        return sum(1 for v in self._recent_outcomes if v >= 0) / len(self._recent_outcomes)

    # ------------------------------------------------------------------
    # 梦境消化（dream）—— 安全：不冒充真实记忆，不自噬
    # ------------------------------------------------------------------

    def dream(self, window: Optional[float] = None, max_dreams: Optional[int] = None) -> List[str]:
        """把最近变动的记忆消化成温和的"梦"。

        梦只做展示与「已消化」标记，绝不进入检索池，也不反向强化源记忆
        （对应心潮念的"梦境安全化"：梦是消化残渣、不冒充真实记忆）。
        返回本次生成的梦境文本列表。
        """
        if not self.learning_enabled:
            return []
        window = window if window is not None else self.cfg["dream_window"]
        max_dreams = max_dreams if max_dreams is not None else self.cfg["dream_max"]
        cands = [m for m in self.episodic.values()
                 if m.last_active >= self.time - window and not m.digested]
        cands.sort(key=lambda m: (m.importance, abs(m.valence), m.weight), reverse=True)
        dreams = []
        for m in cands[:max_dreams]:
            dreams.append(f"梦里回到「{m.situation[:42]}」，做了{m.action or '某事'}，{m.outcome or '无结果'}")
            m.digested = True
        if dreams:
            self.dreams_history.append({"time": self.time, "texts": dreams})
            self.stats["dreams"] += len(dreams)
        return dreams

    # ------------------------------------------------------------------
    # 巩固：情景 → 语义（海马体向皮层固化）
    # ------------------------------------------------------------------

    def consolidate(self) -> int:
        """回放聚类：把反复出现的相似情景提炼成语义规则。

        被巩固的情景记忆会降权（已完成使命，退居二线），
        语义知识则以慢得多的遗忘速率长期保存。
        返回新生成的语义记忆条数。
        """
        if not self.learning_enabled:
            return 0
        eps = [m for m in self.episodic.values()
               if not m.consolidated and m.superseded_by is None]
        if not eps:
            return 0

        # 惊喜度加权巩固（surprise-weighted consolidation）：固定预算内优先
        # 提炼强结果/失败/高重要性的重复经历；记忆研究基准显示其保留率
        # 显著优于按时间的朴素策略。种子有界聚类：每轮只围绕 K 个种子做
        # 相似扫描，把 O(n²) 全两两比较降为 O(K·n)，长期运行不随库增长变慢。
        def surprise(m: EpisodicMemory) -> float:
            outcome_signal = 0.5 * abs(_bounded_float(m.valence, 0.0, -1.0, 1.0))
            failure_signal = 0.3 if m.valence < 0 else 0.0
            importance_signal = 0.2 * max(1, min(10, m.importance)) / 10.0
            recurrence = math.log1p(max(1, m.observation_count))
            return (outcome_signal + failure_signal + importance_signal) * recurrence

        seed_budget = _bounded_int(self.cfg.get("consolidate_seeds", 24), 24, 1, 256)
        ordered = sorted(eps, key=lambda m: (-surprise(m), m.id))
        seeds = ordered[:seed_budget]

        used = set()
        clusters = []
        for seed in seeds:
            if seed.id in used:
                continue
            cluster = [seed]
            used.add(seed.id)
            for o in eps:
                if o.id in used:
                    continue
                if (o.action == seed.action and o.scope == seed.scope
                        and self._memory_similarity(seed, o) >= self.cfg["consolidate_sim"]):
                    cluster.append(o)
                    used.add(o.id)
            clusters.append(cluster)

        made = 0
        for cl in clusters:
            support = sum(max(1, x.observation_count) for x in cl)
            if support < self.cfg["consolidate_min_support"]:
                continue
            avg_w = sum(x.weight * max(1, x.observation_count) for x in cl) / support
            if avg_w < self.cfg["consolidate_min_weight"]:
                continue
            action = cl[0].action
            if not action:
                continue
            avg_val = sum(x.valence * max(1, x.observation_count) for x in cl) / support
            avg_ar = sum(x.arousal * max(1, x.observation_count) for x in cl) / support
            context = cl[0].situation
            rule = f"当「{context}」→ 倾向行动 {action}（平均结果 {avg_val:+.2f}）"

            # 以结构相似性而不是带浮点数的 rule 字符串判重。
            existing = next((sm for sm in self.semantic.values()
                             if sm.action == action and sm.scope == cl[0].scope
                             and self._memory_similarity(sm, cl[0])
                             >= self.cfg["consolidate_sim"]), None)
            if existing is not None:
                old_support = max(1, existing.support_count)
                total = old_support + support
                existing.valence = (existing.valence * old_support + avg_val * support) / total
                existing.arousal = (existing.arousal * old_support + avg_ar * support) / total
                existing.support_count = total
                existing.confidence = min(0.99, total / (total + 2.0))
                existing.weight = min(1.0, existing.weight + 0.03 * math.log1p(support))
                existing.last_active = self.time
                existing.tags = sorted(set(existing.tags).union(*(set(x.tags) for x in cl)))
                existing.source_episodes = sorted(
                    set(existing.source_episodes) | {x.id for x in cl})
                existing.rule = (f"当「{existing.context}」→ 倾向行动 {action}"
                                 f"（平均结果 {existing.valence:+.2f}）")
                made += 1
            else:
                sm = SemanticMemory(
                    id=self.next_id, rule=rule, action=action, context=context,
                    confidence=min(0.99, support / (support + 2.0)),
                    support_count=support,
                    weight=min(1.0, 0.48 + 0.08 * math.log1p(support)),
                    valence=avg_val, arousal=avg_ar,
                    importance=min(10, 5 + int(math.log2(max(1, support)))),
                    tags=sorted(set().union(*(set(x.tags) for x in cl))),
                    features=list(cl[0].features) if cl[0].features else None,
                    source_episodes=[x.id for x in cl],
                    created=self.time, last_active=self.time, last_access=self.time,
                    scope=cl[0].scope,
                )
                self.semantic[sm.id] = sm
                self.next_id += 1
                self.stats["consolidated"] += 1
                made += 1

            # 贡献的经历降权、标记已巩固（退居二线）
            for x in cl:
                x.consolidated = True
                x.weight *= 0.45
        return made

    # ------------------------------------------------------------------
    # 剪枝：自适应精简
    # ------------------------------------------------------------------

    def prune(self, weak_threshold: Optional[float] = None) -> int:
        """将弱或超容量的情景移入冷归档，并清理弱联想。"""
        th = weak_threshold if weak_threshold is not None else self.cfg["min_weight"]
        pruned = 0
        candidates = []
        for mid, m in self.episodic.items():
            if m.pinned:
                continue
            expired = m.valid_until is not None and self.time >= m.valid_until
            if expired or m.superseded_by is not None or m.weight < th:
                candidates.append((float("-inf"), mid))
                continue
            age = max(0.0, self.time - m.last_active)
            confidence = max(0.1, float(getattr(m, "confidence", 0.5)))
            keep = (m.weight * (0.6 + 0.04 * m.importance)
                    * (0.8 + 0.2 * confidence)
                    * (1.0 + 0.20 * max(-0.5, m.utility))
                    * math.exp(-0.002 * age))
            if m.consolidated:
                keep *= 0.55
            candidates.append((keep, mid))

        forced = {mid for score, mid in candidates if score == float("-inf")}
        excess = max(0, len(self.episodic) - len(forced) - self.cfg["max_episodic"])
        ranked = [mid for score, mid in sorted(candidates) if mid not in forced]
        to_archive = forced | set(ranked[:excess])
        for mid in sorted(to_archive):
            if self._archive_memory(mid):
                pruned += 1

        # 语义层只在超硬预算时清理最弱的派生规则。
        semantic_excess = max(0, len(self.semantic) - self.cfg["max_semantic"])
        removable = sorted(
            (m.weight * m.confidence * math.log1p(m.support_count), mid)
            for mid, m in self.semantic.items() if not m.pinned
        )
        for _, mid in removable[:semantic_excess]:
            del self.semantic[mid]
            self._remove_memory_associations(mid)
            self.stats["semantic_pruned"] += 1
            pruned += 1

        alive = set(self.episodic) | set(self.semantic)
        # 清理关联到冷归档/已删记忆、或强度过弱的联想
        for key in list(self.assoc.keys()):
            a = self.assoc[key]
            if (a.a not in alive or a.b not in alive
                    or a.strength < self.cfg["assoc_prune"]):
                self._remove_assoc(key)

        # 冷归档有自己的预算，超出后淘汰最旧且效用最低的条目。
        archive_excess = max(0, len(self.archive) - self.cfg["max_archive"])
        if archive_excess:
            oldest = sorted(
                self.archive.items(),
                key=lambda item: (item[1].pinned,
                                  item[1].archived_at or item[1].last_active,
                                  item[1].utility),
            )
            for mid, _ in oldest[:archive_excess]:
                del self.archive[mid]
                self.stats["forgotten"] += 1
                pruned += 1

        self.stats["pruned"] += pruned
        return pruned

    def _archive_memory(self, mid: int) -> bool:
        m = self.episodic.pop(mid, None)
        if m is None or m.pinned:
            return False
        m.archived_at = self.time
        self.archive[mid] = m
        self._remove_memory_associations(mid)
        self.stats["archived"] += 1
        return True

    def restore(self, mid: int) -> bool:
        """将一条冷归档情景恢复到活跃检索池。"""
        m = self.archive.pop(mid, None)
        if m is None:
            return False
        m.archived_at = None
        m.weight = max(self.cfg["min_weight"] * 1.5, m.weight)
        m.last_active = self.time
        self.episodic[mid] = m
        self._link_similar(m)
        self.stats["restored"] += 1
        self.prune()
        return mid in self.episodic

    def supersede(self, old_id: int, new_id: int) -> bool:
        """保留旧事实证据，但从活跃检索中排除它。"""
        old = self._get(old_id)
        new = self._get(new_id)
        if old is None or new is None or old_id == new_id:
            return False
        old.superseded_by = new_id
        old.last_active = self.time
        return True

    # ------------------------------------------------------------------
    # 联想网络
    # ------------------------------------------------------------------

    def _link_similar(self, m, threshold: Optional[float] = None) -> None:
        th = threshold if threshold is not None else self.cfg["link_threshold"]
        matches = []
        for other in list(self.episodic.values()) + list(self.semantic.values()):
            if other.id == m.id:
                continue
            if getattr(other, "superseded_by", None) is not None:
                continue
            m_scope = getattr(m, "scope", None)
            o_scope = getattr(other, "scope", None)
            if m_scope and o_scope and m_scope != o_scope:
                continue
            sim = self._memory_similarity(m, other)
            if sim >= th:
                matches.append((sim, other.id))
        for sim, other_id in sorted(matches, reverse=True)[: self.cfg["link_top_n"]]:
            self._touch_assoc(m.id, other_id, sim)

    def _touch_assoc(self, a: int, b: int, strength: float) -> None:
        key = (a, b) if a < b else (b, a)
        assoc = self.assoc.get(key)
        if assoc is None:
            self.assoc[key] = Association(a=key[0], b=key[1],
                                          strength=min(1.0, strength),
                                          last_update=self.time)
            self._assoc_index[key[0]].add(key)
            self._assoc_index[key[1]].add(key)
        else:
            assoc.strength = min(1.0, assoc.strength + self.cfg["assoc_boost"] * strength)
            assoc.last_update = self.time
        self._enforce_degree(a)
        self._enforce_degree(b)

    def _coactivate(self, seeds: List) -> None:
        """一起被回忆的记忆之间，联想增强（Hebbian 式共现强化）。"""
        ids = [m.id for m in seeds]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = self._get(ids[i]), self._get(ids[j])
                if (a is not None and b is not None
                        and self._memory_similarity(a, b) >= self.cfg["coactivate_threshold"]):
                    self._touch_assoc(ids[i], ids[j], self.cfg["assoc_boost"])

    def _neighbors(self, m) -> List[Tuple[int, Association]]:
        out = []
        for key in self._assoc_index.get(m.id, ()):
            a = self.assoc.get(key)
            if a is None:
                continue
            if key[0] == m.id:
                out.append((key[1], a))
            elif key[1] == m.id:
                out.append((key[0], a))
        return out

    def _remove_assoc(self, key: Tuple[int, int]) -> None:
        assoc = self.assoc.pop(key, None)
        if assoc is None:
            return
        self._assoc_index[assoc.a].discard(key)
        self._assoc_index[assoc.b].discard(key)
        self.stats["assoc_pruned"] += 1

    def _remove_memory_associations(self, mid: int) -> None:
        for key in list(self._assoc_index.get(mid, ())):
            self._remove_assoc(key)
        self._assoc_index.pop(mid, None)

    def _enforce_degree(self, mid: int) -> None:
        keys = list(self._assoc_index.get(mid, ()))
        excess = len(keys) - self.cfg["max_assoc_degree"]
        if excess <= 0:
            return
        weakest = sorted(keys, key=lambda key: self.assoc[key].strength)
        for key in weakest[:excess]:
            self._remove_assoc(key)

    def _rebuild_assoc_index(self) -> None:
        self._assoc_index = defaultdict(set)
        for key, assoc in self.assoc.items():
            self._assoc_index[assoc.a].add(key)
            self._assoc_index[assoc.b].add(key)

    def _spread(self, seeds: List, steps: Optional[int] = None,
                decay: Optional[float] = None) -> List[Tuple[float, object]]:
        """联想扩散（spreading activation）：从种子记忆沿联想扩散激活。"""
        steps = _bounded_int(steps, self.cfg["spread_steps"], 0, 16)
        decay = _bounded_float(decay, self.cfg["spread_decay"], 0.0, 1.0)
        activation: Dict[int, float] = {}
        for s in seeds:
            activation[s.id] = activation.get(s.id, 0.0) + 1.0
        frontier = [(s.id, 1.0) for s in seeds]
        seen = set(activation.keys())
        for _ in range(steps):
            nxt = []
            for mid, act in frontier:
                m = self._get(mid)
                if m is None:
                    continue
                for other_id, a in self._neighbors(m):
                    if other_id in seen:
                        continue
                    gained = act * a.strength * decay
                    if gained > 0.01:
                        activation[other_id] = activation.get(other_id, 0.0) + gained
                        seen.add(other_id)
                        nxt.append((other_id, gained))
            frontier = nxt
        out = []
        for mid, act in activation.items():
            m = self._get(mid)
            if m is not None:
                out.append((act, m))
        return out

    # ------------------------------------------------------------------
    # 相似度
    # ------------------------------------------------------------------

    def _memory_similarity(self, a, b) -> float:
        """两条记忆之间的相似度（优先用特征向量，其次文本）。"""
        if a.features and b.features:
            return gaussian_sim(a.features, b.features, self.cfg["feature_sigma"])
        return token_sim(a.situation if hasattr(a, "situation") else a.context,
                         b.situation if hasattr(b, "situation") else b.context)

    def _similarity(self, m, query: Optional[str], features: Optional[List[float]]) -> float:
        """记忆与查询之间的相似度（topic 维度）。"""
        if features is not None and m.features is not None:
            return gaussian_sim(features, m.features, self.cfg["feature_sigma"])
        if query is not None:
            text = m.situation if hasattr(m, "situation") else m.context
            return token_sim(query, text)
        return 0.0

    def _most_similar(self, situation: str, features: Optional[List[float]],
                      store: str = "episodic", action: Optional[str] = None,
                      scope: Optional[str] = None) -> Tuple[float, object]:
        best, best_sim = None, 0.0
        pool = self.episodic if store == "episodic" else self.semantic
        for m in pool.values():
            if action is not None and getattr(m, "action", "") != action:
                continue
            if scope is not None and getattr(m, "scope", "agent") != scope:
                continue
            # 已被替代或已过期的记忆不参与去重合并：它们不会进入召回，
            # 把新证据合并进去等于静默丢弃这条经历。
            if getattr(m, "superseded_by", None) is not None:
                continue
            valid_until = getattr(m, "valid_until", None)
            if valid_until is not None and self.time >= valid_until:
                continue
            if features is not None and m.features is not None:
                sim = gaussian_sim(features, m.features, self.cfg["feature_sigma"])
            else:
                sim = token_sim(situation, m.situation if hasattr(m, "situation") else m.context)
            if sim > best_sim:
                best_sim, best = sim, m
        return best_sim, best

    # ------------------------------------------------------------------
    # 统计 & 持久化
    # ------------------------------------------------------------------

    def stats_summary(self) -> Dict:
        recent_return = (sum(self._recent_outcomes) / len(self._recent_outcomes)
                         if self._recent_outcomes else 0.0)
        event_total = sum(self._outcome_events.values())
        return {
            "time": self.time,
            "episodic": len(self.episodic),
            "semantic": len(self.semantic),
            "archived": len(self.archive),
            "associations": len(self.assoc),
            "pruned": self.stats["pruned"],
            "archived_total": self.stats["archived"],
            "restored": self.stats["restored"],
            "forgotten": self.stats["forgotten"],
            "assoc_pruned": self.stats["assoc_pruned"],
            "semantic_pruned": self.stats["semantic_pruned"],
            "consolidated": self.stats["consolidated"],
            "dreams": self.stats["dreams"],
            "stored": self.stats["stored"],
            "merged": self.stats["merged"],
            "reinforced": self.stats["reinforced"],
            "accuracy": self.accuracy,
            "recent_return": recent_return,
            "total_return": self._total_return,
            "average_return": self._total_return / event_total if event_total else 0.0,
            "food": self._outcome_events.get("food", 0),
            "poison": self._outcome_events.get("poison", 0),
            "wall": self._outcome_events.get("wall", 0),
            "moves": self._outcome_events.get("move", 0),
            "total_weight": sum(m.weight for m in self.all_memories()),
        }

    def to_dict(self) -> Dict:
        """Return a JSON-safe snapshot for Mind and external persistence."""
        def snapshot(memory):
            data = memory_to_dict(memory)
            data["metadata"] = _normalise_metadata(data.get("metadata"))
            data["tags"] = _normalise_tags(data.get("tags"))
            data["features"] = _normalise_features(data.get("features"))
            return data

        return {
            "version": 3,
            "time": self.time,
            "next_id": self.next_id,
            "cfg": dict(self.cfg),
            "stats": dict(self.stats),
            "episodic": [snapshot(m) for m in self.episodic.values()],
            "semantic": [snapshot(m) for m in self.semantic.values()],
            "archive": [snapshot(m) for m in self.archive.values()],
            "assoc": [{
                "a": key[0],
                "b": key[1],
                "strength": _bounded_float(edge.strength, 0.0, 0.0, 1.0),
                "last_update": _bounded_float(edge.last_update, 0.0, 0.0),
            } for key, edge in self.assoc.items()],
            "dreams": list(self.dreams_history),
            "outcomes": list(self._recent_outcomes),
            "outcome_events": list(self._recent_events),
            "event_counts": dict(self._outcome_events),
            "total_return": self._total_return,
        }

    def save(self, path: str) -> None:
        _atomic_json_write(path, self.to_dict())

    def _load_record(self, raw, cls):
        """Build one valid dataclass record from untrusted persisted JSON."""
        if not isinstance(raw, dict):
            return None
        mid = _bounded_int(raw.get("id"), 0, 1)
        if mid < 1:
            return None
        data = dict(raw)
        data["id"] = mid
        if cls is EpisodicMemory:
            data["situation"] = _short_text(data.get("situation"))
        else:
            data["rule"] = _short_text(data.get("rule"))
        try:
            memory = memory_from_dict(data, cls)
        except (TypeError, ValueError):
            return None

        memory.id = mid
        for name, default, lower, upper in (
            ("weight", 0.5, 0.0, 1.0),
            ("valence", 0.0, -1.0, 1.0),
            ("arousal", 0.3, 0.0, 1.0),
            ("confidence", 0.5, 0.0, 1.0),
            ("utility", 0.0, -1.0, 1.0),
            ("created", 0.0, 0.0, None),
            ("last_active", 0.0, 0.0, None),
            ("last_access", 0.0, 0.0, None),
        ):
            if hasattr(memory, name):
                setattr(memory, name, _bounded_float(
                    getattr(memory, name, default), default, lower, upper))
        for name, default in (
            ("importance", 5),
            ("access_count", 0),
            ("touch_count", 0),
            ("success_count", 0),
            ("failure_count", 0),
        ):
            if hasattr(memory, name):
                lower, upper = ((1, 10) if name == "importance" else (0, 1_000_000_000))
                setattr(memory, name, _bounded_int(
                    getattr(memory, name, default), default, lower, upper))

        memory.tags = _normalise_tags(getattr(memory, "tags", []))
        memory.features = _normalise_features(getattr(memory, "features", None))
        memory.source = _short_text(getattr(memory, "source", "experience"), 128) or "experience"
        memory.scope = _short_text(getattr(memory, "scope", "agent"), 128) or "agent"
        memory.pinned = _as_bool(getattr(memory, "pinned", False))
        memory.valid_until = _optional_time(getattr(memory, "valid_until", None))
        memory.superseded_by = _bounded_int(
            getattr(memory, "superseded_by", None), 0, 0) or None
        memory.metadata = _normalise_metadata(getattr(memory, "metadata", {}))

        if isinstance(memory, EpisodicMemory):
            memory.situation = _short_text(memory.situation)
            memory.action = _short_text(memory.action, 512)
            memory.outcome = _short_text(memory.outcome)
            memory.lesson = _short_text(memory.lesson, 4_000)
            memory.observation_count = _bounded_int(
                memory.observation_count, 1, 1, 1_000_000_000)
            memory.archived_at = _optional_time(memory.archived_at)
            memory.consolidated = _as_bool(memory.consolidated)
            memory.digested = _as_bool(memory.digested)
        else:
            memory.rule = _short_text(memory.rule)
            memory.action = _short_text(memory.action, 512)
            memory.context = _short_text(memory.context)
            source_ids = getattr(memory, "source_episodes", [])
            if not isinstance(source_ids, list):
                source_ids = []
            memory.source_episodes = sorted({
                _bounded_int(value, 0, 1)
                for value in source_ids
                if _bounded_int(value, 0, 1) > 0
            })[:1_024]
            memory.support_count = _bounded_int(
                memory.support_count, 1, 1, 1_000_000_000)
        return memory

    def _load_collection(self, values, cls, known_ids: set) -> Dict[int, object]:
        if not isinstance(values, list):
            return {}
        collection = {}
        for raw in values[:100_000]:
            memory = self._load_record(raw, cls)
            if memory is None or memory.id in known_ids:
                continue
            known_ids.add(memory.id)
            collection[memory.id] = memory
        return collection

    def _apply_loaded_data(self, data: Dict) -> None:
        """Apply a validated data object to this fresh Hippocampus instance."""
        if not isinstance(data, dict):
            raise ValueError("记忆文件根节点必须是 JSON 对象")
        loaded_cfg = data.get("cfg", {})
        loaded_cfg = dict(loaded_cfg) if isinstance(loaded_cfg, dict) else {}
        if _bounded_int(data.get("version", 1), 1, 1) < 3:
            # v2 的召回升权和钳位阈值会造成热门记忆垄断、且永不剪枝。
            for key in ("recall_boost", "recall_boost_semantic", "min_weight",
                        "recency_scale"):
                loaded_cfg.pop(key, None)
        self.cfg = self._sanitize_cfg({**self.cfg, **loaded_cfg})
        self.time = _bounded_float(data.get("time"), 0.0, 0.0)
        self.next_id = _bounded_int(data.get("next_id"), 1, 1)
        raw_stats = data.get("stats", {})
        raw_stats = raw_stats if isinstance(raw_stats, dict) else {}
        self.stats = {
            key: _bounded_int(raw_stats.get(key), 0, 0, 1_000_000_000)
            for key in self.stats
        }
        known_ids = set()
        self.episodic = self._load_collection(
            data.get("episodic", []), EpisodicMemory, known_ids)
        self.semantic = self._load_collection(
            data.get("semantic", []), SemanticMemory, known_ids)
        self.archive = self._load_collection(
            data.get("archive", []), EpisodicMemory, known_ids)

        self.assoc = {}
        alive = set(self.episodic) | set(self.semantic)
        associations = data.get("assoc", [])
        if not isinstance(associations, list):
            associations = []
        for raw in associations[:200_000]:
            if not isinstance(raw, dict):
                continue
            a = _bounded_int(raw.get("a"), 0, 1)
            b = _bounded_int(raw.get("b"), 0, 1)
            if a == b or a not in alive or b not in alive:
                continue
            key = (a, b) if a < b else (b, a)
            candidate = Association(
                a=key[0], b=key[1],
                strength=_bounded_float(raw.get("strength"), 0.0, 0.0, 1.0),
                last_update=_bounded_float(raw.get("last_update"), 0.0, 0.0),
            )
            old = self.assoc.get(key)
            if old is None or candidate.strength > old.strength:
                self.assoc[key] = candidate
        self._rebuild_assoc_index()

        raw_dreams = data.get("dreams", [])
        self.dreams_history = deque(
            [item for item in raw_dreams if isinstance(item, dict)][-20:]
            if isinstance(raw_dreams, list) else [], maxlen=20)
        outcomes = data.get("outcomes", [])
        self._recent_outcomes = deque(
            [_bounded_float(value, 0.0, -1_000_000.0, 1_000_000.0)
             for value in outcomes[-300:]] if isinstance(outcomes, list) else [],
            maxlen=300)
        events = data.get("outcome_events", [])
        self._recent_events = deque(
            [_short_text(value, 64) or "move" for value in events[-300:]]
            if isinstance(events, list) else [], maxlen=300)
        raw_counts = data.get("event_counts", {})
        self._outcome_events = Counter()
        if isinstance(raw_counts, dict):
            for event, count in raw_counts.items():
                name = _short_text(event, 64)
                if name:
                    self._outcome_events[name] = _bounded_int(
                        count, 0, 0, 1_000_000_000)
        if not self._outcome_events:
            self._outcome_events.update(self._recent_events)
        self._total_return = _bounded_float(
            data.get("total_return"), sum(self._recent_outcomes),
            -1_000_000_000_000.0, 1_000_000_000_000.0)

        all_ids = set(self.episodic) | set(self.semantic) | set(self.archive)
        for memory in (list(self.episodic.values()) + list(self.semantic.values())
                       + list(self.archive.values())):
            if memory.superseded_by not in all_ids or memory.superseded_by == memory.id:
                memory.superseded_by = None
        if all_ids:
            self.next_id = max(self.next_id, max(all_ids) + 1)
        for mid in list(alive):
            self._enforce_degree(mid)
        # Imported files may predate the active-pool budgets. Archive overflow
        # rather than letting a one-off import make normal recall unbounded.
        self.prune()

    def load_dict(self, data: Dict) -> None:
        """Load JSON data transactionally: malformed data leaves this instance intact."""
        if not isinstance(data, dict):
            raise ValueError("记忆文件根节点必须是 JSON 对象")
        candidate = Hippocampus(**self.cfg)
        candidate.rng = self.rng
        candidate.learning_enabled = self.learning_enabled
        candidate._apply_loaded_data(data)
        self.__dict__.update(candidate.__dict__)

    def load(self, path: str) -> None:
        self.load_dict(_read_json(path))

    def clear(self) -> None:
        self.episodic.clear()
        self.semantic.clear()
        self.archive.clear()
        self.assoc.clear()
        self._assoc_index.clear()
        self.dreams_history.clear()
        self.next_id = 1
        self.time = 0.0
        self.stats = {k: 0 for k in self.stats}
        self._recent_outcomes.clear()
        self._recent_events.clear()
        self._outcome_events.clear()
        self._total_return = 0.0
        self._consolidation_timer = 0.0
        self._dream_timer = 0.0
        self._maintenance_timer = 0.0
