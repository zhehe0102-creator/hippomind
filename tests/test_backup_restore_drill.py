"""Backup/restore drill: end-to-end guard for scripts/backup.sh + scripts/restore.sh.

The drill itself is a shell script; this test drives it as a subprocess so the
exact commands an operator/CI runs are exercised.  It also proves the drill is
hermetic: nothing may appear in the repository's own backups/ directory.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup_restore_drill.sh"
BACKUPS = ROOT / "backups"


def _repo_backups() -> list:
    if not BACKUPS.is_dir():
        return []
    return sorted(path.name for path in BACKUPS.iterdir())


class BackupRestoreDrillTests(unittest.TestCase):
    def test_drill_round_trips_and_refuses_tampering(self):
        self.assertTrue(SCRIPT.is_file(), "drill script is missing")
        before = _repo_backups()
        env = dict(os.environ)
        env.update({
            "PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(ROOT),
            "KEEP_LOGS": "0",
        })
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--json"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=240,
        )
        self.assertEqual(0, proc.returncode,
                         "drill failed\nstdout:\n%s\nstderr:\n%s"
                         % (proc.stdout[-2000:], proc.stderr[-4000:]))
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        payload = json.loads(lines[-1])
        self.assertTrue(payload["ok"])
        for check in ("create_ledger", "backup", "checksum", "restore",
                      "counts", "checkup", "tamper_refusal",
                      "tamper_ledger_untouched"):
            self.assertIn(check, payload["checks"])
        self.assertEqual("ok", payload["counts"]["integrity"])
        self.assertGreater(payload["counts"]["memories"], 0)
        self.assertEqual(before, _repo_backups(),
                         "drill must not write snapshots into repo backups/")

    def test_help_exits_zero(self):
        proc = subprocess.run(["bash", str(SCRIPT), "--help"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(0, proc.returncode)
        self.assertIn("backup_restore_drill", proc.stdout)

    def test_unknown_argument_exits_two(self):
        proc = subprocess.run(["bash", str(SCRIPT), "--nope"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(2, proc.returncode)


if __name__ == "__main__":
    unittest.main()

