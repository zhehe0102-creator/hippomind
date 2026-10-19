"""The causal chain must actually be written, not just documented.

``memory_relations`` was measured at **0 rows** on a live 1.5M-row ledger: the
adapter never passed ``causal_parents`` and nothing else created edges, so
``causal_ancestors`` — a headline capability of the design doc — could only ever
return an empty list.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from hippomind.agent_adapter import MemoryHubAgentAdapter
from hippomind.ledger import SQLiteLedger


class CausalChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "ledger.sqlite")
        self.ledger = SQLiteLedger(self.path)
        self.adapter = MemoryHubAgentAdapter(
            source_agent="codex", session_id="s1", task_id="T1", ledger=self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()

    def _relation_count(self) -> int:
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]

    def test_sequence_writes_relations(self):
        self.adapter.checkpoint("完成 schema 抽取", evidence=["c:1"])
        self.adapter.decision("采用 fail-closed 归档", evidence=["c:1"])
        self.assertEqual(self._relation_count(), 1)

    def test_chain_is_walkable_from_the_last_record(self):
        first = self.adapter.checkpoint("开始重构", evidence=["c:1"])
        second = self.adapter.decision("拆分 ledger", evidence=["c:1"])
        third = self.adapter.complete("阶段完成", evidence=["c:1"])
        ancestors = self.ledger.causal_ancestors([third.memory_id], max_depth=5)
        ids = {row["memory_id"] for row in ancestors}
        self.assertIn(second.memory_id, ids)
        self.assertIn(first.memory_id, ids)

    def test_duplicate_record_does_not_extend_the_chain(self):
        self.adapter.checkpoint("同一条进展", evidence=["c:1"])
        self.adapter.checkpoint("同一条进展", evidence=["c:1"])  # deduped
        self.assertEqual(self._relation_count(), 0,
                         "重复写入不应制造因果边")

    def test_explicit_causal_parents_are_respected(self):
        parent = self.ledger.ingest({"content": "手工指定的父节点", "kind": "decision",
                                     "source_agent": "codex", "evidence": ["c:1"]})
        self.adapter.record("result", "子节点结果", evidence=["c:1"],
                            metadata={"causal_parents": [parent.memory_id]})
        self.assertEqual(self._relation_count(), 1)

    def test_relations_do_not_form_cycles(self):
        first = self.adapter.checkpoint("第一步", evidence=["c:1"])
        # Try to make the first record a child of itself via explicit metadata.
        self.adapter.record("progress", "回指自身", evidence=["c:1"],
                            metadata={"causal_parents": [first.memory_id]})
        ancestors = self.ledger.causal_ancestors([first.memory_id], max_depth=10)
        self.assertNotIn(first.memory_id, {row["memory_id"] for row in ancestors})


if __name__ == "__main__":
    unittest.main()
