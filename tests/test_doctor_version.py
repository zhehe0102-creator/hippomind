"""``hippomind doctor`` must not claim a version the rest of the CLI denies.

The drift this file exists for
------------------------------
``cmd_doctor`` hard-coded its own report version as ``"0.9.2"`` -- in the JSON
payload and in the human banner -- while the package, the daemon and
``hippomind version`` all said 1.0.0.  ``doctor`` is the command people paste
into an issue when something is wrong, so a stale literal there is not cosmetic:
it points support at a release that no longer matches the code, and it is the
exact failure mode the version single-source in ``hippomind/daemon.py``
(``SERVER_VERSION = __version__``, "a release only has to bump one place") was
written to prevent -- one place had simply been missed.

The fix routes doctor through ``SERVER_VERSION``.  What is pinned here is the
agreement, not the number: a release bump must never need an edit in this file.
Same class of guard as tests/test_doc_claims.py, which keeps the *docs* honest
about counts; this keeps the tool's self-reporting honest.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hippomind.cli import SERVER_VERSION, main


class DoctorVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-doctor-ver-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = ["--ledger", str(self.root / "h.sqlite"),
                     "--runtime-dir", str(self.root / "rt"), "--port", "1"]

    def _json(self, *argv: str) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            main(list(argv) + self.base)
        return json.loads(out.getvalue())

    def _text(self, *argv: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            main(list(argv) + self.base)
        return out.getvalue()

    def test_doctor_json_version_matches_the_package(self):
        """One source: bump the package and both reports move together."""
        import hippomind

        payload = self._json("doctor", "--format", "json")
        self.assertEqual(payload["version"], SERVER_VERSION)
        self.assertEqual(payload["version"], hippomind.__version__)

    def test_doctor_agrees_with_the_version_command(self):
        """The two commands an operator compares must never disagree."""
        doctor = self._json("doctor", "--format", "json")["version"]
        reported = self._json("version")
        self.assertEqual(doctor, reported["server"])
        self.assertEqual(doctor, reported["package"])

    def test_doctor_banner_uses_the_same_version(self):
        banner = self._text("doctor").splitlines()[0]
        self.assertEqual(banner, f"HippoMind doctor v{SERVER_VERSION}")

    def test_no_stale_literal_survives(self):
        """The bug was a hard-coded 0.9.2; neither output form may show one."""
        self.assertNotIn("0.9.2", self._json("doctor", "--format", "json")["version"])
        self.assertNotIn("0.9.2", self._text("doctor"))

    def test_doctor_still_reports_every_check(self):
        """Routing the version through the constant changed nothing else."""
        payload = self._json("doctor", "--format", "json")
        self.assertEqual([c["id"] for c in payload["checks"]],
                         ["ledger", "daemon", "token", "injection", "privacy"])
        self.assertIn("ready", payload)


if __name__ == "__main__":
    unittest.main()
