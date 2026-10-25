"""GET /graph 端点 + hippomind monitor 巡检（前景轮七）。"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import os as _os
import tempfile as _tempfile

# 隔离 HIPPOMIND_HOME：daemon 启动时会对检测到的 agent 执行 auto_install，
# 不隔离就会把测试用的临时 ledger 路径写进用户真实的 ~/.claude/CLAUDE.md、
# ~/.gemini/settings.json 等文件里（本机实测污染过 6 个测试）。
_HIPPO_HOME = _tempfile.mkdtemp(prefix="hippomind-test-home-")
_os.environ["HIPPOMIND_HOME"] = _HIPPO_HOME

import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths
from hippomind.ledger import SQLiteLedger


def _free_port() -> int:
    """Ask the kernel for a port nobody is listening on.

    This file used to hard-code 18890/18891.  Once other test modules started
    launching real daemons (tests/test_daemon_refusal_not_bypassed.py,
    test_restart_read_only.py, test_stop_exit_code.py), a fixed port became a
    cross-module race: the whole suite intermittently failed depending on
    whether two modules happened to overlap.  The offline probe needs a port
    that is *free*, which a fixed number cannot guarantee either.
    """
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])



class GraphEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-graph-ep-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = RuntimePaths.for_ledger(root / "hub.sqlite", root / "rt")
        self.service = MemoryHubService(self.paths, watch=False)
        self.server = MemoryHubHTTPServer(("127.0.0.1", 0), self.service)
        self.service._server = self.server
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.service.ledger.ingest({
            "content": "service-a 状态 v1", "kind": "fact",
            "source_agent": "t", "evidence": ["t:1"],
            "claims": [{"subject": "service-a", "predicate": "status",
                        "slot_key": "state", "value": {"v": "1"},
                        "confidence": 0.8}]})

    def _get(self, path, token=True):
        headers = {"X-HippoMind-Token": self.service.token} if token else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{self.port}{path}", headers=headers),
                    timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_graph_found(self):
        from urllib.parse import quote
        status, body = self._get("/graph?subject=" + quote("service-a"))
        self.assertEqual(status, 200)
        self.assertTrue(body["found"])
        self.assertTrue(body["nodes"])

    def test_graph_requires_subject(self):
        status, _ = self._get("/graph")
        self.assertEqual(status, 400)

    def test_graph_requires_auth(self):
        status, _ = self._get("/graph?subject=x", token=False)
        self.assertEqual(status, 401)

    def test_admin_cross_scope_empty_ok(self):
        status, body = self._get("/graph?subject=x&scope=agent:other")
        self.assertEqual(status, 200)
        self.assertFalse(body["found"])


def _reap(proc: "subprocess.Popen") -> None:
    """Stop a detached test daemon and wait for it to actually exit."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - last resort
        pass



class MonitorCommandTests(unittest.TestCase):
    def test_monitor_rounds_against_daemon(self):
        tmp = tempfile.TemporaryDirectory(prefix="hippo-mon-test-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        led_path = root / "d.sqlite"
        led = SQLiteLedger(led_path)
        for i in range(5):
            led.ingest({"content": f"巡检数据 {i}", "kind": "fact",
                        "source_agent": "t", "evidence": [f"t:{i}"]})
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "hippomind.daemon", "--ledger", str(led_path),
             "--runtime-dir", str(root / "rt"), "--port", str(port),
             "--no-watch", "--no-auth"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(ROOT))
        # The daemon is started with start_new_session=True and is only ever
        # stopped by this test.  ``addCleanup(proc.terminate)`` alone leaked it:
        # terminate() is asynchronous, so the TemporaryDirectory cleanup that
        # runs immediately afterwards tried to remove a directory whose SQLite
        # file was still open, and 77 ``hippo-mon-test-*`` dirs piled up in TMPDIR.
        # Reap properly -- terminate, wait, escalate -- *before* the rmtree.
        self.addCleanup(_reap, proc)
        time.sleep(2.0)
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "monitor",
             "--ledger", str(led_path), "--runtime-dir", str(root / "rt"),
             "--port", str(port), "--interval", "1", "--rounds", "2"],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0, r.stderr[-200:])
        self.assertEqual(r.stdout.count("memories=5"), 2)

    def test_monitor_offline_reports_not_crash(self):
        tmp = tempfile.TemporaryDirectory(prefix="hippo-mon-off-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "d.sqlite").touch()
        r = subprocess.run(
            [sys.executable, "-m", "hippomind.cli", "monitor",
             "--ledger", str(root / "d.sqlite"),
             "--runtime-dir", str(root / "rt"), "--port", str(_free_port()),
             "--interval", "1", "--rounds", "1"],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0)
        self.assertIn("OFFLINE", r.stdout)


if __name__ == "__main__":
    unittest.main()
