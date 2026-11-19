"""关系重分类影响报告（scripts/relation_impact.py）的回归锁。

为什么这个工具需要测试：它是「不可逆的排序变更」在 apply 之前的唯一证据来源。
如果它自己静默算错（比如拿同一份账本当 before 和 after，或者把权重变化算成 0），
apply 就会在「预期校验通过」的假象下被执行——比没有报告更糟。

写这些测试时踩到的两个真实约束，一并记在这里：

1. **同主语的两条记忆必须用不同的 slot_key**，否则 ingest 会把它们判成同一个
   slot 的冲突/取代，边根本不会存在。
2. **`same_subject` 边在 `entity_graph` 里结构性不可见**：两端记忆共享同一个
   claim 主语 -> 映射到同一个实体节点 -> `if a == b: continue` 跳过。所以验证
   「图里权重变了」必须用 `same_task` 边（两端不同主语）；`same_subject` 的
   影响只能从 `causal_ancestors` 侧看到。

这里的测试全部在临时账本上跑，不碰生产库。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import relation_impact as ri  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind.ledger.graph import (  # noqa: E402
    RELATION_RELATED_TO,
    RELATION_SAME_SUBJECT,
    RELATION_SAME_TASK,
    relation_weight,
)


class ImpactCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-impact-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "src.sqlite"
        self.ledger = SQLiteLedger(self.path)
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.ledger.close()
        except Exception:
            pass

    def _memory(self, content, *, subject, slot="", value="", task_id=""):
        """One memory with one claim.  ``slot`` must differ between memories
        that share a subject, or ingest treats them as the same slot."""
        record = {"content": content, "kind": "fact", "source_agent": "tester",
                  "scope": "shared", "task_id": task_id,
                  "evidence": ["test:" + content[:20]],
                  "claims": [{"subject": subject, "predicate": "state",
                              "slot_key": slot or (subject + ":" + content[:8]),
                              "value": value or content[:20],
                              "confidence": 0.8}]}
        return self.ledger.ingest(record)

    def _link(self, child, parent, relation_type=RELATION_RELATED_TO):
        return self.ledger.add_relations(
            child.memory_id, [{"memory_id": parent.memory_id,
                               "relation_type": relation_type,
                               "confidence": 0.6}])

    def _snapshot(self, name="before.sqlite"):
        """Snapshot the writer into a separate file the report can read."""
        target = self.dir / name
        if target.exists():
            target.unlink()
        ri.snapshot(self.path, target)
        return target

    @staticmethod
    def _types(path):
        conn = sqlite3.connect("file:%s?mode=ro" % str(path), uri=True)
        try:
            return sorted(row[0] for row in conn.execute(
                "SELECT relation_type FROM memory_relations").fetchall())
        finally:
            conn.close()


class SnapshotTests(ImpactCase):
    """快照与副本：不能碰源库，且必须能被只读打开。"""

    def test_snapshot_does_not_modify_the_source(self):
        self._memory("源库样本", subject="svc-a")
        before = self.path.stat().st_mtime_ns
        target = self.dir / "snap.sqlite"
        ri.snapshot(self.path, target)
        self.assertTrue(target.is_file())
        self.assertEqual(before, self.path.stat().st_mtime_ns,
                         "取快照不得修改源库")

    def test_snapshot_is_readable_read_only(self):
        # 回归锁定：WAL 模式的库没有 -shm 时 mode=ro 打不开，必须归一化 journal。
        self._memory("可读样本", subject="svc-a")
        target = self.dir / "snap.sqlite"
        ri.snapshot(self.path, target)
        conn = ri._readonly_conn(target)
        try:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM memories").fetchone()[0])
        finally:
            conn.close()

    def test_refuses_to_overwrite_an_existing_snapshot(self):
        self._memory("重复快照样本", subject="svc-a")
        target = self.dir / "snap.sqlite"
        ri.snapshot(self.path, target)
        with self.assertRaises(SystemExit):
            ri.snapshot(self.path, target)


class SimulationTests(ImpactCase):
    """模拟 apply：必须走真实写入路径，且真的改变类型分布。"""

    def test_simulation_rewrites_only_evidenced_edges(self):
        shared_a = self._memory("共享主语 A", subject="svc-shared", slot="s:alpha")
        shared_b = self._memory("共享主语 B", subject="svc-shared", slot="s:beta")
        lonely_a = self._memory("无证据 A", subject="svc-lonely", slot="l:alpha")
        lonely_b = self._memory("无证据 B", subject="svc-other", slot="o:beta")
        self._link(shared_b, shared_a)
        self._link(lonely_b, lonely_a)
        target = self.dir / "after.sqlite"
        outcome = ri.simulate_reclassify(self.path, target)
        self.assertEqual(1, outcome["planned"])
        self.assertEqual(1, outcome["applied"]["inserted"])
        self.assertEqual(1, outcome["applied"]["deleted"])
        self.assertEqual(1, outcome["applied"]["audited"])
        self.assertEqual({RELATION_SAME_SUBJECT: 1, RELATION_RELATED_TO: 1},
                         outcome["by_target"])
        self.assertEqual(sorted([RELATION_SAME_SUBJECT, RELATION_RELATED_TO]),
                         self._types(target))

    def test_simulation_leaves_the_source_untouched(self):
        a = self._memory("源 A", subject="svc-shared", slot="s:alpha")
        b = self._memory("源 B", subject="svc-shared", slot="s:beta")
        self._link(b, a)
        self.assertEqual([RELATION_RELATED_TO], self._types(self.path))
        target = self.dir / "after.sqlite"
        ri.simulate_reclassify(self.path, target)
        self.assertEqual([RELATION_RELATED_TO], self._types(self.path),
                         "模拟只能写副本，源库必须原样")


class DiffTests(ImpactCase):
    """diff 逻辑本身：权重变化、顺序变化、以及「无变化」都要能分辨。"""

    def _same_subject_pair(self):
        a = self._memory("同主语 A", subject="svc-shared", slot="s:alpha")
        b = self._memory("同主语 B", subject="svc-shared", slot="s:beta")
        self._link(b, a)
        return a, b

    def _same_task_pair(self):
        a = self._memory("同任务 A", subject="svc-task-a", slot="a:state",
                         task_id="T1")
        b = self._memory("同任务 B", subject="svc-task-b", slot="b:state",
                         task_id="T1")
        self._link(b, a)
        return a, b

    def test_identical_ledgers_report_no_change(self):
        self._memory("稳定样本", subject="svc-shared")
        before = self._snapshot("b.sqlite")
        after = self.dir / "a.sqlite"
        shutil.copyfile(str(before), str(after))
        ri._normalise_journal_mode(after)
        report = ri.build_report(before, after, subjects=5, memories=5)
        self.assertEqual(0, report["summary"]["changed_edges"])
        self.assertEqual(0, report["summary"]["changed_ancestors"])
        self.assertEqual([], ri.check_expectations(report))

    def test_report_detects_the_weight_drop_on_a_same_task_edge(self):
        # same_task 的两端主语不同 -> 在图里是两个节点 -> 边可见。
        self._same_task_pair()
        before = self._snapshot("b.sqlite")
        after = self.dir / "a.sqlite"
        ri.simulate_reclassify(before, after)
        report = ri.build_report(before, after, subjects=5, memories=5)
        self.assertEqual(1, report["summary"]["changed_edges"])
        change = report["subjects"][0]["changed_edges"][0]
        self.assertEqual(RELATION_RELATED_TO, change["type_before"])
        self.assertEqual(RELATION_SAME_TASK, change["type_after"])
        self.assertEqual(1.0, change["weight_before"])
        self.assertEqual(0.7, change["weight_after"])
        self.assertEqual([], ri.check_expectations(report))

    def test_report_detects_the_ancestor_score_drop(self):
        a, b = self._same_subject_pair()
        before = self._snapshot("b.sqlite")
        after = self.dir / "a.sqlite"
        ri.simulate_reclassify(before, after)
        report = ri.build_report(before, after, subjects=5, memories=5)
        changed = report["memories"][0]["changed_ancestors"]
        self.assertTrue(changed, "同主语边必须让祖先分数下降")
        self.assertAlmostEqual(-0.045, changed[0]["delta"], places=6)
        self.assertEqual(0.405, changed[0]["score_after"])
        self.assertEqual(0.45, changed[0]["score_before"])
        self.assertEqual(RELATION_SAME_SUBJECT, changed[0]["type_after"])

    def test_same_subject_edges_are_invisible_in_entity_graph(self):
        """实测契约：同主语边在 entity_graph 里是自环，被跳过。

        这条不是「已知缺陷」，是必须写进报告的**影响范围**：585 条
        same_subject 改写对图视图的影响是 0，只在 causal_ancestors 侧可见。
        如果哪天 entity_graph 改成不跳自环，这条测试会红，提醒重写报告。
        """
        self._same_subject_pair()
        before = self._snapshot("b.sqlite")
        after = self.dir / "a.sqlite"
        ri.simulate_reclassify(before, after)
        ledger = SQLiteLedger(after)
        try:
            snapshot = ri.graph_snapshot(ledger, "svc-shared")
        finally:
            ledger.close()
        self.assertEqual([], snapshot["edges"],
                         "同主语边两端映射到同一节点，entity_graph 跳过")
        visibility = ri.count_invisible_edges(before, after)
        self.assertEqual({RELATION_SAME_SUBJECT: 1}, visibility["hidden"])
        self.assertEqual({}, visibility["visible"])

    def test_same_task_edges_are_visible_in_entity_graph(self):
        self._same_task_pair()
        before = self._snapshot("b.sqlite")
        after = self.dir / "a.sqlite"
        ri.simulate_reclassify(before, after)
        visibility = ri.count_invisible_edges(before, after)
        self.assertEqual({}, visibility["hidden"])
        self.assertEqual({RELATION_SAME_TASK: 1}, visibility["visible"])


    def test_pair_dedupe_is_reported_not_hidden(self):
        """回归锁定生产那次「622 计划 / 583 新增」的形态。

        同一对记忆先有 related_to、后有 same_subject（新 distill 路径写入的），
        重分类时 INSERT OR IGNORE 命中已有 same_subject -> 只删旧 related_to，
        于是边数下降而记忆对数不变。报告必须把这件事说清楚，否则「边数 -39」
        会被读成数据丢失。
        """
        a = self._memory("冗余 A", subject="svc-shared", slot="s:alpha")
        b = self._memory("冗余 B", subject="svc-shared", slot="s:beta")
        self._link(b, a, RELATION_RELATED_TO)
        self._link(b, a, RELATION_SAME_SUBJECT)
        self.assertEqual(2, len(self._types(self.path)))
        before = self._snapshot("b.sqlite")
        after = self.dir / "a.sqlite"
        outcome = ri.simulate_reclassify(before, after)
        self.assertEqual(1, outcome["planned"])
        self.assertEqual(0, outcome["applied"]["inserted"],
                         "目标类型已存在 -> 插入是 no-op")
        self.assertEqual(1, outcome["applied"]["deleted"])
        integrity = ri.count_pair_dedupe(before, after)
        self.assertEqual(-1, integrity["edge_delta"])
        self.assertEqual(0, integrity["pairs_after"] - integrity["pairs_before"],
                         "记忆对数必须不变")
        self.assertEqual([], integrity["pairs_lost"])

    def test_no_pairs_lost_on_a_plain_rewrite(self):
        self._same_subject_pair()
        before = self._snapshot("b.sqlite")
        after = self.dir / "a.sqlite"
        ri.simulate_reclassify(before, after)
        integrity = ri.count_pair_dedupe(before, after)
        self.assertEqual(0, integrity["edge_delta"])
        self.assertEqual([], integrity["pairs_lost"])
    def test_relation_type_counts_come_from_the_ledger(self):
        self._same_subject_pair()
        before = self._snapshot("b.sqlite")
        after = self.dir / "a.sqlite"
        ri.simulate_reclassify(before, after)
        report = ri.build_report(before, after, subjects=5, memories=5)
        self.assertEqual(1, report["relation_types_before"][RELATION_RELATED_TO])
        self.assertEqual(1, report["relation_types_after"][RELATION_SAME_SUBJECT])
        self.assertNotIn(RELATION_SAME_SUBJECT, report["relation_types_before"])


class ExpectationCheckTests(unittest.TestCase):
    """预期校验必须能抓到「权重上升」这种错误改动。"""

    def _report_with(self, changed_edges):
        return {"subjects": [{"subject": "svc", "changed_edges": changed_edges,
                              "order_changed": False, "nodes_changed": False}],
                "memories": []}

    def test_accepts_a_weight_drop_that_matches_the_type(self):
        report = self._report_with([{
            "edge": "a->b", "type_before": RELATION_RELATED_TO,
            "type_after": RELATION_SAME_SUBJECT,
            "weight_before": 1.0, "weight_after": 0.9, "confidence": 0.5}])
        self.assertEqual([], ri.check_expectations(report))

    def test_rejects_a_weight_that_does_not_match_its_type(self):
        report = self._report_with([{
            "edge": "a->b", "type_before": RELATION_RELATED_TO,
            "type_after": RELATION_SAME_SUBJECT,
            "weight_before": 1.0, "weight_after": 1.0, "confidence": 0.5}])
        problems = ri.check_expectations(report)
        self.assertTrue(problems)
        self.assertIn("does not match", problems[0])

    def test_rejects_a_weight_increase(self):
        report = self._report_with([{
            "edge": "a->b", "type_before": RELATION_SAME_TASK,
            "type_after": RELATION_RELATED_TO,
            "weight_before": 0.7, "weight_after": 1.0, "confidence": 0.5}])
        problems = ri.check_expectations(report)
        self.assertTrue(any("INCREASED" in p for p in problems), problems)

    def test_rejects_an_ancestor_score_increase(self):
        report = {"subjects": [], "memories": [{"memory_id": "m1",
            "changed_ancestors": [{
                "memory_id": "m1", "score_before": 0.45, "score_after": 0.5,
                "delta": 0.05, "type_before": RELATION_RELATED_TO,
                "type_after": RELATION_RELATED_TO,
                "weight_before": 1.0, "weight_after": 1.0, "depth": 1}]}]}
        problems = ri.check_expectations(report)
        self.assertTrue(any("INCREASED" in p for p in problems), problems)


class CliTests(ImpactCase):
    """CLI 端到端：默认必须在副本上模拟，不能写 before 那一侧。"""

    def test_cli_predict_writes_json_and_leaves_before_untouched(self):
        a = self._memory("CLI A", subject="svc-shared", slot="s:alpha")
        b = self._memory("CLI B", subject="svc-shared", slot="s:beta")
        self._link(b, a)
        before = self._snapshot("cli-before.sqlite")
        out = self.dir / "report.json"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = ri.main(["--before", str(before),
                            "--workdir", str(self.dir),
                            "--json", str(out)])
        self.assertEqual(0, code)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["simulated"]["planned"])
        self.assertEqual([], payload["problems"])
        self.assertIn("预期校验：通过", buffer.getvalue())
        self.assertEqual([RELATION_RELATED_TO], self._types(before),
                         "CLI 默认只能写副本")

    def test_cli_reports_a_missing_ledger(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = ri.main(["--before", str(self.dir / "nope.sqlite")])
        self.assertEqual(2, code)
        self.assertIn("not found", buffer.getvalue())


class WeightContractTests(unittest.TestCase):
    """权重契约：报告与 graph 模块必须用同一张表。"""

    def test_only_two_types_lower_a_weight(self):
        # 这是 check_expectations 的前提：只有这两个类型会让权重下降，
        # 所以「权重只降不升」才是可断言的。
        lowered = {name for name in (RELATION_SAME_SUBJECT, RELATION_SAME_TASK)
                   if relation_weight(name) < 1.0}
        self.assertEqual({RELATION_SAME_SUBJECT, RELATION_SAME_TASK}, lowered)

    def test_report_uses_the_shared_weight_table(self):
        for name in (RELATION_RELATED_TO, RELATION_SAME_SUBJECT,
                     RELATION_SAME_TASK, "caused_by", "unknown_future_type"):
            self.assertEqual(relation_weight(name), ri._expected_weight(name))


if __name__ == "__main__":
    unittest.main()
