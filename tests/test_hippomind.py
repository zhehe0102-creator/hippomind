from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import unittest

from hippomind.hippocampus import Hippocampus, token_sim
from hippomind.llm_agent import LLMHippoAgent
from hippomind.mind import Mind
from hippomind.commons import MemoryCommons


class HippocampusLifecycleTests(unittest.TestCase):
    def make_hippo(self, **cfg) -> Hippocampus:
        return Hippocampus(
            seed=7,
            consolidate_every=10_000,
            dream_every=10_000,
            auto_prune_every=10_000,
            **cfg,
        )

    def test_action_aware_dedup_keeps_outcomes_separate(self):
        h = self.make_hippo()
        north = h.store("same state", action="N", valence=0.8, features=[0.0, 1.0])
        south = h.store("same state", action="S", valence=-0.8, features=[0.0, 1.0])
        north_again = h.store("same state", action="N", valence=0.4, features=[0.0, 1.0])

        self.assertNotEqual(north, south)
        self.assertEqual(north, north_again)
        self.assertEqual(h.episodic[north].observation_count, 2)
        self.assertAlmostEqual(h.episodic[north].valence, 0.6)
        self.assertEqual(h.episodic[south].action, "S")

    def test_decay_can_cross_threshold_and_archive(self):
        h = self.make_hippo(decay_episodic=1.0)
        mid = h.store("forgettable", action="N")
        h.tick(10.0)
        self.assertLess(h.episodic[mid].weight, h.cfg["min_weight"])
        h.prune()
        self.assertNotIn(mid, h.episodic)
        self.assertIn(mid, h.archive)
        self.assertTrue(h.restore(mid))
        self.assertIn(mid, h.episodic)

    def test_capacity_and_association_degree_are_bounded(self):
        h = self.make_hippo(
            max_episodic=12,
            max_assoc_degree=3,
            link_top_n=3,
            dedup_threshold=1.1,
            link_threshold=0.1,
        )
        for i in range(30):
            h.store(f"state {i}", action="NESW"[i % 4], features=[i / 100.0])
        h.prune()

        self.assertLessEqual(len(h.episodic), 12)
        self.assertGreaterEqual(len(h.archive), 18)
        self.assertTrue(all(len(keys) <= 3 for keys in h._assoc_index.values()))
        self.assertLessEqual(len(h.assoc), 18)

    def test_consolidation_updates_existing_rule_once(self):
        h = self.make_hippo(dedup_threshold=0.9999, consolidate_min_support=3)
        for _ in range(3):
            h.store("state a", action="N", valence=0.6, features=[0.0])
        self.assertEqual(h.consolidate(), 1)
        self.assertEqual(len(h.semantic), 1)

        for _ in range(3):
            h.store("state b", action="N", valence=0.2, features=[0.2])
        self.assertEqual(h.consolidate(), 1)
        self.assertEqual(len(h.semantic), 1)
        rule = next(iter(h.semantic.values()))
        self.assertEqual(rule.support_count, 6)
        self.assertEqual(len(rule.source_episodes), 2)

    def test_dream_digests_once_without_strengthening_source(self):
        h = self.make_hippo()
        mid = h.store("a difficult day", action="reflect", valence=-0.4)
        before = h.episodic[mid].weight

        self.assertEqual(len(h.dream()), 1)
        self.assertEqual(h.dream(), [])
        self.assertEqual(h.episodic[mid].weight, before)
        self.assertTrue(h.episodic[mid].digested)

    def test_expired_and_superseded_memories_do_not_surface(self):
        h = self.make_hippo()
        old = h.store("用户喜欢咖啡", action="remember")
        new = h.store("用户现在喜欢茶", action="remember")
        expired = h.store("今天临时下雨", action="umbrella", valid_until=0.0)
        self.assertTrue(h.supersede(old, new))

        ids = {m.id for _, m in h.recall(query="用户喜欢什么", k=10)}
        self.assertNotIn(old, ids)
        self.assertNotIn(expired, ids)
        self.assertGreater(token_sim("用户喜欢咖啡", "用户喜欢什么"), 0.0)

    def test_persistence_roundtrip_restores_archive_metrics_and_index(self):
        h = self.make_hippo(max_episodic=1, link_threshold=0.0)
        h.store("one", action="N", features=[0.0])
        h.store("two", action="S", features=[0.1])
        h.note_outcome(2.0, "food")
        h.prune()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "memory.json")
            h.save(path)
            loaded = self.make_hippo()
            loaded.load(path)

        self.assertEqual(len(loaded.episodic), 1)
        self.assertEqual(len(loaded.archive), 1)
        self.assertEqual(loaded.stats_summary()["food"], 1)
        self.assertAlmostEqual(loaded.stats_summary()["total_return"], 2.0)
        for key in loaded.assoc:
            self.assertIn(key, loaded._assoc_index[key[0]])
            self.assertIn(key, loaded._assoc_index[key[1]])

    def test_v2_migration_keeps_data_but_replaces_unsafe_thresholds(self):
        legacy = self.make_hippo()
        mid = legacy.store("legacy memory", action="N")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "legacy.json")
            legacy.save(path)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["version"] = 2
            data["cfg"].update({"recall_boost": 0.9, "min_weight": 0.5})
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)

            loaded = self.make_hippo()
            loaded.load(path)

        self.assertIn(mid, loaded.episodic)
        self.assertEqual(loaded.cfg["recall_boost"], Hippocampus.DEFAULT_CFG["recall_boost"])
        self.assertEqual(loaded.cfg["min_weight"], Hippocampus.DEFAULT_CFG["min_weight"])

    def test_mind_roundtrip_restores_drives_and_memory(self):
        mind = Mind(seed=5, hippo_consolidate_every=10_000,
                    hippo_dream_every=10_000, hippo_auto_prune_every=10_000)
        mid = mind.store("remember this", action="keep")
        mind.interact("progress", strength=0.5)
        expected_drives = dict(mind.drives.values)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "mind.json")
            mind.save(path)
            loaded = Mind(seed=6)
            loaded.load(path)

        self.assertIn(mid, loaded.episodic)
        self.assertEqual(loaded.drives.values, expected_drives)

    def test_load_normalizes_corrupt_values_and_preserves_existing_on_failure(self):
        h = self.make_hippo()
        mid = h.store("稳定记忆", action="keep", features=[0.0, 1.0])
        data = h.to_dict()
        data["cfg"]["decay_episodic"] = "nan"
        data["episodic"][0].update({
            "valence": float("nan"),
            "arousal": float("inf"),
            "features": [0.0, float("nan")],
            "tags": ["x" * 500],
        })
        data["assoc"] = [{"a": mid, "b": mid, "strength": float("nan")}]

        loaded = self.make_hippo()
        loaded.load_dict(data)
        memory = loaded.episodic[mid]
        self.assertEqual(memory.valence, 0.0)
        self.assertEqual(memory.arousal, 0.3)
        self.assertIsNone(memory.features)
        self.assertLessEqual(len(memory.tags[0]), 96)
        self.assertEqual(loaded.cfg["decay_episodic"],
                         Hippocampus.DEFAULT_CFG["decay_episodic"])
        self.assertEqual(loaded.assoc, {})

        before = dict(loaded.episodic)
        with self.assertRaises(ValueError):
            loaded.load_dict([])
        self.assertEqual(loaded.episodic, before)

    def test_mind_save_uses_single_atomic_snapshot(self):
        mind = Mind(seed=9)
        mind.store("保存测试", action="write")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "mind.json")
            mind.save(path)
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".hippo.tmp"))
            self.assertFalse(os.path.exists(path + ".mind.tmp"))


class LLMFeedbackTests(unittest.TestCase):
    def test_model_prediction_is_not_committed_without_external_feedback(self):
        h = Hippocampus(seed=3, consolidate_every=10_000, dream_every=10_000,
                        auto_prune_every=10_000)
        source = h.store("deploy service", action="run tests", valence=0.5)
        agent = LLMHippoAgent(h, api_key="test")
        agent._chat = lambda prompt: "ACTION: deploy\nLESSON: test first\nRESULT: good"

        agent.run_task("deploy service")
        self.assertEqual(len(h.episodic), 1)
        before = h.episodic[source].utility

        committed = agent.record_feedback(-0.8, outcome="deployment failed")
        self.assertEqual(len(h.episodic), 2)
        self.assertLess(h.episodic[source].utility, before)
        self.assertEqual(h.episodic[committed].source, "external-feedback")

    def test_llm_default_does_not_inject_raw_chat_and_marks_memory_untrusted(self):
        mind = Mind(seed=3)
        calls = {}
        prompt = {}
        mind.commons = object()

        def recall_all(**kwargs):
            calls.update(kwargs)
            return []

        def fake_chat(value):
            prompt["value"] = value
            return "ACTION: inspect\nLESSON: verify\nRESULT: neutral"

        mind.recall_all = recall_all
        agent = LLMHippoAgent(mind, api_key="test")
        agent._chat = fake_chat

        agent.run_task("检查部署")
        self.assertFalse(calls["include_raw_chats"])
        self.assertIn("MEMORY_DATA_UNTRUSTED", prompt["value"])


class MemoryCommonsTests(unittest.TestCase):
    def make_archive(self, root: str) -> str:
        index = os.path.join(root, "index")
        os.makedirs(index, exist_ok=True)
        path = os.path.join(index, "index.sqlite")
        with sqlite3.connect(path) as conn:
            conn.execute("""
                CREATE TABLE messages (
                    agent TEXT, session_id TEXT, event_id TEXT, ts TEXT,
                    role TEXT, content TEXT, model TEXT, cwd TEXT,
                    title TEXT, source_file TEXT
                )
            """)
            conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)", [
                ("codex", "c1", "e1", "2026-08-01T00:00:00Z", "assistant",
                 "hippomind 使用冷归档控制活跃记忆容量", "gpt", "/p", "memory", "a"),
                ("claude", "c2", "e2", "2026-08-02T00:00:00Z", "user",
                 "需要让所有 Agent 的记忆互通", "claude", "/p", "shared", "b"),
                ("claude", "c2", "e3", "2026-08-02T00:00:01Z", "tool",
                 "secret token in tool output", "claude", "/p", "shared", "b"),
                ("codex", "c3", "e5", "2026-08-04T00:00:00Z", "assistant",
                 "The following is the Codex agent history added since approval. "
                 "Agent 记忆互通", "gpt", "/p", "runtime", "d"),
            ])
        return path

    def test_cross_agent_search_is_live_and_filters_tool_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self.make_archive(tmpdir)
            commons = MemoryCommons(tmpdir)
            hits = commons.search_messages("Agent 记忆互通", limit=10)
            self.assertIn("claude", {hit.memory.agent for hit in hits})
            self.assertTrue(all(hit.memory.role != "tool" for hit in hits))
            self.assertTrue(all("history added since" not in hit.memory.content for hit in hits))
            claude_only = commons.search_messages(
                "Agent 记忆互通", limit=10, agents=["claude"])
            self.assertEqual({hit.memory.agent for hit in claude_only}, {"claude"})

            with sqlite3.connect(path) as conn:
                conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    "gemini", "g1", "e4", "2026-08-03T00:00:00Z", "assistant",
                    "Agent 记忆互通已经接入实时归档", "gemini", "/g", "new", "c"))
            refreshed = commons.search_messages("Agent 记忆互通", limit=10)

        self.assertIn("gemini", {hit.memory.agent for hit in refreshed})

    def test_external_volume_uses_portable_sqlite_journal(self):
        commons = object.__new__(MemoryCommons)
        commons.shared_db = type("PortablePath", (), {
            "__str__": lambda self: "/Volumes/exfat/archive/index/commons.sqlite"
        })()
        self.assertEqual(commons._shared_journal_mode(), "DELETE")

    def test_shared_ledger_requires_provenance_and_supports_feedback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.make_archive(tmpdir)
            commons = MemoryCommons(tmpdir)
            with self.assertRaises(ValueError):
                commons.publish("unsupported fact", source_agent="codex")
            with self.assertRaises(ValueError):
                commons.publish("bad ref", source_agent="codex",
                                source_refs=["not a reference"])
            old = commons.publish("部署前运行集成测试", source_agent="codex",
                                  source_refs=["chat:e1"], tags=["deploy"])
            new = commons.publish("部署前运行集成测试和健康检查", source_agent="claude",
                                  source_refs=["chat:e2"], tags=["deploy"])
            self.assertTrue(commons.feedback(new, 0.8))
            self.assertTrue(commons.supersede(old, new))
            # 重复发布必须保留跨 Agent 反馈，也不能复活已被替代的旧规则。
            commons.publish("部署前运行集成测试", source_agent="codex",
                            source_refs=["chat:e3"], tags=["release"], memory_id=old)
            commons.publish("部署前运行集成测试和健康检查", source_agent="claude",
                            source_refs=["chat:e4"], tags=["release"], memory_id=new)
            hits = commons.search_shared("部署 集成测试 健康检查", limit=10)
            recent = commons.recent_shared(limit=1)

        self.assertEqual([hit.memory.id for hit in hits], [new])
        self.assertEqual([hit.memory.id for hit in recent], [new])
        self.assertGreater(hits[0].memory.utility, 0.0)
        self.assertEqual(hits[0].memory.success_count, 1)
        self.assertEqual(set(hits[0].memory.source_refs), {"chat:e2", "chat:e4"})

    def test_promote_is_idempotent_and_recall_all_merges_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.make_archive(tmpdir)
            mind = Mind(seed=4, commons_path=tmpdir, enable_commons=True,
                        hippo_consolidate_every=10_000, hippo_dream_every=10_000,
                        hippo_auto_prune_every=10_000)
            mind.store("本地记忆使用主动归档", action="archive")
            hits = mind.recall_all("hippomind 记忆归档", k=5)
            shared_hit = next(hit for hit in hits if hit.origin == "chat")
            first = mind.promote_common(shared_hit)
            second = mind.promote_common(shared_hit)

        self.assertEqual(first, second)
        self.assertEqual(mind.episodic[first].metadata["commons_id"], shared_hit.memory.id)


class LayeredVisualizerTests(unittest.TestCase):
    def test_layered_network_renders_real_memory_topology(self):
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt

        from hippomind.agent import MemoryAgent
        from hippomind.grid_world import GridWorld
        from hippomind.network_visualizer import LayeredMemoryVisualizer

        mind = Mind(seed=11)
        env = GridWorld(seed=11)
        agent = MemoryAgent(env, mind, seed=11)
        for _ in range(180):
            agent.step()
        visualizer = LayeredMemoryVisualizer(mind, env, agent,
                                             max_nodes=140, max_edges=500)
        visualizer.frame()  # first frame performs the initial topology build
        initial_rebuilds = visualizer._rebuild_count
        for _ in range(100):
            visualizer.frame()
        self.assertEqual(visualizer._rebuild_count, initial_rebuilds)
        visualizer.do_recall()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "network.png")
            visualizer.fig.savefig(path, dpi=90)
            size = os.path.getsize(path)
        plt.close(visualizer.fig)

        self.assertGreater(len(visualizer._positions), 10)
        self.assertGreater(visualizer._edge_count, 0)
        self.assertGreater(visualizer._max_curve_deviation, 0.01)
        self.assertGreater(visualizer._visible_counts["episodic"], 0)
        # 迭代 29 视觉契约：边宽随强度变化且有界（0.32 + 1.05*strength），
        # 等宽边是"毛球感"主因，已废除。
        widths = list(width for width in visualizer._edge_data(
            visualizer._collect_nodes())[2])
        self.assertGreater(len(widths), 0)
        lo, hi = visualizer._edge_width_range
        self.assertLess(lo, hi, "边宽应随强度变化")
        self.assertGreaterEqual(lo, 0.32 - 1e-9)
        self.assertLessEqual(hi, 0.32 + 1.05 + 1e-9)
        self.assertGreater(visualizer._edge_type_counts["drive_domain"], 0)
        self.assertGreater(visualizer._edge_type_counts["drive_memory"], 0)
        self.assertGreater(size, 10_000)


class VisualizerMemoryTests(unittest.TestCase):
    def test_gui_panel_and_layout_caches_drop_stale_memory_ids(self):
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt

        from hippomind.agent import MemoryAgent
        from hippomind.grid_world import GridWorld
        from hippomind.visualizer import Visualizer

        mind = Mind(seed=17)
        env = GridWorld(seed=17)
        agent = MemoryAgent(env, mind, seed=17)
        visualizer = Visualizer(mind, env, agent)
        visualizer._curves = {mid: [(0.0, 0.5)] for mid in range(20)}
        visualizer._layout = {mid: (0.0, 0.0) for mid in range(20)}
        visualizer._update_curves()
        visualizer._update_net()

        self.assertEqual(visualizer._panel_refresh_frames, 300)
        self.assertEqual(visualizer._curves, {})
        self.assertEqual(visualizer._layout, {})
        plt.close(visualizer.fig)


if __name__ == "__main__":
    unittest.main()
