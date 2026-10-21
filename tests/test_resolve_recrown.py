"""Re-adjudicating a conflict must restore the winner's *lifecycle*, not just its quality.

The defect
----------
``resolve_claim`` crowns a winner by setting ``quality_state='accepted'`` and
demoting the losers to ``lifecycle='superseded'``.  The function explicitly
supports re-adjudication (the same slot group can be resolved again, "翻案重裁"),
but it only ever restored the *quality* half of the winner's state.  A memory
that had already lost once therefore came back as::

    lifecycle      = 'superseded'     <- never restored
    quality_state  = 'accepted'       <- restored
    superseded_by  = '<the old winner>'  <- never cleared

Every recall query requires ``lifecycle='active'``, so the newly crowned winner
was permanently unrecallable — ``search`` returned nothing for its own text —
and ``health_overview.fts`` reported ``drift=True`` because an ``accepted`` row
was missing from the index.  The conflict-resolution workflow the project
advertises was, in this path, a no-op that looked like success.

What is pinned here
-------------------
1. After a full lose-then-re-crown cycle the winner is ``active`` + ``accepted``
   with ``superseded_by`` cleared, is findable by search, and leaves the ledger
   with no FTS drift.
2. The loser keeps the existing semantics (``superseded``/``superseded``, out of
   the index).
3. A re-crowned memory whose quality was *quarantined* does not become
   recallable just because it won a slot: adjudication restores lifecycle, it
   does not launder quality.  This is a deliberate boundary, asserted so a
   future "make the winner visible" edit cannot quietly promote noise.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger  # noqa: E402


def _claim_record(content: str, marker: str, confidence: float = 0.5) -> dict:
    """A record whose slot key collides with every other one in this module."""
    return {
        "kind": "fact", "scope": "shared", "source_agent": "tester",
        "content": content, "evidence": [f"evidence:{marker}"],
        "confidence": confidence,
        "claims": [{"subject": "service.db", "predicate": "config",
                    "slot_key": "port", "value": {"port": marker},
                    "confidence": confidence}],
    }


class RecrownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-recrown-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _claim_id(self, memory_id: str) -> str:
        with self.ledger._reading() as conn:
            row = conn.execute(
                "SELECT claim_id FROM memory_claims WHERE memory_id=?",
                (memory_id,)).fetchone()
        self.assertIsNotNone(row, "夹具里的 claim 没有落库")
        return row["claim_id"]

    def _states(self, memory_id: str) -> dict:
        memory = self.ledger.get_memory(memory_id)
        return {key: memory[key] for key in
                ("lifecycle", "quality_state", "superseded_by")}

    def _make_conflict(self):
        """Two near-tied values in one slot, which the ledger puts in conflict."""
        first = self.ledger.ingest(_claim_record("数据库端口配置甲 1111", "1111", 0.50))
        second = self.ledger.ingest(_claim_record("数据库端口配置乙 2222", "2222", 0.52))
        return first, second

    def test_recrown_restores_lifecycle_and_recall(self):
        first, second = self._make_conflict()
        # Round 1: second wins, so `first` is superseded (lifecycle included).
        self.ledger.resolve_claim(self._claim_id(second.memory_id), second.memory_id)
        self.assertEqual("superseded", self._states(first.memory_id)["lifecycle"])
        # Search is deliberately fuzzy, so assert on the loser's identity rather
        # than on an empty result set: a sibling sharing tokens may still match.
        loser_hits = [hit["memory_id"] for hit in
                      self.ledger.search("数据库端口配置甲", scope="shared", limit=10)]
        self.assertNotIn(first.memory_id, loser_hits, "败者必须不可召回")

        # Round 2: 翻案 —— first wins.
        self.ledger.resolve_claim(self._claim_id(first.memory_id), first.memory_id)
        state = self._states(first.memory_id)
        self.assertEqual("active", state["lifecycle"],
                         "重裁胜者的 lifecycle 没有恢复，导致它永久不可召回")
        self.assertEqual("accepted", state["quality_state"])
        self.assertEqual("", state["superseded_by"], "胜者仍带着旧的取代指针")

        hits = [hit["memory_id"] for hit in
                self.ledger.search("数据库端口配置甲", scope="shared", limit=10)]
        self.assertIn(first.memory_id, hits, "重裁胜者必须重新可召回")

        report = self.ledger.fts_sync_report()
        self.assertEqual(0, report["stale"], report)
        self.assertEqual(0, report["unindexed"], report)
        self.assertFalse(self.ledger.health_overview()["fts"]["drift"])

    def test_loser_semantics_unchanged(self):
        first, second = self._make_conflict()
        self.ledger.resolve_claim(self._claim_id(second.memory_id), second.memory_id)
        self.ledger.resolve_claim(self._claim_id(first.memory_id), first.memory_id)
        loser = self._states(second.memory_id)
        self.assertEqual("superseded", loser["lifecycle"])
        self.assertEqual("superseded", loser["quality_state"])
        self.assertEqual(first.memory_id, loser["superseded_by"])
        with self.ledger._reading() as conn:
            indexed = conn.execute("SELECT 1 FROM memory_fts WHERE memory_id=?",
                                   (second.memory_id,)).fetchone()
        self.assertIsNone(indexed, "败者不应留在热索引里")

    def test_recrown_promotes_quality_by_contract(self):
        """Adjudication is an explicit human confirmation, and promotes quality.

        The documented contract (BACKLOG: 「裁决后胜者恢复 accepted、败者
        superseded」) is that crowning a winner restores ``accepted``.  This test
        pins that decision *together with* the lifecycle fix, so a future edit
        cannot restore lifecycle while silently leaving a winner quarantined
        (which would reproduce the same "winner is not recallable" defect), nor
        drop the promotion while claiming to fix recall.

        The fixture forces the winner into quarantine first, which the normal
        path never does: a quarantined row only reaches a claim group if it
        carries a structured claim, i.e. it is not free-floating noise.
        """
        first, second = self._make_conflict()
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET quality_state='quarantine', "
                         "quality_score=0.15 WHERE memory_id=?", (first.memory_id,))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (first.memory_id,))
        self.ledger.resolve_claim(self._claim_id(second.memory_id), second.memory_id)
        self.ledger.resolve_claim(self._claim_id(first.memory_id), first.memory_id)

        state = self._states(first.memory_id)
        self.assertEqual("active", state["lifecycle"], "lifecycle 必须恢复")
        self.assertEqual("accepted", state["quality_state"],
                         "裁决恢复 accepted 是既有契约")
        hits = [hit["memory_id"] for hit in
                self.ledger.search("数据库端口配置甲", scope="shared", limit=10)]
        self.assertIn(first.memory_id, hits, "胜者必须可召回（这正是本缺陷的核心）")
        self.assertFalse(self.ledger.health_overview()["fts"]["drift"],
                         "accepted+active 必须已入索引，不得报漂移")

    def test_recrown_of_a_cold_row_returns_it_to_the_index(self):
        """A cold-but-accepted winner must come back into the hot index."""
        first, second = self._make_conflict()
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET lifecycle='cold' WHERE memory_id=?",
                         (first.memory_id,))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (first.memory_id,))
        self.ledger.resolve_claim(self._claim_id(second.memory_id), second.memory_id)
        self.ledger.resolve_claim(self._claim_id(first.memory_id), first.memory_id)
        self.assertEqual("active", self._states(first.memory_id)["lifecycle"])
        with self.ledger._reading() as conn:
            indexed = conn.execute("SELECT 1 FROM memory_fts WHERE memory_id=?",
                                   (first.memory_id,)).fetchone()
        self.assertIsNotNone(indexed, "重裁胜者必须重新进入热索引")
        self.assertFalse(self.ledger.health_overview()["fts"]["drift"])


if __name__ == "__main__":
    unittest.main()
