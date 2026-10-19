"""HippoMind GUI 共享主题：统一深色视觉语言、排版与通用小组件。

两个 matplotlib 界面（visualizer / network_visualizer）共用同一套色板与
样式助手，保证"记忆中枢"在全屏层状网络和经典工作台之间切换时观感一致。
"""

from __future__ import annotations

import matplotlib as mpl

# 中文字体回退（macOS PingFang → 黑体 → Arial Unicode；找不到就用默认）
FONT_STACK = ["PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti",
              "Arial Unicode MS", "Noto Sans CJK SC", "Microsoft YaHei", "SimHei"]

# ---- 色板令牌 -------------------------------------------------------------
BG = "#0b0f1a"          # 全局背景（深海军蓝黑）
BG_DEEP = "#070910"     # 更深一档（全屏网络）
PANEL = "#121a2b"       # 卡片底色
PANEL_SOFT = "#0f1522"  # 次级卡片
PANEL_EDGE = "#263049"  # 卡片描边
GRID_LINE = "#1d2537"   # 网格线
TEXT = "#e8ecf8"        # 主文本
TEXT_SUB = "#aab3cc"    # 次级文本
MUTED = "#78829c"       # 弱化文本
ACCENT_BLUE = "#4f8cff"
ACCENT_GREEN = "#34c759"
ACCENT_ORANGE = "#ff9f0a"
ACCENT_RED = "#ff453a"
ACCENT_PURPLE = "#c792ea"
ACCENT_YELLOW = "#ffd60a"
ACCENT_CYAN = "#69d2c1"

VALENCE_CMAP = None     # 延迟构建（需要 matplotlib.colors）


def apply_theme(toolbar: bool = False) -> None:
    """设置全局 rcParams：字体、颜色、去工具栏。两个 GUI 入口各调用一次。"""
    mpl.rcParams["font.sans-serif"] = FONT_STACK + list(mpl.rcParams.get("font.sans-serif", []))
    mpl.rcParams["font.family"] = "sans-serif"
    mpl.rcParams["axes.unicode_minus"] = False
    mpl.rcParams["toolbar"] = "None" if not toolbar else mpl.rcParams.get("toolbar", "None")
    mpl.rcParams["figure.facecolor"] = BG
    mpl.rcParams["axes.facecolor"] = BG
    mpl.rcParams["savefig.facecolor"] = BG
    mpl.rcParams["text.color"] = TEXT
    mpl.rcParams["axes.edgecolor"] = PANEL_EDGE
    mpl.rcParams["axes.labelcolor"] = TEXT_SUB
    mpl.rcParams["xtick.color"] = MUTED
    mpl.rcParams["ytick.color"] = MUTED


def valence_cmap():
    """坏 → 中性 → 好的情感配色（延迟构建避免导入顺序问题）。"""
    global VALENCE_CMAP
    if VALENCE_CMAP is None:
        import matplotlib.colors as mcolors
        VALENCE_CMAP = mcolors.LinearSegmentedColormap.from_list(
            "hippomind_valence", [ACCENT_RED, "#b9c2d2", ACCENT_GREEN])
    return VALENCE_CMAP


def style_axes(ax, *, title: str = "", title_size: float = 10.5,
               facecolor: str = PANEL_SOFT) -> None:
    """统一的卡片式坐标轴外观：柔和底色、弱化四边、主题化标题。"""
    ax.set_facecolor(facecolor)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(PANEL_EDGE)
    ax.tick_params(colors=MUTED, labelsize=7.5)
    if title:
        ax.set_title(title, color=TEXT, fontsize=title_size, pad=8,
                     fontweight="medium", loc="left")


def card(ax, x: float, y: float, w: float, h: float, *, title: str = "",
         accent: str = ACCENT_BLUE, facecolor: str = PANEL) -> None:
    """在坐标轴内绘制一张带左侧强调条的圆角感卡片（Rectangle 近似）。"""
    import matplotlib.patches as mpatches
    body = mpatches.FancyBboxPatch(
        (x + 0.008, y), w - 0.012, h,
        boxstyle="round,pad=0.004,rounding_size=0.010",
        facecolor=facecolor, edgecolor=PANEL_EDGE, lw=1.0,
        transform=ax.transAxes, zorder=1)
    ax.add_patch(body)
    strip = mpatches.Rectangle((x + 0.008, y), 0.0045, h, transform=ax.transAxes,
                               facecolor=accent, edgecolor="none", zorder=2)
    ax.add_patch(strip)
    if title:
        ax.text(x + 0.028, y + h - 0.052, title, color=accent, fontsize=10,
                fontweight="bold", transform=ax.transAxes, zorder=3)


def chip(fig, x: float, y: float, label: str, value: str = "", *,
         color: str = MUTED) -> list:
    """页眉状态胶囊：返回 [标签, 值] 两个 text artist 便于后续更新。"""
    label_artist = fig.text(x, y, label, color=MUTED, fontsize=8.2,
                            ha="left", va="center")
    value_artist = fig.text(x + 0.052, y, value, color=color, fontsize=8.4,
                            ha="left", va="center", fontweight="bold")
    return [label_artist, value_artist]


def pill_button(figure, rect, label, callback, *, accent: str = ACCENT_BLUE):
    """统一风格的按钮：深色底、强调字、悬停微亮。"""
    from matplotlib.widgets import Button
    ax = figure.add_axes(rect)
    ax.set_facecolor(PANEL)
    for side in ax.spines.values():
        side.set_color(PANEL_EDGE)
    button = Button(ax, label, color=PANEL, hovercolor="#1b2438")
    button.label.set_color(TEXT)
    button.label.set_fontsize(8.6)
    button.on_clicked(callback)
    return button
