"""Forgetting audit (hippomind/forgetting.py + scripts/forgetting_report.py).

Every test builds its own temporary ledger and writes the decay events by hand
against the metadata contract, so nothing here depends on production data or on
the write side (lifecycle.maintain) having landed yet.  The assertions pin the
two properties the report exists for:

* a count is not an explanation -- the reason, weights and age that decided a
  row must be reproduced from the event, and a window with no events over a
  cold population must say "无法解释" instead of printing a tidy zero;
* "cold" must mean "recoverable" -- the recoverability verdict is checked
  against SQLiteLedger.search() itself, not against a copied predicate, so the
  report cannot drift away from the recall path it claims to describe.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import forgetting_report  # noqa: E402
from hippomind import forgetting  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402

#: Fixed "now" so window boundaries are arithmetic, not wall-clock.
#:
#: 2026-10-25 修正：这个常量原来写死 2026-10-25T12:00Z，一旦真实时钟越过
#: 那一刻，_ingest() 写入的行（valid_from = 真实 now）就会晚于 _NOW，
#: recall_blockers() 于是报「valid_from 尚未生效」，7 条测试一起变红——
#: 而它们本来想测的是「cold 行能不能召回」，与时间无关。
#:
#: 固定时刻的用意是让窗口边界可算，不是让测试依赖「今天恰好还没到 12 点」。
#: 所以只把 _NOW 往前推一年：仍是常量（算术可复现），但永远不会被真实时钟追上。
_NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

_SEQ = [0]


def _ingest(ledger, content, **extra):
    _SEQ[0] += 1
    record = {"content": content, "kind": "fact", "source_agent": "claude",
              "scope": "shared", "evidence": ["ev:%d" % _SEQ[0]],
              # 显式写入 valid_from：不写的话 ingest 会用真实 now，而本文件
              # 的时间基准是固定的 _NOW。一旦真实时钟越过 _NOW，所有行都会
              # 变成「valid_from 尚未生效」，recall_blockers 报出一条与测试
              # 意图无关的阻断条件——7 条测试在 2026-10-25 12:00 之后集体
              # 变红就是这个原因。窗口边界要可算，就不能让写入侧取墙钟。
              "valid_from": (_NOW - timedelta(days=30)).isoformat()}
    record.update(extra)
    return ledger.ingest(record)


def _decay(ledger, memory_id, *, reason="weight_floor", occurred_at="",
           from_lifecycle="active", to_lifecycle="cold", weight_before=0.5,
           weight_after=0.4, decay_per_day=0.008, age_days=12.5, utility=0.3,
           importance=6, source_agent="hippomind-maintain", metadata=None,
           raw_metadata=None, row_age_days=30.0, include_row_age=True):
    """Append one decay event exactly as the contract specifies.

    Passing 'metadata' replaces the whole payload (used for contract-violation
    cases); passing 'raw_metadata' bypasses JSON encoding entirely (unparseable
    evidence).
    """
    _SEQ[0] += 1
    occurred = occurred_at or _NOW.isoformat()
    payload = {"memory_id": memory_id, "from_lifecycle": from_lifecycle,
               "to_lifecycle": to_lifecycle, "reason": reason,
               "weight_before": weight_before, "weight_after": weight_after,
               "decay_per_day": decay_per_day, "age_days": age_days,
               "utility": utility, "importance": importance}
    # row_age_days is the writer's supplementary field (11th).  include_row_age
    # simulates a writer that predates it, which the report must survive.
    if include_row_age:
        payload["row_age_days"] = row_age_days
    if metadata is not None:
        payload = metadata
    raw = raw_metadata if raw_metadata is not None else json.dumps(
        payload, ensure_ascii=False)
    event_id = "evt_decay_%d" % _SEQ[0]

    def write(conn):
        conn.execute(
            "INSERT INTO ledger_events(event_id,event_type,occurred_at,ingested_at,"
            "source_agent,session_id,task_id,project,role,scope,evidence_json,content,"
            "content_hash,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "decay", occurred, occurred, source_agent, "", "", "", "",
             "system", "[]", "", "hash_" + event_id, raw))
    ledger._transaction(write)
    return event_id


class ForgettingReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-forgetting-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    # -- helpers -----------------------------------------------------------

    def _reader(self):
        return forgetting.ReadOnlyLedger(self.ledger.path)

    def _report(self, **kwargs):
        with self._reader() as reader:
            return forgetting.forgetting_report(reader, now=_NOW, **kwargs)

    def _set_lifecycle(self, memory_id, lifecycle, *, quality_state=None):
        """Mirror what lifecycle.maintain does: move lifecycle, drop the FTS row."""
        def write(conn):
            if quality_state is None:
                conn.execute("UPDATE memories SET lifecycle=? WHERE memory_id=?",
                             (lifecycle, memory_id))
            else:
                conn.execute(
                    "UPDATE memories SET lifecycle=?,quality_state=? WHERE memory_id=?",
                    (lifecycle, quality_state, memory_id))
            if lifecycle != "active":
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        self.ledger._transaction(write)

    def _sql(self, statement, params=()):
        def write(conn):
            conn.execute(statement, params)
        self.ledger._transaction(write)

    # -- aggregation: reasons, weights, rollup -----------------------------

    def test_reason_distribution_reproduces_written_events(self):
        a = _ingest(self.ledger, "记忆甲：部署端口变更")
        b = _ingest(self.ledger, "记忆乙：构建脚本超时")
        c = _ingest(self.ledger, "记忆丙：临时会议纪要")
        _decay(self.ledger, a.memory_id, reason="weight_floor", weight_before=0.4,
               weight_after=0.11)
        _decay(self.ledger, b.memory_id, reason="weight_floor", weight_before=0.5,
               weight_after=0.10)
        _decay(self.ledger, c.memory_id, reason="expired", weight_before=0.9,
               weight_after=0.9, to_lifecycle="expired")
        report = self._report()
        by_reason = {item["reason"]: item for item in report["by_reason"]}
        self.assertEqual(2, by_reason["weight_floor"]["events"])
        self.assertEqual(2, by_reason["weight_floor"]["memories"])
        self.assertEqual(1, by_reason["expired"]["events"])
        # The archived count is derived from to_lifecycle, not from the reason.
        self.assertEqual(2, by_reason["weight_floor"]["archived"])
        self.assertEqual(1, by_reason["expired"]["archived"])
        # Ranked by event count: the dominant reason must lead.
        self.assertEqual("weight_floor", report["by_reason"][0]["reason"])
        self.assertIn("权重", by_reason["weight_floor"]["rule"])
        self.assertEqual(3, report["events"]["total"])
        self.assertEqual(3, report["events"]["memories"])

    def test_weights_and_age_are_taken_from_the_event(self):
        memory = _ingest(self.ledger, "记忆：被衰减到冷置")
        _decay(self.ledger, memory.memory_id, weight_before=0.62, weight_after=0.18,
               age_days=41.5, utility=-0.25, importance=9)
        report = self._report()
        self.assertEqual(1, len(report["top_drops"]))
        row = report["top_drops"][0]
        self.assertAlmostEqual(0.62, row["weight_before"], places=6)
        self.assertAlmostEqual(0.18, row["weight_after"], places=6)
        self.assertAlmostEqual(0.44, row["drop"], places=6)
        self.assertAlmostEqual(41.5, row["age_days"], places=6)
        self.assertAlmostEqual(-0.25, row["utility"], places=6)
        self.assertEqual(9, row["importance"])
        self.assertTrue(row["archived"])
        self.assertEqual(["weight_floor"], row["reasons"])

    def test_repeated_ticks_collapse_to_one_memory_with_span_not_sum(self):
        """Summing per-event drops would double-count the same weight loss."""
        memory = _ingest(self.ledger, "记忆：连续三轮衰减")
        _decay(self.ledger, memory.memory_id, weight_before=0.50, weight_after=0.40,
               occurred_at=(_NOW - timedelta(days=3)).isoformat())
        _decay(self.ledger, memory.memory_id, weight_before=0.40, weight_after=0.30,
               occurred_at=(_NOW - timedelta(days=2)).isoformat())
        _decay(self.ledger, memory.memory_id, weight_before=0.30, weight_after=0.20,
               occurred_at=(_NOW - timedelta(days=1)).isoformat())
        report = self._report()
        self.assertEqual(3, report["events"]["total"])
        self.assertEqual(1, report["events"]["memories"])
        self.assertEqual(1, len(report["top_drops"]))
        row = report["top_drops"][0]
        self.assertEqual(3, row["events"])
        self.assertAlmostEqual(0.50, row["weight_before"], places=6)
        self.assertAlmostEqual(0.20, row["weight_after"], places=6)
        # span = 0.30, NOT the 0.10+0.10+0.10 sum of naive per-event arithmetic
        self.assertAlmostEqual(0.30, row["drop"], places=6)
        self.assertEqual(row["last_at"], (_NOW - timedelta(days=1)).isoformat())

    def test_top_drops_are_ordered_by_drop_and_respect_limit(self):
        small = _ingest(self.ledger, "记忆：小幅降权")
        big = _ingest(self.ledger, "记忆：大幅降权")
        mid = _ingest(self.ledger, "记忆：中幅降权")
        _decay(self.ledger, small.memory_id, weight_before=0.30, weight_after=0.28)
        _decay(self.ledger, big.memory_id, weight_before=0.90, weight_after=0.10)
        _decay(self.ledger, mid.memory_id, weight_before=0.60, weight_after=0.35)
        report = self._report(limit=2)
        self.assertEqual(2, len(report["top_drops"]))
        self.assertEqual([big.memory_id, mid.memory_id],
                         [row["memory_id"] for row in report["top_drops"]])

    def test_archive_targets_counted_separately(self):
        a = _ingest(self.ledger, "记忆：退居冷存储")
        b = _ingest(self.ledger, "记忆：仅降权")
        c = _ingest(self.ledger, "记忆：被替代")
        _decay(self.ledger, a.memory_id, to_lifecycle="cold")
        _decay(self.ledger, b.memory_id, to_lifecycle="active")
        _decay(self.ledger, c.memory_id, to_lifecycle="superseded", reason="expired")
        report = self._report()
        targets = {item["to_lifecycle"]: item["events"]
                   for item in report["archive"]["by_target"]}
        self.assertEqual(1, targets["cold"])
        self.assertEqual(1, targets["active"])
        self.assertEqual(1, targets["superseded"])
        # A weight-only event is not an archive event.
        self.assertEqual(2, report["archive"]["total"])

    def test_agent_and_kind_buckets_come_from_the_live_projection(self):
        a = _ingest(self.ledger, "记忆：claude 的决策", kind="decision",
                    source_agent="claude")
        b = _ingest(self.ledger, "记忆：codex 的任务", kind="task",
                    source_agent="codex")
        _decay(self.ledger, a.memory_id)
        _decay(self.ledger, b.memory_id)
        report = self._report()
        agents = {item["agent"]: item["events"] for item in report["by_agent"]}
        kinds = {item["kind"]: item["events"] for item in report["by_kind"]}
        self.assertEqual(1, agents["claude"])
        self.assertEqual(1, agents["codex"])
        self.assertEqual(1, kinds["decision"])
        self.assertEqual(1, kinds["task"])

    def test_memory_missing_from_projection_is_marked_not_dropped(self):
        _decay(self.ledger, "mem_gone_forever", reason="raw_retention")
        report = self._report()
        self.assertEqual(1, report["events"]["total"])
        self.assertEqual(forgetting.GONE_KIND, report["top_drops"][0]["kind"])
        self.assertEqual(1, report["recoverability"]["gone"])

    # -- contract violations stay visible ----------------------------------

    def test_out_of_contract_reason_is_reported_not_folded(self):
        memory = _ingest(self.ledger, "记忆：写入侧用了未知原因")
        _decay(self.ledger, memory.memory_id, reason="because-i-said-so")
        report = self._report()
        reasons = [item["reason"] for item in report["by_reason"]]
        self.assertEqual([forgetting.UNKNOWN_REASON], reasons)
        self.assertEqual(1, report["by_reason"][0]["events"])
        self.assertIn("契约外", report["by_reason"][0]["label"])

    def test_missing_reason_counts_as_unknown(self):
        memory = _ingest(self.ledger, "记忆：写入侧没写 reason")
        _decay(self.ledger, memory.memory_id, reason="")
        report = self._report()
        self.assertEqual(forgetting.UNKNOWN_REASON, report["by_reason"][0]["reason"])
        self.assertEqual(1, report["by_reason"][0]["events"])

    def test_unparseable_metadata_is_counted_not_dropped(self):
        _decay(self.ledger, "mem_x", raw_metadata="{not json")
        report = self._report()
        self.assertEqual(0, report["events"]["total"])
        self.assertEqual(1, report["events"]["malformed"])
        # A shrinking denominator must be announced, not silently absorbed.
        self.assertTrue(any("无法解析" in caveat for caveat in report["verdict"]["caveats"]))

    def test_event_without_memory_id_is_malformed(self):
        _decay(self.ledger, "ignored", metadata={"reason": "weight_floor"})
        report = self._report()
        self.assertEqual(0, report["events"]["total"])
        self.assertEqual(1, report["events"]["malformed"])

    def test_event_outside_window_is_excluded_even_when_string_sort_passes(self):
        """A +14:00 timestamp sorts after the window start but parses before it."""
        memory = _ingest(self.ledger, "记忆：窗口外的事件")
        start = _NOW - timedelta(days=7)
        inside = (_NOW - timedelta(days=1)).isoformat()
        tricky = (start - timedelta(hours=2)).astimezone(
            timezone(timedelta(hours=14))).isoformat()
        self.assertGreater(tricky, start.isoformat(), "前置条件：字符串序会放行")
        _decay(self.ledger, memory.memory_id, occurred_at=inside)
        _decay(self.ledger, memory.memory_id, occurred_at=tricky)
        with self._reader() as reader:
            loaded = forgetting.decay_events(reader, days=7, now=_NOW)
        self.assertEqual(1, len(loaded["events"]))
        self.assertEqual(inside, loaded["events"][0]["occurred_at"])

    def test_truncation_keeps_newest_and_is_reported(self):
        memory = _ingest(self.ledger, "记忆：事件超过上限")
        for offset in range(3):
            _decay(self.ledger, memory.memory_id,
                   occurred_at=(_NOW - timedelta(days=3 - offset)).isoformat())
        with self._reader() as reader:
            loaded = forgetting.decay_events(reader, days=7, now=_NOW, max_events=2)
        self.assertTrue(loaded["truncated"])
        self.assertEqual(2, len(loaded["events"]))
        self.assertEqual(2, loaded["budget"])
        # Newest two survive; the oldest is the one dropped.
        self.assertEqual((_NOW - timedelta(days=2)).isoformat(),
                         loaded["events"][0]["occurred_at"])
        report = self._report(max_events=2)
        self.assertTrue(report["events"]["truncated"])
        self.assertEqual(2, report["events"]["budget"])
        self.assertTrue(any("不完整" in caveat for caveat in report["verdict"]["caveats"]))
        # Truncation must be visible in the rendered report, not just the JSON.
        text = forgetting.render(report)
        self.assertIn("【已截断】", text)
        self.assertIn("总数可能更大", text)

    def test_event_budget_scales_with_the_window(self):
        """A fixed cap silently truncates a long window; the budget must scale.

        The floor protects short windows (a 1-day report on a busy ledger still
        gets the floor), the ceiling protects the process, and the value is
        monotonic in between -- no window length may read *fewer* events than a
        shorter one.
        """
        self.assertEqual(forgetting.MIN_EVENT_BUDGET, forgetting.event_budget(1))
        self.assertEqual(forgetting.MIN_EVENT_BUDGET, forgetting.event_budget(2))
        self.assertGreater(forgetting.event_budget(30), forgetting.event_budget(7))
        self.assertGreater(forgetting.event_budget(365), forgetting.event_budget(30))
        self.assertEqual(forgetting.MAX_EVENT_BUDGET, forgetting.event_budget(100_000))
        budgets = [forgetting.event_budget(days) for days in range(1, 120)]
        self.assertEqual(budgets, sorted(budgets), "预算必须随窗口单调不减")

    def test_default_budget_is_reported_even_without_truncation(self):
        """The budget is part of the answer: readers must know the ceiling."""
        memory = _ingest(self.ledger, "记忆：预算可见")
        _decay(self.ledger, memory.memory_id)
        report = self._report(days=7)
        self.assertEqual(forgetting.event_budget(7), report["events"]["budget"])
        self.assertFalse(report["events"]["truncated"])
        text = forgetting.render(report)
        # The ceiling is part of the answer even when it was not hit: a reader
        # must be able to tell "no more events exist" from "I stopped looking".
        self.assertIn("本窗口读取上限", text)
        self.assertIn("{:,}".format(forgetting.event_budget(7)), text)
        self.assertNotIn("【已用满", text)

    # -- the archive side table --------------------------------------------

    def _archive_table(self):
        """Create ledger_events_archive with the same columns maintain() uses."""
        self._sql("""
            CREATE TABLE IF NOT EXISTS ledger_events_archive (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL UNIQUE,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                asserted_at TEXT NOT NULL DEFAULT ''
            )
        """)

    def _archive_decay(self, memory_id, *, occurred_at, event_id):
        payload = json.dumps({"memory_id": memory_id, "from_lifecycle": "active",
                              "to_lifecycle": "cold", "reason": "weight_floor",
                              "weight_before": 0.4, "weight_after": 0.1,
                              "decay_per_day": 0.008, "age_days": 30.0,
                              "utility": 0.2, "importance": 5},
                             ensure_ascii=False)
        self._sql(
            "INSERT INTO ledger_events_archive(event_id,event_type,occurred_at,"
            "ingested_at,source_agent,session_id,task_id,project,role,scope,"
            "evidence_json,content,content_hash,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "decay", occurred_at, occurred_at, "hippomind-maintain",
             "", "", "", "", "system", "[]", "", "ahash_" + event_id, payload))

    def test_archived_decay_events_are_still_read(self):
        """maintain() moves orphan audit rows to a side table.

        A decay event is an orphan by construction (it documents a row, it does
        not anchor one), so reading only ledger_events would silently lose the
        oldest explanations and report "no evidence" for a period that WAS
        explained.  Both tables must be merged.
        """
        memory = _ingest(self.ledger, "记忆：事件已被归档到侧表")
        self._set_lifecycle(memory.memory_id, "cold")
        self._archive_table()
        self._archive_decay(memory.memory_id,
                            occurred_at=(_NOW - timedelta(days=2)).isoformat(),
                            event_id="evt_archived_1")
        report = self._report()
        self.assertEqual(1, report["events"]["total"])
        self.assertEqual(1, report["coverage"]["explained_cold"])
        self.assertIn("ledger_events_archive", report["events"]["tables"])
        self.assertEqual(1, report["coverage"]["decay_events_all_time"])
        self.assertTrue(report["verdict"]["explainable"])

    def test_events_split_across_both_tables_are_merged_without_double_count(self):
        """An interrupted archive pass can leave one event_id in both tables."""
        a = _ingest(self.ledger, "记忆：主表事件")
        b = _ingest(self.ledger, "记忆：侧表事件")
        self._set_lifecycle(a.memory_id, "cold")
        self._set_lifecycle(b.memory_id, "cold")
        self._archive_table()
        _decay(self.ledger, a.memory_id,
               occurred_at=(_NOW - timedelta(days=1)).isoformat())
        self._archive_decay(b.memory_id,
                            occurred_at=(_NOW - timedelta(days=3)).isoformat(),
                            event_id="evt_archived_2")
        # Same event_id present in BOTH tables: the writer INSERTs into the
        # archive before DELETEing from the main table.
        self._archive_decay(a.memory_id,
                            occurred_at=(_NOW - timedelta(days=1)).isoformat(),
                            event_id="evt_decay_%d" % _SEQ[0])
        report = self._report()
        self.assertEqual(2, report["events"]["total"],
                         "同一个 event_id 在两张表里只能算一次")
        self.assertEqual(2, report["coverage"]["explained_cold"])

    def test_missing_archive_table_is_not_an_error(self):
        """The side table is created lazily, so a fresh ledger has none."""
        memory = _ingest(self.ledger, "记忆：没有侧表")
        _decay(self.ledger, memory.memory_id)
        self.assertNotIn("ledger_events_archive", self._reader().tables())
        report = self._report()
        self.assertEqual(1, report["events"]["total"])
        self.assertEqual(["ledger_events"], report["events"]["tables"])

    # -- the zero that is not a zero ---------------------------------------

    def test_cold_population_without_events_says_unexplainable(self):
        memory = _ingest(self.ledger, "记忆：被归档但没有留下原因")
        self._set_lifecycle(memory.memory_id, "cold")
        report = self._report()
        self.assertEqual(0, report["events"]["total"])
        self.assertEqual(1, report["coverage"]["cold_total_now"])
        self.assertEqual(0, report["coverage"]["explained_cold"])
        self.assertEqual(1, report["coverage"]["unexplained_cold"])
        self.assertFalse(report["coverage"]["decay_events_available"])
        self.assertFalse(report["verdict"]["explainable"])
        self.assertIn("1 条 cold", report["verdict"]["headline"])
        self.assertIn("没有留下原因", report["verdict"]["headline"])
        text = forgetting.render(report)
        self.assertIn("原因不可查", text)
        self.assertIn("不能证明「没遗忘」", text)

    def test_coverage_counts_only_cold_rows_explained_in_window(self):
        explained = _ingest(self.ledger, "记忆：有解释的归档")
        silent = _ingest(self.ledger, "记忆：无解释的归档")
        self._set_lifecycle(explained.memory_id, "cold")
        self._set_lifecycle(silent.memory_id, "cold")
        _decay(self.ledger, explained.memory_id)
        report = self._report()
        self.assertEqual(2, report["coverage"]["cold_total_now"])
        self.assertEqual(1, report["coverage"]["explained_cold"])
        self.assertEqual(1, report["coverage"]["unexplained_cold"])
        # 1 of 2 cold rows explained is "partial", which is deliberately NOT
        # reported as explainable: half the archive has no reason recorded.
        self.assertEqual("partial", report["coverage"]["state"])
        self.assertTrue(report["verdict"]["explainable"])
        self.assertEqual(1, report["verdict"]["explained_rows"])
        self.assertEqual(1, report["verdict"]["unexplained_rows"])
        self.assertTrue(any("不能外推到全部" in caveat
                            for caveat in report["verdict"]["caveats"]))
        self.assertTrue(any("其余是窗口外归档" in caveat
                            for caveat in report["verdict"]["caveats"]))

    def test_empty_ledger_is_explainable_and_says_why_it_could_be_wrong(self):
        report = self._report()
        self.assertEqual(0, report["coverage"]["cold_total_now"])
        self.assertTrue(report["verdict"]["explainable"])
        self.assertTrue(any("没有维护" in caveat for caveat in report["verdict"]["caveats"]))

    # -- recoverability: the part that must not be assumed ------------------

    def test_cold_memory_is_still_recallable(self):
        """The recovery path this report documents, proven against search().

        The cold row is the only row in the ledger matching the query, which is
        the condition under which the bounded LIKE rescue channel actually runs.
        The shadowing case below covers what happens when it is not.
        """
        memory = _ingest(self.ledger, "生产部署端口是 9345，由 systemd 托管")
        self._set_lifecycle(memory.memory_id, "cold")
        _decay(self.ledger, memory.memory_id, reason="weight_floor",
               weight_before=0.4, weight_after=0.11)
        # cold left the hot FTS index (that is what maintain does)...
        self.assertEqual(0, self.ledger._reader().execute(
            "SELECT COUNT(*) FROM memory_fts WHERE memory_id=?",
            (memory.memory_id,)).fetchone()[0])
        # ...but an explicit query still returns it, because include_cold is on.
        hits = self.ledger.search("生产部署端口是 9345", limit=5)
        self.assertEqual([memory.memory_id], [hit["memory_id"] for hit in hits])
        # ...and turning it off is what actually hides the row.
        self.assertEqual([], self.ledger.search("生产部署端口是 9345", limit=5,
                                                include_cold=False))
        report = self._report()
        self.assertEqual(1, report["recoverability"]["memories"])
        self.assertEqual(1, report["recoverability"]["predicate_reachable"])
        self.assertEqual(1, report["recoverability"]["cold_now"])
        self.assertEqual(0, report["recoverability"]["blocked"])
        self.assertEqual({}, report["recoverability"]["blockers"])
        self.assertTrue(report["verdict"]["recall_is_conditional"])
        self.assertTrue(any("cold 兜底通道" in caveat
                            for caveat in report["verdict"]["caveats"]))

    def test_cold_recall_requires_no_hot_competitor(self):
        """The discovered limitation, pinned from both sides.

        search() generates candidates from memory_fts first and only falls back
        to the bounded LIKE rescue when the hot index left the budget unfilled.
        maintain() deletes cold rows from memory_fts (correctly -- cold
        projections must not occupy the hot index), so a cold row is reachable
        ONLY when no active row matches the same query.  include_cold=True
        relaxes the SQL filter but cannot influence candidate generation.

        This is a retrieval-side limitation (search.py), reported here and
        deliberately NOT worked around: the report must describe the recall path
        as it is, and the fix belongs to whoever owns search.py.

        IF THIS TEST FAILS AT THE "前置条件" ASSERTION, do not "fix" the test.
        It means search.py's candidate generation changed so that cold rows now
        enter the candidate set even when an active row matches.  That would be
        the retrieval-side fix landing.  What to do, in order:
          1. confirm the change is intentional (read the diff to search.py);
          2. update hippomind/forgetting.py section 6 -- the text
             "可召回是【条件性】的" and the capacity-constraint paragraph are
             then FALSE, and a report that keeps printing them is lying;
          3. flip this test's expectation (cold row SHOULD now be in the
             result) and keep it as the pin for the new behaviour;
          4. update verdict.recall_is_conditional in _verdict() accordingly.
        The point of this test is that stale wording fails loudly instead of
        quietly promising something the retrieval path no longer does.
        """
        cold = _ingest(self.ledger, "coldshadow uniquetoken 冷行")
        self._set_lifecycle(cold.memory_id, "cold")
        _decay(self.ledger, cold.memory_id)
        query = "coldshadow uniquetoken"
        # Alone, the cold row IS reachable through the rescue channel.
        alone = [hit["memory_id"] for hit in self.ledger.search(query, limit=5)]
        self.assertEqual([cold.memory_id], alone)
        # Add an active row matching the same query.  BEFORE the 2026-10-25 fix
        # the cold row disappeared here (FTS-first candidate generation).
        # AFTER the fix a dedicated cold rescue channel keeps it visible.
        hot = _ingest(self.ledger, "coldshadow uniquetoken 热行")
        shadowed = [hit["memory_id"] for hit in self.ledger.search(query, limit=5)]
        self.assertIn(hot.memory_id, shadowed)
        self.assertIn(cold.memory_id, shadowed,
                      "cold 兜底通道失效：热行命中时 cold 行又被遮蔽了 "
                      "（这是 2026-10-25 修掉的缺口，不要回退）")
        # The row is not gone -- it is still there and now reachable.
        self.assertIsNotNone(self.ledger.get_memory(cold.memory_id))
        # The report must describe the fixed path, not the old limitation.
        report = self._report()
        self.assertEqual(1, report["recoverability"]["cold_now"])
        self.assertEqual(0, report["recoverability"]["blocked"])
        text = forgetting.render(report)
        self.assertIn("cold 兜底通道", text)
        self.assertIn("降权不等于删除", text)
        # 旧措辞必须消失：留着它就是继续承诺一个已经不存在的限制。
        self.assertNotIn("可召回是【条件性】的", text)
        self.assertNotIn("不声称已修复", text)

    def test_recall_blockers_agree_with_search(self):
        """Each named clause must be the clause search() actually applies.

        This is the anti-drift assertion: if someone changes the recall
        predicate, the blocker names stop matching and this test fails instead
        of the report quietly promising recoverability that no longer exists.

        Each case owns a distinct query string and the assertion is about THAT
        memory_id, not about an empty result set: a shared query would let a
        neighbour's row mask the row under test.
        """
        cases = [
            ("healthy", None, None),
            # A cold row with no active competitor is reachable: no SQL clause
            # blocks it and the LIKE rescue channel runs.  The shadowing case is
            # covered separately by test_cold_recall_requires_no_hot_competitor.
            ("cold", lambda mid: self._set_lifecycle(mid, "cold"), None),
            ("superseded", lambda mid: self._set_lifecycle(mid, "superseded"),
             "lifecycle=superseded"),
            ("quarantine",
             lambda mid: self._set_lifecycle(mid, "cold", quality_state="quarantine"),
             "quality_state=quarantine"),
            ("no_evidence",
             lambda mid: self._sql("UPDATE memories SET evidence_json='[]' "
                                   "WHERE memory_id=?", (mid,)),
             "evidence_json=[]"),
            ("expired",
             lambda mid: self._sql("UPDATE memories SET valid_until=? "
                                   "WHERE memory_id=?",
                                   ("2020-01-01T00:00:00+00:00", mid)),
             "valid_until"),
            ("not_yet_valid",
             lambda mid: self._sql("UPDATE memories SET valid_from=? "
                                   "WHERE memory_id=?",
                                   ("2099-01-01T00:00:00+00:00", mid)),
             "valid_from"),
            ("raw_chat",
             lambda mid: self._sql("UPDATE memories SET raw_chat=1 WHERE memory_id=?",
                                   (mid,)),
             "raw_chat=1"),
        ]
        for index, (name, mutate, expected) in enumerate(cases):
            with self.subTest(case=name):
                # FULLY unique tokens per case (no shared prefix or filler):
                # any token shared between cases makes every earlier row a
                # candidate for this query, and an active row among them then
                # shadows the cold row under test -- the test would be
                # measuring candidate shadowing instead of the SQL predicate.
                token = (chr(ord("a") + index) * 8) + str(index)
                content = "%s %s" % (token, token[::-1])
                memory = _ingest(self.ledger, content)
                if mutate is not None:
                    mutate(memory.memory_id)
                if name != "healthy":
                    # Cold cases must also leave the hot index, as maintain does.
                    self._sql("DELETE FROM memory_fts WHERE memory_id=?",
                              (memory.memory_id,))
                row = self.ledger.get_memory(memory.memory_id)
                blockers = forgetting.recall_blockers(row, scope="shared", now=_NOW)
                hits = [hit["memory_id"] for hit in
                        self.ledger.search(content, limit=5)]
                if expected is None:
                    self.assertEqual([], blockers, "%s 不应有阻断条件" % name)
                    self.assertIn(memory.memory_id, hits)
                else:
                    self.assertTrue(blockers, "%s 应被判定为不可召回" % name)
                    self.assertNotIn(memory.memory_id, hits,
                                     "%s 实际仍可召回，阻断判定失真" % name)
                    self.assertTrue(any(expected in blocker for blocker in blockers),
                                    "%s 的阻断条件未指名 %r: %r"
                                    % (name, expected, blockers))

    def test_recall_blockers_accept_both_row_shapes(self):
        """memory_projection yields evidence_json; get_memory yields evidence.

        Reading only one of the two shapes is how a report starts declaring
        every row unrecoverable (or none) depending on its data source.
        """
        memory = _ingest(self.ledger, "记忆：两种行形状")
        raw_row = forgetting.memory_projection(
            self._reader(), [memory.memory_id])[memory.memory_id]
        self.assertIn("evidence_json", raw_row)
        parsed_row = self.ledger.get_memory(memory.memory_id)
        self.assertIn("evidence", parsed_row)
        self.assertNotIn("evidence_json", parsed_row)
        self.assertEqual([], forgetting.recall_blockers(raw_row, now=_NOW))
        self.assertEqual([], forgetting.recall_blockers(parsed_row, now=_NOW))
        # ...and both must agree once the evidence really is empty.
        self._sql("UPDATE memories SET evidence_json='[]' WHERE memory_id=?",
                  (memory.memory_id,))
        self.assertTrue(forgetting.recall_blockers(
            forgetting.memory_projection(self._reader(), [memory.memory_id])[memory.memory_id],
            now=_NOW))
        self.assertTrue(forgetting.recall_blockers(
            self.ledger.get_memory(memory.memory_id), now=_NOW))

    def test_scope_mismatch_is_named_as_a_blocker(self):
        memory = _ingest(self.ledger, "记忆：属于另一个 scope", scope="other")
        self._set_lifecycle(memory.memory_id, "cold")
        _decay(self.ledger, memory.memory_id)
        report = self._report(scope="shared")
        self.assertEqual(1, report["recoverability"]["blocked"])
        self.assertIn("scope=other", report["recoverability"]["blockers"])
        self.assertEqual([], self.ledger.search("记忆：属于另一个 scope", limit=5))
        self.assertTrue(self.ledger.search("记忆：属于另一个 scope", scope="other",
                                           limit=5))

    def test_raw_chat_rows_are_reported_as_needing_the_flag(self):
        memory = _ingest(self.ledger, "原始会话：某次调试记录", kind="raw_chat",
                         raw_chat=True)
        self._set_lifecycle(memory.memory_id, "cold")
        _decay(self.ledger, memory.memory_id, reason="raw_retention")
        report = self._report()
        self.assertEqual(1, report["recoverability"]["needs_raw_flag"])
        self.assertEqual(0, report["recoverability"]["blocked"])
        # Passing the flag must actually change the verdict, not be ignored.
        opted_in = self._report(include_raw_chat=True)
        self.assertEqual(1, opted_in["recoverability"]["predicate_reachable"])
        self.assertEqual(0, opted_in["recoverability"]["needs_raw_flag"])
        self.assertEqual(0, opted_in["recoverability"]["blocked"])
        self.assertEqual([], self.ledger.search("原始会话：某次调试记录", limit=5))
        self.assertTrue(self.ledger.search("原始会话：某次调试记录", limit=5,
                                           include_raw_chat=True))

    # -- the two age fields -------------------------------------------------

    def test_age_limit_reports_row_age_not_decay_age(self):
        """The self-contradiction this guards against.

        A row created 2000 days ago and archived for exceeding the cold age has
        age_days (since last weighting) near 0.  Printing that under a column
        labelled 年龄 makes the report say "a memory written moments ago was
        archived for being too old".  The decisive age for age_limit is
        row_age_days.
        """
        memory = _ingest(self.ledger, "记忆：因超龄被归档的老行")
        _decay(self.ledger, memory.memory_id, reason="age_limit",
               age_days=0.0, row_age_days=2000.0)
        report = self._report()
        row = report["top_drops"][0]
        self.assertEqual(0.0, row["age_days"])
        self.assertEqual(2000.0, row["row_age_days"])
        age, field = forgetting.decisive_age(row)
        self.assertEqual(2000.0, age, "age_limit 的决定性年龄必须是行龄")
        self.assertEqual("row_age_days", field)
        text = forgetting.render(report)
        self.assertIn("2000.0", text)
        self.assertIn("决定性年龄", text)

    def test_weight_floor_reports_decay_age(self):
        """The opposite case: weight_floor is driven by the decay exponent."""
        memory = _ingest(self.ledger, "记忆：因权重跌破阈值被归档")
        _decay(self.ledger, memory.memory_id, reason="weight_floor",
               age_days=41.5, row_age_days=3.0)
        row = self._report()["top_drops"][0]
        age, field = forgetting.decisive_age(row)
        self.assertEqual(41.5, age)
        self.assertEqual("age_days", field)

    def test_archive_reason_wins_over_earlier_reason(self):
        """weight_floor then age_limit: the archive rule is the answer."""
        memory = _ingest(self.ledger, "记忆：先衰减后超龄")
        _decay(self.ledger, memory.memory_id, reason="weight_floor",
               age_days=50.0, row_age_days=10.0, to_lifecycle="active",
               occurred_at=(_NOW - timedelta(days=3)).isoformat())
        _decay(self.ledger, memory.memory_id, reason="age_limit",
               age_days=0.2, row_age_days=99.0, to_lifecycle="cold",
               occurred_at=(_NOW - timedelta(days=1)).isoformat())
        row = self._report()["top_drops"][0]
        age, field = forgetting.decisive_age(row)
        self.assertEqual(99.0, age, "应取让这行真正下线的那条规则的年龄")
        self.assertEqual("row_age_days", field)

    def test_reasons_not_driven_by_age_report_no_age(self):
        for reason in ("expired", "capacity_overflow"):
            with self.subTest(reason=reason):
                memory = _ingest(self.ledger, "记忆：非年龄规则 %s" % reason)
                _decay(self.ledger, memory.memory_id, reason=reason,
                       age_days=5.0, row_age_days=7.0)
                report = self._report()
                row = next(item for item in report["top_drops"]
                           if item["memory_id"] == memory.memory_id)
                age, field = forgetting.decisive_age(row)
                self.assertIsNone(age, "%s 与年龄无关，不该显示年龄" % reason)
                self.assertEqual("", field)
                self.assertIn("n/a", forgetting.render(report))

    def test_writer_without_row_age_days_is_reported_as_a_gap(self):
        """An event that exists but cannot explain itself is still a gap.

        Silently substituting age_days would print a wrong number; silently
        printing n/a would hide that the writer is out of date.  The report does
        neither: it names the missing field and the count.
        """
        memory = _ingest(self.ledger, "记忆：旧版写入侧缺 row_age_days")
        _decay(self.ledger, memory.memory_id, reason="age_limit",
               age_days=0.0, include_row_age=False)
        report = self._report()
        row = report["top_drops"][0]
        self.assertIsNone(row["row_age_days"])
        self.assertEqual({"row_age_days": 1}, report["coverage"]["missing_age_field"])
        age, _field = forgetting.decisive_age(row)
        self.assertIsNone(age, "缺少决定性字段时不得拿另一个年龄顶替")
        text = forgetting.render(report)
        self.assertIn("缺少 row_age_days", text)
        self.assertIn("两者不可互相替代", text)

    def test_age_field_presence_is_not_a_coverage_gap(self):
        """A complete writer must not be flagged."""
        memory = _ingest(self.ledger, "记忆：完整写入侧")
        _decay(self.ledger, memory.memory_id, reason="age_limit",
               age_days=0.0, row_age_days=120.0)
        report = self._report()
        self.assertEqual({}, report["coverage"]["missing_age_field"])
        self.assertIn("age_limit", report["coverage"]["reasons_seen"])

    # -- coverage honesty: sparse evidence must not read as complete --------

    def test_sparse_events_never_report_complete_coverage(self):
        """3 explained cold rows out of 100 must show 97 unexplained.

        This is the state right after the write side lands: rows archived
        before it can never acquire an event retroactively.  Reporting that as
        "已解释" would be the exact class of lie this report exists to prevent.
        """
        explained = []
        for index in range(3):
            memory = _ingest(self.ledger, "有解释的归档 %d" % index)
            self._set_lifecycle(memory.memory_id, "cold")
            _decay(self.ledger, memory.memory_id)
            explained.append(memory.memory_id)
        for index in range(97):
            memory = _ingest(self.ledger, "无解释的归档 %d" % index)
            self._set_lifecycle(memory.memory_id, "cold")
        report = self._report()
        self.assertEqual(3, report["events"]["total"])
        self.assertEqual(100, report["coverage"]["cold_total_now"])
        self.assertEqual(3, report["coverage"]["explained_cold"])
        self.assertEqual(97, report["coverage"]["unexplained_cold"])
        self.assertEqual("partial", report["coverage"]["state"])
        self.assertEqual("部分可解释", report["verdict"]["coverage_label"])
        # A bounded yes: the 3 rows that do have events are explained with
        # evidence.  What must NOT happen is claiming the other 97 are covered.
        self.assertTrue(report["verdict"]["explainable"])
        self.assertEqual(3, report["verdict"]["explained_rows"])
        self.assertEqual(97, report["verdict"]["unexplained_rows"])
        self.assertNotEqual("complete", report["coverage"]["state"])
        self.assertIn("另 97 条无解释", report["verdict"]["headline"])
        text = forgetting.render(report)
        self.assertIn("只有 3/100 条 cold 有解释", text)
        self.assertIn("不能声称遗忘已被完整解释", text)

    def test_full_coverage_is_reported_as_complete(self):
        memory = _ingest(self.ledger, "记忆：唯一一条归档")
        self._set_lifecycle(memory.memory_id, "cold")
        _decay(self.ledger, memory.memory_id)
        report = self._report()
        self.assertEqual("complete", report["coverage"]["state"])
        self.assertTrue(report["verdict"]["explainable"])
        self.assertIn("可以逐条回答", forgetting.render(report))

    def test_significance_threshold_is_stated_in_output(self):
        """Readers must know the report only covers >=1% drops."""
        memory = _ingest(self.ledger, "记忆：阈值可见性")
        _decay(self.ledger, memory.memory_id)
        report = self._report()
        self.assertAlmostEqual(0.01, report["coverage"]["significant_drop"], places=9)
        text = forgetting.render(report)
        self.assertIn("0.010", text)
        self.assertIn("低于该阈值的缓慢漂移按设计不落事件", text)

    def test_threshold_constant_matches_the_documented_contract(self):
        """The writer's significance threshold lives in exactly one place here."""
        self.assertEqual(0.01, forgetting.SIGNIFICANT_DROP)
        self.assertIn("SIGNIFICANT_DROP", forgetting.__doc__ or "")

    def test_verdict_is_data_driven_across_coverage_states(self):
        """The same code path must produce each state, with no hardcoded text."""
        # empty
        self.assertEqual("empty", self._report()["coverage"]["state"])
        # none: cold rows, no events
        memory = _ingest(self.ledger, "记忆：状态 none")
        self._set_lifecycle(memory.memory_id, "cold")
        none_report = self._report()
        self.assertEqual("none", none_report["coverage"]["state"])
        self.assertEqual("无法解释", none_report["verdict"]["coverage_label"])
        # partial: add an explained cold row
        other = _ingest(self.ledger, "记忆：状态 partial")
        self._set_lifecycle(other.memory_id, "cold")
        _decay(self.ledger, other.memory_id)
        partial_report = self._report()
        self.assertEqual("partial", partial_report["coverage"]["state"])
        # complete: explain the first row too
        _decay(self.ledger, memory.memory_id)
        complete_report = self._report()
        self.assertEqual("complete", complete_report["coverage"]["state"])
        self.assertNotEqual(none_report["verdict"]["headline"],
                            complete_report["verdict"]["headline"])

    def test_recoverability_buckets_partition_the_memories(self):
        """Every memory must land in exactly one bucket; no row may vanish."""
        rows = []
        healthy = _ingest(self.ledger, "invariant healthy row")
        rows.append(healthy)
        cold = _ingest(self.ledger, "invariant cold row")
        self._set_lifecycle(cold.memory_id, "cold")
        rows.append(cold)
        raw = _ingest(self.ledger, "invariant raw row", kind="raw_chat", raw_chat=True)
        self._set_lifecycle(raw.memory_id, "cold")
        rows.append(raw)
        quarantined = _ingest(self.ledger, "invariant quarantined row")
        self._set_lifecycle(quarantined.memory_id, "cold", quality_state="quarantine")
        rows.append(quarantined)
        for memory in rows:
            _decay(self.ledger, memory.memory_id)
        _decay(self.ledger, "mem_vanished", reason="raw_retention")
        report = self._report()
        recall = report["recoverability"]
        self.assertEqual(5, recall["memories"])
        total = (recall["predicate_reachable"] + recall["needs_raw_flag"]
                 + recall["blocked"] + recall["gone"])
        self.assertEqual(recall["memories"], total,
                         "可召回性分桶必须恰好划分全部记忆，不能有行凭空消失")
        self.assertEqual(1, recall["gone"])
        self.assertEqual(1, recall["needs_raw_flag"])
        self.assertEqual(1, recall["blocked"])
        self.assertEqual(2, recall["predicate_reachable"])

    # -- rendering / CLI ---------------------------------------------------

    def test_render_has_every_section_and_the_recovery_statement(self):
        memory = _ingest(self.ledger, "记忆：用于渲染断言")
        self._set_lifecycle(memory.memory_id, "cold")
        _decay(self.ledger, memory.memory_id, weight_before=0.5, weight_after=0.1)
        text = forgetting.render(self._report())
        for section in ("1. 证据", "2. 为什么被降权/归档", "3. 按 agent / kind",
                        "4. 降权最狠", "5. 归档去向", "6. 还能不能召回",
                        "7. 覆盖缺口", "8. 结论", "9. 复现方式"):
            self.assertIn(section, text)
        self.assertIn("weight_floor", text)
        self.assertIn("降权不等于删除", text)
        self.assertIn("include_cold 默认 True", text)

    def test_main_json_round_trips_and_honours_days_and_limit(self):
        memory = _ingest(self.ledger, "记忆：CLI 参数")
        # 窗口断言必须相对真实当前时间：固定 _NOW 会让这条用例在
        # 2026-10-25 之后随日期推移自行失败（--days 7 窗口外）。
        now = datetime.now(timezone.utc).replace(microsecond=0)
        _decay(self.ledger, memory.memory_id, occurred_at=now.isoformat())
        _decay(self.ledger, memory.memory_id,
               occurred_at=(now - timedelta(days=30)).isoformat())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = forgetting_report.main(["--ledger", str(self.ledger.path),
                                           "--json", "--days", "7", "--limit", "3"])
        self.assertEqual(0, code)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(1, payload["events"]["total"])   # 30 天前那条在窗口外
        self.assertEqual(3, payload["limit"])
        for key in ("by_reason", "by_agent", "by_kind", "archive", "top_drops",
                    "recoverability", "coverage", "verdict", "window"):
            self.assertIn(key, payload)

    def test_main_prints_text_by_default(self):
        memory = _ingest(self.ledger, "记忆：文本输出")
        _decay(self.ledger, memory.memory_id)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = forgetting_report.main(["--ledger", str(self.ledger.path)])
        self.assertEqual(0, code)
        text = buffer.getvalue()
        self.assertIn("遗忘审计", text)
        self.assertIn("weight_floor", text)

    def test_main_missing_ledger_returns_2(self):
        missing = Path(self.tmp.name) / "nope.sqlite"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = forgetting_report.main(["--ledger", str(missing), "--json"])
        self.assertEqual(2, code)

    def test_reader_is_read_only(self):
        _ingest(self.ledger, "内容")
        with self._reader() as reader:
            with self.assertRaises(Exception):
                reader._conn.execute("DELETE FROM memories")


if __name__ == "__main__":
    unittest.main()
