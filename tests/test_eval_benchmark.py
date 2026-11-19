"""Tests for the benchmark-style retrieval gate (scripts/eval_benchmark.py).

All ledgers are created inside temporary directories: these tests never read or
write the production ledger.  The bundled fixture is exercised for shape and for
its committed gate, and synthetic fixtures prove that the new metrics can
actually fail (update-hit on a recalled old value, abstention on an injected
unrelated memory) instead of only ever printing PASS.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_benchmark  # noqa: E402


def _fact(content, evidence="ev-1", **extra):
    record = {
        "content": content,
        "kind": "fact",
        "scope": "shared",
        "source_agent": "tester",
        "evidence": [evidence],
    }
    record.update(extra)
    return record


def _claim(subject, slot, value, confidence, valid_from):
    return {"subject": subject, "predicate": "config", "slot_key": slot,
            "value": value, "confidence": confidence, "valid_from": valid_from}


def _fixture(memories, queries, *, set_version="test_v1"):
    return {
        "schema": 1,
        "set_version": set_version,
        "memories": list(memories),
        "queries": list(queries),
        "search_options": {"pack_min_score": 0.20, "include_raw_chat": False},
    }


class BenchmarkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-eval-benchmark-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write_fixture(self, fixture, name="fixture.json") -> Path:
        path = self.dir / name
        path.write_text(json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = eval_benchmark.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_json(self, argv):
        code, out, err = self.run_main(list(argv) + ["--json"])
        payload = json.loads(out)
        return code, payload, err

    def write_baseline(self, thresholds, *, set_version="test_v1", name="baseline.json"):
        path = self.dir / name
        path.write_text(json.dumps({
            "set_version": set_version,
            "measured": {},
            "thresholds": thresholds,
        }, ensure_ascii=False), encoding="utf-8")
        return path


class NumericHelperTests(unittest.TestCase):
    def test_percentile_linear_interpolation(self):
        self.assertAlmostEqual(eval_benchmark.percentile([1.0, 2.0, 3.0, 4.0], 0.5), 2.5)
        self.assertAlmostEqual(eval_benchmark.percentile([1.0, 2.0, 3.0, 4.0], 0.95), 3.85)
        self.assertEqual(eval_benchmark.percentile([], 0.95), 0.0)
        self.assertEqual(eval_benchmark.percentile([7.0], 0.95), 7.0)

    def test_duplicate_count_normalises_text(self):
        self.assertEqual(eval_benchmark._duplicate_count(["same", "same", "other"]), 1)
        self.assertEqual(eval_benchmark._duplicate_count(["same", " other "]), 0)


class BundledFixtureTests(BenchmarkCase):
    def test_fixture_shape_and_capability_counts(self):
        fixture = eval_benchmark.load_fixture(eval_benchmark.DEFAULT_FIXTURE)
        self.assertEqual(48, len(fixture["queries"]))
        self.assertEqual(
            {"fact_recall": 12, "knowledge_update": 8, "temporal": 8,
             "continuity": 8, "abstention": 12},
            fixture["category_counts"])
        self.assertGreaterEqual(len(fixture["memories"]), 48)
        # The fixture must be self-consistent: load_fixture already validated that
        # every expect is backed by a memory and abstention terms never leak.
        self.assertEqual("benchmark_zh_v1", fixture["set_version"])

    def test_bundled_fixture_passes_committed_gate(self):
        code, payload, err = self.run_json([])
        self.assertEqual(0, code, err)
        self.assertTrue(payload["gate"]["enabled"])
        self.assertTrue(payload["gate"]["passed"], payload["gate"]["failures"])
        measured = payload["measured"]
        self.assertEqual(48, measured["queries"])
        self.assertEqual(set(eval_benchmark.CATEGORIES), set(measured["per_category"]))
        self.assertEqual([], payload["failures"])

    def test_human_report_lists_categories_and_gate(self):
        code, out, err = self.run_main([])
        self.assertEqual(0, code, err)
        for category in eval_benchmark.CATEGORIES:
            self.assertIn(category, out)
        self.assertIn("gate: PASS", out)
        self.assertIn("recall@k", out)


class MetricGateTests(BenchmarkCase):
    def clean_fixture(self):
        return _fixture(
            [_fact("Orion 网关限流阈值是每分钟 1200 次请求", "ev:fact")],
            [
                {"id": "fact-rate", "category": "fact_recall",
                 "query": "Orion 网关限流阈值", "expect": ["1200 次"]},
                {"id": "abs-mars", "category": "abstention",
                 "query": "火星基地的氧气配额是多少", "expect": [],
                 "expect_absent": ["火星", "氧气"]},
            ])

    def test_per_category_metrics_are_reported(self):
        result = eval_benchmark.evaluate(eval_benchmark.validate_fixture(self.clean_fixture()))
        measured = result["measured"]
        self.assertEqual(1.0, measured["per_category"]["fact_recall"]["recall_at_k"])
        self.assertEqual(1.0, measured["per_category"]["abstention"]["abstain_accuracy"])
        self.assertEqual(1.0, measured["abstain_accuracy"])
        self.assertEqual(0.0, measured["duplicate_rate"])
        self.assertGreaterEqual(measured["p95_ms"], 0.0)

    def test_update_hit_catches_a_recalled_old_value(self):
        fixture = _fixture(
            [
                _fact("Orion 搜索集群分片数为 8", "ev:old"),
                _fact("Orion 搜索集群分片数调整为 16", "ev:new"),
            ],
            [{"id": "upd", "category": "knowledge_update",
              "query": "Orion 搜索集群分片数", "expect": ["分片数调整为 16"],
              "expect_absent": ["分片数为 8"]}])
        result = eval_benchmark.evaluate(eval_benchmark.validate_fixture(fixture))
        self.assertEqual(0.0, result["measured"]["update_hit"])
        failed = eval_benchmark.query_failures(result["per_query"])
        self.assertEqual(1, len(failed))
        self.assertIn("旧值", failed[0]["reason"])

        path = self.write_fixture(fixture)
        baseline = self.write_baseline({"min_queries": 1, "min_update_hit": 0.9})
        code, payload, _ = self.run_json([
            "--fixture", str(path), "--baseline", str(baseline)])
        self.assertEqual(1, code)
        self.assertFalse(payload["gate"]["passed"])
        self.assertTrue(any("update_hit" in reason for reason in payload["gate"]["failures"]))

    def test_update_hit_passes_when_claims_supersede_the_old_value(self):
        old = _fact("Orion 搜索集群分片数为 8", "ev:old",
                    timestamp="2026-01-01T00:00:00+00:00",
                    claims=[_claim("orion-search", "shards", 8, 0.60,
                                   "2026-01-01T00:00:00+00:00")])
        new = _fact("Orion 搜索集群分片数调整为 16", "ev:new",
                    timestamp="2026-02-01T00:00:00+00:00",
                    claims=[_claim("orion-search", "shards", 16, 0.90,
                                   "2026-02-01T00:00:00+00:00")])
        fixture = _fixture([old, new], [{
            "id": "upd", "category": "knowledge_update",
            "query": "Orion 搜索集群分片数", "expect": ["分片数调整为 16"],
            "expect_absent": ["分片数为 8"]}])
        result = eval_benchmark.evaluate(eval_benchmark.validate_fixture(fixture))
        self.assertEqual(1.0, result["measured"]["update_hit"])

    def test_abstention_catches_an_injected_unrelated_memory(self):
        fixture = _fixture(
            [_fact("Orion 缓存服务部署在 cache-07 主机", "ev:cache")],
            [{"id": "abs-injected", "category": "abstention",
              "query": "Orion 缓存服务部署在哪个主机", "expect": [],
              "expect_absent": ["火星"]}])
        result = eval_benchmark.evaluate(eval_benchmark.validate_fixture(fixture))
        self.assertEqual(0.0, result["measured"]["abstain_accuracy"])
        failed = eval_benchmark.query_failures(result["per_query"])
        self.assertEqual(1, len(failed))
        self.assertIn("abstention", failed[0]["reason"])

        path = self.write_fixture(fixture)
        baseline = self.write_baseline({"min_queries": 1, "min_abstain_accuracy": 0.9})
        code, payload, _ = self.run_json([
            "--fixture", str(path), "--baseline", str(baseline)])
        self.assertEqual(1, code)
        self.assertTrue(any("abstain_accuracy" in reason
                            for reason in payload["gate"]["failures"]))

    def test_gate_min_queries_is_enforced(self):
        path = self.write_fixture(self.clean_fixture())
        baseline = self.write_baseline({"min_queries": 99})
        code, payload, _ = self.run_json([
            "--fixture", str(path), "--baseline", str(baseline)])
        self.assertEqual(1, code)
        self.assertTrue(any("min_queries" in reason for reason in payload["gate"]["failures"]))

    def test_gate_rejects_a_baseline_for_a_different_set_version(self):
        path = self.write_fixture(self.clean_fixture())
        baseline = self.write_baseline({"min_queries": 1}, set_version="other_v9")
        code, payload, _ = self.run_json([
            "--fixture", str(path), "--baseline", str(baseline)])
        self.assertEqual(1, code)
        self.assertTrue(any("set_version" in reason for reason in payload["gate"]["failures"]))

    def test_update_baseline_records_measured_and_conservative_thresholds(self):
        path = self.write_fixture(self.clean_fixture())
        baseline = self.dir / "generated-baseline.json"
        code, payload, err = self.run_json([
            "--fixture", str(path), "--baseline", str(baseline), "--update-baseline"])
        self.assertEqual(0, code, err)
        document = json.loads(baseline.read_text(encoding="utf-8"))
        self.assertEqual({"set_version", "measured", "thresholds"}, set(document))
        self.assertEqual("test_v1", document["set_version"])
        self.assertEqual(2, document["measured"]["queries"])
        thresholds = document["thresholds"]
        measured = document["measured"]
        self.assertEqual(2, thresholds["min_queries"])
        self.assertAlmostEqual(
            max(0.0, round(measured["recall_at_k"] - 0.05, 4)),
            thresholds["min_recall_at_k"])
        self.assertGreaterEqual(thresholds["max_duplicate_rate"],
                                measured["duplicate_rate"])
        self.assertGreaterEqual(thresholds["max_p95_ms"], measured["p95_ms"])
        # The freshly written baseline must pass when used as a gate.
        code, payload, err = self.run_json(["--fixture", str(path), "--baseline", str(baseline)])
        self.assertEqual(0, code, err)
        self.assertTrue(payload["gate"]["passed"])

    def test_fixture_validation_rejects_an_unbacked_expect(self):
        fixture = _fixture(
            [_fact("Orion 网关限流阈值是每分钟 1200 次请求", "ev:fact")],
            [{"id": "bad", "category": "fact_recall",
              "query": "Orion 网关限流阈值", "expect": ["不存在的值"]}])
        path = self.write_fixture(fixture)
        code, out, err = self.run_main(["--fixture", str(path), "--json"])
        self.assertEqual(2, code)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("不存在的值", payload["error"])


class LongMemEvalAdapterTests(BenchmarkCase):
    def json_sessions_file(self) -> Path:
        records = [
            {"question_id": "q1", "question_type": "single-session-user",
             "question": "What degree in Business Administration did I graduate with?",
             "answer": "Business Administration",
             "haystack_sessions": [[
                 {"role": "user", "content": "I graduated with a Business Administration degree."}]],
             "haystack_session_ids": ["s1"]},
            {"question_id": "q2", "question_type": "multi-session",
             "question": "Which city, Lisbon, did I move to?",
             "answer": "Lisbon",
             "haystack_sessions": [[
                 {"role": "user", "content": "I moved to Lisbon last year."}]]},
        ]
        path = self.dir / "longmemeval.json"
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        return path

    def jsonl_evidence_file(self) -> Path:
        lines = [
            {"question_id": "j1", "question": "Which database, PostgreSQL?",
             "question_type": "knowledge-update", "answer": "PostgreSQL",
             "evidence": ["The project uses PostgreSQL for storage."]},
            {"question_id": "j2", "question": "What is the cache host cache-07?",
             "answer": "cache-07", "evidence": ["The cache host is cache-07."]},
        ]
        path = self.dir / "longmemeval.jsonl"
        path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines)
                        + "\n", encoding="utf-8")
        return path

    def test_json_haystack_sessions_adapter(self):
        path = self.json_sessions_file()
        code, payload, err = self.run_json(["--format", "longmemeval", "--data", str(path)])
        self.assertEqual(0, code, err)
        self.assertFalse(payload["gate"]["enabled"])
        self.assertEqual(2, payload["measured"]["queries"])
        self.assertEqual(["q1", "q2"], [row["id"] for row in payload["per_query"]])
        self.assertEqual(1.0, payload["measured"]["recall_at_k"])
        self.assertEqual(1, payload["measured"]["per_category"]["fact_recall"]["queries"])
        self.assertEqual(1, payload["measured"]["per_category"]["continuity"]["queries"])

    def test_jsonl_evidence_adapter(self):
        path = self.jsonl_evidence_file()
        code, payload, err = self.run_json(["--format", "longmemeval", "--data", str(path)])
        self.assertEqual(0, code, err)
        self.assertEqual(2, payload["measured"]["queries"])
        self.assertEqual(1.0, payload["measured"]["recall_at_k"])

    def test_adapter_rejects_unverifiable_answer_with_question_id(self):
        records = [{"question_id": "bad1", "question": "What is my pet's name?",
                    "answer": "Narnia",
                    "haystack_sessions": [[{"role": "user", "content": "I like tea."}]]}]
        path = self.dir / "bad.json"
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        code, out, err = self.run_main(["--format", "longmemeval", "--data", str(path)])
        self.assertEqual(2, code)
        self.assertIn("bad1", err)
        self.assertIn("Narnia", err)
        self.assertIn("LongMemEval", err)

    def test_adapter_rejects_ambiguous_record_without_memory_source(self):
        records = [{"question_id": "bad2", "question": "Anything?", "answer": "yes"}]
        path = self.dir / "bad2.json"
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        code, out, err = self.run_main(["--format", "longmemeval", "--data", str(path)])
        self.assertEqual(2, code)
        self.assertIn("bad2", err)

    def test_external_format_requires_data_and_rejects_update_baseline(self):
        code, _, err = self.run_main(["--format", "longmemeval"])
        self.assertEqual(2, code)
        self.assertIn("--data", err)
        path = self.jsonl_evidence_file()
        code, _, err = self.run_main([
            "--format", "longmemeval", "--data", str(path), "--update-baseline"])
        self.assertEqual(2, code)
        self.assertIn("update-baseline", err)

    def test_external_gate_requires_an_explicit_baseline(self):
        path = self.jsonl_evidence_file()
        code, _, err = self.run_main([
            "--format", "longmemeval", "--data", str(path), "--gate"])
        self.assertEqual(2, code)
        self.assertIn("--baseline", err)


class CliSubprocessTests(BenchmarkCase):
    def test_cli_json_is_parseable_without_production_ledger(self):
        fixture = _fixture(
            [_fact("Orion 网关限流阈值是每分钟 1200 次请求", "ev:fact")],
            [{"id": "fact-rate", "category": "fact_recall",
              "query": "Orion 网关限流阈值", "expect": ["1200 次"]}])
        path = self.write_fixture(fixture)
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": str(ROOT),
            "HIPPOMIND_HOME": str(self.dir / "home"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "eval_benchmark.py"),
             "--fixture", str(path), "--json", "--no-gate"],
            capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120)
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(1, payload["measured"]["queries"])
        self.assertEqual(1.0, payload["measured"]["recall_at_k"])
        self.assertIn("per_category", payload["measured"])


if __name__ == "__main__":
    unittest.main()
