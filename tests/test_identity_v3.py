"""identity v3 回归：显式事件 ID、流式修订 supersede、v2 兼容探测。

锁定 WP-E 的验收口径：

1. **不同 event_id 的相同文本保留两条**：raw 身份键加入显式来源事件 ID 后，
   两个真实事件即使正文一样也是两次观测（append-only 契约）。
2. **同 event_id 重放幂等**：同一事件同内容重复采集只写一行、返回同一 memory_id。
3. **同 event_id 不同内容旧行 superseded**：流式 bubble 的草稿行在事务内被下线
   （lifecycle/superseded_by/valid_until + DELETE FTS），search 只返回完整版本。
4. **无 event_id 保持 v2 去重行为**：采集时间漂移不产生新身份。
5. **采集适配器真的给了稳定 ID**：cursor composer / pi JSONL 的流式记录带
   external_id，修订后热索引里只剩一条可召回。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hippomind import cursor_capture, pi_capture  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind.ledger.identity import (  # noqa: E402
    IDENTITY_VERSION, IDENTITY_VERSION_LEGACY, IDENTITY_VERSION_V2,
    IdentityMixin)

STREAM_AGENT = "cursor"
STREAM_SCOPE = "agent:cursor"
STREAM_SESSION = "composer-1"
STREAM_EVENT = "cursor:bubble:composer-1:b1"

PARTIAL = ("流式回答草稿：生产数据库迁移方案的第一段，端口与备份步骤已经列出，"
           "但最终校验尚未完成。")
FULL = PARTIAL + "最终完整版本补充：三个校验项全部通过，可以上线。"


def raw_record(content, *, event_id="", timestamp="2026-05-10T10:30:00+00:00",
               session=STREAM_SESSION, role="assistant", evidence=None, **extra):
    """Cursor 风格的流式 raw 记录（scope/session/evidence 与采集器一致）。"""
    record = {
        "content": content, "kind": "raw_chat", "raw_chat": True,
        "source_agent": STREAM_AGENT, "session_id": session, "role": role,
        "scope": STREAM_SCOPE,
        "evidence": [evidence or ("cursor:bubble:%s:b1" % session)],
        "timestamp": timestamp,
    }
    if event_id:
        record["event_id"] = event_id
    record.update(extra)
    return record


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-idv3-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "identity.sqlite")
        self.addCleanup(self.ledger.close)

    def count(self, where="", params=()) -> int:
        with self.ledger._reading() as conn:
            sql = "SELECT COUNT(*) FROM memories"
            if where:
                sql += " WHERE " + where
            return int(conn.execute(sql, params).fetchone()[0])

    def row(self, memory_id):
        with self.ledger._reading() as conn:
            return conn.execute("SELECT * FROM memories WHERE memory_id=?",
                                (memory_id,)).fetchone()

    def active_rows(self):
        with self.ledger._reading() as conn:
            return conn.execute(
                "SELECT * FROM memories WHERE lifecycle='active' ORDER BY created_at"
            ).fetchall()

    def fts_ids(self):
        with self.ledger._reading() as conn:
            return {str(row[0]) for row in conn.execute(
                "SELECT memory_id FROM memory_fts").fetchall()}


class EventIdDiscriminatorTests(LedgerCase):
    """v3 核心：显式事件 ID 区分同文本的不同事件。"""

    def test_different_event_ids_with_same_text_stay_two_rows(self):
        first = self.ledger.ingest(raw_record("好的，收到这条相同文本的消息",
                                              event_id="evt-a"))
        second = self.ledger.ingest(raw_record("好的，收到这条相同文本的消息",
                                               event_id="evt-b"))
        self.assertTrue(first.inserted)
        self.assertTrue(second.inserted, "不同来源事件即使文本相同也必须共存")
        self.assertNotEqual(first.memory_id, second.memory_id)
        self.assertEqual(2, self.count())

    def test_same_event_id_replay_is_idempotent(self):
        first = self.ledger.ingest(raw_record(PARTIAL, event_id=STREAM_EVENT))
        replay = self.ledger.ingest(raw_record(PARTIAL, event_id=STREAM_EVENT,
                                              timestamp="2026-05-10T11:00:00+00:00"))
        self.assertTrue(first.inserted)
        self.assertFalse(replay.inserted, "同一事件的同内容重放必须幂等")
        self.assertEqual(first.memory_id, replay.memory_id)
        self.assertEqual(1, self.count())

    def test_external_id_participates_in_the_raw_identity_key(self):
        with_id = self.ledger._prepare_ingest(raw_record(PARTIAL, event_id="evt-1"))
        without_id = self.ledger._prepare_ingest(raw_record(PARTIAL))
        other_id = self.ledger._prepare_ingest(raw_record(PARTIAL, event_id="evt-2"))
        self.assertNotEqual(with_id["content_hash"], other_id["content_hash"],
                            "不同事件 ID 必须得到不同身份")
        self.assertNotEqual(with_id["content_hash"], without_id["content_hash"])
        self.assertEqual(with_id["compat_hash"], without_id["compat_hash"],
                         "v3 才加入 external_id：v2 兼容 digest 必须与无 ID 记录一致")
        self.assertEqual("evt-1", with_id["identity_external_id"])
        self.assertIn("evt-1", with_id["metadata_json"],
                      "事件 ID 必须投影到 metadata，供修订链查找")

    def test_unanchored_same_event_revision_is_still_superseded(self):
        """没有 session/task 锚点也要 supersede：回退到有界最新窗口探测。"""
        base = {"kind": "raw_chat", "raw_chat": True, "source_agent": "cursor",
                "role": "assistant", "scope": "agent:cursor",
                "evidence": ["cursor:bubble:x:y"],
                "timestamp": "2026-05-10T10:30:00+00:00"}
        first = self.ledger.ingest({**base, "content": PARTIAL,
                                    "event_id": "evt-unanchored"})
        second = self.ledger.ingest({**base, "content": FULL,
                                     "event_id": "evt-unanchored",
                                     "timestamp": "2026-05-10T10:31:00+00:00"})
        self.assertTrue(second.inserted)
        self.assertEqual("superseded", self.row(first.memory_id)["lifecycle"])
        self.assertEqual("active", self.row(second.memory_id)["lifecycle"])

    def test_no_event_id_keeps_v2_dedup_behaviour(self):
        first = self.ledger.ingest(raw_record(PARTIAL,
                                              timestamp="2026-01-01T00:00:00+00:00"))
        second = self.ledger.ingest(raw_record(PARTIAL,
                                               timestamp="2026-02-01T00:00:00+00:00"))
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted, "无事件 ID 时采集时间漂移不得分叉身份")
        self.assertEqual(first.memory_id, second.memory_id)
        self.assertEqual(1, self.count())


class StreamRevisionSupersedeTests(LedgerCase):
    """流式修订：同一事件的新内容让旧行下线，不再并存。"""

    def _ingest_revision_chain(self):
        partial = self.ledger.ingest(raw_record(PARTIAL, event_id=STREAM_EVENT))
        full = self.ledger.ingest(raw_record(FULL, event_id=STREAM_EVENT,
                                             timestamp="2026-05-10T10:31:00+00:00"))
        return partial, full

    def test_new_content_supersedes_the_previous_revision(self):
        partial, full = self._ingest_revision_chain()
        self.assertTrue(partial.inserted)
        self.assertTrue(full.inserted, "内容变化是新修订，不是重复")
        old = self.row(partial.memory_id)
        self.assertEqual("superseded", old["lifecycle"])
        self.assertEqual(full.memory_id, old["superseded_by"])
        self.assertEqual("superseded", old["quality_state"])
        self.assertEqual("active", self.row(full.memory_id)["lifecycle"])

    def test_superseded_revision_leaves_the_hot_index(self):
        partial, full = self._ingest_revision_chain()
        self.assertNotIn(partial.memory_id, self.fts_ids())
        self.assertIn(full.memory_id, self.fts_ids())

    def test_search_returns_only_the_complete_revision(self):
        partial, full = self._ingest_revision_chain()
        hits = self.ledger.search("最终完整版本", scope=STREAM_SCOPE, limit=10,
                                  include_raw_chat=True)
        self.assertEqual([FULL], [hit["content"] for hit in hits])
        self.assertEqual(full.memory_id, hits[0]["memory_id"])
        hits = self.ledger.search("流式回答草稿", scope=STREAM_SCOPE, limit=10,
                                  include_raw_chat=True)
        self.assertEqual([FULL], [hit["content"] for hit in hits],
                         "草稿关键词也只能命中完整版本")

    def test_retired_revision_gets_a_closed_valid_window(self):
        partial, _full = self._ingest_revision_chain()
        old = self.row(partial.memory_id)
        self.assertNotEqual("", old["valid_until"])
        self.assertGreater(old["valid_until"], old["valid_from"])

    def test_supersede_does_not_touch_other_events(self):
        other = self.ledger.ingest(raw_record(
            "同一会话里的另一条完整消息内容",
            event_id="cursor:bubble:composer-1:b2",
            evidence="cursor:bubble:composer-1:b2"))
        self._ingest_revision_chain()
        self.assertEqual("active", self.row(other.memory_id)["lifecycle"])
        self.assertIn(other.memory_id, self.fts_ids())

    def test_batch_with_two_revisions_keeps_only_the_last_active(self):
        results = self.ledger.ingest_many([
            raw_record(PARTIAL, event_id=STREAM_EVENT),
            raw_record(FULL, event_id=STREAM_EVENT,
                       timestamp="2026-05-10T10:31:00+00:00"),
        ])
        self.assertEqual(2, self.count(), "两行都写入了账本（append-only）")
        self.assertEqual([True, True], [item.inserted for item in results])
        self.assertEqual(1, len(self.active_rows()), "但热集合里只有一个修订")
        self.assertEqual(FULL, self.active_rows()[0]["content"])
        self.assertEqual([results[1].memory_id], sorted(self.fts_ids()))

    def test_supersede_retires_claims_of_the_losing_revision(self):
        draft = self.ledger.ingest({
            "content": "部署方案草稿版本，状态仍是草稿", "kind": "fact",
            "source_agent": "tester", "scope": "shared", "evidence": ["deploy:1"],
            "event_id": "deploy-plan",
            "claims": [{"subject": "deploy", "predicate": "status",
                        "slot_key": "phase", "value": "draft"}],
        })
        final = self.ledger.ingest({
            "content": "部署方案完整版本，校验通过", "kind": "fact",
            "source_agent": "tester", "scope": "shared", "evidence": ["deploy:1"],
            "event_id": "deploy-plan",
            "claims": [{"subject": "deploy", "predicate": "status",
                        "slot_key": "phase", "value": "verified"}],
        })
        self.assertTrue(final.inserted)
        active = self.ledger.list_claims(scope="shared", state="active")
        self.assertEqual([final.memory_id], [item["memory_id"] for item in active])
        with self.ledger._reading() as conn:
            retired = conn.execute(
                "SELECT state, valid_until FROM memory_claims WHERE memory_id=?",
                (draft.memory_id,)).fetchall()
        self.assertEqual(["superseded"], [item["state"] for item in retired])

    def test_replay_of_the_winning_revision_is_idempotent(self):
        _partial, full = self._ingest_revision_chain()
        again = self.ledger.ingest(raw_record(FULL, event_id=STREAM_EVENT,
                                             timestamp="2026-05-10T10:33:00+00:00"))
        self.assertFalse(again.inserted)
        self.assertEqual(full.memory_id, again.memory_id)
        self.assertEqual(1, len(self.active_rows()))


class StructuredEventIdTests(LedgerCase):
    """structured 记录的 external_id 自 v2 起就在身份键里；v3 只补修订链。"""

    def test_same_structured_event_id_with_new_content_supersedes(self):
        first = self.ledger.ingest({
            "content": "结构化事件第一版内容", "kind": "fact",
            "source_agent": "tester", "scope": "shared", "evidence": ["e:1"],
            "event_id": "structured-evt"})
        second = self.ledger.ingest({
            "content": "结构化事件第二版完整内容", "kind": "fact",
            "source_agent": "tester", "scope": "shared", "evidence": ["e:1"],
            "event_id": "structured-evt"})
        self.assertTrue(second.inserted)
        self.assertEqual("superseded", self.row(first.memory_id)["lifecycle"])
        self.assertEqual("active", self.row(second.memory_id)["lifecycle"])
        hits = self.ledger.search("第二版完整内容", scope="shared", limit=5)
        self.assertEqual([second.memory_id], [hit["memory_id"] for hit in hits])


class IdentityVersionContractTests(LedgerCase):
    def test_version_constants(self):
        self.assertEqual(3, IDENTITY_VERSION)
        self.assertEqual(2, IDENTITY_VERSION_V2)
        self.assertEqual(1, IDENTITY_VERSION_LEGACY)

    def test_v1_probe_still_reproduces_a_v1_row(self):
        prepared = self.ledger._prepare_ingest(raw_record(PARTIAL, event_id=STREAM_EVENT))
        legacy_hash = IdentityMixin._legacy_identity_hash({
            "raw_chat": prepared["raw_chat"], "source_agent": prepared["source_agent"],
            "session_id": prepared["session_id"], "role": prepared["role"],
            "content": prepared["content"], "kind": prepared["kind"],
            "task_id": prepared["task_id"], "project": prepared["project"],
            "scope": prepared["scope"], "valid_from": prepared["valid_from"],
            "valid_until": prepared["valid_until"]})
        # v1 从不哈希 external_id：v3 的 legacy_hash 必须仍与重建值一致。
        self.assertEqual(legacy_hash, prepared["legacy_hash"])


class CursorCaptureRevisionTests(unittest.TestCase):
    """端到端：Cursor composer bubble 原地补全后只剩一条可召回。"""

    COMPOSER = "11111111-1111-1111-1111-111111111111"
    BUBBLE = "aaaaaaaa-0000-0000-0000-000000000001"
    PARTIAL = "流式补全过程中的半截答案，第一段"
    FULL = "流式补全过程中的完整答案，第一段与最终校验说明全部到齐。"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-cursor-v3-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.db = self.root / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        self.db.parent.mkdir(parents=True)
        self._write_bubble(self.PARTIAL)

    def _write_bubble(self, text):
        payload = json.dumps({"_v": 3, "type": 2, "text": text,
                              "createdAt": "2026-05-10T10:30:00Z"},
                             ensure_ascii=False)
        if not self.db.exists():
            conn = sqlite3.connect(self.db)
            try:
                conn.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value BLOB)")
                conn.execute("CREATE TABLE ItemTable(key TEXT PRIMARY KEY, value BLOB)")
                conn.executemany(
                    "INSERT INTO cursorDiskKV(key,value) VALUES(?,?)",
                    [("bubbleId:%s:%s" % (self.COMPOSER, self.BUBBLE), payload)])
                conn.commit()
            finally:
                conn.close()
            return
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE cursorDiskKV SET value=? WHERE key=?",
                         (payload, "bubbleId:%s:%s" % (self.COMPOSER, self.BUBBLE)))
            conn.commit()
        finally:
            conn.close()

    def _scanner(self):
        return cursor_capture.CursorComposerScanner(self.ledger, [self.db])

    def _rows(self):
        with self.ledger._reading() as conn:
            return conn.execute(
                "SELECT * FROM memories ORDER BY created_at").fetchall()

    def test_streaming_bubble_keeps_one_recallable_version(self):
        self._scanner().sync(max_messages=20)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertEqual("cursor:bubble:%s:%s" % (self.COMPOSER, self.BUBBLE),
                         metadata["external_id"])
        self.assertIn("content_revision", metadata)
        self._write_bubble(self.FULL)
        self._scanner().sync(max_messages=20)
        rows = self._rows()
        self.assertEqual(2, len(rows), "旧修订保留在账本里（append-only）")
        active = [row for row in rows if row["lifecycle"] == "active"]
        self.assertEqual(1, len(active), "热集合里只能有一条")
        self.assertEqual(self.FULL, active[0]["content"])
        hits = self.ledger.search("最终校验说明", scope="agent:cursor", limit=10,
                                  include_raw_chat=True)
        self.assertEqual([self.FULL], [hit["content"] for hit in hits])

    def test_two_bubbles_with_identical_text_stay_two_rows(self):
        conn = sqlite3.connect(self.db)
        try:
            payload = json.dumps({"_v": 3, "type": 1, "text": "同文本的另一条消息",
                                  "createdAt": "2026-05-10T10:40:00Z"},
                                 ensure_ascii=False)
            conn.execute("INSERT INTO cursorDiskKV(key,value) VALUES(?,?)",
                         ("bubbleId:%s:bbbb" % self.COMPOSER, payload))
            conn.commit()
        finally:
            conn.close()
        self._scanner().sync(max_messages=20)
        same = [row for row in self._rows() if row["content"] == "同文本的另一条消息"]
        self.assertEqual(1, len(same))
        # 再写一个不同 bubble id、相同文本的 user 气泡
        conn = sqlite3.connect(self.db)
        try:
            payload = json.dumps({"_v": 3, "type": 1, "text": "同文本的另一条消息",
                                  "createdAt": "2026-05-10T10:41:00Z"},
                                 ensure_ascii=False)
            conn.execute("INSERT INTO cursorDiskKV(key,value) VALUES(?,?)",
                         ("bubbleId:%s:cccc" % self.COMPOSER, payload))
            conn.commit()
        finally:
            conn.close()
        self._scanner().sync(max_messages=20)
        same = [row for row in self._rows() if row["content"] == "同文本的另一条消息"]
        self.assertEqual(2, len(same), "两个不同 bubble 是两条独立事件")


class PiCaptureEventIdTests(unittest.TestCase):
    """端到端：Pi JSONL 的 entry id 进入 raw 身份，重发修订 supersede 旧行。"""

    SESSION = "01a0b02b-0a17-72bc-b137-65b9cadad13b"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-pi-v3-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.sessions = self.root / "sessions"
        self.workdir = self.sessions / "--work-atlas--"
        self.workdir.mkdir(parents=True)
        self.path = self.workdir / ("2026-10-23T10-00-00-000Z_" + self.SESSION + ".jsonl")
        self.scanner = pi_capture.PiSessionScanner(self.ledger, roots=[self.sessions])
        self._write([self._message("u1", "请记住：项目使用 SQLite")])

    def _message(self, entry_id, text):
        return {"type": "message", "id": entry_id, "timestamp": "2026-10-23T10:00:05Z",
                "message": {"role": "user",
                            "content": [{"type": "text", "text": text}]}}

    def _write(self, messages):
        lines = [{"type": "session", "version": 3, "id": self.SESSION,
                  "timestamp": "2026-10-23T10:00:00Z", "cwd": "/work/atlas"}]
        lines.extend(messages)
        self.path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n"
                                     for item in lines), encoding="utf-8")

    def _rows(self):
        with self.ledger._reading() as conn:
            return conn.execute("SELECT * FROM memories ORDER BY created_at").fetchall()

    def test_entry_id_is_projected_and_replay_is_idempotent(self):
        self.scanner.sync(max_messages=20)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertEqual("pi:message:%s:u1" % self.SESSION, metadata["external_id"])
        second = self.scanner.sync(max_messages=20)
        self.assertEqual(0, second["inserted"])
        self.assertEqual(1, len(self._rows()))

    def test_resend_with_same_entry_id_supersedes_the_old_content(self):
        self.scanner.sync(max_messages=20)
        # Pi 的 resend/编辑流程会在文件尾部再写一条同 id 的新版本记录。
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._message("u1", "请记住：项目迁移到 SQLite WAL 模式"),
                                   ensure_ascii=False) + "\n")
        summary = self.scanner.sync(max_messages=20)
        self.assertEqual(1, summary["inserted"])
        rows = self._rows()
        active = [row for row in rows if row["lifecycle"] == "active"]
        self.assertEqual(1, len(active), "同 entry id 的修订不得双活")
        self.assertIn("WAL", active[0]["content"])


if __name__ == "__main__":
    unittest.main()
