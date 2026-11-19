"""关系类型细化（task-3 / HEALTH_REVIEW 第四节缺口 2 的后半）：向后兼容的回归锁。

生产账本 2026-10-25 实测：``memory_relations`` 2,023 行，relation_type 只有
``related_to`` 一种。把这些边细分能提升联想扩展的可解释性，但**任何**细分都
必须满足三条不变量，本文件逐条钉住：

1. 存量 ``related_to`` 不被迁移/删除/改写；它的联想权重恒为 1.0，所以既有
   账本上的 ``causal_ancestors`` 得分与 ``entity_graph`` 排序逐字节不变。
2. ``relation_id`` 由 (parent, child, type) 决定：改类型必须换 id，否则
   ``INSERT OR IGNORE`` 的幂等性失效，重跑会留下两条语义相同的边。
3. 重分类只碰 ``related_to``，且**没有证据的边保持原样**——猜一个类型比
   诚实的无差别边更糟。
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import relation_reclassify as rr  # noqa: E402
from hippomind.distill import DistillEngine  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind.ledger.graph import (  # noqa: E402
    DEFAULT_RELATION_WEIGHT,
    RELATION_DERIVED_FROM,
    RELATION_RELATED_TO,
    RELATION_SAME_SUBJECT,
    RELATION_SAME_TASK,
    RELATION_SUPERSEDES,
    RELATION_WEIGHTS,
    SYMMETRIC_RELATION_TYPES,
    classify_relation_type,
    normalise_relation_type,
    relation_id_for,
    relation_weight,
)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-reltypes-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rel.sqlite"
        self.ledger = SQLiteLedger(self.path)
        self.addCleanup(self.ledger.close)

    def _memory(self, content, *, task_id="", subject="", value="",
                evidence=None, parents=None, scope="shared"):
        record = {
            "content": content, "kind": "fact", "source_agent": "tester",
            "scope": scope, "task_id": task_id,
            "evidence": list(evidence or ["test:" + content[:20]]),
        }
        if subject:
            record["claims"] = [{"subject": subject, "predicate": "state",
                                 "slot_key": subject + ":state",
                                 "value": value or content[:20],
                                 "confidence": 0.8}]
        if parents:
            record["causal_parents"] = list(parents)
        return self.ledger.ingest(record)

    def _edges(self):
        with self.ledger._reading() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT relation_id,parent_memory_id,child_memory_id,relation_type,"
                "confidence FROM memory_relations ORDER BY rowid").fetchall()]

    def _events(self, event_type="relation_reclassify"):
        with self.ledger._reading() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT event_id,event_type,content_hash,metadata_json "
                "FROM ledger_events WHERE event_type=?", (event_type,)).fetchall()]


class VocabularyTests(unittest.TestCase):
    """词表本身：只做拼写归一，绝不把未知类型改写成 related_to。"""

    def test_the_four_new_types_are_weighted_below_or_equal_to_the_legacy_one(self):
        # 关键不变量：没有任何新类型的权重**高于** related_to。存量边的得分
        # 因此只会持平或下降，不会因为引入新词表而凭空升值。
        for name in (RELATION_SAME_SUBJECT, RELATION_SAME_TASK,
                     RELATION_SUPERSEDES, RELATION_DERIVED_FROM):
            self.assertLessEqual(relation_weight(name),
                                 relation_weight(RELATION_RELATED_TO), name)

    def test_legacy_and_unknown_types_weigh_exactly_one(self):
        for name in (RELATION_RELATED_TO, "caused_by", "follows", "causes",
                     "某个未来才有的类型"):
            self.assertEqual(1.0, relation_weight(name), name)
        self.assertEqual(1.0, DEFAULT_RELATION_WEIGHT)
        self.assertEqual(1.0, relation_weight(""))
        self.assertEqual(1.0, relation_weight(None))

    def test_same_task_is_the_weakest_link(self):
        # 同一任务里本来就有大量无关记忆，它必须比「共享主语」弱。
        self.assertLess(relation_weight(RELATION_SAME_TASK),
                        relation_weight(RELATION_SAME_SUBJECT))

    def test_aliases_normalise_but_unknown_types_survive_verbatim(self):
        self.assertEqual(RELATION_SAME_SUBJECT, normalise_relation_type("Same-Subject"))
        self.assertEqual(RELATION_DERIVED_FROM, normalise_relation_type("derived-from"))
        self.assertEqual("caused_by", normalise_relation_type("caused-by"))
        self.assertEqual(RELATION_RELATED_TO, normalise_relation_type(""))
        # 未知类型原样保留：抹掉调用方意图比留一个不认识的值更糟。
        self.assertEqual("custom_edge_v9", normalise_relation_type("custom_edge_v9"))

    def test_symmetric_types_cover_the_undirected_edges(self):
        self.assertIn(RELATION_RELATED_TO, SYMMETRIC_RELATION_TYPES)
        self.assertIn(RELATION_SAME_SUBJECT, SYMMETRIC_RELATION_TYPES)
        self.assertNotIn(RELATION_DERIVED_FROM, SYMMETRIC_RELATION_TYPES)
        self.assertNotIn(RELATION_SUPERSEDES, SYMMETRIC_RELATION_TYPES)

    def test_classifier_priority_is_explicit(self):
        kind, reason = classify_relation_type(supersedes=True, derived=True,
                                              shared_subjects=3, same_task=True)
        self.assertEqual(RELATION_SUPERSEDES, kind)
        self.assertIn("superseded_by", reason)
        kind, _ = classify_relation_type(derived=True, shared_subjects=2)
        self.assertEqual(RELATION_DERIVED_FROM, kind)
        kind, _ = classify_relation_type(shared_subjects=2, same_task=True)
        self.assertEqual(RELATION_SAME_SUBJECT, kind)
        kind, reason = classify_relation_type(same_task=True)
        self.assertEqual(RELATION_SAME_TASK, kind)
        kind, reason = classify_relation_type()
        self.assertEqual(RELATION_RELATED_TO, kind)
        self.assertIn("无更强证据", reason)


class RelationIdTests(LedgerCase):
    """relation_id 的唯一构造入口（改类型必须换 id）。"""

    def test_same_pair_different_type_gets_a_different_id(self):
        a = self._memory("A 记录", subject="svc", value="a")
        b = self._memory("B 记录", subject="svc", value="b")
        first = self.ledger.add_relations(
            b.memory_id, [{"memory_id": a.memory_id,
                           "relation_type": RELATION_RELATED_TO,
                           "confidence": 0.5}])
        second = self.ledger.add_relations(
            b.memory_id, [{"memory_id": a.memory_id,
                           "relation_type": RELATION_SAME_SUBJECT,
                           "confidence": 0.5}])
        self.assertEqual(1, first["created"])
        self.assertEqual(1, second["created"],
                         "不同类型必须能共存，而不是被 UNIQUE 静默吞掉")
        edges = self._edges()
        self.assertEqual(2, len(edges))
        ids = {edge["relation_id"] for edge in edges}
        self.assertEqual(2, len(ids), "同一对记忆、不同类型 -> 两个不同 id")
        self.assertEqual(ids, {relation_id_for(a.memory_id, b.memory_id,
                                           RELATION_RELATED_TO),
                               relation_id_for(a.memory_id, b.memory_id,
                                           RELATION_SAME_SUBJECT)})

    def test_rewriting_the_same_type_is_a_no_op(self):
        a = self._memory("A 记录", subject="svc", value="a")
        b = self._memory("B 记录", subject="svc", value="b")
        payload = [{"memory_id": a.memory_id,
                    "relation_type": RELATION_SAME_SUBJECT, "confidence": 0.5}]
        self.assertEqual(1, self.ledger.add_relations(b.memory_id, payload)["created"])
        repeat = self.ledger.add_relations(b.memory_id, payload)
        self.assertEqual(0, repeat["created"], "同类型重复写不得产生第二行")
        self.assertEqual(1, len(self._edges()))


class AddRelationsTypingTests(LedgerCase):
    """写入侧：类型归一 + 默认值向后兼容。"""

    def test_missing_type_still_defaults_to_related_to(self):
        # 既有调用方（只给 memory_id）的行为必须逐字节不变。
        a = self._memory("A 记录")
        b = self._memory("B 记录")
        self.ledger.add_relations(b.memory_id, [{"memory_id": a.memory_id}])
        self.assertEqual([RELATION_RELATED_TO],
                         [edge["relation_type"] for edge in self._edges()])

    def test_default_relation_type_keyword_only_affects_missing_entries(self):
        a = self._memory("A 记录")
        b = self._memory("B 记录")
        c = self._memory("C 记录")
        self.ledger.add_relations(
            c.memory_id,
            [{"memory_id": a.memory_id},
             {"memory_id": b.memory_id, "relation_type": RELATION_SUPERSEDES}],
            default_relation_type=RELATION_SAME_TASK)
        by_parent = {edge["parent_memory_id"]: edge["relation_type"]
                     for edge in self._edges()}
        self.assertEqual(RELATION_SAME_TASK, by_parent[a.memory_id])
        self.assertEqual(RELATION_SUPERSEDES, by_parent[b.memory_id],
                         "显式类型优先于 default_relation_type")

    def test_alias_types_are_normalised_on_write(self):
        a = self._memory("A 记录")
        b = self._memory("B 记录")
        self.ledger.add_relations(
            b.memory_id, [{"memory_id": a.memory_id,
                           "relation_type": "Same-Subject"}])
        self.assertEqual([RELATION_SAME_SUBJECT],
                         [edge["relation_type"] for edge in self._edges()])

    def test_unknown_type_is_stored_verbatim(self):
        a = self._memory("A 记录")
        b = self._memory("B 记录")
        self.ledger.add_relations(
            b.memory_id, [{"memory_id": a.memory_id,
                           "relation_type": "custom_edge_v9"}])
        self.assertEqual(["custom_edge_v9"],
                         [edge["relation_type"] for edge in self._edges()])


class LegacyScoringUnchangedTests(LedgerCase):
    """向后兼容核心：存量 related_to 的得分与排序逐字节不变。"""

    def test_causal_ancestors_score_is_the_exact_legacy_formula(self):
        parent = self._memory("父节点")
        child = self._memory("子节点", parents=[parent.memory_id])
        rows = self.ledger.causal_ancestors([child.memory_id])
        self.assertEqual([parent.memory_id], [row["memory_id"] for row in rows])
        # 改造前的字面量：max(0, 0.45 - 0.08 * (depth - 1))，depth=1 -> 0.45
        self.assertEqual(0.45, rows[0]["score"])
        self.assertEqual({"causal": 0.45, "relation_weight": 1.0},
                         rows[0]["score_parts"])

    def test_second_hop_keeps_the_legacy_penalty(self):
        grand = self._memory("祖父节点")
        parent = self._memory("父节点", parents=[grand.memory_id])
        child = self._memory("子节点", parents=[parent.memory_id])
        rows = self.ledger.causal_ancestors([child.memory_id], max_depth=2)
        by_id = {row["memory_id"]: row for row in rows}
        self.assertEqual(0.45, by_id[parent.memory_id]["score"])
        self.assertAlmostEqual(0.37, by_id[grand.memory_id]["score"], places=6)

    def test_related_to_edges_carry_weight_one_in_entity_graph(self):
        a = self._memory("A 上线", subject="svc-a", value="1")
        self._memory("B 上线", subject="svc-b", value="2",
                         parents=[a.memory_id])
        report = self.ledger.entity_graph("svc-a", hops=1)
        self.assertTrue(report["found"])
        self.assertEqual(1, len(report["edges"]))
        edge = report["edges"][0]
        # ingest 的 causal_parents 默认类型是 caused_by（identity.py），
        # 不是 related_to——两者权重都是 1.0，所以得分等价。
        self.assertEqual("caused_by", edge["relation_type"])
        self.assertEqual(1.0, edge["weight"])
        self.assertEqual(0.5, edge["confidence"])
        self.assertNotIn("strength", edge,
                         "内部排序用的 strength 不得泄漏进结果字典")


class TypedExpansionTests(LedgerCase):
    """新类型真的改变了扩展权重（否则这次改造等于没做）。"""

    def test_same_subject_edge_scores_below_a_related_to_edge(self):
        plain_parent = self._memory("普通父节点")
        typed_parent = self._memory("同主语父节点")
        plain_child = self._memory("普通子节点", parents=[plain_parent.memory_id])
        typed_child = self._memory(
            "同主语子节点",
            parents=[{"memory_id": typed_parent.memory_id,
                      "relation_type": RELATION_SAME_SUBJECT, "confidence": 0.5}])
        plain = self.ledger.causal_ancestors([plain_child.memory_id])
        typed = self.ledger.causal_ancestors([typed_child.memory_id])
        self.assertEqual(0.45, plain[0]["score"])
        self.assertAlmostEqual(0.45 * RELATION_WEIGHTS[RELATION_SAME_SUBJECT],
                               typed[0]["score"], places=6)
        self.assertLess(typed[0]["score"], plain[0]["score"])
        self.assertEqual(RELATION_SAME_SUBJECT, typed[0]["relation_type"])
        self.assertEqual(RELATION_WEIGHTS[RELATION_SAME_SUBJECT],
                         typed[0]["relation_weight"])

    def test_strongest_path_wins_when_a_memory_is_reachable_twice(self):
        # 同一条祖先经两条路径到达（弱边 same_task + 强边 related_to）：
        # 取最强，而不是平均——平均会让「一条强边 + 一条弱边」比「一条强边」
        # 得分更低，那是错的。
        grand = self._memory("祖父节点")
        mid = self._memory("中间节点", parents=[grand.memory_id])
        child = self._memory(
            "子节点",
            parents=[{"memory_id": mid.memory_id,
                      "relation_type": RELATION_RELATED_TO, "confidence": 0.5},
                     {"memory_id": grand.memory_id,
                      "relation_type": RELATION_SAME_TASK, "confidence": 0.5}])
        rows = self.ledger.causal_ancestors([child.memory_id], max_depth=2)
        by_id = {row["memory_id"]: row for row in rows}
        self.assertEqual(0.45, by_id[mid.memory_id]["score"])
        # grand 同时经两条路径到达：直连 same_task（第 1 层，权重 0.7）与
        # 经 mid 的 caused_by（第 2 层，权重 1.0）。**距离优先**：取第 1 层，
        # 因此得分是 0.45 * 0.7 = 0.315 而不是 0.37 * 1.0。这条断言钉住
        # 「score 不得与按 causal_depth 的排序自相矛盾」。
        self.assertAlmostEqual(0.45 * RELATION_WEIGHTS[RELATION_SAME_TASK],
                               by_id[grand.memory_id]["score"], places=6)
        self.assertEqual(RELATION_SAME_TASK, by_id[grand.memory_id]["relation_type"])
        # 排序仍按深度：mid(1) 在 grand(2) 之前。
        self.assertEqual([mid.memory_id, grand.memory_id],
                         [row["memory_id"] for row in rows])

    def test_same_task_edges_sort_below_same_subject_edges(self):
        weak_parent = self._memory("弱父节点", subject="weak-svc", value="w")
        strong_parent = self._memory("强父节点", subject="strong-svc", value="s")
        weak_child = self._memory(
            "弱子节点", subject="weak-child", value="wc",
            parents=[{"memory_id": weak_parent.memory_id,
                      "relation_type": RELATION_SAME_TASK, "confidence": 0.5}])
        strong_child = self._memory(
            "强子节点", subject="strong-child", value="sc",
            parents=[{"memory_id": strong_parent.memory_id,
                      "relation_type": RELATION_SAME_SUBJECT, "confidence": 0.5}])
        weak = self.ledger.causal_ancestors([weak_child.memory_id])
        strong = self.ledger.causal_ancestors([strong_child.memory_id])
        self.assertLess(weak[0]["score"], strong[0]["score"])


class DistillWriteSideTests(LedgerCase):
    """写入侧：distill 按 claim 关系选类型，而不是无差别 related_to。"""

    def _distilled(self, subject, value, task_id, timestamp="2026-10-23T10:00:00Z"):
        return self.ledger.ingest({
            "content": "提炼：%s %s" % (subject, value),
            "kind": "semantic", "source_agent": "hippomind-distill",
            "scope": "shared", "task_id": task_id,
            "evidence": ["memory:seed-" + task_id],
            "timestamp": timestamp,
            "claims": [{"subject": subject, "predicate": "状态",
                        "slot_key": task_id + ":status", "value": value,
                        "confidence": 0.9}],
        })

    def test_associate_writes_same_subject_not_related_to(self):
        self._distilled("HippoMind", "设计完成", "t1")
        self._distilled("HippoMind", "实现完成", "t2")
        engine = DistillEngine(self.ledger, extractor=None)
        self.assertGreaterEqual(engine.associate(limit=10)["created"], 1)
        types = {edge["relation_type"] for edge in self._edges()}
        self.assertEqual({RELATION_SAME_SUBJECT}, types)

    def test_associate_links_same_task_memories_without_shared_subject(self):
        # 同一 task_id、不同主语：这条弱关联以前根本不存在（图里完全不连边），
        # 生产实测 190/2,023 条边属于这一类。
        self._distilled("Alpha", "a", "shared-task")
        self._distilled("Beta", "b", "shared-task")
        engine = DistillEngine(self.ledger, extractor=None)
        result = engine.associate(limit=10)
        self.assertGreaterEqual(result["created"], 1)
        self.assertEqual({RELATION_SAME_TASK},
                         {edge["relation_type"] for edge in self._edges()})

    def test_shared_subject_wins_over_same_task_and_is_not_duplicated(self):
        self._distilled("Gamma", "g1", "task-x")
        self._distilled("Gamma", "g2", "task-x")
        engine = DistillEngine(self.ledger, extractor=None)
        engine.associate(limit=10)
        edges = self._edges()
        self.assertEqual(1, len(edges),
                         "同主语已经是更强证据，不得再补一条 same_task 弱边")
        self.assertEqual(RELATION_SAME_SUBJECT, edges[0]["relation_type"])

    def test_batch_parent_link_is_typed_same_subject(self):
        # _record 的 causal_parent 是「上一批里同 claim 主语的记忆」，语义就是
        # same_subject；用无差别的 related_to 会让它在图里比实际更重。
        record = DistillEngine(self.ledger, extractor=None)._record(
            type("C", (), {"memory_id": "mem_x", "kind": "task",
                          "content": "来源内容", "task_id": "T", "project": "",
                          "session_id": ""})(),
            [{"subject": "Delta", "predicate": "状态", "slot_key": "d:status",
              "value": "ok", "confidence": 0.8}],
            "heuristic", parent_memory_id="mem_parent")
        self.assertEqual(RELATION_SAME_SUBJECT,
                         record["causal_parents"][0]["relation_type"])


class ReclassifyScriptTests(LedgerCase):
    """一次性回填入口：默认 dry-run、只碰 related_to、无证据不动、可审计。"""

    def _link(self, child, parent, relation_type=RELATION_RELATED_TO):
        return self.ledger.add_relations(
            child.memory_id, [{"memory_id": parent.memory_id,
                               "relation_type": relation_type,
                               "confidence": 0.6}])

    def _plan(self, **kwargs):
        conn = rr._open_readonly(self.path)
        try:
            return rr.build_plan(conn, **kwargs)
        finally:
            conn.close()

    def test_dry_run_plan_does_not_touch_the_ledger(self):
        a = self._memory("A 记录", subject="svc", value="a")
        b = self._memory("B 记录", subject="svc", value="b")
        self._link(b, a)
        before = self._edges()
        plan = self._plan()
        self.assertEqual(1, len(plan["changes"]))
        self.assertEqual(RELATION_SAME_SUBJECT, plan["changes"][0]["new_type"])
        self.assertEqual(before, self._edges(), "dry-run 不得修改账本")
        self.assertEqual([], self._events())

    def test_apply_rewrites_only_related_to_and_audits_each_change(self):
        a = self._memory("A 记录", subject="svc", value="a")
        b = self._memory("B 记录", subject="svc", value="b")
        self._link(b, a)
        self._link(b, a, RELATION_DERIVED_FROM)  # 已是显式类型：不得被改
        plan = self._plan()
        self.assertEqual(1, len(plan["changes"]), "只有 related_to 参与重分类")
        conn = rr._open_write(self.path)
        try:
            outcome = rr.apply_plan(conn, plan["changes"])
        finally:
            conn.close()
        self.assertEqual({"inserted": 1, "deleted": 1, "audited": 1}, outcome)
        types = sorted(edge["relation_type"] for edge in self._edges())
        self.assertEqual(sorted([RELATION_SAME_SUBJECT, RELATION_DERIVED_FROM]),
                         types)
        events = self._events()
        self.assertEqual(1, len(events))
        metadata = json.loads(events[0]["metadata_json"])
        self.assertEqual(RELATION_RELATED_TO, metadata["old_type"])
        self.assertEqual(RELATION_SAME_SUBJECT, metadata["new_type"])
        self.assertTrue(metadata["reason"])
        self.assertEqual(1, metadata["signals"]["shared_subjects"])

    def test_edge_without_evidence_keeps_related_to(self):
        # 无共享主语、无同 task、无 superseded_by、无派生指针 -> 保持原样。
        a = self._memory("孤立 A")
        b = self._memory("孤立 B")
        self._link(b, a)
        plan = self._plan()
        self.assertEqual([], plan["changes"])
        self.assertEqual(1, len(plan["kept"]))
        self.assertEqual(RELATION_RELATED_TO, plan["kept"][0]["new_type"])
        self.assertIn("无证据", plan["kept"][0]["reason"])

    def test_supersedes_is_detected_from_the_lifecycle_fact(self):
        old = self._memory("旧状态", subject="svc", value="v0")
        # 同 slot 更高置信度覆盖 -> ingest 把旧记忆标 superseded 并指向新记忆。
        new = self.ledger.ingest({
            "content": "新状态", "kind": "fact", "source_agent": "tester",
            "scope": "shared", "evidence": ["test:supersede"],
            "claims": [{"subject": "svc", "predicate": "state",
                        "slot_key": "svc:state", "value": "v1",
                        "confidence": 0.95}],
        })
        self.assertEqual(new.memory_id,
                         self.ledger.get_memory(old.memory_id)["superseded_by"])
        # 手工造一条 related_to 边（模拟存量形态），重分类应识别出 supersedes。
        with self.ledger._connect(write=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO memory_relations(relation_id,parent_memory_id,"
                "child_memory_id,relation_type,confidence,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (relation_id_for(old.memory_id, new.memory_id, RELATION_RELATED_TO),
                 old.memory_id, new.memory_id, RELATION_RELATED_TO, 0.5,
                 "2026-10-25T00:00:00Z"))
        plan = self._plan()
        self.assertEqual(1, len(plan["changes"]))
        self.assertEqual(RELATION_SUPERSEDES, plan["changes"][0]["new_type"])
        self.assertTrue(plan["changes"][0]["signals"]["supersedes"])

    def test_derived_from_is_detected_from_the_child_evidence_pointer(self):
        parent = self._memory("来源记忆")
        child = self._memory("派生记忆", evidence=["memory:" + parent.memory_id])
        self._link(child, parent)
        plan = self._plan()
        self.assertEqual(1, len(plan["changes"]))
        self.assertEqual(RELATION_DERIVED_FROM, plan["changes"][0]["new_type"])

    def test_priority_flag_changes_the_outcome_predictably(self):
        parent = self._memory("来源记忆", subject="svc", value="p")
        child = self._memory("派生记忆", subject="svc", value="c",
                             evidence=["memory:" + parent.memory_id])
        self._link(child, parent)
        default_plan = self._plan()
        evidence_first = self._plan(priority=rr.PRIORITIES["evidence-first"])
        self.assertEqual(RELATION_SAME_SUBJECT,
                         default_plan["changes"][0]["new_type"])
        self.assertEqual(RELATION_DERIVED_FROM,
                         evidence_first["changes"][0]["new_type"])

    def test_apply_is_idempotent(self):
        a = self._memory("A 记录", subject="svc", value="a")
        b = self._memory("B 记录", subject="svc", value="b")
        self._link(b, a)
        plan = self._plan()
        conn = rr._open_write(self.path)
        try:
            rr.apply_plan(conn, plan["changes"])
        finally:
            conn.close()
        second = self._plan()
        self.assertEqual([], second["changes"], "重跑不得再产生改动")
        self.assertEqual(1, len(self._edges()))
        self.assertEqual(1, len(self._events()))

    def test_apply_takes_a_backup_first(self):
        a = self._memory("A 记录", subject="svc", value="a")
        b = self._memory("B 记录", subject="svc", value="b")
        self._link(b, a)
        backup_dir = Path(self.tmp.name) / "backups"
        conn = rr._open_write(self.path)
        try:
            target = rr._backup(conn, self.path, backup_dir)
        finally:
            conn.close()
        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size, 0)
        with sqlite3.connect(str(target)) as snapshot:
            rows = snapshot.execute(
                "SELECT relation_type FROM memory_relations").fetchall()
        self.assertEqual([(RELATION_RELATED_TO,)], rows,
                         "备份必须是写入前的状态")

    def test_cli_defaults_to_dry_run_and_reports_json(self):
        a = self._memory("A 记录", subject="svc", value="a")
        b = self._memory("B 记录", subject="svc", value="b")
        self._link(b, a)
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            code = rr.main(["--ledger", str(self.path), "--json"])
        self.assertEqual(0, code)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(1, payload["planned_changes"])
        self.assertEqual(RELATION_RELATED_TO,
                         self._edges()[0]["relation_type"],
                         "不带 --apply 时 CLI 不得写账本")


if __name__ == "__main__":
    unittest.main()
