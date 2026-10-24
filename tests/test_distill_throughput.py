"""Distillation throughput knobs: content dedupe, batch size, breaker safety.

The engine used to send one batch of 8 rows to the model and pay a call per
row.  Two things were wrong with that: the batch was small, and most pending
rows are *repeats* (raw chat re-captured across sessions), so the same content
was paid for many times over.

These tests pin the invariants of the replacement:

* identical content costs one model call, but every row still gets a distilled
  record and a cursor -- provenance and completeness are not traded for speed;
* batch size is configurable, bounded, and defaults to the measured optimum;
* a bigger batch does not weaken the circuit breaker: a dead API still cools
  down and the deterministic fallback still runs.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import distill  # noqa: E402
from hippomind.distill import (  # noqa: E402
    CURSOR_PREFIX,
    DEFAULT_BATCH_SIZE,
    DistillEngine,
    MAX_BATCH,
    default_batch_size,
    default_run_limit,
)
from hippomind.ledger import SQLiteLedger  # noqa: E402


class RecordingExtractor:
    """Fake model that records how many calls it saw and with what."""

    name = "fake"
    available = True

    def __init__(self):
        self.batches = []

    def extract(self, candidates):
        self.batches.append([c.memory_id for c in candidates])
        return [{"subject": "s", "predicate": "p",
                 "slot_key": "k_" + candidates[0].memory_id,
                 "value": "v_" + candidates[0].memory_id,
                 "confidence": 0.9, "source": 1}]


class ThroughputKnobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-distill-tp-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self._seq = 0

    def _ingest(self, content, **extra):
        self._seq += 1
        record = {"content": content, "kind": "raw_chat", "source_agent": "claude",
                  "scope": "shared", "raw_chat": True,
                  "evidence": ["claude:%d" % self._seq]}
        record.update(extra)
        return self.ledger.ingest(record)

    def _cursors(self):
        with self.ledger._reading() as conn:
            return {row["source_id"]: row["metadata_json"] for row in conn.execute(
                "SELECT source_id, metadata_json FROM source_cursors WHERE source_id LIKE ?",
                (CURSOR_PREFIX + "%",))}

    # -- configuration -----------------------------------------------------

    def test_defaults_are_the_measured_values(self):
        # Batch stays at 8: the matched experiment showed a batch of 16 drops
        # ~11% of the items a batch of 8 would have distilled.  The speedup this
        # module ships comes from dedupe + per-pass limit, not from the batch.
        self.assertEqual(8, DEFAULT_BATCH_SIZE)
        self.assertEqual(8, default_batch_size())
        self.assertEqual(64, default_run_limit())
        self.assertGreaterEqual(MAX_BATCH, DEFAULT_BATCH_SIZE)

    def test_batch_size_can_still_be_raised_explicitly(self):
        """16 remains available for operators who accept the coverage trade."""
        with mock.patch.dict("os.environ", {"HIPPOMIND_DISTILL_BATCH": "16"}):
            self.assertEqual(16, default_batch_size())

    def test_environment_overrides_batch_and_limit(self):
        with mock.patch.dict("os.environ", {"HIPPOMIND_DISTILL_BATCH": "24",
                                            "HIPPOMIND_DISTILL_LIMIT": "32"}):
            self.assertEqual(24, default_batch_size())
            self.assertEqual(32, default_run_limit())

    def test_out_of_range_environment_falls_back_to_default(self):
        """A typo must not silently produce a batch the model cannot handle."""
        for value in ("0", "-4", "9999", "abc", ""):
            with mock.patch.dict("os.environ", {"HIPPOMIND_DISTILL_BATCH": value}):
                self.assertEqual(DEFAULT_BATCH_SIZE, default_batch_size(),
                                 "value %r should fall back" % value)

    def test_explicit_batch_size_is_clamped(self):
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        for index in range(4):
            self._ingest("这是一段足够长的待提炼内容编号 %d" % index, session_id="s-%d" % index)
        engine.run(limit=4, batch_size=1)
        self.assertEqual(4, len(extractor.batches))
        self.assertTrue(all(len(batch) == 1 for batch in extractor.batches))

    def test_batch_size_groups_the_representatives(self):
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        for index in range(5):
            self._ingest("这是一段足够长的待提炼内容编号 %d" % index, session_id="s-%d" % index)
        engine.run(limit=5, batch_size=3)
        self.assertEqual([3, 2], [len(batch) for batch in extractor.batches])

    # -- content dedupe ----------------------------------------------------

    def test_identical_content_costs_one_model_call(self):
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        for index in range(4):
            # Different sessions, identical body: the ledger stores 4 rows.
            self._ingest("这是一段完全相同的待提炼内容，用于验证去重", session_id="s-%d" % index)
        result = engine.run(limit=10, batch_size=8)
        self.assertEqual(4, result["candidates"])
        self.assertEqual(1, result["distinct"])
        self.assertEqual(3, result["duplicates"])
        # One call, one representative -- not four.
        self.assertEqual(1, len(extractor.batches))
        self.assertEqual(1, len(extractor.batches[0]))

    def test_every_duplicate_row_still_gets_a_cursor_and_a_record(self):
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        body = "这是一段重复出现的待提炼内容，用于验证每一行都被标记"
        memory_ids = [self._ingest(body, session_id="s-%d" % index).memory_id
                      for index in range(3)]
        engine.run(limit=10, batch_size=8)
        cursors = self._cursors()
        for memory_id in memory_ids:
            self.assertIn(CURSOR_PREFIX + memory_id, cursors,
                          "每一行都必须被标记，否则会永远留在队列里")
        # Each row also produced its own distilled memory with its own evidence.
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT evidence_json FROM memories WHERE source_agent='hippomind-distill'"
            ).fetchall()
        linked = {item for row in rows for item in
                  __import__("json").loads(row["evidence_json"] or "[]")}
        for memory_id in memory_ids:
            self.assertIn("memory:" + memory_id, linked)

    def test_duplicate_cursor_records_which_row_answered(self):
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        body = "这是一段相同内容，用于验证 dedupe_of 指向哪一行"
        ids = [self._ingest(body, session_id="s-%d" % index).memory_id
               for index in range(2)]
        engine.run(limit=10, batch_size=8)
        cursors = self._cursors()
        import json
        marked = [json.loads(cursors[CURSOR_PREFIX + mid]) for mid in ids]
        dedupe_of = [item.get("dedupe_of") for item in marked if item.get("dedupe_of")]
        self.assertEqual(1, len(dedupe_of), "重复行必须记录它复用了哪一行")
        self.assertIn(dedupe_of[0], ids)

    def test_dedupe_can_be_disabled(self):
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        for index in range(3):
            self._ingest("这是一段相同内容，用于验证 dedupe_of 指向哪一行", session_id="s-%d" % index)
        result = engine.run(limit=10, batch_size=8, dedupe=False)
        self.assertEqual(3, result["distinct"])
        self.assertEqual(0, result["duplicates"])

    def test_dedupe_keeps_distinct_content_separate(self):
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        self._ingest("这是内容甲，一段足够长的待提炼文本", session_id="s-1")
        self._ingest("这是内容乙，另一段足够长的待提炼文本", session_id="s-2")
        result = engine.run(limit=10, batch_size=8)
        self.assertEqual(2, result["distinct"])
        self.assertEqual(0, result["duplicates"])

    def test_empty_content_never_collapses_together(self):
        """Two blank bodies are not "the same fact"; they must not share a call."""
        groups = DistillEngine._dedupe_groups([
            distill.Candidate("m1", "raw_chat", "", "a", "", "", "", "", {}),
            distill.Candidate("m2", "raw_chat", "", "b", "", "", "", "", {}),
        ])
        self.assertEqual(2, len(groups))

    def test_run_reports_duplicates_and_distinct(self):
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        for index in range(3):
            self._ingest("这是一段重复的待提炼内容", session_id="s-%d" % index)
        self._ingest("这是一段独立不同的待提炼内容", session_id="s-x")
        result = engine.run(limit=10, batch_size=8)
        self.assertEqual(4, result["candidates"])
        self.assertEqual(2, result["distinct"])
        self.assertEqual(2, result["duplicates"])
        self.assertEqual(8, result["batch_size"])

    def test_candidates_counts_rows_handled_not_the_fetch_window(self):
        """Dedupe over-fetches; the reported candidate count must not inflate.

        Any throughput number divided out of "candidates" would be ~4x too high
        if this reported the fetch window instead of the rows the pass handles.
        """
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        for index in range(4):
            self._ingest("这是一段重复的待提炼内容", session_id="s-%d" % index)
        result = engine.run(limit=4, batch_size=8)
        self.assertEqual(4, result["candidates"], "4 行被处理，不是 16 行窗口")
        self.assertEqual(1, result["distinct"])
        self.assertEqual(3, result["duplicates"])
        self.assertGreaterEqual(result["fetched"], result["candidates"],
                                "fetched 是原始抓取窗口，应 >= 实际处理行数")

    def test_second_run_finds_nothing_left(self):
        extractor = RecordingExtractor()
        engine = DistillEngine(self.ledger, extractor=extractor)
        body = "这是一段重复出现的待提炼内容，用于验证每一行都被标记"
        for index in range(3):
            self._ingest(body, session_id="s-%d" % index)
        first = engine.run(limit=10, batch_size=8)
        self.assertEqual(3, first["candidates"])
        second = engine.run(limit=10, batch_size=8)
        self.assertEqual(0, second["candidates"])

    # -- circuit breaker survives the bigger batch -------------------------

    def test_breaker_still_opens_with_a_large_batch(self):
        extractor = distill.ClaudeExtractor(timeout=1)
        extractor.binary = "/bin/false"
        candidates = [distill.Candidate("m%d" % i, "task", "内容", "codex",
                                        "", "", "", "", {}) for i in range(16)]
        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            self.assertIsNone(extractor.extract(candidates))
            self.assertTrue(extractor.available)
            self.assertIsNone(extractor.extract(candidates))
        self.assertFalse(extractor.available)
        self.assertIn("OSError", extractor.last_error)

    def test_open_breaker_falls_back_to_heuristic_without_stalling(self):
        """A dead API must still make progress, not burn the whole pass."""
        dead = distill.ClaudeExtractor(timeout=1)
        dead.binary = "/bin/false"
        dead._cooldown_until = 10 ** 12          # already open
        engine = DistillEngine(self.ledger, extractor=dead)
        self._ingest("决定采用 SQLite 作为存储，因为需要 WAL 并发。", session_id="s-1")
        result = engine.run(limit=4, batch_size=8)
        self.assertEqual("heuristic", result["extractor"])
        self.assertEqual(0, result["errors"])


if __name__ == "__main__":
    unittest.main()
