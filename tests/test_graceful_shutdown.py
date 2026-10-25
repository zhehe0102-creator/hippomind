"""daemon 优雅停机集成测试（迭代 31）。

真实子进程 + SIGTERM：验证退出码 0、运行态文件清理、WAL 侧车归零。
"""
import json
import signal
import subprocess
import sys
import tempfile
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


def _free_port() -> int:
    """Bind port 0 and use what the kernel hands back.

    Hard-coded 18791 made this test collide with any other module that happens
    to start a daemon at the same moment (the suite intermittently failed with
    'daemon 提前退出' / address-in-use once test_daemon_refusal_not_bypassed.py
    and the read-only tests joined in).  A dedicated free port removes the
    cross-module dependency entirely.
    """
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])



class GracefulShutdownTests(unittest.TestCase):
    def test_sigterm_exits_cleanly_with_zero_wal(self):
        tmp = Path(tempfile.mkdtemp(prefix="hippo-shutdown-test-"))
        self.addCleanup(lambda: subprocess.run(
            ["rm", "-rf", str(tmp)], check=False))
        ledger = tmp / "sd.sqlite"
        runtime = tmp / "runtime"
        port = _free_port()

        proc = subprocess.Popen(
            [sys.executable, "-m", "hippomind.daemon", "--ledger", str(ledger),
             "--runtime-dir", str(runtime), "--port", str(port),
             "--no-auth", "--no-watch"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 20
            ready = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.fail("daemon 提前退出")
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/stats", timeout=1) as r:
                        if r.status == 200:
                            ready = True
                            break
                except Exception:
                    time.sleep(0.2)
            self.assertTrue(ready, "daemon 未在时限内就绪")

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ingest",
                data=json.dumps({"content": "优雅停机测试记忆", "kind": "fact",
                                 "source_agent": "shutdown-test",
                                 "evidence": ["test:shutdown"]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)

            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

        self.assertEqual(rc, 0, "SIGTERM 后必须以 0 退出")
        self.assertFalse((runtime / "daemon-state.json").exists(),
                         "运行态状态文件必须被清理")
        wal = Path(str(ledger) + "-wal")
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        self.assertEqual(wal_bytes, 0, "停机检查点后 WAL 必须归零")


if __name__ == "__main__":
    unittest.main()
