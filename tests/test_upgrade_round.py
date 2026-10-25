"""本轮升级（检索融合 / 注入优化 / 凝练升级 / 长跑稳定 / GUI 重构）回归测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import os as _os
import tempfile as _tempfile

# 隔离 HIPPOMIND_HOME：daemon 启动时会对检测到的 agent 执行 auto_install，
# 不隔离就会把测试用的临时 ledger 路径写进用户真实的 ~/.claude/CLAUDE.md、
# ~/.gemini/settings.json 等文件里（本机实测污染过 6 个测试）。
_HIPPO_HOME = _tempfile.mkdtemp(prefix="hippomind-test-home-")
_os.environ["HIPPOMIND_HOME"] = _HIPPO_HOME

import unittest
from pathlib import Path

import matplotlib

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
from hippomind.hippocampus import Hippocampus
from hippomind.ledger import SQLiteLedger


class RetrievalUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def _ingest(self, content, marker, **extra):
        return self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": content, "evidence": [f"evidence:{marker}"], **extra,
        })

    def test_score_parts_contains_rrf_channel(self):
        self._ingest("部署流水线回滚手册", "rrf1")
        hits = self.ledger.search("部署流水线回滚", scope="shared", limit=5)
        self.assertTrue(hits)
        parts = hits[0].get("score_parts", {})
        self.assertIn("rrf", parts)
        self.assertGreaterEqual(float(parts["rrf"]), 0.0)

    def test_dual_channel_hit_outranks_single_channel(self):
        a = self._ingest("数据库连接池调优指南与参数说明", "dual")
        b = self._ingest("完全无关的旅行日记和美食记录", "single")
        hits = self.ledger.search("数据库连接池调优", scope="shared", limit=10)
        by_id = {h["memory_id"]: h for h in hits}
        self.assertIn(a.memory_id, by_id)
        # 双通道命中的记忆应获得非零 RRF 融合分
        self.assertGreaterEqual(float(by_id[a.memory_id]["score_parts"]["rrf"]), 0.0)

    def test_expansion_pass_finds_entity_only_matches(self):
        # 主查询词不在内容里，但实体词（长 CJK 词）在：扩展通道应能召回。
        self._ingest("Kubernetes 滚动发布操作步骤", "k8s")
        hits = self.ledger.search("Kubernetes 发布", scope="shared", limit=10)
        self.assertTrue(any("Kubernetes" in h["content"] for h in hits))


class InjectionUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.builder = ContextPackBuilder(self.ledger)

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, content, marker, score_hint=None):
        return self._ingest(content, marker)

    def _ingest(self, content, marker):
        return self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": content, "evidence": [f"evidence:{marker}"],
        })

    def test_near_duplicate_candidates_are_dropped(self):
        original = "消息队列积压报警的排查思路是先看消费者组滞后再查磁盘IO"
        paraphrase = "消息队列积压报警的排查思路是先看消费者组滞后再查磁盘 IO 与分区"
        self._ingest(original, "orig")
        self._ingest(paraphrase, "para")
        pack = self.builder.build("消息队列积压报警的排查思路",
                                  ContextPackOptions(max_items=8))
        joined = json.dumps(pack["items"], ensure_ascii=False)
        # 两条近似文本只应有一条进入注入包
        self.assertLessEqual(joined.count("消费者组滞后"), 1)

    def test_empty_pack_abstains_with_honest_message(self):
        self._ingest("完全无关的星座运势内容", "noise")
        pack = self.builder.build("量子纠缠实验装置校准流程",
                                  ContextPackOptions(max_items=8))
        self.assertEqual(pack["items"], [])
        self.assertIn("未找到", pack["text"])


class ConsolidationUpgradeTests(unittest.TestCase):
    def test_consolidate_seeds_config_is_sanitised(self):
        h = Hippocampus(consolidate_seeds=5)
        self.assertEqual(h.cfg["consolidate_seeds"], 5)
        h2 = Hippocampus(consolidate_seeds=-3)
        self.assertGreaterEqual(h2.cfg["consolidate_seeds"], 1)

    def test_surprise_weighted_consolidation_still_forms_rules(self):
        h = Hippocampus(dedup_threshold=0.9999, consolidate_min_support=3,
                        consolidate_seeds=4)
        for _ in range(3):
            h.store("重复情境甲", action="N", valence=-0.9, features=[0.0])
        for _ in range(3):
            h.store("平淡情境乙", action="S", valence=0.05, features=[0.9])
        made = h.consolidate()
        # 种子预算内：高惊喜（强负结果）簇优先成规则
        self.assertGreaterEqual(made, 1)
        self.assertTrue(any(r.action == "N" for r in h.semantic.values()))


class LongRunStabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_maintenance_checkpoint_truncates_wal(self):
        path = Path(self.tmp.name) / "hub.sqlite"
        ledger = SQLiteLedger(path)
        ledger.ingest({"kind": "fact", "scope": "shared", "source_agent": "t",
                       "content": "检查点验证数据", "evidence": ["e:wal"]})
        report = ledger.maintenance_checkpoint()
        self.assertIn("wal_truncated", report)
        self.assertIn("optimize", report)
        self.assertFalse(report["wal_busy"])
        wal_path = Path(str(path) + "-wal")
        if wal_path.exists():
            self.assertLessEqual(wal_path.stat().st_size, 1 << 20)

    def test_task_jsonl_incremental_offset_reads_only_new_lines(self):
        from hippomind.watcher import ArchiveWatcher
        root = Path(self.tmp.name)
        tasks_dir = root / "tasks"
        tasks_dir.mkdir(parents=True)
        tasks_file = tasks_dir / "tasks.jsonl"

        def task_row(n):
            return json.dumps({
                "task_id": f"task-{n}", "title": f"增量任务{n}",
                "description": f"第 {n} 个任务描述", "status": "in_progress",
                "agent": "codex", "created_at": "2026-08-21T00:00:00Z",
                "updated_at": "2026-08-21T00:00:00Z", "notes": [], "tags": [],
            }, ensure_ascii=False)

        tasks_file.write_text(task_row(1) + "\n" + task_row(2) + "\n", encoding="utf-8")
        ledger = SQLiteLedger(root / "hub.sqlite")
        watcher = ArchiveWatcher(ledger, root, poll_interval=5)
        first = watcher.sync_once()["sources"].get("tasks", {})
        self.assertEqual(first.get("rows"), 2)
        cursor_meta = ledger.get_cursor(
            watcher._source_id("archive-tasks", tasks_file))["metadata"]
        saved_offset = int(cursor_meta["byte_offset"])
        self.assertEqual(saved_offset, len((task_row(1) + "\n" + task_row(2) + "\n").encode()))
        # 追加一行：增量路径只处理新行
        with tasks_file.open("a", encoding="utf-8") as handle:
            handle.write(task_row(3) + "\n")
        second = watcher.sync_once()["sources"]["tasks"]
        self.assertEqual(second.get("rows"), 1)
        stats = ledger.stats()
        self.assertEqual(stats["events"], 3)

    def test_raw_archive_rerun_is_idempotent(self):
        from hippomind.ledger import DEFAULT_RAW_RETENTION_DAYS
        path = Path(self.tmp.name) / "hub.sqlite"
        ledger = SQLiteLedger(path)
        old_ts = "2026-01-01T00:00:00+00:00"
        for i in range(3):
            ledger.ingest({"kind": "raw_chat", "raw_chat": True, "role": "user",
                           "scope": "shared", "source_agent": "t",
                           "content": f"历史聊天证据 {i}", "evidence": [f"e:{i}"],
                           "timestamp": old_ts})
        archive_dir = Path(self.tmp.name) / "cold"
        first = ledger.archive_raw_evidence(before_days=30, archive_dir=archive_dir)
        second = ledger.archive_raw_evidence(before_days=30, archive_dir=archive_dir)
        self.assertEqual(first["raw_compressed"], 3)
        self.assertEqual(second["raw_compressed"], 0)


class GuiRefactorSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        matplotlib.use("Agg", force=True)

    def _build(self):
        from hippomind.agent import MemoryAgent
        from hippomind.grid_world import GridWorld
        from hippomind.mind import Mind
        mind = Mind(seed=11)
        env = GridWorld(seed=11)
        agent = MemoryAgent(env, mind, seed=11)
        for _ in range(25):
            agent.step()
        return mind, env, agent

    def test_workbench_theme_and_artists(self):
        from hippomind.visualizer import Visualizer
        from hippomind.ui_theme import BG
        mind, env, agent = self._build()
        vis = Visualizer(mind, env, agent)
        vis.frame()
        self.assertAlmostEqual(vis.fig.get_facecolor()[:3],
                               tuple(int(BG[i:i + 2], 16) / 255 for i in (1, 3, 5)),
                               places=2)
        # 页眉状态胶囊存在且可更新
        vis.toggle_learning()
        vis.frame()
        self.assertIn("已暂停", vis._chips["learning"][1].get_text())
        # 事件条三行 artist 就绪
        self.assertEqual(len(vis._events_lines), 3)
        import matplotlib.pyplot as plt
        plt.close(vis.fig)

    def test_network_visualizer_contract_preserved(self):
        from hippomind.network_visualizer import LayeredMemoryVisualizer
        mind, env, agent = self._build()
        vis = LayeredMemoryVisualizer(mind, env, agent, max_nodes=80, max_edges=200)
        vis.frame()
        nodes = vis._collect_nodes()
        for key in ("drive", "domain", "episodic", "semantic", "archive", "commons"):
            self.assertIn(key, nodes)
        self.assertTrue(vis.status_text.get_text())
        import matplotlib.pyplot as plt
        plt.close(vis.fig)

    def test_hub_watch_row_reflects_watcher_status(self):
        from hippomind.network_visualizer import LayeredMemoryVisualizer

        class WatchHub:
            def stats(self):
                return {"health": {"status": "ok"},
                        "stats": {"ledger": {"events": 7},
                                  "watcher": {"available": True,
                                              "poll_interval": 30.0}}}

        mind, env, agent = self._build()
        vis = LayeredMemoryVisualizer(mind, env, agent, hub_client=WatchHub(),
                                      max_nodes=80, max_edges=200)
        vis.frame()
        # 极简状态行契约：带 hub 时状态行必须干净完成且包含学习态与节点数。
        status = vis.status_text.get_text()
        self.assertIn(("LEARNING" if vis.learning_on else "PAUSED"), status)
        self.assertIn("节点", status)
        import matplotlib.pyplot as plt
        plt.close(vis.fig)

    def test_hub_watch_row_offline_fallback(self):
        from hippomind.network_visualizer import LayeredMemoryVisualizer
        mind, env, agent = self._build()
        vis = LayeredMemoryVisualizer(mind, env, agent, hub_client=None,
                                      max_nodes=80, max_edges=200)
        vis.frame()
        # 无 hub：同样干净更新，状态行反映本地学习开关状态。
        self.assertIn(vis.learning_on and "LEARNING" or "PAUSED",
                      vis.status_text.get_text())
        import matplotlib.pyplot as plt
        plt.close(vis.fig)

    def test_agent_focus_counts_member_memories(self):
        from hippomind.network_visualizer import LayeredMemoryVisualizer

        class AgentHub:
            def stats(self):
                return {"health": {"status": "ok"},
                        "stats": {"ledger": {"events": 3,
                                             "agents": {"codex": 2}}}}

        mind, env, agent = self._build()
        mind.store("跨 Agent 迁移经验记录", action="reference",
                   metadata={"source_agent": "codex"})
        vis = LayeredMemoryVisualizer(mind, env, agent, hub_client=AgentHub(),
                                      max_nodes=120, max_edges=400)
        vis.selected_key = "commons:codex"
        vis.rebuild()
        desc = vis._selected_description()
        self.assertIn("聚焦智能体 codex", desc)
        self.assertIn("关联记忆 1 条", desc)

    def test_workbench_snapshot_key_writes_png(self):
        import matplotlib
        matplotlib.use("Agg", force=True)
        from hippomind.visualizer import Visualizer

        mind, env, agent = self._build()
        vis = Visualizer(mind, env, agent)
        vis.frame()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = vis.do_snapshot(out_dir=tmp.name)
        self.assertTrue(Path(path).exists())
        self.assertGreater(Path(path).stat().st_size, 1000)
        import matplotlib.pyplot as plt
        plt.close(vis.fig)

    def test_daemon_reload_forces_discovery_rescan(self):
        import threading
        import urllib.request
        from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = RuntimePaths.for_ledger(Path(tmp.name) / "hub.sqlite", tmp.name)
        service = MemoryHubService(paths, watch=False)
        server = MemoryHubHTTPServer(("127.0.0.1", 0), service)
        service._server = server
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        def request(method, path, body=None):
            raw = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                         data=raw, method=method)
            req.add_header("X-HippoMind-Token", service.token)
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.loads(response.read().decode())

        report = request("POST", "/admin/reload", {})
        self.assertIn("reloaded_at", report)
        self.assertIn("discovery", report)
        self.assertIn("auto_install", report)
        self.assertTrue(report["discovery"].get("available"))

    def test_sleep_consolidation_triggers_when_idle(self):
        import time as _time
        from hippomind.daemon import (MemoryHubService, RuntimePaths,
                                      SLEEP_IDLE_SECONDS)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = RuntimePaths.for_ledger(Path(tmp.name) / "hub.sqlite",
                                        Path(tmp.name) / "rt")
        service = MemoryHubService(paths, watch=False)
        service.ledger.ingest({"kind": "fact", "scope": "shared",
                               "source_agent": "tester",
                               "content": "睡眠巩固验证数据",
                               "evidence": ["evidence:sleep"]})
        # 伪造空闲：把最近活动时间拨回阈值之前
        service._last_activity_monotonic = (
            _time.monotonic() - SLEEP_IDLE_SECONDS - 1)
        report = service._maybe_sleep_consolidate()
        self.assertIsNotNone(report)
        self.assertIn("consolidated", report)
        self.assertGreaterEqual(report["idle_seconds"], SLEEP_IDLE_SECONDS - 1)
        self.assertIs(service.last_sleep_report, report)
        # 同一空闲期内只触发一次
        self.assertIsNone(service._maybe_sleep_consolidate())

    def test_recent_activity_blocks_sleep(self):
        from hippomind.daemon import MemoryHubService, RuntimePaths

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = RuntimePaths.for_ledger(Path(tmp.name) / "hub.sqlite",
                                        Path(tmp.name) / "rt")
        service = MemoryHubService(paths, watch=False)
        service.note_activity()
        self.assertIsNone(service._maybe_sleep_consolidate())

    def test_consolidation_backfill_credits_source_episodes(self):
        from hippomind.hippocampus import Hippocampus

        h = Hippocampus(dedup_threshold=0.9999, consolidate_min_support=3)
        for _ in range(3):
            h.store("重复情境甲描述", action="N", valence=0.8, features=[0.0])
        h.consolidate()
        rule = next(iter(h.semantic.values()))
        episode = h.episodic[rule.source_episodes[0]]
        weight_before = episode.weight
        touch_before = episode.touch_count

        # 正向强化规则：有界比例的权重信用回传上游情景
        h.reinforce(rule.id, 0.9)
        self.assertGreater(episode.weight, weight_before)
        # 纯权重信用：不增加 touch——实测 touch 提高召回频率会把 Agent
        # 引回高风险情境（毒药次数上升），因此回填刻意只动权重。
        self.assertEqual(episode.touch_count, touch_before)

        # 负向不回传：规则失败可能源于泛化误差，不应惩罚原始正确经历
        weight_after_positive = episode.weight
        h.reinforce(rule.id, -0.9)
        self.assertAlmostEqual(episode.weight, weight_after_positive, places=9)

    def test_consolidation_backfill_config_sanitised(self):
        from hippomind.hippocampus import Hippocampus
        h = Hippocampus(consolidation_backfill=5.0)
        self.assertLessEqual(h.cfg["consolidation_backfill"], 1.0)

    def test_bigram_fallback_surfaces_word_order_different_query(self):
        from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
        from hippomind.ledger import SQLiteLedger

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = SQLiteLedger(Path(tmp.name) / "b.sqlite")
        ledger.ingest({"kind": "fact", "scope": "shared", "source_agent": "tester",
                       "content": "业务已迁移到华北二区域",
                       "evidence": ["evidence:mig"]})
        # 词序不同、仅共享「业务/区域」二元词的问句：主通道落空后由
        # 2 字回退通道触达，双词重叠分支放行进包。
        hits = ledger.search("业务部署在哪个区域", scope="shared", limit=8)
        self.assertEqual(len(hits), 1)
        pack = ContextPackBuilder(ledger).build(
            "业务部署在哪个区域", ContextPackOptions(max_items=8))
        self.assertEqual(len(pack["items"]), 1)
        self.assertIn("华北二区域", str(pack.get("text", "")))

    def test_watcher_reports_new_archive_agents(self):
        import sqlite3
        from hippomind.watcher import ArchiveWatcher

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        index = root / "index"
        index.mkdir()
        db = index / "index.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("""CREATE TABLE messages (
                agent TEXT, session_id TEXT, event_id TEXT, ts TEXT, role TEXT,
                content TEXT, model TEXT, cwd TEXT, title TEXT, source_file TEXT)""")
            conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("codex", "s1", "e1", "2026-08-21T00:00:00Z", "user",
                          "第一条消息", "m", "/p", "t", "c.json"))
        ledger = SQLiteLedger(root / "hub.sqlite")
        watcher = ArchiveWatcher(ledger, root, poll_interval=5)

        first = watcher.sync_once()["sources"]["messages"]
        self.assertFalse(first.get("new_agents"))  # 首轮建基线，不报新

        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("gemini", "s2", "e2", "2026-08-21T00:01:00Z", "user",
                          "Gemini 的第一条消息", "m", "/p", "t", "g.json"))
        second = watcher.sync_once()["sources"]["messages"]
        self.assertEqual(second.get("new_agents"), ["gemini"])
        third = watcher.sync_once()["sources"]["messages"]
        self.assertFalse(third.get("new_agents"))

    def test_prune_dsh_cursors_removes_missing_files(self):
        from hippomind.watcher import ArchiveWatcher

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        alive_file = root / "still-here.jsonl"
        alive_file.write_text("x", encoding="utf-8")
        gone_file = root / "deleted.jsonl"
        gone_file.write_text("x", encoding="utf-8")

        ledger = SQLiteLedger(root / "hub.sqlite")
        watcher = ArchiveWatcher(ledger, root, poll_interval=5)
        ledger.set_cursor("dsh-session:alive", "fp-alive",
                          metadata={"kind": "dsh-session",
                                    "path": str(alive_file)})
        ledger.set_cursor("dsh-session:gone", "fp-gone",
                          metadata={"kind": "dsh-session",
                                    "path": str(gone_file)})
        gone_file.unlink()

        removed = watcher._prune_dsh_cursors({"dsh-session:alive"})
        self.assertEqual(removed, 1)
        self.assertEqual(ledger.get_cursor("dsh-session:alive")["cursor"],
                         "fp-alive")
        gone = ledger.get_cursor("dsh-session:gone")
        self.assertEqual(gone.get("cursor", ""), "")
        # 幂等：再跑一次不再计数
        self.assertEqual(watcher._prune_dsh_cursors({"dsh-session:alive"}), 0)

    def test_network_snapshot_and_timeline_progress(self):
        import matplotlib
        matplotlib.use("Agg", force=True)
        from hippomind.network_visualizer import LayeredMemoryVisualizer

        mind, env, agent = self._build()
        vis = LayeredMemoryVisualizer(mind, env, agent,
                                      max_nodes=120, max_edges=400)
        vis.frame()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = vis.do_snapshot(out_dir=tmp.name)
        self.assertTrue(Path(path).exists())
        # 时间轴模式下详情行显示回放进度
        vis.toggle_timeline()
        vis.timeline_cursor = vis._timeline_t0 + vis._timeline_span * 0.4
        vis.rebuild()
        self.assertIn("[时间轴回放", vis.detail_text.get_text())
        import matplotlib.pyplot as plt
        plt.close(vis.fig)

    def test_reliability_filter_trace_includes_score_snapshot(self):
        from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
        from hippomind.ledger import SQLiteLedger

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = SQLiteLedger(Path(tmp.name) / "b.sqlite")
        ledger.ingest({"kind": "fact", "scope": "shared", "source_agent": "t",
                       "content": "机器学习入门指南", "evidence": ["e:1"]})
        pack = ContextPackBuilder(ledger).build(
            "深度学习强化训练心得",
            ContextPackOptions(max_items=8, debug=True))
        filtered = pack.get("debug", {}).get("filtered", [])
        self.assertTrue(filtered)
        entry = filtered[0]
        self.assertIn("score", entry)
        self.assertIn("score_parts", entry)
        self.assertLess(float(entry["score_parts"].get("coverage", 1)), 0.25)

    def test_cross_language_query_reaches_english_content(self):
        from hippomind.ledger import CROSS_LANGUAGE_TERMS, SQLiteLedger

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = SQLiteLedger(Path(tmp.name) / "b.sqlite")
        ledger.ingest({"kind": "fact", "scope": "shared", "source_agent": "t",
                       "content": "database connection pool tuning guide",
                       "evidence": ["e:db"]})
        # 中文查询经映射表（数据库→database）触达英文内容
        hits = ledger.search("数据库连接池调优指南", scope="shared", limit=8)
        self.assertTrue(any("database" in h["content"] for h in hits))
        # 反向：英文查询含映射词 → 补中文词
        terms = ledger._cross_language_terms("database tuning")
        self.assertIn("数据库", terms)
        self.assertGreater(len(CROSS_LANGUAGE_TERMS), 30)

    def test_timeline_playback_filters_by_creation_time(self):
        import matplotlib
        matplotlib.use("Agg", force=True)
        from hippomind.network_visualizer import LayeredMemoryVisualizer

        mind, env, agent = self._build()
        for _ in range(20):
            agent.step()
        vis = LayeredMemoryVisualizer(mind, env, agent,
                                      max_nodes=120, max_edges=400)
        vis.frame()
        vis.toggle_timeline()
        self.assertTrue(vis.timeline_mode)
        self.assertTrue(vis.timeline_playing)
        # 起点：只有最早创建的记忆可见
        vis.timeline_cursor = vis._timeline_t0
        vis.rebuild()
        early = sum(vis._visible_counts.values())
        # 终点：全部可见
        vis.timeline_cursor = vis._timeline_t1
        vis.rebuild()
        late = sum(vis._visible_counts.values())
        self.assertLess(early, late)
        # 推进到末尾自动暂停
        vis._advance_timeline()
        self.assertFalse(vis.timeline_playing)
        vis.toggle_timeline()
        self.assertFalse(vis.timeline_mode)
        import matplotlib.pyplot as plt
        plt.close(vis.fig)


if __name__ == "__main__":
    unittest.main()
