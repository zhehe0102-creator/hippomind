"""Conflict review: inventory, adjudication, audit trail, apply safety.

Every test here is offline.  The model decider is exercised through its
``runner`` injection seam, so no subprocess is ever spawned and no network is
touched; the rules decider needs no model at all.

The conflict shapes are not synthesised by poking the database - they are
produced by real ``ingest`` calls, because the production queue is dominated
by *indirect* conflicts: a memory that loses one slot is taken offline as a
whole, dragging its unrelated claims into the queue with it.  Reproducing that
is the only way these tests can fail for the same reason production did.
"""

from __future__ import annotations

import contextlib
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

import conflict_review as cr  # noqa: E402
from hippomind.ledger import LedgerError, SQLiteLedger  # noqa: E402


def _claim(slot, value, confidence, *, subject="svc", predicate="p"):
    return {"subject": subject, "predicate": predicate, "slot_key": slot,
            "value": value, "confidence": confidence}


class LedgerCase(unittest.TestCase):
    """Temporary ledger plus helpers that build each conflict root cause."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-cr-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self._seq = 0

    def ingest(self, claims, content="记忆内容", *, agent="tester"):
        self._seq += 1
        return self.ledger.ingest({
            "content": content, "kind": "fact", "source_agent": agent,
            "evidence": ["%s:%d" % (agent, self._seq)], "claims": claims,
        })

    # -- conflict shapes ---------------------------------------------------
    def genuine_pair(self):
        """Two different values with near-equal scores -> one genuine group."""
        a = self.ingest([_claim("port", "5432", 0.50)], "端口 5432")
        b = self.ingest([_claim("port", "6432", 0.52)], "端口 6432")
        return a, b

    def duplicate_setup(self):
        """Reproduce the production "duplicate" conflict shape.

        Two identical values never conflict on their own: ingest skips the
        comparison when the value hashes match.  The production duplicates are
        indirect, and the ordering is what produces them:

        1. ``multi`` asserts two independent facts (port + theme);
        2. ``rival`` disagrees on *port* with a near-equal score, so ingest
           parks both sides in conflict - and the memory-level
           quality_state=conflict flag takes ``multi``'s *theme* claim down
           with it;
        3. ``agree`` asserts the same theme value, and because ``multi``'s
           theme claim is no longer active it is not compared, so it lands as
           a live duplicate of a claim already parked in the queue.

        Result: one genuine group (port) plus one phantom group (theme) whose
        two sides carry an identical value_hash.
        """
        multi = self.ingest([_claim("port", "5432", 0.50),
                             _claim("theme", "纸本", 0.90)], "端口与主题")
        rival = self.ingest([_claim("port", "6432", 0.52)], "端口 6432")
        agree = self.ingest([_claim("theme", "纸本", 0.95)], "主题 纸本")
        return multi, rival, agree

    def orphan_setup(self):
        """A slot whose only claim is in conflict (production orphan shape).

        ``multi`` is taken offline by the port conflict, which also flags its
        pdf claim.  The pdf slot then holds exactly one claim, in conflict,
        with no competitor at all - so it can never be settled by comparison.
        """
        multi = self.ingest([_claim("port", "5432", 0.50),
                             _claim("pdf", "补充截图", 0.80)], "端口与 PDF")
        rival = self.ingest([_claim("port", "6432", 0.52)], "端口 6432")
        return multi, rival

    def truncation_setup(self):
        """One value is a truncated prefix of the other -> truncation cause."""
        full = self.ingest(
            [_claim("scope", "FTS 过滤候选、多归档导入、raw chat 安全提炼", 0.85)],
            "完整范围")
        self.ingest([_claim("scope", "FTS 过滤候选（片段截断）", 0.50)], "截断范围")
        return full

    # -- inspection --------------------------------------------------------
    def groups(self):
        return cr.collect_groups(self.ledger)

    def group_for(self, slot):
        """The single group on ``slot``; asserts it is actually in conflict."""
        for group in self.groups():
            if group.slot_key == slot:
                return group
        raise AssertionError("预期 %r 槽位处于冲突，实际有: %r"
                             % (slot, [g.slot_key for g in self.groups()]))

    def claim_states(self):
        return {(c["memory_id"], c["slot_key"]): c["state"]
                for c in self.ledger.list_claims()}

    def review_rows(self):
        with sqlite3.connect(self.ledger.path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM memory_claim_reviews ORDER BY created_at")]
            except sqlite3.OperationalError:
                return []

    def conflict_count(self):
        return len(self.ledger.list_claims(state="conflict"))

    def memory_flags(self):
        with sqlite3.connect(self.ledger.path) as conn:
            return {row[0]: (row[1], row[2]) for row in conn.execute(
                "SELECT memory_id,quality_state,lifecycle FROM memories")}


class InventoryTests(LedgerCase):
    def test_groups_conflict_claims_by_slot(self):
        self.genuine_pair()
        groups = self.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].subject, "svc")
        self.assertEqual(groups[0].slot_key, "port")
        self.assertEqual(len(groups[0].sides), 2)

    def test_clean_ledger_has_no_groups(self):
        self.ingest([_claim("port", "5432", 0.9)], "只有一个端口")
        self.assertEqual(self.groups(), [])

    def test_classify_genuine(self):
        self.genuine_pair()
        self.assertEqual(self.group_for("port").cause, "genuine")

    def test_classify_duplicate(self):
        self.duplicate_setup()
        group = self.group_for("theme")
        self.assertEqual(group.cause, "duplicate")
        self.assertEqual(len({s.value_hash for s in group.sides}), 1,
                         "重复组的 value_hash 必须相同")

    def test_classify_orphan(self):
        self.orphan_setup()
        group = self.group_for("pdf")
        self.assertEqual(group.cause, "orphan")
        self.assertEqual(len(group.sides), 1)

    def test_classify_truncation(self):
        self.truncation_setup()
        self.assertEqual(self.group_for("scope").cause, "truncation")

    def test_classify_ignores_a_duplicate_pair_when_a_third_side_differs(self):
        """A slot can hold three sides; the top two are often identical.

        Production had exactly this: two memories asserting the same
        release_target while a third said something slightly longer.  Looking
        only at the two strongest sides classified it as a real disagreement.
        """
        self.ingest([_claim("target", "已配置的 GitHub 目标", 0.80)], "目标甲")
        self.ingest([_claim("target", "已配置的 GitHub 目标", 0.80)], "目标乙")
        self.ingest([_claim("target", "已配置的 GitHub 目标仓库", 0.70)], "目标丙")
        group = self.group_for("target")
        self.assertEqual(len(group.sides), 3)
        # Two sides agree, one differs only by a trailing word: that is a
        # truncation-shaped disagreement, never "genuine".
        self.assertNotEqual(group.cause, "genuine")
        self.assertIn(group.cause, ("duplicate", "truncation"))

    def test_classify_stale_when_every_side_is_out_of_recall(self):
        self.genuine_pair()
        with sqlite3.connect(self.ledger.path) as conn:
            conn.execute("UPDATE memories SET lifecycle='superseded', "
                         "quality_state='superseded'")
        self.assertEqual(self.group_for("port").cause, "stale")

    def test_resolved_slot_leaves_no_group(self):
        self.genuine_pair()
        group = self.group_for("port")
        winner = group.ranked()[0]
        self.ledger.settle_claim_slot(winner.claim_id, winner.memory_id)
        self.assertEqual(self.groups(), [])

    def test_groups_are_sorted_by_cause_certainty(self):
        self.genuine_pair()
        self.duplicate_setup()
        causes = [g.cause for g in self.groups()]
        self.assertEqual(causes, sorted(causes, key=cr.CAUSES.index),
                         "最确定的根因应排在最前，便于先处理")

    def test_json_shape_is_stable(self):
        self.genuine_pair()
        payload = self.group_for("port").as_dict()
        self.assertEqual(payload["cause"], "genuine")
        self.assertEqual(len(payload["sides"]), 2)
        for key in ("claim_id", "value", "confidence", "source_trust",
                    "asserted_at", "claim_state", "memory_state", "excerpt"):
            self.assertIn(key, payload["sides"][0])
        json.dumps(payload, ensure_ascii=False)


class RulesDeciderTests(LedgerCase):
    def test_duplicate_verdict_supersedes_the_weaker_side(self):
        self.duplicate_setup()
        verdict = cr.rules_decide(self.group_for("theme"))
        self.assertEqual(verdict.verdict, "supersede_a")
        self.assertGreaterEqual(verdict.confidence, 0.9)
        self.assertEqual(len(verdict.retire), 1)

    def test_genuine_verdict_escalates_instead_of_guessing(self):
        self.genuine_pair()
        verdict = cr.rules_decide(self.group_for("port"))
        self.assertEqual(verdict.verdict, "needs_human")
        self.assertEqual(verdict.winner, "")
        self.assertEqual(verdict.retire, [])

    def test_orphan_verdict_keeps_the_last_standing_side(self):
        self.orphan_setup()
        verdict = cr.rules_decide(self.group_for("pdf"))
        # The single surviving side is crowned; nothing is retired.
        self.assertEqual(verdict.verdict, "supersede_a")
        self.assertGreaterEqual(verdict.confidence, 0.9)
        self.assertEqual(verdict.retire, [])
        self.assertTrue(verdict.winner)

    def test_truncation_verdict_keeps_the_complete_value(self):
        full = self.truncation_setup()
        group = self.group_for("scope")
        verdict = cr.rules_decide(group)
        self.assertEqual(verdict.verdict, "supersede_a")
        self.assertIn(verdict.winner,
                      [s.claim_id for s in group.sides
                       if s.memory_id == full.memory_id])

    def test_every_verdict_is_one_of_the_four(self):
        self.genuine_pair()
        self.duplicate_setup()
        for group in self.groups():
            self.assertIn(cr.rules_decide(group).verdict, cr.VERDICTS)


class ParseVerdictTests(unittest.TestCase):
    def setUp(self):
        self.a = cr.Side("cl_a", "mem_a", "1", "h1", 0.9, 0.5, "2026-01-01",
                         "conflict", "accepted", "active", "2026-01-01", "t", "x")
        self.b = cr.Side("cl_b", "mem_b", "2", "h2", 0.5, 0.5, "2026-01-02",
                         "conflict", "accepted", "active", "2026-01-02", "t", "y")

    def test_plain_json(self):
        v = cr.parse_verdict(
            '{"verdict":"supersede_b","confidence":0.8,"reason":"B 更晚"}',
            a=self.a, b=self.b)
        self.assertEqual(v.verdict, "supersede_b")
        self.assertEqual(v.winner, "cl_b")
        self.assertEqual(v.retire, ["cl_a"])

    def test_markdown_fenced_json(self):
        text = '```json\n{"verdict":"keep_both","confidence":0.7,"reason":"x"}\n```'
        v = cr.parse_verdict(text, a=self.a, b=self.b)
        self.assertEqual(v.verdict, "keep_both")
        self.assertEqual(v.winner, "")

    def test_unknown_verdict_is_rejected(self):
        v = cr.parse_verdict('{"verdict":"delete_everything","confidence":1}',
                             a=self.a, b=self.b)
        self.assertIsNone(v, "未知裁决必须拒绝，不能被强转成破坏性动作")

    def test_non_json_is_rejected(self):
        self.assertIsNone(cr.parse_verdict("我觉得 A 对", a=self.a, b=self.b))

    def test_confidence_is_clamped(self):
        v = cr.parse_verdict('{"verdict":"keep_both","confidence":9}',
                             a=self.a, b=self.b)
        self.assertEqual(v.confidence, 1.0)


class DryRunTests(LedgerCase):
    def test_dry_run_writes_nothing(self):
        self.genuine_pair()
        before = self.claim_states()
        run = cr.review(self.ledger, decider="rules", apply=False)
        self.assertTrue(run.dry_run)
        self.assertEqual(run.applied, 0)
        self.assertEqual(self.claim_states(), before, "dry-run 不得改动 claim")
        self.assertEqual(self.review_rows(), [], "dry-run 不得写审计表")

    def test_dry_run_still_reports_verdicts(self):
        self.duplicate_setup()
        run = cr.review(self.ledger, decider="rules", apply=False)
        self.assertTrue(run.results)
        for item in run.results:
            self.assertIn(item["verdict"], cr.VERDICTS)
            self.assertIn("dry-run", item["outcome"])

    def test_dry_run_text_rendering_mentions_dry_run(self):
        self.duplicate_setup()
        run = cr.review(self.ledger, decider="rules", apply=False)
        text = "\n".join(cr.format_run(run))
        self.assertIn("DRY-RUN", text)
        self.assertIn("根因分布", text)


class ApplyTests(LedgerCase):
    def test_apply_duplicate_clears_that_slot_only(self):
        self.duplicate_setup()
        before = self.conflict_count()
        self.assertGreaterEqual(before, 2)
        run = cr.review(self.ledger, decider="rules", apply=True,
                        only_causes=["duplicate"])
        self.assertEqual(run.errors, 0)
        self.assertEqual(run.applied, 1)
        self.assertNotIn("theme", [g.slot_key for g in self.groups()],
                         "重复槽位应被清空")
        self.assertIn("port", [g.slot_key for g in self.groups()],
                      "无关的真冲突不得被顺手处理")

    def test_apply_writes_an_auditable_review_row(self):
        self.duplicate_setup()
        cr.review(self.ledger, decider="rules", apply=True,
                  only_causes=["duplicate"])
        rows = self.review_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["decider"], "rules")
        self.assertEqual(row["verdict"], "supersede_a")
        self.assertEqual(row["cause"], "duplicate")
        self.assertTrue(row["reason"], "必须留理由")
        self.assertTrue(row["evidence_digest"].startswith("sha256:"))
        self.assertNotEqual(row["value_hash_a"], "")
        self.assertEqual(row["applied"], 1)

    def test_apply_is_idempotent(self):
        self.duplicate_setup()
        first = cr.review(self.ledger, decider="rules", apply=True,
                          only_causes=["duplicate"])
        self.assertEqual(first.applied, 1)
        second = cr.review(self.ledger, decider="rules", apply=True,
                           only_causes=["duplicate"])
        self.assertEqual(second.groups, [], "第二次没有重复冲突可处理")
        self.assertEqual(len(self.review_rows()), 1, "重复 apply 不得重复写审计行")

    def test_apply_escalation_leaves_the_ledger_untouched(self):
        self.genuine_pair()
        before = self.claim_states()
        run = cr.review(self.ledger, decider="rules", apply=True)
        self.assertEqual(run.applied, 0)
        self.assertEqual(self.claim_states(), before, "needs_human 不得改动账本")
        rows = self.review_rows()
        self.assertEqual(len(rows), 1, "即使不改账本也要留审计")
        self.assertEqual(rows[0]["verdict"], "needs_human")
        self.assertEqual(rows[0]["applied"], 0)

    def test_apply_keep_both_releases_without_retiring(self):
        a, b = self.genuine_pair()
        verdict = cr.Verdict("keep_both", 0.9, "两侧都成立", "test")
        group = self.group_for("port")
        outcome = cr.apply_verdict(self.ledger, group, verdict)
        self.assertTrue(outcome["applied"])
        states = self.claim_states()
        self.assertEqual(states[(a.memory_id, "port")], "active")
        self.assertEqual(states[(b.memory_id, "port")], "active")
        self.assertEqual(self.ledger.claims_consistency_report()["dangling"], 0)

    def test_apply_never_touches_other_slots_of_the_losing_memory(self):
        """The whole reason apply does not call resolve_claim."""
        loser = self.ingest(
            [_claim("port", "5432", 0.50), _claim("region", "us", 0.90)],
            "端口 5432 区域 us")
        winner = self.ingest([_claim("port", "6432", 0.52)], "端口 6432")
        verdict = cr.Verdict("supersede_a", 0.95, "test", "test",
                             winner=next(s.claim_id for s
                                         in self.group_for("port").sides
                                         if s.memory_id == winner.memory_id))
        outcome = cr.apply_verdict(self.ledger, self.group_for("port"), verdict)
        self.assertTrue(outcome["applied"])
        states = self.claim_states()
        self.assertEqual(states[(winner.memory_id, "port")], "active")
        self.assertEqual(states[(loser.memory_id, "region")], "active",
                         "败者的其它槽位事实必须保留")
        self.assertEqual(self.ledger.claims_consistency_report()["dangling"], 0)

    def test_apply_survives_a_failing_write_without_corrupting_state(self):
        self.duplicate_setup()
        before = self.claim_states()
        original = self.ledger.settle_claim_slot

        def boom(*args, **kwargs):
            raise LedgerError("模拟写失败")

        self.ledger.settle_claim_slot = boom
        try:
            run = cr.review(self.ledger, decider="rules", apply=True,
                            only_causes=["duplicate"])
        finally:
            self.ledger.settle_claim_slot = original
        self.assertEqual(run.errors, 1)
        self.assertEqual(self.claim_states(), before, "失败不得破坏 claim 状态")
        rows = self.review_rows()
        self.assertEqual(len(rows), 1, "失败也要留审计")
        self.assertEqual(rows[0]["applied"], 0)
        self.assertIn("模拟写失败", rows[0]["outcome"])

    def test_limit_processes_only_the_first_group(self):
        self.duplicate_setup()
        self.assertEqual(len(self.groups()), 2)
        run = cr.review(self.ledger, decider="rules", apply=True, limit=1)
        self.assertEqual(len(run.groups), 1)

    def test_cause_filter_narrows_the_run(self):
        self.genuine_pair()
        self.duplicate_setup()
        run = cr.review(self.ledger, decider="rules", apply=False,
                        only_causes=["genuine"])
        self.assertEqual([g.cause for g in run.groups], ["genuine"])


class ModelDeciderTests(LedgerCase):
    """The model decider, driven through its runner injection seam."""

    def _decider(self, answers):
        replies = list(answers)

        def runner(prompt):
            self.assertIn("证据 A", prompt)
            self.assertIn("证据 B", prompt)
            return replies.pop(0)

        return cr.ClaudeDecider(runner=runner, timeout=1.0)

    def test_model_verdict_is_applied_for_a_genuine_group(self):
        self.genuine_pair()
        decider = self._decider([
            '{"verdict":"supersede_a","confidence":0.9,"reason":"A 更可信"}'])
        run = cr.review(self.ledger, decider="llm", apply=True, llm=decider)
        self.assertEqual(run.errors, 0)
        self.assertEqual(run.llm_calls, 1)
        self.assertEqual(self.review_rows()[0]["decider"], "llm")

    def test_model_is_not_consulted_for_non_genuine_causes(self):
        """truncation is conclusive, so it must not cost a model call."""
        self.truncation_setup()
        causes = {g.cause for g in self.groups()}
        self.assertEqual(causes, {"truncation"})
        decider = self._decider([])  # would IndexError if ever called
        run = cr.review(self.ledger, decider="llm", apply=False, llm=decider)
        self.assertEqual(run.llm_calls, 0, "非真冲突不该花模型调用")
        self.assertTrue(run.results)

    def test_failed_model_falls_back_to_rules(self):
        self.genuine_pair()

        def runner(prompt):
            raise RuntimeError("API 挂了")

        decider = cr.ClaudeDecider(runner=runner, timeout=1.0)
        run = cr.review(self.ledger, decider="llm", apply=False, llm=decider)
        self.assertEqual(run.llm_failures, 1)
        self.assertIn("回退规则", run.results[0]["reason"])
        self.assertIn(run.results[0]["verdict"], cr.VERDICTS)

    def test_breaker_stops_calling_after_repeated_failures(self):
        """A dead API must not cost one timeout per group."""
        for index in range(4):
            self.ingest([_claim("slot%d" % index, "a", 0.50,
                                subject="s%d" % index)], "甲 %d" % index)
            self.ingest([_claim("slot%d" % index, "b", 0.52,
                                subject="s%d" % index)], "乙 %d" % index)
        calls = []

        def runner(prompt):
            calls.append(1)
            raise RuntimeError("API 挂了")

        decider = cr.ClaudeDecider(runner=runner, timeout=1.0)
        run = cr.review(self.ledger, decider="llm", apply=False, llm=decider)
        self.assertEqual(len(calls), cr.BREAKER_THRESHOLD,
                         "熔断后不应再调用模型")
        self.assertTrue(run.llm_breaker)
        self.assertEqual(len(run.results), 4, "其余组仍要出裁决（走规则）")

    def test_unparseable_output_is_a_failure_not_a_verdict(self):
        self.genuine_pair()
        decider = self._decider(["我觉得都行"])
        run = cr.review(self.ledger, decider="llm", apply=False, llm=decider)
        self.assertEqual(run.llm_failures, 1)
        self.assertIn("回退规则", run.results[0]["reason"])

    def test_low_confidence_supersede_is_escalated(self):
        self.genuine_pair()
        decider = self._decider([
            '{"verdict":"supersede_a","confidence":0.2,"reason":"不太确定"}'])
        run = cr.review(self.ledger, decider="llm", apply=False, llm=decider)
        self.assertEqual(run.results[0]["verdict"], "needs_human")
        self.assertIn("低于阈值", run.results[0]["reason"])

    def test_decider_without_binary_or_runner_is_unavailable(self):
        # use_api=False: "no transport at all" is the state under test, so the
        # direct API must not quietly make this decider available.
        decider = cr.ClaudeDecider(binary="/nonexistent/claude", use_api=False)
        decider.binary = ""
        self.assertFalse(decider.available)
        self.assertIsNone(decider.decide(self.group_for("port"))
                          if self.groups() else None)


class LifecycleHelperTests(LedgerCase):
    """The slot-scoped ledger methods the script depends on."""

    def test_release_keeps_both_sides_and_the_invariant(self):
        self.genuine_pair()
        claim = self.ledger.list_claims(state="conflict")[0]
        result = self.ledger.release_claim_slot(claim["claim_id"])
        self.assertTrue(result["slot_cleared"])
        self.assertEqual(self.conflict_count(), 0)
        self.assertEqual(self.ledger.claims_consistency_report()["dangling"], 0)
        self.assertEqual(self.ledger.repair_claim_states()["conflict"], 0)

    def test_release_refuses_a_memory_that_still_has_other_conflicts(self):
        """Releasing must never manufacture a dangling active claim."""
        self.orphan_setup()
        multi_claim = next(
            c for c in self.ledger.list_claims(state="conflict")
            if c["slot_key"] == "pdf")
        result = self.ledger.release_claim_slot(multi_claim["claim_id"])
        self.assertEqual(result["released"], [])
        self.assertTrue(result["blocked"])
        self.assertEqual(self.ledger.claims_consistency_report()["dangling"], 0)

    def test_settle_crowns_one_side_only_in_that_slot(self):
        loser = self.ingest(
            [_claim("port", "5432", 0.50), _claim("region", "us", 0.90)],
            "端口与区域")
        winner = self.ingest([_claim("port", "6432", 0.52)], "端口 6432")
        claim = next(c for c in self.ledger.list_claims(state="conflict")
                     if c["memory_id"] == loser.memory_id
                     and c["slot_key"] == "port")
        result = self.ledger.settle_claim_slot(claim["claim_id"],
                                               winner.memory_id)
        self.assertEqual(result["winner"], winner.memory_id)
        states = self.claim_states()
        self.assertEqual(states[(winner.memory_id, "port")], "active")
        self.assertEqual(states[(loser.memory_id, "port")], "superseded")
        self.assertEqual(states[(loser.memory_id, "region")], "active")
        self.assertEqual(self.ledger.claims_consistency_report()["dangling"], 0)

    def test_settle_without_a_winner_retires_the_slot(self):
        self.genuine_pair()
        claim = self.ledger.list_claims(state="conflict")[0]
        result = self.ledger.settle_claim_slot(claim["claim_id"], "")
        self.assertEqual(result["winner"], "")
        self.assertTrue(result["retired"])
        self.assertEqual(self.conflict_count(), 0)
        self.assertEqual(self.ledger.claims_consistency_report()["dangling"], 0)

    def test_settle_rejects_unknown_claim_and_foreign_winner(self):
        self.genuine_pair()
        claim = self.ledger.list_claims(state="conflict")[0]
        with self.assertRaises(LedgerError):
            self.ledger.settle_claim_slot("cl_missing", "")
        with self.assertRaises(LedgerError):
            self.ledger.settle_claim_slot(claim["claim_id"], "mem_missing")
        with self.assertRaises(LedgerError):
            self.ledger.release_claim_slot("cl_missing")

    def test_release_retires_stale_conflicts_on_a_dead_memory(self):
        """A conflict on a superseded memory is bookkeeping, not a dispute."""
        self.orphan_setup()
        with sqlite3.connect(self.ledger.path) as conn:
            conn.execute("UPDATE memories SET lifecycle='superseded', "
                         "quality_state='superseded'")
        claim = self.ledger.list_claims(state="conflict")[0]
        result = self.ledger.release_claim_slot(claim["claim_id"])
        self.assertEqual(result["released"], [], "死记忆不得被放回 active")
        self.assertTrue(result["slot_cleared"], "清账必须如实报告为已处理")
        self.assertTrue(result["stale_retired"])
        self.assertEqual(self.conflict_count(), 0, "陈旧冲突应被清账")
        self.assertEqual(self.ledger.claims_consistency_report()["dangling"], 0)

    def test_release_writes_a_ledger_event(self):
        self.genuine_pair()
        claim = self.ledger.list_claims(state="conflict")[0]
        self.ledger.release_claim_slot(claim["claim_id"])
        with sqlite3.connect(self.ledger.path) as conn:
            types = [r[0] for r in conn.execute(
                "SELECT event_type FROM ledger_events WHERE event_type IN "
                "('release','resolve')")]
        self.assertTrue(types, "释放/裁决必须落审计事件")

    def test_settle_writes_a_ledger_event(self):
        self.genuine_pair()
        claim = self.ledger.list_claims(state="conflict")[0]
        self.ledger.settle_claim_slot(claim["claim_id"], "")
        with sqlite3.connect(self.ledger.path) as conn:
            types = [r[0] for r in conn.execute(
                "SELECT event_type FROM ledger_events WHERE event_type='resolve'")]
        self.assertTrue(types)

    def test_existing_resolve_claim_semantics_are_unchanged(self):
        """The pre-existing memory-wide API must still behave as before."""
        a, b = self.genuine_pair()
        claim = next(c for c in self.ledger.list_claims(state="conflict")
                     if c["memory_id"] == a.memory_id)
        result = self.ledger.resolve_claim(claim["claim_id"], b.memory_id)
        self.assertEqual(result["winner"], b.memory_id)
        self.assertIn(a.memory_id, result["losers"])
        flags = self.memory_flags()
        self.assertEqual(flags[b.memory_id][0], "accepted")
        self.assertEqual(flags[a.memory_id][1], "superseded")


class MainCliTests(LedgerCase):
    def test_show_reviews_on_a_fresh_ledger(self):
        text = "\n".join(cr.show_reviews(self.ledger))
        self.assertIn("尚无裁决记录", text)

    def test_json_output_is_parseable(self):
        self.duplicate_setup()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cr.main(["--ledger", str(self.ledger.path), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["groups"], 2)
        self.assertTrue(payload["dry_run"])
        self.assertIn("causes", payload)

    def test_apply_and_dry_run_are_mutually_exclusive(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = cr.main(["--ledger", str(self.ledger.path),
                            "--apply", "--dry-run"])
        self.assertEqual(code, 2)

    def test_cli_apply_writes_and_reports(self):
        self.duplicate_setup()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cr.main(["--ledger", str(self.ledger.path), "--apply",
                            "--cause", "duplicate"])
        self.assertEqual(code, 0)
        self.assertIn("APPLY", buffer.getvalue())
        self.assertEqual(len(self.review_rows()), 1)


if __name__ == "__main__":
    unittest.main()
