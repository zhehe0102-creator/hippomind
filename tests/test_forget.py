"""隐私遗忘请求测试：软遗忘保留审计、purge 真清除、时间窗选择性。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from hippomind.ledger import LedgerError, SQLiteLedger


def _fact(content: str, marker: str, *, scope: str = "shared",
          timestamp: str = "2026-08-10T00:00:00+00:00") -> dict:
    return {"kind": "fact", "scope": scope, "source_agent": "tester",
            "content": content, "evidence": [f"evidence:{marker}"],
            "timestamp": timestamp}


class ForgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.ledger.ingest(_fact("冰川消融年度观测数据", "old1",
                                 timestamp="2026-07-01T00:00:00+00:00"))
        self.ledger.ingest(_fact("部署流水线优化完成", "new1",
                                 timestamp="2026-08-20T00:00:00+00:00"))

    def tearDown(self):
        self.tmp.cleanup()

    def _event_count(self, event_type: str) -> int:
        with sqlite3.connect(self.ledger.path) as conn:
            return conn.execute(
                "SELECT count(*) FROM ledger_events WHERE event_type=?",
                (event_type,)).fetchone()[0]

    def test_soft_forget_removes_projection_keeps_audit(self):
        result = self.ledger.forget(scope="shared",
                                    before_time="2026-08-01T00:00:00+00:00")
        self.assertEqual(result["forgotten"], 1)
        # 旧记录从召回消失，新记录保留
        old_hits = self.ledger.search("冰川消融年度观测数据", scope="shared", limit=5)
        new_hits = self.ledger.search("部署流水线优化完成", scope="shared", limit=5)
        self.assertEqual(old_hits, [])
        self.assertTrue(new_hits)
        # append-only 审计保留：两条原始事件仍在
        with sqlite3.connect(self.ledger.path) as conn:
            events = conn.execute(
                "SELECT count(*) FROM ledger_events WHERE event_type='fact'"
            ).fetchone()[0]
        self.assertEqual(events, 2)
        # forget 审计事件落账且不含被删内容
        self.assertEqual(self._event_count("forget"), 1)
        with sqlite3.connect(self.ledger.path) as conn:
            content = conn.execute(
                "SELECT content FROM ledger_events WHERE event_type='forget'"
            ).fetchone()[0]
        self.assertNotIn("旧部署记录甲", content)

    def test_purge_removes_event_content_entirely(self):
        self.ledger.forget(scope="shared", purge=True)
        hits = self.ledger.search("部署记录", scope="shared", limit=10,
                                  include_raw_chat=True, include_cold=True)
        self.assertEqual(hits, [])
        with sqlite3.connect(self.ledger.path) as conn:
            remaining = conn.execute(
                "SELECT count(*) FROM ledger_events WHERE event_type='fact'"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)
        self.assertEqual(self._event_count("forget"), 1)

    def test_requires_scope_or_ids(self):
        with self.assertRaises(LedgerError):
            self.ledger.forget()

    def test_no_match_reports_zero(self):
        result = self.ledger.forget(scope="nonexistent-scope")
        self.assertEqual(result["forgotten"], 0)


if __name__ == "__main__":
    unittest.main()
