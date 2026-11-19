"""事实卡（fact_cards）原型回归。

验收口径：

1. 同 slot 两版更新（旧 superseded、新 active）-> 当前值=新值、history 含旧值；
2. 同 subject 不同 slot 不串卡；多版本 history 按 valid_from 倒序且有 history_limit；
3. as_of=过去返回旧值（窗口语义与 search 一致），as_of 之前没有任何卡；
4. quarantine / conflict（记忆级与 claim 级）任何时刻都不进当前卡；
5. render 严格 <= max_chars、截断有标注、中文按字符截断；
6. 空账本 / limit / subjects 过滤稳健；
7. 只读：调用前后账本主文件 sha256+mtime 不变；
8. 卡片渲染文本显著短于原始 claim JSON（本文件的 compression 测试即量化口径）。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import fact_cards  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402

T1 = "2024-01-01T00:00:00+00:00"
TMID = "2024-03-01T00:00:00+00:00"
T2 = "2024-06-01T00:00:00+00:00"
AS_OF_BEFORE_ALL = "2023-12-01T00:00:00+00:00"
AS_OF_FIRST = "2024-02-01T00:00:00+00:00"
AS_OF_MIDDLE = "2024-04-01T00:00:00+00:00"
AS_OF_AFTER = "2024-07-01T00:00:00+00:00"


def _claim(subject, predicate, slot_key, value, confidence, valid_from=None):
    claim = {"subject": subject, "predicate": predicate, "slot_key": slot_key,
             "value": value, "confidence": confidence}
    if valid_from:
        claim["valid_from"] = valid_from
    return claim


def _ingest_revision(ledger, subject, predicate, slot_key, value, confidence,
                     timestamp, content=None, evidence=None, kind="fact"):
    return ledger.ingest({
        "content": content or ("%s %s %s = %r" % (subject, predicate, slot_key, value)),
        "kind": kind, "source_agent": "tester", "scope": "shared",
        "evidence": evidence or ["t:1"], "timestamp": timestamp,
        "claims": [_claim(subject, predicate, slot_key, value, confidence)],
    })


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-factcards-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    # -- fixture helpers -------------------------------------------------
    def _memory_id(self, subject, slot_key):
        with self.ledger._reading() as conn:
            row = conn.execute(
                "SELECT memory_id FROM memory_claims WHERE scope='shared'"
                " AND subject=? AND slot_key=? ORDER BY asserted_at DESC LIMIT 1",
                (subject, slot_key)).fetchone()
        self.assertIsNotNone(row)
        return str(row[0])

    def _update_memory(self, memory_id, **columns):
        assignments = ",".join("%s=?" % name for name in columns)
        values = list(columns.values())
        self.ledger._transaction(lambda conn: conn.execute(
            "UPDATE memories SET " + assignments + " WHERE memory_id=?",
            (*values, memory_id)))

    def _update_claim(self, subject, slot_key, **columns):
        assignments = ",".join("%s=?" % name for name in columns)
        values = list(columns.values())
        self.ledger._transaction(lambda conn: conn.execute(
            "UPDATE memory_claims SET " + assignments +
            " WHERE scope='shared' AND subject=? AND slot_key=?",
            (*values, subject, slot_key)))

    def _seed(self):
        """db.port 三版（v1->v2->v3）、db.theme 一版、misc 一版、long 一版，
        外加 quarantine / conflict(memory) / conflict(claim) 三个应被排除的 slot。"""
        _ingest_revision(self.ledger, "db", "config", "port", 5432, 0.5, T1)
        _ingest_revision(self.ledger, "db", "config", "port", 5433, 0.7, TMID)
        _ingest_revision(self.ledger, "db", "config", "port", 6432, 0.9, T2)
        _ingest_revision(self.ledger, "db", "config", "theme", "纸本", 0.7, T1)
        _ingest_revision(self.ledger, "misc", "m", "k", 1, 0.6, T1)
        _ingest_revision(self.ledger, "long", "text", "body", "长" * 400, 0.6, T1)
        _ingest_revision(self.ledger, "secret", "p", "s", "quarantined", 0.6, T1)
        _ingest_revision(self.ledger, "cm", "p", "s", "memory-conflict", 0.6, T1)
        _ingest_revision(self.ledger, "cc", "p", "s", "claim-conflict", 0.6, T1)
        self._update_memory(self._memory_id("secret", "s"), quality_state="quarantine")
        self._update_memory(self._memory_id("cm", "s"), quality_state="conflict")
        self._update_claim("cc", "s", state="conflict")

    def card(self, cards, subject, slot_key):
        matches = [card for card in cards
                   if card["subject"] == subject and card["slot_key"] == slot_key]
        self.assertEqual(1, len(matches), "expected exactly one card for %s.%s"
                         % (subject, slot_key))
        return matches[0]


class CurrentCardTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self._seed()
        self.cards = fact_cards.build_fact_cards(self.ledger)

    def test_current_value_is_the_newest_active_claim(self):
        card = self.card(self.cards, "db", "port")
        self.assertEqual(6432, card["current_value"])
        self.assertAlmostEqual(0.9, card["confidence"])
        self.assertAlmostEqual(0.5, card["source_trust"])
        self.assertEqual(T2, card["valid_from"])
        self.assertEqual("config", card["predicate"])
        self.assertTrue(card["memory_id"].startswith("mem_"))
        self.assertTrue(card["updated_at"])
        self.assertEqual(["t:1"], card["evidence"])

    def test_history_contains_superseded_values_newest_first(self):
        card = self.card(self.cards, "db", "port")
        self.assertEqual([5433, 5432], [item["value"] for item in card["history"]])
        first, second = card["history"]
        self.assertEqual("superseded", first["state"])
        self.assertEqual(TMID, first["valid_from"])
        self.assertEqual(T2, first["valid_until"])
        self.assertEqual(T1, second["valid_from"])
        self.assertEqual(TMID, second["valid_until"])
        self.assertTrue(first["memory_id"].startswith("mem_"))

    def test_different_slots_do_not_mix(self):
        port = self.card(self.cards, "db", "port")
        theme = self.card(self.cards, "db", "theme")
        self.assertEqual("纸本", theme["current_value"])
        self.assertEqual([5433, 5432], [item["value"] for item in port["history"]])
        self.assertEqual([], theme["history"], "theme 的历史里不得出现 port 的旧值")
        subjects = {(card["subject"], card["slot_key"]) for card in self.cards}
        self.assertIn(("db", "port"), subjects)
        self.assertIn(("db", "theme"), subjects)

    def test_cards_are_sorted_and_one_per_group(self):
        keys = [(card["subject"], card["predicate"], card["slot_key"])
                for card in self.cards]
        self.assertEqual(sorted(keys), keys)
        self.assertEqual(len(keys), len(set(keys)), "同一 slot 只能有一张当前卡")

    def test_history_limit_is_honoured(self):
        card = self.card(fact_cards.build_fact_cards(self.ledger, history_limit=1),
                         "db", "port")
        self.assertEqual([5433], [item["value"] for item in card["history"]])
        none = self.card(fact_cards.build_fact_cards(self.ledger, history_limit=0),
                         "db", "port")
        self.assertEqual([], none["history"])

    def test_quarantine_and_conflict_are_excluded_from_current_cards(self):
        excluded = {(card["subject"], card["slot_key"]) for card in self.cards}
        for subject in ("secret", "cm", "cc"):
            self.assertNotIn((subject, "s"), excluded,
                             "%s 不应进入当前事实卡" % subject)

    def test_json_value_types_round_trip(self):
        _ingest_revision(self.ledger, "typed", "json", "object",
                         {"port": 6432, "tls": True}, 0.8, T2)
        cards = fact_cards.build_fact_cards(self.ledger, subjects=["typed"])
        card = self.card(cards, "typed", "object")
        self.assertEqual({"port": 6432, "tls": True}, card["current_value"])

    def test_evidence_is_bounded(self):
        _ingest_revision(self.ledger, "ev", "p", "s", "evidence-bounded", 0.8, T2,
                         evidence=["e:%02d" % index for index in range(20)])
        cards = fact_cards.build_fact_cards(self.ledger, subjects=["ev"])
        card = self.card(cards, "ev", "s")
        self.assertLessEqual(len(card["evidence"]), fact_cards.EVIDENCE_LIMIT)
        self.assertEqual(["e:00", "e:01"], card["evidence"][:2])


class AsOfTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self._seed()

    def test_as_of_past_returns_the_then_current_value(self):
        cards = fact_cards.build_fact_cards(self.ledger, as_of=AS_OF_FIRST)
        card = self.card(cards, "db", "port")
        self.assertEqual(5432, card["current_value"])
        self.assertEqual(T1, card["valid_from"])
        self.assertEqual([], card["history"],
                         "as_of=第一版窗口内时，后面的版本是未来信息，不得进 history")

    def test_as_of_middle_returns_the_second_revision(self):
        card = self.card(fact_cards.build_fact_cards(self.ledger, as_of=AS_OF_MIDDLE),
                         "db", "port")
        self.assertEqual(5433, card["current_value"])
        self.assertEqual([5432], [item["value"] for item in card["history"]])

    def test_as_of_after_returns_the_newest_value_with_full_history(self):
        card = self.card(fact_cards.build_fact_cards(self.ledger, as_of=AS_OF_AFTER),
                         "db", "port")
        self.assertEqual(6432, card["current_value"])
        self.assertEqual([5433, 5432], [item["value"] for item in card["history"]])

    def test_as_of_before_everything_has_no_cards(self):
        self.assertEqual([], fact_cards.build_fact_cards(
            self.ledger, as_of=AS_OF_BEFORE_ALL))

    def test_as_of_never_revives_quarantine_or_conflict(self):
        cards = fact_cards.build_fact_cards(self.ledger, as_of=AS_OF_AFTER)
        subjects = {card["subject"] for card in cards}
        for subject in ("secret", "cm", "cc"):
            self.assertNotIn(subject, subjects)

    def test_invalid_as_of_is_rejected(self):
        with self.assertRaises(ValueError):
            fact_cards.build_fact_cards(self.ledger, as_of="not-a-time")


class EmptyAndFilterTests(LedgerCase):
    def test_empty_ledger_is_stable(self):
        self.assertEqual([], fact_cards.build_fact_cards(self.ledger))
        self.assertEqual("", fact_cards.render_fact_cards([]))
        stats = fact_cards.card_coverage_stats([])
        self.assertEqual(0, stats["cards"])
        self.assertEqual(0.0, stats["avg_tokens"])
        self.assertEqual(0, stats["history_items"])

    def test_limit_and_subjects_filter(self):
        self._seed()
        all_cards = fact_cards.build_fact_cards(self.ledger)
        self.assertGreater(len(all_cards), 2)
        self.assertEqual(1, len(fact_cards.build_fact_cards(self.ledger, limit=1)))
        self.assertEqual(0, len(fact_cards.build_fact_cards(self.ledger, limit=0)))
        only_db = fact_cards.build_fact_cards(self.ledger, subjects=["db"])
        self.assertTrue(only_db)
        self.assertEqual({"db"}, {card["subject"] for card in only_db})
        self.assertEqual([], fact_cards.build_fact_cards(self.ledger,
                                                         subjects=["does-not-exist"]))

    def test_scope_isolation(self):
        self._seed()
        self.ledger.ingest({
            "content": "private scope fact", "kind": "fact", "source_agent": "tester",
            "scope": "private", "evidence": ["t:private"], "timestamp": T2,
            "claims": [_claim("db", "config", "private-slot", "private", 0.8)],
        })
        cards = fact_cards.build_fact_cards(self.ledger)
        self.assertNotIn("private-slot", {card["slot_key"] for card in cards})
        private_cards = fact_cards.build_fact_cards(self.ledger, scope="private")
        self.assertEqual(["private"], [card["current_value"] for card in private_cards])


class RenderTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self._seed()
        self.cards = fact_cards.build_fact_cards(self.ledger)

    def test_render_is_one_line_per_card(self):
        rendered = fact_cards.render_fact_cards(self.cards)
        lines = rendered.split("\n")
        self.assertEqual(len(self.cards), len(lines))
        self.assertTrue(all(line.strip() for line in lines))
        joined = rendered
        self.assertIn("db.port: 6432", joined)
        self.assertIn("置信度 0.90", joined)
        self.assertIn("生效 2024-06-01", joined)
        self.assertIn("曾被 2 个旧值取代", joined)
        self.assertNotIn("5433", joined, "历史旧值不展开进渲染文本")

    def test_render_respects_max_chars_and_marks_truncation(self):
        rendered = fact_cards.render_fact_cards(self.cards, max_chars=120)
        self.assertLessEqual(len(rendered), 120)
        self.assertIn("已截断", rendered)
        self.assertIn("张卡", rendered)

    def test_render_with_tiny_budget_still_obeys_the_ceiling(self):
        for budget in (0, 1, 5, 16, 40):
            rendered = fact_cards.render_fact_cards(self.cards, max_chars=budget)
            self.assertLessEqual(len(rendered), budget)

    def test_render_truncates_long_cjk_without_exceeding_limit(self):
        rendered = fact_cards.render_fact_cards(self.cards, max_chars=80)
        self.assertLessEqual(len(rendered), 80)
        self.assertTrue(rendered)
        # 截断后的字符数按 Python str 计（中文字符=1），不是字节数。
        self.assertEqual(len(rendered), len(str(rendered)))

    def test_render_default_budget_is_enough_for_normal_fixture(self):
        rendered = fact_cards.render_fact_cards(self.cards)
        self.assertLess(len(rendered), fact_cards.DEFAULT_RENDER_CHARS)
        self.assertNotIn("已截断", rendered)

    def test_card_render_is_much_compacter_than_raw_claim_json(self):
        raw_rows = []
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT c.subject,c.predicate,c.slot_key,c.value_json,"
                "c.valid_from,c.valid_until,c.state,c.memory_id,c.confidence "
                "FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id "
                "WHERE c.scope='shared' AND m.scope='shared' "
                "AND c.state IN ('active','superseded','expired')"
                " ORDER BY c.subject,c.predicate,c.slot_key,c.valid_from DESC").fetchall()
        for row in rows:
            raw_rows.append(dict(row))
        raw_text = json.dumps(raw_rows, ensure_ascii=False, separators=(",", ":"))
        rendered = fact_cards.render_fact_cards(self.cards)
        self.assertLess(len(rendered), len(raw_text),
                        "事实卡必须比原始 claim JSON 紧凑")
        self.assertLess(len(rendered), len(raw_text) * 0.5,
                        "目标：至少压缩一半（报告用这一夹具量化）")


class MemoryIdFilterTests(LedgerCase):
    """WP-Q：memory_ids 非空时，当前卡只来自已选记忆，history 仍按 slot 聚合。"""

    def setUp(self):
        super().setUp()
        self._seed()
        self.current_port_memory = self._memory_id("db", "port")

    def test_current_cards_only_come_from_given_memories(self):
        cards = fact_cards.build_fact_cards(
            self.ledger, memory_ids=[self.current_port_memory])
        keys = {(card["subject"], card["slot_key"]) for card in cards}
        self.assertEqual({("db", "port")}, keys)
        card = self.card(cards, "db", "port")
        self.assertEqual(6432, card["current_value"])

    def test_history_still_aggregates_across_memories(self):
        cards = fact_cards.build_fact_cards(
            self.ledger, memory_ids=[self.current_port_memory])
        card = self.card(cards, "db", "port")
        self.assertEqual([5433, 5432],
                         [item["value"] for item in card["history"]],
                         "history 必须仍可来自其它（已下线的）记忆")

    def test_empty_memory_ids_is_the_same_as_no_filter(self):
        self.assertEqual(
            fact_cards.build_fact_cards(self.ledger, memory_ids=[]),
            fact_cards.build_fact_cards(self.ledger))

    def test_unknown_memory_ids_yield_no_current_cards(self):
        self.assertEqual([], fact_cards.build_fact_cards(
            self.ledger, memory_ids=["mem_does_not_exist"]))

    def test_memory_ids_filter_with_as_of(self):
        cards = fact_cards.build_fact_cards(
            self.ledger, as_of=AS_OF_MIDDLE,
            memory_ids=[self.current_port_memory])
        # v3 记忆的 claim 在 as_of 时还没生效；当前卡必须来自可见 claim。
        self.assertEqual([], cards)


class CoverageStatsTests(LedgerCase):
    def test_stats_count_groups_histories_and_tokens(self):
        self._seed()
        cards = fact_cards.build_fact_cards(self.ledger)
        stats = fact_cards.card_coverage_stats(cards)
        self.assertEqual(len(cards), stats["cards"])
        self.assertEqual(len(cards), stats["groups"])
        self.assertEqual(len({card["subject"] for card in cards}),
                         stats["subject_count"])
        self.assertEqual(sum(1 for card in cards if card["history"]),
                         stats["with_history"])
        self.assertEqual(sum(len(card["history"]) for card in cards),
                         stats["history_items"])
        self.assertGreater(stats["avg_tokens"], 0)
        self.assertGreaterEqual(stats["max_tokens"], stats["avg_tokens"])
        self.assertEqual(sum(len(fact_cards._render_card_line(card)) for card in cards),
                         stats["rendered_chars"])

    def test_stats_are_json_serialisable(self):
        self._seed()
        cards = fact_cards.build_fact_cards(self.ledger)
        payload = fact_cards.card_coverage_stats(cards)
        self.assertEqual(payload, json.loads(json.dumps(payload)))


class ReadOnlyTests(LedgerCase):
    def test_reading_cards_does_not_touch_the_ledger_file(self):
        self._seed()
        path = Path(self.ledger.path)
        before = (_sha256(path), path.stat().st_mtime_ns, path.stat().st_size)
        for _ in range(3):
            cards = fact_cards.build_fact_cards(self.ledger)
            fact_cards.render_fact_cards(cards)
            fact_cards.card_coverage_stats(cards)
        after = (_sha256(path), path.stat().st_mtime_ns, path.stat().st_size)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
