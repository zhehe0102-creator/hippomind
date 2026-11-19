"""WP-Q：事实卡接入 Context Pack（opt-in）回归。

验收口径：

1. 默认关闭：fact_cards=False 与不带该选项的 pack JSON（含 debug）逐字节相等，
   且 pack 中不出现 fact_cards 键；
2. 开启：pack.text 以「当前事实卡：」前置，含当前值/历史标记，长度 <= max_chars，
   顶层 fact_cards 与 debug.fact_cards 同步；
3. memory_ids 过滤：当前卡只来自已选记忆，history 仍按 slot 聚合（旧值来自旧
   记忆不丢），无关 subject 不泄漏；
4. 去重：current_value 已出现在已选文本时该卡被跳过并计入 skipped_duplicates；
5. as_of：历史切片卡显示当时值（不受现行版本影响）；
6. 预算：max_fact_card_chars=1/5 等极小值不超限、不崩，且不挤掉既有文本；
7. 环境开关：auto_context/daemon 默认读 HIPPOMIND_FACT_CARDS（默认关闭），
   daemon body.options.fact_cards 可覆盖。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import fact_cards  # noqa: E402
from hippomind.context_pack import ContextPackBuilder, ContextPackOptions  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402

T1 = "2024-01-01T00:00:00+00:00"
T2 = "2024-06-01T00:00:00+00:00"
AS_OF_MID = "2024-03-01T00:00:00+00:00"
AS_OF_BEFORE_ALL = "2023-12-01T00:00:00+00:00"

CARD_LABEL = "当前事实卡："


def _canonical(pack):
    return json.dumps(pack, sort_keys=True, ensure_ascii=False)


class _LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-fc-inject-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        self.builder = ContextPackBuilder(self.ledger)

    def ingest(self, subject, predicate, slot_key, value, confidence, timestamp,
               content, *, task_id="", evidence="e:ref", metadata=None):
        record = {
            "content": content, "kind": "fact", "scope": "shared",
            "source_agent": "tester", "evidence": [evidence],
            "timestamp": timestamp,
            "claims": [{"subject": subject, "predicate": predicate,
                        "slot_key": slot_key, "value": value,
                        "confidence": confidence}],
        }
        if task_id:
            record["task_id"] = task_id
        if metadata:
            record["metadata"] = metadata
        return self.ledger.ingest(record)

    def seed(self):
        self.old = self.ingest(
            "db", "config", "port", 5432, 0.5, T1, "数据库端口旧值记录",
            task_id="task-db", evidence="e:old-port")
        self.new = self.ingest(
            "db", "config", "port", 6432, 0.9, T2, "数据库端口配置任务已更新",
            task_id="task-db", evidence="e:new-port",
            metadata={"context": {"goal": ["数据库端口配置"]}})
        self.theme = self.ingest(
            "db", "config", "theme", "纸本", 0.9, T1, "数据库主题配置界面为纸本",
            task_id="task-theme", evidence="e:theme")
        self.misc = self.ingest(
            "misc", "m", "k", 1, 0.6, T1, "无关配置项记录",
            task_id="task-misc", evidence="e:misc")

    def pack(self, **overrides):
        options = {"max_items": 4, "min_score": 0.0, "debug": True,
                   "fact_cards": True}
        options.update(overrides)
        return self.builder.build("数据库端口", ContextPackOptions(**options))


class DefaultOffTests(_LedgerCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_default_options_are_off(self):
        self.assertFalse(ContextPackOptions().fact_cards)
        self.assertEqual(600, ContextPackOptions().max_fact_card_chars)

    def test_off_pack_is_byte_identical_to_default_path(self):
        default = self.builder.build("数据库端口", ContextPackOptions(
            max_items=4, min_score=0.0, debug=True))
        off = self.pack(fact_cards=False)
        self.assertEqual(_canonical(default), _canonical(off),
                         "fact_cards=False 与旧默认路径必须逐字节相等")
        self.assertNotIn("fact_cards", off)
        self.assertNotIn("fact_cards", off.get("debug") or {})
        self.assertNotIn(CARD_LABEL, off["text"])

    def test_on_adds_card_block_and_metadata(self):
        pack = self.pack()
        self.assertTrue(pack["text"].startswith(CARD_LABEL))
        first_line = pack["text"].split("\n", 1)[0]
        self.assertIn("6432", first_line)
        self.assertIn("曾被 1 个旧值取代", first_line)
        self.assertLessEqual(len(pack["text"]), 16_000)
        meta = pack["fact_cards"]
        self.assertEqual({"text", "count", "skipped_duplicates", "chars"}, set(meta))
        self.assertGreaterEqual(meta["count"], 1)
        self.assertIn("6432", meta["text"])
        self.assertLessEqual(meta["chars"],
                             ContextPackOptions().max_fact_card_chars)
        self.assertEqual(meta, (pack.get("debug") or {}).get("fact_cards"))


class MemoryIdsFilterTests(_LedgerCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.cards = fact_cards.build_fact_cards(
            self.ledger, memory_ids=[self.new.memory_id])

    def test_current_cards_only_come_from_selected_memory_ids(self):
        keys = {(card["subject"], card["slot_key"]) for card in self.cards}
        self.assertEqual({("db", "port")}, keys,
                         "只给已选记忆出卡，无关 subject 不得泄漏")

    def test_history_still_aggregates_the_whole_slot(self):
        card = self.cards[0]
        self.assertEqual(6432, card["current_value"])
        self.assertEqual([5432], [item["value"] for item in card["history"]],
                         "旧值来自另一条记忆，history 必须保留")

    def test_empty_memory_ids_means_no_filter(self):
        self.assertEqual(
            _canonical(fact_cards.build_fact_cards(self.ledger, memory_ids=[])),
            _canonical(fact_cards.build_fact_cards(self.ledger)))

    def test_unknown_memory_ids_yield_no_cards(self):
        self.assertEqual([], fact_cards.build_fact_cards(
            self.ledger, memory_ids=["mem_does_not_exist"]))

    def test_pack_card_block_respects_the_filter(self):
        pack = self.pack()
        self.assertIn("db.port", pack["fact_cards"]["text"])
        self.assertNotIn("db.theme", pack["fact_cards"]["text"])
        self.assertNotIn("misc.k", pack["fact_cards"]["text"])


class DedupeTests(_LedgerCase):
    def setUp(self):
        super().setUp()
        self.only = self.ingest(
            "db", "config", "port", 6432, 0.9, T1,
            "数据库端口 6432 已生效", evidence="e:current")

    def test_card_already_in_selected_text_is_skipped(self):
        pack = self.pack()
        meta = pack["fact_cards"]
        self.assertEqual("", meta["text"])
        self.assertEqual(0, meta["count"])
        self.assertEqual(1, meta["skipped_duplicates"],
                         "current_value 已在已选文本中，卡片应被去重跳过")
        self.assertNotIn(CARD_LABEL, pack["text"])
        self.assertIn("6432", pack["text"], "既有已选文本不得被改动")

    def test_case_insensitive_dedupe(self):
        ledger2 = SQLiteLedger(Path(self.tmp.name) / "case.sqlite")
        self.addCleanup(ledger2.close)
        ledger2.ingest({
            "content": "数据库端口配置值为 ABC-6432", "kind": "fact",
            "scope": "shared", "source_agent": "tester", "evidence": ["e:1"],
            "timestamp": T1,
            "claims": [{"subject": "db", "predicate": "config",
                        "slot_key": "port", "value": "abc-6432",
                        "confidence": 0.9}]})
        pack = ContextPackBuilder(ledger2).build(
            "数据库端口", ContextPackOptions(max_items=4, min_score=0.0,
                                              debug=True, fact_cards=True))
        self.assertEqual(0, pack["fact_cards"]["count"])
        self.assertEqual(1, pack["fact_cards"]["skipped_duplicates"],
                         "去重必须大小写不敏感")
        meta = pack["fact_cards"]
        eligible = meta["count"] + meta["skipped_duplicates"]
        dedupe_hit_rate = meta["skipped_duplicates"] / float(eligible)
        self.assertEqual(1.0, dedupe_hit_rate, "全部卡都因去重被跳过时应为 100%")


class AsOfInjectionTests(_LedgerCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_historical_card_shows_the_then_current_value(self):
        pack = self.pack(as_of=AS_OF_MID, fact_cards=True)
        meta = pack["fact_cards"]
        self.assertGreaterEqual(meta["count"], 1)
        self.assertIn("5432", meta["text"])
        self.assertNotIn("6432", meta["text"],
                         "as_of 之后才生效的值不得进入历史卡")
        self.assertTrue(pack["text"].startswith(CARD_LABEL))

    def test_as_of_before_everything_has_no_pack_and_no_card_block(self):
        pack = self.pack(as_of=AS_OF_BEFORE_ALL)
        self.assertEqual([], pack["items"])
        self.assertNotIn("fact_cards", pack,
                         "空包路径不得凭空生成事实卡块")


class BudgetTests(_LedgerCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_tiny_card_budgets_never_exceed_limits_or_drop_text(self):
        baseline = self.pack(fact_cards=False)["text"]
        for budget in (1, 5, 6, 7, 10, 40, 600):
            pack = self.pack(max_fact_card_chars=budget)
            self.assertLessEqual(len(pack["text"]), 16_000, budget)
            meta = pack["fact_cards"]
            self.assertLessEqual(meta["chars"], budget,
                                 "卡片块（含标签）超出 max_fact_card_chars")
            if budget <= len(CARD_LABEL):
                self.assertEqual("", meta["text"])
                self.assertNotIn(CARD_LABEL, pack["text"])
                self.assertEqual(baseline, pack["text"],
                                 "极小预算不得挤掉既有已选文本")
            else:
                self.assertTrue(pack["text"].startswith(CARD_LABEL))
                self.assertTrue(pack["text"].endswith(baseline))

    def test_max_chars_contract_is_preserved_with_cards(self):
        for max_chars in (200, 260, 400):
            pack = self.pack(max_chars=max_chars)
            self.assertLessEqual(len(pack["text"]), max_chars)
            self.assertIn("数据库端口", pack["text"])

    def test_normalise_clamps_absurd_budgets(self):
        self.assertEqual(1, ContextPackOptions(max_fact_card_chars=-50).normalise()
                         .max_fact_card_chars)
        self.assertEqual(2_000, ContextPackOptions(max_fact_card_chars=99_999)
                         .normalise().max_fact_card_chars)
        self.assertEqual(600, ContextPackOptions(max_fact_card_chars="junk")
                         .normalise().max_fact_card_chars)


class EnvSwitchTests(_LedgerCase):
    def setUp(self):
        super().setUp()

    def test_fact_cards_enabled_env_values(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with mock.patch.dict(os.environ, {"HIPPOMIND_FACT_CARDS": value}):
                self.assertTrue(fact_cards.fact_cards_enabled(), value)
        for value in ("", "0", "off", "false", "no", "garbage"):
            with mock.patch.dict(os.environ, {"HIPPOMIND_FACT_CARDS": value}):
                self.assertFalse(fact_cards.fact_cards_enabled(), value)
        env = dict(os.environ)
        env.pop("HIPPOMIND_FACT_CARDS", None)
        self.assertFalse(fact_cards.fact_cards_enabled(env))

    def test_daemon_options_env_default_and_body_override(self):
        from hippomind.daemon import MemoryHubRequestHandler
        with mock.patch.dict(os.environ, {"HIPPOMIND_FACT_CARDS": "1"}):
            self.assertTrue(MemoryHubRequestHandler._options({}).fact_cards)
            self.assertFalse(MemoryHubRequestHandler._options(
                {"options": {"fact_cards": False}}).fact_cards,
                "body.options.fact_cards 必须覆盖环境默认")
        env = dict(os.environ)
        env.pop("HIPPOMIND_FACT_CARDS", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(MemoryHubRequestHandler._options({}).fact_cards)
            self.assertTrue(MemoryHubRequestHandler._options(
                {"options": {"fact_cards": True}}).fact_cards)
        data = MemoryHubRequestHandler._query_data(
            "query=x&fact_cards=0&max_fact_card_chars=321")
        self.assertIs(False, data["fact_cards"])
        options = MemoryHubRequestHandler._options(data).normalise()
        self.assertFalse(options.fact_cards)
        self.assertEqual(321, options.max_fact_card_chars)

    def test_auto_context_reads_env_switch(self):
        captured = {}

        class DummyBuilder:
            def __init__(self, *args, **kwargs):
                pass

            def build(self, request, options):
                captured["fact_cards"] = options.fact_cards
                return {"items": [], "text": ""}

        from hippomind import auto_context
        with mock.patch.object(auto_context, "ContextPackBuilder", DummyBuilder):
            with mock.patch.object(auto_context, "_load_vector_index",
                                   return_value=None):
                with mock.patch.object(auto_context, "_load_entity_index",
                                       return_value=None):
                    with mock.patch("hippomind.semantic_space.load_embedder",
                                    return_value=None):
                        with mock.patch.dict(os.environ,
                                             {"HIPPOMIND_FACT_CARDS": "1"}):
                            auto_context.build_auto_context(self.ledger, "查询")
                            self.assertTrue(captured["fact_cards"])
                        with mock.patch.dict(os.environ,
                                             {"HIPPOMIND_FACT_CARDS": "0"}):
                            auto_context.build_auto_context(self.ledger, "查询")
                            self.assertFalse(captured["fact_cards"])


class QuantificationTests(_LedgerCase):
    """量化口径：同一夹具下 pack 字符数 / 卡片数 / 当前事实是否显式出现。"""

    def setUp(self):
        super().setUp()
        self.seed()

    def test_quantify_chars_cards_and_explicit_current_fact(self):
        off = self.pack(fact_cards=False)
        on = self.pack(fact_cards=True)
        # 当前事实（6432）在关闭卡片时没有被显式写出，开启后由卡块提供。
        self.assertNotIn("6432", off["text"])
        self.assertIn("6432", on["text"].split("\n", 1)[0])
        self.assertTrue(on["text"].startswith(CARD_LABEL))
        meta = on["fact_cards"]
        self.assertGreaterEqual(meta["count"], 1)
        # 字符串增量 = 卡片块（含标签与换行）可逐字符核对。
        self.assertEqual(len(on["text"]) - len(off["text"]),
                         meta["chars"] + 1)
        # 去重命中率 = skipped / (skipped + 实注入卡数)；theme 卡因已选正文
        # 已含「纸本」被去重（若 theme 恰好未被选中则 skipped=0，仍可计算）。
        eligible = meta["count"] + meta["skipped_duplicates"]
        self.assertGreater(eligible, 0)
        self.assertLessEqual(meta["skipped_duplicates"], eligible)


if __name__ == "__main__":
    unittest.main()

