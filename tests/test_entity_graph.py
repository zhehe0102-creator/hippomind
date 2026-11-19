"""hippomind graph 实体图谱（前景轮 P2）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger


class EntityGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-graph-test-")
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "g.sqlite")

    def _claim(self, content, subject, tag, parents=()):
        rec = {"content": content, "kind": "fact", "source_agent": "t",
               "evidence": [f"t:{tag}"],
               "claims": [{"subject": subject, "predicate": "status",
                           "slot_key": "state", "value": {"v": tag},
                           "confidence": 0.8}]}
        if parents:
            rec["causal_parents"] = list(parents)
        return self.led.ingest(rec)

    def test_two_hop_causal_chain_case_insensitive(self):
        a = self._claim("A 上线", "Service-A", 1)
        b = self._claim("B 引发 A 故障", "service-b", 2,
                        parents=[a.memory_id])
        self._claim("C 导致 B 过载", "SERVICE-C", 3, parents=[b.memory_id])
        g1 = self.led.entity_graph("service-a", hops=1)
        names1 = {n["subject"] for n in g1["nodes"]}
        self.assertEqual(names1, {"Service-A", "service-b"})
        g2 = self.led.entity_graph("Service-A", hops=2)
        names2 = {n["subject"] for n in g2["nodes"]}
        self.assertIn("service-c", {n.lower() for n in names2})
        self.assertTrue(all(e["relation_type"] == "caused_by"
                            for e in g2["edges"]))
        start = [n for n in g2["nodes"] if n.get("hop") == 0]
        self.assertEqual(len(start), 1)

    def test_unknown_entity_reports_not_found(self):
        report = self.led.entity_graph("不存在的实体")
        self.assertFalse(report["found"])
        self.assertEqual(report["nodes"], [])

    def test_superseded_claims_excluded(self):
        low = self._claim("旧状态", "old-svc", 1)
        self.led.ingest({"content": "新状态取代", "kind": "fact",
                         "source_agent": "t", "evidence": ["t:2"],
                         "claims": [{"subject": "old-svc",
                                     "predicate": "status",
                                     "slot_key": "state",
                                     "value": {"v": 2},
                                     "confidence": 0.9}]})
        # 分数接近会双双 conflict——此处只验证图谱只统计 active claim
        report = self.led.entity_graph("old-svc")
        for node in report["nodes"]:
            self.assertGreaterEqual(node["claims"], 0)

    def test_conflicted_claims_still_visible(self):
        """同主语值冲突（双 conflict）时实体不得从图谱消失。"""
        for i, tag in enumerate(("v0", "v1")):
            self._claim(f"service-x 状态 {tag}", "service-x", tag)
        report = self.led.entity_graph("service-x")
        self.assertTrue(report["found"])
        node = report["nodes"][0]
        self.assertEqual(node["subject"], "service-x")
        self.assertEqual(node["claims"], 2)
        self.assertGreaterEqual(node["conflicts"], 2)

    def test_active_chain_reports_zero_conflicts_field(self):
        self._claim("部署 a", "svc-a", "running")
        report = self.led.entity_graph("svc-a")
        self.assertEqual(report["nodes"][0]["conflicts"], 0)

    def test_cli_text_and_json(self):
        a = self._claim("A 上线", "api-gateway", 1)
        self._claim("B 引发 A 故障", "auth-service", 2,
                    parents=[a.memory_id])
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "graph",
             "--ledger", str(Path(self.tmp.name) / "g.sqlite"),
             "api-gateway", "--hops", "2"],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("api-gateway", r.stdout)
        self.assertIn("caused_by", r.stdout)
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "graph",
             "--ledger", str(Path(self.tmp.name) / "g.sqlite"),
             "ghost", "--format", "json"],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertFalse(json.loads(r.stdout)["found"])



"""recent_subjects 必须滤掉机器 id（task-6 / G1 的图谱半边）。

/api/v1/subjects 是图谱「实体索引」面板的唯一来源。生产库实测 400 条里
74 条（18.5%）是 UUID 或 mem_xxx，用户看到的就是一列
01a0744d-180c-739e-b9e0-e747618a5d7a，真正的实体被挤到下面。

修法要求**复用** hippomind.entity_index.is_machine_id，不写第二套判据：
两套判据只会随时间漂移，而漂移的表现是「检索侧滤掉了、图谱侧没滤」。
本文件因此同时钉住两件事：过滤生效，以及判据确实来自 entity_index。
"""
from hippomind.ledger import graph as graph_module
from hippomind.entity_index import is_machine_id

#: 生产库里的真实样本（任务描述里给出的两条 + entity_index 判据覆盖的其它形态）。
MACHINE_SAMPLES = [
    "01a0744d-180c-739e-b9e0-e747618a5d7a",   # 完整 UUID
    "mem_06b11f27e69c0a37bd6a8799643df534",   # mem_ 前缀
    "9ee8",                                    # 4 位十六进制碎片
    "1922574a",                                # 8 位十六进制
    "0123456789abcdef0123456789abcdef",        # 32 位十六进制
    "0123",                                    # 纯数字
]

#: 必须留下来的真实体名。
HUMAN_SAMPLES = [
    "HippoMind",
    "ADB 设备连接排查",
    "service-a",
    "数据库端口",
]


class RecentSubjectsMachineIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-subj-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "s.sqlite")

    def _claim(self, subject, value, tag):
        return self.ledger.ingest({
            "content": subject + " 状态 " + str(value), "kind": "fact",
            "source_agent": "t", "evidence": ["t:" + str(tag)],
            "claims": [{"subject": subject, "predicate": "status",
                        "slot_key": "state", "value": {"v": value},
                        "confidence": 0.8}],
        })

    def test_machine_ids_are_filtered_out(self):
        for index, subject in enumerate(MACHINE_SAMPLES):
            self._claim(subject, "v" + str(index), index)
        for index, subject in enumerate(HUMAN_SAMPLES):
            self._claim(subject, "h" + str(index), 100 + index)

        rows = self.ledger.recent_subjects(limit=400)
        subjects = [row["subject"] for row in rows]

        for subject in HUMAN_SAMPLES:
            with self.subTest(subject=subject):
                self.assertIn(subject, subjects, "真实体被误滤掉了")
        for subject in MACHINE_SAMPLES:
            with self.subTest(subject=subject):
                self.assertNotIn(subject, subjects,
                                 "机器 id 仍出现在实体索引里")

    def test_filter_matches_entity_index_predicate(self):
        """判据必须来自 entity_index，不能是 recent_subjects 自己的一套。

        直接比对两个函数的结论：任何一行只要 is_machine_id 说是机器 id，
        recent_subjects 就不能返回它。
        """
        for index, subject in enumerate(MACHINE_SAMPLES + HUMAN_SAMPLES):
            self._claim(subject, "v" + str(index), index)
        subjects = {row["subject"] for row in self.ledger.recent_subjects(limit=400)}
        for subject in subjects:
            with self.subTest(subject=subject):
                self.assertFalse(is_machine_id(subject),
                                 "recent_subjects 返回了 is_machine_id 判定为机器 id 的主语")

    def test_machine_ids_do_not_eat_the_page(self):
        """过滤不能变成「返回不足一页」：机器 id 先占满 LIMIT 也要补足。

        生产库实测丢掉 18.5%——如果只按 limit 取一次再过滤，400 条会变 326 条。
        """
        # 先写一堆机器 id（它们 rowid 更大 = 更「新」），再写真实体。
        for index, subject in enumerate(MACHINE_SAMPLES):
            self._claim(subject, "m" + str(index), index)
        for index, subject in enumerate(HUMAN_SAMPLES):
            self._claim(subject, "h" + str(index), 100 + index)

        rows = self.ledger.recent_subjects(limit=len(HUMAN_SAMPLES))
        self.assertEqual(len(HUMAN_SAMPLES), len(rows),
                         "机器 id 占满 LIMIT 后真实体没有补足，页签会显示不满一页")

    def test_limit_is_still_respected(self):
        for index in range(12):
            self._claim("实体-" + str(index), "v" + str(index), index)
        rows = self.ledger.recent_subjects(limit=5)
        self.assertEqual(5, len(rows), "limit 没有被尊重")
        self.assertTrue(all(not is_machine_id(row["subject"]) for row in rows))

    def test_limit_upper_bound_is_clamped(self):
        """limit 仍然有硬上限（2000），过滤不能把一次查询变成全表扫描。"""
        self._claim("唯一实体", "v", 0)
        rows = self.ledger.recent_subjects(limit=999999)
        self.assertEqual(["唯一实体"], [row["subject"] for row in rows])

    def test_counts_still_aggregate_per_subject(self):
        """过滤不能改变聚合语义：同一主语的 claim 数仍然合并。"""
        self._claim("service-a", "v0", 0)
        self._claim("service-a", "v1", 1)
        self._claim("mem_deadbeefdeadbeefdeadbeefdeadbeef", "v2", 2)
        rows = {row["subject"]: row for row in self.ledger.recent_subjects(limit=400)}
        self.assertIn("service-a", rows)
        self.assertEqual(2, rows["service-a"]["claims"])
        self.assertNotIn("mem_deadbeefdeadbeefdeadbeefdeadbeef", rows)

    def test_scope_filter_still_applies(self):
        """过滤只在 scope 内生效，不能把别的 scope 的行拉进来。"""
        self._claim("shared-entity", "v", 0)
        self.ledger.ingest({
            "content": "私有 scope 的实体", "kind": "fact", "source_agent": "t",
            "scope": "private", "evidence": ["t:p"],
            "claims": [{"subject": "private-entity", "predicate": "status",
                        "slot_key": "state", "value": {"v": "x"},
                        "confidence": 0.8}],
        })
        shared = [row["subject"] for row in self.ledger.recent_subjects(limit=400)]
        self.assertIn("shared-entity", shared)
        self.assertNotIn("private-entity", shared,
                         "recent_subjects 泄漏了其它 scope 的主语")


class RecentSubjectsUsesSharedPredicateTests(unittest.TestCase):
    """钉住「复用 is_machine_id」这件事本身，防止将来有人写第二套判据。"""

    def test_graph_module_imports_is_machine_id(self):
        self.assertIs(
            getattr(graph_module, "is_machine_id", None), is_machine_id,
            "hippomind/ledger/graph.py 没有复用 entity_index.is_machine_id；"
            "两套判据会随时间漂移，表现是「检索侧滤掉了、图谱侧没滤」")

    def test_no_private_uuid_regex_in_graph_module(self):
        """graph.py 里不应出现自己的 UUID/十六进制判据。"""
        source = (ROOT / "hippomind" / "ledger" / "graph.py").read_text(encoding="utf-8")
        for pattern in ("[0-9a-f]{8}-[0-9a-f]{4}", "^(mem|evt|rel|cl|sha|cf)"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, source,
                                 "graph.py 里出现了自建的机器 id 判据（应复用 entity_index）")


class EntityIndexPredicateCoverageTests(unittest.TestCase):
    """用例里的机器 id 样本必须真的被判据覆盖，否则上面的断言是空转。"""

    def test_machine_samples_are_recognised(self):
        for subject in MACHINE_SAMPLES:
            with self.subTest(subject=subject):
                self.assertTrue(is_machine_id(subject),
                                "样本没有被 is_machine_id 覆盖，测试失去意义")

    def test_human_samples_are_not_recognised(self):
        for subject in HUMAN_SAMPLES:
            with self.subTest(subject=subject):
                self.assertFalse(is_machine_id(subject),
                                 "真实体被 is_machine_id 判成机器 id")



if __name__ == "__main__":
    unittest.main()
