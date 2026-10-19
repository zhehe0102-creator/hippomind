"""记忆置顶 pin/unpin（迭代 33）回归测试。"""
import tempfile
import unittest
from pathlib import Path

from hippomind.ledger import SQLiteLedger, LedgerError


def _rec(text: str, marker: str) -> dict:
    return {"kind": "fact", "scope": "shared", "source_agent": "t",
            "content": text, "evidence": [f"t:{marker}"]}


class PinMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.led = SQLiteLedger(Path(self.tmp.name) / "pin.sqlite")
        for i in range(30):
            self.led.ingest(_rec(f"常规会议周报记忆 {i}", f"n{i}"))
        self.led.ingest(_rec("生产数据库端口是 5432", "db"))
        self.target = self.led.ingest(_rec("生产数据库备份策略是每日全量", "bak")).memory_id

    def test_pin_boosts_rank_among_relevant_candidates(self):
        before = self.led.search("生产数据库", limit=8)
        self.assertNotEqual(before[0]["memory_id"], self.target)
        self.led.pin_memory(self.target, True)
        after = self.led.search("生产数据库", limit=8)
        self.assertEqual(after[0]["memory_id"], self.target)
        self.assertEqual(after[0]["score_parts"]["pinned"], 1)

    def test_unpin_restores_order(self):
        self.led.pin_memory(self.target, True)
        self.led.pin_memory(self.target, False)
        hits = self.led.search("生产数据库", limit=8)
        top = next(h for h in hits if h["memory_id"] == self.target)
        self.assertEqual(top["score_parts"]["pinned"], 0)

    def test_pin_does_not_force_irrelevant_memory_into_results(self):
        """置顶只重排相关候选——无关查询不得因置顶而召回。"""
        self.led.ingest(_rec("量子退相干测量协议与贝尔不等式", "q"))
        qid = self.led.ingest(_rec("完全无关的爵士和弦进行笔记", "j")).memory_id
        self.led.pin_memory(qid, True)
        hits = self.led.search("量子退相干", limit=8)
        self.assertFalse(any(h["memory_id"] == qid for h in hits),
                         "置顶不得把无关记忆带进结果")

    def test_list_pinned_and_missing_id(self):
        self.led.pin_memory(self.target, True)
        pinned = self.led.list_pinned()
        self.assertEqual([p["memory_id"] for p in pinned], [self.target])
        with self.assertRaises(LedgerError):
            self.led.pin_memory("mem-does-not-exist", True)

    def test_pin_writes_audit_event_without_content(self):
        self.led.pin_memory(self.target, True)
        with self.led._reading() as conn:
            rows = conn.execute(
                "SELECT event_type,content FROM ledger_events WHERE event_type='pin'"
            ).fetchall()
        self.assertTrue(rows, "pin 必须落审计事件")
        self.assertTrue(all(r["content"] == "" for r in rows),
                        "审计事件不得携带记忆内容")


if __name__ == "__main__":
    unittest.main()
