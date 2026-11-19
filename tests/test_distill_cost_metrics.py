"""Distill extractor cost/latency metrics (WP-S) and llm_client counters.

The metrics are observability only: these tests pin that the numbers are
recorded at the extract/batch boundary, are accumulated per instance and per
run, that the deterministic fallback is counted, and that instrumenting the
extraction path did not change what it produces.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import llm_client  # noqa: E402
from hippomind.distill import COST_FIELDS, DistillEngine  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402

#: Exact distilled content observed before the instrumentation (fixed output
#: regression gate: same input must still produce this row byte-for-byte).
BASELINE_DISTILL_CONTENT = (
    "提炼（raw_chat）：决定采用 SQLite WAL 作为默认存储，并保留双写日志。\n"
    "task-1 statement=决定采用 SQLite WAL 作为默认存储，并保留双写日志"
)


class FakeExtractor:
    """Injected extractor: no socket, controlled success/failure/latency."""

    name = "fake"
    available = True

    def __init__(self, *, claims=None, error=None, delay=0.0):
        self.claims = claims
        self.error = error
        self.delay = delay
        self.calls = 0

    def extract(self, candidates):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.claims is None:
            return None
        return list(self.claims)


CLAIM = {"subject": "atlas", "predicate": "uses", "slot_key": "store",
         "value": "SQLite", "confidence": 0.9, "source": 1}


class DistillCostMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-distill-cost-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self._sequence = 0

    def _seed(self, content, session=None, timestamp="2026-11-13T10:00:00Z"):
        self._sequence += 1
        self.ledger.ingest({
            "content": content, "kind": "raw_chat", "raw_chat": True,
            "source_agent": "codex", "scope": "agent:codex",
            "session_id": session or ("s-%d" % self._sequence),
            "project": "atlas", "task_id": "task-1",
            "evidence": ["codex:cost:%d" % self._sequence],
            "timestamp": timestamp,
        })

    def _distilled_rows(self):
        with self.ledger._reading() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT content, scope FROM memories "
                "WHERE source_agent='hippomind-distill' ORDER BY content")]

    # -- success path ------------------------------------------------------
    def test_successful_extraction_records_calls_items_and_run_summary(self):
        self._seed("决定采用 SQLite WAL 作为默认存储，并保留双写日志。")
        self._seed("决定把默认主题改为纸本，并更新验收清单。")
        extractor = FakeExtractor(claims=[CLAIM])
        engine = DistillEngine(self.ledger, extractor=extractor)

        summary = engine.run(limit=5, batch_size=8, dedupe=False)

        self.assertEqual(1, extractor.calls)
        self.assertEqual(1, summary["batch_count"])
        self.assertEqual(1, summary["batches"], "既有 batches 键语义不变")
        self.assertEqual(1, summary["extractor_calls"])
        self.assertEqual(0, summary["extractor_failures"])
        self.assertEqual(2, summary["items_in"])
        self.assertEqual(1, summary["items_out"])
        self.assertEqual(0, summary["deterministic_fallback_count"])
        self.assertGreaterEqual(summary["extractor_ms"], 0.0)
        for field in COST_FIELDS:
            self.assertIn(field, summary)

        stats = engine.cost_stats()
        self.assertEqual(1, stats["cumulative"]["runs"])
        self.assertEqual(1, stats["cumulative"]["extractor_calls"])
        self.assertEqual(0, stats["cumulative"]["extractor_failures"])
        self.assertEqual(2, stats["cumulative"]["items_in"])
        self.assertEqual(1, stats["cumulative"]["items_out"])
        self.assertEqual(summary["extractor_calls"],
                         stats["last_run"]["extractor_calls"])
        self.assertEqual(summary["extractor_ms"], stats["last_run"]["extractor_ms"])
        self.assertEqual("fake", stats["extractor"])
        self.assertTrue(stats["available"])
        snapshot = stats["cumulative"]
        snapshot["extractor_calls"] = -1
        self.assertEqual(1, engine.cost_stats()["cumulative"]["extractor_calls"],
                         "cost_stats 返回副本，调用方突变不得污染内部计数")
        self.assertGreaterEqual(len(self._distilled_rows()), 1)

    # -- failure / fallback ------------------------------------------------
    def test_extractor_returning_none_counts_failure_and_deterministic_fallback(self):
        self._seed("决定采用 SQLite WAL 作为默认存储，并保留双写日志。")
        extractor = FakeExtractor(claims=None)
        engine = DistillEngine(self.ledger, extractor=extractor)

        summary = engine.run(limit=5, batch_size=8, dedupe=False)

        self.assertEqual(1, summary["extractor_calls"])
        self.assertEqual(1, summary["extractor_failures"])
        self.assertEqual(1, summary["deterministic_fallback_count"])
        self.assertEqual("heuristic", summary["extractor"])
        self.assertGreaterEqual(summary["distilled"], 1, "heuristic 回退仍要写回")
        stats = engine.cost_stats()
        self.assertEqual(1, stats["cumulative"]["extractor_failures"])
        self.assertEqual(1, stats["cumulative"]["deterministic_fallback_count"])
        self.assertEqual(1, stats["last_run"]["deterministic_fallback_count"])

    def test_extractor_that_raises_is_counted_and_still_propagates(self):
        self._seed("决定采用 SQLite WAL 作为默认存储，并保留双写日志。")
        extractor = FakeExtractor(error=RuntimeError("boom"))
        engine = DistillEngine(self.ledger, extractor=extractor)

        with self.assertRaises(RuntimeError):
            engine.run(limit=5, batch_size=8, dedupe=False)

        stats = engine.cost_stats()
        self.assertEqual(1, stats["cumulative"]["extractor_calls"])
        self.assertEqual(1, stats["cumulative"]["extractor_failures"])
        self.assertEqual({}, stats["last_run"], "未完成的 run 不发布 last_run")
        self.assertEqual(0, len(self._distilled_rows()))

    def test_slow_extractor_latency_is_measured_and_accumulated(self):
        self._seed("决定采用 SQLite WAL 作为默认存储，并保留双写日志。")
        extractor = FakeExtractor(claims=[CLAIM], delay=0.03)
        engine = DistillEngine(self.ledger, extractor=extractor)

        summary = engine.run(limit=5, batch_size=8, dedupe=False)

        self.assertGreaterEqual(summary["extractor_ms"], 20.0,
                                "30ms 的假提取器不能被记成 0")
        stats = engine.cost_stats()
        self.assertAlmostEqual(summary["extractor_ms"],
                               stats["cumulative"]["extractor_ms"], places=3)
        self.assertAlmostEqual(summary["extractor_ms"],
                               stats["cumulative"]["extractor_ms_avg"], places=3)

    # -- two runs ----------------------------------------------------------
    def test_two_runs_accumulate_but_last_run_is_per_run(self):
        extractor = FakeExtractor(claims=[CLAIM])
        engine = DistillEngine(self.ledger, extractor=extractor)

        self._seed("决定采用 SQLite WAL 作为默认存储，并保留双写日志。")
        first = engine.run(limit=5, batch_size=8, dedupe=False)
        self.assertEqual(1, first["extractor_calls"])

        self._seed("决定把默认主题改为纸本，并更新验收清单。")
        second = engine.run(limit=5, batch_size=8, dedupe=False)
        self.assertEqual(1, second["extractor_calls"])

        stats = engine.cost_stats()
        self.assertEqual(2, stats["cumulative"]["runs"])
        self.assertEqual(2, stats["cumulative"]["extractor_calls"])
        self.assertEqual(2, stats["cumulative"]["batches"])
        self.assertEqual(2, stats["cumulative"]["items_in"])
        self.assertEqual(2, stats["cumulative"]["items_out"])
        self.assertEqual(1, stats["last_run"]["extractor_calls"],
                         "last_run 是最近一轮，不是累计")
        self.assertEqual(1, stats["last_run"]["items_in"])
        self.assertEqual(2, extractor.calls)

    # -- no behavioural regression ----------------------------------------
    def test_heuristic_output_and_existing_summary_keys_are_unchanged(self):
        self._seed("决定采用 SQLite WAL 作为默认存储，并保留双写日志。",
                   session="s-1")
        engine = DistillEngine(self.ledger, extractor=None)
        summary = engine.run(limit=5, batch_size=8)

        rows = self._distilled_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(BASELINE_DISTILL_CONTENT, rows[0]["content"])
        self.assertEqual("agent:codex", rows[0]["scope"])
        # Existing summary keys keep their meaning...
        self.assertEqual(1, summary["batches"])
        self.assertEqual("heuristic", summary["extractor"])
        self.assertEqual(1, summary["distilled"])
        self.assertEqual(1, summary["claims"])
        self.assertEqual(1, summary["candidates"])
        self.assertEqual(1, summary["distinct"])
        self.assertEqual(0, summary["duplicates"])
        # ...and the new keys describe the deterministic path.
        self.assertEqual(0, summary["extractor_calls"])
        self.assertEqual(0, summary["extractor_failures"])
        self.assertEqual(1, summary["batch_count"])
        self.assertEqual(1, summary["items_in"])
        self.assertEqual(1, summary["items_out"])
        self.assertEqual(1, summary["deterministic_fallback_count"])
        self.assertEqual(0, engine.run(limit=5, batch_size=8)["candidates"])
        stats = engine.cost_stats()
        self.assertEqual(2, stats["cumulative"]["runs"])
        self.assertEqual(1, stats["cumulative"]["batches"],
                         "第二轮没有候选，不产生 batch")
        self.assertEqual(0, stats["last_run"]["batches"],
                         "最近一轮是第二轮（无候选），batches 应为 0")


# ---------------------------------------------------------------------------
# llm_client transport counters
# ---------------------------------------------------------------------------
class Response:
    def __init__(self, body):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def message_body(text):
    return {"content": [{"type": "text", "text": text}], "model": "m"}


class LlmClientStatsTests(unittest.TestCase):
    FAKE_KEY = "sk-test-not-a-real-credential-0000000000"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-llm-stats-")
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "settings.json"
        self.settings.write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://llm.example.test",
            "ANTHROPIC_API_KEY": self.FAKE_KEY,
            "ANTHROPIC_MODEL": "test-model-1",
        }}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ,
                                  {llm_client.SETTINGS_ENV: str(self.settings)},
                                  clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("HIPPOMIND_LLM_BASE_URL", "HIPPOMIND_LLM_API_KEY",
                     "HIPPOMIND_LLM_MODEL"):
            os.environ.pop(name, None)
        llm_client.reset()
        self.addCleanup(llm_client.reset)

    def test_successful_calls_record_count_and_latency(self):
        def opener(request, timeout=None):
            return Response(message_body("ok"))
        with mock.patch("urllib.request.urlopen", opener):
            self.assertEqual("ok", llm_client.complete("a"))
            self.assertEqual("ok", llm_client.complete("b"))
        stats = llm_client.stats()
        self.assertEqual(2, stats["calls"])
        self.assertEqual(2, stats["successes"])
        self.assertEqual(0, stats["failures"])
        self.assertGreaterEqual(stats["total_ms"], 0.0)
        self.assertGreaterEqual(stats["last_ms"], 0.0)
        self.assertEqual(2, stats["breaker"]["calls"])

    def test_timeout_is_counted_separately_without_changing_behaviour(self):
        def opener(request, timeout=None):
            raise TimeoutError("timed out")
        with mock.patch.object(llm_client, "RETRY_BACKOFF", 0.0):
            with mock.patch("urllib.request.urlopen", opener):
                self.assertIsNone(llm_client.complete("hi"))
        stats = llm_client.stats()
        self.assertEqual(1, stats["calls"], "一次 complete 只记一个调用")
        self.assertEqual(1, stats["timeouts"])
        self.assertEqual(1, stats["failures"])

    def test_http_error_and_parse_failure_are_classified(self):
        def http_error(request, timeout=None):
            raise urllib.error.HTTPError("https://llm.example.test", 500,
                                         "boom", {}, None)
        with mock.patch("urllib.request.urlopen", http_error):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertEqual(1, llm_client.stats()["http_errors"])

        llm_client.reset()
        def opener(request, timeout=None):
            return Response("not json")
        with mock.patch("urllib.request.urlopen", opener):
            self.assertIsNone(llm_client.complete("hi"))
        self.assertEqual(1, llm_client.stats()["parse_failures"])

    def test_pre_request_skips_are_not_counted_and_reset_zeroes_counters(self):
        llm_client.breaker().record_failure("boom")
        llm_client.breaker().record_failure("boom")
        self.assertIsNone(llm_client.complete("hi"))
        self.assertEqual(0, llm_client.stats()["calls"], "冷却期内未发请求")
        self.assertIsNone(llm_client.complete(""))
        self.assertEqual(0, llm_client.stats()["calls"])
        llm_client.reset()
        self.assertEqual(0, llm_client.stats()["calls"])


if __name__ == "__main__":
    unittest.main()
