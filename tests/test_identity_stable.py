"""identity key 稳定性回归测试（v2/v3 / 向后兼容 / 双时态）。

锁定生产事故的根因与修复，防止回退：

1. **根因**：v1 把 ``valid_from`` 放进 hash，而采集侧传的是 ``timestamp``
   （由源文件 mtime 推导），每次采集都变 → 每次新 identity → 重复入库。
   生产实测：同一条 Gemini 会话摘要存了 141 份，content 与 evidence 全同，
   141 个不同 ``valid_from``；用 v1 公式重建 141/141 命中，剔除 valid_from
   后塌缩成 1 条。
2. **v2 修复**：只有**显式** ``valid_from`` 参与 identity；派生时间不参与。
3. **向后兼容**：旧 v1 行仍被识别为重复（语义探测 + 重建 v1 hash 确认），
   不会因为升级导致全库重新入库。
4. **双时态不退化**：显式 ``valid_from`` 不同仍是两次观测。
5. **v3 兼容探测**：上一发布版本（v2）写下的行仍被识别为重复；raw 行升级后
   补上事件 ID，后续修订可继续 supersede。v3 新增行为见
   ``tests/test_identity_v3.py``。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind.ledger.identity import (  # noqa: E402
    IDENTITY_VERSION, IDENTITY_VERSION_KEY, IDENTITY_VERSION_LEGACY,
    IDENTITY_VERSION_V2, IdentityMixin)

#: 生产事故里的那条内容（141 份重复）。
GEMINI_CONTENT = (
    "我使用临时 root 之后，使用 AI 帮我优化三星手机系统，但是好像把三星手表相关的"
    "东西给我直接关闭了，导致我现在使用不了手表。连接不了手表，帮我看一下怎么回事，"
    "直接操控我的手机看一下。")

CONVERSATION_ID = "1922574a-8409-405c-9ee8-0a81c9df6402"


def gemini_record(timestamp: str, step_count: int, **extra):
    """复刻 gemini_capture 产出的记录形态（timestamp 来自源文件 mtime）。"""
    record = {
        "content": GEMINI_CONTENT,
        "kind": "task",
        "source_agent": "gemini",
        "scope": "shared",
        "task_id": CONVERSATION_ID,
        "project": "outside-of-project",
        "evidence": ["gemini:" + CONVERSATION_ID],
        "timestamp": timestamp,
        "metadata": {
            "source": "gemini-antigravity-summary",
            "conversation_id": CONVERSATION_ID,
            "source_modified_time": timestamp,
            "step_count": step_count,
            "status": "CASCADE_RUN_STATUS_RUNNING",
        },
    }
    record.update(extra)
    return record


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-identity-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "identity.sqlite")
        self.addCleanup(self.ledger.close)

    def count(self) -> int:
        with self.ledger._reading() as conn:
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


class VolatileTimestampTests(LedgerCase):
    """核心回归：观察时间漂移不得产生新 identity。"""

    def test_refreshed_timestamp_does_not_create_a_new_row(self):
        """生产事故的最小复现：同一内容，timestamp 每次不同。"""
        ids = set()
        for index in range(5):
            result = self.ledger.ingest(gemini_record(
                "2026-10-24T04:5%d:22.099622+00:00" % index, 58 + index))
            ids.add(result.memory_id)
        self.assertEqual(1, len(ids), "timestamp 漂移不得产生新 identity")
        self.assertEqual(1, self.count(), "同一内容只能存一行")

    def test_only_the_first_ingest_reports_inserted(self):
        first = self.ledger.ingest(gemini_record("2026-10-24T04:52:22Z", 58))
        second = self.ledger.ingest(gemini_record("2026-10-24T05:53:18Z", 63))
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted, "重复观测必须报告为未插入")
        self.assertEqual(first.memory_id, second.memory_id)

    def test_metadata_drift_alone_does_not_create_a_new_row(self):
        """metadata 本就不参与 hash —— 这条锁住"别把它加回去"。"""
        base = gemini_record("2026-10-24T04:52:22Z", 58)
        first = self.ledger.ingest(base)
        second = self.ledger.ingest({**base, "metadata": {
            "source": "gemini-antigravity-summary", "step_count": 9999,
            "build": "another", "source_modified_time": "2030-01-01T00:00:00Z"}})
        self.assertFalse(second.inserted)
        self.assertEqual(first.memory_id, second.memory_id)

    def test_explicit_valid_from_still_creates_bitemporal_observations(self):
        """显式 valid_from 是真实断言：两个日期必须仍是两次观测。"""
        base = {"content": "部署端口是 9000", "kind": "fact",
                "source_agent": "t", "scope": "shared", "evidence": ["e1"]}
        first = self.ledger.ingest({**base, "valid_from": "2026-10-01T00:00:00Z"})
        second = self.ledger.ingest({**base, "valid_from": "2026-10-02T00:00:00Z"})
        repeat = self.ledger.ingest({**base, "valid_from": "2026-10-01T00:00:00Z"})
        self.assertTrue(first.inserted)
        self.assertTrue(second.inserted, "显式双时态不得被合并")
        self.assertFalse(repeat.inserted, "同一显式 valid_from 必须幂等")
        self.assertEqual(2, self.count())

    def test_raw_chat_is_stable_across_capture_time(self):
        base = {"content": "用户问：端口是多少", "kind": "raw_chat",
                "raw_chat": True, "source_agent": "codex", "session_id": "s-1",
                "role": "user", "evidence": ["codex:s-1"]}
        first = self.ledger.ingest({**base, "timestamp": "2026-10-24T01:00:00Z"})
        second = self.ledger.ingest({**base, "timestamp": "2026-10-24T09:00:00Z"})
        self.assertFalse(second.inserted)
        self.assertEqual(first.memory_id, second.memory_id)


class IdentityDiscriminatorTests(LedgerCase):
    """该区分的必须区分：不同内容 / session / role / task / scope。"""

    def test_different_content_is_a_different_identity(self):
        first = self.ledger.ingest(gemini_record("2026-10-24T04:52:22Z", 58))
        second = self.ledger.ingest({**gemini_record("2026-10-24T04:52:22Z", 58),
                                     "content": "完全不同的另一句话"})
        self.assertTrue(second.inserted)
        self.assertNotEqual(first.memory_id, second.memory_id)

    def test_different_task_is_a_different_identity(self):
        first = self.ledger.ingest(gemini_record("2026-10-24T04:52:22Z", 58))
        second = self.ledger.ingest({**gemini_record("2026-10-24T04:52:22Z", 58),
                                     "task_id": "another-conversation",
                                     "evidence": ["gemini:another"]})
        self.assertTrue(second.inserted)
        self.assertNotEqual(first.memory_id, second.memory_id)

    def test_different_session_is_a_different_identity(self):
        base = {"content": "同一句话", "kind": "raw_chat", "raw_chat": True,
                "source_agent": "codex", "role": "user", "evidence": ["e"]}
        first = self.ledger.ingest({**base, "session_id": "s-1"})
        second = self.ledger.ingest({**base, "session_id": "s-2"})
        self.assertTrue(second.inserted)
        self.assertNotEqual(first.memory_id, second.memory_id)

    def test_different_role_is_a_different_identity_for_raw_chat(self):
        base = {"content": "同一句话", "kind": "raw_chat", "raw_chat": True,
                "source_agent": "codex", "session_id": "s-1", "evidence": ["e"]}
        first = self.ledger.ingest({**base, "role": "user"})
        second = self.ledger.ingest({**base, "role": "assistant"})
        self.assertTrue(second.inserted, "user 与 assistant 是不同的观测")
        self.assertNotEqual(first.memory_id, second.memory_id)

    def test_different_scope_is_a_different_identity(self):
        base = {"content": "同一句话", "kind": "fact", "source_agent": "t",
                "evidence": ["e"]}
        first = self.ledger.ingest({**base, "scope": "shared"})
        second = self.ledger.ingest({**base, "scope": "private"})
        self.assertTrue(second.inserted)
        self.assertNotEqual(first.memory_id, second.memory_id)


class BackwardCompatibilityTests(LedgerCase):
    """升级不得让历史行被重新入库。"""

    def _write_v1_row(self, record) -> str:
        """把一条记录以 v1 形态写入账本（模拟升级前的历史数据）。"""
        prepared = self.ledger._prepare_ingest(record)
        legacy_hash = IdentityMixin._legacy_identity_hash({
            "raw_chat": prepared["raw_chat"],
            "source_agent": prepared["source_agent"],
            "session_id": prepared["session_id"],
            "role": prepared["role"],
            "content": prepared["content"],
            "kind": prepared["kind"],
            "task_id": prepared["task_id"],
            "project": prepared["project"],
            "scope": prepared["scope"],
            "valid_from": prepared["valid_from"],
            "valid_until": prepared["valid_until"],
        })
        legacy = dict(prepared)
        legacy["content_hash"] = legacy_hash
        legacy["event_id"] = "evt_" + legacy_hash[:32]
        legacy["memory_id"] = "mem_" + legacy_hash[:32]
        self.ledger._transaction(
            lambda conn: self.ledger._insert_prepared(conn, legacy))
        return legacy["memory_id"]

    def _write_v2_row(self, record) -> str:
        """把一条记录以 v2 形态写入账本（模拟上一发布版本的数据）。"""
        prepared = self.ledger._prepare_ingest(record)
        v2_hash = prepared["compat_hash"]
        legacy = dict(prepared)
        legacy["content_hash"] = v2_hash
        legacy["event_id"] = "evt_" + v2_hash[:32]
        legacy["memory_id"] = "mem_" + v2_hash[:32]
        self.ledger._transaction(
            lambda conn: self.ledger._insert_prepared(conn, legacy))
        return legacy["memory_id"]

    def test_v2_row_is_recognised_as_duplicate_after_upgrade(self):
        """升级不得重灌上一发布版本已经写下的行。"""
        original = self._write_v2_row(gemini_record("2026-10-24T04:52:22Z", 58))
        before = self.count()
        result = self.ledger.ingest(gemini_record("2026-10-25T09:00:00+00:00", 999))
        self.assertFalse(result.inserted, "v2 行必须被兼容探测识别为重复")
        self.assertEqual(before, self.count())
        self.assertEqual(original, result.memory_id)

    def test_v2_row_is_not_merged_with_distinct_content(self):
        self._write_v2_row(gemini_record("2026-10-24T04:52:22Z", 58))
        before = self.count()
        result = self.ledger.ingest({"content": "同一会话里的另一句话", "kind": "task",
                                       "source_agent": "gemini", "scope": "shared",
                                       "task_id": CONVERSATION_ID,
                                       "evidence": ["gemini:other"],
                                       "timestamp": "2026-10-25T09:00:00Z"})
        self.assertTrue(result.inserted)
        self.assertEqual(before + 1, self.count())

    def test_raw_v2_row_is_tagged_with_event_id_and_then_superseded(self):
        """v2 raw 行没有事件 ID 投影；v3 首次重放要补标签，之后修订才能 supersede。"""
        base = {"content": "流式回答草稿，第一段已经写好", "kind": "raw_chat",
                "raw_chat": True, "source_agent": "cursor", "session_id": "s-1",
                "role": "assistant", "evidence": ["cursor:bubble:s-1:b1"]}
        original = self._write_v2_row({**base, "timestamp": "2026-05-10T10:30:00Z"})
        tagged = self.ledger.ingest({**base, "timestamp": "2026-05-10T10:30:00Z",
                                      "event_id": "cursor:bubble:s-1:b1"})
        self.assertFalse(tagged.inserted, "同一内容的 v2 行必须被识别为重复")
        self.assertEqual(original, tagged.memory_id)
        with self.ledger._reading() as conn:
            row = conn.execute("SELECT metadata_json FROM memories WHERE memory_id=?",
                               (original,)).fetchone()
        self.assertIn("cursor:bubble:s-1:b1", str(row["metadata_json"]),
                      "重复探测必须把事件 ID 补进 v2 行的投影，否则后续修订找不到它")
        full = self.ledger.ingest({**base, "content": base["content"] +
                                  "，第二段完整版本校验通过",
                                  "timestamp": "2026-05-10T10:31:00Z",
                                  "event_id": "cursor:bubble:s-1:b1"})
        self.assertTrue(full.inserted)
        with self.ledger._reading() as conn:
            row = conn.execute("SELECT lifecycle, superseded_by FROM memories WHERE memory_id=?",
                               (original,)).fetchone()
        self.assertEqual("superseded", row["lifecycle"])
        self.assertEqual(full.memory_id, row["superseded_by"])

    def test_prepared_record_carries_compat_hash_and_external_id(self):
        prepared = self.ledger._prepare_ingest({
            "content": "显式事件消息", "kind": "raw_chat", "raw_chat": True,
            "source_agent": "cursor", "session_id": "s-1", "role": "user",
            "evidence": ["cursor:bubble:s-1:b2"], "event_id": "evt-42"})
        self.assertEqual("evt-42", prepared["identity_external_id"])
        self.assertNotEqual(prepared["content_hash"], prepared["compat_hash"],
                            "v3 与 v2 digest 必须不同，否则无法区分两个世代")
        self.assertIn("evt-42", prepared["metadata_json"])
    def test_legacy_identity_hash_reproduces_a_v1_row(self):
        record = gemini_record("2026-10-24T04:52:22Z", 58)
        memory_id = self._write_v1_row(record)
        with self.ledger._reading() as conn:
            row = conn.execute(
                "SELECT m.*, e.content_hash AS _eh FROM memories m "
                "JOIN ledger_events e ON e.event_id=m.origin_event_id "
                "WHERE m.memory_id=?", (memory_id,)).fetchone()
        self.assertEqual(IdentityMixin._legacy_identity_hash(row), row["_eh"])

    def test_reingesting_a_legacy_row_is_recognised_as_duplicate(self):
        """升级后第一次采集：历史行必须仍被认作重复，而不是新增一行。"""
        original = self._write_v1_row(gemini_record("2026-10-24T04:52:22Z", 58))
        before = self.count()
        result = self.ledger.ingest(gemini_record(
            "2026-10-25T09:00:00+00:00", 999))
        self.assertFalse(result.inserted, "历史行必须被识别为重复")
        self.assertEqual(before, self.count(), "不得新增行")
        self.assertEqual(original, result.memory_id, "应返回历史行的 memory_id")

    def test_legacy_probe_is_idempotent_across_passes(self):
        self._write_v1_row(gemini_record("2026-10-24T04:52:22Z", 58))
        before = self.count()
        for index in range(4):
            self.ledger.ingest(gemini_record(
                "2026-10-2%dT12:00:00+00:00" % (5 + index), 100 + index))
        self.assertEqual(before, self.count(), "多轮采集不得累积重复行")

    def test_legacy_probe_does_not_merge_distinct_content(self):
        """语义探测必须比对规范化内容，不能把同 task 的不同句子合并。"""
        self._write_v1_row(gemini_record("2026-10-24T04:52:22Z", 58))
        before = self.count()
        result = self.ledger.ingest({**gemini_record(
            "2026-10-25T09:00:00+00:00", 999), "content": "同一会话里的另一句话"})
        self.assertTrue(result.inserted, "不同内容必须插入新行")
        self.assertEqual(before + 1, self.count())

    def test_legacy_probe_does_not_merge_two_v2_rows(self):
        """两条 v2 行即使内容相同也不得被探测合并（v1 hash 不会匹配）。"""
        first = self.ledger.ingest(gemini_record("2026-10-24T04:52:22Z", 58))
        self.assertTrue(first.inserted)
        with self.ledger._reading() as conn:
            row = conn.execute(
                "SELECT m.*, e.content_hash AS _eh FROM memories m "
                "JOIN ledger_events e ON e.event_id=m.origin_event_id "
                "WHERE m.memory_id=?", (first.memory_id,)).fetchone()
        self.assertNotEqual(IdentityMixin._legacy_identity_hash(row), row["_eh"],
                            "v2 行不得能重建出自己的 v1 hash（否则探测会误合并）")

    def test_v1_and_v2_identity_differ_for_the_same_record(self):
        record = gemini_record("2026-10-24T04:52:22Z", 58)
        prepared = self.ledger._prepare_ingest(record)
        self.assertNotEqual(prepared["content_hash"], prepared["legacy_hash"],
                            "v2 必须与 v1 不同，否则无法区分新旧")


class AnchorDisambiguationTests(LedgerCase):
    """派生观测时间只在**没有稳定来源锚点**时才参与 identity。

    这条分界线同时满足两个契约：

    * 带 ``task_id`` / ``session_id`` 的记录是"关于某对象的一条陈述"，
      重读同一对象（mtime 变了）仍是同一条 -> 不重复入库（本次修复）。
    * 不带任何锚点的记录是"某时刻的一次独立断言"，观测时间就是唯一区分
      -> ``tests/test_ingest_idempotency`` 的 append-only 契约保持不变。
    """

    def test_record_without_anchor_keeps_append_only_semantics(self):
        base = {"content": "同一时刻同一内容", "kind": "fact",
                "source_agent": "t", "evidence": ["t:1"]}
        first = self.ledger.ingest({**base, "occurred_at": "2026-08-22T08:00:00+00:00"})
        later = self.ledger.ingest({**base, "occurred_at": "2026-08-22T09:00:00+00:00"})
        self.assertTrue(first.inserted)
        self.assertTrue(later.inserted, "无锚点的记录：新观测时刻仍是新事件")
        self.assertNotEqual(first.memory_id, later.memory_id)
        self.assertEqual(2, self.count())

    def test_task_anchor_makes_the_timestamp_volatile(self):
        first = self.ledger.ingest(gemini_record("2026-10-24T04:00:00Z", 58))
        later = self.ledger.ingest(gemini_record("2026-10-25T09:00:00Z", 99))
        self.assertFalse(later.inserted, "有 task_id 锚点：时间戳不得分叉 identity")
        self.assertEqual(first.memory_id, later.memory_id)
        self.assertEqual(1, self.count())

    def test_session_anchor_makes_the_timestamp_volatile(self):
        base = {"content": "同一条消息", "kind": "fact", "source_agent": "pi",
                "session_id": "pi-session-1", "scope": "shared", "evidence": ["e"]}
        first = self.ledger.ingest({**base, "timestamp": "2026-10-24T01:00:00Z"})
        later = self.ledger.ingest({**base, "timestamp": "2026-10-25T02:00:00Z"})
        self.assertFalse(later.inserted)
        self.assertEqual(first.memory_id, later.memory_id)

    def test_explicit_valid_from_still_wins_over_anchor(self):
        """显式 valid_from 是调用方的真实断言，即使有锚点也必须保留。"""
        base = {"content": "端口是 9000", "kind": "fact", "source_agent": "t",
                "session_id": "s-1", "scope": "shared", "evidence": ["e"]}
        first = self.ledger.ingest({**base, "valid_from": "2026-10-01T00:00:00Z"})
        second = self.ledger.ingest({**base, "valid_from": "2026-10-02T00:00:00Z"})
        self.assertTrue(first.inserted)
        self.assertTrue(second.inserted, "显式双时态优先于锚点判定")

    def test_anchor_does_not_change_what_counts_as_different_content(self):
        base = {"kind": "fact", "source_agent": "t", "session_id": "s-1",
                "scope": "shared", "evidence": ["e"]}
        first = self.ledger.ingest({**base, "content": "第一句话"})
        second = self.ledger.ingest({**base, "content": "第二句话"})
        self.assertTrue(first.inserted)
        self.assertTrue(second.inserted, "同 session 的不同句子是不同记忆")


class IdentityVersionTests(LedgerCase):
    """版本标记：便于将来演进。"""

    def test_version_constants_are_pinned(self):
        self.assertEqual(3, IDENTITY_VERSION)
        self.assertEqual(2, IDENTITY_VERSION_V2)
        self.assertEqual(1, IDENTITY_VERSION_LEGACY)
        self.assertEqual("identity_version", IDENTITY_VERSION_KEY)

    def test_prepared_record_carries_the_version(self):
        prepared = self.ledger._prepare_ingest(
            gemini_record("2026-10-24T04:52:22Z", 58))
        self.assertEqual(IDENTITY_VERSION, prepared["identity_version"])

    def test_version_participates_in_the_hash(self):
        """版本必须进 payload：否则 v2 行能被 v1 探测误认为历史行。"""
        record = gemini_record("2026-10-24T04:52:22Z", 58)
        prepared = self.ledger._prepare_ingest(record)
        self.assertNotEqual(prepared["content_hash"], prepared["legacy_hash"])


class ProductionShapeTests(LedgerCase):
    """复刻生产事故的完整形态并验证收敛。"""

    def test_141_observations_collapse_to_one_row(self):
        """生产事故：同一条内容被采集 141 次。修复后必须只有 1 行。"""
        for index in range(141):
            self.ledger.ingest(gemini_record(
                "2026-10-24T%02d:%02d:%02d.000000+00:00"
                % (4 + index // 60, index % 60, (index * 7) % 60),
                58 + index))
        self.assertEqual(1, self.count(),
                         "141 次采集必须塌缩成 1 行（生产事故回归）")

    def test_second_pass_adds_zero_rows(self):
        """验收口径：连跑两轮采集，第二轮新增重复行 = 0。"""
        for index in range(20):
            self.ledger.ingest(gemini_record(
                "2026-10-24T04:%02d:00+00:00" % index, 58 + index))
        after_first = self.count()
        for index in range(20):
            self.ledger.ingest(gemini_record(
                "2026-10-25T04:%02d:00+00:00" % index, 900 + index))
        self.assertEqual(after_first, self.count(),
                         "第二轮采集不得新增任何行")

    def test_ingest_many_is_stable_within_one_batch(self):
        """同一批里重复的内容也必须只写一行（批量路径共用同一 identity）。"""
        records = [gemini_record("2026-10-24T04:5%d:22Z" % index, 58 + index)
                   for index in range(6)]
        results = self.ledger.ingest_many(records)
        self.assertEqual(1, len({result.memory_id for result in results}))
        self.assertEqual(1, self.count())


if __name__ == "__main__":
    unittest.main()
