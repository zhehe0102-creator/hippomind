"""GUI 内嵌查询框（前景轮三）。"""
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
warnings.filterwarnings("ignore")

from hippomind.mind import Mind
from hippomind.grid_world import GridWorld
from hippomind.agent import MemoryAgent
from hippomind.network_visualizer import LayeredMemoryVisualizer


class _Hub:
    def __init__(self, items):
        self._items = items

    def stats(self):
        return {"health": {"status": "ok"}, "stats": {}}

    def context_pack(self, query):
        return {"items": self._items}


class GuiQueryBoxTests(unittest.TestCase):
    def setUp(self):
        self.mind = Mind(seed=9)
        self.env = GridWorld(seed=9)
        self.agent = MemoryAgent(self.env, self.mind, seed=9)
        for _ in range(20):
            self.agent.step()
        self.out_dir = tempfile.TemporaryDirectory(prefix="hippo-qbox-test-")
        self.addCleanup(self.out_dir.cleanup)
        self.viz = LayeredMemoryVisualizer(self.mind, self.env, self.agent)

    def test_query_renders_results_via_hub(self):
        self.viz.hub_client = _Hub([
            {"content": "命中 记忆甲", "score": 0.91},
            {"content": "命中 记忆乙", "score": 0.55}])
        self.viz._run_gui_query("测试查询词")
        self.assertIn("2 条", self.viz.last_action)
        self.assertIn("[0.910] 命中 记忆甲", self.viz._hub_context)

    def test_no_hub_falls_back_to_cli_hint(self):
        self.viz.hub_client = None
        self.viz._run_gui_query("另一个问题")
        self.assertIn("CLI", self.viz.last_action)

    def test_blank_query_ignored(self):
        before = self.viz.last_action
        self.viz._run_gui_query("   ")
        self.assertEqual(self.viz.last_action, before)

    def test_enable_query_box_safe_on_agg(self):
        self.viz._enable_query_box()
        # Agg 下 TextBox 可构造；重复调用幂等
        box = self.viz.query_box
        self.viz._enable_query_box()
        self.assertIs(self.viz.query_box, box)


if __name__ == "__main__":
    unittest.main()
