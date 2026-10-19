"""Capture deterministic, real HippoMind GUI frames for the user guide."""

from __future__ import annotations

from pathlib import Path
import sys

import matplotlib

matplotlib.use("Agg", force=True)

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hippomind.agent import MemoryAgent
from hippomind.grid_world import GridWorld
from hippomind.mind import Mind
from hippomind.network_visualizer import LayeredMemoryVisualizer
from hippomind.visualizer import Visualizer


OUT = ROOT / "docs" / "images"


class GuideHub:
    def stats(self):
        return {"health": {"status": "ok"}, "stats": {"ledger": {
            "events": 206_563, "active": 7_000, "archived": 199_563,
            "kinds": {"semantic": 1_294}, "agents": {"codex": 8, "pi": 4, "deepseek": 3},
        }}}

    def context_pack(self, _query):
        return {"text": "任务：v0.9.2 GUI 指南\n下一步：核对显式记忆、证据与上下文预算"}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    mind = Mind(seed=42)
    env = GridWorld(seed=42)
    agent = MemoryAgent(env, mind, epsilon=0.12, seed=42)
    for _ in range(240):
        agent.step()
    visualizer = Visualizer(mind, env, agent)
    visualizer.do_manual_recall()
    visualizer.frame()
    visualizer.fig.savefig(OUT / "gui-grid-world.png", dpi=150, facecolor=visualizer.fig.get_facecolor())
    plt.close(visualizer.fig)

    network = LayeredMemoryVisualizer(mind, env, agent, hub_client=GuideHub(),
                                      max_nodes=120, max_edges=320)
    network.do_context_pack()
    network.do_recall()
    network.fig.savefig(OUT / "gui-memory-network.png", dpi=150, facecolor=network.fig.get_facecolor())
    plt.close(network.fig)
    print(OUT)


if __name__ == "__main__":
    main()
