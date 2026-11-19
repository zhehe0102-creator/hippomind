"""WP-C 回归：单槽裁决不产生记忆级爆炸半径。

审计复现夹具（lean 版）：

* 记忆 a 同时持有两个槽位的 claim：port=5432 与 theme=纸本；
* 记忆 b 的 port=6432 与 a 的近分（confidence 0.52 vs 0.50）→ 两者进入
  conflict；
* identity 的冲突探测是 memory 级 flag：a 的**全部** active claim（含无关的
  theme）都被打成 conflict；
* 人工裁决 port 槽位（b 胜）。

旧 resolve_claim 的实现是「记忆级」的：它把败者的所有 claim 下线，只要败者
在冲突槽位里没有 active claim 就把整条记忆标成 superseded。结果：theme 这条
无关事实既不是 active、也不可召回——裁决一个槽位，丢掉一条事实。这正是
审计里的「裁决爆炸半径」。

新语义（resolve_claim 委托 settle_claim_slot）：

* 只有 port 槽位让位；theme claim 收敛回 active；
* a 的 lifecycle 保持 active、quality 收敛回 accepted，仍可召回；
* 只有在败者不再持有任何 active/conflict claim 时才整体 superseded。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger  # noqa: E402


def _claim(slot: str, value: str, confidence: float,
           *, subject: str = "svc", predicate: str = "p") -> dict:
    return {"subject": subject, "predicate": predicate, "slot_key": slot,
            "value": value, "confidence": confidence}


class BlastRadiusTests(unittest.TestCase):
    """resolve_claim 的作用域必须是槽位，不是整条记忆。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-blast-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _ingest(self, claims, content):
        return self.ledger.ingest({
            "content": content, "kind": "fact", "source_agent": "tester",
            "evidence": ["evidence:" + content[:8]], "claims": claims,
        })

    def _blast_fixture(self):
        a = self._ingest(
            [_claim("port", "5432", 0.50), _claim("theme", "纸本", 0.90)],
            "数据库端口与主题 纸本 主题标记甲 5432")
        b = self._ingest(
            [_claim("port", "6432", 0.52)],
            "数据库端口乙 6432 端口标记乙")
        conflicts = self.ledger.list_claims(state="conflict")
        # a 的两个 claim 都被 memory 级 flag 打成 conflict，加上 b 的 port，
        # 冲突队列里应有 3 条 claim（2 条 port + 1 条无关 theme）。
        self.assertEqual(3, len(conflicts), "夹具没有造出 port 冲突")
        self.assertEqual({a.memory_id, b.memory_id},
                         {c["memory_id"] for c in conflicts})
        claim_a_port = next(
            c["claim_id"] for c in conflicts
            if c["memory_id"] == a.memory_id and c["slot_key"] == "port")
        return a, b, claim_a_port

    def _snapshot(self):
        with sqlite3.connect(str(self.ledger.path)) as conn:
            conn.row_factory = sqlite3.Row
            memories = {row["memory_id"]: dict(row) for row in conn.execute(
                "SELECT memory_id,lifecycle,quality_state,superseded_by,"
                "valid_until FROM memories")}
            claims = {(row["memory_id"], row["slot_key"]): dict(row)
                      for row in conn.execute(
                          "SELECT memory_id,slot_key,state,valid_from,valid_until "
                          "FROM memory_claims")}
        return memories, claims

    def test_slot_ruling_does_not_delete_unrelated_facts(self):
        a, b, claim_a_port = self._blast_fixture()
        # 前置：memory 级 conflict flag 已经把 a 的 theme claim 一起拖下水。
        memories, claims = self._snapshot()
        self.assertEqual("conflict", memories[a.memory_id]["quality_state"])
        self.assertEqual("conflict", claims[(a.memory_id, "theme")]["state"])

        result = self.ledger.resolve_claim(claim_a_port, b.memory_id)

        # 兼容返回字段 + 新增的槽位明细。
        self.assertEqual(b.memory_id, result["winner"])
        self.assertEqual([a.memory_id], result["losers"])
        self.assertEqual([a.memory_id], result["loser_memories"])
        self.assertEqual([], result["demoted"],
                         "a 仍持有无关事实，不得整体下线")
        self.assertTrue(result["settled"])

        memories, claims = self._snapshot()
        # 槽位结果：b 胜出；a 的 port 让位并闭合窗口（非零宽）。
        self.assertEqual("active", claims[(b.memory_id, "port")]["state"])
        self.assertEqual("superseded", claims[(a.memory_id, "port")]["state"])
        loser_window_end = claims[(a.memory_id, "port")]["valid_until"]
        self.assertNotEqual("", loser_window_end,
                            "败者 claim 的窗口没有闭合")
        self.assertGreater(loser_window_end,
                           claims[(a.memory_id, "port")]["valid_from"],
                           "败者窗口必须严格非零")
        # 记忆与无关 claim：a 仍 active/accepted，theme 收敛回 active。
        self.assertEqual("active", memories[a.memory_id]["lifecycle"])
        self.assertEqual("accepted", memories[a.memory_id]["quality_state"])
        self.assertEqual("", memories[a.memory_id]["superseded_by"])
        self.assertEqual("active", claims[(a.memory_id, "theme")]["state"])
        self.assertEqual("", claims[(a.memory_id, "theme")]["valid_until"])

    def test_both_sides_remain_recallable(self):
        a, b, claim_a_port = self._blast_fixture()
        self.ledger.resolve_claim(claim_a_port, b.memory_id)

        kept_hits = [hit["memory_id"] for hit in
                     self.ledger.search("主题标记甲", scope="shared", limit=10)]
        self.assertIn(a.memory_id, kept_hits,
                      "败者记忆的无关事实必须仍可召回")
        winner_hits = [hit["memory_id"] for hit in
                       self.ledger.search("6432", scope="shared", limit=10)]
        self.assertIn(b.memory_id, winner_hits, "胜者必须可召回")

    def test_invariants_and_audit_event(self):
        a, b, claim_a_port = self._blast_fixture()
        self.ledger.resolve_claim(claim_a_port, b.memory_id)

        self.assertEqual([], self.ledger.list_claims(state="conflict"),
                         "裁决后该槽位的冲突组必须清空")
        self.assertEqual(0, self.ledger.claims_consistency_report()["dangling"])
        self.assertFalse(self.ledger.health_overview()["fts"]["drift"],
                         "保留的败者不得造成 FTS 漂移")

        with sqlite3.connect(str(self.ledger.path)) as conn:
            rows = conn.execute(
                "SELECT event_type,metadata_json FROM ledger_events "
                "WHERE event_type='resolve'").fetchall()
        self.assertEqual(1, len(rows), "裁决必须恰好落一条 resolve 事件")
        metadata = json.loads(rows[0][1])
        self.assertEqual("slot", metadata["mode"])
        self.assertEqual(b.memory_id, metadata["winner"])
        self.assertEqual([a.memory_id], metadata["loser_memories"])
        self.assertTrue(metadata["retired"], "让位的 claim 必须出现在事件里")
        self.assertEqual([], metadata["demoted"])


class SingleClaimLoserStillRetired(unittest.TestCase):
    """爆炸半径收窄不能把「败者确实没有其它事实」的旧语义改坏。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-blast2-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def test_memory_without_other_facts_is_still_superseded(self):
        a = self.ledger.ingest({
            "content": "数据库端口配置甲 1111", "kind": "fact",
            "source_agent": "tester", "evidence": ["e:1111"],
            "claims": [_claim("port", "1111", 0.50)]})
        b = self.ledger.ingest({
            "content": "数据库端口配置乙 2222", "kind": "fact",
            "source_agent": "tester", "evidence": ["e:2222"],
            "claims": [_claim("port", "2222", 0.52)]})
        claim_a = next(c["claim_id"] for c in
                       self.ledger.list_claims(state="conflict")
                       if c["memory_id"] == a.memory_id)
        result = self.ledger.resolve_claim(claim_a, b.memory_id)
        self.assertEqual([a.memory_id], result["demoted"])
        memory = self.ledger.get_memory(a.memory_id)
        self.assertEqual("superseded", memory["lifecycle"])
        self.assertEqual("superseded", memory["quality_state"])
        self.assertEqual(b.memory_id, memory["superseded_by"])


if __name__ == "__main__":
    unittest.main()

