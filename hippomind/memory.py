"""记忆数据结构：情景记忆、语义记忆、联想连接。

类比生物海马体 + 心潮念概念：
  * EpisodicMemory  —— 刚发生的经历，权重（salience）会随使用升权、随遗忘降权
  * SemanticMemory  —— 从重复经历中巩固出来的稳定知识（皮层化）
  * Association     —— 记忆之间的联想连接，越常一起被回忆越强
  * 每条记忆带 情感坐标(valence/arousal, Russell 环形)、重要性分级(1-10)、
    触碰次数(touch_count) 与 激活时间(last_active) —— 对应 Ombre Brain 的四维检索
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional


@dataclass
class EpisodicMemory:
    """一条情景记忆：在什么情境（situation）下做了什么（action）、结果如何（outcome/valence）。"""

    id: int
    situation: str                              # 情境的文字描述
    action: str = ""                            # 采取的行动
    outcome: str = ""                           # 结果描述
    valence: float = 0.0                        # 结果好坏 -1..1（奖励信号）
    arousal: float = 0.3                        # 情感唤醒度 0..1（Russell 环形）
    importance: int = 5                         # 重要性分级 1-10（OB 风格）
    lesson: str = ""                            # 可选的教训总结
    tags: List[str] = field(default_factory=list)  # 标签/域（用于共振与检索）
    features: Optional[List[float]] = None      # 结构化特征向量（用于相似度计算）
    created: float = 0.0                        # 创建时间
    last_active: float = 0.0                    # 最近写入/修改时间（回忆不更新它！）
    last_access: float = 0.0                    # 最近一次被回忆的时间
    access_count: int = 0                       # 被回忆次数（语义层面）
    touch_count: int = 0                        # 触碰频率（四维检索的 touch 维度）
    weight: float = 0.5                         # 记忆强度/显著性（自动升权/降权）
    consolidated: bool = False                  # 是否已被巩固成语义记忆
    observation_count: int = 1                  # 同一情境-动作被观察的次数
    utility: float = 0.0                        # 被用于行动后的效用 EMA，-1..1
    success_count: int = 0                      # 对行动有正贡献的次数
    failure_count: int = 0                      # 对行动有负贡献的次数
    confidence: float = 0.5                     # 经验可靠度，随重复证据增长
    digested: bool = False                      # 是否已被梦境/后台反思处理
    source: str = "experience"                  # 来源，便于审计与冲突处理
    scope: str = "agent"                        # agent/user/session/team 等隔离域
    pinned: bool = False                        # 关键记忆不自动归档
    valid_until: Optional[float] = None         # 可选失效时间（内部时钟）
    superseded_by: Optional[int] = None         # 新事实替代旧事实时保留谱系
    archived_at: Optional[float] = None         # 进入冷归档的时间
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SemanticMemory:
    """巩固后的语义知识：从重复情景中提炼的稳定规则。"""

    id: int
    rule: str                                   # 规则内容，如 "当X → 倾向Y"
    action: str = ""                            # 推荐的行动
    context: str = ""                           # 适用情境
    confidence: float = 0.5                     # 置信度 0..1（随支持证据增长）
    support_count: int = 1                      # 支持它的经历条数
    weight: float = 0.5                         # 知识强度（衰退比情景记忆慢得多）
    valence: float = 0.0                        # 该行动的平均预期结果 -1..1
    arousal: float = 0.3                        # 情感唤醒度
    importance: int = 6                         # 重要性分级 1-10
    tags: List[str] = field(default_factory=list)
    features: Optional[List[float]] = None      # 结构化特征（代表这类情境）
    source_episodes: List[int] = field(default_factory=list)  # 来源经历 id
    created: float = 0.0
    last_active: float = 0.0
    last_access: float = 0.0
    access_count: int = 0
    touch_count: int = 0
    utility: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    source: str = "consolidation"
    scope: str = "agent"
    pinned: bool = False
    valid_until: Optional[float] = None
    superseded_by: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Association:
    """两条记忆之间的联想连接（联想网络的一条边）。"""

    a: int
    b: int
    strength: float = 0.5                       # 连接强度 0..1
    last_update: float = 0.0                    # 最近一次强化时间


def memory_to_dict(m):
    """任意记忆 → dict（用于 JSON 持久化）。"""
    return asdict(m)


def memory_from_dict(d, cls):
    """dict → 记忆对象。忽略未来版本字段，保持 JSON 双向兼容。"""
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in allowed})
