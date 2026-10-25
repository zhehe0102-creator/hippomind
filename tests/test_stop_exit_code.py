"""``hippomind stop`` must tell the truth through its exit code.

The defect this file exists for
-------------------------------
``cmd_stop`` printed the verdict and then ``return 0`` unconditionally, so a
daemon that refused to stop -- ``running: true``, ``message: "daemon 仍在运行"``
-- still reported success. That is not cosmetic: the whole reason ``restore``,
``redact-raw`` and ``maintain`` insist on a stopped daemon is that the ledger
must have no concurrent writer, and the natural script is

    hippomind stop && hippomind restore SNAPSHOT

With exit 0 that pipeline restores a live ledger while the writer is still
attached. The command's own JSON already knew the truth; only the exit code
lied.

Now: ``running`` -> exit 1, stopped -> exit 0, and a stop the CLI refuses for
ownership reasons keeps propagating as a hard error (exit 2 via ``main``).

Coverage
--------
* a real daemon (spawned as a child process, reaped by this test) stops and
  stops answering -- exit 0, ``running: false``;
* a daemon that will not shut down (a stub that answers /health and refuses
  /shutdown, i.e. exactly the "still running" shape) -- exit 1, ``running:
  true``, and the refusal reason still surfaced so an operator can act;
* a daemon serving *another* ledger -- the CLI refuses to stop it, exit 2 with
  the ownership message, and the other ledger's daemon survives (this is a
  guard, not a failure);
* ``stop`` with nothing running is a no-op that does not claim a stop happened.
"""

from __future__ import annotations

import io
import json
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import os as _os
import tempfile as _tempfile

# 隔离 HIPPOMIND_HOME：daemon 启动时会对检测到的 agent 执行 auto_install，
# 不隔离就会把测试用的临时 ledger 路径写进用户真实的 ~/.claude/CLAUDE.md、
# ~/.gemini/settings.json 等文件里（本机实测污染过 6 个测试）。
_HIPPO_HOME = _tempfile.mkdtemp(prefix="hippomind-test-home-")
_os.environ["HIPPOMIND_HOME"] = _HIPPO_HOME

import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.cli import main
from hippomind.daemon import RuntimePaths, ledger_identity


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class StopHarnessMixin(unittest.TestCase):
    def _prepare(self, prefix: str) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "h.sqlite"
        self.runtime = self.root / "rt"
        self.paths = RuntimePaths.for_ledger(self.ledger, self.runtime)
        self.proc = None

    def _cli(self, *argv: str, port: int) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv) + ["--ledger", str(self.ledger),
                                      "--runtime-dir", str(self.runtime),
                                      "--port", str(port)])
        text = out.getvalue().strip()
        return code, (json.loads(text) if text.startswith("{") else {}), err.getvalue()

    def _health_alive(self, port: int) -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port,
                                        timeout=1) as r:
                return json.loads(r.read()).get("status") == "ok"
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _wait_health(self, port: int, timeout: float = 12.0) -> None:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._health_alive(port):
                return
            time.sleep(0.05)
        self.fail("daemon on port %d never became healthy" % port)

    def _spawn_real_daemon(self, port: int) -> None:
        """One real daemon child process; this test owns and reaps it."""
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "hippomind.daemon", "--ledger", str(self.ledger),
             "--runtime-dir", str(self.runtime), "--port", str(port),
             "--no-watch", "--watch-interval", "30"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(ROOT))
        # reap-first: registered before anything can fail the test
        self.addCleanup(self._reap)
        self._wait_health(port)

    def _reap(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


class StopSucceedsTests(StopHarnessMixin):
    def test_stop_halts_a_running_daemon_and_exits_zero(self):
        self._prepare("hippo-stop-ok-")
        port = _free_port()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "--ledger", str(self.ledger),
                                   "--runtime-dir", str(self.runtime)]), 0)
        self._spawn_real_daemon(port)
        self.assertTrue(self._health_alive(port))

        code, payload, err = self._cli("stop", port=port)
        self.assertEqual(code, 0, err)
        self.assertIs(payload.get("running"), False, payload)
        self.assertFalse(self._health_alive(port),
                         "stop reported success but the daemon still answers")
        self.assertEqual(self.proc.wait(timeout=8), 0,
                         "the daemon child should have exited cleanly")


class StopRefusesOwnershipTests(StopHarnessMixin):
    def test_stop_refuses_to_kill_a_daemon_serving_another_ledger(self):
        """Guard, not failure: a foreign daemon must survive, and the CLI must
        say so loudly (exit 2) rather than quietly claiming success."""
        self._prepare("hippo-stop-foreign-")
        port = _free_port()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "--ledger", str(self.ledger),
                                   "--runtime-dir", str(self.runtime)]), 0)
        self._spawn_real_daemon(port)

        # retarget the same port at a different ledger
        other = self.root / "other.sqlite"
        self.ledger = other
        self.paths = RuntimePaths.for_ledger(other, self.runtime)

        code, payload, err = self._cli("stop", port=port)
        self.assertEqual(code, 2, err)
        self.assertIn("另一份账本", err)
        self.assertEqual(payload, {})
        self.assertTrue(self._health_alive(port),
                        "the refused stop must not have disturbed the daemon")

    def test_read_only_daemon_can_still_be_stopped(self):
        """e5a9cc9 keeps /shutdown for the local admin, so a read-only mirror is
        stoppable -- and the stop must report it honestly."""
        self._prepare("hippo-stop-ro-")
        port = _free_port()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["init", "--ledger", str(self.ledger),
                                   "--runtime-dir", str(self.runtime)]), 0)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "hippomind.daemon", "--ledger", str(self.ledger),
             "--runtime-dir", str(self.runtime), "--port", str(port),
             "--no-watch", "--read-only"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(ROOT))
        self.addCleanup(self._reap)
        self._wait_health(port)

        code, payload, err = self._cli("stop", port=port)
        self.assertEqual(code, 0, err)
        self.assertIs(payload.get("running"), False, payload)
        self.assertFalse(self._health_alive(port))


class StopFailsTests(StopHarnessMixin):
    def test_daemon_that_will_not_shut_down_exits_nonzero(self):
        """The exact lie this fixes: a daemon that answers /health but refuses
        /shutdown used to make ``stop`` exit 0 while printing running: true."""
        self._prepare("hippo-stop-fail-")
        port = _free_port()
        identity = {"ledger_id": "", "status": "ok"}
        ledger_path = self.root / "h.sqlite"
        ledger_path.touch()
        identity["ledger_id"] = ledger_identity(ledger_path)

        class Stubborn(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # keep test output clean; this stub is deliberate noise

            def _send(self, code, body):
                raw = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.rstrip("/") == "/health":
                    self._send(200, dict(identity))
                else:
                    self._send(404, {"error": "no"})

            def do_POST(self):
                self._send(403, {"error": "shutdown unavailable in this fixture"})

        server = ThreadingHTTPServer(("127.0.0.1", port), Stubborn)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._stub = (server, thread)
        self.addCleanup(self._close_stub)
        self._wait_health(port)

        code, payload, err = self._cli("stop", port=port)
        self.assertEqual(code, 1, err)
        self.assertIs(payload.get("running"), True, payload)
        self.assertIn("daemon 仍在运行", payload.get("message", ""))
        # the refusal reason stays visible so an operator can act on it
        self.assertIn("shutdown unavailable", json.dumps(payload, ensure_ascii=False))

    def _close_stub(self):
        server, thread = self._stub
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class StopWithNoDaemonTests(StopHarnessMixin):
    def test_stop_without_a_daemon_is_a_noop_and_exits_zero(self):
        self._prepare("hippo-stop-none-")
        port = _free_port()
        code, payload, err = self._cli("stop", port=port)
        self.assertEqual(code, 0, err)
        self.assertIs(payload.get("running"), False, payload)


if __name__ == "__main__":
    unittest.main()
