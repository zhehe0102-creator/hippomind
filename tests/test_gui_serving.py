"""Static /ui serving contract for the HippoMind Atlas console.

The console is served by the daemon itself (same origin, no CORS).  These tests
pin the security-relevant parts of that contract: path containment, CSP and
framing headers, cache revalidation, MIME/charset handling, and the fact that
the page never carries the local token or ledger path.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.daemon import (MemoryHubHTTPServer, MemoryHubRequestHandler,
                              MemoryHubService, RuntimePaths)


class GuiServingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hippo-gui-serving-")
        root = Path(cls.tmp.name)
        ui_root = root / "web"
        (ui_root / "assets").mkdir(parents=True)
        (ui_root / "index.html").write_text(
            "<!doctype html><html><body><div id=\"app\"></div>"
            "<script type=\"module\" src=\"js/app.js\"></script></body></html>",
            encoding="utf-8")
        (ui_root / "assets" / "app.js").write_text("console.log('atlas');\n", encoding="utf-8")
        (root / "secret.txt").write_text("SECRET-CONTENT-MUST-NOT-LEAK", encoding="utf-8")

        cls.paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        cls.service = MemoryHubService(cls.paths, watch=False)
        cls._ui_patch = mock.patch.object(MemoryHubRequestHandler, "UI_ROOT", ui_root)
        cls._ui_patch.start()
        cls.server = MemoryHubHTTPServer(("127.0.0.1", 0), cls.service)
        cls.service._server = cls.server
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.service.ledger.close()
        cls._ui_patch.stop()
        cls.tmp.cleanup()

    def _fetch(self, path: str):
        try:
            response = urllib.request.urlopen(self.base + path, timeout=10)
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()
        return response.status, dict(response.headers), response.read()

    def test_index_is_served_without_auth_with_security_headers(self):
        status, headers, body = self._fetch("/ui/")
        self.assertEqual(200, status)
        self.assertIn(b"<!doctype html>", body)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("charset=utf-8", headers.get("Content-Type", ""))
        self.assertEqual("no-cache", headers.get("Cache-Control"))
        self.assertEqual("nosniff", headers.get("X-Content-Type-Options"))
        self.assertEqual("DENY", headers.get("X-Frame-Options"))
        self.assertEqual("no-referrer", headers.get("Referrer-Policy"))
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertNotIn(self.service.token.encode(), body)
        self.assertNotIn(str(self.paths.ledger_path).encode(), body)

    def test_trailing_slash_serves_index(self):
        status, _headers, body = self._fetch("/ui/")
        self.assertEqual(200, status)
        self.assertIn(b"<div id=\"app\">", body)

    def test_bare_ui_redirects_to_canonical_root(self):
        # Relative asset URLs need /ui/ as the document base path; without the
        # redirect every css/js request resolved against / and 404ed.
        import urllib.request as _request

        class NoRedirect(_request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = _request.build_opener(NoRedirect)
        try:
            opener.open(self.base + "/ui", timeout=10)
            self.fail("expected a redirect")
        except urllib.error.HTTPError as error:
            self.assertEqual(302, error.code)
            self.assertEqual("/ui/", error.headers.get("Location"))

    def test_asset_mime_and_charset(self):
        status, headers, body = self._fetch("/ui/assets/app.js")
        self.assertEqual(200, status)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn("charset=utf-8", headers.get("Content-Type", ""))
        self.assertIn(b"console.log('atlas')", body)

    def test_missing_asset_is_404(self):
        status, _headers, _body = self._fetch("/ui/missing.css")
        self.assertEqual(404, status)

    def test_directory_is_not_listed(self):
        status, _headers, _body = self._fetch("/ui/assets/")
        self.assertEqual(404, status)

    def test_encoded_traversal_is_403(self):
        # Both 400 (NUL/dot-led segment guard) and 403 (containment check) are
        # safe refusals; the traversal must never return the outside file.
        status, _headers, body = self._fetch("/ui/%2e%2e/secret.txt")
        self.assertIn(status, (400, 403))
        self.assertNotIn(b"SECRET-CONTENT-MUST-NOT-LEAK", body)
        status, _headers, body = self._fetch("/ui/assets/%2e%2e/%2e%2e/secret.txt")
        self.assertIn(status, (400, 403))
        self.assertNotIn(b"SECRET-CONTENT-MUST-NOT-LEAK", body)

    def test_unknown_ui_path_returns_json_error(self):
        status, headers, body = self._fetch("/ui/nope.js")
        self.assertEqual(404, status)
        self.assertIn(b"error", body)
        self.assertIn("application/json", headers.get("Content-Type", ""))


if __name__ == "__main__":
    unittest.main()
