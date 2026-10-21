"""ingest/feedback 离线直写回退（前景轮二）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    """Bind port 0 and use what the kernel hands back.

    These tests assert the *offline-direct* fallback, which only happens when no
    daemon answers.  Without ``--port`` the CLI resolves the port from runtime
    state and then falls back to the global default 8765, so the test silently
    talked to whatever daemon happened to own that port — a real one serving the
    live ledger answered ``401 local authorization token required`` and the test
    failed for a reason that had nothing to do with the code under test.  A
    dedicated free port makes "nothing is listening" true by construction.
    """
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class OfflineFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hippo-off-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)],
                                               check=False))
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        self.ledger_path = self.tmp / "d.sqlite"
        SQLiteLedger(self.ledger_path)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "ingest" if args[0] != "fb"
             else "feedback",
             "--ledger", str(self.ledger_path),
             "--runtime-dir", str(self.tmp / "rt"),
             "--port", str(_free_port()), *args[1:]],
            capture_output=True, text=True, cwd=str(ROOT))

    def test_ingest_offline_direct_marks_mode(self):
        r = self._run("ingest", "--agent", "t", "--evidence", "x:1",
                      "--content", "离线直写探针")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out.get("mode"), "offline-direct")
        self.assertTrue(out["results"][0]["inserted"])

    def test_feedback_offline_direct(self):
        r = self._run("ingest", "--agent", "t", "--evidence", "x:1",
                      "--content", "反馈探针")
        mid = json.loads(r.stdout)["results"][0]["memory_id"]
        r = self._run("fb", mid, "1.0")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["updated"])
        self.assertEqual(out.get("mode"), "offline-direct")

    def test_memory_retrievable_after_offline_write(self):
        self._run("ingest", "--agent", "t", "--evidence", "x:1",
                  "--content", "落库可见性探针")
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        hits = SQLiteLedger(self.ledger_path).search("落库可见性探针",
                                                     scope="shared", limit=5)
        self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()
