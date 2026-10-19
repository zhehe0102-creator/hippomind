import unittest
import tempfile
from pathlib import Path

from hippomind.ledger import SQLiteLedger
from hippomind.retrieval_eval import (ACTRActivation, reciprocal_rank_fusion,
                                      run_english_qa_suite,
                                      run_memory_qa_suite)


class RetrievalEvalTests(unittest.TestCase):
    def test_rrf_rewards_consensus_without_duplicate_votes(self):
        ranked = reciprocal_rank_fusion([
            ["common", "lexical-only", "tail"],
            ["semantic-only", "common", "tail"],
        ])
        self.assertEqual(ranked[0][0], "common")
        self.assertGreater(ranked[0][1], ranked[-1][1])

    def test_actr_rank_is_finite_and_uses_spaced_accesses(self):
        model = ACTRActivation(decay=0.5)
        now = 1_000_000.0
        records = [
            {"memory_id": "spaced", "access_times": [now - 86_400, now - 7 * 86_400]},
            {"memory_id": "burst", "access_times": [now - 10, now - 20]},
        ]
        ranked = model.rank(records, now=now)
        self.assertEqual({item[0] for item in ranked}, {"spaced", "burst"})
        self.assertTrue(all(value == value and abs(value) < 100 for _, value in ranked))


class MemoryQABenchmarkTests(unittest.TestCase):
    """中文记忆 QA 基准：40 例四类，作为检索质量的滚动回归门槛。"""

    def test_suite_reaches_quality_thresholds(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        counter = {"n": 0}

        def ledger_factory():
            counter["n"] += 1
            return SQLiteLedger(Path(tmp.name) / f"eval-{counter['n']}.sqlite")

        metrics = run_memory_qa_suite(ledger_factory)
        # 基线门槛（首版实测：recall 0.94 / update 1.0 / temporal 1.0 / abstain 1.0）
        self.assertGreaterEqual(metrics["recall_at_8"], 0.75,
                                f"事实召回不达标: {metrics}")
        self.assertGreaterEqual(metrics["update_accuracy"], 0.85,
                                f"知识更新不达标: {metrics}")
        self.assertGreaterEqual(metrics["temporal_accuracy"], 0.85,
                                f"时间推理不达标: {metrics}")
        self.assertGreaterEqual(metrics["abstention_precision"], 0.90,
                                f"弃权精度不达标: {metrics}")
        self.assertEqual(metrics["cases"], 40)

    def test_english_suite_cross_language_generalization(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        counter = {"n": 0}

        def ledger_factory():
            counter["n"] += 1
            return SQLiteLedger(Path(tmp.name) / f"en-{counter['n']}.sqlite")

        metrics = run_english_qa_suite(ledger_factory)
        self.assertGreaterEqual(metrics["recall_at_8"], 0.75,
                                f"英文召回不达标: {metrics}")
        self.assertGreaterEqual(metrics["update_accuracy"], 0.85,
                                f"英文更新不达标: {metrics}")
        self.assertGreaterEqual(metrics["abstention_precision"], 0.90,
                                f"英文弃权不达标: {metrics}")
        self.assertEqual(metrics["cases"], 32)

    def test_update_cases_leave_old_value_out_of_recall(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = SQLiteLedger(Path(tmp.name) / "u.sqlite")
        case = dict(UPDATE_CASES[0])
        for rec in (case["old"], case["new"]):
            rec = dict(rec)
            rec.setdefault("subject", "s")
            rec.setdefault("predicate", "p")
            rec.setdefault("slot_key", "k")
            ledger.ingest(rec)
        hits = ledger.search(case["query"], scope="shared", limit=8)
        joined = " ".join(str(h["content"]) for h in hits).lower()
        self.assertIn("/v2/pay", joined)
        self.assertNotIn("/v1/charge", joined)


from hippomind.retrieval_eval import FACT_CASES, UPDATE_CASES  # noqa: E402


if __name__ == "__main__":
    unittest.main()
