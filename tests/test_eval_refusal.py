"""Tests for the refusal evaluation gate (scripts/eval_refusal.py).

拒答是 LongMemEval 五项能力里 HippoMind 从没测过的一项（docs/HEALTH_REVIEW.md
第六节）。这些测试钉的是**判定逻辑本身**，不是"跑起来不抛异常"：

* classify_hits 必须把「返回空」「返回低分噪音」「返回可注入噪音」分成三档，
  且分档依据是生产注入门 _passes_relevance_gate，而不是原始分数——
  无关查询的分数地板约 0.37，用分数阈值判会把噪音误判成拒答；
* 门禁必须真的在 leak 时失败、在标注不干净时失败；
* ReadOnlyLedger 必须真的写不进去（连 PRAGMA 级写入都拒绝）；
* 内置拒答集必须结构合法（20+ 条、id 唯一、term 至少 2 字、类别覆盖）。
"""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_refusal  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402


def _hit(content, *, score=0.30, fts=0.0, coverage=0.0, memory_id="mem_x"):
    """One retrieval row shaped like ledger.search output."""
    return {
        "memory_id": memory_id,
        "content": content,
        "score": score,
        "score_parts": {"fts": fts, "coverage": coverage, "topic": 0.0},
    }


def _record(content, *, evidence="ev-1", **extra):
    record = {
        "content": content,
        "kind": "semantic",
        "source_agent": "tester",
        "evidence": [evidence],
    }
    record.update(extra)
    return record


class ClassifyHitsTests(unittest.TestCase):
    """三档判定：empty / noise / leak。"""

    def test_no_hits_is_empty(self):
        verdict = eval_refusal.classify_hits([])
        self.assertEqual(verdict["verdict"], "empty")
        self.assertEqual(verdict["hits"], 0)
        self.assertEqual(verdict["injectable"], 0)
        self.assertEqual(verdict["top_score"], 0.0)

    def test_high_score_without_lexical_evidence_is_noise_not_leak(self):
        """0.394 分但没有词面证据 = 噪音，不是泄漏。

        这是实测里最重要的一条：「今天天气怎么样」返回 5 条记忆、最高分 0.394，
        高于 MIN_RELIABLE_SCORE(0.22)，但 fts=0 / coverage=0.167。若按分数判，
        这条会被误判成 leak，拒答门禁永远失败。
        """
        hits = [_hit("我使用临时 root 之后……", score=0.394, fts=0.0,
                     coverage=0.167)]
        verdict = eval_refusal.classify_hits(hits)
        self.assertEqual(verdict["verdict"], "noise")
        self.assertEqual(verdict["hits"], 1)
        self.assertEqual(verdict["injectable"], 0)
        self.assertAlmostEqual(verdict["top_score"], 0.394, places=4)
        self.assertAlmostEqual(verdict["max_coverage"], 0.167, places=4)

    def test_real_fts_hit_is_a_leak(self):
        """有真实词面命中（fts>0）= 这条无关提问会被注入，必须算泄漏。"""
        hits = [_hit("RSA 密钥的最小安全长度是 2048", score=0.481, fts=0.204,
                     coverage=0.222)]
        verdict = eval_refusal.classify_hits(hits)
        self.assertEqual(verdict["verdict"], "leak")
        self.assertEqual(verdict["injectable"], 1)

    def test_high_coverage_alone_is_a_leak(self):
        """coverage >= 0.6 单独也过注入门（与 _passes_relevance_gate 一致）。"""
        hits = [_hit("某条记忆", score=0.30, fts=0.0, coverage=0.75)]
        self.assertEqual(eval_refusal.classify_hits(hits)["verdict"], "leak")

    def test_low_score_below_min_score_is_not_injectable(self):
        """分数低于 min_score 时即便有 fts 也不注入。"""
        hits = [_hit("某条记忆", score=0.10, fts=0.5, coverage=0.9)]
        verdict = eval_refusal.classify_hits(hits, min_score=0.20)
        self.assertEqual(verdict["verdict"], "noise")
        self.assertEqual(verdict["injectable"], 0)

    def test_one_leak_among_noise_still_leaks(self):
        """只要有一条能过门，整条查询就算泄漏——噪音不能稀释掉泄漏。"""
        hits = [
            _hit("噪音一", score=0.39, fts=0.0, coverage=0.1),
            _hit("噪音二", score=0.38, fts=0.0, coverage=0.2),
            _hit("命中", score=0.45, fts=0.3, coverage=0.3),
        ]
        verdict = eval_refusal.classify_hits(hits)
        self.assertEqual(verdict["verdict"], "leak")
        self.assertEqual(verdict["injectable"], 1)
        self.assertEqual(verdict["hits"], 3)

    def test_missing_score_parts_do_not_crash(self):
        """没有 score_parts 的行按零词面证据处理，不能抛异常。"""
        verdict = eval_refusal.classify_hits([{"content": "x", "score": 0.5}])
        self.assertEqual(verdict["verdict"], "noise")
        self.assertEqual(verdict["max_fts"], 0.0)


class ValidateRefusalQueriesTests(unittest.TestCase):
    def test_accepts_a_well_formed_spec(self):
        specs = eval_refusal.validate_refusal_queries([
            {"id": " a ", "query": " q ", "absent_terms": [" T1 ", "T2"],
             "category": " c ", "scope": "agent:x", "note": " n "},
        ])
        self.assertEqual(specs[0]["id"], "a")
        self.assertEqual(specs[0]["query"], "q")
        self.assertEqual(specs[0]["absent_terms"], ["T1", "T2"])
        self.assertEqual(specs[0]["category"], "c")
        self.assertEqual(specs[0]["scope"], "agent:x")
        self.assertEqual(specs[0]["note"], "n")

    def test_rejects_malformed_sets(self):
        bad_sets = [
            {},
            [],
            [{"query": "x", "absent_terms": ["t"]}][:0],          # empty list
            [{"absent_terms": ["t"]}],                             # no query
            [{"query": "", "absent_terms": ["t"]}],
            [{"query": "x"}],                                      # no terms
            [{"query": "x", "absent_terms": []}],
            [{"query": "x", "absent_terms": [""]}],
            [{"query": "x", "absent_terms": [1]}],
            [{"query": "x", "absent_terms": ["单"]}],              # 1-char term
            [{"query": "x", "absent_terms": ["t"], "id": "dup"},
             {"query": "y", "absent_terms": ["u"], "id": "dup"}],
        ]
        for bad in bad_sets:
            with self.assertRaises(ValueError, msg=repr(bad)):
                eval_refusal.validate_refusal_queries(bad)

    def test_one_character_terms_are_rejected_with_a_reason(self):
        """单字 term 在任何中文账本里都存在，用它做「不存在」证明永远失败。"""
        with self.assertRaises(ValueError) as caught:
            eval_refusal.validate_refusal_queries(
                [{"query": "x", "absent_terms": ["的"]}])
        self.assertIn("1-character", str(caught.exception))

    def test_load_refusal_queries_reads_json_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "refusal.json"
            path.write_text(json.dumps([
                {"id": "r1", "query": "q", "absent_terms": ["term"]},
            ], ensure_ascii=False), encoding="utf-8")
            specs = eval_refusal.load_refusal_queries(path)
            self.assertEqual(specs[0]["id"], "r1")
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                eval_refusal.load_refusal_queries(path)
            with self.assertRaises(ValueError):
                eval_refusal.load_refusal_queries(Path(tmp) / "missing.json")


class BuiltinRefusalSetTests(unittest.TestCase):
    """内置集的结构护栏。这些断言失败意味着标注本身有问题。"""

    def setUp(self):
        self.specs = eval_refusal.validate_refusal_queries(
            eval_refusal.REFUSAL_QUERIES)

    def test_has_at_least_twenty_queries(self):
        """HEALTH_REVIEW 第六节要求「扩充到 20+ 条并进门禁」。"""
        self.assertGreaterEqual(len(self.specs), 20)
        self.assertEqual(eval_refusal.REFUSAL_THRESHOLDS["min_queries"], 20)

    def test_ids_are_unique(self):
        ids = [spec["id"] for spec in self.specs]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_term_is_a_substantive_topic_word(self):
        """term 至少 2 个字符，且不能是「我的」这类满库都有的填充词。"""
        filler = {"我的", "多少", "什么", "怎么", "请问", "一下", "这个", "那个"}
        for spec in self.specs:
            for term in spec["absent_terms"]:
                self.assertGreaterEqual(len(term), 2,
                                        "%s has a too-short term %r"
                                        % (spec["id"], term))
                self.assertNotIn(term, filler,
                                 "%s uses a filler word %r as a topic term"
                                 % (spec["id"], term))

    def test_covers_at_least_eight_categories(self):
        """类别分散是刻意的：只测一类会把「这一类没语料」当成拒答能力。"""
        categories = {spec.get("category") for spec in self.specs}
        self.assertGreaterEqual(len(categories), 8)
        self.assertNotIn(None, categories)

    def test_every_query_has_a_note_for_auditability(self):
        for spec in self.specs:
            self.assertTrue(spec.get("note"), "%s has no note" % spec["id"])

    def test_queries_are_chinese(self):
        """拒答集是中文的（LongMemEval 的英文集与本地语料不匹配）。"""
        for spec in self.specs:
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in spec["query"]),
                            "%s is not a Chinese query" % spec["id"])

    def test_thresholds_expressed_only_as_zero_leak(self):
        """阈值只表达「零泄漏」+ 集合不缩水，没有 recall 类阈值。"""
        thresholds = eval_refusal.REFUSAL_THRESHOLDS
        self.assertEqual(thresholds["max_leak_rate"], 0.0)
        self.assertTrue(thresholds["require_grounded"])
        self.assertNotIn("min_recall_at_k", thresholds)
        self.assertNotIn("min_mrr", thresholds)
        self.assertNotIn("max_noise_rate", thresholds)

    def test_baseline_matches_the_builtin_set_size(self):
        baseline = eval_refusal.REFUSAL_BASELINE
        self.assertEqual(baseline["queries"], len(self.specs))
        self.assertEqual(baseline["leak"], 0)
        self.assertAlmostEqual(baseline["refusal_rate"], 1.0)
        self.assertEqual(baseline["empty"] + baseline["noise"], len(self.specs))

    def test_baseline_records_the_drifting_split_as_a_range(self):
        """empty/noise 的切分随账本漂移，基线必须记区间而不是点。

        实测（2026-10-25，18 次采样）：empty 22-29 / noise 9-16，leak 恒 0。
        把切分钉成一个点，会让「账本多长了一条无关记忆」看起来像回归。
        """
        baseline = eval_refusal.REFUSAL_BASELINE
        empty_low, empty_high = baseline["empty_range"]
        noise_low, noise_high = baseline["noise_range"]
        self.assertLessEqual(empty_low, empty_high)
        self.assertLessEqual(noise_low, noise_high)
        # 区间端点必须覆盖「总数」：任一极端都仍是一条不泄漏的跑法。
        self.assertEqual(empty_low + noise_high, len(self.specs))
        self.assertEqual(empty_high + noise_low, len(self.specs))
        # 记录的代表值必须落在区间内，否则文档与代码会各说各话。
        self.assertTrue(empty_low <= baseline["empty"] <= empty_high)
        self.assertTrue(noise_low <= baseline["noise"] <= noise_high)
        self.assertGreaterEqual(baseline["samples"], 3)


class GroundingAuditTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self._tmp.name) / "refusal.sqlite")
        self.addCleanup(self.ledger.close)
        self.ledger.ingest(_record("部署笔记：生产环境使用 PostgreSQL 作为主数据库"))

    def test_clean_terms_pass(self):
        specs = eval_refusal.validate_refusal_queries([
            {"id": "r1", "query": "火星基地的氧气配额", "absent_terms": ["火星基地"]},
        ])
        grounding = eval_refusal.audit_grounding(self.ledger, specs)
        self.assertTrue(grounding["clean"])
        self.assertEqual(grounding["violations"], [])
        self.assertEqual(grounding["checked_terms"], 1)

    def test_present_term_is_reported_as_a_violation(self):
        """标注写错（term 其实存在）必须被报出来，而不是变成一次「泄漏」。"""
        specs = eval_refusal.validate_refusal_queries([
            {"id": "bad", "query": "数据库选型", "absent_terms": ["PostgreSQL"]},
        ])
        grounding = eval_refusal.audit_grounding(self.ledger, specs)
        self.assertFalse(grounding["clean"])
        self.assertEqual(len(grounding["violations"]), 1)
        violation = grounding["violations"][0]
        self.assertEqual(violation["id"], "bad")
        self.assertEqual(violation["term"], "PostgreSQL")
        self.assertGreaterEqual(violation["recallable"], 1)

    def test_quarantined_rows_do_not_count_as_recallable(self):
        """可召回集谓词必须与 ledger.search 一致：quarantine 不算存在。"""
        ledger = SQLiteLedger(Path(self._tmp.name) / "quarantine.sqlite")
        self.addCleanup(ledger.close)
        result = ledger.ingest(_record("量子编译器 术语表"))
        # quality_state 由 _quality_score 推导，不能从 record 强行指定；
        # 这里直接改投影，模拟 data_hygiene 把重复记忆降级为 quarantine。
        conn = sqlite3.connect(str(ledger.path))
        try:
            conn.execute("UPDATE memories SET quality_state='quarantine' "
                         "WHERE memory_id=?", (result.memory_id,))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?",
                         (result.memory_id,))
            conn.commit()
        finally:
            conn.close()
        specs = eval_refusal.validate_refusal_queries([
            {"id": "q", "query": "量子编译器", "absent_terms": ["量子编译器"]},
        ])
        grounding = eval_refusal.audit_grounding(ledger, specs)
        self.assertTrue(grounding["clean"],
                        "quarantine 行不该算「账本里有」")


class EvaluateRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self._tmp.name) / "eval.sqlite")
        self.addCleanup(self.ledger.close)
        self.ledger.ingest(_record("部署笔记：生产环境使用 PostgreSQL 作为主数据库"))

    def _specs(self, count=3):
        return eval_refusal.validate_refusal_queries([
            {"id": "r%d" % index, "category": "fiction",
             "query": "火星基地的氧气配额 %d" % index,
             "absent_terms": ["火星基地"]}
            for index in range(count)
        ])

    def test_all_empty_scores_a_perfect_refusal_rate(self):
        result = eval_refusal.evaluate_refusal(
            self.ledger, self._specs(),
            search=lambda q, scope, limit: [],
            pack=lambda q, scope: {"items": [], "text": ""})
        metrics = result["metrics"]
        self.assertEqual(result["queries"], 3)
        self.assertEqual(metrics["empty"], 3)
        self.assertEqual(metrics["noise"], 0)
        self.assertEqual(metrics["leak"], 0)
        self.assertAlmostEqual(metrics["refusal_rate"], 1.0)
        self.assertAlmostEqual(metrics["leak_rate"], 0.0)

    def test_noise_counts_as_refusal_but_is_reported_separately(self):
        """noise 是「可接受的拒答」：不进 prompt，但必须被单独看见。"""
        result = eval_refusal.evaluate_refusal(
            self.ledger, self._specs(2),
            search=lambda q, scope, limit: [
                _hit("无关记忆", score=0.39, fts=0.0, coverage=0.16)],
            pack=lambda q, scope: {"items": [], "text": ""})
        metrics = result["metrics"]
        self.assertEqual(metrics["noise"], 2)
        self.assertEqual(metrics["leak"], 0)
        self.assertAlmostEqual(metrics["refusal_rate"], 1.0)
        self.assertAlmostEqual(metrics["noise_rate"], 1.0)
        for row in result["details"]:
            self.assertEqual(row["verdict"], "noise")
            self.assertTrue(row["pack_agrees"])

    def test_a_leak_lowers_the_refusal_rate(self):
        result = eval_refusal.evaluate_refusal(
            self.ledger, self._specs(4),
            search=lambda q, scope, limit: (
                [_hit("真命中", score=0.5, fts=0.3, coverage=0.3)]
                if "0" in q else []),
            pack=lambda q, scope: {"items": [{"memory_id": "m"}], "text": "x"})
        metrics = result["metrics"]
        self.assertEqual(metrics["leak"], 1)
        self.assertAlmostEqual(metrics["leak_rate"], 0.25)
        self.assertAlmostEqual(metrics["refusal_rate"], 0.75)
        leaked = [row for row in result["details"] if row["verdict"] == "leak"]
        self.assertEqual(len(leaked), 1)
        self.assertEqual(leaked[0]["id"], "r0")

    def test_pack_disagreement_is_flagged(self):
        """有可注入 hit 但 pack 为空 = 靠分组/预算挡下的，不是靠相关性门。"""
        result = eval_refusal.evaluate_refusal(
            self.ledger, self._specs(1),
            search=lambda q, scope, limit: [
                _hit("真命中", score=0.5, fts=0.3, coverage=0.3)],
            pack=lambda q, scope: {"items": [], "text": ""})
        self.assertEqual(result["metrics"]["pack_disagreements"], 1)
        self.assertFalse(result["details"][0]["pack_agrees"])

    def test_forwards_scope_and_limit(self):
        calls = []

        def search(query, scope, limit):
            calls.append((query, scope, limit))
            return []

        specs = eval_refusal.validate_refusal_queries([
            {"id": "s", "query": "q1", "absent_terms": ["term"],
             "scope": "agent:codex"},
        ])
        eval_refusal.evaluate_refusal(
            self.ledger, specs, limit=5, search=search,
            pack=lambda q, scope: {"items": [], "text": ""})
        self.assertEqual(calls, [("q1", "agent:codex", 5)])

    def test_by_category_groups_leaks(self):
        specs = eval_refusal.validate_refusal_queries([
            {"id": "a", "category": "cat_a", "query": "qa",
             "absent_terms": ["ta"]},
            {"id": "b", "category": "cat_b", "query": "qb",
             "absent_terms": ["tb"]},
        ])
        result = eval_refusal.evaluate_refusal(
            self.ledger, specs,
            search=lambda q, scope, limit: (
                [_hit("x", score=0.5, fts=0.3)] if q == "qa" else []),
            pack=lambda q, scope: {"items": [], "text": ""})
        self.assertEqual(result["by_category"]["cat_a"]["leak"], 1)
        self.assertEqual(result["by_category"]["cat_b"]["leak"], 0)
        self.assertEqual(result["by_category"]["cat_b"]["empty"], 1)

    def test_default_paths_run_against_a_real_ledger(self):
        """不打桩：真走 ledger.search + ContextPackBuilder。

        账本里只有一条 PostgreSQL 记忆，问「火星基地的氧气配额」必须拒答。
        """
        result = eval_refusal.evaluate_refusal(
            self.ledger,
            eval_refusal.validate_refusal_queries([
                {"id": "mars", "query": "火星基地的氧气配额是多少",
                 "absent_terms": ["火星基地"]},
            ]))
        self.assertEqual(result["queries"], 1)
        self.assertEqual(result["details"][0]["verdict"], "empty")
        self.assertGreater(result["metrics"]["latency_p95_ms"], 0.0)


class CheckRefusalGateTests(unittest.TestCase):
    def _result(self, *, queries=25, empty=25, noise=0, leak=0):
        rows = ([{"id": "e%d" % i, "verdict": "empty"} for i in range(empty)]
                + [{"id": "n%d" % i, "verdict": "noise"} for i in range(noise)]
                + [{"id": "l%d" % i, "verdict": "leak"} for i in range(leak)])
        return {
            "queries": queries,
            "metrics": {"empty": empty, "noise": noise, "leak": leak,
                        "leak_rate": round(leak / queries, 4) if queries else 0.0},
            "details": rows,
        }

    def test_passes_with_zero_leaks(self):
        gate = eval_refusal.check_refusal_gate(
            self._result(), grounding={"clean": True, "violations": []})
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["failures"], [])
        self.assertTrue(gate["audited"])

    def test_fails_on_a_single_leak(self):
        gate = eval_refusal.check_refusal_gate(
            self._result(empty=24, leak=1),
            grounding={"clean": True, "violations": []})
        self.assertFalse(gate["passed"])
        self.assertTrue(any("leak_rate" in f for f in gate["failures"]))
        self.assertTrue(any("l0" in f for f in gate["failures"]))

    def test_fails_when_the_set_shrinks(self):
        gate = eval_refusal.check_refusal_gate(
            self._result(queries=5, empty=5),
            grounding={"clean": True, "violations": []})
        self.assertFalse(gate["passed"])
        self.assertTrue(any("min_queries" in f for f in gate["failures"]))

    def test_fails_on_a_grounding_violation(self):
        """标注不干净必须挡住门禁：否则标注 bug 会被当成检索 bug 修。"""
        gate = eval_refusal.check_refusal_gate(
            self._result(),
            grounding={"clean": False,
                       "violations": [{"id": "x", "term": "t",
                                       "scope": "shared", "recallable": 3}]})
        self.assertFalse(gate["passed"])
        self.assertTrue(any("grounding" in f for f in gate["failures"]))

    def test_unaudited_run_is_labelled_and_does_not_claim_clean(self):
        """--no-audit 时门禁不能假装审计通过了。"""
        gate = eval_refusal.check_refusal_gate(self._result(), grounding=None)
        self.assertTrue(gate["passed"])
        self.assertFalse(gate["audited"])

    def test_leak_threshold_can_be_loosened_explicitly(self):
        gate = eval_refusal.check_refusal_gate(
            self._result(empty=24, leak=1), max_leak_rate=0.5,
            grounding={"clean": True, "violations": []})
        self.assertTrue(gate["passed"])


class ReadOnlyLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "prod.sqlite"
        writer = SQLiteLedger(self.path)
        try:
            writer.ingest(_record("部署笔记：生产环境使用 PostgreSQL 作为主数据库"))
        finally:
            writer.close()
        self.ledger = eval_refusal.ReadOnlyLedger(self.path)
        self.addCleanup(self.ledger.close)

    def test_reads_work(self):
        hits = self.ledger.search("PostgreSQL", limit=5)
        self.assertEqual(len(hits), 1)
        self.assertIn("PostgreSQL", hits[0]["content"])

    def test_writes_are_refused_at_the_transaction_layer(self):
        from hippomind.ledger._helpers import LedgerError
        with self.assertRaises(LedgerError):
            self.ledger._transaction(lambda conn: None)

    def test_touch_is_a_noop(self):
        """评测不该把生产记忆的 touch_count 加热（ContextPack 会调它）。"""
        self.assertEqual(self.ledger.touch(["mem_x"]), 0)

    def test_query_only_blocks_writes_at_the_sqlite_layer(self):
        """即便绕过 _transaction 直接执行 UPDATE，SQLite 也必须拒绝。"""
        conn = self.ledger._connect()
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE memories SET content='hacked'")
        finally:
            conn.close()
        reader = sqlite3.connect(str(self.path))
        try:
            content = reader.execute(
                "SELECT content FROM memories LIMIT 1").fetchone()[0]
        finally:
            reader.close()
        self.assertNotIn("hacked", content)

    def test_missing_ledger_is_rejected_without_creating_it(self):
        from hippomind.ledger._helpers import LedgerError
        missing = Path(self._tmp.name) / "nope.sqlite"
        with self.assertRaises(LedgerError):
            eval_refusal.ReadOnlyLedger(missing)
        self.assertFalse(missing.exists())

    def test_source_file_is_not_modified(self):
        before = (self.path.stat().st_mtime_ns, self.path.stat().st_size)
        self.ledger.search("PostgreSQL", limit=5)
        eval_refusal.audit_grounding(self.ledger, eval_refusal.validate_refusal_queries(
            [{"id": "x", "query": "q", "absent_terms": ["火星基地"]}]))
        after = (self.path.stat().st_mtime_ns, self.path.stat().st_size)
        self.assertEqual(before, after)


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
        self.query_path = self.dir / "refusal.json"
        self.query_path.write_text(json.dumps([
            {"id": "mars", "category": "fiction",
             "query": "火星基地的氧气配额是多少", "absent_terms": ["火星基地"]},
            {"id": "moon", "category": "fiction",
             "query": "月球电梯的年度维护预算", "absent_terms": ["月球电梯"]},
        ], ensure_ascii=False), encoding="utf-8")

    def _main(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), \
                contextlib.redirect_stderr(io.StringIO()):
            code = eval_refusal.main(argv)
        return code, buffer.getvalue()

    def test_gate_passes_when_nothing_leaks(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path), "--gate", "--json",
            "--min-queries", "2"])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["gate"]["passed"])
        self.assertEqual(payload["set"], "refusal")
        self.assertEqual(payload["queries"], 2)
        self.assertTrue(payload["grounding"]["clean"])

    def test_gate_fails_when_a_term_is_actually_present(self):
        """term 存在于账本 -> 标注审计失败 -> 门禁失败（退出码 1）。"""
        path = self.dir / "bad.json"
        path.write_text(json.dumps([
            {"id": "bad", "query": "数据库选型", "absent_terms": ["PostgreSQL"]},
            {"id": "ok", "query": "火星基地的氧气配额",
             "absent_terms": ["火星基地"]},
        ], ensure_ascii=False), encoding="utf-8")
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(path), "--gate", "--json", "--min-queries", "2"])
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["gate"]["passed"])
        self.assertTrue(any("grounding" in f for f in payload["gate"]["failures"]))

    def test_report_mode_exits_zero_even_with_a_leak(self):
        """不带 --gate 时只报告，不判退出码（便于排查回归）。"""
        path = self.dir / "bad.json"
        path.write_text(json.dumps([
            {"id": "bad", "query": "数据库选型", "absent_terms": ["PostgreSQL"]},
        ], ensure_ascii=False), encoding="utf-8")
        code, _ = self._main(["--ledger", str(self.ledger_path),
                              "--queries", str(path)])
        self.assertEqual(code, 0)

    def test_human_report_contains_the_three_verdicts(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path)])
        self.assertEqual(code, 0)
        self.assertIn("== set: refusal ==", output)
        self.assertIn("empty=", output)
        self.assertIn("noise=", output)
        self.assertIn("leak=", output)
        self.assertIn("grounding: OK", output)
        self.assertIn("gate:", output)

    def test_no_audit_marks_the_gate_as_unaudited(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path), "--gate", "--no-audit",
            "--json", "--min-queries", "2"])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIsNone(payload["grounding"])
        self.assertFalse(payload["gate"]["audited"])
        self.assertIn("未审计", self._main([
            "--ledger", str(self.ledger_path),
            "--queries", str(self.query_path), "--no-audit"])[1])

    def test_missing_ledger_exits_two_without_creating_it(self):
        missing = self.dir / "nope.sqlite"
        code, _ = self._main(["--ledger", str(missing),
                              "--queries", str(self.query_path)])
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())

    def test_bad_query_file_exits_two(self):
        path = self.dir / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        code, _ = self._main(["--ledger", str(self.ledger_path),
                              "--queries", str(path)])
        self.assertEqual(code, 2)

    def test_dump_queries_writes_the_builtin_set(self):
        out = self.dir / "dumped.json"
        code, _ = self._main(["--dump-queries", str(out)])
        self.assertEqual(code, 0)
        dumped = json.loads(out.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(dumped), 20)
        self.assertEqual(len({item["id"] for item in dumped}), len(dumped))
        self.assertIn("absent_terms", dumped[0])

    def test_min_queries_override_is_honoured(self):
        code, output = self._main([
            "--ledger", str(self.ledger_path), "--queries", str(self.query_path),
            "--gate", "--min-queries", "99", "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertTrue(any("min_queries" in f
                            for f in payload["gate"]["failures"]))


if __name__ == "__main__":
    unittest.main()
