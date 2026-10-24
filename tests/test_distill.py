"""Automatic distillation: evidence -> claims, idempotent and conflict-aware."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import llm_client
from hippomind.distill import Candidate, ClaudeExtractor, DistillEngine
from hippomind.ledger import SQLiteLedger


class DistillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-distill-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _seed(self):
        self.ledger.ingest({
            "content": "任务：把 GUI 主题默认设为纸本",
            "kind": "task", "source_agent": "codex", "scope": "shared",
            "project": "atlas", "task_id": "task-1",
            "evidence": ["codex:1"], "timestamp": "2026-10-23T10:00:00Z",
            "metadata": {"context": {"goal": ["把 GUI 主题默认设为纸本"],
                                     "key_decisions": ["决定使用纸本作为默认主题"]}},
        })
        self.ledger.ingest({
            "content": "决定采用 SQLite + WAL；数据库使用 SQLite。",
            "kind": "raw_chat", "source_agent": "claude", "scope": "agent:claude",
            "raw_chat": True, "session_id": "s-1", "project": "atlas",
            "evidence": ["claude:1"], "timestamp": "2026-10-23T10:01:00Z",
        })

    def test_heuristic_distillation_creates_claims_and_is_idempotent(self):
        self._seed()
        engine = DistillEngine(self.ledger, extractor=None)
        first = engine.run(limit=10)
        self.assertGreaterEqual(first["distilled"], 1)
        self.assertGreaterEqual(first["claims"], 2)
        with self.ledger._reading() as conn:
            distilled = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_agent='hippomind-distill'").fetchone()[0]
            claims = conn.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0]
        self.assertGreaterEqual(distilled, 1)
        self.assertGreaterEqual(claims, 2)
        second = engine.run(limit=10)
        self.assertEqual(0, second["candidates"])

    def test_dry_run_does_not_write(self):
        self._seed()
        engine = DistillEngine(self.ledger, extractor=None)
        result = engine.run(limit=10, dry_run=True)
        self.assertGreaterEqual(result["claims"], 1)
        with self.ledger._reading() as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_agent='hippomind-distill'").fetchone()[0])

    def test_conflict_resolution_is_bounded(self):
        self._seed()
        engine = DistillEngine(self.ledger, extractor=None)
        resolved = engine.resolve_conflicts(limit=10)
        self.assertIsInstance(resolved, int)
        self.assertGreaterEqual(resolved, 0)


def _claims_payload(*items):
    """A model-shaped answer: JSON with one claim per (source, value) pair."""
    return json.dumps({"claims": [
        {"subject": "atlas", "predicate": "uses", "slot_key": "store_%d" % index,
         "value": value, "confidence": 0.9, "source": source}
        for index, (source, value) in enumerate(items)]})


class TransportTests(unittest.TestCase):
    """Which transport answers, and what the caller is told about it.

    Every test here injects its transport.  Nothing in this class may reach a
    socket or spawn a process: the point is the routing decision, not the
    network.
    """

    def _candidate(self, index=1):
        return Candidate("m%d" % index, "task", "内容 %d" % index, "codex",
                         "atlas", "task-1", "s-1", "2026-10-24T00:00:00Z", {})

    def test_api_answer_is_parsed_without_the_cli(self):
        calls = []

        def fake(prompt, max_tokens=None, timeout=None):
            calls.append((prompt, max_tokens, timeout))
            return _claims_payload((1, "用 SQLite"))

        extractor = ClaudeExtractor(timeout=7.0, complete=fake, use_cli=False)
        extractor.use_api = True
        claims = extractor.extract([self._candidate()])
        self.assertEqual(1, len(claims))
        self.assertEqual("用 SQLite", claims[0]["value"])
        self.assertEqual(1, len(calls))
        self.assertEqual(7.0, calls[0][2], "超时必须传到传输层")
        self.assertEqual("api", extractor.name)
        self.assertEqual("api", extractor.transport)

    def test_the_prompt_is_unchanged_by_the_transport_swap(self):
        """The comparison in docs/DISTILL.md is only valid if the prompt is."""
        seen = {}

        def fake(prompt, max_tokens=None, timeout=None):
            seen["prompt"] = prompt
            return _claims_payload((1, "v"))

        extractor = ClaudeExtractor(complete=fake, use_cli=False)
        extractor.use_api = True
        extractor.extract([self._candidate()])
        prompt = seen["prompt"]
        # The contract the model is asked to satisfy, verbatim from before.
        for fragment in ("你是 HippoMind 的记忆提炼器", "只输出 JSON",
                         "subject", "predicate", "slot_key", "value",
                         "confidence", "source", "[1] agent=codex"):
            self.assertIn(fragment, prompt)
        self.assertIn("片段：", prompt)

    def test_api_failure_falls_back_to_the_cli(self):
        def broken(prompt, max_tokens=None, timeout=None):
            raise OSError("endpoint down")

        payload = _claims_payload((1, "cli 答案"))
        completed = mock.Mock(returncode=0, stdout=payload, stderr="")
        extractor = ClaudeExtractor(complete=broken)
        extractor.use_api = True
        extractor.binary = "/bin/echo"
        with mock.patch("subprocess.run", return_value=completed):
            claims = extractor.extract([self._candidate()])
        self.assertEqual("cli 答案", claims[0]["value"])
        self.assertEqual("cli", extractor.transport)

    def test_both_transports_failing_returns_none_for_the_heuristic(self):
        def broken(prompt, max_tokens=None, timeout=None):
            return None

        extractor = ClaudeExtractor(complete=broken, use_cli=False)
        extractor.use_api = True
        self.assertIsNone(extractor.extract([self._candidate()]))

    def test_a_transport_exception_never_escapes(self):
        def exploding(prompt, max_tokens=None, timeout=None):
            raise RuntimeError("boom")

        extractor = ClaudeExtractor(complete=exploding, use_cli=False)
        extractor.use_api = True
        self.assertIsNone(extractor.extract([self._candidate()]))
        self.assertIn("api returned no usable answer", extractor.last_error)

    def test_unparseable_api_answer_is_not_a_success(self):
        extractor = ClaudeExtractor(complete=lambda *a, **k: "我觉得还行", use_cli=False)
        extractor.use_api = True
        self.assertIsNone(extractor.extract([self._candidate()]))

    def test_api_transport_reports_itself_in_the_engine_summary(self):
        tmp = tempfile.TemporaryDirectory(prefix="hippo-transport-")
        self.addCleanup(tmp.cleanup)
        ledger = SQLiteLedger(Path(tmp.name) / "hub.sqlite")
        self.addCleanup(ledger.close)
        ledger.ingest({"content": "决定采用 SQLite 作为存储。", "kind": "decision",
                       "source_agent": "codex", "scope": "shared",
                       "evidence": ["codex:1"], "timestamp": "2026-10-24T00:00:00Z"})
        extractor = ClaudeExtractor(
            complete=lambda *a, **k: _claims_payload((1, "用 SQLite")), use_cli=False)
        extractor.use_api = True
        engine = DistillEngine(ledger, extractor=extractor)
        result = engine.run(limit=4, batch_size=8)
        self.assertEqual("api", result["extractor"])
        self.assertEqual(1, result["distilled"])

    def test_the_token_budget_is_large_enough_for_a_dense_batch(self):
        """Regression: a low cap truncates the JSON and loses the whole batch.

        Measured on production data, one 8-item batch needed ~2,600 output
        tokens; at max_tokens=2048 the answer ended mid-object, json.loads
        raised, and all 8 items fell through to the heuristic.  The cap must
        stay comfortably above the measured worst case.
        """
        from hippomind.distill import EXTRACT_MAX_TOKENS

        self.assertGreaterEqual(EXTRACT_MAX_TOKENS, 4_096)

    def test_a_truncated_answer_is_a_miss_not_a_partial_batch(self):
        """Half an object must never be salvaged into claims for some items."""
        truncated = '{"claims":[{"subject":"a","predicate":"b","slot_key":"c","value":"d"'
        extractor = ClaudeExtractor(complete=lambda *a, **k: truncated, use_cli=False)
        extractor.use_api = True
        self.assertIsNone(extractor.extract([self._candidate()]))

    def test_the_budget_is_passed_to_the_transport(self):
        seen = {}

        def fake(prompt, max_tokens=None, timeout=None):
            seen["max_tokens"] = max_tokens
            return _claims_payload((1, "v"))

        extractor = ClaudeExtractor(complete=fake, use_cli=False)
        extractor.use_api = True
        extractor.extract([self._candidate()])
        self.assertGreaterEqual(seen["max_tokens"], 4_096)

    def test_a_mixed_pass_reports_every_extractor_it_used(self):
        """A pass that mixes transports must not be summarised by its last batch.

        Real case: a batch whose model answer is a valid *empty* claims array
        falls back to the heuristic for that batch only.  Reporting only the
        last batch's name made a mostly-API run look entirely heuristic.
        """
        tmp = tempfile.TemporaryDirectory(prefix="hippo-mixed-")
        self.addCleanup(tmp.cleanup)
        ledger = SQLiteLedger(Path(tmp.name) / "hub.sqlite")
        self.addCleanup(ledger.close)
        for index in range(3):
            ledger.ingest({"content": "决定采用 SQLite 作为存储 %d。" % index,
                           "kind": "decision", "source_agent": "codex",
                           "scope": "shared", "evidence": ["codex:%d" % index],
                           "timestamp": "2026-10-24T00:00:0%dZ" % index})
        # Candidates come newest-first, so this order is deterministic: the
        # middle batch is the one whose model answer is legitimately empty.
        answers = iter([_claims_payload((1, "用 SQLite")), '{"claims":[]}',
                        _claims_payload((1, "用 SQLite")), '{"claims":[]}'])

        def fake(prompt, max_tokens=None, timeout=None):
            return next(answers)

        extractor = ClaudeExtractor(complete=fake, use_cli=False)
        extractor.use_api = True
        engine = DistillEngine(ledger, extractor=extractor)
        result = engine.run(limit=4, batch_size=1)
        self.assertEqual({"api": 2, "heuristic": 1}, result["extractors"])

    def test_heuristic_is_reported_when_no_transport_is_usable(self):
        tmp = tempfile.TemporaryDirectory(prefix="hippo-transport-")
        self.addCleanup(tmp.cleanup)
        ledger = SQLiteLedger(Path(tmp.name) / "hub.sqlite")
        self.addCleanup(ledger.close)
        ledger.ingest({"content": "决定采用 SQLite 作为存储。", "kind": "decision",
                       "source_agent": "codex", "scope": "shared",
                       "evidence": ["codex:1"], "timestamp": "2026-10-24T00:00:00Z"})
        extractor = ClaudeExtractor(use_cli=False)
        extractor.use_api = False
        engine = DistillEngine(ledger, extractor=extractor)
        result = engine.run(limit=4, batch_size=8)
        self.assertEqual("heuristic", result["extractor"])


class TransportPolicyTests(unittest.TestCase):
    """HIPPOMIND_DISTILL_TRANSPORT selects which transports may run."""

    def tearDown(self):
        llm_client.reset()

    def test_unknown_policy_means_auto_not_nothing(self):
        with mock.patch.dict("os.environ", {"HIPPOMIND_DISTILL_TRANSPORT": "bogus"}):
            extractor = ClaudeExtractor()
        self.assertEqual("auto", extractor.transport_policy)
        self.assertEqual(bool(extractor.use_api or extractor.use_cli), True)

    def test_api_policy_excludes_the_cli(self):
        extractor = ClaudeExtractor(transport="api")
        self.assertFalse(extractor.use_cli)

    def test_cli_policy_excludes_the_api(self):
        extractor = ClaudeExtractor(transport="cli")
        self.assertFalse(extractor.use_api)


if __name__ == "__main__":
    unittest.main()
