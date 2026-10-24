"""Injection reuse measurement: samples, controls and honest uncertainty.

The tests build a tiny synthetic ledger and drive the evaluator with explicit
trace records, so nothing here touches a real database, the network or a model
file.  Two things matter more than the numbers:

* every aggregate must carry its sample size;
* a thin sample must produce an explicit "not enough evidence" verdict rather
  than a confident-looking ratio.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.injection_quality import (
    MIN_CITATION_RATE,
    MIN_SAMPLES,
    MIN_TERM_OVERLAP,
    ReadOnlyLedger,
    _parse_time,
    evaluate,
    extract_signals,
    report,
    wilson,
)
from hippomind.ledger import SQLiteLedger


class _Embedder:
    """Deterministic bag-of-characters embedder: no model file, no network."""

    def __init__(self):
        self.dim = 64

    def embed(self, text):
        vector = [0.0] * self.dim
        for char in str(text or ""):
            vector[ord(char) % self.dim] += 1.0
        return vector

    def cosine(self, a, b):
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0


class InjectionQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-injq-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

    # -- fixtures -----------------------------------------------------------
    def _memory(self, content, *, kind="semantic", agent="codex"):
        return self.ledger.ingest({
            "content": content, "kind": kind, "source_agent": agent,
            "scope": "shared", "evidence": ["codex:1"],
            "timestamp": self.now.isoformat(),
        }).memory_id

    def _reply(self, content, minutes=5, *, agent="codex", raw=True):
        self.ledger.ingest({
            "content": content, "kind": "raw_chat", "source_agent": agent,
            "scope": "agent:%s" % agent, "raw_chat": raw, "role": "assistant",
            "session_id": "s1", "evidence": ["codex:r"],
            "timestamp": (self.now + timedelta(minutes=minutes)).isoformat(),
        })

    def _trace(self, memory_ids, *, at=None, agent="codex", items=None, source="codex-hook"):
        return {"at": (at or self.now).isoformat(), "agent": agent, "source": source,
                "items": len(memory_ids) if items is None else items,
                "memory_ids": list(memory_ids)}

    # -- time parsing -------------------------------------------------------
    def test_trace_offset_without_colon_parses(self):
        # injection_trace writes %z ("-0700"); fromisoformat rejects it on 3.9.
        parsed = _parse_time("2026-10-24T04:02:05-0700")
        self.assertIsNotNone(parsed)
        self.assertEqual(11, parsed.hour)  # 04:02 -0700 == 11:02 UTC
        self.assertIsNone(_parse_time("not-a-time"))
        self.assertIsNone(_parse_time(""))

    def test_unparseable_timestamp_is_reported_not_guessed(self):
        memory_id = self._memory("默认主题使用纸本")
        self._reply("我会把默认主题设为纸本")
        result = evaluate(self.ledger, [{
            "at": "garbage", "agent": "codex", "memory_ids": [memory_id]}])
        self.assertEqual(0, result["sampled"])
        self.assertEqual(1, result["sample"]["skip_reasons"].get("bad_timestamp"))

    # -- sample accounting --------------------------------------------------
    def test_trace_without_ids_is_counted_as_skipped(self):
        result = evaluate(self.ledger, [{"at": self.now.isoformat(), "agent": "codex"}])
        self.assertEqual(0, result["traced_with_ids"])
        self.assertEqual(1, result["sample"]["skip_reasons"].get("no_memory_ids"))
        self.assertIsNone(result["reuse_rate"])

    def test_vanished_memory_is_skipped_with_reason(self):
        result = evaluate(self.ledger, [self._trace(["mem_does_not_exist"])])
        self.assertEqual(0, result["sampled"])
        self.assertEqual(1, result["sample"]["skip_reasons"].get("memories_missing"))

    def test_missing_reply_window_is_skipped_with_reason(self):
        memory_id = self._memory("默认主题使用纸本")
        result = evaluate(self.ledger, [self._trace([memory_id])])
        self.assertEqual(0, result["sampled"])
        self.assertEqual(1, result["sample"]["skip_reasons"].get("no_replies"))

    # -- the original proxy still works -------------------------------------
    def test_overlap_marks_reuse(self):
        memory_id = self._memory("默认主题使用纸本，数据库使用 SQLite WAL")
        self._reply("我会把默认主题设为纸本，并用 SQLite WAL 存储")
        result = evaluate(self.ledger, [self._trace([memory_id])])
        self.assertEqual(1, result["traced_with_ids"])
        self.assertEqual(1, result["sampled"])
        self.assertEqual(1, result["reused"])
        self.assertGreaterEqual(result["reuse_rate"], 0.5)

    def test_overlap_threshold_is_the_documented_constant(self):
        # Pin the threshold so a silent change cannot move every verdict.
        memory_id = self._memory("默认主题使用纸本，数据库使用 SQLite WAL，检索使用 FTS5")
        self._reply("默认主题使用纸本")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None,
                          controls=False)
        row = result["details"][-1]
        self.assertEqual(MIN_TERM_OVERLAP, 0.15)
        self.assertEqual(row["used"], row["overlap"] >= MIN_TERM_OVERLAP)
        self.assertLess(row["overlap"], 1.0)

    def test_unrelated_reply_is_not_reuse(self):
        memory_id = self._memory("默认主题使用纸本，数据库使用 SQLite WAL")
        self._reply("今天先休息一下，明天再看")
        result = evaluate(self.ledger, [self._trace([memory_id])])
        self.assertEqual(0, result["reused"])
        self.assertEqual(0.0, result["reuse_rate"])

    def test_window_boundary_excludes_late_replies(self):
        memory_id = self._memory("默认主题使用纸本")
        self._reply("默认主题使用纸本", minutes=90)
        result = evaluate(self.ledger, [self._trace([memory_id])], window_minutes=30)
        self.assertEqual(0, result["sampled"])
        self.assertEqual(1, result["sample"]["skip_reasons"].get("no_replies"))

    def test_other_agent_replies_do_not_count(self):
        memory_id = self._memory("默认主题使用纸本")
        self._reply("默认主题使用纸本", agent="claude")
        result = evaluate(self.ledger, [self._trace([memory_id])])
        self.assertEqual(0, result["sampled"])

    # -- harness noise ------------------------------------------------------
    def test_harness_error_replies_are_not_treated_as_usage(self):
        memory_id = self._memory("默认主题使用纸本")
        self._reply("API Error: Request rejected (429) · Upstream rate limit")
        result = evaluate(self.ledger, [self._trace([memory_id])])
        self.assertEqual(0, result["sampled"])
        self.assertEqual(1, result["sample"]["skip_reasons"].get("no_replies"))

    def test_structured_probe_replies_are_not_treated_as_usage(self):
        memory_id = self._memory("默认主题使用纸本")
        self._reply('{"claims":[]}')
        result = evaluate(self.ledger, [self._trace([memory_id])])
        self.assertEqual(0, result["sampled"])

    # -- citation -----------------------------------------------------------
    def test_distinctive_signal_extraction(self):
        signals = extract_signals(
            "见 hippomind/ledger/base.py 与 mem_abc123，版本 v4.2.1，命令 sqlite3 --version")
        self.assertEqual("path", signals.get("hippomind/ledger/base.py"))
        self.assertEqual("file", signals.get("base.py"))
        self.assertEqual("ident", signals.get("mem_abc123"))
        self.assertIn("v4.2.1", signals)
        self.assertEqual("version", signals.get("v4.2.1"))

    def test_citation_needs_a_distinctive_signal(self):
        memory_id = self._memory("修改 hippomind/injection_quality.py 里的 MIN_SAMPLES")
        self._reply("我已经改过 hippomind/injection_quality.py，MIN_SAMPLES 保持 30")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        row = result["details"][-1]
        self.assertGreater(row["signals"], 0)
        self.assertGreaterEqual(row["citation_rate"], MIN_CITATION_RATE)
        self.assertTrue(row["cited"])

    def test_citation_absent_when_reply_shares_only_prose(self):
        memory_id = self._memory("修改 hippomind/injection_quality.py 里的 MIN_SAMPLES")
        self._reply("好的，我会继续优化这个记忆系统的整体体验")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        row = result["details"][-1]
        self.assertEqual(0.0, row["citation_rate"])
        self.assertFalse(row["cited"])

    def test_common_signals_are_downweighted_by_background(self):
        # A signal every background reply contains must not dominate citation.
        for index in range(6):
            self._reply("common_token 出现在每一轮回复里 %d" % index, minutes=1 + index)
        memory_id = self._memory("common_token 以及 hippomind/rare_file.py")
        self._reply("我只提到了 hippomind/rare_file.py", minutes=20)
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        row = result["details"][-1]
        self.assertTrue(row["cited"])
        self.assertGreater(row["citation_rate"], 0.5)

    # -- paraphrase ---------------------------------------------------------
    def test_paraphrase_uses_injected_embedder(self):
        memory_id = self._memory("默认主题使用纸本")
        self._reply("我会把默认主题设为纸本")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=_Embedder())
        self.assertEqual(1, result["metrics"]["paraphrase"]["n"])
        self.assertGreater(result["metrics"]["paraphrase"]["mean"], 0.0)
        self.assertTrue(result["metrics"]["paraphrase"]["enabled"])

    def test_paraphrase_disabled_without_embedder(self):
        memory_id = self._memory("默认主题使用纸本")
        self._reply("我会把默认主题设为纸本")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        self.assertEqual(0, result["metrics"]["paraphrase"]["n"])
        self.assertFalse(result["metrics"]["paraphrase"]["enabled"])

    def test_broken_embedder_does_not_break_the_report(self):
        class Broken:
            def embed(self, text):
                raise RuntimeError("model exploded")

            def cosine(self, a, b):
                raise RuntimeError("model exploded")

        memory_id = self._memory("默认主题使用纸本")
        self._reply("我会把默认主题设为纸本")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=Broken())
        self.assertEqual(1, result["sampled"])
        self.assertEqual(0.0, result["metrics"]["paraphrase"]["mean"])

    # -- correction ---------------------------------------------------------
    def test_correction_is_a_negative_signal(self):
        memory_id = self._memory("数据库使用 hippomind/ledger/base.py 的 SQLite WAL")
        self._reply("不对，hippomind/ledger/base.py 已经改为 rollback journal 了")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        row = result["details"][-1]
        self.assertTrue(row["corrected"])
        self.assertEqual(1, result["metrics"]["correction"]["corrected"])

    def test_plain_agreement_is_not_a_correction(self):
        memory_id = self._memory("数据库使用 hippomind/ledger/base.py 的 SQLite WAL")
        self._reply("好的，hippomind/ledger/base.py 继续使用 SQLite WAL")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        self.assertFalse(result["details"][-1]["corrected"])

    def test_correction_marker_without_signal_is_ignored(self):
        memory_id = self._memory("数据库使用 hippomind/ledger/base.py 的 SQLite WAL")
        self._reply("不对，这个和另一件事无关")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        self.assertFalse(result["details"][-1]["corrected"])

    # -- control arm --------------------------------------------------------
    def test_control_arm_compares_against_not_injected_memories(self):
        injected = self._memory("见 hippomind/injected_only.py 的实现")
        # A same-kind, never-injected memory whose signal is the one reused.
        self._memory("见 hippomind/control_only.py 的实现")
        self._reply("我读了 hippomind/control_only.py 但没有提到注入的那份")
        result = evaluate(self.ledger, [self._trace([injected])], embedder=None)
        control = result["control"]
        self.assertEqual("matched_not_injected", control["mode"])
        self.assertGreaterEqual(control["pairs"], 1)
        self.assertTrue(control["observational"])
        self.assertFalse(control["causal"])
        self.assertLess(result["details"][-1]["lift"], 0)

    def test_control_arm_disabled(self):
        injected = self._memory("见 hippomind/injected_only.py 的实现")
        self._memory("见 hippomind/control_only.py 的实现")
        self._reply("我读了 hippomind/control_only.py")
        result = evaluate(self.ledger, [self._trace([injected])],
                          embedder=None, controls=False)
        self.assertEqual("none", result["control"]["mode"])
        self.assertEqual(0, result["control"]["pairs"])

    def test_control_arm_never_contains_another_traces_injected_memory(self):
        """Regression: leaking injected ids into the control collapses lift to 0."""
        first = self._memory("见 hippomind/leak_a.py 的独特实现")
        second = self._memory("见 hippomind/leak_b.py 的独特实现")
        self._reply("我读了 hippomind/leak_a.py", minutes=3)
        self._reply("我读了 hippomind/leak_b.py", minutes=8)
        traces = [self._trace([first]), self._trace([second])]
        result = evaluate(self.ledger, traces, embedder=None, min_samples=2)
        # Nothing outside the two injected memories exists, so no control can
        # be paired at all; the report must say so instead of inventing one.
        self.assertEqual("none", result["control"]["mode"])
        self.assertEqual(0, result["control"]["pairs"])
        self.assertEqual("no_control", result["verdict"]["code"])

    # -- verdict / honesty --------------------------------------------------
    def test_zero_samples_yields_explicit_no_conclusion(self):
        result = evaluate(self.ledger, [{"at": self.now.isoformat(), "agent": "codex"}])
        self.assertEqual("no_samples", result["verdict"]["code"])
        self.assertFalse(result["verdict"]["conclusive"])
        self.assertFalse(result["conclusive"])
        self.assertIn("没有可用样本", result["verdict"]["headline"])

    def test_thin_sample_is_called_insufficient_not_conclusive(self):
        memory_id = self._memory("见 hippomind/thin_sample.py 的实现")
        self._reply("我读了 hippomind/thin_sample.py")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        self.assertEqual(1, result["sampled"])
        self.assertEqual("insufficient_sample", result["verdict"]["code"])
        self.assertFalse(result["conclusive"])
        self.assertEqual(MIN_SAMPLES - 1, result["sample_shortfall"])
        self.assertIn("不足以支撑结论", result["verdict"]["headline"])

    def test_verdict_caveats_always_name_observational_limit(self):
        memory_id = self._memory("见 hippomind/caveat.py")
        self._reply("我读了 hippomind/caveat.py")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        joined = " ".join(result["verdict"]["caveats"])
        self.assertIn("观察性", joined)
        self.assertIn("不等于因果", joined)

    def test_enough_samples_positive_lift_is_still_observational(self):
        traces = []
        for index in range(3):
            injected = self._memory("见 hippomind/win_%d.py 的独特实现" % index)
            # A never-injected same-kind memory supplies the control arm; its
            # signal is absent from the replies, so lift is positive.
            self._memory("见 hippomind/lose_ctrl_%d.py 的独特实现" % index)
            self._reply("我读了 hippomind/win_%d.py 并照此实现" % index, minutes=5 + index)
            traces.append(self._trace([injected]))
        result = evaluate(self.ledger, traces, embedder=None, min_samples=3)
        self.assertEqual(3, result["sampled"])
        self.assertEqual("observational_positive", result["verdict"]["code"])
        self.assertIn("不是因果证明", result["verdict"]["headline"])

    def test_negative_lift_is_reported_as_no_support(self):
        traces = []
        for index in range(3):
            injected = self._memory("见 hippomind/lose_%d.py 的独特实现" % index)
            self._memory("见 hippomind/other_%d.py 的独特实现" % index)
            self._reply("我读了 hippomind/other_%d.py" % index, minutes=5 + index)
            traces.append(self._trace([injected]))
        result = evaluate(self.ledger, traces, embedder=None, min_samples=3)
        self.assertEqual("matched_not_injected", result["control"]["mode"])
        self.assertEqual(3, result["sampled"])
        self.assertEqual("no_lift", result["verdict"]["code"])
        self.assertIn("没有证据支持注入有效", result["verdict"]["headline"])

    # -- strata -------------------------------------------------------------
    def test_strata_group_by_agent_and_item_count(self):
        first = self._memory("见 hippomind/a1.py 的实现")
        self._reply("我读了 hippomind/a1.py")
        second = self._memory("见 hippomind/a2.py 的实现")
        self._reply("我读了 hippomind/a2.py", agent="claude", minutes=6)
        traces = [self._trace([first]), self._trace([second], agent="claude")]
        result = evaluate(self.ledger, traces, embedder=None)
        self.assertEqual(2, result["sampled"])
        self.assertIn("codex", result["strata"]["by_agent"])
        self.assertIn("claude", result["strata"]["by_agent"])
        self.assertIn("1", result["strata"]["by_items"])
        self.assertIn("codex-hook", result["strata"]["by_source"])

    def test_every_stratum_carries_its_own_sample_size(self):
        memory_id = self._memory("见 hippomind/strata.py")
        self._reply("我读了 hippomind/strata.py")
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        for dimension in result["strata"].values():
            for stats in dimension.values():
                self.assertIn("sampled", stats)

    # -- diagnostics --------------------------------------------------------
    # The distinction these tests protect: "the collector is broken" and "the
    # user stopped using this agent" look identical in the reply table but call
    # for opposite responses.  Only cursor evidence separates them.
    def _cursor(self, agent, *, offset, size, error="", when=None, name="aa"):
        """Write a transcript cursor with an explicit scan timestamp.

        set_cursor() always stamps updated_at=now, but these tests need the
        scan time to sit on a chosen side of the injection, so the row is
        written directly.
        """
        import json
        source_id = "transcript:%s:%s" % (agent, name)
        stamp = (when or self.now).isoformat()
        payload = json.dumps({"offset": offset, "size": size, "updated_at": stamp},
                             separators=(",", ":"))

        def write(conn):
            conn.execute(
                "INSERT INTO source_cursors(source_id,cursor,updated_at,last_success_at,"
                "last_error,metadata_json) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET cursor=excluded.cursor,"
                "updated_at=excluded.updated_at,last_error=excluded.last_error",
                (source_id, payload, stamp, stamp if not error else "", error,
                 json.dumps({"kind": "transcript"})))

        self.ledger._transaction(write)

    def test_idle_agent_is_not_reported_as_a_capture_failure(self):
        """Caught-up cursors + no new data == the agent simply was not used."""
        memory_id = self._memory("见 hippomind/idle.py")
        self._reply("这是注入之前的旧回复", minutes=-120)
        self._cursor("codex", offset=100, size=100, when=self.now + timedelta(hours=2))
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        entry = result["diagnostics"]["agents"]["codex"]
        self.assertEqual(0, entry["assistant_after_injection"])
        self.assertEqual("source_idle", entry["verdict"])
        self.assertEqual("current", entry["capture"]["status"])
        self.assertEqual(0, entry["capture"]["errors"])

    def test_cursor_error_is_reported_as_a_capture_failure(self):
        memory_id = self._memory("见 hippomind/stalled.py")
        self._reply("这是注入之前的旧回复", minutes=-120)
        self._cursor("codex", offset=100, size=100, error="decode failed",
                     when=self.now + timedelta(hours=2))
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        entry = result["diagnostics"]["agents"]["codex"]
        self.assertEqual("capture_stalled", entry["verdict"])
        self.assertEqual("stalled", entry["capture"]["status"])
        self.assertEqual(1, entry["capture"]["errors"])

    def test_partial_read_backlog_is_not_a_capture_failure(self):
        """transcripts.sync() reads a bounded batch per cycle: offset<size is normal."""
        memory_id = self._memory("见 hippomind/backlog.py")
        self._reply("这是注入之前的旧回复", minutes=-120)
        self._cursor("codex", offset=10, size=1000, when=self.now + timedelta(hours=2))
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        entry = result["diagnostics"]["agents"]["codex"]
        self.assertEqual(1, entry["capture"]["backlog"])
        self.assertEqual("current", entry["capture"]["status"])
        self.assertEqual("source_idle", entry["verdict"])

    def test_scan_older_than_injection_is_unverified_not_idle(self):
        memory_id = self._memory("见 hippomind/stale.py")
        self._reply("这是注入之前的旧回复", minutes=-120)
        self._cursor("codex", offset=100, size=100, when=self.now - timedelta(days=2))
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        entry = result["diagnostics"]["agents"]["codex"]
        self.assertEqual("capture_unverified", entry["verdict"])
        self.assertEqual("stale", entry["capture"]["status"])

    def test_agent_without_cursors_is_unknown_not_failure(self):
        memory_id = self._memory("见 hippomind/no_cursor.py")
        result = evaluate(self.ledger, [self._trace([memory_id], agent="ghost")],
                          embedder=None)
        entry = result["diagnostics"]["agents"]["ghost"]
        self.assertEqual("capture_unknown", entry["verdict"])
        self.assertEqual(0, entry["capture"]["cursors"])

    def test_replies_in_window_outrank_capture_state(self):
        memory_id = self._memory("见 hippomind/live.py")
        self._reply("我读了 hippomind/live.py", minutes=5)
        self._cursor("codex", offset=100, size=100, error="noise",
                     when=self.now + timedelta(hours=2))
        result = evaluate(self.ledger, [self._trace([memory_id])], embedder=None)
        entry = result["diagnostics"]["agents"]["codex"]
        self.assertEqual("replies_available", entry["verdict"])
        self.assertEqual(1, result["sampled"])

    # -- statistics ---------------------------------------------------------
    def test_wilson_interval_bounds(self):
        self.assertIsNone(wilson(0, 0))
        low, high = wilson(0, 10)
        self.assertEqual(0.0, low)
        self.assertGreater(high, 0.0)
        low, high = wilson(10, 10)
        self.assertEqual(1.0, high)
        self.assertLess(low, 1.0)
        # The interval must contain the point estimate.
        low, high = wilson(3, 10)
        self.assertLessEqual(low, 0.3)
        self.assertGreaterEqual(high, 0.3)

    # -- report() wiring ----------------------------------------------------
    def test_report_accepts_explicit_traces(self):
        memory_id = self._memory("见 hippomind/report_path.py")
        self._reply("我读了 hippomind/report_path.py")
        result = report(self.ledger, traces=[self._trace([memory_id])],
                        embedder=None, controls=False)
        self.assertEqual(1, result["traces"])
        self.assertEqual(1, result["sampled"])

    def test_report_reads_trace_file_when_traces_omitted(self):
        import os
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        trace_file = home / "injection.jsonl"
        trace_file.write_text(
            '{"at":"2026-10-24T04:02:05-0700","agent":"codex","items":1,'
            '"memory_ids":["mem_x"]}\n', encoding="utf-8")
        previous = os.environ.get("HIPPOMIND_HOME")
        os.environ["HIPPOMIND_HOME"] = str(home)
        try:
            result = report(self.ledger, embedder=None, controls=False)
        finally:
            if previous is None:
                os.environ.pop("HIPPOMIND_HOME", None)
            else:
                os.environ["HIPPOMIND_HOME"] = previous
        self.assertEqual(1, result["traces"])
        self.assertEqual(1, result["sample"]["skip_reasons"].get("memories_missing"))


class ReadOnlyLedgerTests(unittest.TestCase):
    """The reporting path must never be able to mutate production."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-injq-ro-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "hub.sqlite"
        ledger = SQLiteLedger(self.path)
        self.now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        self.memory_id = ledger.ingest({
            "content": "见 hippomind/readonly.py 的实现", "kind": "semantic",
            "source_agent": "codex", "scope": "shared", "evidence": ["codex:1"],
            "timestamp": self.now.isoformat(),
        }).memory_id
        ledger.ingest({
            "content": "我读了 hippomind/readonly.py", "kind": "raw_chat",
            "source_agent": "codex", "scope": "agent:codex", "raw_chat": True,
            "role": "assistant", "session_id": "s1", "evidence": ["codex:r"],
            "timestamp": (self.now + timedelta(minutes=2)).isoformat(),
        })
        ledger.close()

    def test_read_only_ledger_exposes_what_evaluate_needs(self):
        ledger = ReadOnlyLedger(self.path)
        self.addCleanup(ledger.close)
        memory = ledger.get_memory(self.memory_id)
        self.assertIsNotNone(memory)
        self.assertIsNone(ledger.get_memory("mem_missing"))
        result = evaluate(ledger, [{
            "at": self.now.isoformat(), "agent": "codex",
            "memory_ids": [self.memory_id]}], embedder=None, controls=False)
        self.assertEqual(1, result["sampled"])

    def test_read_only_ledger_rejects_writes(self):
        import sqlite3
        ledger = ReadOnlyLedger(self.path)
        self.addCleanup(ledger.close)
        with ledger._reading() as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM memories")

    def test_missing_ledger_file_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            ReadOnlyLedger(Path(self.tmp.name) / "absent.sqlite")


if __name__ == "__main__":
    unittest.main()
