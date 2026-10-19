"""GUI 实体图层叠加（前景轮八）：选中记忆高亮账本主语邻域。"""
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
from hippomind.ledger import SQLiteLedger
from hippomind.network_visualizer import LayeredMemoryVisualizer


class _Memory:
    """最小记忆桩：id + situation 文本。"""

    def __init__(self, mid, situation):
        self.id = mid
        self.situation = situation


class EntityOverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-entity-")
        self.addCleanup(self.tmp.cleanup)
        # 账本：两个主语，其一有冲突
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "l.sqlite")
        for i, (subject, value) in enumerate(
                [("service-a", "v0"), ("service-a", "v1"),
                 ("数据库端口", "5432")]):
            self.ledger.ingest({
                "content": f"{subject} 状态 {value}", "kind": "fact",
                "source_agent": "t", "evidence": [f"t:{i}"],
                "claims": [{"subject": subject, "predicate": "status",
                            "slot_key": "state", "value": {"v": value},
                            "confidence": 0.8}]})

        self.mind = Mind(seed=7)
        self.env = GridWorld(seed=7)
        self.agent = MemoryAgent(self.env, self.mind, seed=7)
        for _ in range(10):
            self.agent.step()
        self.viz = LayeredMemoryVisualizer(self.mind, self.env, self.agent,
                                           ledger=self.ledger)

    def test_recent_subjects_aggregates(self):
        rows = self.ledger.recent_subjects()
        by = {r["subject"]: r for r in rows}
        self.assertIn("service-a", by)
        self.assertEqual(by["service-a"]["claims"], 2)
        self.assertEqual(by["service-a"]["conflicts"], 2)
        self.assertEqual(by["数据库端口"]["conflicts"], 0)

    def test_neighborhood_hits_and_peers(self):
        target = _Memory(101, "service-a 部署在机房 A")
        peer = _Memory(102, "service-a 升级完成")
        other = _Memory(103, "无关内容")
        # 注入演示 Mind 以便共现扫描
        from dataclasses import replace
        from hippomind.memory import EpisodicMemory
        em_target = EpisodicMemory(id=target.id, situation=target.situation)
        em_peer = EpisodicMemory(id=peer.id, situation=peer.situation)
        em_other = EpisodicMemory(id=other.id, situation=other.situation)
        self.mind.episodic[em_target.id] = em_target
        self.mind.episodic[em_peer.id] = em_peer
        self.mind.episodic[em_other.id] = em_other

        nb = self.viz._entity_neighborhood(em_target)
        self.assertIn("service-a", nb["subjects"])
        self.assertNotIn("数据库端口", nb["subjects"])
        self.assertIn(em_peer.id, nb["peers"])
        self.assertNotIn(em_other.id, nb["peers"])
        meta = nb["subjects"]["service-a"]
        self.assertGreaterEqual(meta["claims"], 2)

    def test_no_ledger_no_overlay(self):
        bare = LayeredMemoryVisualizer(self.mind, self.env, self.agent)
        self.assertEqual(bare._entity_neighborhood(_Memory(1, "x")), {})
        self.assertEqual(bare._entity_subjects(), {})

    def test_selected_description_includes_entity_line(self):
        from hippomind.memory import EpisodicMemory
        em = EpisodicMemory(id=201, situation="service-a 运行正常")
        self.mind.episodic[em.id] = em
        self.viz._objects[f"memory:{em.id}"] = em
        self.viz.selected_key = f"memory:{em.id}"
        text = self.viz._selected_description()
        self.assertIn("实体邻域", text)
        self.assertIn("service-a", text)


if __name__ == "__main__":
    unittest.main()
