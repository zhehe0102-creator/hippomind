"""claim 冲突手动裁决测试：冲突制造 → 裁决 → 双向验证 + 错误路径。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hippomind.ledger import LedgerError, SQLiteLedger


def _claim_record(content: str, marker: str, confidence: float,
                  *, subject: str = "service.db") -> dict:
    return {
        "kind": "fact", "scope": "shared", "source_agent": "tester",
        "content": content, "evidence": [f"evidence:{marker}"],
        "subject": subject, "predicate": "config", "slot_key": "port",
        "value": {"port": marker}, "confidence": confidence,
    }


class ResolveClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        # 近似平局（分差 ≤0.05）→ 双双进入 conflict，均不可召回
        self.a = self.ledger.ingest(_claim_record("数据库端口配置甲 1111", "1111", 0.50))
        self.b = self.ledger.ingest(_claim_record("数据库端口配置乙 2222", "2222", 0.52))
        conflicts = self.ledger.list_claims(state="conflict", scope="shared")
        self.assertEqual(len(conflicts), 2)
        self.claim_a = next(c["claim_id"] for c in conflicts
                            if c["memory_id"] == self.a.memory_id)
        self.claim_b = next(c["claim_id"] for c in conflicts
                            if c["memory_id"] == self.b.memory_id)

    def tearDown(self):
        self.tmp.cleanup()

    def _states(self):
        import sqlite3
        with sqlite3.connect(self.ledger.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = {r["memory_id"]: dict(r) for r in conn.execute(
                "SELECT memory_id,lifecycle,quality_state FROM memories")}
            claims = {r["memory_id"]: r["state"] for r in conn.execute(
                "SELECT memory_id,state FROM memory_claims")}
        return rows, claims

    def test_conflict_creation_takes_both_offline(self):
        states, claims = self._states()
        for mid in (self.a.memory_id, self.b.memory_id):
            self.assertEqual(states[mid]["quality_state"], "conflict")
            self.assertEqual(claims[mid], "conflict")
        hits = self.ledger.search("数据库端口配置", scope="shared", limit=10)
        self.assertEqual(hits, [])

    def test_resolution_crowns_winner_and_demotes_loser(self):
        result = self.ledger.resolve_claim(self.claim_a, self.b.memory_id)
        self.assertEqual(result["winner"], self.b.memory_id)
        self.assertIn(self.a.memory_id, result["losers"])

        states, claims = self._states()
        # 胜者恢复 active/accepted 并回到 FTS 召回
        self.assertEqual(states[self.b.memory_id]["quality_state"], "accepted")
        self.assertEqual(states[self.b.memory_id]["lifecycle"], "active")
        self.assertEqual(claims[self.b.memory_id], "active")
        # 败者让位并保留谱系
        self.assertEqual(states[self.a.memory_id]["quality_state"], "superseded")
        self.assertEqual(states[self.a.memory_id]["lifecycle"], "superseded")
        self.assertEqual(claims[self.a.memory_id], "superseded")

        hits = self.ledger.search("数据库端口配置", scope="shared", limit=10)
        joined = " ".join(h["content"] for h in hits)
        self.assertIn("2222", joined)
        self.assertNotIn("1111", joined)

        # 审计事件落账
        import sqlite3
        with sqlite3.connect(self.ledger.path) as conn:
            event_types = [r[0] for r in conn.execute(
                "SELECT event_type FROM ledger_events WHERE event_type='resolve'")]
        self.assertEqual(len(event_types), 1)

    def test_reverse_resolution_flips_the_crown(self):
        self.ledger.resolve_claim(self.claim_a, self.b.memory_id)
        result = self.ledger.resolve_claim(self.claim_b, self.a.memory_id)
        self.assertEqual(result["winner"], self.a.memory_id)
        states, claims = self._states()
        self.assertEqual(states[self.a.memory_id]["quality_state"], "accepted")
        self.assertEqual(states[self.b.memory_id]["quality_state"], "superseded")
        self.assertEqual(claims[self.a.memory_id], "active")

    def test_unknown_claim_raises(self):
        with self.assertRaises(LedgerError):
            self.ledger.resolve_claim("cl_does_not_exist", self.b.memory_id)

    def test_unknown_winner_raises(self):
        with self.assertRaises(LedgerError):
            self.ledger.resolve_claim(self.claim_a, "mem_not_real")

    def test_winner_outside_conflict_group_raises(self):
        outside = self.ledger.ingest(_claim_record(
            "无关主题的记录 9999", "9999", 0.8, subject="service.cache"))
        with self.assertRaises(LedgerError):
            self.ledger.resolve_claim(self.claim_a, outside.memory_id)


if __name__ == "__main__":
    unittest.main()
