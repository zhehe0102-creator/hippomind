"""A read-only daemon must still be stoppable by the operator who started it.

Read-only mode is meant to downgrade the *data plane*: no ingest, feedback, sync
or reload.  The previous blanket downgrade also removed ``shutdown``, so the
local operator had no way to stop a mirror they had just started:

* ``hippomind stop`` received 403, fell into its transport-race path, printed
  ``{"running": true, "message": "daemon 仍在运行"}`` and still exited 0;
* ``hippomind start --restart`` onto that daemon then deadlocked in its 5-second
  stop wait and failed with "hippomindd 未在 5 秒内停止，拒绝并发启动第二个服务".

Lifecycle control is not a data-plane privilege, so the admin credential keeps
``shutdown`` (and only that) in read-only mode.  The downgrade is still not an
administrator: ``admin=False`` keeps ``/admin/reload`` refused and raw-chat
export refused.
"""

from __future__ import annotations

import io
import json
import signal
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import warnings
from contextlib import redirect_stdout
from pathlib import Path

from hippomind.cli import main
from hippomind.daemon import RuntimePaths, load_runtime_state, read_local_token
from hippomind.ledger import SQLiteLedger


def _free_port() -> int:
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ReadOnlyShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-ro-stop-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger = root / "h.sqlite"
        self.runtime = root / "rt"
        self.port = _free_port()
        self.paths = RuntimePaths.for_ledger(self.ledger, self.runtime)
        self.daemon_pid = None
        with redirect_stdout(io.StringIO()):
            code = main(["init", "--ledger", str(self.ledger),
                         "--runtime-dir", str(self.runtime)])
        self.assertEqual(code, 0)

    def tearDown(self):
        # Belt and braces: this file's whole point is that stop works, but a
        # failing assertion must still not leave a daemon holding the temp dir.
        pid = self.daemon_pid or load_runtime_state(self.paths).get("pid")
        if not pid:
            return
        try:
            os_kill = __import__("os").kill
            os_kill(int(pid), signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    os_kill(int(pid), 0)
                except OSError:
                    return
                time.sleep(0.05)
            os_kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def _cli(self, *argv):
        out = io.StringIO()
        base = ["--ledger", str(self.ledger), "--runtime-dir", str(self.runtime),
                "--port", str(self.port)]
        with redirect_stdout(out), warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            code = main(list(argv) + base)
        text = out.getvalue().strip()
        return code, (json.loads(text) if text.startswith("{") else {"raw": text})

    def _wait_health(self):
        deadline = time.monotonic() + 10.0
        last = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/health" % self.port, timeout=2) as response:
                    return json.loads(response.read())
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = exc
                time.sleep(0.05)
        self.fail("daemon never came up: %r" % (last,))

    def _post(self, endpoint, body):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, endpoint),
            data=json.dumps(body).encode("utf-8"), method="POST")
        request.add_header("X-HippoMind-Token", read_local_token(self.paths))
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

    def _start_read_only(self):
        code, payload = self._cli("start", "--read-only", "--no-watch")
        self.assertEqual(code, 0, payload)
        health = self._wait_health()
        self.daemon_pid = int(health["pid"])
        return health

    def test_local_token_keeps_shutdown_but_loses_ingest(self):
        self._start_read_only()
        status, body = self._post("/ingest", {"content": "只读写入探针", "kind": "fact",
                                              "source_agent": "t", "evidence": ["t:1"]})
        self.assertEqual(status, 403, body)
        self.assertEqual(SQLiteLedger(self.ledger).stats()["memories"], 0,
                         "read-only must still refuse the data plane")

    def test_stop_terminates_a_read_only_daemon(self):
        self._start_read_only()
        code, payload = self._cli("stop")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload.get("running"), payload)
        self.assertNotIn("仍在运行", json.dumps(payload, ensure_ascii=False))
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/health" % self.port, timeout=1) as response:
                    json.loads(response.read())
            except (urllib.error.URLError, OSError, ValueError):
                return
            time.sleep(0.05)
        self.fail("read-only daemon still answering /health after stop")

    def test_restart_over_a_read_only_daemon_does_not_deadlock(self):
        """The trap this fixes: restart sends /shutdown, so it must be granted."""
        self._start_read_only()
        started = time.monotonic()
        code, payload = self._cli("start", "--restart", "--read-only", "--no-watch")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload.get("restarted"), payload)
        self.assertLess(time.monotonic() - started, 12.0,
                        "restart should not burn the 5s stop-wait twice")
        health = self._wait_health()
        self.daemon_pid = int(health["pid"])
        status, body = self._post("/ingest", {"content": "重启后仍只读", "kind": "fact",
                                              "source_agent": "t", "evidence": ["t:2"]})
        self.assertEqual(status, 403, body)
        self.assertEqual(SQLiteLedger(self.ledger).stats()["memories"], 0)

    def test_read_only_downgrade_is_not_administrator(self):
        """The grant is shutdown *only*: /admin/reload stays refused."""
        self._start_read_only()
        status, body = self._post("/admin/reload", {})
        self.assertEqual(status, 403, body)
        self.assertIn("管理员", json.dumps(body, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
