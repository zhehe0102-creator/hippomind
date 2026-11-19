"""A refusing daemon must not be silently bypassed by a direct master write.

The defect this file exists for
-------------------------------
``cmd_ingest``, ``cmd_feedback`` and ``cmd_sync`` all meant to fall back to a
local write *only when the daemon is unreachable*. They wrote it as::

    try:
        _health(paths, port)
        raise exc
    except LedgerError:      # <-- catches the LedgerError raised one line above
        pass

so the guard could never fire: the re-raised daemon error is itself a
``LedgerError`` and was swallowed by the very clause that followed it. With a
healthy daemon running in read-only mode (or any credential lacking the
permission), the CLI turned the daemon's deliberate 403 into a direct write on
the master ledger and reported ``"mode": "offline-direct"`` -- an agent that
the daemon refused could still write, and it did so outside the daemon's
single-writer serialisation.

``cmd_sync`` was worse: it had no reachability probe at all, so *any* daemon
error -- including a policy refusal -- dropped straight into a local
``sync_once()`` write.

The repair is a probe that never raises (:func:`hippomind.cli._daemon_reachable`)
and a bare ``raise`` outside any second ``try``, so the control flow cannot
swallow itself. What is pinned here:

  * with a reachable daemon that refuses the write, ``ingest``/``feedback``/
    ``sync`` exit non-zero, surface the daemon's own refusal text, and leave the
    master ledger byte-for-byte unchanged (no ``offline-direct`` success path);
  * a restricted ``--agent-config`` gets the same refusal rather than a direct
    write, and is still refused outright when the daemon is genuinely down;
  * the *legitimate* fallback still works: with no daemon listening, ``sync``
    imports from the archive locally and ``ingest`` writes offline.

The daemon under test is the in-process HTTP server the existing read-only
tests use, on an ephemeral port: real policy decisions, no spawned process and
therefore nothing to reap.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.cli import _daemon_reachable, main
from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths
from hippomind.agent_config import write_agent_config
from hippomind.ledger import SQLiteLedger


class RefusingDaemonMixin(unittest.TestCase):
    """A healthy read-only daemon: reachable, and it refuses every write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-refuse-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = RuntimePaths.for_ledger(self.root / "h.sqlite", self.root / "rt")
        # Read-only mode now requires an existing, initialised ledger snapshot:
        # the served daemon is RO and never bootstraps or migrates the file.
        SQLiteLedger(self.paths.ledger_path).close()
        self.service = MemoryHubService(self.paths, watch=False, read_only=True)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self._wait_health()

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _wait_health(self) -> None:
        deadline = 50
        for _ in range(deadline):
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/health" % self.port, timeout=1) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            threading.Event().wait(0.05)
        self.fail("read-only daemon never became reachable")

    def base_args(self, port: int = None) -> list:
        return ["--ledger", str(self.paths.ledger_path),
                "--runtime-dir", str(self.paths.state_path.parent),
                "--port", str(self.port if port is None else port)]

    def seed(self, text: str = "已存在的记忆") -> str:
        """Pre-load data through a writable handle: the served daemon is RO."""
        writer = SQLiteLedger(self.paths.ledger_path)
        try:
            return writer.ingest({
                "content": text, "kind": "fact", "source_agent": "t",
                "evidence": ["t:seed"]}).memory_id
        finally:
            writer.close()

    def rows(self) -> int:
        return int(SQLiteLedger(self.paths.ledger_path).stats().get("memories", 0))

    def run_cli(self, *argv: str, port: int = None) -> tuple[int, str, str]:
        """Run the CLI in-process. ``port=`` aims it at a different endpoint."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv) + self.base_args(port))
        return code, out.getvalue(), err.getvalue()


class ProbeTests(RefusingDaemonMixin):
    def test_probe_reports_a_live_daemon(self):
        self.assertTrue(_daemon_reachable(self.paths, self.port))

    def test_probe_reports_no_daemon_and_never_raises(self):
        # an unused port: no listener at all
        with sqlite3.connect(":memory:") as conn:
            conn.execute("SELECT 1")
        self.assertFalse(_daemon_reachable(self.paths, 1))

    def test_probe_ignores_a_daemon_serving_another_ledger(self):
        """Reachability is about *presence*; ledger ownership is checked where
        it matters (stop/restart), not by this probe."""
        self.assertTrue(_daemon_reachable(
            RuntimePaths.for_ledger(self.root / "other.sqlite", self.root / "rt2"),
            self.port))


class IngestRefusalTests(RefusingDaemonMixin):
    def test_refused_ingest_exits_nonzero_and_writes_nothing(self):
        self.seed()
        before = self.rows()
        code, out, err = self.run_cli("ingest", "--content", "越权写入尝试",
                                      "--agent", "t", "--evidence", "t:1")
        self.assertEqual(code, 2)
        self.assertIn("没有 ingest 权限", err)
        self.assertNotIn("offline-direct", out)
        self.assertEqual(self.rows(), before, "被 daemon 拒绝的写入绝不可落主账本")


class FeedbackRefusalTests(RefusingDaemonMixin):
    def test_refused_feedback_exits_nonzero_and_writes_nothing(self):
        memory_id = self.seed()
        code, out, err = self.run_cli("feedback", memory_id, "1.0")
        self.assertEqual(code, 2)
        self.assertIn("没有 feedback 权限", err)
        self.assertNotIn("offline-direct", out)

    def test_refused_feedback_leaves_weight_untouched(self):
        memory_id = self.seed()
        with self.service.ledger._reading() as conn:
            before = conn.execute("SELECT weight FROM memories WHERE memory_id=?",
                                  (memory_id,)).fetchone()["weight"]
        self.run_cli("feedback", memory_id, "5.0")
        with self.service.ledger._reading() as conn:
            after = conn.execute("SELECT weight FROM memories WHERE memory_id=?",
                                 (memory_id,)).fetchone()["weight"]
        self.assertEqual(after, before)


class SyncRefusalTests(RefusingDaemonMixin):
    def test_refused_sync_exits_nonzero_instead_of_syncing_locally(self):
        archive = self.root / "archive"
        (archive / "index").mkdir(parents=True)
        db = archive / "index" / "index.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE messages (agent TEXT, session_id TEXT, "
                         "event_id TEXT, ts TEXT, role TEXT, content TEXT, model TEXT, "
                         "cwd TEXT, title TEXT, source_file TEXT)")
            conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("codex", "c1", "chat-1", "2026-08-16T00:00:00Z", "assistant",
                          "归档里的一条记忆", "m", "/p", "t", "c.json"))
        before = self.rows()
        code, out, err = self.run_cli("sync", "--archive", str(archive))
        self.assertEqual(code, 2)
        self.assertIn("没有 sync 权限", err)
        self.assertEqual(self.rows(), before,
                         "daemon 在线并拒绝时，sync 不得本地直写")

    def test_legitimate_offline_sync_still_imports(self):
        """No daemon at all -> the documented local fallback must still work."""
        archive = self.root / "archive2"
        (archive / "index").mkdir(parents=True)
        with sqlite3.connect(archive / "index" / "index.sqlite") as conn:
            conn.execute("CREATE TABLE messages (agent TEXT, session_id TEXT, "
                         "event_id TEXT, ts TEXT, role TEXT, content TEXT, model TEXT, "
                         "cwd TEXT, title TEXT, source_file TEXT)")
            conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("codex", "c2", "chat-2", "2026-08-16T00:00:00Z", "assistant",
                          "离线同步进来的一条", "m", "/p", "t", "c.json"))
        before = self.rows()
        code, out, err = self.run_cli("sync", "--archive", str(archive), port=1)
        self.assertEqual(code, 0, err)
        self.assertGreater(self.rows(), before,
                           "daemon 不可达时 sync 的本地降级必须照旧可用")


class LegitimateFallbackStillWorksTests(RefusingDaemonMixin):
    def test_offline_ingest_still_writes_when_no_daemon_listens(self):
        """Port 1 has nothing behind it: offline-direct is the correct answer."""
        code, out, err = self.run_cli("ingest", "--content", "真的离线写入",
                                      "--agent", "t", "--evidence", "t:9", port=1)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload.get("mode"), "offline-direct")
        self.assertTrue(payload["results"][0]["inserted"])


class RestrictedAgentRefusalTests(RefusingDaemonMixin):
    def test_restricted_agent_gets_the_refusal_not_a_direct_write(self):
        config = write_agent_config(self.root / "agent.json",
                                    {"agent_id": "pi", "token": "z" * 32})
        before = self.rows()
        code, out, err = self.run_cli("ingest", "--content", "受限 Agent 越权写",
                                      "--agent", "t", "--evidence", "t:1",
                                      "--agent-config", str(config))
        self.assertEqual(code, 2)
        self.assertNotIn("offline-direct", out)
        self.assertEqual(self.rows(), before)

    def test_restricted_agent_still_refused_when_daemon_is_down(self):
        config = write_agent_config(self.root / "agent2.json",
                                    {"agent_id": "pi", "token": "z" * 32})
        code, out, err = self.run_cli("ingest", "--content", "离线也不许直写",
                                      "--agent", "t", "--evidence", "t:1",
                                      "--agent-config", str(config), port=1)
        self.assertEqual(code, 2)
        self.assertIn("受限 Agent", err)


class OutOfProcessSmokeTests(unittest.TestCase):
    """The same rule through the real entry point, once, without a daemon."""

    def test_cli_ingest_offline_direct_via_subprocess(self):
        with tempfile.TemporaryDirectory(prefix="hippo-refuse-sub-") as tmp:
            root = Path(tmp)
            ledger = root / "h.sqlite"
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            subprocess.run([sys.executable, "-m", "hippomind.cli", "init",
                            "--ledger", str(ledger), "--runtime-dir", str(root / "rt")],
                           capture_output=True, text=True, cwd=str(ROOT), env=env)
            r = subprocess.run(
                [sys.executable, "-m", "hippomind.cli", "ingest", "--content",
                 "子进程离线探针", "--agent", "t", "--evidence", "t:1",
                 "--ledger", str(ledger), "--runtime-dir", str(root / "rt"),
                 "--port", "1"],
                capture_output=True, text=True, cwd=str(ROOT), env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout).get("mode"), "offline-direct")


if __name__ == "__main__":
    unittest.main()
