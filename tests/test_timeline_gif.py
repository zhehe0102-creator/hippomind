"""时间轴 GIF 导出（迭代 86）。"""
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


class TimelineGifTests(unittest.TestCase):
    def setUp(self):
        self.mind = Mind(seed=42)
        self.env = GridWorld(seed=42)
        self.agent = MemoryAgent(self.env, self.mind, seed=42)
        for _ in range(60):
            self.agent.step()
        self.out_dir = tempfile.TemporaryDirectory(prefix="hippo-gif-test-")
        self.addCleanup(self.out_dir.cleanup)
        self.viz = LayeredMemoryVisualizer(self.mind, self.env, self.agent)

    def test_exports_multi_frame_gif(self):
        path = self.viz.export_timeline_gif(out_dir=self.out_dir.name,
                                            frames=6, fps=4)
        self.assertIsNotNone(path, self.viz.last_action)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 10_000)
        self.assertIn("GIF", self.viz.last_action)

    def test_cursor_restored_after_export(self):
        self.viz.toggle_timeline()
        self.assertTrue(self.viz.timeline_mode)
        saved = self.viz.timeline_cursor
        self.viz.timeline_cursor = self.viz._timeline_t1  # 跳到末尾
        path = self.viz.export_timeline_gif(out_dir=self.out_dir.name,
                                            frames=4, fps=2)
        self.assertIsNotNone(path)
        self.assertAlmostEqual(self.viz.timeline_cursor,
                               self.viz._timeline_t1, places=3)


if __name__ == "__main__":
    unittest.main()
