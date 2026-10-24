"""Tests for the labelled retrieval evaluation gate (scripts/eval_memory.py).

These tests exercise the metric functions end-to-end against temporary ledgers,
so they never read or write the production ledger.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_memory  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402


def _record(content, *, agent="tester", evidence="ev-1", **extra):
    record = {
        "content": content,
        "kind": "semantic",
        "source_agent": agent,
        "evidence": [evidence],
    }
    record.update(extra)
    return record


class MetricMathTests(unittest.TestCase):
    def test_percentile_uses_linear_interpolation(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(eval_memory.percentile(values, 0.0), 1.0)
        self.assertAlmostEqual(eval_memory.percentile(values, 0.5), 2.5)
        self.assertAlmostEqual(eval_memory.percentile(values, 1.0), 4.0)
        self.assertAlmostEqual(eval_memory.percentile(values, 0.95), 3.85)
        self.assertEqual(eval_memory.percentile([], 0.5), 0.0)
        self.assertEqual(eval_memory.percentile([7.0], 0.95), 7.0)

    def test_is_relevant_requires_every_expected_substring(self):
        item = {"content": "部署使用 SQLite 主数据库"}
        self.assertTrue(eval_memory.is_relevant(item, ["sqlite"]))
        self.assertTrue(eval_memory.is_relevant(item, ["SQLite", "主数据库"]))
        self.assertFalse(eval_memory.is_relevant(item, ["SQLite", "PostgreSQL"]))
        self.assertFalse(eval_memory.is_relevant(item, []))

    def test_render_hit_includes_context_fields(self):
        item = {
            "content": "摘要",
            "metadata": {"context": {
                "key_decisions": ["默认主题：纸本"],
                "next_steps": [],
            }},
        }
        self.assertIn("纸本", eval_memory.render_hit(item))
        self.assertTrue(eval_memory.is_relevant(item, ["纸本"]))
        self.assertEqual(eval_memory.render_hit({"metadata": "not-a-dict"}), "")
        self.assertEqual(eval_memory.render_hit({}), "")

    def test_score_hits_reports_rank_mrr_and_term_recall(self):
        hits = [
            {"content": "alpha"},
            {"content": "beta"},
            {"content": "gamma beta"},
        ]
        row = eval_memory.score_hits(hits, ["beta"], k=8)
        self.assertEqual(row["rank"], 2)
        self.assertAlmostEqual(row["reciprocal_rank"], 0.5)
        self.assertTrue(row["relevant"])
        self.assertAlmostEqual(row["term_recall"], 1.0)
        row = eval_memory.score_hits(hits, ["gamma"], k=8)
        self.assertEqual(row["rank"], 3)
        row = eval_memory.score_hits(hits, ["missing"], k=8)
        self.assertEqual(row["rank"], 0)
        self.assertEqual(row["reciprocal_rank"], 0.0)
        self.assertFalse(row["relevant"])
        self.assertAlmostEqual(row["term_recall"], 0.0)

    def test_score_hits_respects_k_window(self):
        hits = [{"content": "alpha"}, {"content": "beta"}]
        row = eval_memory.score_hits(hits, ["beta"], k=1)
        self.assertEqual(row["hits"], 1)
        self.assertEqual(row["rank"], 0)
        self.assertFalse(row["relevant"])

    def test_score_hits_duplicate_rate_normalises_whitespace(self):
        hits = [
            {"content": "same"},
            {"content": " same "},
            {"content": "other"},
            {"content": "other"},
        ]
        row = eval_memory.score_hits(hits, ["same"], k=8)
        self.assertAlmostEqual(row["duplicate_rate"], 0.5)
        self.assertEqual(eval_memory.score_hits([], ["x"])["duplicate_rate"], 0.0)


class QueryValidationTests(unittest.TestCase):
    def test_validate_queries_normalises_specs(self):
        specs = eval_memory.validate_queries([
            {"id": " a ", "query": " q ", "expect": [" E "], "project": "p1"},
        ])
        self.assertEqual(specs[0]["id"], "a")
        self.assertEqual(specs[0]["query"], "q")
        self.assertEqual(specs[0]["expect"], ["E"])
        self.assertEqual(specs[0]["project"], "p1")

    def test_validate_queries_rejects_malformed_sets(self):
        bad_sets = [
            {},
            [],
            [{"query": "x"}],
            [{"query": "", "expect": ["e"]}],
            [{"query": "x", "expect": []}],
            [{"query": "x", "expect": [""]}],
            [{"query": "x", "expect": [1]}],
            [{"query": "x", "expect": ["e"], "id": "dup"},
             {"query": "y", "expect": ["f"], "id": "dup"}],
        ]
        for bad in bad_sets:
            with self.assertRaises(ValueError):
                eval_memory.validate_queries(bad)

    def test_load_queries_reads_json_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queries.json"
            path.write_text(
                json.dumps([{"query": "q", "expect": ["e"]}]), encoding="utf-8")
            specs = eval_memory.load_queries(path)
            self.assertEqual(specs[0]["id"], "q")
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                eval_memory.load_queries(path)
            with self.assertRaises(ValueError):
                eval_memory.load_queries(Path(tmp) / "missing.json")

    def test_builtin_labelled_set_is_well_formed(self):
        specs = eval_memory.validate_queries(eval_memory.LABELLED_QUERIES)
        self.assertGreaterEqual(len(specs), 30)
        ids = [spec["id"] for spec in specs]
        self.assertEqual(len(ids), len(set(ids)))
        categories = {spec.get("category") for spec in specs}
        self.assertGreaterEqual(len(categories), 3)
        self.assertTrue(any(spec.get("project") for spec in specs))
        for spec in specs:
            self.assertTrue(spec["query"])
            self.assertTrue(spec["expect"])


class LedgerEvaluationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self._tmp.name) / "eval.sqlite")
        self.addCleanup(self.ledger.close)
        records = [
            _record("部署笔记：生产环境使用 PostgreSQL 作为主数据库", evidence="ev-1"),
            _record("前端构建：Vite 打包与 esbuild 压缩配置", evidence="ev-2"),
            _record("部署笔记：生产环境使用 PostgreSQL 作为主数据库",
                    agent="other", evidence="ev-3"),
            _record("缓存层采用 Redis 集群与哨兵模式", evidence="ev-4",
                    metadata={"context": {"key_decisions": ["采用哨兵模式做主从故障转移"]}}),
        ]
        for record in records:
            self.ledger.ingest(record)
        self.queries = [
            {"id": "pg", "query": "PostgreSQL 主数据库", "expect": ["PostgreSQL"]},
            {"id": "vite", "query": "esbuild 压缩", "expect": ["esbuild"]},
            {"id": "missing", "query": "量子编译器 术语", "expect": ["量子编译器"]},
        ]

    def test_evaluate_reports_recall_mrr_duplicates_and_latency(self):
        result = eval_memory.evaluate(self.ledger, self.queries, limit=8)
        self.assertEqual(result["queries"], 3)
        self.assertEqual(result["k"], 8)
        self.assertAlmostEqual(result["metrics"]["recall_at_k"], round(2 / 3, 4))
        self.assertAlmostEqual(result["metrics"]["mrr"], round(2 / 3, 4))
        # The PostgreSQL query returns the same content twice: 1 duplicate / 2.
        self.assertAlmostEqual(
            result["metrics"]["duplicate_rate"], round((1 / 3) * 0.5, 4))
        self.assertGreater(result["metrics"]["latency_p95_ms"], 0.0)
        by_id = {row["id"]: row for row in result["details"]}
        self.assertEqual(by_id["pg"]["rank"], 1)
        self.assertEqual(by_id["missing"]["rank"], 0)
        self.assertEqual(by_id["missing"]["hits"], 0)

    def test_evaluate_accepts_injected_search(self):
        def search(query, project, scope, limit):
            return [{"content": "injected " + query}]

        result = eval_memory.evaluate(
            self.ledger, [{"query": "q", "expect": ["injected"]}], search=search)
        self.assertEqual(result["metrics"]["recall_at_k"], 1.0)
        self.assertEqual(result["details"][0]["rank"], 1)

    def test_evaluate_forwards_project_scope_and_limit_filters(self):
        calls = []

        def search(query, project, scope, limit):
            calls.append((query, project, scope, limit))
            return []

        eval_memory.evaluate(
            self.ledger,
            [{"query": "q", "expect": ["x"], "project": "p1",
              "scope": "agent:codex"}],
            limit=3, search=search)
        self.assertEqual(calls, [("q", "p1", "agent:codex", 3)])

    def test_check_gate_pass_and_fail(self):
        result = eval_memory.evaluate(self.ledger, self.queries, limit=8)
        default_gate = eval_memory.check_gate(result)
        self.assertFalse(default_gate["passed"])
        self.assertTrue(default_gate["failures"])
        passing = eval_memory.check_gate(
            result, min_recall_at_k=0.5, min_mrr=0.5, max_duplicate_rate=0.5,
            max_p95_ms=10_000.0, min_queries=1)
        self.assertTrue(passing["passed"])
        self.assertEqual(passing["failures"], [])
        strict = eval_memory.check_gate(
            result, min_recall_at_k=1.0, min_mrr=1.0, max_duplicate_rate=0.0,
            max_p95_ms=0.0, min_queries=99)
        self.assertFalse(strict["passed"])
        self.assertGreaterEqual(len(strict["failures"]), 4)


class MainEntrypointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ledger_path = self.dir / "eval.sqlite"
        ledger = SQLiteLedger(self.ledger_path)
        try:
            ledger.ingest(_record("部署笔记：生产环境使用 PostgreSQL 作为主数据库"))
        finally:
            ledger.close()
        self.query_path = self.dir / "queries.json"
        self.query_path.write_text(json.dumps([
            {"id": "pg", "query": "PostgreSQL 主数据库", "expect": ["PostgreSQL"]},
            {"id": "missing", "query": "量子编译器", "expect": ["量子编译器"]},
        ]), encoding="utf-8")

    def _main(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = eval_memory.main(argv)
        return code, buffer.getvalue()

    def test_main_passes_and_fails_by_gate(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path),
            "--json", "--min-recall", "0.5", "--min-mrr", "0.4",
            "--max-duplicate-rate", "0.5", "--max-p95-ms", "10000",
            "--min-queries", "2",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["gate"]["passed"])
        self.assertEqual(payload["queries"], 2)
        failing, _ = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path),
            "--min-recall", "1.0", "--min-queries", "2",
        ])
        self.assertEqual(failing, 1)
        bypass, _ = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path),
            "--min-recall", "1.0", "--min-queries", "2", "--no-gate",
        ])
        self.assertEqual(bypass, 0)

    def test_main_human_report_contains_gate_summary(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path),
            "--min-recall", "0.5", "--min-mrr", "0.4",
            "--max-duplicate-rate", "0.5", "--min-queries", "2",
        ])
        self.assertEqual(code, 0)
        self.assertIn("recall@", output)
        self.assertIn("gate: PASS", output)

    def test_main_errors_on_missing_ledger_without_creating_it(self):
        missing = self.dir / "nope.sqlite"
        code, _ = self._main([
            "--ledger", str(missing), "--queries", str(self.query_path)])
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())

    def test_main_can_include_ledger_stats(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path),
            "--json", "--ledger-stats",
            "--min-recall", "0.5", "--min-mrr", "0.4",
            "--max-duplicate-rate", "0.5", "--max-p95-ms", "10000",
            "--min-queries", "2",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        stats = payload["ledger_stats"]
        self.assertGreaterEqual(stats["memories"], 1)
        self.assertIn("claims", stats)
        self.assertIn("schema_version", stats)

    def test_main_dumps_the_active_query_set(self):
        out = self.dir / "dumped.json"
        code, _ = self._main(["--dump-queries", str(out)])
        self.assertEqual(code, 0)
        dumped = json.loads(out.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(dumped), 30)
        self.assertEqual(len({item["id"] for item in dumped}), len(dumped))


if __name__ == "__main__":
    unittest.main()
