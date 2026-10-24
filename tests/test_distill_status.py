"""Distillation backlog/throughput reporting (scripts/distill_status.py).

Every test builds its own temporary ledger, so the numbers are asserted against
data the test controls rather than against production.  The point of the module
under test is that a specific miscount (treating archived cold rows as pending
work) must be impossible to reproduce silently, so that is the invariant these
tests defend.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import distill_status  # noqa: E402
from hippomind.distill import CURSOR_PREFIX  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402


_EVIDENCE_SEQ = [0]


def _ingest(ledger, content, **extra):
    # Evidence ids must be unique per row; deriving them from the content would
    # collide for the whitespace-variant fixtures that test dedupe.
    _EVIDENCE_SEQ[0] += 1
    record = {
        "content": content,
        "kind": "raw_chat",
        "source_agent": "claude",
        "scope": "shared",
        "raw_chat": True,
        "evidence": ["claude:%d" % _EVIDENCE_SEQ[0]],
    }
    record.update(extra)
    return ledger.ingest(record)


class StatusReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-distill-status-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _reader(self):
        return distill_status.ReadOnlyLedger(self.ledger.path)

    def _set_lifecycle(self, memory_id, lifecycle, quality_state="accepted"):
        def write(conn):
            conn.execute("UPDATE memories SET lifecycle=?, quality_state=? WHERE memory_id=?",
                         (lifecycle, quality_state, memory_id))
        self.ledger._transaction(write)

    # -- the core invariant ------------------------------------------------

    def test_cold_rows_are_not_counted_as_pending(self):
        """The whole point: archived cold rows must never inflate the backlog."""
        hot = _ingest(self.ledger, "活跃的会话内容，需要提炼")
        cold = _ingest(self.ledger, "已经归档的旧会话内容")
        self._set_lifecycle(cold.memory_id, "cold")
        with self._reader() as reader:
            result = distill_status.distill_status(reader, hours=6)
        self.assertEqual(1, result["pending"]["rows"])
        self.assertEqual(1, result["pending"]["raw_rows"])
        self.assertGreaterEqual(result["cold_rows"], 1)
        # ...and the cold row is still visible in the distribution, not hidden.
        cold_buckets = [b for b in result["distribution"] if b["lifecycle"] == "cold"]
        self.assertTrue(cold_buckets)
        self.assertGreaterEqual(sum(b["rows"] for b in cold_buckets), 1)
        self.assertTrue(hot.memory_id)

    def test_pending_excludes_distilled_and_quarantined_and_derived(self):
        pending = _ingest(self.ledger, "待提炼的内容 A")
        done = _ingest(self.ledger, "已经提炼过的内容 B")
        quarantined = _ingest(self.ledger, "质量不达标的内容 C")
        derived = _ingest(self.ledger, "提炼器自己产出的内容 D",
                          source_agent="hippomind-distill")
        self.ledger.set_cursor(CURSOR_PREFIX + done.memory_id, "1", metadata={"claims": 1})
        self._set_lifecycle(quarantined.memory_id, "active", quality_state="quarantine")
        with self._reader() as reader:
            result = distill_status.distill_status(reader, hours=6)
        self.assertEqual(1, result["pending"]["rows"])
        self.assertEqual(1, result["progress"]["cursors"])
        self.assertTrue(pending.memory_id)
        self.assertTrue(derived.memory_id)

    # -- distinct-content accounting ---------------------------------------

    def test_distinct_content_collapses_whitespace_variants(self):
        """Rows differing only by captured whitespace are one model call.

        The two variants live in different sessions on purpose: the ledger's own
        identity key already collapses same-session repeats at ingest time, so a
        same-session fixture would test ingest dedupe instead of distill dedupe.
        """
        _ingest(self.ledger, "同一段  内容\n换行", session_id="s-a")
        _ingest(self.ledger, "同一段 内容 换行", session_id="s-b")
        _ingest(self.ledger, "完全不同的一段内容", session_id="s-c")
        with self._reader() as reader:
            result = distill_status.distill_status(reader, hours=6)
        self.assertEqual(3, result["pending"]["rows"])
        self.assertEqual(2, result["pending"]["distinct"])
        self.assertLessEqual(result["pending"]["distinct"], result["pending"]["rows"])

    def test_distinct_is_split_by_raw_and_structured(self):
        _ingest(self.ledger, "raw 内容一")
        _ingest(self.ledger, "raw 内容二")
        self.ledger.ingest({"content": "结构化内容一", "kind": "task",
                            "source_agent": "codex", "scope": "shared",
                            "evidence": ["codex:1"]})
        with self._reader() as reader:
            result = distill_status.distill_status(reader, hours=6)
        self.assertEqual(2, result["pending"]["raw_distinct"])
        self.assertEqual(1, result["pending"]["structured_distinct"])
        self.assertEqual(3, result["pending"]["distinct"])

    def test_scan_limit_truncation_is_reported_not_hidden(self):
        for index in range(5):
            _ingest(self.ledger, "内容编号 %d" % index)
        with self._reader() as reader:
            result = distill_status.distill_status(reader, hours=6, scan_limit=2)
        self.assertTrue(result["pending"]["scan_truncated"])
        self.assertEqual(2, result["pending"]["scanned"])
        self.assertEqual(5, result["pending"]["rows"])

    # -- throughput --------------------------------------------------------

    def test_throughput_is_computed_from_cursor_timestamps(self):
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        ids = []
        for index in range(4):
            memory = _ingest(self.ledger, "游标样本 %d" % index)
            ids.append(memory.memory_id)
            self.ledger.set_cursor(CURSOR_PREFIX + memory.memory_id, "1",
                                   metadata={"claims": 0})
        # Rewrite each cursor's timestamp individually onto a known 1.5h span.
        def write(conn):
            for offset, memory_id in enumerate(ids):
                stamp = (now - timedelta(hours=2) + timedelta(minutes=30 * offset)).isoformat()
                conn.execute("UPDATE source_cursors SET updated_at=? WHERE source_id=?",
                             (stamp, CURSOR_PREFIX + memory_id))
        self.ledger._transaction(write)
        with self._reader() as reader:
            rate = distill_status.throughput(reader, hours=6, now=now)
        self.assertEqual(4, rate["samples"])
        self.assertAlmostEqual(1.5, rate["span_hours"], places=2)
        self.assertAlmostEqual(4 / 1.5, rate["rows_per_hour"], places=1)

    def test_throughput_without_samples_says_so(self):
        with self._reader() as reader:
            rate = distill_status.throughput(reader, hours=6)
        self.assertIsNone(rate["rows_per_hour"])
        self.assertIn("note", rate)

    def test_drain_estimate_reports_calls_and_passes(self):
        pending = {"rows": 100, "distinct": 40}
        drain = distill_status.drain_estimate(pending, 20.0, batch_size=16, limit=64)
        self.assertEqual(5.0, drain["hours_at_measured_rate"])
        self.assertEqual(40, drain["model_calls_needed"])
        # 64 rows per pass / batch 16 => 4 calls per pass
        self.assertEqual(4, drain["calls_per_run"])
        self.assertEqual(1, drain["runs_needed"])
        self.assertEqual(64, drain["rows_per_run"])

    def test_drain_estimate_without_rate_returns_none(self):
        drain = distill_status.drain_estimate({"rows": 10, "distinct": 3}, None,
                                              batch_size=16, limit=64)
        self.assertIsNone(drain["hours_at_measured_rate"])
        self.assertIsNone(drain["days_at_measured_rate"])

    # -- rendering / CLI ---------------------------------------------------

    def test_render_mentions_cold_is_not_backlog(self):
        _ingest(self.ledger, "一条待提炼内容")
        with self._reader() as reader:
            result = distill_status.distill_status(reader, hours=6)
        text = distill_status.render(result)
        self.assertIn("cold", text)
        self.assertIn("不是积压", text)
        self.assertIn("真正剩余待提炼", text)
        self.assertIn("按内容去重后", text)

    def test_main_json_round_trips(self):
        _ingest(self.ledger, "一条待提炼内容")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = distill_status.main(["--ledger", str(self.ledger.path), "--json"])
        self.assertEqual(0, code)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(1, payload["pending"]["rows"])
        for key in ("distribution", "active", "progress", "pending", "throughput",
                    "drain", "cold_rows", "memories_total"):
            self.assertIn(key, payload)

    def test_main_missing_ledger_returns_2(self):
        missing = Path(self.tmp.name) / "nope.sqlite"
        with redirect_stdout(io.StringIO()):
            code = distill_status.main(["--ledger", str(missing), "--json"])
        self.assertEqual(2, code)

    def test_reader_is_read_only(self):
        _ingest(self.ledger, "内容")
        with self._reader() as reader:
            with self.assertRaises(Exception):
                reader._conn.execute("DELETE FROM memories")


if __name__ == "__main__":
    unittest.main()
