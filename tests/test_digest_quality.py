"""Session digest skips harness traffic and strips prompt boilerplate."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger
from hippomind.session_digest import SessionDigester, _informative, _strip_boilerplate


class DigestQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-digest-q-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def test_boilerplate_tags_are_stripped(self):
        raw = "<USER_REQUEST> 帮我检查手表连接问题 </USER_REQUEST> <ADDITIONAL_METADATA> now </ADDITIONAL_METADATA>"
        cleaned = _strip_boilerplate(raw)
        self.assertNotIn("USER_REQUEST", cleaned)
        self.assertIn("手表", cleaned)

    def test_harness_traffic_is_not_informative(self):
        self.assertFalse(_informative("Return only JSON: {\"ok\":true}"))
        self.assertFalse(_informative("<MEMORY_DATA_UNTRUSTED>x</MEMORY_DATA_UNTRUSTED>"))
        self.assertTrue(_informative("我们决定把默认主题改为纸本，并且把数据库换成 SQLite WAL 模式，已验证通过。"))

    def test_noise_session_is_skipped(self):
        for index in range(6):
            self.ledger.ingest({
                "content": "Return only JSON: {\"ok\":true} step %d" % index,
                "kind": "raw_chat", "source_agent": "claude",
                "scope": "agent:claude", "raw_chat": True, "session_id": "noise-1",
                "evidence": ["claude:noise:%d" % index],
                "timestamp": "2026-10-23T10:0%d:00Z" % index,
            })
        digester = SessionDigester(self.ledger, extractor=None)
        result = digester.run(limit=5)
        self.assertEqual(0, result["digested"])
        self.assertEqual(1, result["skipped"])


if __name__ == "__main__":
    unittest.main()
