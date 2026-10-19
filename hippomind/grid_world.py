"""GridWorld —— 演示环境：一个带食物/毒药/墙壁的网格世界。

Agent 需要"从经验中学会"：向食物走（+1）、避开毒药（-1）、别撞墙（-0.1）。
没有海马体（记忆）时它只能随机乱走；有了海马体后，它会积累"在什么情境
做什么好"的经验，靠记忆的升权/降权自动学会趋利避害。
"""

from __future__ import annotations

import random
from typing import List, Tuple

EMPTY, WALL, FOOD, POISON = 0, 1, 2, 3

CELL_NAMES = {EMPTY: "空地", WALL: "墙壁", FOOD: "食物", POISON: "毒药"}

SHAPING = 0.20  # 趋食塑形系数（每接近食物一格的正信号）
POISON_SHAPING = 0.12  # 避险塑形系数（每远离毒药一格的正信号）
FOOD_REWARD = 2.0  # 吃到食物的奖励
POISON_REWARD = -1.0


class GridWorld:
    SIZE = 12

    # 动作名 → 位移
    DELTAS = {"N": (0, -1), "E": (1, 0), "S": (0, 1), "W": (-1, 0)}
    ACTIONS = ["N", "E", "S", "W"]

    def __init__(self, seed: int = None, wall_density: float = 0.08):
        self.rng = random.Random(seed)
        self.wall_density = wall_density
        self.grid = [[EMPTY] * self.SIZE for _ in range(self.SIZE)]
        self.agent = (self.SIZE // 2, self.SIZE // 2)
        self.food_pos: Tuple[int, int] = (0, 0)
        self.poison_pos: Tuple[int, int] = (0, 0)
        self.steps = 0
        self.episodes = 0
        self.last_reward = 0.0
        self.last_action = ""
        self.last_event = "move"
        self.event_counts = {"food": 0, "poison": 0, "wall": 0, "move": 0}
        self.episode_return = 0.0
        self._build()
        self._spawn_agent()
        self._spawn_food()
        self._spawn_poison()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------

    def _build(self) -> None:
        for y in range(self.SIZE):
            for x in range(self.SIZE):
                if self.rng.random() < self.wall_density:
                    self.grid[y][x] = WALL
        # 保证中心附近留空，方便 Agent 出生
        cx = cy = self.SIZE // 2
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                self.grid[cy + dy][cx + dx] = EMPTY

    def _spawn_agent(self) -> None:
        for _ in range(1000):
            x, y = self.rng.randrange(self.SIZE), self.rng.randrange(self.SIZE)
            if self.grid[y][x] == EMPTY:
                self.agent = (x, y)
                return
        self.agent = (self.SIZE // 2, self.SIZE // 2)

    def _spawn_food(self) -> None:
        self.food_pos = self._random_empty()
        self.grid[self.food_pos[1]][self.food_pos[0]] = FOOD

    def _spawn_poison(self) -> None:
        self.poison_pos = self._random_empty()
        self.grid[self.poison_pos[1]][self.poison_pos[0]] = POISON

    def _random_empty(self) -> Tuple[int, int]:
        for _ in range(5000):
            x, y = self.rng.randrange(self.SIZE), self.rng.randrange(self.SIZE)
            if self.grid[y][x] == EMPTY and (x, y) != self.agent:
                return (x, y)
        raise RuntimeError("no empty cell")

    def reset(self) -> None:
        """新一局：清掉食物/毒药，重铺场景。"""
        for y in range(self.SIZE):
            for x in range(self.SIZE):
                if self.grid[y][x] in (FOOD, POISON):
                    self.grid[y][x] = EMPTY
        self._spawn_agent()
        self._spawn_food()
        self._spawn_poison()
        self.episodes += 1
        self.episode_return = 0.0

    # ------------------------------------------------------------------
    # 感知 & 行动
    # ------------------------------------------------------------------

    def observe(self) -> Tuple[List[float], str]:
        """返回 (特征向量, 情境文字)。特征：4 个方向的格子类型 + 食物/毒药相对坐标与距离。"""
        ax, ay = self.agent
        feats: List[float] = []
        for d in self.ACTIONS:
            dx, dy = self.DELTAS[d]
            nx, ny = ax + dx, ay + dy
            if 0 <= nx < self.SIZE and 0 <= ny < self.SIZE:
                feats.append(float(self.grid[ny][nx]))
            else:
                feats.append(float(WALL))
        # 食物相对位置（归一化到 [-1,1]）与距离
        fx, fy = self.food_pos
        dxf, dyf = (fx - ax) / self.SIZE, (fy - ay) / self.SIZE
        df = ((fx - ax) ** 2 + (fy - ay) ** 2) ** 0.5 / self.SIZE
        # 毒药相对位置与距离
        px, py = self.poison_pos
        dxp, dyp = (px - ax) / self.SIZE, (py - ay) / self.SIZE
        dp = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5 / self.SIZE
        feats += [dxf, dyf, df, dxp, dyp, dp]

        names = "".join(CELL_NAMES[int(v)] for v in feats[:4])
        desc = (f"在({ax},{ay}) 四周[{names}] "
                f"食物在右下方偏差({dxf:+.1f},{dyf:+.1f})距离{df:.2f} "
                f"毒药偏差({dxp:+.1f},{dyp:+.1f})距离{dp:.2f}")
        return feats, desc

    def step(self, action: str) -> float:
        """执行动作，返回奖励。

        奖励设计：吃到食物 +1、毒药 -1、撞墙/边界 -0.1、空走 -0.02，
        另加"趋食塑形"：每步接近食物给正信号、远离给负信号——
        密集信号让纯记忆 Agent 能学会趋利避害。
        """
        ax, ay = self.agent
        dx, dy = self.DELTAS[action]
        nx, ny = ax + dx, ay + dy
        self.last_action = action
        self.steps += 1

        reward = -0.02  # 时间成本
        event = "move"
        if not (0 <= nx < self.SIZE and 0 <= ny < self.SIZE):
            reward = -0.10  # 撞边界
            event = "wall"
        elif self.grid[ny][nx] == WALL:
            reward = -0.10  # 撞墙
            event = "wall"
        else:
            fx, fy = self.food_pos
            prev_d = ((fx - ax) ** 2 + (fy - ay) ** 2) ** 0.5
            cell = self.grid[ny][nx]
            self.agent = (nx, ny)
            if cell == FOOD:
                reward = FOOD_REWARD
                event = "food"
                self.grid[ny][nx] = EMPTY
                self._spawn_food()
            elif cell == POISON:
                reward = POISON_REWARD
                event = "poison"
                self.grid[ny][nx] = EMPTY
                self._spawn_poison()
            else:
                # 趋食塑形：接近食物 → 正奖励（让记忆学会趋利）
                fx2, fy2 = self.food_pos
                new_d = ((fx2 - nx) ** 2 + (fy2 - ny) ** 2) ** 0.5
                reward += SHAPING * (prev_d - new_d)
                # 避险塑形：远离毒药 → 正奖励（让记忆学会避害）
                px, py = self.poison_pos
                prev_dp = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
                new_dp = ((px - nx) ** 2 + (py - ny) ** 2) ** 0.5
                reward += POISON_SHAPING * (new_dp - prev_dp)
        self.last_reward = reward
        self.last_event = event
        self.event_counts[event] += 1
        self.episode_return += reward
        return reward

    # ------------------------------------------------------------------
    # 渲染辅助（供 visualizer 使用）
    # ------------------------------------------------------------------

    def cell_color(self, x: int, y: int):
        c = self.grid[y][x]
        return {EMPTY: "#f7f5ef", WALL: "#4a4a52", FOOD: "#34c759", POISON: "#ff3b30"}[c]
