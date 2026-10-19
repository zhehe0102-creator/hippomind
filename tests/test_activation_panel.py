"""GUI 激活分解面板（迭代 49）：选中记忆显示激活分量分解。"""
import matplotlib
matplotlib.use("Agg")

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ActivationBreakdownTests(unittest.TestCase):
    def test_selected_memory_shows_activation_breakdown(self):
        sys.path.insert(0, str(ROOT))
        from hippomind.mind import Mind
        from hippomind.grid_world import GridWorld
        from hippomind.agent import MemoryAgent
        from hippomind.network_visualizer import LayeredMemoryVisualizer

        mind = Mind(seed=42)
        env = GridWorld(seed=42)
        agent = MemoryAgent(env, mind, seed=42)
        for _ in range(30):
            agent.step()
        viz = LayeredMemoryVisualizer(mind, env, agent,
                                      max_nodes=100, max_edges=200)
        picked = False
        for key in list(viz._objects.keys()):
            if key.startswith("memory:"):
                viz.selected_key = key
                desc = viz._selected_description()
                self.assertIn("激活 [", desc)
                for component in ("强度", "新近", "触碰", "效用", "重要", "置信"):
                    self.assertIn(component, desc, f"缺少激活分量 {component}")
                picked = True
                break
        self.assertTrue(picked, "演示库中应有可选中的记忆")


if __name__ == "__main__":
    unittest.main()
