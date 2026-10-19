"""事实/技能投影测试：结构化视图、过滤与状态隔离。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hippomind.ledger import SQLiteLedger


class FactSkillProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        # 两条事实（同槽位，高置信取代低置信）
        self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "生产数据库端口 5432", "evidence": ["e:1"],
            "confidence": 0.55, "subject": "db.prod", "predicate": "config",
            "slot_key": "port", "value": {"port": "5432"},
        })
        self.current = self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "生产数据库端口迁移至 6432", "evidence": ["e:2"],
            "confidence": 0.9, "subject": "db.prod", "predicate": "config",
            "slot_key": "port", "value": {"port": "6432"},
        })
        # 一条技能
        self.skill = self.ledger.ingest({
            "kind": "procedural", "scope": "shared", "source_agent": "tester",
            "content": "数据库连接池耗尽处置手册", "evidence": ["e:s1"],
            "procedural": {
                "trigger_pattern": ["连接池耗尽告警"],
                "preconditions": ["具有 DBA 权限"],
                "verification_assertion": ["连接池使用率回落"],
                "execution_pipeline": [
                    {"action": "定位泄漏连接"},
                    {"action": "滚动重启应用实例"},
                ],
            },
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_facts_projection_returns_active_claims_only(self):
        facts = self.ledger.list_facts(scope="shared")
        # 事实视图只含 active claim：旧值被高置信新值取代后不再出现
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["value"], {"port": "6432"})
        self.assertEqual(facts[0]["subject"], "db.prod")

    def test_superseded_claim_excluded_from_facts(self):
        facts = self.ledger.list_facts(scope="shared")
        values = [json.dumps(f["value"], ensure_ascii=False, sort_keys=True)
                  for f in facts]
        # 旧值 5432 的 claim 已 superseded，不应出现在事实投影
        self.assertNotIn('{"port": "5432"}', values)

    def test_subject_filter(self):
        facts = self.ledger.list_facts(scope="shared", subject="db.prod")
        self.assertTrue(all(f["subject"] == "db.prod" for f in facts))
        empty = self.ledger.list_facts(scope="shared", subject="no.such")
        self.assertEqual(empty, [])

    def test_skills_projection(self):
        skills = self.ledger.list_skills(scope="shared")
        self.assertEqual(len(skills), 1)
        skill = skills[0]
        self.assertEqual(skill["trigger_pattern"], ["连接池耗尽告警"])
        self.assertEqual(skill["preconditions"], ["具有 DBA 权限"])
        self.assertEqual(len(skill["execution_pipeline"]), 2)
        self.assertEqual(skill["execution_pipeline"][0]["action"], "定位泄漏连接")

    def test_superseded_skill_excluded(self):
        # 用 forget 移除技能后不再出现在投影中
        self.ledger.forget(memory_ids=[self.skill.memory_id])
        self.assertEqual(self.ledger.list_skills(scope="shared"), [])


if __name__ == "__main__":
    unittest.main()
