"""Associative edges: distilled memories that share a claim subject get linked."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.distill import DistillEngine
from hippomind.ledger import SQLiteLedger


class AssociationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-assoc-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _distilled(self, subject, value, task_id):
        return self.ledger.ingest({
            "content": "提炼：%s %s" % (subject, value),
            "kind": "semantic", "source_agent": "hippomind-distill",
            "scope": "shared", "task_id": task_id,
            "evidence": ["memory:seed-" + task_id],
            "timestamp": "2026-10-23T10:00:00Z",
            "claims": [{"subject": subject, "predicate": "状态",
                        "slot_key": task_id + ":status", "value": value,
                        "confidence": 0.9}],
        })

    def test_same_subject_memories_are_linked(self):
        first = self._distilled("HippoMind", "设计完成", "t1")
        second = self._distilled("HippoMind", "实现完成", "t2")
        engine = DistillEngine(self.ledger, extractor=None)
        result = engine.associate(limit=10)
        self.assertGreaterEqual(result["created"], 1)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT parent_memory_id, child_memory_id, relation_type "
                "FROM memory_relations").fetchall()
        self.assertTrue(rows)
        pairs = {(row["parent_memory_id"], row["child_memory_id"]) for row in rows}
        # related_to is undirected: either memory can be the parent depending on
        # which one the pass scanned first.
        connected = ((first.memory_id, second.memory_id) in pairs
                     or (second.memory_id, first.memory_id) in pairs)
        self.assertTrue(connected, pairs)
        self.assertTrue(all(row["relation_type"] == "related_to" for row in rows))

    def test_association_is_idempotent(self):
        self._distilled("HippoMind", "a", "t1")
        self._distilled("HippoMind", "b", "t2")
        engine = DistillEngine(self.ledger, extractor=None)
        first = engine.associate(limit=10)
        second = engine.associate(limit=10)
        self.assertGreaterEqual(first["created"], 1)
        self.assertEqual(0, second["created"])

    def test_unrelated_subjects_stay_unlinked(self):
        self._distilled("Alpha", "a", "t1")
        self._distilled("Beta", "b", "t2")
        engine = DistillEngine(self.ledger, extractor=None)
        self.assertEqual(0, engine.associate(limit=10)["created"])


if __name__ == "__main__":
    unittest.main()
