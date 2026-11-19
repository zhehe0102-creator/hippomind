"""实体第三路检索信号（task-3 / HEALTH_REVIEW 第四节缺口 2 的前半）。

背景：检索原本只有两路候选生成（词法 FTS/LIKE + 神经向量），实体只用于**事后**
图扩展。mem0 v3 把 entity 也做成检索信号。本文件锁定 hippomind/entity_index.py
与 ledger.search(entity_index=...) 的契约。

最重要的一条：**开关关闭时行为逐字节不变**。不是「结果差不多」，是
score_parts 的键集合、channel_count、rrf 公式全部与改造前相同。

写这些测试时踩到的一件事，记在这里：实体通道只在**查询里真的出现了某个实体
名**时才会命中（这是它的定义，不是缺陷）。所以每条断言实体命中的用例，查询
都必须写上那个专有名词——用「数据库端口」去断言能命中实体 postgresql 是错的。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.entity_index import (  # noqa: E402
    DEFAULT_ENTITY_TOPN,
    _matches_in_text,
    is_machine_id,
    EntityIndex,
    entity_channel_enabled,
    entity_terms,
    is_indexable_entity,
    load_entity_index,
    normalise_entity,
)
from hippomind.ledger import SQLiteLedger  # noqa: E402

ENTITY_KEYS = {"entity", "entity_rank", "entity_score"}

#: 改造前 ledger.search 写进 score_parts 的全部键（冻结的字面量）。
#: 默认路径（不传 vector_index / entity_index / embedder）必须恰好是这一组。
HISTORICAL_SCORE_PART_KEYS = {
    "topic", "fts", "coverage", "phrase",
    "rrf", "time", "touch", "weight",
    "confidence", "trust", "quality",
    "utility", "task_scope", "structured",
    "pinned",
}


class EntityTermTests(unittest.TestCase):
    """实体抽取与归一：不需要 NER，claim 的 subject/slot_key 就是实体。"""

    def test_normalises_case_and_whitespace(self):
        self.assertEqual("hippomind", normalise_entity("  HippoMind  "))
        self.assertEqual("adb 设备连接排查",
                         normalise_entity("ADB   设备连接排查"))
        self.assertEqual("", normalise_entity(None))
        self.assertEqual("", normalise_entity("   "))

    def test_short_ascii_entities_are_dropped_but_short_cjk_kept(self):
        # 2 字符 ASCII 实体（ai/ok/v1）参与倒排等于全库噪声源。
        self.assertFalse(is_indexable_entity("ai"))
        self.assertTrue(is_indexable_entity("adb"))
        self.assertTrue(is_indexable_entity("端口"))
        self.assertFalse(is_indexable_entity("端"))
        self.assertFalse(is_indexable_entity(""))

    def test_subject_and_slot_key_both_contribute(self):
        terms = entity_terms("HippoMind", "storage_backend")
        self.assertIn("hippomind", terms)
        self.assertIn("storage_backend", terms, "整串判别力最高，必须保留")
        self.assertIn("storage", terms, "分词让 storage 单独也能触达")
        self.assertEqual(len(terms), len(set(terms)), "不得重复")

    def test_cjk_entity_is_kept_whole(self):
        # subject 整串保留、**不**分词（实测：分词会多出 168 个噪声实体，
        # recall 不变而 keyword MRR 略降）。
        terms = entity_terms("ADB 设备连接排查", "task_status")
        self.assertEqual("adb 设备连接排查", terms[0])
        self.assertNotIn("设备连接排查", terms,
                         "subject 不得被分词")

    def test_subject_is_not_split_but_slot_key_is(self):
        # 这个不对称是刻意设计：subject 是实体名，slot_key 是机器槽位。
        terms = entity_terms("RelayDesk Omni", "storage_backend")
        self.assertEqual(["relaydesk omni", "storage_backend", "storage",
                          "backend"], terms)
        # 中文查询要能靠槽位分词触达（这是实体通道对中文唯一的入口）。
        self.assertIn("storage", terms)

    def test_blank_subject_and_slot_yield_no_terms(self):
        self.assertEqual([], entity_terms("", ""))
        self.assertEqual([], entity_terms(None, None))

    def test_machine_ids_are_rejected(self):
        # 这四类形态都是生产库里**实际存在**的（不是构造的样例）：
        # subject 里 1,554 条 UUID、117 条 mem_xxx，以及 UUID 按连字符切出来
        # 的十六进制碎片。它们建进倒排就是纯噪声。
        for value in ("01a0744d-180c-739e-b9e0-e747618a5d7a",   # 完整 UUID
                      "mem_06b11f27e69c0a37bd6a8799643df534",      # mem_ 前缀
                      "evt-abc123def456", "rel_0123456789abcdef",  # 其他前缀
                      "1922574a",                                # 8 位碎片
                      "9ee8", "8409",                            # 4 位碎片
                      "00380702ad32de276e7758135f8adaf3",        # 32 位 hex
                      "1234",                                    # 纯数字
                      "0"):
            self.assertTrue(is_machine_id(value), value)
            self.assertFalse(is_indexable_entity(value), value)
            self.assertEqual([], entity_terms(value, None), value)

    def test_real_entity_names_are_not_mistaken_for_machine_ids(self):
        for value in ("HippoMind", "NeoZygisk", "RelayDesk Omni",
                      "cache-02", "Zygisk Next v1.5.2", "fei-ke/HMSPush",
                      "ADB 设备连接排查", "三星手机推送延迟修复项目"):
            self.assertFalse(is_machine_id(value), value)
            self.assertTrue(is_indexable_entity(value), value)

    def test_uuid_fragments_are_never_produced_by_splitting(self):
        # UUID 分词会切出 9ee8 / 8409 / 1922574a 这类碎片。整串丢弃之后
        # 一个碎片都不该出现。
        terms = entity_terms("1922574a-8409-405c-9ee8-0a81c9df6402",
                            "task_status")
        self.assertEqual(["task_status", "task", "status"], terms)

    def test_ascii_entities_match_on_word_boundaries_only(self):
        # 无边界子串是错的：实测 anti 会命中 quantization、mac 会命中 macabre，
        # 把完全无关的记忆拉进候选集。
        self.assertFalse(_matches_in_text("anti", "quantization"))
        self.assertTrue(_matches_in_text("anti", "anti-aliasing"))
        self.assertFalse(_matches_in_text("mac", "macabre"))
        # 但连字符/下划线/点分隔的复合词必须仍然命中——那正是期望行为。
        self.assertTrue(_matches_in_text("mac", "mac-mini 部署"))
        self.assertTrue(_matches_in_text("mac", "macOS 版本"))
        self.assertTrue(_matches_in_text("storage", "storage_backend"))

    def test_cjk_entities_match_as_substrings(self):
        # 中文没有词边界，子串是唯一正确的判定。
        self.assertTrue(_matches_in_text("adb 设备连接排查",
                                        "adb 设备连接排查怎么做"))
        self.assertTrue(_matches_in_text("端口", "数据库端口是多少"))


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-entity-channel-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "e.sqlite")
        self.addCleanup(self.ledger.close)

    def _memory(self, content, *, subject="", slot="", value="", scope="shared",
                task_id="", kind="fact"):
        record = {"content": content, "kind": kind, "source_agent": "tester",
                  "scope": scope, "task_id": task_id,
                  "evidence": ["test:" + content[:24]]}
        if subject:
            record["claims"] = [{"subject": subject, "predicate": "state",
                                 "slot_key": slot or (subject + ":state"),
                                 "value": value or content[:24],
                                 "confidence": 0.8}]
        return self.ledger.ingest(record)

    @staticmethod
    def _keys(hits):
        return set().union(*[set(hit["score_parts"]) for hit in hits])


class EntityIndexBuildTests(LedgerCase):
    """倒排构建：覆盖集合与 FTS 一致、可重建、失败降级。"""

    def test_build_maps_entity_to_memory_ids(self):
        first = self._memory("HippoMind 默认 token 预算 6000",
                             subject="HippoMind", slot="default_token_budget")
        second = self._memory("HippoMind 存储后端 SQLite",
                              subject="HippoMind", slot="storage_backend")
        index = EntityIndex.build(self.ledger)
        self.assertGreater(len(index), 0)
        hits = {memory_id for memory_id, _score, _entity
                in index.search_text("HippoMind", topn=10)}
        self.assertIn(first.memory_id, hits)
        self.assertIn(second.memory_id, hits)

    def test_only_accepted_active_claims_are_indexed(self):
        # 与 FTS / 向量同一集合：隔离的记忆不得进倒排。
        keep = self._memory("保留的 NeoZygisk 记忆", subject="NeoZygisk",
                            slot="version")
        drop = self._memory("隔离的 NeoZygisk 记忆", subject="NeoZygisk",
                            slot="data_dir_exists")
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET quality_state=? WHERE memory_id=?",
                         ("quarantine", drop.memory_id))
        index = EntityIndex.build(self.ledger)
        memory_ids = {memory_id for memory_id, _score, _entity
                      in index.search_text("NeoZygisk", topn=20)}
        self.assertIn(keep.memory_id, memory_ids)
        self.assertNotIn(drop.memory_id, memory_ids)

    def test_rare_entity_outranks_common_entity(self):
        # idf：命中一个稀有实体比命中一个烂大街的词更能定位一条记忆。
        for index in range(6):
            self._memory("常见实体记忆 %d" % index, subject="HippoMind",
                         slot="slot_%d" % index)
        rare = self._memory("稀有实体记忆", subject="NeoZygisk", slot="version")
        entity_index = EntityIndex.build(self.ledger)
        results = entity_index.search_text("NeoZygisk HippoMind", topn=10)
        self.assertTrue(results)
        self.assertEqual(rare.memory_id, results[0][0],
                         "稀有实体命中的记忆必须排在常见实体之前")

    def test_rebuild_is_deterministic(self):
        self._memory("确定性样本 A", subject="svc-a", slot="state")
        self._memory("确定性样本 B", subject="svc-b", slot="state")
        first = EntityIndex.build(self.ledger).search_text("svc-a", topn=10)
        second = EntityIndex.build(self.ledger).search_text("svc-a", topn=10)
        self.assertEqual(first, second)

    def test_load_returns_none_on_empty_or_broken_ledger(self):
        self.assertIsNone(load_entity_index(self.ledger),
                          "空账本没有实体，通道必须静默不可用")

        class Boom:
            def _reading(self):
                raise RuntimeError("ledger down")

        self.assertIsNone(load_entity_index(Boom()))
        self.assertIsNone(load_entity_index(Boom(), build=False))

    def test_env_switch_defaults_to_off(self):
        import os
        previous = os.environ.pop("HIPPOMIND_ENTITY_CHANNEL", None)
        try:
            self.assertFalse(entity_channel_enabled())
            os.environ["HIPPOMIND_ENTITY_CHANNEL"] = "1"
            self.assertTrue(entity_channel_enabled())
            os.environ["HIPPOMIND_ENTITY_CHANNEL"] = "off"
            self.assertFalse(entity_channel_enabled())
        finally:
            os.environ.pop("HIPPOMIND_ENTITY_CHANNEL", None)
            if previous is not None:
                os.environ["HIPPOMIND_ENTITY_CHANNEL"] = previous

    def test_index_cache_follows_the_ledger_generation(self):
        # 缓存必须随写事务失效：否则新写入的记忆永远进不了倒排。
        first = self._memory("缓存失效样本 A", subject="CacheProbe",
                             slot="state")
        index = load_entity_index(self.ledger)
        self.assertIsNotNone(index)
        self.assertIn(first.memory_id,
                      {m for m, _s, _e in index.search_text("CacheProbe", topn=10)})
        # 新写入一个同实体记忆 -> generation 自增 -> 缓存必须重建。
        second = self._memory("缓存失效样本 B", subject="CacheProbe",
                              slot="state_b")
        refreshed = load_entity_index(self.ledger)
        self.assertIsNot(refreshed, index, "写事务后必须重建")
        self.assertIn(second.memory_id,
                      {m for m, _s, _e in refreshed.search_text("CacheProbe", topn=10)})

    def test_repeated_loads_reuse_the_same_instance(self):
        self._memory("复用样本", subject="ReuseProbe", slot="state")
        first = load_entity_index(self.ledger)
        second = load_entity_index(self.ledger)
        self.assertIs(first, second, "同一代账本必须复用同一个索引实例")
    def test_stats_report_the_rebuildable_shape(self):
        self._memory("统计样本", subject="svc-a", slot="state")
        stats = EntityIndex.build(self.ledger).stats()
        self.assertEqual("entity", stats["granularity"])
        self.assertGreaterEqual(stats["entities"], 1)
        self.assertGreaterEqual(stats["memories"], 1)
        self.assertEqual("", stats["path"], "实体索引不落盘")


class SearchIntegrationTests(LedgerCase):
    """ledger.search(entity_index=...) 的融合契约。"""

    def setUp(self):
        super().setUp()
        self.db = self._memory("生产环境 PostgreSQL 数据库端口是 5432",
                               subject="PostgreSQL", slot="db_port")
        self.cache = self._memory("缓存集群部署在 cache-02 机器",
                                  subject="cache-02", slot="host")
        self.index = EntityIndex.build(self.ledger)
        # 这条查询同时命中词法（端口）与实体（postgresql），用于对照。
        self.query = "PostgreSQL 数据库端口"

    def test_default_search_has_no_entity_trace(self):
        """默认路径（不传 entity_index）不得出现任何实体痕迹。"""
        hits = self.ledger.search(self.query, scope="shared", limit=5)
        self.assertTrue(hits)
        for hit in hits:
            self.assertFalse(ENTITY_KEYS & set(hit["score_parts"]),
                             "默认路径不得出现实体通道的键")

    def test_default_score_parts_key_set_is_the_historical_one(self):
        """默认路径的 score_parts 键集合必须与改造前**逐键相同**。"""
        """
        这里钉的是字面量而不是「等于改造前算出来的东西」：改造前没有别的
        地方能算，只有冻结一份期望值才能防住「多加一个键但没人发现」。
        """
        hits = self.ledger.search(self.query, scope="shared", limit=5)
        self.assertTrue(hits)
        self.assertEqual(HISTORICAL_SCORE_PART_KEYS, set(hits[0]["score_parts"]))

    def test_score_parts_key_set_differs_by_exactly_the_entity_keys(self):
        """「逐字节不变」的可执行定义：键集合的差集恰好是实体三键。

        不是断言「结果差不多」——是断言关闭时连一个多余的键都没有，
        打开时不多不少只多这三个。
        """
        baseline = self.ledger.search(self.query, scope="shared", limit=5)
        enabled = self.ledger.search(self.query, scope="shared", limit=5,
                                     entity_index=self.index)
        self.assertTrue(baseline and enabled)
        baseline_keys = self._keys(baseline)
        enabled_keys = self._keys(enabled)
        self.assertEqual(set(), baseline_keys & ENTITY_KEYS)
        self.assertEqual(ENTITY_KEYS, enabled_keys - baseline_keys,
                         "打开实体通道只允许新增这三个键")
        self.assertEqual(set(), baseline_keys - enabled_keys,
                         "打开实体通道不得让任何既有键消失")

    def test_channel_count_ignores_a_channel_with_no_candidates(self):
        """空通道不得抬高 rrf 归一化分母。

        rrf_norm = rrf / (channel_count / (k+1))。若把一个「打开了但零命中」的
        通道也算进 channel_count，分母被抬高，所有既有通道的分数被无端稀释——
        那等于「打开开关但没命中」反而让排序变差。
        """
        # 查询里不含任何已索引实体 -> 实体通道零命中 -> 分母必须仍是 2/61。
        no_entity_query = "完全无关的一句话内容"
        baseline = self.ledger.search(no_entity_query, scope="shared", limit=5)
        enabled = self.ledger.search(no_entity_query, scope="shared", limit=5,
                                     entity_index=self.index)
        self.assertEqual(len(baseline), len(enabled))
        for left, right in zip(baseline, enabled):
            self.assertEqual(left["memory_id"], right["memory_id"])
            self.assertEqual(left["score_parts"]["rrf"],
                             right["score_parts"]["rrf"],
                             "零命中时 rrf 必须逐字节相同")

    def test_entity_channel_recalls_a_lexically_missed_memory(self):
        """改写式查询：词法完全没命中，但实体通道能把它拉进来。"""
        target = self._memory("部署笔记见附件", subject="NeoZygisk",
                              slot="zygote_injection")
        index = EntityIndex.build(self.ledger)
        query = "NeoZygisk 怎么注入 zygote"
        lexical = self.ledger.search(query, scope="shared", limit=5)
        self.assertNotIn(target.memory_id, {hit["memory_id"] for hit in lexical},
                         "前提：该记忆在词法通道上不可达")
        fused = self.ledger.search(query, scope="shared", limit=5,
                                   entity_index=index)
        self.assertIn(target.memory_id, {hit["memory_id"] for hit in fused},
                      "实体通道必须生成候选")
        matched = [hit for hit in fused
                   if hit["memory_id"] == target.memory_id][0]
        self.assertIn("entity_rank", matched["score_parts"])
        self.assertEqual("neozygisk", matched["score_parts"]["entity"])
        self.assertGreater(matched["score_parts"]["entity_score"], 0.0)

    def test_entity_candidates_respect_scope_filter(self):
        """过滤必须在 SQL 里重放：倒排覆盖全库，不能把别的 scope 带进来。"""
        private = self._memory("私有作用域的 NeoZygisk 记忆", subject="NeoZygisk",
                               slot="private_note", scope="private")
        index = EntityIndex.build(self.ledger)
        hits = self.ledger.search("NeoZygisk", scope="shared", limit=10,
                                  entity_index=index)
        self.assertNotIn(private.memory_id, {hit["memory_id"] for hit in hits})
        for hit in hits:
            self.assertEqual("shared", hit["scope"],
                             "实体通道不得绕过 scope 过滤")

    def test_entity_candidates_respect_require_evidence(self):
        stripped = self._memory("没有证据的 NeoZygisk 记忆", subject="NeoZygisk",
                                slot="no_evidence")
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET evidence_json=? WHERE memory_id=?",
                         ("[]", stripped.memory_id))
        index = EntityIndex.build(self.ledger)
        hits = self.ledger.search("NeoZygisk", scope="shared", limit=10,
                                  entity_index=index, require_evidence=True)
        self.assertNotIn(stripped.memory_id, {hit["memory_id"] for hit in hits})
        for hit in hits:
            self.assertTrue(hit["evidence"], "实体通道不得绕过证据门槛")

    def test_switching_the_entity_channel_does_not_hit_stale_cache(self):
        first = self.ledger.search(self.query, scope="shared", limit=5)
        second = self.ledger.search(self.query, scope="shared", limit=5,
                                    entity_index=self.index)
        self.assertFalse(ENTITY_KEYS & self._keys(first),
                         "开关关闭的结果里不得有实体键（否则说明命中了旧缓存）")
        self.assertIn("entity_rank", self._keys(second))


    def test_cache_key_differs_when_the_entity_channel_toggles(self):
        """cache_key 必须包含实体通道：否则切换开关会命中旧缓存，A/B 全废。

        直接断言两个键不相等（而不是只看结果），因为「结果碰巧一样」在
        小账本上完全可能——那会让这条测试在真正的 bug 面前保持绿色。
        """
        from hippomind.ledger.search import SearchMixin
        captured = []
        original = SearchMixin._search_cached

        def spy(self, key):
            captured.append(key)
            return original(self, key)

        SearchMixin._search_cached = spy
        try:
            self.ledger.search(self.query, scope="shared", limit=5)
            self.ledger.search(self.query, scope="shared", limit=5,
                               entity_index=self.index)
        finally:
            SearchMixin._search_cached = original
        self.assertEqual(2, len(captured))
        self.assertNotEqual(captured[0], captured[1],
                            "开关实体通道必须改变 cache_key")
        # 实体字段**总是**追加在末尾（关闭时是 None/None），因此两个键等长、
        # 前缀逐字节相同、只有最后两位不同。这条同时钉住了「只追加不插入」：
        # 一旦有人把字段插到中间，前缀比较立刻失败。
        self.assertEqual(len(captured[0]), len(captured[1]))
        self.assertEqual(captured[0][:-2], captured[1][:-2],
                         "cache_key 的既有前缀必须逐字节不变（只允许末尾追加）")
        self.assertEqual((None, None), captured[0][-2:],
                         "关闭时两个末尾字段必须是 None")
        self.assertIsNotNone(captured[1][-2])

    def test_entity_topn_is_part_of_the_cache_key(self):
        from hippomind.ledger.search import SearchMixin
        captured = []
        original = SearchMixin._search_cached

        def spy(self, key):
            captured.append(key)
            return original(self, key)

        SearchMixin._search_cached = spy
        try:
            self.ledger.search(self.query, scope="shared", limit=5,
                               entity_index=self.index, entity_topn=8)
            self.ledger.search(self.query, scope="shared", limit=5,
                               entity_index=self.index, entity_topn=16)
        finally:
            SearchMixin._search_cached = original
        self.assertNotEqual(captured[0], captured[1],
                            "entity_topn 也必须进 cache_key")

    def test_broken_index_degrades_to_lexical(self):
        class Boom:
            def search_text(self, query, topn=96):
                raise RuntimeError("index down")

        hits = self.ledger.search(self.query, scope="shared", limit=5,
                                  entity_index=Boom())
        self.assertTrue(hits)
        self.assertFalse(ENTITY_KEYS & self._keys(hits))

    def test_entity_topn_is_honoured(self):
        for index in range(4):
            self._memory("PostgreSQL 端口变体 %d" % index, subject="PostgreSQL",
                         slot="db_port_%d" % index)
        index = EntityIndex.build(self.ledger)
        hits = self.ledger.search("PostgreSQL", scope="shared", limit=10,
                                  entity_index=index, entity_topn=1)
        ranked = [hit for hit in hits if "entity_rank" in hit["score_parts"]]
        self.assertLessEqual(len(ranked), 1)

    def test_entity_and_vector_channels_can_coexist(self):
        """两路可选通道同时开启时 channel_count=4，且各自留下审计字段。"""

        class FakeVectorIndex:
            def __init__(self, memory_id):
                self.memory_id = memory_id

            def search_text(self, query, topn=96):
                return [(self.memory_id, 0.9, "cl_fake")]

        fake = FakeVectorIndex(self.db.memory_id)
        hits = self.ledger.search(self.query, scope="shared", limit=5,
                                  vector_index=fake, entity_index=self.index)
        self.assertTrue(hits)
        keys = self._keys(hits)
        self.assertIn("vec_rank", keys)
        self.assertIn("entity_rank", keys)
        for hit in hits:
            self.assertGreaterEqual(hit["score_parts"]["rrf"], 0.0)
            self.assertLessEqual(hit["score_parts"]["rrf"], 1.0)


class CandidateGenerationContractTests(LedgerCase):
    """候选生成是「多条通路」的集合：实体通道必须真的把行放进候选集。"""

    def test_entity_channel_generates_candidates_not_just_filters(self):
        """回归锁定：只放宽 WHERE 而不扩候选生成，等于没有生效。

        构造方式：让目标记忆在**词法上完全不可达**（正文与查询无任何共享词元），
        只有 claim 的 subject 与查询相同。若实体通道只过滤不生成，这条记忆
        永远不会出现在结果里。
        """
        target = self._memory("见附件第七节", subject="RelayDesk",
                              slot="launch_mode")
        index = EntityIndex.build(self.ledger)
        query = "RelayDesk 是怎么启动的"
        lexical = self.ledger.search(query, scope="shared", limit=10)
        self.assertNotIn(target.memory_id, {hit["memory_id"] for hit in lexical})
        fused = self.ledger.search(query, scope="shared", limit=10,
                                   entity_index=index)
        self.assertIn(target.memory_id, {hit["memory_id"] for hit in fused})

    def test_empty_entity_index_degrades_to_lexical(self):
        target = self._memory("数据库端口是 5432", subject="PostgreSQL",
                              slot="db_port")
        empty = EntityIndex({})
        hits = self.ledger.search("PostgreSQL 数据库端口", scope="shared",
                                  limit=5, entity_index=empty)
        self.assertIn(target.memory_id, {hit["memory_id"] for hit in hits})
        self.assertFalse(ENTITY_KEYS & self._keys(hits))

    def test_entity_candidates_are_appended_after_lexical_candidates(self):
        """实体候选必须在词法候选生成**全部跑完**之后才追加。

        否则「实体先找到东西」会让 LIKE 救援通道被跳过——开启实体通道反而
        改变了词法行为，那就不是叠加而是替换。用一条只有 LIKE 救援能命中的
        短词查询来钉住：加上实体通道后它仍然能被救援到。
        """
        short = self._memory("端口 是 5432", subject="端口", slot="value")
        index = EntityIndex.build(self.ledger)
        baseline = self.ledger.search("端口 5432", scope="shared", limit=5)
        self.assertIn(short.memory_id, {hit["memory_id"] for hit in baseline})
        fused = self.ledger.search("端口 5432", scope="shared", limit=5,
                                   entity_index=index)
        self.assertIn(short.memory_id, {hit["memory_id"] for hit in fused},
                      "开启实体通道后词法救援通道必须仍然有效")

    def test_default_entity_topn_matches_the_vector_channel_scale(self):
        self.assertEqual(96, DEFAULT_ENTITY_TOPN)



class ProductionWiringTests(LedgerCase):
    """接线测试：实体通道必须真的接到生产注入路径上，而不只是存在。

    没有这组测试时出现过的情况：`search(entity_index=...)` 写好了、测试也绿，
    但**没有任何生产代码调用它**——那不是「默认关闭」，那是死代码。
    """

    def setUp(self):
        super().setUp()
        self.previous = __import__("os").environ.pop("HIPPOMIND_ENTITY_CHANNEL", None)
        self.addCleanup(self._restore_env)
        self._memory("NeoZygisk 注入 zygote 的步骤", subject="NeoZygisk",
                     slot="zygote_injection")

    def _restore_env(self):
        import os
        os.environ.pop("HIPPOMIND_ENTITY_CHANNEL", None)
        if self.previous is not None:
            os.environ["HIPPOMIND_ENTITY_CHANNEL"] = self.previous

    def test_auto_context_loader_is_off_by_default(self):
        from hippomind import auto_context
        self.assertIsNone(auto_context._load_entity_index(self.ledger),
                          "开关默认关闭时不得构建索引")

    def test_auto_context_loader_returns_an_index_when_enabled(self):
        import os
        from hippomind import auto_context
        os.environ["HIPPOMIND_ENTITY_CHANNEL"] = "1"
        index = auto_context._load_entity_index(self.ledger)
        self.assertIsNotNone(index)
        self.assertGreater(len(index), 0)

    def test_context_pack_builder_forwards_the_index_to_search(self):
        """ContextPackBuilder 必须把 entity_index 传下去（不是只存成属性）。"""
        from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
        index = load_entity_index(self.ledger)
        self.assertIsNotNone(index)
        pack = ContextPackBuilder(self.ledger, entity_index=index).build(
            "NeoZygisk 注入 zygote",
            ContextPackOptions(max_items=8, min_score=0.0, debug=True))
        candidates = (pack.get("debug") or {}).get("candidates") or []
        self.assertTrue(candidates)
        self.assertTrue(
            [c for c in candidates if "entity" in (c.get("score_parts") or {})],
            "生产 pack 的候选里必须出现 entity 键")

    def test_context_pack_builder_defaults_to_no_entity_channel(self):
        from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
        pack = ContextPackBuilder(self.ledger).build(
            "NeoZygisk 注入 zygote",
            ContextPackOptions(max_items=8, min_score=0.0, debug=True))
        candidates = (pack.get("debug") or {}).get("candidates") or []
        self.assertTrue(candidates)
        self.assertEqual([], [c for c in candidates
                              if "entity" in (c.get("score_parts") or {})],
                         "默认构造的 builder 不得出现实体键")

    def test_daemon_loop_publishes_health_and_swaps_the_live_builder(self):
        import os
        import time
        from hippomind.daemon import MemoryHubService, RuntimePaths
        os.environ["HIPPOMIND_ENTITY_CHANNEL"] = "1"
        service = MemoryHubService(
            RuntimePaths.for_ledger(self.ledger.path, Path(self.tmp.name) / "rt"),
            watch=False, require_auth=False)
        self.addCleanup(service.ledger.close)
        service.start_entity_index_loop(tick_seconds=0.05)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (service.health.get("entity_index") or {}).get("state") == "ready":
                break
            time.sleep(0.02)
        health = service.health.get("entity_index") or {}
        self.assertEqual("ready", health.get("state"), health)
        self.assertGreater(health.get("entities", 0), 0)
        self.assertIsNotNone(service.contexts.entity_index,
                             "live builder 必须拿到索引（否则生产路径用不到）")

    def test_daemon_reports_disabled_when_the_switch_is_off(self):
        from hippomind.daemon import MemoryHubService, RuntimePaths
        service = MemoryHubService(
            RuntimePaths.for_ledger(self.ledger.path, Path(self.tmp.name) / "rt2"),
            watch=False, require_auth=False)
        self.addCleanup(service.ledger.close)
        service.start_entity_index_loop(tick_seconds=0.05)
        self.assertEqual({"state": "disabled"},
                         service.health.get("entity_index"))
        self.assertIsNone(service.contexts.entity_index)

if __name__ == "__main__":
    unittest.main()
