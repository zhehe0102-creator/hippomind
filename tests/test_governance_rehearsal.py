"""WP-L governance rehearsal: frozen-copy safety and convergence.

The rehearsal tool must prove three things on every run: the input ledger is
only read (size/mtime/sha256 unchanged), all governance writes happen on the
frozen copy, and the frozen copy converges (no dangling claims, no missing
windows, duplicates collapsed).  apply must be idempotent.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import governance_rehearsal as rehearsal  # noqa: E402

TEST_ROWS = 240


class GovernanceRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-gov-reh-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.sqlite"
        self.synthetic = rehearsal.build_synthetic_ledger(self.source,
                                                          rows=TEST_ROWS)

    def _metrics(self, path):
        return rehearsal.collect_metrics(path)

    def _fingerprint(self, path):
        return rehearsal.file_fingerprint(path)

    # -- synthetic fixture -------------------------------------------------
    def test_synthetic_ledger_has_representative_dirty_shapes(self):
        metrics = self._metrics(self.source)
        self.assertEqual(TEST_ROWS, metrics["memories"])
        self.assertGreaterEqual(metrics["dangling_claims"], 8)
        self.assertGreaterEqual(metrics["missing_windows"], 8)
        self.assertGreaterEqual(metrics["duplicate_groups"], 6)
        self.assertGreaterEqual(metrics["duplicate_redundant"], 18)
        self.assertGreaterEqual(metrics["cold_rows"], 10)
        self.assertGreaterEqual(self.synthetic["stream_pairs"], 3)
        self.assertGreaterEqual(self.synthetic["backfill_pairs"], 8)

    # -- dry-run safety ----------------------------------------------------
    def test_dry_run_does_not_modify_source_or_retain_mutated_copy(self):
        workdir = self.root / "dry"
        before = self._fingerprint(self.source)
        result = rehearsal.rehearse(source=self.source, workdir=workdir,
                                    rows=TEST_ROWS)
        after = self._fingerprint(self.source)
        self.assertTrue(result["ok"], result["step_errors"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(before["sha256"], after["sha256"], "dry-run 不得修改源账本")
        self.assertEqual(before["mtime_ns"], after["mtime_ns"])
        self.assertEqual(before["size"], after["size"])
        self.assertTrue(result["source"]["unchanged"])
        self.assertTrue(result["frozen"]["unchanged"])
        # 治理步骤在一次性副本上真实执行，所以 after 展示了 apply 会达到的收敛状态
        self.assertGreater(result["before"]["dangling_claims"], 0)
        self.assertEqual(0, result["after"]["dangling_claims"])
        self.assertEqual(0, result["after"]["missing_windows"])
        self.assertEqual(0, result["after"]["duplicate_redundant"])
        self.assertGreater(result["after"]["reclaimable_bytes"],
                           result["before"]["reclaimable_bytes"])
        # simulation 已删除；冻结快照保留且字节未变
        self.assertTrue(result["copies"]["simulation_removed"])
        self.assertFalse(Path(result["copies"]["simulation"]).exists())
        self.assertFalse((workdir / "work.sqlite").exists())
        frozen = Path(result["frozen"]["path"])
        self.assertTrue(frozen.exists())
        self.assertEqual(result["frozen"]["fingerprint"]["sha256"],
                         self._fingerprint(frozen)["sha256"])

    def test_dry_run_does_not_touch_an_existing_target_copy(self):
        seeded = rehearsal.rehearse(source=self.source,
                                    workdir=self.root / "seed", apply=True,
                                    rows=TEST_ROWS)
        self.assertTrue(seeded["ok"], seeded["step_errors"])
        target = Path(seeded["copies"]["delivered"])
        fingerprint = self._fingerprint(target)
        result = rehearsal.rehearse(source=self.source, target=target,
                                    workdir=self.root / "dry-target",
                                    rows=TEST_ROWS)
        self.assertTrue(result["ok"], result["step_errors"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(fingerprint["sha256"],
                         self._fingerprint(target)["sha256"])
        self.assertTrue(result["copies"]["target_unchanged"])
        self.assertTrue(result["copies"]["simulation_removed"])
        # 已有副本是 before-state：它已经是收敛态，不是脏源账本
        self.assertEqual(0, result["before"]["dangling_claims"])

    # -- apply safety and convergence --------------------------------------
    def test_apply_writes_only_the_frozen_copy_and_converges(self):
        workdir = self.root / "apply"
        source_before = self._fingerprint(self.source)
        result = rehearsal.rehearse(source=self.source, workdir=workdir,
                                    apply=True, rows=TEST_ROWS)
        source_after = self._fingerprint(self.source)
        self.assertTrue(result["ok"], result["step_errors"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(source_before["sha256"], source_after["sha256"],
                         "apply 不得写源账本")
        self.assertEqual(source_before["mtime_ns"], source_after["mtime_ns"])
        self.assertTrue(result["source"]["unchanged"])
        self.assertTrue(result["frozen"]["unchanged"])
        delivered = Path(result["copies"]["delivered"])
        self.assertTrue(delivered.exists())
        self.assertTrue(result["copies"]["delivered_changed"])
        self.assertGreater(result["before"]["dangling_claims"], 0)
        converged = self._metrics(delivered)
        self.assertEqual(0, converged["dangling_claims"])
        self.assertEqual(0, converged["missing_windows"])
        self.assertEqual(0, converged["missing_memory_windows"])
        self.assertEqual(0, converged["duplicate_redundant"])
        self.assertEqual(converged["fts_rows"], converged["fts_expected"])

    def test_second_apply_is_idempotent(self):
        first = rehearsal.rehearse(source=self.source,
                                   workdir=self.root / "idem", apply=True,
                                   rows=TEST_ROWS)
        self.assertTrue(first["ok"], first["step_errors"])
        target = Path(first["copies"]["delivered"])
        second = rehearsal.rehearse(source=self.source, target=target,
                                    workdir=self.root / "idem2", apply=True,
                                    rows=TEST_ROWS)
        self.assertTrue(second["ok"], second["step_errors"])
        self.assertEqual(0, second["before"]["dangling_claims"])
        self.assertEqual(0, second["before"]["missing_windows"])
        self.assertEqual(0, second["before"]["duplicate_redundant"])
        for key in ("dangling_claims", "missing_windows",
                    "duplicate_redundant", "active_claims", "fts_rows",
                    "reclaimable_bytes"):
            self.assertEqual(0, int(second["delta"].get(key) or 0),
                             "重复 apply 不得产生新的变化: %s" % key)
        details = {step["name"]: step["detail"] for step in second["steps"]}
        self.assertEqual(0, int(details["repair_claim_states"]["superseded"]))
        self.assertEqual(0, int(details["backfill_valid_until"]["claims_updated"]))
        self.assertEqual(0, int(details["collapse_duplicate_rows"]["collapsed"]))
        self.assertTrue(second["source"]["unchanged"])

    # -- refusals ----------------------------------------------------------
    def test_source_validation_rejects_missing_and_non_sqlite_paths(self):
        missing = self.root / "missing.sqlite"
        self.assertEqual(2, rehearsal.main(["--source", str(missing), "--json"]))
        plain = self.root / "plain.txt"
        plain.write_text("not a database", encoding="utf-8")
        self.assertEqual(2, rehearsal.main(["--source", str(plain), "--json"]))

    def test_apply_refuses_the_source_literal_path(self):
        # target 与 source 同一文件：apply 只能作用于副本
        self.assertEqual(2, rehearsal.main(
            ["--source", str(self.source), "--target", str(self.source),
             "--apply", "--json"]))
        # 只给 target（无法证明它是副本）也拒绝
        self.assertEqual(2, rehearsal.main(
            ["--target", str(self.source), "--apply", "--json"]))

    # -- CLI ---------------------------------------------------------------
    def test_cli_json_report_round_trips(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = rehearsal.main(["--source", str(self.source),
                                   "--rows", str(TEST_ROWS),
                                   "--workdir", str(self.root / "cli"),
                                   "--json"])
        self.assertEqual(0, code)
        payload = json.loads(buffer.getvalue())
        for key in ("schema", "dry_run", "source", "frozen", "copies",
                    "before", "after", "delta", "steps",
                    "reclaimable_bytes", "elapsed_ms"):
            self.assertIn(key, payload)
        self.assertTrue(payload["source"]["unchanged"])
        self.assertEqual("frozen-source", payload["mode"])
        self.assertIsNone(payload["synthetic"])
        self.assertTrue(all(step["ok"] for step in payload["steps"]))
        self.assertEqual("repair_claim_states", payload["steps"][0]["name"])


if __name__ == "__main__":
    unittest.main()
