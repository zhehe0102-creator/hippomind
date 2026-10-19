"""daemon 健康自监控（迭代 55）：睡眠巩固异常计数进 stats。"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sys.path.insert(0, str(ROOT))
        from hippomind.daemon import MemoryHubService, RuntimePaths
        paths = RuntimePaths.for_ledger(Path(self.tmp.name) / "h.sqlite",
                                        Path(self.tmp.name) / "runtime")
        self.svc = MemoryHubService(paths, watch=False, require_auth=False)
        self.addCleanup(self.svc.shutdown)

    def _force_idle(self):
        self.svc._last_activity_monotonic = time.monotonic() - 1200
        if getattr(self.svc.watcher, "last_import_monotonic", None) is not None:
            self.svc.watcher.last_import_monotonic = time.monotonic() - 1200
        self.svc._last_sleep_run = None

    def test_error_increments_counter_and_daemon_survives(self):
        def boom(*args, **kwargs):
            raise RuntimeError("注入的维护故障")

        original = self.svc.ledger.maintenance_checkpoint
        self.svc.ledger.maintenance_checkpoint = boom
        try:
            self._force_idle()
            report = self.svc._maybe_sleep_consolidate()
            self.assertIsNotNone(report)
            self.assertIn("error", report)
        finally:
            self.svc.ledger.maintenance_checkpoint = original
        stats = self.svc.stats()
        self.assertEqual(stats["health"]["sleep_errors"], 1)

    def test_success_increments_cycles(self):
        self._force_idle()
        report = self.svc._maybe_sleep_consolidate()
        self.assertIsNotNone(report)
        self.assertNotIn("error", report)
        stats = self.svc.stats()
        self.assertEqual(stats["health"]["sleep_cycles"], 1)
        self.assertTrue(stats["health"]["last_sleep_report_at"])


if __name__ == "__main__":
    unittest.main()
