"""daemon stats WAL 体积告警（迭代 38）。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WalWarningTests(unittest.TestCase):
    def test_stats_reports_wal_bytes_without_warning_when_small(self):
        sys.path.insert(0, str(ROOT))
        from hippomind.daemon import MemoryHubService, RuntimePaths

        tmp = Path(tempfile.mkdtemp(prefix="hippo-walwarn-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        paths = RuntimePaths.for_ledger(tmp / "w.sqlite", tmp / "runtime")
        svc = MemoryHubService(paths, watch=False, require_auth=False)
        try:
            st = svc.stats()
            self.assertIsInstance(st.get("wal_bytes"), int)
            self.assertIsNone(st.get("warnings"))
        finally:
            svc.shutdown()


if __name__ == "__main__":
    unittest.main()
