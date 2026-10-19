"""Regression tests for the three P0 data defects found in the live 4.48GB ledger.

Each test pins one behaviour that silently failed in production:

1. Raw re-capture produced a new hash every pass, so de-duplication never fired
   (1,475,584 raw rows, all distinct hashes, only ~3,573 unique messages per
   session).
2. Retention only demoted rows to ``cold``; nothing ever deleted them, so the
   file grew without bound.
3. Consolidation embedded source ids in row order, so every idle pass minted a new
   summary (56,010 semantic rows holding 142 distinct texts).
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest

from hippomind.ledger import SQLiteLedger


class StorageP0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.ledger = SQLiteLedger(os.path.join(self.home, "ledger.sqlite"))

    def tearDown(self) -> None:
        self.ledger.close()

    # -- P0 #1: stable identity for raw evidence --------------------------
    def test_replayed_raw_message_does_not_create_a_second_row(self):
        record = {
            "kind": "raw_chat", "raw_chat": True,
            "content": "部署失败是因为 daemon 没有重启",
            "source_agent": "codex", "session_id": "s1", "role": "user",
            "evidence": ["archive:1"],
        }
        first = self.ledger.ingest(dict(record, occurred_at="2026-08-10T10:00:00+00:00"))
        replay = self.ledger.ingest(dict(record, occurred_at="2026-08-11T18:30:00+00:00"))
        self.assertTrue(first.inserted)
        self.assertFalse(replay.inserted, "重放的同一条原始消息必须幂等")
        self.assertEqual(first.memory_id, replay.memory_id)
        self.assertEqual(self.ledger.stats()["memories"], 1)

    def test_raw_dedup_ignores_harness_wrapper_noise(self):
        """Same user text wrapped with different per-run timestamps is one row."""
        a = self.ledger.ingest({
            "kind": "raw_chat", "raw_chat": True, "source_agent": "pi", "session_id": "s",
            "role": "user", "evidence": ["a:1"],
            "content": ("<USER_REQUEST>检查服务状态</USER_REQUEST>\n"
                        "<ADDITIONAL_METADATA>2026-09-05T07:50:55-07:00</ADDITIONAL_METADATA>"),
        })
        b = self.ledger.ingest({
            "kind": "raw_chat", "raw_chat": True, "source_agent": "pi", "session_id": "s",
            "role": "user", "evidence": ["a:2"],
            "content": ("<USER_REQUEST>检查服务状态</USER_REQUEST>\n"
                        "<ADDITIONAL_METADATA>2026-09-06T09:11:02-07:00</ADDITIONAL_METADATA>"),
        })
        self.assertTrue(a.inserted)
        self.assertFalse(b.inserted)
        self.assertEqual(a.memory_id, b.memory_id)

    def test_structured_fact_keeps_bitemporal_versions(self):
        """The same claim asserted at two times is legitimately two observations."""
        base = {"kind": "decision", "content": "用 SQLite 作为事实账本",
                "source_agent": "codex", "session_id": "s", "evidence": ["a:1"]}
        one = self.ledger.ingest(dict(base, valid_from="2026-08-10T00:00:00+00:00"))
        two = self.ledger.ingest(dict(base, valid_from="2026-08-12T00:00:00+00:00"))
        self.assertTrue(one.inserted and two.inserted)
        self.assertNotEqual(one.memory_id, two.memory_id)

    # -- P0 #2: retention must actually delete ----------------------------
    def _old_raw(self, count: int = 12) -> None:
        for index in range(count):
            self.ledger.ingest({
                "content": f"旧聊天消息 {index} 关于部署失败的排查过程",
                "kind": "raw_chat", "raw_chat": True, "source_agent": "codex",
                "session_id": f"s{index % 3}", "role": "user",
                "evidence": [f"archive:{index}"],
                "occurred_at": "2026-06-01T10:00:00+00:00",
            })

    def test_maintain_without_purge_keeps_rows_cold_and_auditable(self):
        self._old_raw()
        self.ledger.maintain(raw_after_days=1, raw_active_limit=100)
        rows = self.ledger.stats()["memories"]
        self.assertEqual(rows, 12, "默认策略只冷置，不删除（可用 include_cold 审计）")

    def test_purge_removes_rows_and_keeps_a_verifiable_archive(self):
        self._old_raw()
        archive_dir = os.path.join(self.home, "archives")
        result = self.ledger.purge_expired_raw(before_days=90, archive_dir=archive_dir)
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["purged"], 12)
        self.assertEqual(self.ledger.stats()["memories"], 0)
        self.assertEqual(self.ledger.stats()["events"], 0)
        # The content must still be recoverable from the cold tier.
        with gzip.open(result["archive_path"], "rt", encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(rows), 12)
        self.assertTrue(any("旧聊天消息 3" in row["content"] for row in rows))

    def test_purge_is_idempotent(self):
        self._old_raw()
        first = self.ledger.purge_expired_raw(before_days=90,
                                              archive_dir=os.path.join(self.home, "a"))
        second = self.ledger.purge_expired_raw(before_days=90,
                                               archive_dir=os.path.join(self.home, "a"))
        self.assertEqual(first["purged"], 12)
        self.assertEqual(second["purged"], 0)

    def test_purge_dry_run_deletes_nothing(self):
        self._old_raw()
        result = self.ledger.purge_expired_raw(before_days=90, dry_run=True)
        self.assertEqual(result["purge_candidates"], 12)
        self.assertEqual(result["purged"], 0)
        self.assertEqual(self.ledger.stats()["memories"], 12)

    def test_purge_never_touches_recent_or_structured_rows(self):
        self._old_raw(4)
        self.ledger.ingest({"kind": "decision", "content": "旧但重要的结构化决定",
                            "source_agent": "codex", "session_id": "s",
                            "evidence": ["a:99"],
                            "occurred_at": "2020-01-01T00:00:00+00:00"})
        self.ledger.ingest({"kind": "raw_chat", "raw_chat": True, "content": "刚写入的新消息",
                            "source_agent": "codex", "session_id": "s", "role": "user",
                            "evidence": ["a:100"]})
        result = self.ledger.purge_expired_raw(before_days=90,
                                               archive_dir=os.path.join(self.home, "a"))
        self.assertEqual(result["purged"], 4)
        remaining = {row["kind"] for row in self.ledger.search("", include_cold=True, limit=50)}
        self.assertIn("decision", remaining)

    # -- P0 #3: consolidation must not feed itself ------------------------
    def test_consolidation_converges_instead_of_growing(self):
        for index in range(4):
            self.ledger.ingest({
                "content": f"任务进展 {index}：修复部署脚本",
                "kind": "progress", "source_agent": "codex", "task_id": "T1",
                "project": "p", "session_id": "s", "evidence": [f"a:{index}"],
                "context": {"goal": ["把部署流程自动化"]},
            })
        counts = []
        for _ in range(20):
            self.ledger.consolidate(limit=64)
            counts.append(self.ledger.stats()["memories"])
        self.assertEqual(len(set(counts)), 1, f"巩固在自噬增殖: {counts}")

    def test_consolidation_is_idempotent_across_new_source_rows(self):
        """Adding one new source row must add at most one new summary."""
        for index in range(2):
            self.ledger.ingest({
                "content": f"进展 {index}", "kind": "progress", "source_agent": "codex",
                "task_id": "T2", "project": "p", "session_id": "s", "evidence": [f"a:{index}"],
                "context": {"goal": ["目标 A"]},
            })
        self.ledger.consolidate()
        baseline = self.ledger.stats()["memories"]
        self.ledger.consolidate()
        self.assertEqual(self.ledger.stats()["memories"], baseline)
        self.ledger.ingest({
            "content": "进展 2", "kind": "progress", "source_agent": "codex",
            "task_id": "T2", "project": "p", "session_id": "s", "evidence": ["a:2"],
            "context": {"goal": ["目标 A"]},
        })
        self.ledger.consolidate()
        self.assertEqual(self.ledger.stats()["memories"], baseline + 2,
                         "新增一条来源应最多新增一条摘要（来源行 + 摘要）")


if __name__ == "__main__":
    unittest.main()


class DedupeCollapseTests(unittest.TestCase):
    """Collapsing historical duplicates must be session-scoped and recoverable."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "ledger.sqlite")
        self.ledger = SQLiteLedger(self.path)

    def tearDown(self) -> None:
        self.ledger.close()

    def _legacy_rows(self, count: int, session: str, text: str) -> None:
        """Insert pre-fix rows directly: distinct hashes for identical text."""
        import sqlite3
        with sqlite3.connect(self.path) as conn:
            for index in range(count):
                mid = f"mem_{session}_{index:03d}"
                eid = f"evt_{session}_{index:03d}"
                conn.execute(
                    "INSERT INTO ledger_events(event_id,event_type,occurred_at,ingested_at,"
                    "source_agent,session_id,scope,evidence_json,content,content_hash) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (eid, "raw_chat", f"2026-08-10T10:{index:02d}:00+00:00",
                     "2026-08-10T10:00:00+00:00", "codex", session, "shared",
                     '["a:1"]', text, f"hash_{session}_{index}"))
                conn.execute(
                    "INSERT INTO memories(memory_id,origin_event_id,kind,content,source_agent,"
                    "session_id,scope,evidence_json,created_at,updated_at,last_weighted_at,"
                    "raw_chat,quality_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, eid, "raw_chat", text, "codex", session, "shared", '["a:1"]',
                     "2026-08-10T10:00:00+00:00", "2026-08-10T10:00:00+00:00",
                     "2026-08-10T10:00:00+00:00", 1, "accepted"))

    def test_collapse_keeps_one_row_per_session(self):
        text = "同一条被重复捕获的消息：部署失败因为 daemon 未重启"
        self._legacy_rows(20, "s1", text)
        self._legacy_rows(1, "s2", text)  # different conversation, must survive
        result = self.ledger.collapse_duplicate_rows(
            archive_dir=os.path.join(self.home, "arch"))
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["collapsed"], 19)
        self.assertEqual(self.ledger.stats()["memories"], 2)

    def test_collapse_dry_run_is_read_only(self):
        self._legacy_rows(5, "s1", "重复内容 abc")
        before = self.ledger.stats()["memories"]
        self.ledger.collapse_duplicate_rows(dry_run=True)
        self.assertEqual(self.ledger.stats()["memories"], before)

    def test_collapse_is_recoverable_from_archive(self):
        self._legacy_rows(4, "s1", "可恢复性验证内容 xyz")
        result = self.ledger.collapse_duplicate_rows(
            archive_dir=os.path.join(self.home, "arch"))
        with gzip.open(result["archive_path"], "rt", encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["reason"] == "duplicate" for row in rows))

    def test_new_ingest_does_not_create_duplicates_to_collapse(self):
        """The write path must already be idempotent; collapse finds nothing."""
        for index in range(10):
            self.ledger.ingest({
                "content": "已经幂等的消息", "kind": "raw_chat", "raw_chat": True,
                "source_agent": "codex", "session_id": "s", "role": "user",
                "evidence": [f"a:{index}"],
            })
        result = self.ledger.collapse_duplicate_rows(dry_run=True)
        self.assertEqual(result["duplicate_groups"], 0)
        self.assertEqual(self.ledger.stats()["memories"], 1)
