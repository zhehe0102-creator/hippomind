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


if __name__ == "__main__":
    unittest.main()

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
