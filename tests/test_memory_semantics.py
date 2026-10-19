"""记忆语义写路径测试：claim 自动派生、技能提炼、结果驱动信用分配。

背景（已实测）：线上 154 万行账本里 ``memory_claims``、``memory_procedural``
（kind='procedural'）和 ``memory_feedback`` 都是 0 行——账本层其实早已实现
槽位冲突裁决、双时态和技能投影，但**没有任何写入路径会传 claims / procedural
/ feedback**，所以这些能力全是死代码。本文件锁定 ``MemoryHubAgentAdapter``
新增的三条写路径及其保守性边界。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from hippomind.agent_adapter import (
    CLAIM_KINDS,
    MAX_CREDIT_TARGETS,
    MAX_DERIVED_CLAIM_CHARS,
    MIN_DERIVED_CLAIM_CONFIDENCE,
    OUTCOME_SIGNAL,
    MemoryHubAgentAdapter,
)
from hippomind.ledger import LedgerError, SQLiteLedger


class _AdapterCase(unittest.TestCase):
    """每个测试独占一个临时账本，避免跨测试的 claim/信用污染。"""

    task_id = "T1"
    project = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "hub.sqlite")
        self.ledger = SQLiteLedger(self.path)
        self.adapter = self.make_adapter()

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def make_adapter(self, source_agent: str = "codex", **kwargs) -> MemoryHubAgentAdapter:
        kwargs.setdefault("task_id", self.task_id)
        kwargs.setdefault("project", self.project)
        return MemoryHubAgentAdapter(source_agent, ledger=self.ledger, **kwargs)

    # -- helpers --------------------------------------------------------
    def claims(self, memory_id: str = "", state: str = ""):
        return self.ledger.list_claims(scope="shared", memory_id=memory_id, state=state)

    def sql(self, query: str, *params):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(query, params).fetchall()

    def feedback_rows(self) -> int:
        return int(self.sql("SELECT COUNT(*) FROM memory_feedback")[0][0])


class ClaimDerivationTests(_AdapterCase):
    """A. claim 自动派生：只有可冲突断言才派生，且宁可少派生。"""

    def test_decision_derives_slot_claim(self):
        result = self.adapter.decision("采用 SQLite", evidence=["c:1"])
        claims = self.claims(memory_id=result.memory_id)
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim["subject"], "T1")
        self.assertEqual(claim["predicate"], "decision")
        self.assertEqual(claim["slot_key"], "key_decisions")
        self.assertEqual(claim["value"], "采用 SQLite")
        self.assertEqual(claim["state"], "active")
        self.assertEqual(self.ledger.get_memory(result.memory_id)["quality_state"], "accepted")

    def test_decision_content_fallback_derives(self):
        # context 没有 key_decisions 时回退用 content（短句）。
        result = self.adapter.record("decision", "采用 PostgreSQL", evidence=["c:1"])
        claims = self.claims(memory_id=result.memory_id)
        self.assertEqual([claim["value"] for claim in claims], ["采用 PostgreSQL"])

    def test_progress_and_result_do_not_derive(self):
        progress = self.adapter.checkpoint("完成 schema 抽取", evidence=["c:1"])
        result = self.adapter.complete("阶段完成", evidence=["c:1"])
        self.assertEqual(self.claims(memory_id=progress.memory_id), [])
        self.assertEqual(self.claims(memory_id=result.memory_id), [])
        self.assertEqual(self.claims(), [], "流水记录不应产生任何 claim")

    def test_decision_without_task_id_does_not_derive(self):
        # 没有 task_id 时，同 project 下不同 task 的决策会互相冲突，故不派生。
        adapter = self.make_adapter(task_id="", project="hippomind")
        result = adapter.decision("采用 SQLite", evidence=["c:1"])
        self.assertEqual(self.claims(memory_id=result.memory_id), [])
        self.assertIsNotNone(self.ledger.get_memory(result.memory_id), "记忆本身仍要写入")

    def test_ambiguous_context_does_not_derive(self):
        result = self.adapter.record(
            "decision", "选型", evidence=["c:1"],
            context={"key_decisions": ["方案甲", "方案乙"]})
        self.assertEqual(self.claims(memory_id=result.memory_id), [])
        # 同一个值重复出现（仅标点不同）不算多值。
        same = self.adapter.record(
            "decision", "选型二", evidence=["c:1"],
            context={"key_decisions": ["采用 SQLite。", "采用 SQLite"]})
        self.assertEqual([c["value"] for c in self.claims(memory_id=same.memory_id)],
                         ["采用 SQLite"])

    def test_long_content_does_not_derive(self):
        long_text = "决" * (MAX_DERIVED_CLAIM_CHARS + 1)
        result = self.adapter.record("decision", long_text, evidence=["c:1"])
        self.assertEqual(self.claims(memory_id=result.memory_id), [])
        # 边界内仍派生
        boundary = self.adapter.record("decision", "决" * MAX_DERIVED_CLAIM_CHARS,
                                       evidence=["c:1"])
        self.assertEqual(len(self.claims(memory_id=boundary.memory_id)), 1)

    def test_low_confidence_does_not_derive(self):
        result = self.adapter.record(
            "decision", "低置信决策", evidence=["c:1"],
            confidence=MIN_DERIVED_CLAIM_CONFIDENCE - 0.05)
        self.assertEqual(self.claims(memory_id=result.memory_id), [])
        boundary = self.adapter.record(
            "decision", "边界置信决策", evidence=["c:1"],
            confidence=MIN_DERIVED_CLAIM_CONFIDENCE)
        self.assertEqual(len(self.claims(memory_id=boundary.memory_id)), 1)

    def test_untrusted_metadata_does_not_derive(self):
        for marker in ({"derived_from_raw_chat": True}, {"untrusted_source": True}):
            result = self.adapter.record(
                "decision", "不可信决策", evidence=["c:1"],
                metadata=dict(marker))
            self.assertEqual(self.claims(memory_id=result.memory_id), [],
                             f"{marker} 不应派生 claim")

    def test_explicit_claims_take_priority(self):
        result = self.adapter.record(
            "decision", "内容", evidence=["c:1"],
            context={"key_decisions": ["内容"]},
            claims=[{"subject": "T1", "predicate": "decision",
                     "slot_key": "key_decisions", "value": "显式声明"}])
        claims = self.claims(memory_id=result.memory_id)
        self.assertEqual([claim["value"] for claim in claims], ["显式声明"],
                         "显式 claims 必须透传且抑制派生")
        # 单对象（Mapping）也接受
        other = self.adapter.record(
            "fact", "端口 6432", evidence=["c:1"],
            claims={"subject": "db", "predicate": "config",
                    "slot_key": "port", "value": "6432"})
        self.assertEqual([claim["slot_key"] for claim in self.claims(memory_id=other.memory_id)],
                         ["port"])

    def test_derive_claims_false_disables_derivation(self):
        result = self.adapter.record("decision", "显式关闭派生", evidence=["c:1"],
                                     derive_claims=False)
        self.assertEqual(self.claims(memory_id=result.memory_id), [])
        # 显式 claims 不受 derive_claims=False 影响（它本来就是显式意图）
        explicit = self.adapter.record(
            "decision", "显式关闭派生二", evidence=["c:1"], derive_claims=False,
            claims=[{"subject": "T1", "predicate": "decision",
                     "slot_key": "key_decisions", "value": "显式"}])
        self.assertEqual([c["value"] for c in self.claims(memory_id=explicit.memory_id)],
                         ["显式"])

    def test_fact_free_text_never_auto_derives(self):
        result = self.adapter.record("fact", "生产数据库端口是 5432", evidence=["c:1"])
        self.assertEqual(self.claims(memory_id=result.memory_id), [],
                         "自由文本事实不是单槽断言")

    def test_preference_derives_with_project_subject(self):
        adapter = self.make_adapter(task_id="", project="hippomind")
        result = adapter.preference("偏好中文注释", evidence=["c:1"])
        claims = self.claims(memory_id=result.memory_id)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["subject"], "hippomind")
        self.assertEqual(claims[0]["predicate"], "preference")
        self.assertEqual(claims[0]["slot_key"], "preferences")

    def test_explicit_subject_overrides_identity(self):
        result = self.adapter.record(
            "decision", "采用 SQLite", evidence=["c:1"],
            context={"key_decisions": ["采用 SQLite"]}, subject="arch.db")
        claims = self.claims(memory_id=result.memory_id)
        self.assertEqual([claim["subject"] for claim in claims], ["arch.db"])

    def test_claim_kind_constant_is_the_contract(self):
        self.assertEqual(CLAIM_KINDS, frozenset({"decision", "preference", "fact"}))


class ClaimConflictTests(_AdapterCase):
    """同槽位冲突与知识更新收敛（claim 表不再永远是 0 行）。"""

    def test_two_decisions_collide_and_resolution_crowns_winner(self):
        first = self.adapter.decision("方案甲", evidence=["c:1"])
        second = self.adapter.decision("方案乙", evidence=["c:1"])
        conflicts = self.claims(state="conflict")
        self.assertEqual({claim["memory_id"] for claim in conflicts},
                         {first.memory_id, second.memory_id})
        for memory_id in (first.memory_id, second.memory_id):
            self.assertEqual(self.ledger.get_memory(memory_id)["quality_state"], "conflict")

        claim_id = next(c["claim_id"] for c in conflicts if c["memory_id"] == first.memory_id)
        resolved = self.ledger.resolve_claim(claim_id, second.memory_id)
        self.assertEqual(resolved["winner"], second.memory_id)
        self.assertIn(first.memory_id, resolved["losers"])

        winner = self.ledger.get_memory(second.memory_id)
        loser = self.ledger.get_memory(first.memory_id)
        self.assertEqual(winner["quality_state"], "accepted")
        self.assertEqual(winner["lifecycle"], "active")
        self.assertEqual(loser["quality_state"], "superseded")
        self.assertEqual(loser["lifecycle"], "superseded")
        active = self.claims(state="active")
        self.assertEqual({claim["memory_id"] for claim in active}, {second.memory_id})

    def test_third_decision_converges_to_newest(self):
        first = self.adapter.decision("方案甲", evidence=["c:1"])
        second = self.adapter.decision("方案乙", evidence=["c:1"])
        third = self.adapter.decision("方案丙", evidence=["c:1"])
        states = {claim["memory_id"]: claim["state"] for claim in self.claims()}
        self.assertEqual(states[first.memory_id], "conflict")
        self.assertEqual(states[second.memory_id], "conflict")
        self.assertEqual(states[third.memory_id], "active")
        self.assertEqual(self.ledger.get_memory(third.memory_id)["quality_state"], "accepted")
        for old in (first.memory_id, second.memory_id):
            self.assertEqual(self.ledger.get_memory(old)["quality_state"], "conflict")

    def test_trailing_punctuation_does_not_fake_a_conflict(self):
        first = self.adapter.decision("采用 SQLite。", evidence=["c:1"])
        second = self.adapter.decision("采用 SQLite", evidence=["c:1"])
        states = {claim["memory_id"]: claim["state"] for claim in self.claims()}
        self.assertEqual(states[first.memory_id], "active")
        self.assertEqual(states[second.memory_id], "active")
        self.assertEqual(self.claims(state="conflict"), [])

    def test_explicit_fact_high_confidence_supersedes_low(self):
        old = self.adapter.record(
            "fact", "数据库端口 5432", evidence=["c:1"], subject="db.prod",
            predicate="config", slot="port", value="5432", confidence=0.55)
        new = self.adapter.record(
            "fact", "数据库端口 6432", evidence=["c:2"], subject="db.prod",
            predicate="config", slot="port", value="6432", confidence=0.9)
        facts = self.ledger.list_facts(scope="shared")
        self.assertEqual([fact["value"] for fact in facts], ["6432"])
        self.assertEqual(self.ledger.get_memory(old.memory_id)["lifecycle"], "superseded")
        self.assertEqual(self.ledger.get_memory(new.memory_id)["quality_state"], "accepted")
        states = {claim["memory_id"]: claim["state"] for claim in self.claims()}
        self.assertEqual(states[old.memory_id], "superseded")
        self.assertEqual(states[new.memory_id], "active")


class SkillWritePathTests(_AdapterCase):
    """B. procedural 技能：写入、召回、提炼与拒绝条件。"""

    def test_skill_written_and_recallable(self):
        result = self.adapter.skill(
            "数据库连接池耗尽处置手册", evidence=["c:1"],
            trigger_pattern=["连接池耗尽告警"], preconditions=["具有 DBA 权限"],
            execution_pipeline=["定位泄漏连接", "滚动重启应用实例"],
            verification_assertion=["连接池使用率回落"])
        skills = self.ledger.list_skills(scope="shared")
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["memory_id"], result.memory_id)
        self.assertEqual(skills[0]["trigger_pattern"], ["连接池耗尽告警"])
        self.assertEqual(skills[0]["preconditions"], ["具有 DBA 权限"])
        self.assertEqual([step["action"] for step in skills[0]["execution_pipeline"]],
                         ["定位泄漏连接", "滚动重启应用实例"])
        hits = self.ledger.search("连接池耗尽", scope="shared", limit=10)
        self.assertIn(result.memory_id, {hit["memory_id"] for hit in hits})

    def test_skill_missing_verification_assertion_raises(self):
        with self.assertRaises(LedgerError):
            self.adapter.skill(
                "缺少校验的伪技能", evidence=["c:1"],
                trigger_pattern=["告警"], preconditions=["权限"],
                execution_pipeline=["步骤"])  # verification_assertion 缺失
        self.assertEqual(self.ledger.list_skills(scope="shared"), [])

    def test_procedural_does_not_derive_claim(self):
        result = self.adapter.record(
            "procedural", "手工技能卡", evidence=["c:1"],
            procedural={"trigger_pattern": ["告警"], "preconditions": ["权限"],
                        "execution_pipeline": ["步骤"],
                        "verification_assertion": ["恢复"]})
        self.assertEqual(self.claims(memory_id=result.memory_id), [])

    def test_distill_skill_from_result_chain(self):
        start = self.adapter.checkpoint("开始重构", evidence=["c:1"])
        decide = self.adapter.decision("拆分 ledger", evidence=["c:1"])
        result = self.adapter.complete("阶段完成", evidence=["c:1"], outcome="success")

        skill = self.adapter.distill_skill(title="重构推进手册")
        memory = self.ledger.get_memory(skill.memory_id)
        self.assertEqual(memory["kind"], "procedural")
        self.assertIn("重构推进手册", memory["content"])

        skills = self.ledger.list_skills(scope="shared")
        distilled = next(item for item in skills if item["memory_id"] == skill.memory_id)
        self.assertEqual([step["action"] for step in distilled["execution_pipeline"]],
                         ["开始重构", "拆分 ledger"],
                         "pipeline 应按 created_at 升序包含两层祖先内的步骤")
        self.assertEqual(distilled["trigger_pattern"], ["任务 T1：开始重构"])
        self.assertIn("来源记忆与因果链仍可在账本中检索到", distilled["preconditions"])
        self.assertEqual(distilled["verification_assertion"], ["阶段完成"])

        procedural = memory["metadata"]["procedural"]
        self.assertEqual(procedural["calibration_score"],
                         {"attempts": 1, "successes": 1, "rate": 1.0})
        self.assertEqual(procedural["execution_policy"], "declarative-only")

        parent_ids = {parent["memory_id"]
                      for parent in memory["metadata"]["causal_parents"]}
        self.assertIn(result.memory_id, parent_ids)
        self.assertIn(start.memory_id, parent_ids)
        self.assertIn(decide.memory_id, parent_ids)
        ancestors = self.ledger.causal_ancestors([skill.memory_id], max_depth=2,
                                                 scope="shared")
        self.assertIn(result.memory_id, {row["memory_id"] for row in ancestors})
        self.assertIn(decide.memory_id, {row["memory_id"] for row in ancestors})
        self.assertIn(f"memory:{result.memory_id}", memory["evidence"])

    def test_distill_skill_preconditions_include_blockers(self):
        self.adapter.checkpoint("开始重构", evidence=["c:1"])
        self.adapter.blocker("磁盘空间不足", evidence=["c:1"])
        self.adapter.complete("阶段完成", evidence=["c:1"], outcome="success")
        skill = self.adapter.distill_skill()
        procedural = self.ledger.get_memory(skill.memory_id)["metadata"]["procedural"]
        self.assertIn("磁盘空间不足", procedural["preconditions"])
        self.assertIn("来源记忆与因果链仍可在账本中检索到",
                      procedural["preconditions"])

    def test_distill_skill_accepts_explicit_success_outcome(self):
        self.adapter.checkpoint("第一步", evidence=["c:1"])
        result = self.adapter.complete("完成", evidence=["c:1"])  # 无 outcome 标记
        skill = self.adapter.distill_skill(outcome="success")
        self.assertEqual(skill.memory_id, self.adapter._last_memory_id)
        self.assertEqual(len(self.ledger.list_skills(scope="shared")), 1)
        self.assertIsNotNone(self.ledger.get_memory(result.memory_id))

    def test_distill_skill_rejects_failed_result(self):
        self.adapter.checkpoint("尝试", evidence=["c:1"])
        self.adapter.complete("部署失败", evidence=["c:1"], outcome="failure")
        with self.assertRaises(LedgerError):
            self.adapter.distill_skill()
        self.assertEqual(self.ledger.list_skills(scope="shared"), [])

    def test_distill_skill_rejects_non_result(self):
        progress = self.adapter.checkpoint("只是一条进展", evidence=["c:1"])
        with self.assertRaises(LedgerError):
            self.adapter.distill_skill(result_memory_id=progress.memory_id)

    def test_distill_skill_rejects_result_without_ancestors(self):
        self.adapter.complete("孤零零的结果", evidence=["c:1"], outcome="success")
        with self.assertRaises(LedgerError):
            self.adapter.distill_skill()
        self.assertEqual(self.ledger.list_skills(scope="shared"), [])


class CreditAssignmentTests(_AdapterCase):
    """C. 结果驱动信用分配：只从真实结果出发，且幂等、不跨任务。"""

    def _prepare_result(self, outcome: str = "") -> tuple:
        progress = self.adapter.checkpoint("准备发布", evidence=["c:1"])
        result = self.adapter.complete("发布完成", evidence=["c:1"], outcome=outcome)
        return progress, result

    def test_success_result_credits_ancestors(self):
        progress, _ = self._prepare_result(outcome="success")
        memory = self.ledger.get_memory(progress.memory_id)
        self.assertAlmostEqual(memory["utility"], 0.2 * OUTCOME_SIGNAL, places=6)
        self.assertGreater(memory["utility"], 0.0)
        self.assertEqual(memory["success_count"], 1)
        self.assertGreater(memory["weight"], 0.5)
        self.assertEqual(self.feedback_rows(), 1)

    def test_failure_result_debits_ancestors(self):
        progress, _ = self._prepare_result(outcome="failure")
        memory = self.ledger.get_memory(progress.memory_id)
        self.assertAlmostEqual(memory["utility"], -0.2 * OUTCOME_SIGNAL, places=6)
        self.assertLess(memory["utility"], 0.0)
        self.assertEqual(memory["failure_count"], 1)
        self.assertEqual(self.feedback_rows(), 1)

    def test_metadata_outcome_triggers_credit(self):
        progress = self.adapter.checkpoint("准备回滚", evidence=["c:1"])
        self.adapter.record("result", "回滚完成", evidence=["c:1"],
                            metadata={"success": False})
        memory = self.ledger.get_memory(progress.memory_id)
        self.assertLess(memory["utility"], 0.0)
        self.assertEqual(memory["failure_count"], 1)

    def test_credit_is_idempotent(self):
        progress, result = self._prepare_result()  # 无 outcome → 不自动分配
        self.assertEqual(self.feedback_rows(), 0)
        first = self.adapter.credit_outcome(result.memory_id, outcome="success")
        second = self.adapter.credit_outcome(result.memory_id, outcome="success")
        self.assertEqual(first["applied"], 1)
        self.assertEqual(first["credited"], [progress.memory_id])
        self.assertEqual(second["applied"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(self.feedback_rows(), 1, "重复调用不能重复加信用")
        memory = self.ledger.get_memory(progress.memory_id)
        self.assertEqual(memory["success_count"], 1)
        self.assertAlmostEqual(memory["utility"], 0.2 * OUTCOME_SIGNAL, places=6)

    def test_credit_does_not_cross_tasks(self):
        other = self.make_adapter(task_id="T2")
        other_progress = other.checkpoint("T2 的进展", evidence=["c:1"])
        mine = self.adapter.checkpoint("T1 的进展", evidence=["c:1"])
        other.complete("T2 完成", evidence=["c:1"], outcome="success")
        self.assertGreater(self.ledger.get_memory(other_progress.memory_id)["utility"], 0.0)
        self.assertEqual(self.ledger.get_memory(mine.memory_id)["utility"], 0.0,
                         "不同 task 的进展不得获得信用")
        self.assertEqual(self.feedback_rows(), 1)

    def test_credit_respects_target_limit_and_bounds(self):
        self.adapter.checkpoint("步骤一", evidence=["c:1"])
        self.adapter.checkpoint("步骤二", evidence=["c:1"])
        result = self.adapter.complete("完成", evidence=["c:1"])
        report = self.adapter.credit_outcome(result.memory_id, signal=5.0,
                                             limit=MAX_CREDIT_TARGETS)
        self.assertEqual(report["signal"], OUTCOME_SIGNAL, "显式 signal 必须钳到 ±0.3")
        self.assertLessEqual(report["applied"], MAX_CREDIT_TARGETS)
        self.assertEqual(report["applied"], 2)
        for memory_id in report["credited"]:
            self.assertEqual(self.ledger.get_memory(memory_id)["success_count"], 1)

    def test_credit_window_excludes_old_ancestors(self):
        # 祖先链跨越很久以前的进展：只给窗口内的记录分配信用。
        old = self.ledger.ingest({
            "kind": "progress", "content": "远古进展", "source_agent": "codex",
            "task_id": "T1", "scope": "shared", "evidence": ["c:1"],
            "timestamp": "2020-01-01T00:00:00Z"})
        recent = self.ledger.ingest({
            "kind": "progress", "content": "近期进展", "source_agent": "codex",
            "task_id": "T1", "scope": "shared", "evidence": ["c:1"],
            "causal_parents": [old.memory_id]})
        result = self.ledger.ingest({
            "kind": "result", "content": "完成", "source_agent": "codex",
            "task_id": "T1", "scope": "shared", "evidence": ["c:1"],
            "causal_parents": [recent.memory_id]})
        report = self.adapter.credit_outcome(result.memory_id, outcome="success")
        self.assertEqual(report["credited"], [recent.memory_id])
        self.assertEqual(self.ledger.get_memory(old.memory_id)["utility"], 0.0)
        self.assertEqual(self.ledger.get_memory(old.memory_id)["success_count"], 0)

    def test_recall_does_not_change_utility(self):
        progress = self.adapter.checkpoint("检索不应改变效用", evidence=["c:1"])
        before = self.ledger.get_memory(progress.memory_id)
        hits = self.ledger.search("检索不应改变效用", scope="shared", limit=10)
        self.assertIn(progress.memory_id, {hit["memory_id"] for hit in hits})
        after = self.ledger.get_memory(progress.memory_id)
        self.assertEqual(before["utility"], after["utility"])
        self.assertEqual(before["touch_count"], after["touch_count"])
        self.assertEqual(before["success_count"], after["success_count"])
        self.assertEqual(self.feedback_rows(), 0, "召回不是信用来源")

    def test_credit_rejects_non_result_and_unknown_outcome(self):
        progress = self.adapter.checkpoint("只是进展", evidence=["c:1"])
        with self.assertRaises(LedgerError):
            self.adapter.credit_outcome(progress.memory_id, outcome="success")
        result = self.adapter.complete("完成", evidence=["c:1"])
        with self.assertRaises(LedgerError):
            self.adapter.credit_outcome(result.memory_id)
        with self.assertRaises(LedgerError):
            self.adapter.credit_outcome(result.memory_id, outcome="maybe")
        self.assertEqual(self.feedback_rows(), 0)


if __name__ == "__main__":
    unittest.main()
