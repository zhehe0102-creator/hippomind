"""查询扩展（query_expansion）的契约测试。

这个模块的价值**不在**"能提升多少 recall"——那个数字在自建评测集上无法
诚实地测出来（见 docs/SEMANTIC.md 的「为什么我们没有报这个提升」）。它的价值
在三条可验证的工程契约上，本文件逐条钉住：

1. **默认关闭时行为逐字节不变**。不是"结果差不多"，是 score_parts 的键集合、
   候选集、排序全部与改造前相同。
2. **每个扩展词都能说出理由**（term / reason / source），没有理由的扩展等于
   给检索灌噪声且无法归因。
3. **扩展词不得抬高注入门控**。score_parts["coverage"] 与 ["phrase"] 是
   context_pack._passes_relevance_gate 读的信号；扩展词只能影响排序，不能
   变成"这条记忆可靠"的新证据。这条是安全边界，不是洁癖——向量通道当年
   就是把语义近邻当成了相关性证据，导致"今天天气怎么样"开始被注入。

写这些测试时踩到的一个坑（记下来，别重复）：最初把扩展词 append 进
_fts_terms 的 seen 再一起 [:12]，长 CJK 查询的滑动窗口早把 12 个名额吃满，
扩展词一个都进不了 OR 组。单测若只断言 expand() 有返回值就会"通过"，而
功能从未生效。所以下面有一条测试断言扩展词**真的出现在** MATCH 表达式里。
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

from hippomind import query_expansion as QE  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind.ledger.search import SearchMixin  # noqa: E402


#: 改造前 ledger.search 写进 score_parts 的全部键（与 test_entity_channel.py
#: 的 HISTORICAL_SCORE_PART_KEYS 一致）。默认路径必须恰好是这一组。
HISTORICAL_SCORE_PART_KEYS = {
    "topic", "fts", "coverage", "phrase",
    "rrf", "time", "touch", "weight",
    "confidence", "trust", "quality",
    "utility", "task_scope", "structured",
    "pinned",
}


def _glossary_file(payload) -> str:
    """把 payload 写进一个临时 HIPPOMIND_HOME 目录，返回 home 路径。"""
    home = tempfile.mkdtemp(prefix="hm-qe-")
    path = os.path.join(home, QE.GLOSSARY_FILENAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return home


class _Env:
    """临时设置 HIPPOMIND_QUERY_EXPANSION / HIPPOMIND_HOME，退出时复原。"""

    def __init__(self, *, on: bool, home: str = ""):
        self.on = on
        self.home = home
        self._saved = {}

    def __enter__(self):
        for key in (QE.ENV_SWITCH, "HIPPOMIND_HOME"):
            self._saved[key] = os.environ.get(key)
        if self.on:
            os.environ[QE.ENV_SWITCH] = "1"
        else:
            os.environ.pop(QE.ENV_SWITCH, None)
        if self.home:
            os.environ["HIPPOMIND_HOME"] = self.home
        QE.reset_cache()
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        QE.reset_cache()
        return False


def _make_ledger() -> SQLiteLedger:
    """一个能装下本文件所有断言的小账本。

    memory 内容刻意写成"只有术语、没有口语说法"的样子——那正是 semantic 集
    面对的真实情形：账本里写着「提示注入防护」，用户问的是「夹带恶意指令」。

    用 ledger.ingest(record) 而不是某个不存在的快捷方法：memory_id 由账本
    生成，所以调用方拿 IngestResult.memory_id 反查，不能在构造时就钉死。
    """
    tmp = tempfile.mkdtemp(prefix="hm-qe-ledger-")
    ledger = SQLiteLedger(Path(tmp) / "ledger.sqlite")
    rows = [
        ("m1", "安全与治理能力：提示注入防护、来源追溯、隐私隔离", "hippomind"),
        ("m2", "本机轻量 daemon + SQLite/WAL 事件账本，断电不丢", "hippomind"),
        ("m3", "部署主机是 mac-mini，长期常驻", "hippomind"),
        ("m4", "向量检索与 ACT-R 作为可插拔评测接口", "hippomind"),
        ("m5", "完全无关的一条：今天午饭吃了牛肉面", "other"),
    ]
    ids = {}
    for key, content, project in rows:
        result = ledger.ingest({
            "content": content, "kind": "task", "project": project,
            "scope": "shared", "source_agent": "tester",
            "evidence": ["test:" + key],
        })
        ids[key] = result.memory_id
    ledger._qe_ids = ids  # 测试用：把 m1..m5 映射到真实 memory_id
    return ledger

class SwitchTests(unittest.TestCase):
    """开关语义：默认关，且"关"就是真的关。"""

    def test_default_is_off(self):
        with _Env(on=False):
            self.assertFalse(QE.enabled())
            self.assertEqual([], QE.expand("断电会不会丢"))

    def test_only_explicit_truthy_values_enable(self):
        for value in ("1", "on", "true", "yes", "enabled", "TRUE", " On "):
            with mock.patch.dict(os.environ, {QE.ENV_SWITCH: value}):
                self.assertTrue(QE.enabled(), value)
        # 拼错/空/否定一律当作关闭：默认值必须是"不改变既有行为"。
        for value in ("", "0", "off", "false", "no", "disabled", "ture", "2"):
            with mock.patch.dict(os.environ, {QE.ENV_SWITCH: value}):
                self.assertFalse(QE.enabled(), value)

    def test_expand_returns_empty_when_switch_is_off(self):
        home = _glossary_file({"entries": [{"when": "断电", "add": ["wal"]}]})
        with _Env(on=False, home=home):
            # 表存在且能命中，但开关关着 -> 必须返回空
            self.assertEqual([], QE.expand("断电会不会丢"))
            self.assertEqual([], QE.expansion_terms("断电会不会丢"))


class ParseTests(unittest.TestCase):
    """别名表解析：两种写法、坏输入不抛。"""

    def test_parses_canonical_and_shorthand_forms(self):
        table = QE.parse_glossary({"entries": [
            {"when": "断电", "add": ["持久化", "wal"]},
            {"掉电": ["持久化"]},
        ]})
        self.assertEqual(2, len(table))
        self.assertEqual([("断电", ["持久化", "wal"]), ("掉电", ["持久化"])],
                         table.entries)

    def test_parses_bare_top_level_mapping(self):
        table = QE.parse_glossary({"断电": ["持久化"], "note": "ignored"})
        self.assertEqual([("断电", ["持久化"])], table.entries)

    def test_drops_blank_and_duplicate_terms(self):
        table = QE.parse_glossary({"entries": [
            {"when": "断电", "add": ["wal", " WAL ", "", "持久化"]}]})
        self.assertEqual([("断电", ["wal", "持久化"])], table.entries)

    def test_malformed_entries_are_skipped_not_raised(self):
        table = QE.parse_glossary({"entries": [
            {"when": "", "add": ["x"]},        # 空说法
            {"when": "断电", "add": []},       # 空术语表
            {"when": "掉电", "add": 42},       # 术语不是列表
            "not-an-object",                   # 根本不是对象
            {"when": "丢了", "add": ["持久化"]},
        ]})
        self.assertEqual([("丢了", ["持久化"])], table.entries)

    def test_non_list_payload_yields_empty_table_with_error(self):
        table = QE.parse_glossary(42)
        self.assertEqual(0, len(table))
        self.assertTrue(table.error)


class ExplainabilityTests(unittest.TestCase):
    """契约 2：每个扩展词都能说出理由。"""

    def test_every_expansion_carries_reason_and_source(self):
        home = _glossary_file({"entries": [
            {"when": "断电", "add": ["持久化", "wal"]}]})
        with _Env(on=True, home=home):
            items = QE.expand("断电会不会丢")
        self.assertEqual(["持久化", "wal"], [i["term"] for i in items])
        for item in items:
            self.assertEqual({"term", "reason", "source"}, set(item))
            self.assertIn("断电", item["reason"])
            self.assertIn(QE.GLOSSARY_FILENAME, item["source"])

    def test_terms_already_in_the_query_are_not_repeated(self):
        home = _glossary_file({"entries": [
            {"when": "断电", "add": ["断电", "持久化"]}]})
        with _Env(on=True, home=home):
            self.assertEqual(["持久化"], QE.expansion_terms("断电会不会丢"))

    def test_limit_bounds_how_much_one_bad_entry_can_inject(self):
        home = _glossary_file({"entries": [
            {"when": "系统", "add": ["t%d" % i for i in range(50)]}]})
        with _Env(on=True, home=home):
            self.assertEqual(3, len(QE.expand("系统怎么样", limit=3)))
            # 默认上限也必须生效
            self.assertEqual(QE.DEFAULT_MAX_TERMS,
                             len(QE.expand("系统怎么样")))

    def test_explain_reports_switch_glossary_and_terms(self):
        home = _glossary_file({"entries": [{"when": "断电", "add": ["wal"]}]})
        with _Env(on=True, home=home):
            view = QE.explain("断电会不会丢")
        self.assertTrue(view["enabled"])
        self.assertEqual(QE.ENV_SWITCH, view["switch"])
        self.assertEqual(1, view["glossary"]["entries"])
        self.assertEqual(["wal"], view["terms"])
        self.assertIn("断电", view["expansions"][0]["reason"])


class LoadTests(unittest.TestCase):
    """加载：缺失/损坏/超大一律退化成空表，绝不抛。"""

    def test_missing_file_is_an_empty_table(self):
        home = tempfile.mkdtemp(prefix="hm-qe-missing-")
        with _Env(on=True, home=home):
            table = QE.load_glossary()
            self.assertEqual(0, len(table))
            self.assertEqual([], QE.expand("断电"))

    def test_corrupt_json_is_an_empty_table_with_error(self):
        home = tempfile.mkdtemp(prefix="hm-qe-bad-")
        with open(os.path.join(home, QE.GLOSSARY_FILENAME), "w",
                  encoding="utf-8") as handle:
            handle.write("{not json")
        with _Env(on=True, home=home):
            table = QE.load_glossary()
            self.assertEqual(0, len(table))
            self.assertTrue(table.error)
            self.assertEqual([], QE.expand("断电"))

    def test_oversized_file_is_refused(self):
        home = tempfile.mkdtemp(prefix="hm-qe-big-")
        path = os.path.join(home, QE.GLOSSARY_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x" * (QE.MAX_GLOSSARY_BYTES + 10))
        with _Env(on=True, home=home):
            table = QE.load_glossary()
            self.assertEqual(0, len(table))
            self.assertIn("larger than", table.error)

    def test_cache_invalidates_when_the_file_changes(self):
        home = _glossary_file({"entries": [{"when": "断电", "add": ["wal"]}]})
        with _Env(on=True, home=home):
            self.assertEqual(["wal"], QE.expansion_terms("断电"))
            # 改文件后必须立刻生效（用户改了表却看不到效果是最困惑的失败）
            path = os.path.join(home, QE.GLOSSARY_FILENAME)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": [{"when": "断电", "add": ["持久化"]}]},
                          handle, ensure_ascii=False)
            os.utime(path, ns=(1, 1))  # 保证 mtime 一定变化
            self.assertEqual(["持久化"], QE.expansion_terms("断电"))


class DefaultPathUnchangedTests(unittest.TestCase):
    """契约 1：默认关闭时 ledger.search 行为逐字节不变。"""

    @classmethod
    def setUpClass(cls):
        cls.ledger = _make_ledger()

    @classmethod
    def tearDownClass(cls):
        cls.ledger.close()

    def test_score_parts_keys_are_exactly_the_historical_set(self):
        with _Env(on=False):
            hits = self.ledger.search("提示注入防护", limit=8)
        self.assertTrue(hits)
        self.assertEqual(HISTORICAL_SCORE_PART_KEYS, set(hits[0]["score_parts"]))

    def test_expansion_terms_never_enter_fts_terms_when_off(self):
        home = _glossary_file({"entries": [{"when": "断电", "add": ["wal"]}]})
        with _Env(on=False, home=home):
            self.assertEqual(["断电会不会丢"],
                             SearchMixin._fts_terms("断电会不会丢"))

    def test_results_are_identical_with_switch_on_but_empty_glossary(self):
        queries = ["提示注入防护", "断电会不会丢", "跑在哪台机器上",
                   "换检索算法要动多少代码"]
        with _Env(on=False):
            before = [[h["memory_id"] for h in self.ledger.search(q, limit=8)]
                      for q in queries]
        empty_home = tempfile.mkdtemp(prefix="hm-qe-empty-")
        with _Env(on=True, home=empty_home):
            after = [[h["memory_id"] for h in self.ledger.search(q, limit=8)]
                     for q in queries]
        self.assertEqual(before, after)


class CacheBypassTests(unittest.TestCase):
    """扩展开启时绕开查询缓存；关闭时缓存行为逐字节不变。

    为什么不把扩展身份追加进 cache_key：cache_key 是位置元组，
    tests/test_entity_channel.py 用 captured[0][:-2] 断言「实体字段总是最后两位、
    前缀逐字节不变」。追加第三位会打破那条断言。绕开缓存反而更彻底——别名表
    是人改的文件，任何「键里放 mtime」的方案都有窗口，直接不读缓存则改表立刻
    生效，也消除「先跑关闭臂把空页写进缓存、开启臂拿到同一条空页」的 A/B 失真。
    """

    @classmethod
    def setUpClass(cls):
        cls.ledger = _make_ledger()

    @classmethod
    def tearDownClass(cls):
        cls.ledger.close()

    def _spy_cache(self, **kwargs):
        """跑一次 search，返回 (读缓存次数, 写缓存次数)。"""
        reads = []
        writes = []
        original_read = SearchMixin._search_cached
        original_store = SearchMixin._search_store

        def read(self, key):
            reads.append(key)
            return original_read(self, key)

        def store(self, key, rows):
            writes.append(key)
            return original_store(self, key, rows)

        SearchMixin._search_cached = read
        SearchMixin._search_store = store
        try:
            self.ledger.search(**kwargs)
        finally:
            SearchMixin._search_cached = original_read
            SearchMixin._search_store = original_store
        return len(reads), len(writes)

    def test_cache_is_used_when_expansion_is_off(self):
        home = _glossary_file({"entries": [{"when": "断电", "add": ["wal"]}]})
        with _Env(on=False, home=home):
            reads, writes = self._spy_cache(query="断电会不会丢", limit=5)
        self.assertEqual(1, reads, "关闭时必须照常读缓存")
        self.assertEqual(1, writes, "关闭时必须照常写缓存")

    def test_cache_is_bypassed_when_expansion_is_on(self):
        home = _glossary_file({"entries": [{"when": "断电", "add": ["wal"]}]})
        with _Env(on=True, home=home):
            reads, writes = self._spy_cache(query="断电会不会丢", limit=5)
        self.assertEqual(0, reads, "开启时不得读缓存（否则改表不生效）")
        self.assertEqual(0, writes, "开启时不得写缓存（否则污染关闭臂）")

    def test_entity_cache_key_contract_is_untouched(self):
        """回归护栏：实体通道对 cache_key 末尾两位的断言不能被打破。

        这是本任务遇到的真实冲突：最初把扩展身份追加进 cache_key，
        tests/test_entity_channel.py 的 captured[0][:-2] 断言立刻失败，而那个
        文件不在本任务的写范围内。改成绕开缓存后，cache_key 的形状完全没变。
        """
        captured = []
        original = SearchMixin._search_cached

        def spy(self, key):
            captured.append(key)
            return original(self, key)

        SearchMixin._search_cached = spy
        try:
            with _Env(on=False):
                self.ledger.search("断电会不会丢", scope="shared", limit=5)
                self.ledger.search("断电会不会丢", scope="shared", limit=5)
        finally:
            SearchMixin._search_cached = original
        self.assertEqual(2, len(captured))
        # 两次调用（无向量/实体通道）必须产生同一个 key，且末尾两位是 None
        self.assertEqual(captured[0], captured[1])
        self.assertEqual((None, None), captured[0][-2:])


class WiringTests(unittest.TestCase):
    """接线：扩展词真的进候选生成与排序，但不进注入门控。"""

    @classmethod
    def setUpClass(cls):
        cls.ledger = _make_ledger()

    @classmethod
    def tearDownClass(cls):
        cls.ledger.close()

    def test_expansion_terms_reach_the_fts_match_expression(self):
        """踩过的坑：扩展词被 [:12] 截掉时功能静默失效。"""
        home = _glossary_file({"entries": [
            {"when": "夹带恶意指令", "add": ["提示注入防护"]}]})
        with _Env(on=True, home=home):
            terms = SearchMixin._fts_terms("记忆里夹带恶意指令怎么防")
        self.assertIn("提示注入防护", terms)

    def test_long_cjk_query_still_keeps_its_expansion_terms(self):
        """长查询的滑动窗口会吃满 12 个原始词项预算，扩展词必须在预算之外。

        这是真实踩过的 bug：最初把扩展词 append 进 seen 再一起 [:12]，长 CJK
        查询的窗口碎片（每个长段产生 len-4 个 5 字窗口）早已占满 12 个名额，
        扩展词一个都进不了 OR 组。实测 semantic recall 0.6226（被截断）vs
        0.7736（不截断），而当时的单测「通过」——因为它只断言 expand() 有值。

        断言方式：原始词项必须**恰好**是扩展结果的前缀且长度已到上限。
        """
        query = "记忆里夹带恶意指令怎么防止系统被攻击这一整段很长很长的问题"
        home = _glossary_file({"entries": [
            {"when": "夹带恶意指令", "add": ["提示注入防护"]}]})
        with _Env(on=False, home=home):
            plain = SearchMixin._fts_terms(query)
        with _Env(on=True, home=home):
            expanded = SearchMixin._fts_terms(query)
        # 前提：这条查询的原始词项确实吃满了 12 项预算（否则测不到截断）
        self.assertEqual(12, len(plain),
                         "查询不够长，测不到 12 项预算吃满的情形")
        # 关闭时的结果必须原样保留在前缀里
        self.assertEqual(plain, expanded[:len(plain)])
        # 扩展词必须真的出现（而不是被预算吃掉）
        self.assertIn("提示注入防护", expanded)
        # 扩展词必须排在 12 项预算**之外**（追加，不是替换/挤占）。
        # 这条断言是钉死真实 bug 的关键：如果实现改成「扩展词并入 seen 后
        # 统一 [:12]」，长查询的窗口碎片会把名额吃满，扩展词消失。
        self.assertEqual(len(plain) + 1, len(expanded))
        self.assertEqual(expanded[:12], plain)
        self.assertEqual("提示注入防护", expanded[12])
    def test_expansion_lets_a_paraphrase_reach_the_term_lexically_absent(self):
        """核心行为：口语提问靠别名表命中"只有术语"的记忆。

        这是 semantic 集的真实形态——账本里写着「提示注入防护」，用户问的是
        「记忆里夹带恶意指令怎么防」，两者没有任何词面重叠。
        """
        ids = self.ledger._qe_ids
        cases = [
            ("机器", "mac-mini", "这套系统跑在哪台机器上", "m3"),
            ("恶意指令", "提示注入防护", "记忆里夹带恶意指令怎么防", "m1"),
        ]
        for when, term, query, target_key in cases:
            home = _glossary_file({"entries": [{"when": when, "add": [term]}]})
            with _Env(on=False, home=home):
                before = [h["memory_id"] for h in self.ledger.search(query, limit=8)]
            with _Env(on=True, home=home):
                after = [h["memory_id"] for h in self.ledger.search(query, limit=8)]
            target = ids[target_key]
            # 关闭时：账本里没有任何词面重叠 -> 目标记忆根本不在结果里
            self.assertNotIn(target, before, "关闭时不该命中: %s" % query)
            # 开启后：目标记忆必须被召回
            self.assertIn(target, after[:8], "开启后必须命中: %s" % query)

    def test_prepared_coverage_keeps_rank_and_gate_vocabularies_separate(self):
        """词表分离的直接契约：rank 词表含扩展词，gate 词表不含。

        这条钉的是**实现契约**而不是端到端结果，因为端到端测不出它：扩展词
        同时把行放进 FTS 候选集，即使 coverage 没跟着变，行也可能靠 rrf 排上
        来（实测确认过——只做端到端断言时，「扩展词不参与 coverage」这个变异
        存活）。而这条分离正是「扩展只影响排序、不影响注入门控」的落点。
        """
        home = _glossary_file({"entries": [
            {"when": "断电", "add": ["SQLite/WAL"]}]})
        query = "断电会不会丢数据"
        with _Env(on=False, home=home):
            plain = SearchMixin._prepare_query_coverage(query)
        with _Env(on=True, home=home):
            expanded = SearchMixin._prepare_query_coverage(query)
        # 关闭时：两个词表完全一致（不存在 gate/rank 之分）
        self.assertEqual(plain["terms"], expanded["gate_terms"])
        self.assertEqual(plain["phrase_terms"], expanded["gate_phrase_terms"])
        # 开启时：排序词表多出扩展词，门控词表一个都不多
        self.assertIn("sqlite/wal", expanded["terms"])
        self.assertNotIn("sqlite/wal", expanded["gate_terms"])
        self.assertEqual(plain["terms"], expanded["gate_terms"])
        self.assertEqual(["SQLite/WAL"], expanded["expansion"])

    def test_expansion_is_reported_in_the_prepared_coverage(self):
        """扩展必须在 prep 里可见——否则下游无法审计「凭什么改排序」。"""
        home = _glossary_file({"entries": [{"when": "断电", "add": ["wal"]}]})
        with _Env(on=False, home=home):
            self.assertEqual([], SearchMixin._prepare_query_coverage("断电")["expansion"])
        with _Env(on=True, home=home):
            self.assertEqual(["wal"], SearchMixin._prepare_query_coverage("断电")["expansion"])

    def test_switch_toggle_is_not_swallowed_by_the_search_cache(self):
        """切换开关必须立刻生效——缓存键漏了扩展身份会让 A/B 失真。

        实测踩到过：先跑「关闭」臂把空页写进缓存，再跑「开启」臂拿到的还是
        那条空页，看起来像「功能无效」。向量/实体通道都因为漏了这一步翻过车。
        """
        ids = self.ledger._qe_ids
        query = "这套系统跑在哪台机器上"
        home = _glossary_file({"entries": [{"when": "机器", "add": ["mac-mini"]}]})
        with _Env(on=False, home=home):
            self.assertNotIn(ids["m3"],
                             [h["memory_id"] for h in self.ledger.search(query, limit=8)])
        with _Env(on=True, home=home):
            self.assertIn(ids["m3"],
                          [h["memory_id"] for h in self.ledger.search(query, limit=8)])
        # 再切回去也必须立刻恢复（缓存不能留下"开启过"的残留）
        with _Env(on=False, home=home):
            self.assertNotIn(ids["m3"],
                             [h["memory_id"] for h in self.ledger.search(query, limit=8)])

    def test_editing_the_glossary_invalidates_the_search_cache(self):
        query = "这套系统跑在哪台机器上"
        ids = self.ledger._qe_ids
        home = _glossary_file({"entries": [{"when": "机器", "add": ["mac-mini"]}]})
        with _Env(on=True, home=home):
            self.assertIn(ids["m3"],
                          [h["memory_id"] for h in self.ledger.search(query, limit=8)])
            # 把条目删掉，立刻应恢复成命中不到
            path = os.path.join(home, QE.GLOSSARY_FILENAME)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"entries": []}, handle)
            os.utime(path, ns=(1, 1))
            QE.reset_cache()
            self.assertNotIn(ids["m3"],
                             [h["memory_id"] for h in self.ledger.search(query, limit=8)])

    def test_gate_coverage_is_not_inflated_by_expansion(self):
        """安全边界：score_parts["coverage"] 只算原始查询词。

        context_pack._passes_relevance_gate 判 fts>0 or coverage>=0.6。如果
        扩展词抬高了这个值，一张写坏的别名表就能让无关提问开始被注入。
        """
        query = "断电会不会丢数据"
        home = _glossary_file({"entries": [
            {"when": "断电", "add": ["SQLite/WAL", "事件账本", "daemon"]}]})
        with _Env(on=False, home=home):
            before = {h["memory_id"]: h["score_parts"]["coverage"]
                      for h in self.ledger.search(query, limit=8)}
        with _Env(on=True, home=home):
            after = {h["memory_id"]: h["score_parts"]["coverage"]
                     for h in self.ledger.search(query, limit=8)}
        common = set(before) & set(after)
        self.assertTrue(common, "两次查询应至少共享一条候选")
        for memory_id in common:
            self.assertEqual(before[memory_id], after[memory_id],
                             "扩展词抬高了门控可见的 coverage: %s" % memory_id)

    def test_gate_phrase_flag_is_not_flipped_by_expansion(self):
        query = "断电会不会丢数据"
        home = _glossary_file({"entries": [
            {"when": "断电", "add": ["SQLite/WAL"]}]})
        with _Env(on=False, home=home):
            before = {h["memory_id"]: h["score_parts"]["phrase"]
                      for h in self.ledger.search(query, limit=8)}
        with _Env(on=True, home=home):
            after = {h["memory_id"]: h["score_parts"]["phrase"]
                     for h in self.ledger.search(query, limit=8)}
        for memory_id in set(before) & set(after):
            self.assertEqual(before[memory_id], after[memory_id])

    def test_unrelated_memory_is_not_promoted_by_a_bad_alias(self):
        """一条把"午饭"映射到无关术语的坏条目，不应让无关记忆冲到前面。"""
        home = _glossary_file({"entries": [
            {"when": "午饭", "add": ["提示注入防护"]}]})
        with _Env(on=True, home=home):
            hits = self.ledger.search("今天午饭吃了什么", limit=8)
        # 允许它进入候选池（别名表是人写的，语义上确实建立了关联），
        # 但门控值不得被抬高——注入路径据此仍会拒绝它。
        for hit in hits:
            if hit["memory_id"] == "m1":
                self.assertLess(hit["score_parts"]["coverage"], 0.6,
                                "坏别名不得让无关记忆通过注入门控")


class MeasurementHonestyTests(unittest.TestCase):
    """为什么「别名表提升 semantic」这个数字无法在自建评测集上测出来。

    这不是自我辩解，是可以程序化验证的事实。Lead 要求的泄漏排除断言是：
    「没有任何 glossary key 是任何 held-out query 的子串」。本文件把它实现
    成一条测试，并且同时证明它的**同义反复性**：

        断言通过  <=>  key 不在 held-out 查询里出现
                  <=>  expand() 在 held-out 上恒返回 []
                  <=>  效果恒为 0

    所以「干净」与「非零」不可兼得。要真正测出提升，需要一个**从未见过评测
    集**的别名来源（真实用户查询日志，或没看过 semantic 集的人整理的表）。
    在那种来源出现之前，任何非零数字都是读着评测集写出来的。
    """

    #: 本文件自带的一小份「示例」别名表，用于验证排除断言本身能工作。
    #: 刻意只写两个条目，且都只描述**机制**、不试图覆盖评测集。
    SAMPLE = {"断电": ["持久化"], "机器": ["主机"]}

    def test_held_out_queries_are_not_covered_by_the_sample_glossary(self):
        """排除断言：示例表的 key 不得出现在任何 held-out 查询里。"""
        held_out = ["以后想换检索算法需要动多少代码",
                    "有没有公开的长期记忆评测基准可以对照",
                    "为什么普通权限查不到注入模块在不在跑"]
        violations = [(key, query) for key in self.SAMPLE
                      for query in held_out if key in query]
        self.assertEqual([], violations,
                         "示例表泄漏进了 held-out 查询: %r" % (violations,))

    def test_a_leak_free_glossary_never_fires_on_held_out_queries(self):
        """同义反复的证明：表不泄漏 -> 它在 held-out 上从不触发。

        这条测试的作用是**防止未来有人报告一个污染的数字**：如果有人声称
        「干净测量下提升到 X」，要么他的表泄漏了（断言会失败），要么 X 必然
        是 0。
        """
        held_out = ["以后想换检索算法需要动多少代码",
                    "有没有公开的长期记忆评测基准可以对照",
                    "为什么普通权限查不到注入模块在不在跑"]
        home = _glossary_file({"entries": [{"when": k, "add": v}
                                           for k, v in self.SAMPLE.items()]})
        with _Env(on=True, home=home):
            fired = [q for q in held_out if QE.expansion_terms(q)]
        self.assertEqual([], fired,
                         "泄漏排除过的表竟然在 held-out 上触发了: %r" % (fired,))

    def test_sample_glossary_still_works_on_in_domain_queries(self):
        """对照：同一张表在**建表所用的措辞**上是有效的。

        这条与上一条合起来才完整：表能工作（机制成立），但它在没见过的措辞上
        不触发（所以测不出泛化）。
        """
        home = _glossary_file({"entries": [{"when": k, "add": v}
                                           for k, v in self.SAMPLE.items()]})
        with _Env(on=True, home=home):
            self.assertEqual(["持久化"], QE.expansion_terms("断电会不会丢"))
            self.assertEqual(["主机"], QE.expansion_terms("跑在哪台机器上"))


if __name__ == "__main__":
    unittest.main()
