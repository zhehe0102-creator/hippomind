"""FTS 索引集合不变量：``memory_fts`` 必须恰好是 {accepted AND active}。

修复前实测（2026-10-20，真实库 136,459 行记忆）：
- ``memory_fts`` 5,571 行，但任何召回查询最多只能返回 392 行 —— 5,179 行
  （93%）是永远不可召回的索引垃圾；
- 「哪些记忆该进 FTS」有四个互相矛盾的实现：写路径只索引 accepted；
  ``fts_sync_report`` 用 accepted+active；``health_overview.fts.expected`` 与
  ``_rebuild_fts`` 用「非 raw 或 active」；``maintain`` 只清 lifecycle!='active'。
- 后果一：``checkup --repair`` 会把隔离行重新写回索引（本文件
  ``test_repair_fts_does_not_turn_a_clean_ledger_stale`` 锁定：修复前
  repair 让 stale 从 0 变 1）。
- 后果二：``maintain`` 之后 ``health_overview`` 反而报 drift（行数 3、
  expected 123），把正确的收敛状态判成故障。

修复把判定收敛到 ``hippomind.schema.FTS_INDEXED_PREDICATE`` 一处，本文件
从行为上锁定：repair_fts 只索引该集合；maintain 清理该集合之外的每一行
（含孤儿行）；maintain 之后 ``fts_sync_report().stale == 0`` 且
``health_overview().fts.drift is False``；冷审计回退路径保持可用。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import schema as schema_module  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402

#: 契约字面量。schema 里的常量必须与它逐字相同（测试不引用常量来算期望，
#: 否则常量写错时测试会跟着一起错）。
PREDICATE = "quality_state='accepted' AND lifecycle='active'"


class FtsInvariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-fts-invariant-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "invariant.sqlite"
        self.ledger = SQLiteLedger(self.path)
        self.addCleanup(self.ledger.close)

        # 写路径正常写入：accepted + active，本来就在 FTS 里。
        self.retrievable = [self._ingest(f"可召回记忆 {i}") for i in range(4)]
        # raw 转录同样可被显式 raw 检索召回。
        self.raw_retrievable = [self._ingest(
            "可召回的原始转录 alpha", kind="raw_chat", raw_chat=True,
            role="assistant")]

        # 旧版本遗留的索引垃圾：写路径曾经接受过这些行，随后质量/生命周期
        # 状态变化，但 FTS 行没有随之清理（线上库的真实形态）。
        self.quarantined = [self._ingest(f"隔离记忆 {i}") for i in range(3)]
        for memory_id in self.quarantined:
            self._set_state(memory_id, quality_state="quarantine")
        # 写路径正确形态：隔离行从未进过索引。
        self.quarantined_missing = [self._ingest("隔离且未索引的记忆")]
        self._set_state(self.quarantined_missing[0], quality_state="quarantine",
                        drop_fts=True)

        self.conflicted = [self._ingest(f"冲突记忆 {i}") for i in range(2)]
        for memory_id in self.conflicted:
            self._set_state(memory_id, quality_state="conflict")

        self.cold = [self._ingest(f"冷记忆 {i}") for i in range(2)]
        for memory_id in self.cold:
            self._set_state(memory_id, lifecycle="cold")

        self.superseded = [self._ingest(f"被取代记忆 {i}") for i in range(2)]
        for memory_id in self.superseded:
            self._set_state(memory_id, lifecycle="superseded",
                            quality_state="superseded")

        self.expired = [self._ingest("过期记忆")]
        self._set_state(self.expired[0], lifecycle="expired")

        # 孤儿索引行：memory 已被外部删除，FTS 行残留。
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute("INSERT INTO memory_fts(memory_id,content) VALUES(?,?)",
                         ("mem-ghost", "孤儿索引行"))

        self.garbage = (self.quarantined + self.quarantined_missing
                        + self.conflicted + self.cold + self.superseded
                        + self.expired)
        self.expected_ids = set(self.retrievable + self.raw_retrievable)

    # -- helpers ---------------------------------------------------------
    def _ingest(self, content: str, **extra) -> str:
        record = {"content": content, "kind": "fact", "source_agent": "invariant",
                  "scope": "shared", "evidence": [f"invariant:{content}"]}
        record.update(extra)
        return self.ledger.ingest(record).memory_id

    def _sql(self, statement: str, params=()):
        with sqlite3.connect(str(self.path)) as conn:
            return conn.execute(statement, params).fetchall()

    def _set_state(self, memory_id: str, *, quality_state: str = "",
                   lifecycle: str = "", drop_fts: bool = False) -> None:
        with sqlite3.connect(str(self.path)) as conn:
            if quality_state:
                conn.execute("UPDATE memories SET quality_state=? WHERE memory_id=?",
                             (quality_state, memory_id))
            if lifecycle:
                conn.execute("UPDATE memories SET lifecycle=? WHERE memory_id=?",
                             (lifecycle, memory_id))
            if drop_fts:
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))

    def _fts_ids(self) -> set:
        return {row[0] for row in self._sql("SELECT memory_id FROM memory_fts")}

    def _predicate_ids(self) -> set:
        return {row[0] for row in self._sql(
            "SELECT memory_id FROM memories WHERE " + PREDICATE)}

    # -- 1) 单一事实来源 ------------------------------------------------
    def test_schema_exports_the_single_predicate(self):
        predicate = getattr(schema_module, "FTS_INDEXED_PREDICATE", None)
        self.assertEqual(predicate, PREDICATE,
                         "schema.FTS_INDEXED_PREDICATE 必须是唯一判定常量")
        qualify = getattr(schema_module, "fts_indexed_predicate", None)
        self.assertIsNotNone(qualify, "schema 必须提供带表别名的谓词辅助函数")
        self.assertEqual(qualify(), PREDICATE)
        self.assertEqual(qualify("m"),
                         "m.quality_state='accepted' AND m.lifecycle='active'")

    def test_only_schema_module_owns_the_predicate_literal(self):
        owners = []
        for path in sorted((ROOT / "hippomind").rglob("*.py")):
            # exFAT 卷会生成 AppleDouble ``._foo.py`` 伴生文件（二进制），
            # ruff 配置同样排除它们。
            if path.name.startswith("._"):
                continue
            if PREDICATE in path.read_text(encoding="utf-8"):
                owners.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(owners, ["hippomind/schema.py"],
                         "判定只能定义在 schema.py，其他模块必须复用常量")

    # -- 2) 两个审计面必须对同一个集合达成一致 --------------------------
    def test_audit_surfaces_agree_on_the_expected_set(self):
        report = self.ledger.fts_sync_report()
        overview = self.ledger.health_overview()
        self.assertEqual(report["projections"], len(self.expected_ids))
        self.assertEqual(overview["fts"]["expected"], len(self.expected_ids))
        self.assertEqual(report["projections"], overview["fts"]["expected"])
        # 修复前这里 stale=10、drift=False：审计完全看不见 10 行垃圾。
        self.assertEqual(report["stale"], 10)
        self.assertTrue(overview["fts"]["drift"])

    # -- 3) repair_fts 只回填可检索集合 ---------------------------------
    def test_repair_fts_indexes_exactly_the_predicate_set(self):
        outcome = self.ledger.repair_fts()
        self.assertTrue(outcome["rebuilt"])
        self.assertEqual(self._fts_ids(), self._predicate_ids())
        self.assertEqual(outcome["fts_rows_after"], len(self.expected_ids))
        for memory_id in self.garbage:
            self.assertNotIn(memory_id, self._fts_ids(),
                             "隔离/冲突/冷/过期/被取代的行不得进入热索引")
        report = self.ledger.fts_sync_report()
        self.assertEqual(report["stale"], 0)
        self.assertEqual(report["orphan"], 0)
        self.assertEqual(report["unindexed"], 0)
        self.assertFalse(self.ledger.health_overview()["fts"]["drift"])

    def test_repair_fts_does_not_turn_a_clean_ledger_stale(self):
        """修复前实测：干净库 repair 之后 stale 从 0 变 1。"""
        clean_path = Path(self.tmp.name) / "clean.sqlite"
        ledger = SQLiteLedger(clean_path)
        self.addCleanup(ledger.close)
        memory_id = ledger.ingest({
            "content": "被隔离的结构化记忆探针", "kind": "fact",
            "source_agent": "invariant", "scope": "shared",
            "evidence": ["invariant:quarantine-probe"],
        }).memory_id
        with sqlite3.connect(str(clean_path)) as conn:
            conn.execute("UPDATE memories SET quality_state='quarantine' "
                         "WHERE memory_id=?", (memory_id,))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))

        before = ledger.fts_sync_report()
        self.assertEqual((before["stale"], before["unindexed"]), (0, 0))
        ledger.repair_fts()
        after = ledger.fts_sync_report()
        self.assertEqual(after["stale"], 0,
                         "repair 不得把隔离行重新写回索引（修复前 stale 0 -> 1）")
        with sqlite3.connect(str(clean_path)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_id=?",
                               (memory_id,)).fetchone()[0]
        self.assertEqual(row, 0)
        self.assertFalse(ledger.health_overview()["fts"]["drift"])

    # -- 4) maintain 清理集合之外的每一行 --------------------------------
    def test_maintain_prunes_quarantine_active_fts_row(self):
        quarantine_id = self.quarantined[0]
        self.assertIn(quarantine_id, self._fts_ids(),
                      "场景前提：隔离+active 的遗留行仍在 FTS（修复前 maintain 不清）")
        result = self.ledger.maintain(archive_raw=False)
        self.assertGreaterEqual(result["fts_pruned"], 1)
        self.assertNotIn(quarantine_id, self._fts_ids())

    def test_maintain_prunes_every_row_outside_the_set(self):
        fts_before = self._fts_ids()
        garbage_in_fts = set(self.garbage) & fts_before
        orphans_before = len(fts_before - set(
            row[0] for row in self._sql("SELECT memory_id FROM memories")))
        result = self.ledger.maintain(archive_raw=False)
        self.assertEqual(result["fts_pruned"], len(garbage_in_fts) + orphans_before,
                         "fts_pruned 必须统计所有被清理的行（含孤儿索引行）")
        self.assertEqual(self._fts_ids(), self._predicate_ids())
        for memory_id in garbage_in_fts:
            self.assertNotIn(memory_id, self._fts_ids())
        for memory_id in self.expected_ids:
            self.assertIn(memory_id, self._fts_ids(), "可召回行不得被误删")

    def test_after_maintain_sync_report_and_health_overview_converge(self):
        self.ledger.maintain(archive_raw=False)
        report = self.ledger.fts_sync_report()
        self.assertEqual(report["stale"], 0)
        self.assertEqual(report["orphan"], 0)
        self.assertEqual(report["unindexed"], 0)
        self.assertEqual(report["status"], "ok")
        overview = self.ledger.health_overview()
        self.assertEqual(overview["fts"]["rows"], overview["fts"]["expected"])
        self.assertFalse(overview["fts"]["drift"])
        self.assertEqual(overview["fts"]["note"], "ok")

    def test_maintain_collapses_duplicate_index_rows(self):
        """``memory_id`` 是 UNINDEXED，重复索引行可表示且会被审计看见。

        ``fts_sync_report`` 把它们报成 ``duplicate_rows`` 并给出
        「运行 maintain 可清理」的提示，所以 maintain 必须真的能清掉它们；
        否则提示指向一个永不收敛的操作（实测修复前 fts_pruned=0、drift 恒真）。
        """
        memory_id = self.retrievable[0]
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute("INSERT INTO memory_fts(memory_id,content) VALUES(?,?)",
                         (memory_id, "可召回记忆 0"))
            raw_rows = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        report = self.ledger.fts_sync_report()
        self.assertEqual(report["fts_rows"], raw_rows)
        self.assertEqual(report["duplicate_rows"], 1)
        self.assertEqual(report["status"], "warn")
        self.assertEqual(report["hint"],
                         "FTS 存在重复索引行，运行 hippomind maintain 可清理")

        result = self.ledger.maintain(archive_raw=False)
        self.assertGreaterEqual(result["fts_pruned"], 1)
        self.assertEqual(self._fts_ids(), self._predicate_ids())
        after = self.ledger.fts_sync_report()
        self.assertEqual((after["duplicate_rows"], after["status"]), (0, "ok"))
        self.assertFalse(self.ledger.health_overview()["fts"]["drift"])

    # -- 4b) 漂移提示必须指向真正能收敛的命令 ---------------------------
    def test_drift_note_points_at_the_command_that_converges(self):
        """行数偏多 = 陈旧索引行，只有 maintain 会清理；repair 是空操作。

        修复前 health_overview 对 5,571 行索引/392 行应索引的线上库报
        ``drift=False``；修复后能看见漂移，但如果仍沿用「建议 checkup
        --repair」，运维会跑一个只在 ``unindexed>0`` 时触发的命令。
        """
        overview = self.ledger.health_overview()["fts"]
        self.assertTrue(overview["drift"])
        self.assertEqual(overview["note"],
                         "FTS 存在不可召回的陈旧索引行，建议运行 hippomind maintain")
        # 少行方向仍指向 repair。
        self.ledger.maintain(archive_raw=False)
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (self.retrievable[0],))
        low = self.ledger.health_overview()["fts"]
        self.assertTrue(low["drift"])
        self.assertEqual(low["note"],
                         "FTS 行数与应索引集合不一致，建议 checkup --repair")

    # -- 5) 线上形态：多隔离行、少可召回行 ------------------------------
    def test_live_shaped_ledger_converges_to_predicate_set(self):
        live_path = Path(self.tmp.name) / "live-shape.sqlite"
        ledger = SQLiteLedger(live_path)
        self.addCleanup(ledger.close)
        accepted = [ledger.ingest({
            "content": f"线上可召回记忆 {i}", "kind": "fact",
            "source_agent": "live-shape", "scope": "shared",
            "evidence": [f"live-shape:{i}"],
        }).memory_id for i in range(3)]
        now = "2026-01-01T00:00:00+00:00"
        with sqlite3.connect(str(live_path)) as conn:
            conn.executemany(
                "INSERT INTO memories(memory_id,origin_event_id,kind,content,"
                "source_agent,scope,created_at,updated_at,last_weighted_at,"
                "quality_state,lifecycle,raw_chat) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(f"mem_legacy_{i:03d}", f"evt_legacy_{i:03d}", "fact",
                  f"遗留隔离行 {i}", "legacy", "shared", now, now, now,
                  "quarantine", "active", 0) for i in range(120)])
            conn.executemany(
                "INSERT INTO memory_fts(memory_id,content) VALUES(?,?)",
                [(f"mem_legacy_{i:03d}", f"遗留隔离行 {i}") for i in range(120)])

        before = ledger.health_overview()["fts"]
        self.assertEqual((before["rows"], before["expected"]), (123, 3))
        self.assertTrue(before["drift"],
                        "修复前 expected 也按 123 算，垃圾被审计当成健康")

        result = ledger.maintain(archive_raw=False)
        self.assertEqual(result["fts_pruned"], 120)
        with sqlite3.connect(str(live_path)) as conn:
            remaining = {row[0] for row in
                         conn.execute("SELECT memory_id FROM memory_fts")}
        self.assertEqual(remaining, set(accepted))
        after = ledger.health_overview()["fts"]
        self.assertEqual((after["rows"], after["expected"]), (3, 3))
        self.assertFalse(after["drift"])
        report = ledger.fts_sync_report()
        self.assertEqual((report["stale"], report["unindexed"], report["status"]),
                         (0, 0, "ok"))

    # -- 6) 冷审计回退路径不受影响 --------------------------------------
    def test_cold_audit_fallback_still_finds_cold_rows(self):
        memory_id = self._ingest("冷审计回退验证文本", kind="progress",
                                 timestamp="2000-01-01T00:00:00Z")
        self.ledger.maintain(cold_after_days=1, archive_raw=False)
        self.assertEqual(self.ledger.get_memory(memory_id)["lifecycle"], "cold")
        self.assertNotIn(memory_id, self._fts_ids())
        hits = self.ledger.search("冷审计回退验证", include_cold=True, limit=10)
        self.assertIn(memory_id, [item["memory_id"] for item in hits],
                      "include_cold=True 必须仍能经有界 LIKE 回退找到冷行")
        self.assertEqual(self.ledger.fts_sync_report()["stale"], 0)
        self.assertFalse(self.ledger.health_overview()["fts"]["drift"])


if __name__ == "__main__":
    unittest.main()


class ResidualFtsWriterTests(unittest.TestCase):
    """The last two FTS writers, and the audit's tolerance of duplicate rows.

    ``schema.FTS_INDEXED_PREDICATE`` only stays true if *every* writer derives
    from it.  Two more were found after the main fix (both measured):

    * ``redact_raw_evidence`` re-inserted whenever ``lifecycle == 'active'``,
      so redacting a quarantined raw row re-created the stale index entry
      (``stale`` 0 -> 1, ``drift`` True);
    * ``export_sql`` emitted an unconditional ``SELECT ... FROM memories``, so
      replaying a dump indexed cold rows on the target.

    Separately, ``fts_sync_report`` used to raise ``IntegrityError`` when the
    index held a duplicate row for one memory (representable because
    ``memory_id`` is ``UNINDEXED``): a diagnosable index defect surfaced as an
    opaque traceback instead of a report.
    """

    def setUp(self):
        import sys
        import tempfile
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from hippomind.ledger import SQLiteLedger

        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-residual-fts-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = SQLiteLedger(self.root / "r.sqlite")

    def tearDown(self):
        self.ledger.close()

    # -- redact_raw_evidence ------------------------------------------------
    def test_redact_does_not_reindex_a_quarantined_row(self):
        result = self.ledger.ingest({
            "content": "旧聊天里的 token=sk-live-abcdef123456789", "kind": "raw_chat",
            "raw_chat": True, "source_agent": "a", "session_id": "s",
            "role": "user", "evidence": ["e:1"]})
        # The ingest path sanitises on the way in, so a *new* row cannot
        # exercise redact.  This fixture reproduces the case redact exists for:
        # a legacy row that still carries the raw secret, quarantined but on the
        # active lifecycle (the shape that used to be re-indexed).
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET quality_state='quarantine', content=? "
                         "WHERE memory_id=?",
                         ("旧聊天里的 token=sk-live-abcdef123456789", result.memory_id))
            conn.execute("UPDATE ledger_events SET content=? WHERE event_id=?",
                         ("旧聊天里的 token=sk-live-abcdef123456789", result.event_id))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (result.memory_id,))
        before = self.ledger.fts_sync_report()
        self.assertEqual(0, before["stale"])

        report = self.ledger.redact_raw_evidence()
        self.assertGreaterEqual(report["changed"], 1, "夹具里的密钥没有被脱敏，测试前提失效")
        after = self.ledger.fts_sync_report()
        self.assertEqual(0, after["stale"],
                         "redact 把 quarantine+active 行重新写回了热索引")
        self.assertFalse(self.ledger.health_overview()["fts"]["drift"])

    def test_redact_keeps_an_accepted_row_indexed(self):
        result = self.ledger.ingest({
            "content": "正常原始聊天 token=sk-live-abcdef123456789", "kind": "raw_chat",
            "raw_chat": True, "source_agent": "a", "session_id": "s",
            "role": "user", "evidence": ["e:1"]})
        self.assertEqual("accepted", self.ledger.get_memory(result.memory_id)["quality_state"])
        self.ledger.redact_raw_evidence()
        report = self.ledger.fts_sync_report()
        self.assertEqual(1, report["fts_rows"], "可召回行脱敏后必须仍在索引里")
        self.assertEqual(0, report["unindexed"])
        self.assertEqual(0, report["stale"])

    # -- export_sql ---------------------------------------------------------
    def test_sql_dump_rebuilds_only_the_hot_index(self):
        import sqlite3

        active = self.ledger.ingest({"content": "导出：活跃事实", "kind": "fact",
                                     "source_agent": "a", "evidence": ["e:1"]})
        cold = self.ledger.ingest({"content": "导出：冷归档事实", "kind": "fact",
                                   "source_agent": "a", "evidence": ["e:2"]})
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET lifecycle='cold' WHERE memory_id=?",
                         (cold.memory_id,))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (cold.memory_id,))

        dump = self.ledger.export_sql()
        target = self.root / "target.sqlite"
        raw = sqlite3.connect(str(target))
        raw.executescript(_minimal_target_schema())
        raw.executescript(dump)
        memories = raw.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        indexed = {row[0] for row in raw.execute("SELECT memory_id FROM memory_fts")}
        raw.close()

        self.assertEqual(2, memories, "导出应包含 active 与 cold 两种生命周期")
        self.assertEqual({active.memory_id}, indexed,
                         "回放只应重建 accepted+active 的热索引")

    # -- audit tolerance ----------------------------------------------------
    def test_audit_reports_a_duplicate_index_row_instead_of_raising(self):
        result = self.ledger.ingest({"content": "重复索引行", "kind": "fact",
                                     "source_agent": "a", "evidence": ["e:1"]})
        # memory_id is UNINDEXED in fts5, so this row is representable.
        with self.ledger._connect(write=True) as conn:
            conn.execute("INSERT INTO memory_fts(memory_id,content) VALUES(?,?)",
                         (result.memory_id, "duplicate"))
        report = self.ledger.fts_sync_report()
        self.assertEqual(1, report["duplicate_rows"])
        self.assertEqual(2, report["fts_rows"])
        self.assertEqual("warn", report["status"])
        self.assertIn("重复", report["hint"])
        # And maintenance converges it.
        self.ledger.maintain(archive_raw=False)
        after = self.ledger.fts_sync_report()
        self.assertEqual(0, after["duplicate_rows"])
        self.assertEqual(1, after["fts_rows"])


def _minimal_target_schema() -> str:
    """A replay target with the v4+ quality columns the dump filters on."""
    return """
CREATE TABLE ledger_events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
 occurred_at TEXT NOT NULL, ingested_at TEXT NOT NULL, source_agent TEXT NOT NULL,
 session_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
 project TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL,
 evidence_json TEXT NOT NULL DEFAULT '[]', content TEXT NOT NULL,
 content_hash TEXT NOT NULL UNIQUE, metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE memories (memory_id TEXT PRIMARY KEY,
 origin_event_id TEXT NOT NULL UNIQUE REFERENCES ledger_events(event_id),
 kind TEXT NOT NULL, content TEXT NOT NULL, source_agent TEXT NOT NULL,
 session_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
 project TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL,
 evidence_json TEXT NOT NULL DEFAULT '[]', tags_json TEXT NOT NULL DEFAULT '[]',
 metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, last_access_at TEXT NOT NULL DEFAULT '',
 last_weighted_at TEXT NOT NULL, valid_until TEXT NOT NULL DEFAULT '',
 superseded_by TEXT NOT NULL DEFAULT '', archived_at TEXT NOT NULL DEFAULT '',
 lifecycle TEXT NOT NULL DEFAULT 'active', raw_chat INTEGER NOT NULL DEFAULT 0,
 weight REAL NOT NULL DEFAULT 0.5, importance INTEGER NOT NULL DEFAULT 5,
 touch_count INTEGER NOT NULL DEFAULT 0, utility REAL NOT NULL DEFAULT 0.0,
 confidence REAL NOT NULL DEFAULT 0.5, success_count INTEGER NOT NULL DEFAULT 0,
 failure_count INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
 valid_from TEXT NOT NULL DEFAULT '', asserted_at TEXT NOT NULL DEFAULT '',
 agent_trust REAL NOT NULL DEFAULT 0.5, quality_score REAL NOT NULL DEFAULT 1.0,
 quality_state TEXT NOT NULL DEFAULT 'accepted');
CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, content);
"""
