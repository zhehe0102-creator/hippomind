"""Injection traces are bounded and never contain the raw query."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import injection_trace


class InjectionTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-inject-")
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HIPPOMIND_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_record_and_summary(self):
        entry = injection_trace.record(agent="codex", query="secret query text",
                                       context="<MEMORY_DATA_UNTRUSTED>x</MEMORY_DATA_UNTRUSTED>",
                                       item_count=2, source="test")
        self.assertTrue(entry["injected"])
        self.assertEqual(entry["items"], 2)
        self.assertNotIn("secret query text", Path(self.tmp.name, "injection.jsonl").read_text("utf-8"))
        rows = injection_trace.recent(5)
        self.assertEqual(1, len(rows))
        self.assertEqual("codex", rows[0]["agent"])
        summary = injection_trace.summary()
        self.assertEqual(1, summary["count"])
        self.assertEqual(1, summary["injected"])


if __name__ == "__main__":
    unittest.main()
