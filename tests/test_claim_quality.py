"""Claim-value quality gate: reject values that assert nothing, keep short facts.

The gate exists because the API extractor occasionally emits a value that carries
no information ("3", a bare state word) on short source snippets.  Such a claim
occupies a slot, can manufacture a conflict against a real fact, and puts a
meaningless row in front of retrieval.

The hard part is *not* rejecting too much.  Production holds short values that
are entirely load-bearing -- "6000" (token budget), "pa3q" (device codename),
"SQLite", "ebpf", "/dev/tmp", "v2.2" -- so a length threshold would destroy real
knowledge.  These tests therefore pin both directions: the degenerate values are
rejected, and the meaningful short values survive, keyed on slot semantics.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.distill import (  # noqa: E402
    DistillEngine,
    classify_claim_value,
    is_generic_slot,
    slot_tail,
)
from hippomind.ledger import SQLiteLedger  # noqa: E402


class GateCase(unittest.TestCase):
    def reason(self, slot, value, predicate="p"):
        return classify_claim_value(slot, predicate, value)

    def assertRejected(self, slot, value, predicate="p"):
        self.assertNotEqual("", self.reason(slot, value, predicate),
                            "%r in slot %r should be rejected" % (value, slot))

    def assertKept(self, slot, value, predicate="p"):
        self.assertEqual("", self.reason(slot, value, predicate),
                         "%r in slot %r must be kept" % (value, slot))


class MeaningfulShortValuesAreKept(GateCase):
    """The whole point of the gate: never delete a short but real fact."""

    def test_short_identifiers_survive(self):
        for slot, value in [
            ("device_codename", "pa3q"),
            ("tech.database", "SQLite"),
            ("db.engine", "SQLite"),
            ("host", "mac-mini"),
            ("version", "v1.5.2"),
            ("installed_version", "v2.2"),
            ("active_backend", "ebpf"),
            ("service_backend", "eBPF"),
            ("cpu_governor", "walt"),
            ("worker_tmpdir_value", "/dev/tmp"),
            ("node_config.group_id", "default"),
            ("shell", "/bin/zsh"),
            ("os_version", "darwin"),
        ]:
            self.assertKept(slot, value)

    def test_bare_numbers_in_a_numeric_slot_survive(self):
        """6000 is a token budget; deleting it would lose a real constraint."""
        for slot, value in [
            ("default_token_budget", "6000"),
            ("third_party_app_count", "259"),
            ("wake_lock_count", "1"),
            ("default_timeout_seconds", "120"),
            ("update_interval", "86400"),
            ("compile_sdk_version", "37"),
            ("node_config.revision", "1"),
            ("gateway_daemon_port", "46240"),
            ("design.metrics.radius", "12"),
        ]:
            self.assertKept(slot, value)

    def test_bare_booleans_and_states_in_a_typed_slot_survive(self):
        """In a slot that names a predicate, "true" IS the answer."""
        for slot, value in [
            ("bash_requires_approval", "true"),
            ("auto_update", "false"),
            ("sqlite3_installed", "false"),
            ("has_existing_user_data", "true"),
            ("dex_signature_match", "true"),
            ("task_status", "正常"),
            ("node_import.status", "成功"),
            ("e2e_validation_status", "通过"),
            ("recent_errors", "无"),
        ]:
            self.assertKept(slot, value)

    def test_a_short_number_is_not_killed_for_being_short(self):
        """No length threshold exists; only slot semantics decide."""
        for n in ("1", "2", "3", "8", "12", "99", "259", "6000"):
            self.assertKept("default_token_budget", n)


class DegenerateValuesAreRejected(GateCase):
    """The values the gate exists for: short, in a slot that says nothing."""

    def test_bare_numbers_in_a_generic_slot_are_rejected(self):
        self.assertRejected("statement", "3")
        self.assertRejected("note", "3")
        self.assertRejected("misc", "17")
        self.assertRejected("fact", "6000")

    def test_bare_state_words_in_a_generic_slot_are_rejected(self):
        self.assertRejected("statement", "可用")
        self.assertRejected("note", "不可用")
        self.assertRejected("statement", "正常")
        self.assertRejected("misc", "成功")
        self.assertRejected("statement", "true")
        self.assertRejected("note", "false")

    def test_single_characters_are_rejected(self):
        self.assertRejected("statement", "3")
        self.assertRejected("note", "无")

    def test_punctuation_and_empty_values_are_rejected_everywhere(self):
        """These assert nothing in any slot, typed or not."""
        for slot in ("statement", "default_token_budget", "host"):
            self.assertRejected(slot, "")
            self.assertRejected(slot, "   ")
            self.assertRejected(slot, "-")
            self.assertRejected(slot, "--")
            self.assertRejected(slot, "...")

    def test_distill_heuristic_slots_are_treated_as_generic(self):
        """The heuristic has no stable global slot, so its slots say nothing."""
        self.assertTrue(is_generic_slot("mem_4cb53bf6:s_9f8e7d"))
        self.assertRejected("mem_4cb53bf6:s_9f8e7d", "3")
        self.assertRejected("mem_4cb53bf6:s_9f8e7d", "可用")

    def test_long_values_are_never_rejected_by_the_value_rules(self):
        long_value = "决定采用 SQLite 作为存储，因为需要 WAL 并发"
        for slot in ("statement", "note", "default_token_budget"):
            self.assertKept(slot, long_value)


class SameValueDifferentSlot(GateCase):
    """The requirement spelled out: one value, two slots, two verdicts."""

    def test_the_same_bare_number_depends_on_the_slot(self):
        self.assertKept("default_token_budget", "3")
        self.assertRejected("statement", "3")

    def test_the_same_state_word_depends_on_the_slot(self):
        self.assertKept("task_status", "正常")
        self.assertRejected("note", "正常")

    def test_the_same_boolean_depends_on_the_slot(self):
        self.assertKept("bash_requires_approval", "true")
        self.assertRejected("statement", "true")


class TautologyIsRejected(GateCase):
    def test_a_value_that_repeats_the_slot_is_rejected(self):
        self.assertRejected("project_name", "project_name")

    def test_a_value_that_repeats_the_predicate_is_KEPT(self):
        """Measured: all such cases were real facts with a redundant predicate.

        subject=审计脚本 predicate=未运行 value=未运行 still says the script is
        not running.  Rejecting it would trade information for tidiness, so
        the rule stops at slot-name repetition.
        """
        self.assertKept("usage", "调用处保持写法不变",
                        predicate="调用处保持写法不变")
        self.assertKept("execution_status", "未运行", predicate="未运行")

    def test_an_unrelated_value_in_the_same_slot_is_kept(self):
        self.assertKept("project_name", "relayomni-work")


class SlotHelpers(GateCase):
    def test_slot_tail_strips_the_memory_prefix(self):
        self.assertEqual("s_9f8e7d", slot_tail("mem_abc123:s_9f8e7d"))
        self.assertEqual("task_status", slot_tail("task_status"))
        self.assertEqual("", slot_tail(""))

    def test_generic_slot_detection(self):
        for slot in ("statement", "note", "misc", "summary", "description"):
            self.assertTrue(is_generic_slot(slot), slot)
        for slot in ("task_status", "default_token_budget", "tech.database",
                     "device_codename", "host"):
            self.assertFalse(is_generic_slot(slot), slot)

    def test_case_insensitive(self):
        self.assertTrue(is_generic_slot("STATEMENT"))
        self.assertTrue(is_generic_slot("Note"))


class EngineIntegration(unittest.TestCase):
    """The gate must run before the claim reaches the ledger, and be countable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-claimq-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.ledger.ingest({
            "content": "决定采用 SQLite 作为存储，因为需要 WAL 并发。",
            "kind": "decision", "source_agent": "codex", "scope": "shared",
            "evidence": ["codex:1"], "timestamp": "2026-10-24T00:00:00Z"})

    def _engine(self, claims):
        class Fake:
            name = "fake"
            available = True
            def extract(self, candidates):
                return [dict(c) for c in claims]
        return DistillEngine(self.ledger, extractor=Fake())

    def _stored_claims(self):
        with self.ledger._reading() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT slot_key, value_json FROM memory_claims").fetchall()]

    def test_a_degenerate_claim_never_reaches_the_ledger(self):
        engine = self._engine([
            {"subject": "s", "predicate": "p", "slot_key": "statement",
             "value": "3", "confidence": 0.9, "source": 1}])
        result = engine.run(limit=4)
        self.assertEqual(1, result["claims_dropped_total"])
        self.assertEqual(0, len(self._stored_claims()))
        self.assertGreaterEqual(result["claims_dropped"].get("generic_single_char", 0), 1)

    def test_a_meaningful_short_claim_is_stored(self):
        engine = self._engine([
            {"subject": "atlas", "predicate": "预算",
             "slot_key": "default_token_budget", "value": "6000",
             "confidence": 0.9, "source": 1}])
        result = engine.run(limit=4)
        self.assertEqual(0, result["claims_dropped_total"])
        self.assertEqual(1, result["distilled"])
        stored = self._stored_claims()
        self.assertEqual(1, len(stored))
        self.assertEqual("default_token_budget", stored[0]["slot_key"])

    def test_a_mixed_batch_keeps_the_good_claim_and_drops_the_bad_one(self):
        engine = self._engine([
            {"subject": "atlas", "predicate": "预算",
             "slot_key": "default_token_budget", "value": "6000",
             "confidence": 0.9, "source": 1},
            {"subject": "atlas", "predicate": "陈述", "slot_key": "statement",
             "value": "可用", "confidence": 0.9, "source": 1}])
        result = engine.run(limit=4)
        self.assertEqual(1, result["claims_dropped_total"])
        self.assertEqual(1, result["distilled"])
        stored = self._stored_claims()
        self.assertEqual(["default_token_budget"], [r["slot_key"] for r in stored])

    def test_a_row_whose_claims_are_all_dropped_is_retired_not_retried(self):
        engine = self._engine([
            {"subject": "s", "predicate": "p", "slot_key": "note",
             "value": "-", "confidence": 0.9, "source": 1}])
        first = engine.run(limit=4)
        self.assertGreaterEqual(first["skipped"], 1)
        second = engine.run(limit=4)
        self.assertEqual(0, second["candidates"],
                         "全被过滤的行必须打游标，否则会永远重试")


if __name__ == "__main__":
    unittest.main()
