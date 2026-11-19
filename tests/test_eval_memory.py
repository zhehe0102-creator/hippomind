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
        specs = eval_memory.validate_queries(eval_memory.KEYWORD_QUERIES)
        self.assertGreaterEqual(len(specs), 30)
        ids = [spec["id"] for spec in specs]
        self.assertEqual(len(ids), len(set(ids)))
        categories = {spec.get("category") for spec in specs}
        self.assertGreaterEqual(len(categories), 3)
        self.assertTrue(any(spec.get("project") for spec in specs))
        for spec in specs:
            self.assertTrue(spec["query"])
            self.assertTrue(spec["expect"])

    def test_labelled_queries_alias_points_at_the_keyword_set(self):
        self.assertIs(eval_memory.LABELLED_QUERIES,
                      eval_memory.KEYWORD_QUERIES)

    def test_query_sets_registry_covers_every_order_entry(self):
        self.assertEqual(set(eval_memory.QUERY_SETS), set(eval_memory.SET_ORDER))
        self.assertIs(eval_memory.QUERY_SETS["keyword"],
                      eval_memory.KEYWORD_QUERIES)
        self.assertIs(eval_memory.QUERY_SETS["semantic"],
                      eval_memory.SEMANTIC_QUERIES)


class SemanticQuerySetTests(unittest.TestCase):
    """The paraphrase set is the whole point of this change: guard its shape.

    These assertions are deliberately about *label quality*, not about the
    score.  A query that could never hit (blank expectation, expectation that
    leaks into the query, id collision) makes the metric lie, so it is a test
    failure rather than a low number.
    """

    REQUIRED_CATEGORIES = {"hippomind", "android_ops", "project", "ops",
                           "english"}

    def setUp(self):
        self.specs = eval_memory.validate_queries(eval_memory.SEMANTIC_QUERIES)

    def test_has_at_least_forty_queries(self):
        self.assertGreaterEqual(len(self.specs), 40)

    def test_covers_all_five_categories(self):
        categories = {spec.get("category") for spec in self.specs}
        self.assertTrue(self.REQUIRED_CATEGORIES.issubset(categories),
                        "missing: %s" % (self.REQUIRED_CATEGORIES - categories))

    def test_ids_are_unique_and_disjoint_from_the_keyword_set(self):
        ids = [spec["id"] for spec in self.specs]
        self.assertEqual(len(ids), len(set(ids)))
        keyword_ids = {spec["id"] for spec in
                       eval_memory.validate_queries(eval_memory.KEYWORD_QUERIES)}
        self.assertEqual(keyword_ids & set(ids), set())

    def test_every_query_is_a_paraphrase_not_a_keyword_echo(self):
        """No expected substring may appear in its own query.

        If it did, the query would be answerable by plain term matching and the
        set would stop measuring cross-vocabulary recall.
        """
        leaks = []
        for spec in self.specs:
            query = spec["query"].lower()
            for term in spec["expect"]:
                if term.lower() in query:
                    leaks.append((spec["id"], term))
        self.assertEqual(leaks, [], "expectation leaked into its own query")

    def test_every_query_records_auditable_provenance(self):
        for spec in self.specs:
            self.assertTrue(spec.get("source_ids"),
                            "%s has no source_ids" % spec["id"])
            for memory_id in spec["source_ids"]:
                self.assertTrue(memory_id.startswith("mem_"),
                                "%s has a malformed source id %r"
                                % (spec["id"], memory_id))

    def test_expect_hits_are_positive(self):
        """A recorded expect_hits of 0 means the label was never grounded."""
        for spec in self.specs:
            self.assertGreaterEqual(spec.get("expect_hits", 0), 1,
                                    "%s expectation never matched the ledger"
                                    % spec["id"])

    def test_semantic_thresholds_are_a_ratchet_under_the_measured_baseline(self):
        thresholds = eval_memory.SET_THRESHOLDS["semantic"]
        baseline = eval_memory.SET_BASELINE["semantic"]
        self.assertLess(thresholds["min_recall_at_k"],
                        baseline["recall_at_k"])
        self.assertLess(thresholds["min_mrr"], baseline["mrr"])
        self.assertGreater(thresholds["max_duplicate_rate"],
                           baseline["duplicate_rate"])
        self.assertGreaterEqual(thresholds["min_queries"], 40)

    def test_keyword_thresholds_stay_stricter_than_semantic(self):
        keyword = eval_memory.SET_THRESHOLDS["keyword"]
        semantic = eval_memory.SET_THRESHOLDS["semantic"]
        self.assertGreater(keyword["min_recall_at_k"],
                           semantic["min_recall_at_k"])
        self.assertGreater(keyword["min_mrr"], semantic["min_mrr"])


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
        self.refusal_path = self.dir / "refusal.json"
        self.refusal_path.write_text(json.dumps([
            {"id": "mars", "query": "火星基地的氧气配额是多少",
             "absent_terms": ["火星基地"]},
            {"id": "moon", "query": "月球电梯的年度维护预算",
             "absent_terms": ["月球电梯"]},
        ], ensure_ascii=False), encoding="utf-8")

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

    def test_main_dumps_the_semantic_set(self):
        out = self.dir / "semantic.json"
        code, _ = self._main(["--set", "semantic", "--dump-queries", str(out)])
        self.assertEqual(code, 0)
        dumped = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(dumped), len(eval_memory.SEMANTIC_QUERIES))
        self.assertIn("expect_hits", dumped[0])
        self.assertIn("source_ids", dumped[0])

    def test_main_dumps_both_sets_keyed_by_name(self):
        out = self.dir / "all.json"
        code, _ = self._main(["--set", "all", "--dump-queries", str(out)])
        self.assertEqual(code, 0)
        dumped = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(set(dumped), {"keyword", "semantic"})
        self.assertEqual(len(dumped["keyword"]),
                         len(eval_memory.KEYWORD_QUERIES))

    def test_main_defaults_to_the_keyword_set(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path),
            "--json",
            "--min-recall", "0.5", "--min-mrr", "0.4",
            "--max-duplicate-rate", "0.5", "--max-p95-ms", "10000",
            "--min-queries", "2",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["set"], "keyword")
        self.assertNotIn("sets", payload)

    def test_main_runs_the_semantic_set_and_reports_its_own_thresholds(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path),
            "--set", "semantic", "--json",
            "--min-recall", "0.0", "--min-mrr", "0.0",
            "--max-duplicate-rate", "1.0", "--max-p95-ms", "10000",
            "--min-queries", "2",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["set"], "semantic")
        self.assertTrue(payload["gate"]["passed"])

    def test_main_semantic_defaults_apply_when_flags_are_omitted(self):
        """Omitted flags must fall back to the set's own thresholds."""
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path),
            "--set", "semantic", "--json", "--no-gate",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        thresholds = payload["gate"]["thresholds"]
        self.assertEqual(thresholds["min_queries"],
                         eval_memory.SEMANTIC_THRESHOLDS["min_queries"])
        self.assertEqual(thresholds["min_recall_at_k"],
                         eval_memory.SEMANTIC_THRESHOLDS["min_recall_at_k"])

    def test_main_set_all_reports_both_sets_and_combines_the_gate(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--set", "all", "--json",
            "--min-recall", "0.0", "--min-mrr", "0.0",
            "--max-duplicate-rate", "1.0", "--max-p95-ms", "10000",
            "--min-queries", "1",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(set(payload["sets"]), {"keyword", "semantic"})
        self.assertEqual(payload["queries"],
                         len(eval_memory.KEYWORD_QUERIES)
                         + len(eval_memory.SEMANTIC_QUERIES))
        self.assertTrue(payload["gate"]["passed"])
        self.assertEqual(payload["gate"]["failures"], [])

    def test_main_set_all_fails_when_either_set_fails(self):
        """A combined gate must not let one passing set mask a failing one."""
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--set", "all", "--json",
            "--min-recall", "1.0", "--min-queries", "1",
            "--max-p95-ms", "10000",
        ])
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["gate"]["passed"])
        # Both sets contain a miss, so both must be named in the failures.
        self.assertTrue(any(f.startswith("keyword:") for f in payload["gate"]["failures"]))
        self.assertTrue(any(f.startswith("semantic:") for f in payload["gate"]["failures"]))

    def test_main_human_report_for_all_names_both_sets(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--set", "all",
            "--min-recall", "0.0", "--min-mrr", "0.0",
            "--max-duplicate-rate", "1.0", "--max-p95-ms", "10000",
            "--min-queries", "1",
        ])
        self.assertEqual(code, 0)
        self.assertIn("== set: keyword ==", output)
        self.assertIn("== set: semantic ==", output)
        self.assertIn("== combined ==", output)
        self.assertIn("gate: PASS", output)

    def test_main_rejects_queries_file_combined_with_set_all(self):
        code, _ = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path),
            "--set", "all",
        ])
        self.assertEqual(code, 2)

    def test_main_rejects_unknown_set_name(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                eval_memory.main(["--set", "nope"])

    # ---- refusal delegation (opt-in; the default output must not change) ----

    def test_main_set_all_without_include_refusal_keeps_the_old_report_shape(self):
        """--include-refusal 是 opt-in：不开时 --set all 的 JSON 必须一字不多。

        拒答集的指标与 recall 没有共同分母（没有 expect），一旦默认并进
        combined gate，既有报表的字段和退出码就变了。这条把「默认形状不变」
        钉死：没有 order 键、没有 refusal 键。
        """
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--set", "all", "--json",
            "--min-recall", "0.0", "--min-mrr", "0.0",
            "--max-duplicate-rate", "1.0", "--max-p95-ms", "10000",
            "--min-queries", "1",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(set(payload), {"sets", "queries", "k", "mode",
                                        "ledger", "gate"})
        self.assertEqual(set(payload["gate"]), {"passed", "failures", "sets"})
        self.assertEqual(set(payload["sets"]), {"keyword", "semantic"})

    def test_main_set_all_with_include_refusal_adds_the_third_set(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--set", "all", "--include-refusal", "--json",
            "--min-recall", "0.0", "--min-mrr", "0.0",
            "--max-duplicate-rate", "1.0", "--max-p95-ms", "10000",
            "--min-queries", "1",
        ])
        payload = json.loads(output)
        self.assertIn("refusal", payload["sets"])
        self.assertEqual(payload["gate"]["order"],
                         ["keyword", "semantic", "refusal"])
        refusal = payload["sets"]["refusal"]
        self.assertEqual(refusal["set"], "refusal")
        # 内置拒答集远大于 --min-queries 1，且账本里只有 PostgreSQL 一条记忆，
        # 因此这一路必须零泄漏、标注审计干净。
        self.assertEqual(refusal["metrics"]["leak"], 0)
        self.assertTrue(refusal["grounding"]["clean"])
        self.assertTrue(refusal["gate"]["passed"])
        # 合并门禁里三套都要在（退出码仍由 combined 决定）。
        self.assertEqual(code, 0)

    def test_main_include_refusal_failure_fails_the_combined_gate(self):
        """拒答集失败必须让合并门禁失败，且失败项带 refusal: 前缀。"""
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--set", "all", "--include-refusal", "--json",
            "--min-recall", "0.0", "--min-mrr", "0.0",
            "--max-duplicate-rate", "1.0", "--max-p95-ms", "10000",
            "--min-queries", "1",
            "--min-queries", "1",
        ])
        payload = json.loads(output)
        self.assertTrue(payload["gate"]["passed"])
        # 用一条会泄漏的查询单独验证失败路径（不依赖生产账本）。
        leak_path = self.dir / "leak.json"
        leak_path.write_text(json.dumps([
            {"id": "leak", "query": "PostgreSQL 主数据库",
             "absent_terms": ["PostgreSQL"]},
        ], ensure_ascii=False), encoding="utf-8")
        code, output = self._main([
            "--ledger", str(self.ledger_path), "--queries", str(leak_path),
            "--set", "refusal", "--json", "--min-queries", "1",
        ])
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["gate"]["passed"])

    def test_main_set_refusal_delegates_and_returns_its_exit_code(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--set", "refusal", "--json",
            "--queries", str(self.dir / "refusal.json"),
            "--min-queries", "2",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["set"], "refusal")
        self.assertIn("refusal_rate", payload["metrics"])
        self.assertNotIn("recall_at_k", payload["metrics"])

    def test_main_set_refusal_gate_failure_exits_one(self):
        path = self.dir / "bad-refusal.json"
        path.write_text(json.dumps([
            {"id": "bad", "query": "数据库选型", "absent_terms": ["PostgreSQL"]},
            {"id": "ok", "query": "火星基地的氧气配额",
             "absent_terms": ["火星基地"]},
        ], ensure_ascii=False), encoding="utf-8")
        code, output = self._main([
            "--ledger", str(self.ledger_path), "--set", "refusal",
            "--queries", str(path), "--json", "--min-queries", "2",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)["gate"]["passed"])
        # --no-gate 让同一份数据只报告不判退出码。
        code, _ = self._main([
            "--ledger", str(self.ledger_path), "--set", "refusal",
            "--queries", str(path), "--min-queries", "2", "--no-gate",
        ])
        self.assertEqual(code, 0)

    def test_main_rejects_include_refusal_without_set_all(self):
        code, _ = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path), "--include-refusal",
        ])
        self.assertEqual(code, 2)

    def test_main_human_report_for_all_with_refusal_lists_three_sets(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--set", "all", "--include-refusal",
            "--min-recall", "0.0", "--min-mrr", "0.0",
            "--max-duplicate-rate", "1.0", "--max-p95-ms", "10000",
            "--min-queries", "1",
        ])
        self.assertEqual(code, 0)
        self.assertIn("== set: keyword ==", output)
        self.assertIn("== set: semantic ==", output)
        self.assertIn("== set: refusal ==", output)
        self.assertIn("== combined ==", output)
        self.assertIn("keyword, semantic, refusal", output)


if __name__ == "__main__":
    unittest.main()
