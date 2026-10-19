"""MemoryAgent —— 使用"海马体+心潮"的 Agent（无需 LLM/API）。

决策循环（纯记忆+驱力驱动，没有任何梯度训练）：
  1. 感知当前情境 → 特征向量 + 文字描述 + 情感坐标(情绪唤醒)
  2. 向 Mind.recall() 检索：四维评分 + 驱力偏置（强驱力让相关记忆更易浮现）
  3. 对每个候选行动，按 记忆权重 × 结果好坏(valence) 聚合打分
  4. 选最优行动执行（epsilon 概率探索）
  5. 用实际结果 reinforce() 升权/降权；行动结果 interact() 消解/激发驱力
  6. 新经历 store() 入库（含 domain 标签，供记忆共振回推驱力）
  7. tick() 推进遗忘曲线 + 驱力增长 + 周期性巩固/梦境

这套闭环证明：有了海马体+心潮，Agent 无需反向传播也能"学会经验"。
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import List, Tuple

from .grid_world import GridWorld
from .mind import Mind
def outcome_text(reward: float, event: str = "move") -> str:
    if event == "food":
        return "吃到了食物！"
    if event == "poison":
        return "吃到了毒药！"
    if event == "wall":
        return "被挡住了"
    return "无事发生"


def outcome_domain(reward: float, event: str = "move") -> str:
    """把结果映射成记忆 domain（用于记忆共振回推驱力）。"""
    if event == "food":
        return "food"
    if event == "poison":
        return "poison"
    if event == "wall":
        return "wall"
    return "empty"


def interaction_kind(reward: float, event: str = "move") -> str:
    if event == "food":
        return "eat"
    if event in ("poison", "wall"):
        return "danger"
    return "explore"


class MemoryAgent:
    def __init__(self, env: GridWorld, mind: Mind,
                 epsilon: float = 0.15, recall_k: int = 6, seed: int = None):
        self.env = env
        self.mind = mind
        self.epsilon = epsilon
        self.recall_k = recall_k
        self.rng = random.Random(seed)
        self.steps = 0
        self.last_used: List = []
        self.last_memories: List = []

    # ------------------------------------------------------------------

    def _emotion(self, feats: List[float]) -> Tuple[float, float]:
        """从感知计算当前情境的情感坐标 (valence, arousal)。"""
        # feats: [4 方向, dxf, dyf, df, dxp, dyp, dp]
        df = feats[6] if len(feats) > 6 else 0.5
        dp = feats[9] if len(feats) > 9 else 0.5
        valence = max(-1.0, min(1.0, 0.4 * (1 - df) - 0.5 * (1 - dp)))
        arousal = min(1.0, 0.3 + 0.4 * (1 - min(df, dp)))  # 离东西越近越紧张
        return (valence, arousal)

    def _dist_after(self, action: str, tx: int, ty: int) -> float:
        """执行某动作后与目标 (tx,ty) 的曼哈顿/欧氏距离。"""
        ax, ay = self.env.agent
        dx, dy = self.env.DELTAS[action]
        nx, ny = ax + dx, ay + dy
        return ((tx - nx) ** 2 + (ty - ny) ** 2) ** 0.5

    def _valid_actions(self) -> List[str]:
        ax, ay = self.env.agent
        out = []
        for a in self.env.ACTIONS:
            dx, dy = self.env.DELTAS[a]
            nx, ny = ax + dx, ay + dy
            if (0 <= nx < self.env.SIZE and 0 <= ny < self.env.SIZE
                    and self.env.grid[ny][nx] != 1):
                out.append(a)
        return out

    def _drive_action(self):
        """驱力引导探索（心潮：欲望影响行动）。

        饿了（hunger 高）→ 倾向朝食物走；怕了（safety 高）→ 倾向远离毒药。
        没有记忆可依时，驱力代替随机探索，让行动更有方向。
        """
        d = self.mind.drives.summary()
        hunger = d.get("hunger", 0.0)
        safety = d.get("safety", 0.0)
        fx, fy = self.env.food_pos
        px, py = self.env.poison_pos
        opts = self._valid_actions()
        if not opts:
            return None
        r = self.rng.random()
        if r < hunger * 0.5:
            return min(opts, key=lambda a: self._dist_after(a, fx, fy))
        if r < hunger * 0.5 + safety * 0.4:
            return max(opts, key=lambda a: self._dist_after(a, px, py))
        return None

    def decide(self) -> Tuple[str, List, List[float], str]:
        """感知 + 回忆（含驱力偏置）+ 决策。返回 (action, 用于决策的记忆, 特征, 描述)。"""
        feats, desc = self.env.observe()
        emotion = self._emotion(feats)
        hits = self.mind.recall(query=desc, features=feats,
                                k=self.recall_k, include_semantic=True,
                                emotion=emotion)

        # 按行动聚合：每个行动取「相似记忆的加权平均期望结果」（episodic-control 式）
        # 先过滤：只保留与当前情境高度相似的记忆（严格 k-NN，
        # 避免"食物方向相同但墙不同"的泛化记忆淹没精确匹配）
        if hits:
            top_score = max(s for s, _ in hits)
            hits = [(s, m) for s, m in hits if s >= 0.6 * top_score]
        val_sum: dict = defaultdict(float)
        w_sum: dict = defaultdict(float)
        by_action: dict = defaultdict(list)
        valid = set(self._valid_actions())
        for score, m in hits:
            if not m.action or m.action not in valid:
                continue
            evidence = score * (0.5 + 0.5 * float(getattr(m, "confidence", 0.5)))
            val_sum[m.action] += evidence * m.valence
            w_sum[m.action] += evidence
            by_action[m.action].append(m)
        # 少量证据向中性先验收缩，避免一次偶然正奖励就被当成稳定策略。
        values = {a: val_sum[a] / (w_sum[a] + 0.12)
                  for a in val_sum if w_sum[a] > 0}

        used: List = []
        best_action = max(values, key=values.get) if values else None
        best_value = values[best_action] if best_action else 0.0
        # 置信门槛：只有当某行动的"期望结果"为正才信任记忆；
        # 没有正期望说明没学会，去探索（心潮驱力引导）
        if (best_action and best_value > 0.0
                and self.rng.random() >= self.epsilon):
            used = by_action[best_action]
            action = best_action
        else:
            # 探索：先尝试驱力引导（心潮），再退化为随机
            action = self._drive_action()
            if action is None:
                opts = self._valid_actions()
                action = (self.rng.choice(opts) if opts
                          else self.rng.choice(self.env.ACTIONS))
        self.last_memories = [m for _, m in hits]
        return action, used, feats, desc

    def step(self) -> float:
        """执行一步：决策 → 行动 → 强化 → 互动 → 存储 → 双时钟推进。"""
        action, used, feats, desc = self.decide()
        reward = self.env.step(action)
        self.steps += 1

        # 用实际结果强化指导决策的记忆（升权/降权）
        for m in used:
            self.mind.reinforce(m.id, reward,
                                note="用了它，结果" + ("好" if reward >= 0 else "不好"))

        # 行动结果消解/激发驱力（心潮 interaction effects）
        event = self.env.last_event
        self.mind.interact(interaction_kind(reward, event), strength=1.0)

        # 记录外部事件与回报，和塑形奖励的符号分开统计。
        self.mind.note_outcome(reward, event)

        # 新经历存入海马体（带 domain 标签，供记忆共振）
        domain = outcome_domain(reward, event)
        self.mind.store(
            situation=desc,
            action=action,
            outcome=outcome_text(reward, event),
            valence=min(1.0, max(-1.0, reward)),
            arousal=self._emotion(feats)[1],
            features=feats,
            tags=["grid", domain],
            importance=7 if abs(reward) > 0.5 else 5,
        )

        # 推进海马体 + 驱力时钟
        self.mind.tick(dt=1.0)
        self.last_used = used
        return reward

    # ------------------------------------------------------------------

    @property
    def accuracy(self) -> float:
        """滑动窗口内"收益≥0"的比例（记忆驱动的决策成功率）。"""
        return self.mind.accuracy

    @property
    def episode(self) -> int:
        return self.env.episodes

    def baseline_random(self, n: int = 500) -> float:
        """对照实验：随机策略的表现（证明没有海马体学不会）。"""
        rng = random.Random(12345)
        total = 0.0
        for _ in range(n):
            a = rng.choice(self.env.ACTIONS)
            total += self.env.step(a)
        self.env.reset()
        return total / n
