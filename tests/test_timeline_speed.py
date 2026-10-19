"""时间轴回放速度控制（迭代 59）：+/- 调速与 . 暂停。"""
import matplotlib
matplotlib.use("Agg")

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class _Ev:
    def __init__(self, key):
        self.key = key


class TimelineSpeedTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT))
        from hippomind.mind import Mind
        from hippomind.grid_world import GridWorld
        from hippomind.agent import MemoryAgent
        from hippomind.network_visualizer import LayeredMemoryVisualizer
        mind = Mind(seed=42)
        env = GridWorld(seed=42)
        agent = MemoryAgent(env, mind, seed=42)
        for _ in range(40):
            agent.step()
        self.viz = LayeredMemoryVisualizer(mind, env, agent,
                                           max_nodes=100, max_edges=200)
        self.viz.toggle_timeline()
        self.assertTrue(self.viz.timeline_mode)

    def test_plus_speeds_up_playback(self):
        base = self._timeline_span() / 300.0
        cur0 = self.viz.timeline_cursor
        self.viz._on_key(_Ev("+"))
        self.assertAlmostEqual(self.viz.timeline_speed, 1.5)
        self.viz._advance_timeline()
        self.assertGreater(self.viz.timeline_cursor - cur0, base * 1.4)

    def test_minus_slows_down(self):
        self.viz._on_key(_Ev("+"))
        self.viz._on_key(_Ev("-"))
        self.assertAlmostEqual(self.viz.timeline_speed, 1.0)

    def test_dot_toggles_pause(self):
        self.assertTrue(self.viz.timeline_playing)
        self.viz._on_key(_Ev("."))
        self.assertFalse(self.viz.timeline_playing)
        self.viz._on_key(_Ev("."))
        self.assertTrue(self.viz.timeline_playing)

    def test_speed_clamped_to_bounds(self):
        for _ in range(10):
            self.viz._on_key(_Ev("+"))
        self.assertLessEqual(self.viz.timeline_speed, 8.0)
        for _ in range(20):
            self.viz._on_key(_Ev("-"))
        self.assertGreaterEqual(self.viz.timeline_speed, 0.25)

    def _timeline_span(self):
        return self.viz._timeline_span


if __name__ == "__main__":
    unittest.main()
