"""hippomind ui — one-command launch of the HippoMind Atlas console."""

from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.cli import build_parser
from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths


def _run(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = int(args.func(args) or 0)
    return code, out.getvalue(), err.getvalue()


class UiCommandTests(unittest.TestCase):
    def test_parser_exposes_ui_without_open(self):
        parser = build_parser()
        args = parser.parse_args(["ui", "--no-open"])
        self.assertEqual("cmd_ui", args.func.__name__)
        self.assertTrue(args.no_open)

    def test_unreachable_daemon_is_a_failure_with_a_hint(self):
        with tempfile.TemporaryDirectory(prefix="hippo-ui-offline-") as tmp:
            root = Path(tmp)
            # Reserve and release a port so nothing is listening on it.
            import socket
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]
            sock.close()
            code, out, err = _run([
                "ui", "--no-open", "--ledger", str(root / "hub.sqlite"),
                "--runtime-dir", str(root / "rt"), "--port", str(free_port),
            ])
            self.assertEqual(1, code)
            self.assertIn("hippomind start", err)
            self.assertEqual("", out)

    def test_mints_single_use_bootstrap_url(self):
        with tempfile.TemporaryDirectory(prefix="hippo-ui-live-") as tmp:
            root = Path(tmp)
            paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
            service = MemoryHubService(paths, watch=False)
            server = MemoryHubHTTPServer(("127.0.0.1", 0), service)
            service._server = server
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                code, out, err = _run([
                    "ui", "--no-open", "--ledger", str(root / "hub.sqlite"),
                    "--runtime-dir", str(root / "rt"), "--port", str(port),
                ])
                self.assertEqual(0, code, err)
                self.assertIn("HippoMind Atlas", out)
                match = re.search(r"/ui#/pulse\?boot=([A-Za-z0-9_-]+)", out)
                self.assertIsNotNone(match, out)
                nonce = match.group(1)
                request = urllib.request.Request(
                    "http://127.0.0.1:%d/api/v1/ui-token/exchange" % port,
                    data=('{"nonce":"%s"}' % nonce).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=10) as response:
                    import json as _json
                    payload = _json.loads(response.read().decode("utf-8"))
                self.assertEqual(service.token, payload["token"])
                self.assertNotIn(service.token, out)
            finally:
                server.shutdown()
                server.server_close()
                service.ledger.close()

    def test_old_daemon_falls_back_to_static_console(self):
        """A pre-Atlas daemon answers /health but 404s /api/v1/ui-token."""
        import json
        import http.server

        class OldDaemonHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # /health only
                if self.path.split("?")[0] == "/health":
                    body = json.dumps({"status": "ok", "server_version": "0.9.2"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def do_POST(self):  # no Atlas API yet
                self.send_error(404)

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory(prefix="hippo-ui-fallback-") as tmp:
            root = Path(tmp)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OldDaemonHandler)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                code, out, err = _run([
                    "ui", "--no-open", "--ledger", str(root / "hub.sqlite"),
                    "--runtime-dir", str(root / "rt"), "--port", str(port),
                ])
                self.assertEqual(0, code)
                self.assertIn("/ui#/pulse", out)
                self.assertIn("令牌", out)
                self.assertIn("无法签发一次性 UI 令牌", err)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
