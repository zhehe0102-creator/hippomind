"""Archive provenance + collapse crash-window regression tests.

Two measured defects in the raw-evidence tier are pinned here.  Both destroy
information that exists nowhere else, so neither can be repaired after the fact:

A. **Provenance loss.**  ``purge_expired_raw`` and ``collapse_duplicate_rows``
   are the only code paths that *delete* raw rows.  Once they run, their gzip
   archive is the sole surviving copy.  The payload used to carry content and
   identity but not the row's ``metadata_json`` — which is where
   ``archive_rowid`` / ``agent_chat_path`` live (the pointers into the external
   Agent-Chat archive).  The 4.27GB pre-compaction backup that also holds them
   is a temporary authority; it will be pruned.  Measured before the fix, a
   purge payload's keys were exactly::

       content, content_sha256, created_at, event_id, evidence, memory_id,
       occurred_at, project, purged_at, role, scope, session_id, source_agent,
       task_id

   — no ``metadata``.  The fix adds that one key and keeps every other key
   byte-identical, because operators and ``tools/migration/compact_ledger.py``
   already parse these archives.

B. **Destructive crash window.**  ``collapse_duplicate_rows`` snapshots its
   work into a *persistent* table ``_dedupe_groups`` (``CREATE TABLE IF NOT
   EXISTS``) and drops it in a ``finally``.  A process killed between the two
   leaves the table behind; the next run's ``IF NOT EXISTS`` then silently
   reuses the stale snapshot, whose ``survivor_id`` may name a row that no
   longer exists.  The delete JOIN then treats *every* remaining copy as a
   loser.  Measured before the fix with three identical rows and a stale
   snapshot naming a vanished survivor: ``duplicate_groups=2``,
   ``collapsed=4``, and **zero** rows left instead of one.

The fail-closed ordering (write archive -> chmod/fsync -> re-read verify ->
delete) is asserted here too: the new metadata key must not weaken it.
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import tempfile
import unittest

from hippomind.ledger import SQLiteLedger

#: Payload keys that predate the provenance fix.  These are a contract: an
#: archive written by an older HippoMind is still readable by an operator's
#: scripts, and a future edit must not silently drop any of them.
PURGE_LEGACY_KEYS = frozenset({
    "memory_id", "event_id", "content", "content_sha256", "source_agent",
    "session_id", "task_id", "project", "role", "scope", "evidence",
    "created_at", "occurred_at", "purged_at",
})
COLLAPSE_LEGACY_KEYS = frozenset({
    "reason", "memory_id", "event_id", "content", "content_sha256",
    "source_agent", "session_id", "created_at", "collapsed_at",
})

#: Full wire format after the fix.  Adding a key is a deliberate act: update
#: this set in the same change so the contract cannot drift unnoticed.
PURGE_KEYS = PURGE_LEGACY_KEYS | {"metadata"}
COLLAPSE_KEYS = COLLAPSE_LEGACY_KEYS | {"metadata"}

PROVENANCE = {"archive_rowid": 1000, "agent_chat_path": "/x/0.jsonl"}


def _encoded(metadata: dict) -> str:
    """Encode exactly the way ``hippomind.ledger._helpers._json`` does."""
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False, default=str)


def _read_archive(path: str):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class ArchiveProvenanceTests(unittest.TestCase):
    """Defect A: the archive is the only copy, so it must carry provenance."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "ledger.sqlite")
        self.ledger = SQLiteLedger(self.path)
        self.archive_dir = os.path.join(self.home, "archives")

    def tearDown(self) -> None:
        self.ledger.close()

    def _raw_rows(self, count: int, text: str, session: str = "s1",
                  metadata: dict = None) -> None:
        """Insert pre-fix-shaped rows directly so identity cannot dedupe them.

        ``ingest`` is already idempotent, so byte-identical replays collapse at
        the write path and there would be nothing left to archive.  These tests
        need the *historical* shape: distinct hashes for identical content.
        """
        encoded = _encoded(metadata if metadata is not None else PROVENANCE)
        with sqlite3.connect(self.path) as conn:
            for index in range(count):
                mid = "mem_%s_%03d" % (session, index)
                eid = "evt_%s_%03d" % (session, index)
                conn.execute(
                    "INSERT INTO ledger_events(event_id,event_type,occurred_at,ingested_at,"
                    "source_agent,session_id,scope,evidence_json,content,content_hash,"
                    "metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, "raw_chat", "2026-08-10T10:%02d:00+00:00" % index,
                     "2026-08-10T10:00:00+00:00", "codex", session, "shared",
                     '["a:1"]', text, "hash_%s_%d" % (session, index), encoded))
                conn.execute(
                    "INSERT INTO memories(memory_id,origin_event_id,kind,content,source_agent,"
                    "session_id,role,scope,evidence_json,metadata_json,created_at,updated_at,"
                    "last_weighted_at,raw_chat,quality_state) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, eid, "raw_chat", text, "codex", session, "user", "shared",
                     '["a:1"]', encoded, "2026-08-10T10:00:00+00:00",
                     "2026-08-10T10:00:00+00:00", "2026-08-10T10:00:00+00:00",
                     1, "accepted"))

    def _old_raw(self, count: int, text: str = "旧聊天消息：部署失败排查",
                 metadata: dict = None) -> None:
        """Insert raw rows old enough for the retention window."""
        encoded = _encoded(metadata if metadata is not None else PROVENANCE)
        with sqlite3.connect(self.path) as conn:
            for index in range(count):
                mid = "mem_old_%03d" % index
                eid = "evt_old_%03d" % index
                conn.execute(
                    "INSERT INTO ledger_events(event_id,event_type,occurred_at,ingested_at,"
                    "source_agent,session_id,scope,evidence_json,content,content_hash,"
                    "metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, "raw_chat", "2026-06-01T10:%02d:00+00:00" % index,
                     "2026-06-01T10:00:00+00:00", "codex", "s%d" % (index % 3),
                     "shared", '["archive:%d"]' % index, "%s %d" % (text, index),
                     "hash_old_%d" % index, encoded))
                conn.execute(
                    "INSERT INTO memories(memory_id,origin_event_id,kind,content,source_agent,"
                    "session_id,role,scope,evidence_json,metadata_json,created_at,updated_at,"
                    "last_weighted_at,raw_chat,quality_state) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, eid, "raw_chat", "%s %d" % (text, index), "codex",
                     "s%d" % (index % 3), "user", "shared", '["archive:%d"]' % index,
                     encoded, "2026-06-01T10:00:00+00:00", "2026-06-01T10:00:00+00:00",
                     "2026-06-01T10:00:00+00:00", 1, "accepted"))

    # -- 1) purge carries metadata, byte-for-byte after a JSON round-trip ----
    def test_purge_archive_carries_row_metadata_byte_for_byte(self):
        self._old_raw(1, metadata=PROVENANCE)
        stored = None
        with sqlite3.connect(self.path) as conn:
            stored = conn.execute(
                "SELECT metadata_json FROM memories").fetchone()[0]
        self.assertEqual(stored, _encoded(PROVENANCE))

        result = self.ledger.purge_expired_raw(before_days=90, archive_dir=self.archive_dir)
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["purged"], 1)
        records = _read_archive(result["archive_path"])
        self.assertEqual(len(records), 1)

        record = records[0]
        self.assertIn("metadata", record, "purge 归档丢失了 metadata_json（溯源指针只此一份）")
        self.assertEqual(record["metadata"], json.loads(stored))
        # Byte-for-byte: re-encoding the archived object must reproduce the
        # exact string the database held, so nothing was lossy-normalised.
        self.assertEqual(_encoded(record["metadata"]), stored,
                         "归档 metadata 与库内 metadata_json 字节级不一致")

    def test_purge_archive_records_empty_metadata_as_an_object(self):
        """Rows whose metadata is the default ``{}`` stay JSON objects."""
        self._old_raw(2, metadata={})
        result = self.ledger.purge_expired_raw(before_days=90, archive_dir=self.archive_dir)
        self.assertIsNone(result.get("error"))
        records = _read_archive(result["archive_path"])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["metadata"] == {} for record in records),
                        "空 metadata 必须是 {}，不能缺键或变成字符串")

    def test_purge_survives_unparseable_legacy_metadata(self):
        """One corrupt legacy blob must not block retention forever.

        ``metadata_json`` is machine-written for every live row, so a malformed
        value can only be legacy corruption.  Refusing to purge would recreate
        the unbounded-growth P0 (the row is re-selected on every pass); the
        archive keeps the content and records ``{}`` for the unreadable blob.
        """
        self._old_raw(1)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE memories SET metadata_json='{not json'")
            conn.execute("UPDATE ledger_events SET metadata_json='{not json'")
        result = self.ledger.purge_expired_raw(before_days=90, archive_dir=self.archive_dir)
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["purged"], 1)
        record = _read_archive(result["archive_path"])[0]
        self.assertEqual(record["metadata"], {})
        self.assertIn("旧聊天消息", record["content"], "内容仍必须完整归档")

    # -- 2) collapse carries metadata ---------------------------------------
    def test_collapse_archive_carries_row_metadata(self):
        text = "同一条被重复捕获的消息：部署失败因为 daemon 未重启"
        self._raw_rows(3, text, metadata=PROVENANCE)
        result = self.ledger.collapse_duplicate_rows(archive_dir=self.archive_dir)
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["collapsed"], 2)
        records = _read_archive(result["archive_path"])
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertIn("metadata", record,
                          "collapse 归档丢失了 metadata_json（溯源指针只此一份）")
            self.assertEqual(record["metadata"], PROVENANCE)
            self.assertEqual(_encoded(record["metadata"]), _encoded(PROVENANCE))

    def test_collapse_keeps_per_row_metadata_distinct(self):
        """Each collapsed copy has its own ``archive_rowid``; all must survive."""
        text = "同一内容，但每份拷贝指向不同的归档行"
        with sqlite3.connect(self.path) as conn:
            for index in range(3):
                mid, eid = "mem_%s_%03d" % ("s1", index), "evt_%s_%03d" % ("s1", index)
                meta = {"archive_rowid": 2000 + index,
                        "agent_chat_path": "/x/%d.jsonl" % index}
                conn.execute(
                    "INSERT INTO ledger_events(event_id,event_type,occurred_at,ingested_at,"
                    "source_agent,session_id,scope,evidence_json,content,content_hash,"
                    "metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, "raw_chat", "2026-08-10T10:%02d:00+00:00" % index,
                     "2026-08-10T10:00:00+00:00", "codex", "s1", "shared",
                     '["a:1"]', text, "hash_%d" % index, _encoded(meta)))
                conn.execute(
                    "INSERT INTO memories(memory_id,origin_event_id,kind,content,source_agent,"
                    "session_id,role,scope,evidence_json,metadata_json,created_at,updated_at,"
                    "last_weighted_at,raw_chat,quality_state) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, eid, "raw_chat", text, "codex", "s1", "user", "shared",
                     '["a:1"]', _encoded(meta), "2026-08-10T10:00:00+00:00",
                     "2026-08-10T10:00:00+00:00", "2026-08-10T10:00:00+00:00",
                     1, "accepted"))
        result = self.ledger.collapse_duplicate_rows(archive_dir=self.archive_dir)
        self.assertIsNone(result.get("error"))
        rowids = sorted(record["metadata"]["archive_rowid"]
                        for record in _read_archive(result["archive_path"]))
        self.assertEqual(rowids, [2000, 2001],
                         "被折叠的两份拷贝各自的 archive_rowid 都必须可恢复")

    # -- 4) the old wire format is a contract -------------------------------
    def test_purge_payload_keeps_every_legacy_key(self):
        self._old_raw(1)
        result = self.ledger.purge_expired_raw(before_days=90, archive_dir=self.archive_dir)
        record = _read_archive(result["archive_path"])[0]
        missing = sorted(PURGE_LEGACY_KEYS - set(record))
        self.assertEqual(missing, [], "purge 归档丢弃了既有键（破坏向后兼容）")
        self.assertEqual(set(record), set(PURGE_KEYS),
                         "purge 归档键集合变了：新增键必须同步更新 PURGE_KEYS")

    def test_collapse_payload_keeps_every_legacy_key(self):
        self._raw_rows(3, "契约锁定：折叠归档键集合")
        result = self.ledger.collapse_duplicate_rows(archive_dir=self.archive_dir)
        record = _read_archive(result["archive_path"])[0]
        missing = sorted(COLLAPSE_LEGACY_KEYS - set(record))
        self.assertEqual(missing, [], "collapse 归档丢弃了既有键（破坏向后兼容）")
        self.assertEqual(set(record), set(COLLAPSE_KEYS),
                         "collapse 归档键集合变了：新增键必须同步更新 COLLAPSE_KEYS")
        self.assertEqual(record["reason"], "duplicate")

    # -- fail-closed ordering must survive the new key ----------------------
    def test_archive_verification_still_gates_deletion(self):
        """Archive first, verify, then delete — in that order."""
        self._old_raw(3)
        result = self.ledger.purge_expired_raw(before_days=90, archive_dir=self.archive_dir)
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["purged"], 3)
        self.assertTrue(os.path.isfile(result["archive_path"]))
        # Every selected id is present in the archive before rows are gone.
        archived_ids = {record["memory_id"] for record in _read_archive(result["archive_path"])}
        self.assertEqual(len(archived_ids), 3)
        self.assertEqual(self.ledger.stats()["memories"], 0)

    def test_fsync_failure_aborts_before_any_delete(self):
        """A durability failure in step 2 must leave the ledger untouched.

        The ordering is ``write -> chmod/fsync -> re-read verify -> delete``; the
        new metadata key sits inside step 1 and must not create a path around it.
        An fsync that fails means the archive is not proven durable, so zero rows
        may be removed and the partial file must not be left behind.
        """
        self._old_raw(2, text="fsync 失败必须保留行")
        import hippomind.ledger.archive as archive_module
        from unittest import mock

        with mock.patch.object(archive_module.os, "fsync", side_effect=OSError("disk gone")):
            failure = self.ledger.purge_expired_raw(before_days=90,
                                                    archive_dir=self.archive_dir)
        self.assertIn("error", failure, "fsync 失败必须报告 error")
        self.assertEqual(failure["purged"], 0)
        self.assertEqual(self.ledger.stats()["memories"], 2,
                         "归档未落盘时不得删除任何行（fail-closed）")
        leftovers = [name for name in os.listdir(self.archive_dir)
                     if name.endswith(".jsonl.gz")]
        self.assertEqual(leftovers, [], "失败归档必须清理，不能留下半成品")

    def test_unusable_archive_directory_refuses_rather_than_deleting(self):
        """If the archive root cannot be prepared, the purge refuses outright."""
        self._old_raw(2, text="归档目录不可用必须拒绝")
        blocked = os.path.join(self.home, "blocked")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        with self.assertRaises(Exception) as caught:
            self.ledger.purge_expired_raw(before_days=90, archive_dir=blocked)
        self.assertIn("归档", str(caught.exception))
        self.assertEqual(self.ledger.stats()["memories"], 2,
                         "归档目录不可用时不得删除任何行")


class CollapseCrashWindowTests(unittest.TestCase):
    """Defect B: a stale ``_dedupe_groups`` snapshot must never be trusted."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "ledger.sqlite")
        self.ledger = SQLiteLedger(self.path)
        self.archive_dir = os.path.join(self.home, "arch")

    def tearDown(self) -> None:
        self.ledger.close()

    def _legacy_rows(self, count: int, session: str, text: str) -> None:
        """Pre-fix rows: distinct hashes for identical text (see module docstring)."""
        with sqlite3.connect(self.path) as conn:
            for index in range(count):
                mid = "mem_%s_%03d" % (session, index)
                eid = "evt_%s_%03d" % (session, index)
                conn.execute(
                    "INSERT INTO ledger_events(event_id,event_type,occurred_at,ingested_at,"
                    "source_agent,session_id,scope,evidence_json,content,content_hash) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (eid, "raw_chat", "2026-08-10T10:%02d:00+00:00" % index,
                     "2026-08-10T10:00:00+00:00", "codex", session, "shared",
                     '["a:1"]', text, "hash_%s_%d" % (session, index)))
                conn.execute(
                    "INSERT INTO memories(memory_id,origin_event_id,kind,content,source_agent,"
                    "session_id,role,scope,evidence_json,created_at,updated_at,"
                    "last_weighted_at,raw_chat,quality_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, eid, "raw_chat", text, "codex", session, "user", "shared",
                     '["a:1"]', "2026-08-10T10:00:00+00:00",
                     "2026-08-10T10:00:00+00:00", "2026-08-10T10:00:00+00:00",
                     1, "accepted"))

    def _raw_ids(self):
        with sqlite3.connect(self.path) as conn:
            return sorted(row[0] for row in
                          conn.execute("SELECT memory_id FROM memories WHERE raw_chat=1"))

    def _seed_stale_groups(self, text: str) -> None:
        """Leave behind the table a killed process would have left.

        The snapshot names a survivor that no longer exists (``mem_ghost_999``)
        plus a whole group whose rows are already gone.  ``CREATE TABLE IF NOT
        EXISTS`` used to adopt this state verbatim.
        """
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS _dedupe_groups AS
                SELECT MAX(memory_id) AS survivor_id, content, source_agent, session_id,
                       role, COUNT(*) AS n
                FROM memories WHERE raw_chat=1 AND content!=''
                GROUP BY content, source_agent, session_id, role HAVING COUNT(*)>1
            """)
            conn.execute("UPDATE _dedupe_groups SET survivor_id='mem_ghost_999'")
            conn.execute(
                "INSERT INTO _dedupe_groups(survivor_id,content,source_agent,session_id,role,n) "
                "VALUES('mem_gone_000','已经全部删除的旧内容','codex','s9','user',2)")

    def test_stale_dedupe_groups_cannot_delete_the_survivor(self):
        text = "崩溃窗口探针：同一会话内的重复 raw 行"
        self._legacy_rows(3, "s1", text)
        self._seed_stale_groups(text)
        with sqlite3.connect(self.path) as conn:
            stale_groups = conn.execute("SELECT COUNT(*) FROM _dedupe_groups").fetchone()[0]
        self.assertEqual(stale_groups, 2, "探针前置条件：陈旧快照里有 2 个组")

        result = self.ledger.collapse_duplicate_rows(archive_dir=self.archive_dir)

        self.assertIsNone(result.get("error"))
        # Honest answer: the 3 live copies form exactly one group, and collapsing
        # it removes exactly 2 rows.  The stale snapshot would have reported 2
        # groups and deleted all 3 (measured pre-fix: duplicate_groups=2,
        # collapsed=4, zero rows left).
        self.assertEqual(result["duplicate_groups"], 1,
                         "报告了陈旧快照里的组数，而不是当前库里的真实组数")
        self.assertEqual(result["collapsed"], 2,
                         "按陈旧快照删除，删掉了本应幸存的副本")
        survivors = self._raw_ids()
        self.assertEqual(survivors, ["mem_s1_002"],
                         "折叠后必须恰好剩 1 行，且是稳定排序（MAX memory_id）选出的幸存者")
        # The survivor is untouched, the two losers are in the verified archive.
        archived = {record["memory_id"] for record in _read_archive(result["archive_path"])}
        self.assertEqual(archived, {"mem_s1_000", "mem_s1_001"})

    def test_stale_dedupe_groups_does_not_survive_the_run(self):
        """Whatever happens, the persistent snapshot table is gone afterwards."""
        text = "残留临时表探针"
        self._legacy_rows(3, "s1", text)
        self._seed_stale_groups(text)
        self.ledger.collapse_duplicate_rows(archive_dir=self.archive_dir)
        with sqlite3.connect(self.path) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("_dedupe_groups", tables, "运行结束后不得残留临时表")

    def test_stale_dedupe_groups_is_rebuilt_on_dry_run_too(self):
        text = "dry-run 也必须自建快照"
        self._legacy_rows(3, "s1", text)
        self._seed_stale_groups(text)
        result = self.ledger.collapse_duplicate_rows(dry_run=True)
        self.assertEqual(result["duplicate_groups"], 1,
                         "dry_run 报告的是陈旧快照的组数")
        self.assertEqual(self.ledger.stats()["memories"], 3, "dry_run 不得删除任何行")
        with sqlite3.connect(self.path) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("_dedupe_groups", tables)

    def test_repeated_collapse_is_stable(self):
        """A second run has nothing left to collapse and changes nothing."""
        self._legacy_rows(3, "s1", "重复折叠稳定性")
        first = self.ledger.collapse_duplicate_rows(archive_dir=self.archive_dir)
        self.assertEqual((first["duplicate_groups"], first["collapsed"]), (1, 2))
        second = self.ledger.collapse_duplicate_rows(archive_dir=self.archive_dir)
        self.assertEqual((second["duplicate_groups"], second["collapsed"]), (0, 0))
        self.assertEqual(self._raw_ids(), ["mem_s1_002"])


if __name__ == "__main__":
    unittest.main()
