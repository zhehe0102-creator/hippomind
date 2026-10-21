"""``hippomind doctor`` must be a gate that can fail, and must see stale launchd jobs.

Two defects this file locks down
--------------------------------
1. **A readiness gate that always exits 0.**  ``cmd_doctor`` printed
   ``[FAIL] daemon: hippomindd 未运行或不可达`` and ``结论: NEEDS ATTENTION``
   and then returned 0 regardless.  ``RELEASE.md`` tells operators to use
   doctor as a blocking check, and the whole point of ``stop`` returning 1 when
   the daemon is still alive (ff610e0) was that a health command which cannot
   fail a script is worse than no health command.  Doctor had the same bug.

2. **Stale login items are invisible.**  ``hippomind login-install`` writes the
   current package root into the plist, but a plist written by an older release
   keeps pointing at the old location.  After this workspace moved, launchd
   started a daemon whose ``PYTHONPATH`` no longer existed at every login, and
   nothing reported it.  The hand-written GUI job is the worst case: it is not
   produced by ``login-install`` at all, so it can keep a dead path forever.

What is pinned here
-------------------
* exit 1 iff some check has ``severity == 'error'`` (warnings stay advisory, so
  a healthy-but-warned ledger still exits 0 — the existing doctor tests rely on
  that);
* the launchd check reports a warning for a plist whose ``PYTHONPATH`` is gone,
  and never mutates the plist;
* the launchd check is silent on a machine with no login item;
* the check is skipped, not failed, off macOS.

The tests never touch the real ``~/Library/LaunchAgents``: ``HOME`` is patched
to a temporary directory, which is also what makes the "no plist" case
deterministic on a machine that *does* have one installed.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

from hippomind.cli import main  # noqa: E402


class _DoctorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-doctor-gate-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.base = ["--ledger", str(self.root / "h.sqlite"),
                     "--runtime-dir", str(self.root / "rt"), "--port", "1"]
        # A real ledger, so the only `error` in the fixture is the unreachable
        # daemon.  Without it the ledger check itself fails ("未创建") and the
        # exit-code assertions would pass for the wrong reason.
        from hippomind.ledger import SQLiteLedger

        SQLiteLedger(self.root / "h.sqlite").close()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Path.home() reads HOME on POSIX; make sure the helper agrees.
        home_patcher = mock.patch.object(Path, "home", lambda: self.home)
        home_patcher.start()
        self.addCleanup(home_patcher.stop)

    def _run(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(argv) + self.base)
        return code, out.getvalue()

    def _payload(self, *argv):
        code, text = self._run(*argv)
        return code, json.loads(text)

    def _write_plist(self, name, payload):
        target = self.home / "Library" / "LaunchAgents" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            plistlib.dump(payload, handle)
        return target


class DoctorExitCodeTests(_DoctorCase):
    def test_unreachable_daemon_fails_the_gate(self):
        """The defect: this used to exit 0 while printing FAIL."""
        code, payload = self._payload("doctor", "--format", "json")
        self.assertFalse(payload["ready"])
        self.assertTrue(any(item["id"] == "daemon" and item["severity"] == "error"
                            for item in payload["checks"]))
        self.assertEqual(1, code, "doctor 报了 FAIL 却仍然 exit 0——健康门形同虚设")

    def test_warnings_alone_do_not_fail_the_gate(self):
        """Advisory findings stay non-fatal; only `error` is blocking."""
        code, payload = self._payload("doctor", "--format", "json")
        severities = {item["severity"] for item in payload["checks"]}
        self.assertIn("warning", severities, "这个夹具应当至少有一条 warning")
        self.assertTrue(payload["errors"])
        # Strip the daemon error by pretending it is reachable; the remaining
        # warnings must leave the exit code at 0.
        from hippomind.cli import cmd_lifecycle

        healthy = {"status": "ok", "server_version": "test", "watch_enabled": True}
        with mock.patch.object(cmd_lifecycle, "_health_for_paths", lambda *a, **k: healthy):
            code, payload = self._payload("doctor", "--format", "json")
        self.assertTrue(payload["ready"], payload["errors"])
        self.assertEqual([], payload["errors"])
        self.assertEqual(0, code, "只有 warning 时不应让健康门失败")

    def test_exit_code_matches_the_json_ready_flag(self):
        """The two ways a script reads doctor must never disagree."""
        code, payload = self._payload("doctor", "--format", "json")
        self.assertEqual(0 if payload["ready"] else 1, code)


class LaunchdDriftTests(_DoctorCase):
    def _launchd(self, payload):
        return next(item for item in payload["checks"] if item["id"] == "launchd")

    def test_absent_login_item_is_ok(self):
        code, payload = self._payload("doctor", "--format", "json")
        item = self._launchd(payload)
        self.assertEqual("ok", item["severity"])
        self.assertIn("未安装", item["detail"])

    def test_dead_pythonpath_is_reported(self):
        """The pre-move path that launchd kept using after the workspace moved."""
        dead = "/nonexistent/pi-desktop/chat-workspace/hippocampus-agent"
        self._write_plist("com.hippomind.daemon.plist", {
            "Label": "com.hippomind.daemon",
            "ProgramArguments": [sys.executable, "-m", "hippomind.daemon"],
            "EnvironmentVariables": {"PYTHONPATH": dead},
            "WorkingDirectory": dead,
        })
        code, payload = self._payload("doctor", "--format", "json")
        item = self._launchd(payload)
        self.assertEqual("warning", item["severity"], item["detail"])
        self.assertIn("不存在", item["detail"])
        self.assertIn("hippomind.daemon.plist", item["detail"])
        # A warning must not *add* a blocking error.  (The fixture's daemon is
        # unreachable, so one genuine error is present and the exit code is 1
        # for that reason alone — the launchd warning must not be in it.)
        self.assertNotIn("launchd", " ".join(payload["errors"]))
        self.assertEqual(1 if payload["errors"] else 0, code)

    def test_hand_written_gui_plist_is_checked_too(self):
        """The GUI job is not written by login-install, so nothing else catches it."""
        dead = "/nonexistent/pi-desktop/chat-workspace/hippocampus-agent"
        self._write_plist("com.hippomind.gui.plist", {
            "Label": "com.hippomind.gui",
            "ProgramArguments": [sys.executable, "-m", "hippomind.main"],
            "EnvironmentVariables": {"PYTHONPATH": dead},
        })
        code, payload = self._payload("doctor", "--format", "json")
        item = self._launchd(payload)
        self.assertEqual("warning", item["severity"], item["detail"])
        self.assertIn("com.hippomind.gui.plist", item["detail"])

    def test_current_package_root_is_clean(self):
        """A plist pointing at this package root must not be called stale.

        Pinned with the external-volume condition forced off: on a checkout that
        happens to live on an external drive (this repo's own workspace does)
        the separate volume warning applies instead, and this test is about the
        *path* verdict, not the volume.
        """
        from hippomind.cli import cmd_lifecycle

        self._write_plist("com.hippomind.daemon.plist", {
            "Label": "com.hippomind.daemon",
            "ProgramArguments": [sys.executable, "-m", "hippomind.daemon"],
            "EnvironmentVariables": {"PYTHONPATH": str(ROOT)},
            "WorkingDirectory": str(ROOT),
        })
        with mock.patch.object(cmd_lifecycle, "_is_external_volume", lambda _p: False):
            code, payload = self._payload("doctor", "--format", "json")
        self.assertEqual("ok", self._launchd(payload)["severity"])

    def test_missing_interpreter_is_reported(self):
        self._write_plist("com.hippomind.daemon.plist", {
            "Label": "com.hippomind.daemon",
            "ProgramArguments": ["/nonexistent/bin/python3", "-m", "hippomind.daemon"],
        })
        code, payload = self._payload("doctor", "--format", "json")
        self.assertIn("解释器不存在", self._launchd(payload)["detail"])

    def test_check_is_read_only(self):
        """Doctor must never rewrite, load or unload a login item."""
        dead = "/nonexistent/pi-desktop/chat-workspace/hippocampus-agent"
        target = self._write_plist("com.hippomind.daemon.plist", {
            "Label": "com.hippomind.daemon",
            "ProgramArguments": [sys.executable, "-m", "hippomind.daemon"],
            "EnvironmentVariables": {"PYTHONPATH": dead},
        })
        before = (target.read_bytes(), target.stat().st_mtime_ns)
        with mock.patch("subprocess.run", side_effect=AssertionError(
                "doctor 不允许调用 launchctl 或任何子进程")):
            code, payload = self._payload("doctor", "--format", "json")
        after = (target.read_bytes(), target.stat().st_mtime_ns)
        self.assertEqual(before, after, "doctor 修改了登录项")
        self.assertEqual("warning", self._launchd(payload)["severity"])

    def test_external_volume_is_named_as_the_cause(self):
        """A plist that cannot be fixed by editing it must say so.

        launchd background jobs are denied read access to volumes other than
        the boot volume by macOS privacy protection (reproduced with a minimal
        probe: the same interpreter reads the path from a terminal, while the
        launchd job raises ``PermissionError: [Errno 1]`` inside ``_find_spec``).
        Every path in the plist can be valid and the job still fails at login,
        so reporting "paths are fine" would send the operator chasing the wrong
        thing.
        """
        self._write_plist("com.hippomind.daemon.plist", {
            "Label": "com.hippomind.daemon",
            "ProgramArguments": [sys.executable, "-m", "hippomind.daemon"],
            "EnvironmentVariables": {"PYTHONPATH": str(ROOT)},
            "WorkingDirectory": str(ROOT),
        })
        from hippomind.cli import cmd_lifecycle

        with mock.patch.object(cmd_lifecycle, "_is_external_volume", lambda _p: True):
            code, payload = self._payload("doctor", "--format", "json")
        item = self._launchd(payload)
        self.assertEqual("warning", item["severity"])
        self.assertIn("外置卷", item["detail"])
        self.assertIn("launchd", item["detail"])
        self.assertNotIn("登录项已过期", item["detail"],
                         "路径是对的，不该误报成过期")

    def test_internal_volume_plist_is_clean(self):
        self._write_plist("com.hippomind.daemon.plist", {
            "Label": "com.hippomind.daemon",
            "ProgramArguments": [sys.executable, "-m", "hippomind.daemon"],
            "EnvironmentVariables": {"PYTHONPATH": str(ROOT)},
            "WorkingDirectory": str(ROOT),
        })
        from hippomind.cli import cmd_lifecycle

        with mock.patch.object(cmd_lifecycle, "_is_external_volume", lambda _p: False):
            code, payload = self._payload("doctor", "--format", "json")
        self.assertEqual("ok", self._launchd(payload)["severity"])

    def test_non_macos_skips_the_check(self):
        from hippomind.cli import cmd_lifecycle

        with mock.patch.object(cmd_lifecycle.sys, "platform", "linux"):
            ok, severity, detail = cmd_lifecycle._launchd_check()
        self.assertTrue(ok)
        self.assertEqual("skipped", severity)
        self.assertIn("macOS", detail)


if __name__ == "__main__":
    unittest.main()
