"""claim 状态不变式的回归测试。

## 锁定的真实事故（2026-10-24）

`data_hygiene.py --apply` 把 4,543 条重复记忆降级为 `quality_state='quarantine'`，
但**没有同步收敛它们的 claim**。结果 1,535 条 claim 仍标 `active`：

* 检索时它们不会被召回（记忆已 quarantine）
* 但冲突检测、slot 占用、claim 统计全部把它们算进去
* 一个已隔离记忆的 claim 继续占着 slot，会让新记忆的 claim 被误判成"与它冲突"

health 报的是 "活跃 claim 4486 条，悬空 1535 条"，但那只是文案警告，
没有任何自动收敛。`repair_claim_states` 的旧条件只覆盖
`superseded`/`expired`，恰好漏掉 `quarantine`——而 quarantine 正是清理用的状态。

## 修这条时踩到的第二个坑

第一次写的是 `WHERE <predicate> IS 0`。predicate 是
`quality_state='accepted' AND lifecycle='active'` 两个条件拼起来的，
`IS 0` 只作用于最后一个，整个条件退化成
`quality_state='accepted' AND NOT lifecycle='active'` —— 只修掉 136/1671 条。
必须写成 `WHERE NOT (<predicate>)`。这里用测试钉住"全部收敛"而不是"收敛了一部分"。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hippomind.ledger import SQLiteLedger


class ClaimInvariantTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self._tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _ingest(self, content, *, claims):
        return self.ledger.ingest({
            "content": content, "kind": "decision", "scope": "shared",
            "source_agent": "test", "evidence": ["test:1"],
            "confidence": 0.9, "claims": claims})

    def _scalar(self, sql):
        with self.ledger._reading() as conn:
            return conn.execute(sql).fetchone()[0]

    def _active_claims(self):
        return self._scalar("SELECT COUNT(*) FROM memory_claims WHERE state='active'")

    def _dangling(self):
        return self._scalar(
            "SELECT COUNT(*) FROM memory_claims c "
            "JOIN memories m ON m.memory_id=c.memory_id "
            "WHERE c.state='active' AND ("
            "m.quality_state!='accepted' OR m.lifecycle!='active')")

    def _set_memory(self, memory_id, **columns):
        assignments = ", ".join("%s=?" % key for key in columns)
        params = tuple(columns.values()) + (memory_id,)

        def write(conn):
            conn.execute("UPDATE memories SET " + assignments + " WHERE memory_id=?",
                         params)

        self.ledger._transaction(write)

    def test_quarantined_memory_supersedes_its_claims(self):
        result = self._ingest("数据库端口 5432", claims=[{
            "subject": "svc", "predicate": "port", "slot_key": "db_port",
            "value": "5432", "confidence": 0.9}])
        self.assertEqual(self._active_claims(), 1)

        self._set_memory(result.memory_id, quality_state="quarantine")
        self.assertEqual(self._dangling(), 1)

        self.ledger.repair_claim_states()
        self.assertEqual(self._dangling(), 0)
        self.assertEqual(self._active_claims(), 0)

    def test_cold_archived_memory_supersedes_its_claims(self):
        result = self._ingest("缓存 TTL 300", claims=[{
            "subject": "svc", "predicate": "ttl", "slot_key": "cache_ttl",
            "value": "300", "confidence": 0.9}])
        self._set_memory(result.memory_id, lifecycle="cold")
        self.ledger.repair_claim_states()
        self.assertEqual(self._dangling(), 0)

    def test_all_dangling_rows_are_converged_not_just_some(self):
        # 钉住 SQL 括号 bug：predicate 是两个条件拼起来的，
        # 写成 "<predicate> IS 0" 只会修掉其中一类，这里造三类一起验。
        a = self._ingest("A 1", claims=[{"subject": "a", "predicate": "v",
                                        "slot_key": "k1", "value": "1",
                                        "confidence": 0.9}])
        b = self._ingest("B 2", claims=[{"subject": "b", "predicate": "v",
                                        "slot_key": "k2", "value": "2",
                                        "confidence": 0.9}])
        c = self._ingest("C 3", claims=[{"subject": "c", "predicate": "v",
                                        "slot_key": "k3", "value": "3",
                                        "confidence": 0.9}])
        self._set_memory(a.memory_id, quality_state="quarantine")
        self._set_memory(b.memory_id, lifecycle="cold")
        self._set_memory(c.memory_id, lifecycle="superseded")
        self.assertEqual(self._dangling(), 3)

        self.ledger.repair_claim_states()
        self.assertEqual(self._dangling(), 0)
        self.assertEqual(self._active_claims(), 0)

    def test_healthy_memory_claims_are_untouched(self):
        self._ingest("存活记忆", claims=[{
            "subject": "live", "predicate": "state", "slot_key": "live_state",
            "value": "ok", "confidence": 0.9}])
        self.ledger.repair_claim_states()
        self.assertEqual(self._active_claims(), 1)
        self.assertEqual(self._dangling(), 0)

    def test_repair_is_idempotent(self):
        result = self._ingest("幂等", claims=[{
            "subject": "x", "predicate": "v", "slot_key": "kx",
            "value": "1", "confidence": 0.9}])
        self._set_memory(result.memory_id, quality_state="quarantine")
        first = self.ledger.repair_claim_states()
        second = self.ledger.repair_claim_states()
        self.assertGreaterEqual(first["superseded"], 1)
        self.assertEqual(second["superseded"], 0)
        self.assertEqual(self._dangling(), 0)


if __name__ == "__main__":
    unittest.main()
