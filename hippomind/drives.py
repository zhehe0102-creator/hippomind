"""DriveEngine —— 心潮式动态驱力引擎（Dynamic Drives）。

概念来自"心潮"：Agent 不是每次决策都从零开始，而是有随时间变化的
内在状态（想念、期待、好奇、安全欲……）。这里实现通用化的驱力引擎：

  * 时间地板（Time Floor）：驱力随时间自动增长，顶到"静息天花板"(ceil) 就停
  * 静息松弛：被事件顶到天花板之上后，缓慢松弛回各自 ceil（各维独立）
  * 互动消解（Satisfy）：交互/行动按 satisfyMul 消解对应驱力
  * 记忆共振（Resonance）：回忆到的记忆按 domain→亲和度 回推驱力
    （多 domain 取最大不累加、单次总量封顶、nudge 很小——有界闭环）
  * 驱力偏置（Drive Bias）：强驱力让相关记忆更容易被回忆（供 Mind.recall 用）

默认 5 维（通用，不含心潮的恋爱专属维度，可自行扩展）：
  hunger 想觅食/达成目标 · safety 想安全 · curiosity 想探索
  social 想交流 · duty 责任感
"""

from __future__ import annotations

from .textutil import coerce_random_seed

import math
import random
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_DRIVES = ["hunger", "safety", "curiosity", "social", "duty"]

DRIVE_LABELS = {
    "hunger": "觅食·目标",
    "safety": "安全",
    "curiosity": "探索",
    "social": "交流",
    "duty": "责任",
}

# 每维驱力：增长速率(每小时)、静息天花板、互动消解系数
DEFAULT_GROW = {"hunger": 0.25, "safety": 0.12, "curiosity": 0.08,
                "social": 0.05, "duty": 0.04}
DEFAULT_CEIL = {"hunger": 0.85, "safety": 0.70, "curiosity": 0.55,
                "social": 0.45, "duty": 0.40}
DEFAULT_SATISFY = {"hunger": 0.50, "safety": 0.45, "curiosity": 0.35,
                   "social": 0.40, "duty": 0.50}

# 记忆共振表：记忆 domain/tag → 驱力亲和度（只回推 ≥ 门槛的维度）
DEFAULT_AFFINITY = {
    "food":    {"hunger": 0.70, "curiosity": 0.30},
    "poison":  {"safety": 0.80},
    "wall":    {"safety": 0.50, "curiosity": 0.20},
    "empty":   {"curiosity": 0.40},
    "danger":  {"safety": 0.70},
    "social":  {"social": 0.60},
    "task":    {"duty": 0.60},
    "learn":   {"curiosity": 0.60, "duty": 0.20},
}

# 互动效果：relief 消解驱力，increase 增加驱力
DEFAULT_INTERACTIONS = {
    "eat":      {"relief": {"hunger": 0.55}, "increase": {}},
    "avoid":    {"relief": {"safety": 0.45}, "increase": {}},
    "explore":  {"relief": {"curiosity": 0.45}, "increase": {}},
    "socialize": {"relief": {"social": 0.45}, "increase": {}},
    "progress": {"relief": {"duty": 0.50}, "increase": {}},
    "danger":   {"relief": {}, "increase": {"safety": 0.06, "curiosity": 0.02}},
    "curiosity_burst": {"relief": {}, "increase": {"curiosity": 0.08}},
}


def _finite_float(value, default: float = 0.0, lower: float = 0.0,
                  upper: float = 1.0) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(lower, min(upper, number))


def _drive_names(value) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return list(DEFAULT_DRIVES)
    names = []
    for item in value:
        name = str(item).strip()[:48]
        if name and name not in names:
            names.append(name)
    return names[:32] or list(DEFAULT_DRIVES)


class DriveEngine:
    def __init__(self, seed: Optional[int] = None, **cfg):
        self.rng = random.Random(coerce_random_seed(seed))
        self.cfg = {
            "drives": list(DEFAULT_DRIVES),
            "grow": dict(DEFAULT_GROW),
            "ceil": dict(DEFAULT_CEIL),
            "satisfy": dict(DEFAULT_SATISFY),
            "affinity": {k: dict(v) for k, v in DEFAULT_AFFINITY.items()},
            "interactions": {k: {"relief": dict(v.get("relief", {})),
                                 "increase": dict(v.get("increase", {}))}
                             for k, v in DEFAULT_INTERACTIONS.items()},
            "relax_per_hour": 0.10,      # 超顶后的松弛速率
            "time_scale": 60.0,          # 1 秒内部时间 = 1 分钟驱力时间
            "resonance_min": 0.5,        # 共振亲和度门槛
            "resonance_nudge": 0.02,     # 单次共振回推量
            "resonance_cap": 0.06,       # 单次共振总封顶
            "bias_factor": 0.5,          # 驱力偏置强度（乘到检索分上）
        }
        self.cfg.update(cfg)
        self._normalise_config()
        self.values: Dict[str, float] = {d: 0.0 for d in self.drives}
        self.history: Dict[str, list] = {d: [] for d in self.drives}  # 供可视化
        self.elapsed = 0.0  # 驱力时钟累计秒数（history 时间戳用）

    def _normalise_config(self) -> None:
        """Keep imported/custom drive state finite and structurally bounded."""
        self.drives = _drive_names(self.cfg.get("drives"))
        self.cfg["drives"] = list(self.drives)
        for key, defaults in (
            ("grow", DEFAULT_GROW),
            ("ceil", DEFAULT_CEIL),
            ("satisfy", DEFAULT_SATISFY),
        ):
            raw = self.cfg.get(key, {})
            raw = raw if isinstance(raw, dict) else {}
            self.cfg[key] = {
                drive: _finite_float(raw.get(drive, defaults.get(drive, 0.0)),
                                     defaults.get(drive, 0.0), 0.0, 10.0)
                for drive in self.drives
            }
        self.cfg["relax_per_hour"] = _finite_float(
            self.cfg.get("relax_per_hour"), 0.10, 0.0, 10.0)
        self.cfg["time_scale"] = _finite_float(
            self.cfg.get("time_scale"), 60.0, 1e-6, 1_000_000.0)
        self.cfg["resonance_min"] = _finite_float(
            self.cfg.get("resonance_min"), 0.5, 0.0, 1.0)
        self.cfg["resonance_nudge"] = _finite_float(
            self.cfg.get("resonance_nudge"), 0.02, 0.0, 1.0)
        self.cfg["resonance_cap"] = _finite_float(
            self.cfg.get("resonance_cap"), 0.06, 0.0, 1.0)
        self.cfg["bias_factor"] = _finite_float(
            self.cfg.get("bias_factor"), 0.5, 0.0, 10.0)

        raw_affinity = self.cfg.get("affinity", {})
        raw_affinity = raw_affinity if isinstance(raw_affinity, dict) else {}
        affinity = {}
        for tag, values in list(raw_affinity.items())[:256]:
            if not isinstance(values, dict):
                continue
            clean = {
                drive: _finite_float(value, 0.0, 0.0, 1.0)
                for drive, value in values.items()
                if str(drive) in self.drives
            }
            if clean:
                affinity[str(tag).strip()[:96]] = clean
        self.cfg["affinity"] = affinity

        raw_interactions = self.cfg.get("interactions", {})
        raw_interactions = raw_interactions if isinstance(raw_interactions, dict) else {}
        interactions = {}
        for kind, effects in list(raw_interactions.items())[:128]:
            if not isinstance(effects, dict):
                continue
            clean_effects = {}
            for effect in ("relief", "increase"):
                values = effects.get(effect, {})
                if not isinstance(values, dict):
                    continue
                clean = {
                    drive: _finite_float(value, 0.0, 0.0, 1.0)
                    for drive, value in values.items()
                    if str(drive) in self.drives
                }
                if clean:
                    clean_effects[effect] = clean
            if clean_effects:
                interactions[str(kind).strip()[:96]] = clean_effects
        self.cfg["interactions"] = interactions

    # ------------------------------------------------------------------

    def tick(self, dt_seconds: float) -> None:
        """推进驱力：时间地板增长（托到静息天花板就停）+ 超顶松弛。dt 为内部秒数。"""
        try:
            dt_seconds = float(dt_seconds)
        except (TypeError, ValueError):
            raise ValueError("dt_seconds 必须是非负有限数")
        if not math.isfinite(dt_seconds) or dt_seconds < 0:
            raise ValueError("dt_seconds 必须是非负有限数")
        dt_h = dt_seconds / max(1.0, self.cfg["time_scale"])
        self.elapsed += dt_seconds
        for d in self.drives:
            v = self.values[d]
            ceil = self.cfg["ceil"].get(d, 0.5)
            if v < ceil:
                # 时间地板：缺什么就慢慢涨，托到静息天花板就停
                v = min(ceil, v + self.cfg["grow"].get(d, 0.0) * dt_h)
            elif v > ceil:
                # 被事件/共振顶过天花板后，缓慢松弛回各自静息天花板
                v = max(ceil, v - self.cfg["relax_per_hour"] * dt_h)
            self.values[d] = max(0.0, min(1.0, v))
            # 记录真实时间戳而非恒 0，可视化与回放才能对齐驱力曲线。
            self.history[d].append((round(self.elapsed, 3), self.values[d]))
            if len(self.history[d]) > 400:
                self.history[d].pop(0)

    def interact(self, kind: str, strength: float = 1.0) -> List[str]:
        """一次互动/行动：按效果消解或增加对应驱力。返回受影响的驱力。"""
        fx = self.cfg["interactions"].get(kind)
        if not fx:
            return []
        strength = _finite_float(strength, 1.0, 0.0, 10.0)
        affected = []
        for d, amt in fx.get("relief", {}).items():
            if d in self.values:
                self.values[d] = max(0.0, self.values[d] - amt * strength)
                affected.append(d)
        for d, amt in fx.get("increase", {}).items():
            if d in self.values:
                self.values[d] = min(1.0, self.values[d] + amt * strength)
                affected.append(d)
        return affected

    def resonance(self, domains: List[str]) -> Dict[str, float]:
        """记忆共振：浮现记忆的 domain → 回推驱力。有界（取最大、不累加、封顶）。"""
        if not domains:
            return {}
        affinity: Dict[str, float] = {}
        for raw in domains:
            raw = str(raw).strip()
            mp = self.cfg["affinity"].get(raw)
            if not mp:
                continue
            for d, aff in mp.items():
                if d in affinity:
                    affinity[d] = max(affinity[d], aff)
                else:
                    affinity[d] = aff
        applied: Dict[str, float] = {}
        for d, aff in affinity.items():
            aff = _finite_float(aff, 0.0, 0.0, 1.0)
            if aff < self.cfg["resonance_min"]:
                continue
            delta = min(self.cfg["resonance_nudge"] * aff, self.cfg["resonance_cap"])
            before = self.values[d]
            ceil = self.cfg["ceil"].get(d, 1.0)
            self.values[d] = max(before, min(ceil, max(0.0, before + delta)))
            if abs(self.values[d] - before) > 1e-6:
                applied[d] = self.values[d] - before
        return applied

    def bias_for(self, domains: List[str]) -> float:
        """驱力偏置：当前驱力对某个记忆（按其 domain）的乘法系数增量。"""
        if not domains:
            return 0.0
        total = 0.0
        for raw in domains:
            mp = self.cfg["affinity"].get(str(raw).strip())
            if not mp:
                continue
            for d, aff in mp.items():
                total = max(total, self.values.get(d, 0.0) * aff)  # 取主导驱力
        return self.cfg["bias_factor"] * total

    def hottest(self) -> str:
        """当前最强的驱力名。"""
        if not self.values:
            return ""
        return max(self.values, key=self.values.get)

    def summary(self) -> Dict:
        return dict(self.values)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict:
        return {
            "cfg": {k: v for k, v in self.cfg.items() if k not in ("grow", "ceil", "satisfy",
                                                                   "affinity", "interactions")},
            "values": self.values,
            "grow": self.cfg["grow"],
            "ceil": self.cfg["ceil"],
            "satisfy": self.cfg["satisfy"],
            "affinity": self.cfg["affinity"],
            "interactions": self.cfg["interactions"],
        }

    def from_dict(self, data: Dict) -> None:
        """Load persisted drives without accepting extra names or NaN values."""
        if not isinstance(data, dict):
            return
        saved_cfg = data.get("cfg", {})
        if isinstance(saved_cfg, dict):
            for key in ("drives", "relax_per_hour", "time_scale", "resonance_min",
                        "resonance_nudge", "resonance_cap", "bias_factor"):
                if key in saved_cfg:
                    self.cfg[key] = saved_cfg[key]
        for key in ("grow", "ceil", "satisfy", "affinity", "interactions"):
            if key in data:
                self.cfg[key] = data[key]
        self._normalise_config()

        raw_values = data.get("values", {})
        raw_values = raw_values if isinstance(raw_values, dict) else {}
        self.values = {
            drive: _finite_float(raw_values.get(drive, 0.0), 0.0, 0.0, 1.0)
            for drive in self.drives
        }
        self.elapsed = 0.0
        self.history = {drive: [] for drive in self.drives}
