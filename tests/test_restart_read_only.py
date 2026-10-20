"""``hippomind start --restart --read-only`` must restart into read-only mode.

The regression this file exists for
-----------------------------------
``cmd_start`` already passed ``read_only=`` to ``_restart_daemon`` for the
``--restart`` branch, but the helper's keyword-only signature never declared
it, so every ``hippomind start --restart --read-only`` died with::

    TypeError: _restart_daemon() got an unexpected keyword argument 'read_only'

``_start_or_reuse_daemon``/``_start_or_reuse_daemon_locked`` (the plain start
path) and ``run_daemon`` (the foreground path) all accepted and forwarded the
flag; only the restart path dropped it.  A silently *ignored* parameter would
have been worse than the crash: the operator would believe the LAN mirror is
read-only while it happily accepts writes.

What is pinned here
-------------------
1. Plumbing: ``_restart_daemon`` forwards ``read_only`` into the launch helper.
2. Command line: the daemon child is actually spawned with ``--read-only``.
3. End to end, against a real spawned daemon: before the restart the local
   token can ``POST /ingest`` (200); after ``--restart --read-only`` the same
   request is refused (403) because every credential -- including the local
   admin token -- is downgraded to ``AccessPolicy.read_only_shared()``
   (permissions ``read``/``stats`` only), and the ledger did not gain a row.
   ``GET /health`` keeps working: read-only serves reads.
4. Teardown reaps exactly the process it started (SIGTERM, which run_daemon
   handles) by reading the pid from the daemon's own runtime state file --
   ``POST /shutdown`` is 403 in read-only mode, so the CLI cannot stop such a
   daemon, and an orphan service in CI is not acceptable.
"""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import warnings
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hippomind.cli import LedgerError, main
from hippomind.daemon import RuntimePaths, load_runtime_state, read_local_token

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class RestartForwardsReadOnlyTests(unittest.TestCase):
    """Unit pin: the flag reaches the launch helper, default False intact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-restart-unit-")
        self.addCleanup(self.tmp.cleanup)
        self.paths = RuntimePaths.for_ledger(Path(self.tmp.name) / "h.sqlite",
                                             Path(self.tmp.name) / "rt")

    def _restart(self, *args, **kwargs):
        from hippomind import cli
        with mock.patch.object(cli, "_health", side_effect=LedgerError("down")), \
                mock.patch.object(cli, "_start_or_reuse_daemon_locked",
                                  return_value=({"status": "ok"}, False,
                                                self.paths.state_path.parent / "hippomindd.log")) as started:
            out = cli._restart_daemon(self.paths, 8765, *args, **kwargs)
        return out, started

    def test_read_only_defaults_to_false(self):
        _out, started = self._restart()
        self.assertIs(started.call_args.kwargs["read_only"], False)

    def test_read_only_is_forwarded(self):
        _out, started = self._restart(read_only=True)
        self.assertIs(started.call_args.kwargs["read_only"], True)

    def test_launch_command_carries_the_flag(self):
        """The child daemon is spawned with --read-only (not merely accepted).

        ``subprocess`` is patched wholesale: nothing is ever launched here, and
        the frozen command line is read back from the call.
        """
        from hippomind import cli
        ok = {"status": "ok", "ledger_id": "x"}
        with mock.patch.object(cli.subprocess, "Popen") as popen, \
                mock.patch.object(cli, "ledger_identity", return_value="x"), \
                mock.patch.object(cli, "_health_for_paths",
                                  side_effect=[LedgerError("no daemon yet"), ok]):
            cli._start_or_reuse_daemon_locked(self.paths, 8765, read_only=True)
        command = popen.call_args.args[0]
        self.assertIn("--read-only", command)

        popen.reset_mock()
        with mock.patch.object(cli.subprocess, "Popen") as plain, \
                mock.patch.object(cli, "ledger_identity", return_value="x"), \
                mock.patch.object(cli, "_health_for_paths",
                                  side_effect=[LedgerError("no daemon yet"), ok]):
            cli._start_or_reuse_daemon_locked(self.paths, 8765)
        self.assertNotIn("--read-only", plain.call_args.args[0])


class RestartReadOnlyIntegrationTests(unittest.TestCase):
    """Real daemon: --restart --read-only actually lands in read-only mode."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-restart-ro-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "h.sqlite"
        self.runtime = self.root / "rt"
        self.port = _free_port()
        self.paths = RuntimePaths.for_ledger(self.ledger, self.runtime)
        self.daemon_pid = None
        with redirect_stdout(io.StringIO()):
            code = main(["init", "--ledger", str(self.ledger),
                         "--runtime-dir", str(self.runtime)])
        self.assertEqual(code, 0)
        self.ledger_rows = self._count()

    def tearDown(self):
        # Read-only mode refuses POST /shutdown by design (the policy is
        # downgraded for *every* credential), so reap the process the test
        # started, by the pid the daemon itself recorded.
        pid = self.daemon_pid or load_runtime_state(self.paths).get("pid")
        if pid:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                pass
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    os.kill(int(pid), 0)
                except OSError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass

    def _count(self) -> int:
        from hippomind.ledger import SQLiteLedger
        return int(SQLiteLedger(self.ledger).stats().get("memories", 0))

    def _cli(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        base = ["--ledger", str(self.ledger), "--runtime-dir", str(self.runtime),
                "--port", str(self.port)]
        # _start_or_reuse_daemon deliberately drops its Popen handle (the daemon
        # runs detached in its own session); this test reaps it in tearDown by
        # the pid recorded in the runtime state, so silence the GC warning the
        # detached child would otherwise print when collected mid-test.
        with redirect_stdout(out), warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            code = main(list(argv) + base)
        text = out.getvalue().strip()
        return code, (json.loads(text) if text.startswith("{") else {})

    def _post(self, endpoint: str, body: dict) -> tuple[int, dict]:
        token = read_local_token(self.paths)
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, endpoint),
            data=json.dumps(body).encode("utf-8"), method="POST")
        request.add_header("X-HippoMind-Token", token)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}

    def _wait_health(self) -> dict:
        deadline = time.monotonic() + 10.0
        last = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/health" % self.port, timeout=2) as r:
                    return json.loads(r.read())
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = exc
                time.sleep(0.05)
        self.fail("daemon never came up: %r" % (last,))

    def test_restart_read_only_switches_a_writable_daemon_to_read_only(self):
        # 1. plain start: the local token is a full administrator
        code, payload = self._cli("start", "--no-watch")
        self.assertEqual(code, 0, payload)
        health = self._wait_health()
        self.daemon_pid = int(health["pid"])
        self.assertEqual(health["status"], "ok")
        self.assertEqual(self.ledger_rows, 0)

        status, body = self._post("/ingest", {"content": "写入库前的探针",
                                              "kind": "fact", "source_agent": "t",
                                              "evidence": ["t:1"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(self._count(), 1)
        writable_pid = self.daemon_pid

        # 2. the previously crashing invocation: restart into read-only mode
        code, payload = self._cli("start", "--restart", "--read-only", "--no-watch")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload.get("restarted"), payload)

        health = self._wait_health()
        self.daemon_pid = int(health["pid"])
        self.assertEqual(health["status"], "ok")
        self.assertNotEqual(self.daemon_pid, writable_pid,
                            "restart must have replaced the process")

        # 3. the restarted daemon really is read-only: every credential,
        #    including the local token, is downgraded to
        #    AccessPolicy.read_only_shared() -> no "ingest" permission -> 403.
        status, body = self._post("/ingest", {"content": "只读模式下的写入尝试",
                                              "kind": "fact", "source_agent": "t",
                                              "evidence": ["t:2"]})
        self.assertEqual(status, 403, body)
        self.assertIn("ingest", str(body.get("error", "")))
        self.assertEqual(self._count(), 1, "被拒绝的写入不得落库")

        # 4. reads keep working: read_only_shared() still grants read+stats,
        #    and the pre-restart row is still there (a restart never wipes data)
        request = urllib.request.Request("http://127.0.0.1:%d/stats" % self.port,
                                         method="GET")
        request.add_header("X-HippoMind-Token", read_local_token(self.paths))
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self._count(), 1)

    def test_restart_read_only_survives_no_daemon_at_all(self):
        """A restart with nothing running starts read-only, not writable."""
        code, payload = self._cli("start", "--restart", "--read-only", "--no-watch")
        self.assertEqual(code, 0, payload)
        health = self._wait_health()
        self.daemon_pid = int(health["pid"])
        status, body = self._post("/ingest", {"content": "冷启动只读探针",
                                              "kind": "fact", "source_agent": "t",
                                              "evidence": ["t:3"]})
        self.assertEqual(status, 403, body)
        self.assertEqual(self._count(), 0)


if __name__ == "__main__":
    unittest.main()
