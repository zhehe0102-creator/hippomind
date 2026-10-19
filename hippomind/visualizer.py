"""Visualizer —— 海马体+心潮 记忆系统的经典工作台界面（matplotlib）。

布局（v1.0 主题化重构）：
  * 页眉：产品标题 + 实时状态胶囊（学习状态 / FPS / 记忆时钟 / 主导驱力）
  * 左：网格世界卡片（Agent 用记忆学习趋利避害的现场）
  * 中：记忆网络卡片（圆点=情景、菱形=语义、连线=联想；大小=权重，
        颜色=结果好坏；回忆时联想扩散高亮；附图例与选中详情）
  * 右：SELF-LEARNING / SELF-EVOLUTION 信息卡 + 心潮驱力条 + 权重曲线
  * 底部：快捷键说明 + 控制按钮轨

交互：
  Space 学习开关   C 巩固   P 剪枝   M 手动回忆   D 梦境消化
  R 重置   S 保存   L 读取
  点击节点=查看   右键=取消选择   双击=重置视图   拖动=平移   滚轮=缩放

性能契约：信息面板低频刷新、权重曲线只保留 Top-8 历史，避免长期运行时
Matplotlib artist 重建导致的内存滞留（与 v0.4.2 基准一致）。
"""

from __future__ import annotations

import math
import time as _time
from collections import deque
from typing import Optional

import matplotlib as mpl

from .ui_theme import (ACCENT_BLUE, ACCENT_CYAN, ACCENT_GREEN, ACCENT_ORANGE,
                       ACCENT_PURPLE, ACCENT_RED, ACCENT_YELLOW, BG, GRID_LINE,
                       MUTED, PANEL, PANEL_EDGE, PANEL_SOFT, TEXT, TEXT_SUB,
                       apply_theme, card, chip, pill_button, style_axes,
                       valence_cmap)

apply_theme()

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.collections import LineCollection
from matplotlib.patches import Rectangle

from .mind import Mind
from .grid_world import GridWorld
from .agent import MemoryAgent
from .memory import SemanticMemory
from .drives import DRIVE_LABELS

EDGE_COLOR = "#5f7ba6"
ACTIVE_COLOR = ACCENT_YELLOW


def _sunflower_pos(mid: int) -> tuple:
    """稳定布局：按 id 生成向日葵点阵位置（新记忆不打扰旧记忆）。"""
    theta = mid * 2.399963229728653  # 黄金角
    radius = 0.55 + math.sqrt(mid) * 0.42
    return (math.cos(theta) * radius, math.sin(theta) * radius)


class Visualizer:
    def __init__(self, mind: Mind, env: GridWorld, agent: MemoryAgent):
        self.mind = mind
        self.hippo = mind            # 兼容别名（海马体接口）
        self.env = env
        self.agent = agent
        self.closing = False
        self.learning_on = True
        self.selected_id: Optional[int] = None
        self.highlight: set = set()          # 最近被联想激活的记忆 id
        self._highlight_until = 0.0
        self._press = None
        self._layout: dict = {}
        self._last_frame = _time.perf_counter()
        self._frame_no = 0
        self.fps = 0.0
        # Informational panels do not need frame-rate updates. Rebuilding
        # their Matplotlib artists too often causes allocator retention.
        self._panel_refresh_frames = 300
        self._max_edges = 400

        self._build_figure()
        self._build_world()
        self._build_events()
        self._build_net()
        self._build_drives()
        self._build_controls()

    # ==================================================================
    # 界面搭建
    # ==================================================================

    def _build_figure(self):
        self.fig = plt.figure(figsize=(16.5, 9.4), facecolor=BG)
        try:
            self.fig.canvas.manager.set_window_title("hippomind · 海马记忆中枢")
        except AttributeError:
            pass

        # ---- 页眉：标题 + 状态胶囊 --------------------------------------
        self.fig.text(0.025, 0.962, "HIPPOMIND", color=TEXT, fontsize=15,
                      fontweight="bold")
        self.fig.text(0.118, 0.962, "海马记忆中枢 · 自主学习 × 自主演化",
                      color=TEXT_SUB, fontsize=10)
        self._chips = {
            "learning": chip(self.fig, 0.588, 0.962, "学习", "运行中", color=ACCENT_GREEN),
            "fps": chip(self.fig, 0.694, 0.962, "FPS", "-", color=TEXT_SUB),
            "clock": chip(self.fig, 0.758, 0.962, "时钟", "0.0s", color=TEXT_SUB),
            "drive": chip(self.fig, 0.856, 0.962, "主导驱力", "-", color=ACCENT_ORANGE),
        }

        # ---- 面板区 ------------------------------------------------------
        self.ax_world = self.fig.add_axes([0.025, 0.310, 0.270, 0.615])
        self.ax_events = self.fig.add_axes([0.025, 0.125, 0.270, 0.150])
        self.ax_net = self.fig.add_axes([0.318, 0.125, 0.298, 0.800])
        self.ax_stats = self.fig.add_axes([0.638, 0.540, 0.337, 0.385])
        self.ax_drives = self.fig.add_axes([0.638, 0.372, 0.337, 0.140])
        self.ax_curves = self.fig.add_axes([0.638, 0.125, 0.337, 0.222])
        style_axes(self.ax_world, title="网格世界 · Agent 学习现场")
        style_axes(self.ax_events, facecolor=BG)
        self.ax_events.axis("off")
        style_axes(self.ax_net, title="海马体 · 记忆网络（联想扩散）")
        style_axes(self.ax_stats, facecolor=BG)
        self.ax_stats.axis("off")
        style_axes(self.ax_drives, title="心潮 · 驱力引擎", title_size=10)
        style_axes(self.ax_curves, title="记忆权重升降 · Upweight / Downweight",
                   title_size=10)

        # 底部帮助行（位于按钮轨上方，避免小窗口碰撞）
        self.fig.text(0.025, 0.088,
                      "Space=学习  C=巩固  P=剪枝  M=回忆  D=梦境  R=重置  S=保存  L=读取  G=快照     "
                      "点击节点查看 · 右键取消 · 双击复位 · 拖拽平移 · 滚轮缩放",
                      color=MUTED, fontsize=8.0)

    # ------------------------- 世界视图 -------------------------
    def _build_world(self):
        ax = self.ax_world
        ax.set_xlim(-0.6, self.env.SIZE + 0.6)
        ax.set_ylim(-0.6, self.env.SIZE + 0.6)
        ax.set_aspect("equal")
        ax.set_xticks([]); ax.set_yticks([])
        for x in range(self.env.SIZE + 1):
            ax.axvline(x - 0.5, color=GRID_LINE, lw=0.6)
            ax.axhline(x - 0.5, color=GRID_LINE, lw=0.6)
        for y in range(self.env.SIZE):
            for x in range(self.env.SIZE):
                if self.env.grid[y][x] == 1:
                    ax.add_patch(Rectangle((x - 0.5, y - 0.5), 1, 1,
                                           color="#39404f", zorder=2))
        self.food_scat = ax.scatter([], [], s=280, marker="*",
                                    color=ACCENT_GREEN, edgecolors="white", zorder=4)
        self.poison_scat = ax.scatter([], [], s=230, marker="X",
                                      color=ACCENT_RED, edgecolors="white", zorder=4)
        self.agent_line, = ax.plot([], [], color=ACCENT_BLUE, lw=3.5,
                                   marker="o", markersize=10,
                                   markerfacecolor=ACCENT_BLUE, zorder=5)
        self._world_status = ax.text(0.02, 0.98, "", transform=ax.transAxes,
                                     va="top", color="#c8d2ea", fontsize=9, zorder=6)

    # ------------------------- 事件条（世界下方） -------------------------
    def _build_events(self):
        from .ui_theme import card as _card
        self.ax_events.set_xlim(0, 1)
        self.ax_events.set_ylim(0, 1)
        _card(self.ax_events, 0.0, 0.0, 1.0, 1.0,
              title="最近事件 · OUTCOMES", accent=ACCENT_CYAN)
        self._events_lines = [
            self.ax_events.text(0.05, 0.62, "", color=TEXT_SUB, fontsize=8.6,
                                transform=self.ax_events.transAxes),
            self.ax_events.text(0.05, 0.34, "", color=MUTED, fontsize=8.2,
                                transform=self.ax_events.transAxes),
            self.ax_events.text(0.05, 0.08, "", color=ACCENT_PURPLE, fontsize=7.8,
                                transform=self.ax_events.transAxes),
        ]

    # ------------------------- 记忆网络视图 -------------------------
    def _build_net(self):
        ax = self.ax_net
        ax.set_xlim(-7, 7)
        ax.set_ylim(-7, 7)
        ax.set_aspect("equal")
        ax.set_xticks([]); ax.set_yticks([])
        self.edge_lc = LineCollection([], colors=EDGE_COLOR, linewidths=1.1,
                                      zorder=1, alpha=0.35)
        ax.add_collection(self.edge_lc)
        self.scat_epi = ax.scatter([], [], s=50, zorder=3, linewidths=1.4)
        self.scat_sem = ax.scatter([], [], s=70, marker="D", zorder=4, linewidths=1.4)
        self.scat_act = ax.scatter([], [], s=240, facecolors="none",
                                   edgecolors=ACTIVE_COLOR, linewidths=2.4, zorder=5)
        # 图例：三种节点形态一目了然
        legend_handles = [
            plt.Line2D([], [], linestyle="none", marker="o", markersize=7,
                       markerfacecolor="#aeb9cb", markeredgecolor=PANEL_EDGE,
                       label="情景记忆"),
            plt.Line2D([], [], linestyle="none", marker="D", markersize=7,
                       markerfacecolor=ACCENT_BLUE, markeredgecolor=PANEL_EDGE,
                       label="语义知识"),
            plt.Line2D([], [], linestyle="none", marker="o", markersize=9,
                       markerfacecolor="none", markeredgecolor=ACTIVE_COLOR,
                       markeredgewidth=1.8, label="联想激活"),
        ]
        leg = ax.legend(handles=legend_handles, loc="lower left", fontsize=7,
                        framealpha=0.12, labelcolor=TEXT_SUB,
                        edgecolor=PANEL_EDGE, borderpad=0.6)
        leg.set_zorder(7)
        self._net_instructions = ax.text(
            0.02, 0.985, "", transform=ax.transAxes, va="top",
            color=TEXT_SUB, fontsize=8.5,
            bbox=dict(boxstyle="round,pad=0.35", facecolor=PANEL,
                      edgecolor=PANEL_EDGE, alpha=0.92))
        self._net_counts = ax.text(
            0.98, 0.018, "", transform=ax.transAxes, ha="right",
            color=MUTED, fontsize=8)

    # ------------------------- 驱力面板 -------------------------
    def _build_drives(self):
        ax = self.ax_drives
        names = list(self.mind.drives.drives)
        ax.set_xlim(0, 1)
        ax.set_ylim(-0.4, len(names) - 0.6)
        ax.set_yticks(range(len(names)))
        ax.set_yticklabels([DRIVE_LABELS.get(d, d) for d in names], fontsize=8)
        ax.set_xticks([])
        cmap = mcolors.LinearSegmentedColormap.from_list(
            "drive", [ACCENT_BLUE, ACCENT_GREEN])  # 低 → 高
        self._drive_bars = []
        self._drive_tracks = []
        self._drive_texts = []
        for i in range(len(names)):
            # 背景轨道让"满格"边界可见，数值更直观
            track = ax.barh(i, 1.0, color="#182034", height=0.55, zorder=1)
            bar = ax.barh(i, 0.0, color=cmap(0.0), height=0.55, zorder=2)
            self._drive_tracks.append(track)
            self._drive_bars.append(bar)
            t = ax.text(1.01, i, "", va="center", fontsize=7.5,
                        color="#dfe6f5")
            self._drive_texts.append(t)
        self._drive_cmap = cmap

    # ------------------------- 控制按钮 -------------------------
    def _build_controls(self):
        actions = [
            ("学习 开/关 [Space]", lambda e: self.toggle_learning(), ACCENT_GREEN),
            ("巩固 [C]", lambda e: self.do_consolidate(), ACCENT_BLUE),
            ("剪枝 [P]", lambda e: self.do_prune(), ACCENT_ORANGE),
            ("回忆 [M]", lambda e: self.do_manual_recall(), ACCENT_CYAN),
            ("梦境 [D]", lambda e: self.do_dream(), ACCENT_PURPLE),
            ("重置 [R]", lambda e: self.do_reset(), ACCENT_RED),
            ("保存 [S]", lambda e: self.do_save(), ACCENT_BLUE),
            ("读取 [L]", lambda e: self.do_load(), ACCENT_BLUE),
        ]
        x = 0.025
        for label, callback, accent in actions:
            pill_button(self.fig, [x, 0.028, 0.105, 0.046], label, callback,
                        accent=accent)
            x += 0.113

        self.fig.canvas.mpl_connect("close_event", lambda e: setattr(self, "closing", True))
        self.fig.canvas.mpl_connect("button_press_event", self._on_press)
        self.fig.canvas.mpl_connect("button_release_event", self._on_release)
        self.fig.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.fig.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

    # ==================================================================
    # 每帧更新
    # ==================================================================

    def frame(self):
        self._frame_no += 1
        self._update_world()
        # matplotlib 重建大量文本/曲线对象很昂贵；低变化面板无需逐帧刷新。
        if self._frame_no == 1 or self._frame_no % 2 == 0:
            self._update_net()
            self._update_drives()
        if self._frame_no == 1 or self._frame_no % self._panel_refresh_frames == 0:
            self._update_stats()
            self._update_curves()
            self._update_events()
        self._update_chips()
        now = _time.perf_counter()
        self.fps = 0.9 * self.fps + 0.1 * (1.0 / max(1e-6, now - self._last_frame))
        self._last_frame = now
        self.fig.canvas.draw_idle()

    def _update_chips(self):
        learning_label, learning_value = self._chips["learning"]
        learning_value.set_text("运行中" if self.learning_on else "已暂停")
        learning_value.set_color(ACCENT_GREEN if self.learning_on else ACCENT_RED)
        _, fps_value = self._chips["fps"]
        fps_value.set_text(f"{self.fps:4.1f}")
        _, clock_value = self._chips["clock"]
        clock_value.set_text(f"{self.hippo.time:6.1f}s")
        _, drive_value = self._chips["drive"]
        hot = self.mind.drives.hottest() if self.learning_on else ""
        drive_value.set_text(DRIVE_LABELS.get(hot, hot or "-"))

    # ------------------------- 世界 -------------------------
    def _update_world(self):
        fx, fy = self.env.food_pos
        px, py = self.env.poison_pos
        self.food_scat.set_offsets([(fx, fy)])
        self.poison_scat.set_offsets([(px, py)])
        ax_, ay_ = self.env.agent
        dx, dy = self.env.DELTAS[self.env.last_action] if self.env.last_action else (0, 0)
        self.agent_line.set_data([ax_, ax_ + dx * 0.55], [ay_, ay_ + dy * 0.55])
        text = (f"Agent 在 ({ax_},{ay_}) · 最近行动 {self.env.last_action or '-'} · "
                f"上一步奖励 {self.env.last_reward:+.2f}")
        self._world_status.set_text(text)

    # ------------------------- 事件条 -------------------------
    def _update_events(self):
        s = self.hippo.stats_summary()
        line1 = (f"觅食 {s['food']} · 中毒 {s['poison']} · "
                 f"撞墙 {s['wall']} · 移动 {s['moves']}")
        total_return = s.get("total_return", 0.0)
        line2 = f"累计回报 {total_return:+.2f} · 平均 {s.get('average_return', 0.0):+.4f}"
        dream_line = ""
        if self.hippo.dreams_history:
            last = self.hippo.dreams_history[-1]
            dream_line = "梦境 · " + (last["texts"][-1][:38] if last["texts"] else "")
        for artist, text in zip(self._events_lines, (line1, line2, dream_line)):
            artist.set_text(text)

    # ------------------------- 记忆网络 -------------------------
    def _update_net(self):
        eps = list(self.hippo.episodic.values())
        sem = list(self.hippo.semantic.values())
        live_ids = {m.id for m in eps + sem}
        for mid in set(self._layout) - live_ids:
            self._layout.pop(mid, None)

        ep = [self._layout.get(m.id) or self._layout.setdefault(m.id, _sunflower_pos(m.id))
              for m in eps]
        sp = [self._layout.get(m.id) or self._layout.setdefault(m.id, _sunflower_pos(m.id))
              for m in sem]
        cmap = valence_cmap()
        if ep:
            self.scat_epi.set_offsets(ep)
            self.scat_epi.set_sizes([45 + 300 * m.weight for m in eps])
            self.scat_epi.set_facecolors([cmap((m.valence + 1) / 2) for m in eps])
            self.scat_epi.set_edgecolors([self._node_edge(m) for m in eps])
        if sp:
            self.scat_sem.set_offsets(sp)
            self.scat_sem.set_sizes([90 + 260 * m.weight for m in sem])
            self.scat_sem.set_facecolors([ACCENT_BLUE for _ in sem])
            self.scat_sem.set_edgecolors([self._node_edge(m) for m in sem])

        assoc = sorted(self.hippo.assoc.values(), key=lambda a: a.strength, reverse=True)
        assoc = assoc[: self._max_edges]
        segs, alphas = [], []
        for a in assoc:
            if a.a not in live_ids or a.b not in live_ids:
                continue
            p1 = self._layout.get(a.a) or self._layout.setdefault(a.a, _sunflower_pos(a.a))
            p2 = self._layout.get(a.b) or self._layout.setdefault(a.b, _sunflower_pos(a.b))
            segs.append([p1, p2])
            alphas.append(max(0.05, a.strength))
        self.edge_lc.set_segments(segs)
        if segs:
            self.edge_lc.set_alpha([max(0.06, x * 0.55) for x in alphas])
            self.edge_lc.set_linewidths([0.8 + 2.2 * x for x in alphas])
        else:
            self.edge_lc.set_alpha(0.0)
            self.edge_lc.set_linewidths([])

        if self.hippo.time < self._highlight_until and self.highlight:
            ids = [i for i in self.highlight if i in self._layout]
            pos = [self._layout[i] for i in ids]
            self.scat_act.set_offsets(pos)
            self.scat_act.set_sizes([130 for _ in ids])
        else:
            self.highlight = set()
            self.scat_act.set_offsets(np.empty((0, 2)))

        info = ""
        if self.selected_id:
            m = self.hippo._get(self.selected_id)
            if m is not None:
                kind = "语义知识" if isinstance(m, SemanticMemory) else "情景记忆"
                info = (f"{kind} #{m.id}   权重 {m.weight:.3f}   情感 {m.valence:+.2f}\n"
                        f"情境: {m.situation}\n"
                        f"行动: {m.action or '-'} → {m.outcome or '-'}\n"
                        f"重要度 {m.importance}/10 · 触碰 {m.touch_count} 次 · "
                        f"回忆 {m.access_count} 次 · 唤醒 {m.arousal:.2f}")
        elif self.highlight:
            info = f"联想扩散激活 {len(self.highlight)} 条记忆"
        self._net_instructions.set_text(info)
        self._net_counts.set_text(
            f"情景 {len(eps)} · 知识 {len(sem)} · 联想 {len(self.hippo.assoc)}")

    def _node_edge(self, m) -> str:
        if m.id == self.selected_id:
            return ACTIVE_COLOR
        if m.id in self.highlight:
            return ACCENT_ORANGE
        return "#000000" if m.weight > 0.4 else "#555555"

    # ------------------------- 驱力面板 -------------------------
    def _update_drives(self):
        vals = self.mind.drives.summary()
        for i, d in enumerate(self.mind.drives.drives):
            v = vals.get(d, 0.0)
            for rect in self._drive_bars[i]:
                rect.set_width(v)
                rect.set_color(self._drive_cmap(v))
            self._drive_texts[i].set_text(f"{v:.2f}")

    # ------------------------- 统计面板 -------------------------
    def _update_stats(self):
        ax = self.ax_stats
        ax.clear()
        ax.axis("off")
        s = self.hippo.stats_summary()
        status = "运行中" if self.learning_on else "已暂停"
        status_color = ACCENT_GREEN if self.learning_on else ACCENT_RED
        hot = s.get("drive_hottest", "")
        hot_label = DRIVE_LABELS.get(hot, hot)

        card(ax, 0.0, 0.52, 1.0, 0.47, title="自主学习 · SELF-LEARNING",
             accent=ACCENT_GREEN)
        rows = [
            ("状态", status, status_color),
            ("觅食 / 中毒", f"{s['food']} / {s['poison']}", TEXT),
            ("近期回报", f"{s['recent_return']:+.3f}", TEXT),
            ("存入 / 合并", f"{s['stored']} / {s['merged']}", TEXT),
            ("主导驱力", hot_label or "-", ACCENT_ORANGE),
        ]
        for i, (k, v, c) in enumerate(rows):
            yy = 0.52 + 0.47 - 0.085 - i * 0.072
            ax.text(0.05, yy, k, color=MUTED, fontsize=9, va="center")
            ax.text(0.95, yy, v, color=c, fontsize=9.5, va="center", ha="right",
                    fontweight="bold")

        card(ax, 0.0, 0.01, 1.0, 0.47, title="记忆进化 · SELF-EVOLUTION",
             accent=ACCENT_ORANGE)
        rows = [
            ("活跃情景 / 冷归档", f"{s['episodic']} / {s['archived']}", TEXT),
            ("语义知识", f"{s['semantic']}", TEXT),
            ("联想连接", f"{s['associations']}", TEXT),
            ("累计归档", f"{s['archived_total']}", TEXT),
            ("巩固规则 / 梦境", f"{s['consolidated']} / {s.get('dreams', 0)}", ACCENT_PURPLE),
        ]
        for i, (k, v, c) in enumerate(rows):
            yy = 0.01 + 0.47 - 0.085 - i * 0.072
            ax.text(0.05, yy, k, color=MUTED, fontsize=9, va="center")
            ax.text(0.95, yy, v, color=c, fontsize=9.5, va="center", ha="right",
                    fontweight="bold")

    # ------------------------- 权重曲线 + 梦境 -------------------------
    def _update_curves(self):
        ax = self.ax_curves
        top = sorted(self.all_memories(), key=lambda m: m.weight, reverse=True)[:8]
        if not hasattr(self, "_curves"):
            self._curves = {}
        # Pruned/archived memories must not keep their history forever.
        top_ids = {m.id for m in top}
        for mid in set(self._curves) - top_ids:
            self._curves.pop(mid, None)
        for m in top:
            c = self._curves.setdefault(m.id, deque(maxlen=600))
            c.append((self.hippo.time, m.weight))
        ax.clear()
        style_axes(ax, title_size=10)
        ax.set_facecolor(PANEL_SOFT)
        ax.grid(True, color=GRID_LINE, lw=0.5, alpha=0.7)
        colors = plt.cm.tab10.colors
        for i, (mid, c) in enumerate(self._curves.items()):
            if not c:
                continue
            xs, ys = zip(*c)
            ax.plot(xs, ys, color=colors[i % 10], lw=1.4, alpha=0.9, label=f"#{mid}")
        ax.set_ylim(0, 1.02)
        if self._curves:
            ax.legend(loc="upper left", fontsize=6, framealpha=0.15,
                      labelcolor=TEXT_SUB, ncol=2, edgecolor=PANEL_EDGE)
        ax.set_ylabel("权重", color=MUTED, fontsize=8)
        # 梦境记录（安全：只展示，不冒充真实记忆）
        if self.hippo.dreams_history:
            last = self.hippo.dreams_history[-1]
            dream_txt = " · ".join(last["texts"][:2])
            ax.set_title(f"记忆权重升降 · 最近梦境: {dream_txt[:44]}...",
                         fontsize=8, pad=4, color=TEXT_SUB, loc="left")

    def all_memories(self):
        return self.hippo.all_memories()

    # ==================================================================
    # 控制动作
    # ==================================================================

    def toggle_learning(self):
        self.learning_on = not self.learning_on
        self.hippo.learning_enabled = self.learning_on
        self.mind.learning_enabled = self.learning_on

    def do_consolidate(self):
        n = self.hippo.consolidate()
        print(f"[巩固] 生成 {n} 条语义知识")

    def do_prune(self):
        n = self.hippo.prune()
        print(f"[剪枝] 移除 {n} 条弱记忆/弱连接")

    def do_dream(self):
        dreams = self.hippo.dream()
        print(f"[梦境] 消化 {len(dreams)} 条记忆")
        for d in dreams:
            print(f"   💤 {d}")

    def do_manual_recall(self):
        feats, desc = self.env.observe()
        emotion = self.agent._emotion(feats)
        hits = self.mind.recall(query=desc, features=feats, k=6, emotion=emotion)
        self.highlight = {m.id for _, m in hits}
        self._highlight_until = self.hippo.time + 2.5
        print(f"[回忆] 检索到 {len(hits)} 条记忆（当前主导驱力：{self.mind.drives.hottest()}）：")
        for score, m in hits[:4]:
            action = getattr(m, "action", "") or "知识"
            summary = getattr(m, "outcome", "") or getattr(m, "rule", "")
            print(f"   {score:.2f}  #{m.id} {action} {summary}")

    def do_reset(self):
        self.env.reset()
        self.hippo.clear()
        self._layout.clear()
        self.selected_id = None
        print("[重置] 世界与记忆已清空")

    def do_save(self):
        self.hippo.save("hippocampus_memory.json")
        print("[保存] hippocampus_memory.json（含记忆+驱力）")

    def do_load(self):
        self.hippo.load("hippocampus_memory.json")
        print("[读取] 已加载记忆库+驱力")

    def do_snapshot(self, out_dir: str = "output") -> Optional[Path]:
        """把当前帧保存为 PNG 快照（G 键），返回文件路径。"""
        from pathlib import Path as _Path
        directory = _Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        path = directory / f"hippomind-workbench-{stamp}.png"
        self.fig.savefig(path, dpi=110)
        print(f"[快照] {path}")
        return path

    # ==================================================================
    # 鼠标 / 键盘
    # ==================================================================

    def _on_press(self, event):
        if event.inaxes is not self.ax_net:
            return
        if event.button == 3:
            self.selected_id = None
            return
        self._press = (event.xdata, event.ydata)
        if event.dblclick:
            self.ax_net.set_xlim(-7, 7)
            self.ax_net.set_ylim(-7, 7)
        elif event.button == 1:
            self._select_nearest(event)

    def _on_release(self, event):
        self._press = None

    def _on_motion(self, event):
        if event.inaxes is not self.ax_net or self._press is None or event.xdata is None:
            return
        dx = self._press[0] - event.xdata
        dy = self._press[1] - event.ydata
        xl = self.ax_net.get_xlim(); yl = self.ax_net.get_ylim()
        self.ax_net.set_xlim(xl[0] + dx, xl[1] + dx)
        self.ax_net.set_ylim(yl[0] + dy, yl[1] + dy)

    def _on_scroll(self, event):
        if event.inaxes is not self.ax_net or event.xdata is None:
            return
        base = 1.12 ** event.step
        xl = self.ax_net.get_xlim(); yl = self.ax_net.get_ylim()
        self.ax_net.set_xlim(event.xdata - (event.xdata - xl[0]) * base,
                             event.xdata + (xl[1] - event.xdata) * base)
        self.ax_net.set_ylim(event.ydata - (event.ydata - yl[0]) * base,
                             event.ydata + (yl[1] - event.ydata) * base)

    def _select_nearest(self, event):
        best, best_d = None, 0.4
        for m in self.all_memories():
            pos = self._layout.get(m.id)
            if pos is None:
                continue
            px, py = pos
            d = ((px - event.xdata) ** 2 + (py - event.ydata) ** 2) ** 0.5
            if d < best_d:
                best, best_d = m, d
        self.selected_id = best.id if best else None

    def _on_key(self, event):
        k = event.key.lower()
        if k == " ":
            self.toggle_learning()
        elif k == "c":
            self.do_consolidate()
        elif k == "p":
            self.do_prune()
        elif k == "m":
            self.do_manual_recall()
        elif k == "d":
            self.do_dream()
        elif k == "r":
            self.do_reset()
        elif k == "s":
            self.do_save()
        elif k == "l":
            self.do_load()
        elif k == "g":
            self.do_snapshot()
