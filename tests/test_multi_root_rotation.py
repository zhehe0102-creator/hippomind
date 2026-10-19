"""watcher 多根交错扫描（迭代 60）：源顺序逐轮轮转。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MultiRootRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sys.path.insert(0, str(ROOT))
        from hippomind.ledger import SQLiteLedger
        from hippomind.watcher import ArchiveWatcher
        root_a = Path(self.tmp.name) / "a"
        root_b = Path(self.tmp.name) / "b"
        (root_a / "tasks").mkdir(parents=True)
        (root_b / "tasks").mkdir(parents=True)
        led = SQLiteLedger(Path(self.tmp.name) / "w.sqlite")
        self.watcher = ArchiveWatcher(led, archive_root=[root_a, root_b])

    def test_sync_round_increments_per_cycle(self):
        before = self.watcher._sync_round
        self.watcher.sync_once()
        self.assertEqual(self.watcher._sync_round, before + 1)
        self.watcher.sync_once()
        self.assertEqual(self.watcher._sync_round, before + 2)

    def test_rotation_covers_all_offsets(self):
        sources = list(range(4))
        seen_first = set()
        for rnd in range(4):
            shift = rnd % len(sources)
            rotated = sources[shift:] + sources[:shift]
            seen_first.add(rotated[0])
        self.assertEqual(seen_first, set(sources),
                         "每个根都应轮流获得首位处理权")


if __name__ == "__main__":
    unittest.main()
