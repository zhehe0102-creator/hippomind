"""全屏层状海马网络可视化。

这个视图面向真实记忆生命周期，而不是绘制一个虚构的神经网络：驱力、标签域、
活跃情景、语义规则、冷归档和共享 Agent 各占一层，边来自驱力亲和、标签归属、
已存联想、巩固来源和共通记忆来源。
"""

from __future__ import annotations

import math
import sqlite3
import time as _time
import zlib
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import matplotlib as mpl
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection
from matplotlib.patches import FancyBboxPatch, Rectangle

from .agent import MemoryAgent
from .drives import DRIVE_LABELS
from .grid_world import GridWorld
from .memory import SemanticMemory
from .mind import Mind
from .hub_client import HubStatusClient
from .ui_theme import apply_theme

apply_theme()


_fonts = ["PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti",
          "Arial Unicode MS", "Noto Sans CJK SC", "Microsoft YaHei", "SimHei"]
mpl.rcParams["font.sans-serif"] = _fonts + list(mpl.rcParams.get("font.sans-serif", []))
mpl.rcParams["font.family"] = "sans-serif"
mpl.rcParams["axes.unicode_minus"] = False
mpl.rcParams["toolbar"] = "None"

BG = "#070b12"          # 深空蓝黑（比纯黑更有质感）
BG_PANEL = "#0d1420"    # 面板底色
FRAME = "#1e2a3d"       # 面板描边
TEXT = "#e8edf6"
MUTED = "#7c8aa5"
CYAN = "#5eead4"        # teal 500
BLUE = "#93c5fd"
VIOLET = "#a78bfa"
AMBER = "#fbbf24"
PINK = "#f472b6"
GREEN = "#34d399"
RED = "#f87171"
ACTIVE = "#fde047"
LAVENDER = "#c4b5fd"
EDGE_WIDTH = 0.55
# 终端风等宽字体（拉丁字形），中文逐字符回退到 PingFang 等 CJK 字体。
MONO = ["Menlo", "Monaco", "Courier New", "PingFang SC",
        "Hiragino Sans GB", "Arial Unicode MS"]

EDGE_ALPHA = {
    "drive_domain": (0.22, 0.20),
    "drive_memory": (0.05, 0.16),
    "domain_memory": (0.025, 0.11),
    "association": (0.02, 0.10),
    "resonance": (0.03, 0.11),
    "source": (0.10, 0.22),
    "commons": (0.14, 0.16),
}

EDGE_BUDGET = {
    "drive_domain": 0.08,
    "drive_memory": 0.12,
    "domain_memory": 0.24,
    "association": 0.18,
    "resonance": 0.21,
    "source": 0.11,
    "commons": 0.06,
}

DOMAIN_LABELS = {
    "food": "食物",
    "poison": "毒药",
    "wall": "墙壁",
    "empty": "空地",
    "danger": "危险",
    "social": "社交",
    "task": "任务",
    "learn": "学习",
    "grid": "网格世界",
    "shared-chat": "共享对话",
    "promoted": "已提升",
}

VALENCE_CMAP = mcolors.LinearSegmentedColormap.from_list(
    "network_valence", [RED, "#b9c2d2", GREEN])


class LayeredMemoryVisualizer:
    """Wazone 风格的层状记忆拓扑，所有节点和边都对应真实系统状态。"""

    LAYERS = (
        ("drive", "心潮驱力", 0.50),
        ("domain", "记忆域", 1.42),
        ("episodic", "活跃情景", 2.48),
        ("semantic", "语义规则", 3.58),
        ("archive", "冷归档", 4.66),
        ("commons", "共通智能体", 5.62),
    )

    KIND_STYLE = {
        "drive": (CYAN, "s", 50),
        "domain": ("#2dd4bf", "o", 26),
        "episodic": ("#94a3b8", "o", 20),
        "semantic": (BLUE, "D", 34),
        "archive": ("#64748b", ".", 16),
        "commons": (PINK, "h", 42),
    }

    def __init__(self, mind: Mind, env: GridWorld, agent: MemoryAgent,
                 ledger=None,
                 max_nodes: int = 280, max_edges: int = 300,
                 curve_strength: float = 0.14, curve_steps: int = 11,
                 topology_refresh_frames: int = 900,
                 hub_client: Optional[HubStatusClient] = None):
        self.mind = mind
        self.env = env
        self.agent = agent
        self.max_nodes = max(60, int(max_nodes))
        self.max_edges = max(200, int(max_edges))
        self.curve_strength = max(0.0, min(0.45, float(curve_strength)))
        self.curve_steps = max(7, min(25, int(curve_steps)))
        # Full topology rebuilds call ``Axes.clear`` and recreate hundreds of
        # Matplotlib artists.  On macOS Quartz those allocations are retained
        # by the process allocator, so rebuilding every few frames grows RSS
        # for the entire lifetime of a GUI session.  Keep animation/state
        # updates cheap and rebuild the topology at a human-visible cadence
        # (30 seconds at the default 30 FPS); callers can still tune this down.
        self.topology_refresh_frames = max(30, min(7_200, int(topology_refresh_frames)))
        self._rebuild_count = 0
        self.closing = False
        self.learning_on = True
        self.selected_key: Optional[str] = None
        # 时间轴回放：按记忆创建时间重演网络生长（T 键切换）。
        self.timeline_mode = False
        self.timeline_speed = 1.0   # 回放速度倍率（+/- 调节，空格暂停/继续）
        self.timeline_playing = False
        self.timeline_cursor = 0.0
        self._timeline_t0 = 0.0
        self._timeline_t1 = 0.0
        self._timeline_span = 1.0
        self.highlight: set = set()
        # 实体图层（前景轮八）：选中记忆文本命中账本 claim 主语时，
        # 共现主语的记忆间边提亮，详情面板显示主语邻域摘要。
        self._ledger = ledger
        self._entity_index: Dict[str, Dict[str, int]] = {}
        self._entity_index_at = 0.0
        self.recent: set = set()
        self._highlight_until = 0.0
        self._frame_no = 0
        self._last_frame = _time.perf_counter()
        self.fps = 0.0
        self._press = None
        self._view_dirty = False
        self._positions: Dict[str, Tuple[float, float]] = {}
        self._objects: Dict[str, object] = {}
        self._collection_keys: Dict[int, List[str]] = {}
        self._edge_count = 0
        self._edge_type_counts = Counter()
        self._edge_width_range = (0.0, 0.0)
        self._max_curve_deviation = 0.0
        self._curve_segments: List[List[Tuple[float, float]]] = []
        self._visible_counts = Counter()
        self._commons_stats = None
        self._commons_checked_at = -1000
        self.hub_client = hub_client
        self._hub_stats = None
        self._hub_conflicts: Optional[Dict[str, Any]] = None
        self._hub_checked_at = -1000
        self._hub_context = ""
        self.last_action = "就绪"
        self._build_figure()
        self.rebuild()

    def _build_figure(self) -> None:
        # dpi=150：1920x1080 的画布在 Retina 全屏下会被拉伸发虚；
        # 150dpi 提供 2880x1620 背衬存储，文字与线条明显更锐利。
        self.fig = plt.figure(figsize=(19.2, 10.8), facecolor=BG, dpi=150)
        try:
            self.fig.canvas.manager.set_window_title("hippomind · 六层海马记忆网络")
        except AttributeError:
            pass
        toolbar = getattr(self.fig.canvas.manager, "toolbar", None)
        if toolbar is not None:
            if hasattr(toolbar, "setVisible"):
                toolbar.setVisible(False)
            elif hasattr(toolbar, "pack_forget"):
                toolbar.pack_forget()
        self.ax = self.fig.add_axes([0.175, 0.105, 0.81, 0.80], facecolor=BG)
        self.ax_hud = self.fig.add_axes([0.022, 0.105, 0.135, 0.76], facecolor="none")
        # 内嵌查询框：输入自然语言问题，Enter 即在 GUI 内跑 Context Pack
        self.ax_query = self.fig.add_axes([0.175, 0.012, 0.60, 0.042],
                                          facecolor=BG)
        self.ax_query.set_xticks([])
        self.ax_query.set_yticks([])
        for spine in self.ax_query.spines.values():
            spine.set_color("#1c1c2c")
        self.query_box = None  # 延迟到 show() 后由 _enable_query_box 创建
        self.ax_hud.set_xlim(0.0, 1.0)
        self.ax_hud.set_ylim(0.0, 1.0)
        self.ax_hud.axis("off")

        # 极简文字层：小标题 + 右侧单行状态 + 底部单行键位。
        self.fig.text(0.024, 0.974, "HippoMind 记忆网络",
                      color="#dfe7f2", fontsize=12, fontweight="bold",
                      va="top")
        self.status_text = self.fig.text(
            0.988, 0.972, "", color=MUTED, fontsize=7.5,
            ha="right", va="top", family=MONO)
        # 详情两行放在查询框上方，不再压在框内。
        self.detail_text = self.fig.text(
            0.177, 0.085, "当前选择：无", color="#aeb9cb", fontsize=8.0,
            va="bottom")
        self.context_text = self.fig.text(
            0.177, 0.062, "", color="#7faea7", fontsize=7.2, va="bottom")

        self._build_hud()
        self.fig.canvas.mpl_connect("close_event", lambda e: setattr(self, "closing", True))
        self.fig.canvas.mpl_connect("pick_event", self._on_pick)
        self.fig.canvas.mpl_connect("button_press_event", self._on_press)
        self.fig.canvas.mpl_connect("button_release_event", self._on_release)
        self.fig.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.fig.canvas.mpl_connect("scroll_event", self._on_scroll)
        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

    def _panel(self, title: str, color: str, y_top: float,
               rows: List[Tuple[str, str]], *, box_bottom: float) -> None:
        """圆角卡片 HUD 面板：柔和底色 + 左侧强调条 + 标题分隔线。"""
        panel = FancyBboxPatch(
            (0.012, box_bottom), 0.965, y_top - box_bottom,
            boxstyle="round,pad=0.012,rounding_size=0.030",
            transform=self.ax_hud.transData,
            facecolor=BG_PANEL, edgecolor=FRAME, lw=1.0,
            alpha=0.94, zorder=0)
        self.ax_hud.add_patch(panel)
        # 左侧强调条：面板身份色，一眼区分三块面板。
        self.ax_hud.add_patch(Rectangle(
            (0.012, box_bottom + 0.012), 0.008, y_top - box_bottom - 0.024,
            transform=self.ax_hud.transData, facecolor=color,
            edgecolor="none", alpha=0.85, zorder=1))
        self.ax_hud.text(0.055, y_top - 0.042, title, color=color,
                         fontsize=12.0, fontweight="bold", va="top",
                         family=MONO)
        self.ax_hud.plot([0.036, 0.946], [y_top - 0.078, y_top - 0.078],
                         color=FRAME, lw=0.8, alpha=0.9)
        for index, (key, label) in enumerate(rows):
            yy = y_top - 0.128 - index * 0.062
            self.ax_hud.text(0.055, yy, label, color=MUTED, fontsize=9.0,
                             va="center", family=MONO)
            self._hud_values[key] = self.ax_hud.text(
                0.935, yy, "-", color=TEXT, fontsize=9.2,
                ha="right", va="center", family=MONO)

    def _hud_button(self, label: str, callback, x: float, y: float,
                    width: float = 0.42, height: float = 0.055,
                    fontsize: float = 7.8) -> None:
        patch = FancyBboxPatch(
            (x, y), width, height,
            boxstyle="round,pad=0.006,rounding_size=0.018",
            transform=self.ax_hud.transData,
            facecolor="#16233a", edgecolor="#2c3d5c", lw=0.9)
        self.ax_hud.add_patch(patch)
        self.ax_hud.text(x + width / 2.0, y + height / 2.0, label,
                         color="#c9d6ea", fontsize=fontsize,
                         ha="center", va="center", family=MONO)
        self._hud_buttons.append((x, y, width, height, callback))

    def _build_hud(self) -> None:
        """极简模式：无面板/按钮/图例，状态压缩进顶部单行（status_text）。

        键位帮助保留单行小字（test_keys_help 契约：每个绑定键必须可见）。
        """
        self._hud_values = {}
        self._hud_buttons = []

        # 键位帮助行：全部交互键必须在此可见（含无按钮的 T/G/S/L）。
        self.keys_help = ("键位  Space 学习 · E 巩固 · P 剪枝 · M 回忆 · D 梦境 · "
                          "H 上下文 · Y 同步 · I 初始化 · T 时间轴回放 · "
                          "+/- 回放调速 · . 暂停 · G 快照 · S 存档 · L 读档")
        self.fig.text(0.988, 0.006, self.keys_help, color="#44506a",
                      fontsize=6.0, ha="right", va="bottom")

    def _refresh_hub(self) -> None:
        if self.hub_client is None or self._frame_no - self._hub_checked_at <= 300:
            return
        self._hub_stats = self.hub_client.stats()
        # 冲突裁决面板数据源：与 stats 同频低频轮询（300 帧 ≈10-150s，
        # 随帧率自适应；失败静默保持上次值）。getattr 防御：自定义
        # hub_client 桩可能未实现 conflicts。
        conflicts_fetch = getattr(self.hub_client, "conflicts", None)
        self._hub_conflicts = (conflicts_fetch(limit=10)
                               if callable(conflicts_fetch) else None)
        self._hub_checked_at = self._frame_no

    @staticmethod
    def _ys(count: int) -> List[float]:
        if count <= 0:
            return []
        if count == 1:
            return [0.0]
        return list(np.linspace(0.76, -0.79, count))

    @staticmethod
    def _compact_count(value: int) -> str:
        if value >= 100_000:
            return f"{value / 10_000:.1f}万"
        if value >= 10_000:
            return f"{value / 10_000:.2f}万"
        return str(value)

    @staticmethod
    def _domain_label(tag: str) -> str:
        label = DOMAIN_LABELS.get(str(tag), str(tag))
        return label[:12]

    @staticmethod
    def _memory_rank(memory) -> float:
        confidence = float(getattr(memory, "confidence", 0.5))
        utility = float(getattr(memory, "utility", 0.0))
        return (memory.weight * (0.65 + 0.035 * memory.importance)
                * (0.75 + 0.25 * confidence) * (1.0 + 0.15 * utility))

    def _memory_budget(self) -> Tuple[int, int, int]:
        # Keep room for readable domain labels instead of reserving a tall, unlabeled rail.
        fixed = len(self.mind.drives.drives) + 16 + 10
        available = max(30, self.max_nodes - fixed)
        return int(available * 0.48), int(available * 0.30), int(available * 0.22)

    def _memory_drive_affinity(self, memory) -> Dict[str, float]:
        """Return the actual retrieval-bias affinity encoded by a memory's tags."""
        affinity = self.mind.drives.cfg.get("affinity", {})
        result: Dict[str, float] = {}
        for raw_tag in getattr(memory, "tags", []):
            for drive, value in affinity.get(str(raw_tag), {}).items():
                try:
                    result[drive] = max(result.get(drive, 0.0), float(value))
                except (TypeError, ValueError):
                    continue
        return result

    def _domain_drive_affinity(self, tag: str, memories) -> Dict[str, float]:
        """Use explicit affinity first, then weak evidence from tag co-occurrence.

        The inferred portion is only a visual projection of existing tagged memories;
        it never creates a relationship when neither the tag nor its co-occurrences
        carry a configured drive affinity.
        """
        affinity = self.mind.drives.cfg.get("affinity", {})
        result = {
            drive: float(value)
            for drive, value in affinity.get(str(tag), {}).items()
            if drive in self.mind.drives.drives
        }
        evidence = Counter()
        samples = 0
        for memory in memories:
            tags = {str(value) for value in getattr(memory, "tags", [])}
            if tag not in tags:
                continue
            samples += 1
            for other_tag in tags:
                for drive, value in affinity.get(other_tag, {}).items():
                    if drive in self.mind.drives.drives:
                        evidence[drive] += max(0.0, float(value))
        if samples and evidence:
            peak = max(evidence.values())
            for drive, value in evidence.items():
                inferred = 0.12 + 0.18 * value / peak
                result[drive] = max(result.get(drive, 0.0), inferred)
        return result

    def _layout_memory_order(self, memories, domain_y: Dict[str, float]):
        """Place related memories near the same vertical band to reduce crossings."""
        def sort_key(memory):
            ys = [domain_y[str(tag)] for tag in getattr(memory, "tags", [])
                  if str(tag) in domain_y]
            target = sum(ys) / len(ys) if ys else 0.0
            stable = zlib.crc32(str(memory.id).encode("utf-8")) % 997
            return (-target, -self._memory_rank(memory), stable)

        return sorted(memories, key=sort_key)

    def _collect_nodes(self):
        self._refresh_hub()
        epi_budget, sem_budget, archive_budget = self._memory_budget()
        episodic = sorted(self.mind.episodic.values(), key=self._memory_rank, reverse=True)
        semantic = sorted(
            self.mind.semantic.values(),
            key=lambda m: m.weight * m.confidence * math.log1p(m.support_count),
            reverse=True,
        )
        archive = sorted(
            self.mind.archive.values(),
            key=lambda m: (m.archived_at or m.last_active, self._memory_rank(m)),
            reverse=True,
        )
        if self.timeline_mode:
            # 回放模式：只显示创建时间早于游标的记忆——重演「生长」过程。
            cursor = self.timeline_cursor
            episodic = [m for m in episodic if getattr(m, "created", 0.0) <= cursor]
            semantic = [m for m in semantic if getattr(m, "created", 0.0) <= cursor]
            archive = [m for m in archive if getattr(m, "created", 0.0) <= cursor]
        episodic = episodic[:epi_budget]
        semantic = semantic[:sem_budget]
        archive = archive[:archive_budget]

        tag_counts = Counter()
        for memory in episodic + semantic + archive:
            tag_counts.update(str(tag) for tag in memory.tags if str(tag).strip())
        for tag in self.mind.drives.cfg.get("affinity", {}):
            tag_counts[tag] += 1
        visible_memories = episodic + semantic + archive
        raw_domains = [tag for tag, _ in tag_counts.most_common(16)]
        drive_y = dict(zip(self.mind.drives.drives, self._ys(len(self.mind.drives.drives))))
        domain_affinities = {
            tag: self._domain_drive_affinity(tag, visible_memories)
            for tag in raw_domains
        }

        def domain_order(tag: str):
            values = domain_affinities[tag]
            weight = sum(values.values())
            target = (sum(drive_y[drive] * value for drive, value in values.items()) / weight
                      if weight else 0.0)
            return (-target, -tag_counts[tag], tag)

        domains = sorted(raw_domains, key=domain_order)
        domain_y = dict(zip(domains, self._ys(len(domains))))
        episodic = self._layout_memory_order(episodic, domain_y)
        semantic = self._layout_memory_order(semantic, domain_y)
        archive = self._layout_memory_order(archive, domain_y)

        commons = []
        if self.mind.commons is not None:
            if self._commons_stats is None or self._frame_no - self._commons_checked_at > 300:
                try:
                    self._commons_stats = self.mind.commons.stats()
                except (OSError, ValueError, sqlite3.Error):
                    self._commons_stats = None
                self._commons_checked_at = self._frame_no
            if self._commons_stats:
                commons = sorted(
                    self._commons_stats["agents"].items(),
                    key=lambda item: item[1]["messages"], reverse=True,
                )[:10]
        if self._hub_stats:
            ledger = self._hub_stats.get("stats", {}).get("ledger", {})
            hub_agents = ledger.get("agents", {}) if isinstance(ledger, dict) else {}
            merged = {name: dict(values) for name, values in commons}
            for name, count in hub_agents.items():
                values = merged.setdefault(str(name), {"messages": 0, "sessions": 0})
                values["messages"] = max(int(values.get("messages", 0)), int(count))
            commons = sorted(merged.items(), key=lambda item: item[1]["messages"], reverse=True)[:10]

        return {
            "drive": [(name, self.mind.drives.values.get(name, 0.0))
                      for name in self.mind.drives.drives],
            "domain": [(tag, tag_counts[tag]) for tag in domains],
            "episodic": episodic,
            "semantic": semantic,
            "archive": archive,
            "commons": commons,
        }

    def _key(self, kind: str, item) -> str:
        if kind in ("episodic", "semantic", "archive"):
            return f"memory:{item.id}"
        if kind == "commons":
            return f"commons:{item[0]}"
        return f"{kind}:{item[0]}"

    def _node_value(self, kind: str, item) -> float:
        if kind == "drive":
            return float(item[1])
        if kind == "domain":
            return min(1.0, math.log1p(item[1]) / math.log(12.0))
        if kind == "commons":
            return min(1.0, math.log1p(item[1]["messages"]) / math.log(120_000.0))
        if kind == "semantic":
            return max(0.0, min(1.0, 0.6 * item.weight + 0.4 * item.confidence))
        return max(0.0, min(1.0, item.weight))

    def _face_color(self, kind: str, item, value: float):
        if kind == "episodic":
            return VALENCE_CMAP((item.valence + 1.0) / 2.0)
        color = mcolors.to_rgb(self.KIND_STYLE[kind][0])
        blend = 0.38 + 0.62 * value
        return tuple(channel * blend for channel in color) + (1.0,)

    def _curve_points(self, a: str, b: str) -> List[Tuple[float, float]]:
        """用稳定的二次贝塞尔曲线连接两个真实节点。"""
        start = np.asarray(self._positions[a], dtype=float)
        end = np.asarray(self._positions[b], dtype=float)
        delta = end - start
        length = float(np.linalg.norm(delta))
        if length <= 1e-9 or self.curve_strength <= 0.0:
            return [tuple(start), tuple(end)]

        normal = np.asarray([-delta[1], delta[0]], dtype=float) / length
        stable = zlib.crc32(f"{a}|{b}".encode("utf-8"))
        midpoint = (start + end) * 0.5
        # Alternating deterministic lanes prevent every upper/lower edge from
        # bending into the same fan-shaped bundle.
        direction = 1.0 if stable & 1 else -1.0
        jitter = ((stable >> 1) % 997) / 996.0
        distance_scale = min(1.45, max(0.50, length))
        offset = self.curve_strength * math.sqrt(distance_scale) * (0.80 + 0.35 * jitter)
        control = midpoint + direction * offset * normal
        self._max_curve_deviation = max(self._max_curve_deviation, offset * 0.5)

        t = np.linspace(0.0, 1.0, self.curve_steps)[:, None]
        points = ((1.0 - t) ** 2 * start
                  + 2.0 * (1.0 - t) * t * control
                  + t ** 2 * end)
        return [tuple(point) for point in points]

    def _memory_agents(self, memory) -> set:
        """某条记忆关联的 Agent 名集合（agent:* 标签 + 来源元数据）。"""
        agents = {str(tag)[6:] for tag in getattr(memory, "tags", [])
                  if str(tag).startswith("agent:")}
        metadata = getattr(memory, "metadata", {}) or {}
        source = str(metadata.get("source_agent", "")).strip() if isinstance(metadata, dict) else ""
        if source:
            agents.add(source)
        return agents

    def _entity_subjects(self, *, ttl: float = 60.0) -> Dict[str, Dict[str, int]]:
        """账本 claim 主语索引：subject -> {claims, conflicts}（TTL 缓存，有界）。"""
        if self._ledger is None:
            return {}
        now = _time.monotonic()
        if self._entity_index and now - self._entity_index_at < ttl:
            return self._entity_index
        try:
            rows = self._ledger.recent_subjects(limit=400)
        except Exception:
            return self._entity_index
        index: Dict[str, Dict[str, int]] = {}
        for row in rows:
            subject = str(row.get("subject", "")).strip()
            if not subject:
                continue
            entry = index.setdefault(subject, {"claims": 0, "conflicts": 0})
            entry["claims"] += int(row.get("claims", 0) or 0)
            entry["conflicts"] += int(row.get("conflicts", 0) or 0)
        if index:
            self._entity_index = index
            self._entity_index_at = now
        return self._entity_index

    def _entity_neighborhood(self, memory) -> Dict[str, any]:
        """选中记忆的实体邻域：文本命中的主语 + 共现该主语的其他记忆。"""
        subjects = self._entity_subjects()
        if not subjects or memory is None:
            return {}
        text = (getattr(memory, "situation", "")
                or getattr(memory, "rule", "") or "")
        if not text:
            return {}
        hits = {s: meta for s, meta in subjects.items() if s in text}
        if not hits:
            return {}
        peers: Dict[int, set] = {}
        for other in (list(self.mind.episodic.values())
                      + list(self.mind.semantic.values())
                      + list(self.mind.archive.values())):
            other_text = (getattr(other, "situation", "")
                          or getattr(other, "rule", "") or "")
            if not other_text or other is memory:
                continue
            shared = {s for s in hits if s in other_text}
            if shared:
                peers[other.id] = shared
        return {"subjects": hits, "peers": peers}

    def _focus_agent(self) -> Optional[str]:
        if self.selected_key and self.selected_key.startswith("commons:"):
            return self.selected_key.split(":", 1)[1]
        return None

    def _edge_data(self, nodes) -> Tuple[List, List, List]:
        candidates = defaultdict(list)
        seen = set()

        def add(kind: str, a: str, b: str, strength: float, color: str):
            if a not in self._positions or b not in self._positions:
                return
            try:
                strength = max(0.0, min(1.0, float(strength)))
            except (TypeError, ValueError):
                return
            key = (kind, a, b)
            if key in seen:
                return
            seen.add(key)
            candidates[kind].append((a, b, strength, color))

        visible_memories = nodes["episodic"] + nodes["semantic"] + nodes["archive"]
        visible_domains = {tag for tag, _ in nodes["domain"]}

        # Explicit drive affinities are supplemented only by observed tag co-occurrence.
        for tag, _ in nodes["domain"]:
            domain_key = f"domain:{tag}"
            for drive, strength in self._domain_drive_affinity(tag, visible_memories).items():
                add("drive_domain", f"drive:{drive}", domain_key, strength, CYAN)

        # Mind.recall applies drive bias to every tagged memory. Showing the strongest
        # few links makes the desire -> memory half of the feedback loop visible.
        drive_memory = defaultdict(list)
        for memory in visible_memories:
            value = self._node_value(
                "semantic" if isinstance(memory, SemanticMemory) else "episodic", memory)
            for drive, strength in self._memory_drive_affinity(memory).items():
                score = strength * (0.68 + 0.32 * value)
                drive_memory[drive].append((score, memory, strength))
        for drive, entries in drive_memory.items():
            for _score, memory, strength in sorted(entries, reverse=True,
                                                    key=lambda item: item[0])[:14]:
                add("drive_memory", f"drive:{drive}", f"memory:{memory.id}",
                    strength, "#4f9e9a")

        for memory in visible_memories:
            memory_key = f"memory:{memory.id}"
            for tag in memory.tags:
                if str(tag) in visible_domains:
                    add("domain_memory", f"domain:{tag}", memory_key,
                        0.42 + 0.04 * memory.importance, "#557b78")

        for association in self.mind.assoc.values():
            add("association", f"memory:{association.a}", f"memory:{association.b}",
                association.strength, "#5e7694")

        # 相似度共振边：语义规则 ↔ 最相似的活跃情景（top-6，特征/文本相似），
        # 冷归档 ↔ 活跃情景（top-2），同层情景高相似互连（top-1 互惠）。
        # 织出层间密集但真实的关联网——每条边都有可解释的相似度依据，
        # 不用随机连线制造复杂感。
        hippo = getattr(self.mind, "hippo", self.mind)
        similarity = getattr(hippo, "_memory_similarity", None)
        if callable(similarity):
            for rule in nodes["semantic"]:
                rule_key = f"memory:{rule.id}"
                scored = []
                for memory in nodes["episodic"]:
                    if memory.id in rule.source_episodes:
                        continue  # 已有 source 巩固边
                    try:
                        sim = float(similarity(rule, memory))
                    except (TypeError, ValueError):
                        continue
                    if sim >= 0.08:
                        scored.append((sim, memory.id))
                for sim, mid in sorted(scored, reverse=True)[:6]:
                    add("resonance", f"memory:{mid}", rule_key, sim, "#7e8fb5")
            for cold in nodes["archive"]:
                cold_key = f"memory:{cold.id}"
                scored = []
                for memory in nodes["episodic"][:60]:
                    try:
                        sim = float(similarity(cold, memory))
                    except (TypeError, ValueError):
                        continue
                    if sim >= 0.15:
                        scored.append((sim, memory.id))
                for sim, mid in sorted(scored, reverse=True)[:2]:
                    add("resonance", f"memory:{mid}", cold_key, sim * 0.7, "#5a6478")
            # 同层互连：情景记忆之间的高相似对（sim≥0.25，每节点取最强 1 条，
            # 以 (低id,高id) 去重避免重复边）。让活跃层内部也有真实结构。
            linked: set = set()
            for i, a_mem in enumerate(nodes["episodic"]):
                best_sim, best_id = 0.0, None
                for b_mem in nodes["episodic"]:
                    if b_mem.id <= a_mem.id or (a_mem.id, b_mem.id) in linked:
                        continue
                    try:
                        sim = float(similarity(a_mem, b_mem))
                    except (TypeError, ValueError):
                        continue
                    if sim > best_sim:
                        best_sim, best_id = sim, b_mem.id
                if best_id is not None and best_sim >= 0.25:
                    linked.add((a_mem.id, best_id))
                    add("association", f"memory:{a_mem.id}", f"memory:{best_id}",
                        best_sim * 0.8, "#5e7694")

        for semantic in nodes["semantic"]:
            for source_id in semantic.source_episodes:
                add("source", f"memory:{source_id}", f"memory:{semantic.id}",
                    semantic.confidence, AMBER)

        commons_agents = {agent for agent, _ in nodes["commons"]}
        for memory in visible_memories:
            agent_names = {
                str(tag)[6:] for tag in memory.tags
                if str(tag).startswith("agent:")
            }
            source_agent = str(memory.metadata.get("source_agent", "")).strip()
            if source_agent:
                agent_names.add(source_agent)
            for agent_name in agent_names & commons_agents:
                add("commons", f"memory:{memory.id}", f"commons:{agent_name}",
                    0.75, VIOLET)

        selected = []
        remainder = []
        focus_agent = self._focus_agent()
        for kind, share in EDGE_BUDGET.items():
            entries = sorted(candidates[kind], key=lambda item: item[2], reverse=True)
            limit = max(1, int(self.max_edges * share))
            selected.extend((kind, *entry) for entry in entries[:limit])
            remainder.extend((kind, *entry) for entry in entries[limit:])
        if len(selected) < self.max_edges:
            remainder.sort(key=lambda item: item[3], reverse=True)
            selected.extend(remainder[:self.max_edges - len(selected)])

        # 每节点度数上限：按强度从高到低保留，超限边丢弃。
        # 只约束 memory:/domain: 端点（驱力与共通栏是设计上的枢纽，
        # 不设限），否则中心层会织成不可读的毛球。
        max_degree = {
            "domain_memory": 4,
            "association": 1,
            "resonance": 2,
            "source": 1,
            "drive_memory": 3,
            "commons": 2,
        }
        degree: Counter = Counter()
        capped: List[Tuple[str, str, str, float, str]] = []
        for kind, a, b, strength, color in sorted(
                selected, key=lambda item: item[3], reverse=True):
            limit_degree = max_degree.get(kind)
            if limit_degree is not None:
                hot = [ep for ep in (a, b) if ep.startswith(("memory:", "domain:"))]
                if any(degree[ep] >= limit_degree for ep in hot):
                    continue
                for ep in hot:
                    degree[ep] += 1
            capped.append((kind, a, b, strength, color))
        selected = capped

        segments, colors, widths = [], [], []
        self._edge_type_counts = Counter()
        selected_obj = (self._objects.get(self.selected_key)
                        if self.selected_key else None)
        neighborhood = (self._entity_neighborhood(selected_obj)
                        if selected_obj is not None
                        and self.selected_key.startswith("memory:") else {})
        entity_peers = set(neighborhood.get("peers", {}))
        for kind, a, b, strength, color in selected[:self.max_edges]:
            base, span = EDGE_ALPHA[kind]
            alpha = base + span * strength
            selected_edge = self.selected_key in (a, b)
            recalled_edge = a in self.highlight or b in self.highlight
            recent_edge = a in self.recent or b in self.recent
            if selected_edge:
                alpha = max(alpha, 0.56)
                color = ACTIVE
            elif recalled_edge:
                alpha = min(0.38, alpha + 0.15)
                if a in self.highlight and b in self.highlight:
                    color = ACTIVE
            elif recent_edge:
                alpha = min(0.26, alpha + 0.045)
            if focus_agent:
                # 聚焦 Agent：其来源记忆参与的边整体提亮，织出该 Agent 的证据网
                for endpoint in (a, b):
                    obj = self._objects.get(endpoint)
                    if obj is not None and focus_agent in self._memory_agents(obj):
                        alpha = min(0.34, alpha + 0.06)
                        break
            if entity_peers and selected_obj is not None:
                # 实体图层：与选中记忆共享账本主语的边提亮（青色）
                for endpoint in (a, b):
                    obj = self._objects.get(endpoint)
                    if obj is not None and int(getattr(obj, "id", -1)) in entity_peers:
                        alpha = min(0.40, alpha + 0.10)
                        color = "#2aa8a8"
                        break
            segments.append(self._curve_points(a, b))
            colors.append(mcolors.to_rgba(color, alpha))
            # 宽度随强度：强边醒目、弱边退为背景纹理（等宽是"毛球感"主因）。
            widths.append(0.32 + 1.05 * strength)
            self._edge_type_counts[kind] += 1
        self._edge_width_range = ((min(widths), max(widths)) if widths else (0.0, 0.0))
        return segments, colors, widths

    def rebuild(self) -> None:
        self._rebuild_count += 1
        view = (self.ax.get_xlim(), self.ax.get_ylim()) if self._view_dirty else None
        self.ax.clear()
        self.ax.set_facecolor(BG)
        self.ax.axis("off")
        self.ax.set_xlim(*(view[0] if view else (-0.18, 6.10)))
        self.ax.set_ylim(*(view[1] if view else (-0.93, 0.93)))
        self._positions.clear()
        self._objects.clear()
        self._collection_keys.clear()
        self._max_curve_deviation = 0.0

        nodes = self._collect_nodes()
        self._visible_counts = Counter({kind: len(items) for kind, items in nodes.items()})

        # 层背景带 + 层标题导航条：垂直微色带区分层归属，标题行
        # 「色点 + 名称 + 计数」右对齐，形成干净的导航栏节奏。
        layer_accent = {
            "drive": CYAN, "domain": "#2dd4bf", "episodic": "#94a3b8",
            "semantic": BLUE, "archive": "#64748b", "commons": VIOLET,
        }
        # 垂直渐变底：顶部略亮的深空色，营造深度而非死黑。
        grad = np.linspace(0.0, 1.0, 256).reshape(-1, 1)
        self.ax.imshow(grad, extent=[-0.18, 6.10, -0.93, 0.93],
                       aspect="auto", origin="lower", zorder=-1,
                       cmap=mcolors.LinearSegmentedColormap.from_list(
                           "bg_grad", ["#04060b", "#0a1120"]))
        for index, (kind, title, x) in enumerate(self.LAYERS):
            accent = layer_accent.get(kind, "#5e7694")
            self.ax.axvspan(x - 0.44, x + 0.44, color=accent, alpha=0.020,
                            zorder=0.2, lw=0)
            self.ax.scatter([x - 0.335], [0.906], s=34, color=accent,
                            zorder=1.2, edgecolors="none")
            # 名称截 4 字，计数右对齐到带缘并缩小——长名不再顶到计数。
            self.ax.text(x - 0.27, 0.906, title[:4], color="#c7d2e2",
                         fontsize=8.0, fontweight="bold", ha="left", va="center")
            total = {
                "episodic": len(self.mind.episodic),
                "semantic": len(self.mind.semantic),
                "archive": len(self.mind.archive),
            }.get(kind, len(nodes[kind]))
            count_label = (f"{len(nodes[kind])}/{total}"
                           if kind in ("episodic", "semantic", "archive") else str(total))
            self.ax.text(x + 0.46, 0.906, count_label, color=accent,
                         fontsize=6.5, ha="right", va="center", alpha=0.9)
            self.ax.plot([x - 0.42, x + 0.42], [0.872, 0.872],
                         color=accent, lw=0.8, alpha=0.18, zorder=1,
                         solid_capstyle="round")
            for y, item in zip(self._ys(len(nodes[kind])), nodes[kind]):
                key = self._key(kind, item)
                node_x = x
                if kind in ("episodic", "semantic", "archive") and len(nodes[kind]) > 18:
                    lane = (zlib.crc32(key.encode("utf-8")) % 5) - 2
                    node_x += lane * 0.012
                self._positions[key] = (node_x, y)
                self._objects[key] = item

        segments, edge_colors, edge_widths = self._edge_data(nodes)
        self._curve_segments = segments
        self._edge_count = len(segments)
        # 辉光底层：宽 2.2 倍、低透明度的柔光晕开，配合上层细线形成
        # "发光丝线"质感；边数已受度数上限约束，不会糊成毛球。
        if segments:
            glow = LineCollection(segments, colors=edge_colors,
                                  linewidths=[max(1.2, w * 2.2) for w in edge_widths],
                                  zorder=0.7, antialiased=True)
            glow.set_alpha(0.12)
            self.ax.add_collection(glow)
        edges = LineCollection(segments, colors=edge_colors, linewidths=edge_widths,
                               zorder=1, antialiased=True)
        self.ax.add_collection(edges)

        for kind, _, _ in self.LAYERS:
            keys = [key for key in self._positions if key.startswith(f"{kind}:")]
            if kind in ("episodic", "semantic", "archive"):
                keys = [f"memory:{item.id}" for item in nodes[kind]]
            if not keys:
                continue
            items = [self._objects[key] for key in keys]
            values = [self._node_value(kind, item) for item in items]
            base_color, marker, base_size = self.KIND_STYLE[kind]
            positions = [self._positions[key] for key in keys]
            sizes = [base_size * (0.84 + 0.34 * value) for value in values]
            faces = [self._face_color(kind, item, value)
                     for item, value in zip(items, values)]
            focus_agent = self._focus_agent()
            borders = []
            widths = []
            for key, item in zip(keys, items):
                if key == self.selected_key:
                    borders.append(ACTIVE)
                    widths.append(1.15)
                elif key in self.highlight:
                    borders.append(AMBER)
                    widths.append(0.85)
                elif (focus_agent is not None
                      and kind in ("episodic", "semantic", "archive")
                      and focus_agent in self._memory_agents(item)):
                    # 聚焦 Agent 的来源记忆：紫罗兰描边 + 加粗，一眼圈出归属
                    borders.append(VIOLET)
                    widths.append(1.05)
                elif key in self.recent:
                    borders.append(CYAN)
                    widths.append(0.65)
                else:
                    borders.append("#0b0e15")
                    widths.append(0.45)
            # 三层节点渲染：大柔光晕（氛围）→ 主体 → 高值亮芯，形成发光体。
            self.ax.scatter(
                [p[0] for p in positions], [p[1] for p in positions],
                s=[sz * 5.0 for sz in sizes], c=faces,
                alpha=0.13, linewidths=0, zorder=2,
            )
            collection = self.ax.scatter(
                [p[0] for p in positions], [p[1] for p in positions], s=sizes,
                c=faces, marker=marker, edgecolors=borders, linewidths=widths,
                zorder=3, picker=6,
            )
            self._collection_keys[id(collection)] = keys

        recalled_positions = [self._positions[key] for key in self.highlight
                              if key in self._positions and key != self.selected_key]
        if recalled_positions:
            # 高亮辉光：大而柔的光环 + 细亮环双层
            self.ax.scatter(
                [p[0] for p in recalled_positions], [p[1] for p in recalled_positions],
                s=300, facecolors="none", edgecolors=AMBER, linewidths=2.2,
                alpha=0.16, zorder=4,
            )
            self.ax.scatter(
                [p[0] for p in recalled_positions], [p[1] for p in recalled_positions],
                s=74, facecolors="none", edgecolors=AMBER, linewidths=0.8,
                alpha=0.65, zorder=4,
            )
        if self.selected_key in self._positions:
            x, y = self._positions[self.selected_key]
            self.ax.scatter([x], [y], s=340, facecolors="none", edgecolors=ACTIVE,
                            linewidths=2.6, alpha=0.18, zorder=5)
            self.ax.scatter([x], [y], s=108, facecolors="none", edgecolors=ACTIVE,
                            linewidths=1.15, alpha=0.9, zorder=5)

        # 极简模式：节点旁文字标签全部去除（驱力值/域计数/共通名），
        # 结构信息由层头承载；点击节点可在详情行查看数值。

        self._update_hud()
        self._update_detail()
        self.fig.canvas.draw_idle()

    def _update_hud(self) -> None:
        """极简状态行：学习态 · 代数 · 节点/边 · 冲突 · 账本，单行小字。"""
        stats = self.mind.stats_summary()
        parts = [self.learning_on and "LEARNING" or "PAUSED",
                 f"Epoch {int(stats.get('consolidated', 0) or 0)}",
                 f"节点 {sum(self._visible_counts.values())}",
                 f"连接 {self._edge_count}"]
        conflicts = self._hub_conflicts if isinstance(self._hub_conflicts, dict) else None
        if conflicts is not None:
            total = int(conflicts.get("total", 0) or 0)
            parts.append("冲突 0" if not total else f"冲突 {total}")
        if self._hub_stats:
            ledger = self._hub_stats.get("stats", {}).get("ledger", {})
            if isinstance(ledger, dict) and ledger.get("memories") is not None:
                parts.append(f"账本 {ledger.get('memories', 0)}")
        if self.status_text is not None:
            self.status_text.set_text(" · ".join(str(p) for p in parts if p))

    def _activation_breakdown(self, item) -> str:
        """单条记忆的激活分量分解（与检索评分同源的透明度面板）。"""
        import math as _math
        now = self.mind.time
        age_active = max(0.0, now - getattr(item, "last_active", 0.0))
        recency = 1.0 / (1.0 + age_active / 60.0)
        touch = (min(1.0, _math.log1p(max(0, getattr(item, "touch_count", 0)))
                     / _math.log(11.0)))
        utility = float(getattr(item, "utility", 0.0))
        importance = int(getattr(item, "importance", 5))
        confidence = float(getattr(item, "confidence", 0.5))
        accesses = int(getattr(item, "access_count", 0))
        return (f"激活 [强度{item.weight:.2f} 新近{recency:.2f} 触碰{touch:.2f} "
                f"效用{utility:+.2f} 重要{importance} 置信{confidence:.2f} "
                f"回忆{accesses}次]")

    def _selected_description(self) -> str:
        key = self.selected_key
        item = self._objects.get(key) if key else None
        if item is None:
            return "当前选择：无"
        kind = key.split(":", 1)[0]
        if key.startswith("memory:"):
            neighborhood = self._entity_neighborhood(item)
            entity_line = ""
            if neighborhood:
                parts = []
                for subject, meta in sorted(neighborhood["subjects"].items())[:3]:
                    flag = f" · 冲突{meta['conflicts']}" if meta["conflicts"] else ""
                    parts.append(f"{subject}({meta['claims']}条{flag})")
                peer_n = len(neighborhood["peers"])
                entity_line = (f"\n实体邻域：{' · '.join(parts)}"
                               f" · 共现记忆 {peer_n} 条（青色边）")
            breakdown = self._activation_breakdown(item)
            if isinstance(item, SemanticMemory):
                text = item.rule
                return (f"当前选择 #{item.id} · 语义规则 · 权重 {item.weight:.3f} · "
                        f"置信度 {item.confidence:.2f} · 证据数 {item.support_count} · "
                        f"{text[:76]}\n{breakdown}{entity_line}")
            status = "冷归档" if item.id in self.mind.archive else "活跃情景"
            return (f"当前选择 #{item.id} · {status} · 权重 {item.weight:.3f} · "
                    f"效用 {item.utility:+.2f} · 触碰 {item.touch_count} · "
                    f"{item.situation[:76]}\n{breakdown}{entity_line}")
        if kind == "drive":
            return f"当前选择：驱力 · {DRIVE_LABELS.get(item[0], item[0])} · 强度 {item[1]:.3f}"
        if kind == "domain":
            return f"当前选择：记忆域 · {item[0]} · 关联记忆 {item[1]}"
        if kind == "commons":
            focus_count = sum(
                1 for memory in
                list(self.mind.episodic.values()) + list(self.mind.semantic.values())
                + list(self.mind.archive.values())
                if item[0] in self._memory_agents(memory))
            return (f"聚焦智能体 {item[0]} · 关联记忆 {focus_count} 条（紫色描边）· "
                    f"消息 {item[1]['messages']} · 会话 {item[1]['sessions']}")
        return f"当前选择：{key}"

    def _update_detail(self) -> None:
        description = self._selected_description()
        if self.timeline_mode:
            progress = ((self.timeline_cursor - self._timeline_t0)
                        / max(1e-6, self._timeline_span))
            description = f"[时间轴回放 {min(100, int(progress * 100)):3d}%]  " + description
        self.detail_text.set_text(description)
        self.context_text.set_text(("Context Pack：" + self._hub_context)[:96] if self._hub_context else "")

    def frame(self) -> None:
        self._frame_no += 1
        if _time.monotonic() >= self._highlight_until:
            self.highlight.clear()
        self.recent = {
            f"memory:{memory.id}" for memory in self.mind.all_memories()
            if self.mind.time - memory.last_access <= 1.0
        }
        if self.timeline_mode:
            self._advance_timeline()
            # 回放期间可见集逐帧变化，需要更频繁的拓扑重建；
            # 这是显式的交互性取舍，退出回放即恢复低频契约。
            if self._frame_no == 1 or self._frame_no % 3 == 0:
                self.rebuild()
            elif self._frame_no % 9 == 0:
                self._update_hud()
                self._update_detail()
                self.fig.canvas.draw_idle()
        elif self._frame_no == 1 or self._frame_no % self.topology_refresh_frames == 0:
            self.rebuild()
        elif self._frame_no % 3 == 0:
            self._update_hud()
            self._update_detail()
            self.fig.canvas.draw_idle()
        now = _time.perf_counter()
        instant = 1.0 / max(1e-6, now - self._last_frame)
        self.fps = instant if self.fps <= 0 else 0.9 * self.fps + 0.1 * instant
        self._last_frame = now

    def _enable_query_box(self) -> None:
        """创建查询输入框（需交互式后端；Agg 下静默跳过）。"""
        if self.query_box is not None:
            return
        try:
            from matplotlib.widgets import TextBox
            self.query_box = TextBox(
                self.ax_query, "检索▶ ", initial="",
                color=BG, hovercolor="#0b101b",
                label_pad=0.002)
            for t in (self.query_box.label, self.query_box.text_disp):
                try:
                    t.set_color(TEXT)
                    t.set_family(MONO)
                except AttributeError:
                    pass
            self.query_box.on_submit(self._run_gui_query)
        except Exception:
            self.query_box = None

    def _run_gui_query(self, query: str) -> None:
        """GUI 内执行 Context Pack：优先 hub，失败回退本地账本。"""
        query = str(query or "").strip()
        if not query:
            return
        result = None
        if self.hub_client is not None:
            try:
                result = self.hub_client.context_pack(query)
            except Exception:
                result = None
        if result is None:
            # 无 hub 时提示走 CLI（本地直查需要账本路径，GUI 演示库不含）
            self.last_action = ("查询需要 hub 连接或用 CLI: hippomind context "
                                f"'{query[:40]}'")
            self.rebuild()
            return
        items = result.get("items", []) if isinstance(result, dict) else []
        texts = []
        for item in items[:6]:
            content = str(item.get("content", ""))[:80]
            score = item.get("score", 0)
            texts.append(f"· [{score:.3f}] {content}")
        self._hub_context = " | ".join(texts) if texts else "(无命中)"
        self.last_action = f"GUI 查询「{query[:30]}」→ {len(items)} 条"
        self.rebuild()

    def do_snapshot(self, out_dir: str = "output") -> Optional[Path]:
        """G 键：把当前帧保存为 PNG 快照，返回文件路径。"""
        from pathlib import Path as _Path
        directory = _Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        seed_part = f"-seed{self.mind.seed}" if getattr(self.mind, "seed", None) is not None else ""
        node_count = sum(self._visible_counts.values()) if getattr(self, "_visible_counts", None) else 0
        path = directory / f"hippomind-network-{stamp}{seed_part}-n{node_count}.png"
        self.fig.savefig(path, dpi=100)
        self.last_action = f"快照已保存 {path.name}"
        self.rebuild()
        return path

    def export_timeline_gif(self, out_dir: str = "output", frames: int = 24,
                            fps: int = 6) -> Optional[Path]:
        """时间轴模式逐帧导出动画 GIF（需要 Pillow；非 timeline 模式先进入）。"""
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.last_action = "GIF 导出需要 Pillow（pip install pillow）"
            return None
        if not self.timeline_mode:
            self.toggle_timeline()
            if not self.timeline_mode:
                return None
        frames = max(2, min(96, int(frames)))
        fps = max(1, min(24, int(fps)))
        from pathlib import Path as _Path
        directory = _Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        span_t0, span_t1 = self._timeline_t0, self._timeline_t1
        saved_cursor = self.timeline_cursor
        pil_frames = []
        try:
            for i in range(frames):
                self.timeline_cursor = span_t0 + (span_t1 - span_t0) * i / (frames - 1)
                self.rebuild()
                self.fig.canvas.draw()
                buf = bytes(self.fig.canvas.buffer_rgba())
                w, h = self.fig.canvas.get_width_height()
                pil_frames.append(Image.frombytes("RGBA", (w, h), buf)
                                  .convert("P", palette=Image.WEB,
                                           colors=128))
        finally:
            self.timeline_cursor = saved_cursor
            self.rebuild()
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        path = directory / f"hippomind-timeline-{stamp}.gif"
        pil_frames[0].save(path, save_all=True, append_images=pil_frames[1:],
                           duration=int(1000 / fps), loop=0)
        self.last_action = f"GIF 已保存 {path.name}（{frames} 帧）"
        return path

    def toggle_timeline(self) -> None:
        """T 键：进入/退出时间轴回放。进入时从最早记忆开始自动播放。"""
        if not self.timeline_mode:
            times = [float(getattr(m, "created", 0.0))
                     for m in self.mind.all_memories()]
            times = [t for t in times if t > 0]
            if len(times) < 2:
                self.last_action = "时间轴：需要至少两条带创建时间的记忆"
                self.rebuild()
                return
            self._timeline_t0 = min(times)
            self._timeline_t1 = max(times)
            self._timeline_span = max(1e-6, self._timeline_t1 - self._timeline_t0)
            self.timeline_mode = True
            self.timeline_playing = True
            self.timeline_cursor = self._timeline_t0
            self.last_action = "时间轴回放开始 · 再按 T 退出"
        else:
            self.timeline_mode = False
            self.timeline_playing = False
            self.last_action = "时间轴回放结束"
        self.rebuild()

    def _advance_timeline(self) -> None:
        """每帧推进回放游标；约 10 秒走完全程，到末尾暂停。"""
        if not (self.timeline_mode and self.timeline_playing):
            return
        step = self._timeline_span / 300.0 * max(0.25, min(8.0, self.timeline_speed))
        self.timeline_cursor = min(self._timeline_t1,
                                   self.timeline_cursor + step)
        if self.timeline_cursor >= self._timeline_t1:
            self.timeline_playing = False
            self.last_action = "时间轴回放完成 · T 退出"

    def toggle_learning(self) -> None:
        self.learning_on = not self.learning_on
        self.mind.learning_enabled = self.learning_on
        self.mind.hippo.learning_enabled = self.learning_on
        self.rebuild()

    def do_consolidate(self) -> None:
        self.mind.consolidate()
        self.rebuild()

    def do_prune(self) -> None:
        self.mind.prune()
        self.rebuild()

    def do_recall(self) -> None:
        features, description = self.env.observe()
        hits = self.mind.recall(query=description, features=features, k=8,
                                emotion=self.agent._emotion(features))
        self.highlight = {f"memory:{memory.id}" for _, memory in hits}
        self._highlight_until = _time.monotonic() + 3.0
        self.rebuild()

    def do_dream(self) -> None:
        self.mind.dream()
        self.rebuild()

    def do_context_pack(self) -> None:
        """Fetch a bounded, evidence-first Context Pack without blocking every frame."""
        if self.hub_client is None:
            self.last_action = "Context Pack：未连接 daemon"
            self.rebuild()
            return
        _features, description = self.env.observe()
        pack = self.hub_client.context_pack(description)
        if not pack:
            self.last_action = "Context Pack：无可用 daemon"
            self._hub_context = ""
        else:
            self._hub_context = str(pack.get("text", "未找到相关记忆")).replace("\n", " · ")
            self.last_action = "Context Pack 已刷新"
        self.rebuild()

    def _initialize_commons(self):
        if self.mind.commons is None:
            self.mind.attach_commons()
        self.mind.commons.initialize()
        self._commons_stats = None
        self._commons_checked_at = -1000
        return self.mind.commons

    def do_initialize(self) -> None:
        """初始化共享账本；仅在本地为空时生成演示经历，不清空现有记忆。"""
        try:
            commons = self._initialize_commons()
            generated = 0
            if not self.mind.all_memories():
                for _ in range(240):
                    self.agent.step()
                    generated += 1
            stats = commons.stats()
            self.last_action = f"初始化完成 · 共享 {stats['shared_memories']} · 生成 {generated} 步"
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            self.last_action = f"初始化失败 · {type(exc).__name__}"
        self.rebuild()

    def do_sync(self) -> None:
        """从共通账本幂等提升最近规则；原始聊天仍保持按需只读召回。"""
        try:
            commons = self._initialize_commons()
            known = {
                memory.metadata.get("commons_id")
                for memory in list(self.mind.episodic.values()) + list(self.mind.archive.values())
                if memory.metadata.get("commons_id")
            }
            imported = 0
            for hit in commons.recent_shared(limit=12):
                self.mind.promote_common(hit, importance=hit.memory.importance)
                if hit.memory.id not in known:
                    imported += 1
                    known.add(hit.memory.id)
            stats = commons.stats()
            self._commons_stats = stats
            self.last_action = f"同步 +{imported} · 共享 {stats['shared_memories']}"
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            self.last_action = f"同步失败 · {type(exc).__name__}"
        self.rebuild()

    def _on_key(self, event) -> None:
        key = (event.key or "").lower()
        if key == " ":
            self.toggle_learning()
        elif key == "e":
            self.do_consolidate()
        elif key == "p":
            self.do_prune()
        elif key == "t":
            self.toggle_timeline()
        elif key in ("+", "="):
            if self.timeline_mode:
                self.timeline_speed = min(8.0, self.timeline_speed * 1.5)
                self.last_action = f"回放速度 {self.timeline_speed:.2f}x"
        elif key in ("-", "_"):
            if self.timeline_mode:
                self.timeline_speed = max(0.25, self.timeline_speed / 1.5)
                self.last_action = f"回放速度 {self.timeline_speed:.2f}x"
        elif key == ".":
            if self.timeline_mode:
                self.timeline_playing = not self.timeline_playing
                state = "继续" if self.timeline_playing else "暂停"
                self.last_action = f"时间轴{state}"
        elif key == "g":
            self.do_snapshot()
        elif key == "m":
            self.do_recall()
        elif key == "d":
            self.do_dream()
        elif key == "h":
            self.do_context_pack()
        elif key == "y":
            self.do_sync()
        elif key == "i":
            self.do_initialize()
        elif key == "s":
            self.mind.save("hippocampus_memory.json")
        elif key == "l":
            try:
                self.mind.load("hippocampus_memory.json")
            except (OSError, ValueError):
                return
            self.rebuild()

    def _on_pick(self, event) -> None:
        keys = self._collection_keys.get(id(event.artist), [])
        if not keys or len(event.ind) == 0:
            return
        index = int(event.ind[0])
        if index >= len(keys):
            return
        self.selected_key = keys[index]
        self.rebuild()

    def _on_press(self, event) -> None:
        if event.inaxes == self.ax_hud and event.xdata is not None and event.ydata is not None:
            for x, y, width, height, callback in self._hud_buttons:
                if x <= event.xdata <= x + width and y <= event.ydata <= y + height:
                    callback()
                    return
        if event.inaxes != self.ax:
            return
        if event.button == 3:
            self.selected_key = None
            self.highlight.clear()
            self.recent.clear()
            self.rebuild()
            return
        if event.dblclick:
            self._view_dirty = False
            self.rebuild()
            return
        if event.button == 1 and event.xdata is not None and event.ydata is not None:
            self._press = (event.xdata, event.ydata, self.ax.get_xlim(), self.ax.get_ylim())

    def _on_release(self, _event) -> None:
        self._press = None

    def _on_motion(self, event) -> None:
        if self._press is None or event.inaxes != self.ax:
            return
        if event.xdata is None or event.ydata is None:
            return
        x0, y0, xlim, ylim = self._press
        dx, dy = event.xdata - x0, event.ydata - y0
        self.ax.set_xlim(xlim[0] - dx, xlim[1] - dx)
        self.ax.set_ylim(ylim[0] - dy, ylim[1] - dy)
        self._view_dirty = True
        self.fig.canvas.draw_idle()

    def _on_scroll(self, event) -> None:
        if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
            return
        scale = 0.82 if event.button == "up" else 1.22
        xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        x, y = event.xdata, event.ydata
        self.ax.set_xlim(x - (x - xlim[0]) * scale, x + (xlim[1] - x) * scale)
        self.ax.set_ylim(y - (y - ylim[0]) * scale, y + (ylim[1] - y) * scale)
        self._view_dirty = True
        self.fig.canvas.draw_idle()
