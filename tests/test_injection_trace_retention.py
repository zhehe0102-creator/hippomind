"""injection_trace 保留期与隐私收敛回归（WP-P）。

覆盖：过期行剔除、近期/坏行保留、retention=0 关闭、每小时节流、
0600 权限保持、缺文件/空文件优雅处理、原子替换失败旧文件完好、
.1 轮转历史不参与清理，以及 summary() 的有界 retention 字段。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import injection_trace

#: Fixed wall clock so retention math never depends on the test host.
NOW = 1_760_000_000.0
DAY = 86400.0


def _at(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(epoch))


def _line(at_epoch=None, **fields):
    entry = dict(fields)
    if at_epoch is not None:
        entry["at"] = _at(at_epoch)
    return json.dumps(entry, ensure_ascii=False)


def _write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((chr(10).join(lines) + chr(10)).encode("utf-8"))


class TraceRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-trace-ret-")
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HIPPOMIND_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(injection_trace.RETENTION_ENV, None)
        injection_trace.reset_retention_state()
        self.addCleanup(injection_trace.reset_retention_state)

    def trace(self):
        return Path(self.tmp.name, "injection.jsonl")

    def temp_artifacts(self):
        return list(self.trace().parent.glob("*.retention.tmp"))

    def test_default_retention_is_30_days(self):
        self.assertEqual(30, injection_trace.retention_days())

    def test_retention_env_parsing(self):
        for raw, expected in (("0", 0), ("off", 0), ("false", 0), ("no", 0),
                              ("disabled", 0), ("7", 7), ("garbage", 30),
                              ("-3", 0), ("3650", 3650), ("999999", 3650)):
            with self.subTest(raw=raw):
                os.environ[injection_trace.RETENTION_ENV] = raw
                self.assertEqual(expected, injection_trace.retention_days())

    def test_expired_lines_removed_and_recent_lines_kept(self):
        path = self.trace()
        _write_lines(path, [
            _line(NOW - 40 * DAY, agent="expired"),
            _line(NOW - 2 * DAY, agent="recent"),
        ])
        result = injection_trace.prune_expired(now=NOW, force=True)
        self.assertTrue(result["pruned"])
        self.assertEqual(1, result["removed"])
        self.assertEqual(1, result["kept"])
        agents = [row.get("agent") for row in injection_trace.recent(10)]
        self.assertEqual(["recent"], agents)

    def test_boundary_line_at_cutoff_is_kept(self):
        path = self.trace()
        cutoff = NOW - 30 * DAY
        _write_lines(path, [
            _line(cutoff - 1, agent="just-expired"),
            _line(cutoff, agent="boundary"),
        ])
        result = injection_trace.prune_expired(now=NOW, force=True)
        self.assertEqual(1, result["removed"])
        agents = [row.get("agent") for row in injection_trace.recent(10)]
        self.assertEqual(["boundary"], agents)

    def test_unparsable_and_at_less_lines_are_preserved(self):
        path = self.trace()
        bad = b"not-json " + bytes([255])
        payload = (_line(NOW - 40 * DAY, agent="expired") + chr(10)).encode("utf-8")
        payload += bad + chr(10).encode("utf-8")
        payload += (_line(NOW, agent="fresh", note="ok") + chr(10)).encode("utf-8")
        payload += (_line(None, agent="no-at") + chr(10)).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        result = injection_trace.prune_expired(now=NOW, force=True)
        self.assertEqual(1, result["removed"])
        self.assertEqual(1, result["invalid"])
        raw = path.read_bytes()
        self.assertIn(bad, raw)
        self.assertIn(b'"agent": "no-at"', raw)
        self.assertNotIn(b"expired", raw)
        self.assertIn(b"fresh", raw)

    def test_retention_zero_disables_pruning(self):
        path = self.trace()
        _write_lines(path, [_line(NOW - 400 * DAY, agent="old")])
        before = path.read_bytes()
        os.environ[injection_trace.RETENTION_ENV] = "0"
        result = injection_trace.prune_expired(now=NOW, force=True)
        self.assertFalse(result["enabled"])
        self.assertFalse(result["pruned"])
        self.assertEqual(before, path.read_bytes())
        retention = injection_trace.summary()["retention"]
        self.assertFalse(retention["enabled"])
        self.assertEqual(0, retention["days"])

    def test_throttle_runs_at_most_once_per_hour(self):
        path = self.trace()
        _write_lines(path, [_line(NOW - 40 * DAY, agent="first")])
        first = injection_trace.prune_expired(now=NOW)
        self.assertTrue(first["pruned"])
        _write_lines(path, [_line(NOW - 40 * DAY, agent="second")])
        throttled = injection_trace.prune_expired(now=NOW + 60)
        self.assertTrue(throttled["throttled"])
        self.assertIn(b"second", path.read_bytes())
        injection_trace.reset_retention_state()
        again = injection_trace.prune_expired(now=NOW + 60)
        self.assertFalse(again["throttled"])
        self.assertTrue(again["pruned"])
        self.assertNotIn(b"second", path.read_bytes())

    def test_force_bypasses_throttle(self):
        path = self.trace()
        _write_lines(path, [_line(NOW - 40 * DAY, agent="old")])
        injection_trace.prune_expired(now=NOW)
        _write_lines(path, [_line(NOW - 40 * DAY, agent="old2")])
        forced = injection_trace.prune_expired(now=NOW + 1, force=True)
        self.assertFalse(forced["throttled"])
        self.assertTrue(forced["pruned"])

    def test_missing_file_is_a_silent_noop(self):
        path = self.trace()
        result = injection_trace.prune_expired(now=NOW, force=True)
        self.assertFalse(result["pruned"])
        self.assertEqual("missing", result["reason"])
        self.assertFalse(path.exists())

    def test_empty_file_is_a_silent_noop(self):
        path = self.trace()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        result = injection_trace.prune_expired(now=NOW, force=True)
        self.assertFalse(result["pruned"])
        self.assertEqual(0, path.stat().st_size)

    def test_pruned_file_keeps_private_mode(self):
        path = self.trace()
        _write_lines(path, [_line(NOW - 40 * DAY, agent="old"),
                            _line(NOW, agent="fresh")])
        os.chmod(path, 0o644)
        injection_trace.prune_expired(now=NOW, force=True)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_recent_and_summary_work_after_prune(self):
        path = self.trace()
        _write_lines(path, [_line(NOW - 40 * DAY, agent="old"),
                            _line(NOW - DAY, agent="fresh", injected=True)])
        injection_trace.prune_expired(now=NOW, force=True)
        rows = injection_trace.recent(5)
        self.assertEqual(1, len(rows))
        self.assertEqual("fresh", rows[0]["agent"])
        summary = injection_trace.summary()
        self.assertEqual(1, summary["count"])
        retention = summary["retention"]
        self.assertEqual(30, retention["days"])
        self.assertTrue(retention["enabled"])
        self.assertEqual(3600, retention["check_interval_seconds"])
        self.assertLessEqual(retention["last_removed"], 1)

    def test_rotation_backup_is_not_pruned(self):
        path = self.trace()
        backup = path.with_name(path.name + ".1")
        _write_lines(path, [_line(NOW - 40 * DAY, agent="old"),
                            _line(NOW, agent="fresh")])
        backup.write_bytes((_line(0, agent="rotated-evidence") + chr(10)).encode())
        before_backup = backup.read_bytes()
        injection_trace.prune_expired(now=NOW, force=True)
        self.assertEqual(before_backup, backup.read_bytes())

    def test_replace_failure_leaves_original_file_intact(self):
        path = self.trace()
        _write_lines(path, [_line(NOW - 40 * DAY, agent="old"),
                            _line(NOW, agent="fresh"),
                            "BAD-LINE"])
        before = path.read_bytes()
        with mock.patch.object(injection_trace.os, "replace",
                               side_effect=OSError("simulated")):
            result = injection_trace.prune_expired(now=NOW, force=True)
        self.assertFalse(result["pruned"])
        self.assertEqual("OSError", result.get("error"))
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([], self.temp_artifacts())

    def test_record_triggers_retention(self):
        path = self.trace()
        real_now = time.time()
        _write_lines(path, [_line(real_now - 40 * DAY, agent="expired-record")])
        injection_trace.reset_retention_state()
        injection_trace.record(agent="writer", query="q", context="c", item_count=1)
        raw = path.read_bytes()
        self.assertNotIn(b"expired-record", raw)
        self.assertIn(b"writer", raw)


if __name__ == "__main__":
    unittest.main()
