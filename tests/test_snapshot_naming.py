"""GUI 快照命名带 seed 与节点数（迭代 76）。"""
import re
import sys
import tempfile
import unittest
from pathlib import Path
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
warnings.filterwarnings("ignore")

from hippomind.mind import Mind
from hippomind.grid_world import GridWorld
from hippomind.agent import MemoryAgent
from hippomind.network_visualizer import LayeredMemoryVisualizer


class SnapshotNamingTests(unittest.TestCase):
    def setUp(self):
        self.mind = Mind(seed=42)
        self.env = GridWorld(seed=42)
        self.agent = MemoryAgent(self.env, self.mind, seed=42)
        for _ in range(40):
            self.agent.step()
        self.out_dir = tempfile.TemporaryDirectory(prefix="hippo-snap-test-")
        self.addCleanup(self.out_dir.cleanup)
        self.viz = LayeredMemoryVisualizer(self.mind, self.env, self.agent)

    def test_filename_contains_seed_and_node_count(self):
        path = self.viz.do_snapshot(out_dir=self.out_dir.name)
        m = re.search(r"-seed42-n(\d+)\.png$", path.name)
        self.assertIsNotNone(m, f"文件名应含 seed 与节点数: {path.name}")
        self.assertGreater(int(m.group(1)), 0)

    def test_unseeded_mind_omits_seed_part(self):
        mind = Mind()
        env = GridWorld()
        agent = MemoryAgent(env, mind)
        for _ in range(10):
            agent.step()
        viz = LayeredMemoryVisualizer(mind, env, agent)
        path = viz.do_snapshot(out_dir=self.out_dir.name)
        self.assertNotIn("-seed", path.name)
        self.assertRegex(path.name, r"-n\d+\.png$")


if __name__ == "__main__":
    unittest.main()
