"""Team resume helper: keep the interrupted-teammate workflow reproducible."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import team_state


class TeamStateTests(unittest.TestCase):
    def test_missing_home_returns_empty(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"DSH_HOME": "/nonexistent-dsh-home"}):
            self.assertEqual([], team_state.read_tasks())
            self.assertEqual([], team_state.read_agents())

    def test_merges_boards_by_revision(self):
        import json
        import os
        import tempfile
        from unittest import mock
        tmp = tempfile.TemporaryDirectory(prefix="hippo-team-")
        self.addCleanup(tmp.cleanup)
        cache = Path(tmp.name) / "storages" / "session_projcache" / "sessions"
        cache.mkdir(parents=True)
        board = {"record": {"rows": {"agentTeam": {"val": {
            "members": [{"name": "worker", "phase": "active"}],
            "tasks": [
                {"id": "task-1", "revision": 1, "status": "pending"},
                {"id": "task-2", "revision": 1, "status": "in_progress", "ownerName": "worker"},
            ],
        }}}}}
        newer = {"record": {"rows": {"agentTeam": {"val": {
            "members": [{"name": "worker", "phase": "inactive"}],
            "tasks": [{"id": "task-1", "revision": 3, "status": "completed"}],
        }}}}}
        (cache / "old.json").write_text(json.dumps(board), encoding="utf-8")
        (cache / "new.json").write_text(json.dumps(newer), encoding="utf-8")
        # File mtimes decide which board is newest; force a deterministic order
        # so a fast test run cannot tie.
        os.utime(cache / "old.json", (1_000_000, 1_000_000))
        os.utime(cache / "new.json", (2_000_000, 2_000_000))
        with mock.patch.dict(os.environ, {"DSH_HOME": tmp.name}):
            tasks = {item["id"]: item for item in team_state.read_tasks()}
            agents = team_state.read_agents()
        self.assertEqual("completed", tasks["task-1"]["status"])
        self.assertEqual("in_progress", tasks["task-2"]["status"])
        self.assertEqual("inactive", agents[0]["phase"])


if __name__ == "__main__":
    unittest.main()
