"""知识更新测试：同一 slot 先写 A 后写 B，问「现在」必须答 B、问「以前」必须答 A。

LongMemEval（arXiv 2410.10813）把「知识更新」列为长期记忆五项能力之一，而
HippoMind 从来没有测过它（docs/HEALTH_REVIEW.md 第六节）。这个文件补上这一项，
同时钉住两件**不同**的事——它们很容易被混为一谈，但走的是两条代码路径：

**路径 1：claim 级（同一 slot 的新旧断言）**
  两条记忆在同一个 (scope, subject, predicate, slot_key) 上给出不同 value。
  ingest 时的 _insert_claims 比较 confidence*trust，胜者 active、败者
  superseded，败者记忆整条下线（lifecycle='superseded'），并写入 superseded_by。
  这条路径决定「现在是什么」，也是唯一会自动把旧事实踢出召回的地方。

**路径 2：memory 级（valid_from / valid_until 双时态窗口）**
  两条记忆**不竞争**（不同 slot 或不同 subject），但各自带时间窗口。
  ledger.search(as_of=T) 按窗口求值，于是同一句查询在「现在」和「过去」返回
  不同的记忆。这条路径决定「以前是什么」。

两条路径**目前没有打通**，这是本文件最诚实的结论（见 KnowledgeUpdateLimitsTests）：

* claim 被 supersede 时 lifecycle.py **不写 claim.valid_until**（体检报告第三节的
  结构性缺口 1）。所以 superseded 的 claim 没有失效时间，as_of 查不到它——
  「以前是什么」在 claim 级路径上现在**无法回答**，只能返回空。
* 记忆被 supersede 后 lifecycle='superseded'，而 search 的 as_of 分支只放行
  active/cold/expired。所以即便 memory 自己有 valid_until 窗口，被取代的旧记忆
  在 as_of 查询里仍然不可见。

下面把「已经能工作的」和「还不能工作的」分成两个 TestCase：前者是真门禁，
后者是回归护栏——一旦 T2（valid_until 写入）落地，后者会失败，提醒把它升级成
真断言。这样缺口不会在「测试全绿」里被悄悄忘掉。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402


def _fmt(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


def _fact(content, marker, **extra):
    record = {
        "kind": "fact",
        "scope": "shared",
        "source_agent": "eval",
        "content": content,
        "evidence": ["eval:" + marker],
    }
    record.update(extra)
    return record


def _contents(rows):
    return [str(row["content"]) for row in rows]


class _LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self._tmp.name) / "knowledge.sqlite")
        self.addCleanup(self.ledger.close)


class ClaimLevelUpdateTests(_LedgerCase):
    """路径 1：同 slot 的 A -> B，问现在必须答 B。"""

    SLOT = {"subject": "db.prod", "predicate": "config", "slot_key": "port"}

    def _write_pair(self, *, old_confidence=0.55, new_confidence=0.9,
                    old_content="生产数据库端口 5432",
                    new_content="生产数据库已完成端口迁移，现用 6432",
                    old_value="5432", new_value="6432"):
        old = _fact(old_content, "a", confidence=old_confidence,
                    value={"port": old_value}, **self.SLOT)
        new = _fact(new_content, "b", confidence=new_confidence,
                    value={"port": new_value}, **self.SLOT)
        old_id = self.ledger.ingest(old).memory_id
        new_id = self.ledger.ingest(new).memory_id
        return old_id, new_id

    def test_higher_confidence_new_value_supersedes_the_old_one(self):
        old_id, new_id = self._write_pair()
        old_row = self.ledger.get_memory(old_id)
        new_row = self.ledger.get_memory(new_id)
        self.assertEqual(new_row["lifecycle"], "active")
        self.assertEqual(new_row["quality_state"], "accepted")
        self.assertEqual(old_row["lifecycle"], "superseded")
        self.assertEqual(old_row["quality_state"], "superseded")
        self.assertEqual(old_row["superseded_by"], new_id)

    def test_query_for_the_current_value_returns_b_only(self):
        """问「现在是什么」必须返回 B，且**不能**同时返回 A。"""
        old_id, new_id = self._write_pair()
        hits = self.ledger.search("生产数据库端口", limit=8)
        contents = _contents(hits)
        self.assertTrue(any("6432" in text for text in contents),
                        "现在的问题必须命中新值，实际: %s" % contents)
        self.assertFalse(any("5432" in text for text in contents),
                         "被取代的旧值不得出现在当前召回里: %s" % contents)
        self.assertNotIn(old_id, [row["memory_id"] for row in hits])

    def test_claim_states_follow_the_update(self):
        old_id, new_id = self._write_pair()
        claims = {row["memory_id"]: row
                  for row in self.ledger.list_claims(scope="shared")}
        self.assertEqual(claims[new_id]["state"], "active")
        self.assertEqual(claims[old_id]["state"], "superseded")
        self.assertEqual(claims[new_id]["value"], {"port": "6432"})
        self.assertEqual(claims[old_id]["value"], {"port": "5432"})

    def test_active_facts_view_returns_only_the_new_value(self):
        """事实投影（GUI/MCP 的「现在是什么」视图）同样只留新值。"""
        self._write_pair()
        values = [fact["value"] for fact in self.ledger.list_facts(scope="shared")]
        self.assertEqual(values, [{"port": "6432"}])

    def test_context_pack_for_the_current_value_contains_only_b(self):
        """自动注入路径（pack）也必须是「只有 B」。"""
        self._write_pair()
        pack = ContextPackBuilder(self.ledger).build(
            "生产数据库端口", ContextPackOptions(max_items=8))
        text = str(pack.get("text") or "")
        self.assertIn("6432", text)
        self.assertNotIn("5432", text)

    def test_lower_confidence_new_value_becomes_a_conflict_not_a_silent_overwrite(self):
        """反向场景：新值更弱时不得静默覆盖，必须进 conflict 等人工裁决。"""
        old_id, new_id = self._write_pair(old_confidence=0.9, new_confidence=0.4)
        claims = {row["memory_id"]: row
                  for row in self.ledger.list_claims(scope="shared")}
        self.assertEqual(claims[old_id]["state"], "active")
        self.assertEqual(claims[new_id]["state"], "conflict")
        # 当前视图仍只给出旧值——低置信新断言不能顶掉已成立的事实。
        self.assertEqual([f["value"] for f in self.ledger.list_facts(scope="shared")],
                         [{"port": "5432"}])
        hits = _contents(self.ledger.search("生产数据库端口", limit=8))
        self.assertTrue(any("5432" in text for text in hits))

    def test_close_confidences_put_both_sides_in_conflict(self):
        """分数接近（差值 <= 0.05）时双方同时下线，等待更强证据。"""
        old_id, new_id = self._write_pair(old_confidence=0.60, new_confidence=0.60)
        claims = {row["memory_id"]: row
                  for row in self.ledger.list_claims(scope="shared")}
        self.assertEqual(claims[old_id]["state"], "conflict")
        self.assertEqual(claims[new_id]["state"], "conflict")
        self.assertEqual(self.ledger.list_facts(scope="shared"), [])


class MemoryLevelUpdateTests(_LedgerCase):
    """路径 2：双时态窗口——问「以前是什么」必须答 A，问「现在」必须答 B。

    两条记忆**不竞争**（不同 subject），各自带 valid_from/valid_until 窗口。
    这测的是 ledger.search(as_of=...) 的 point-in-time 语义，与 claim 冲突裁决
    无关：窗口是写入方声明的时间事实，不是置信度竞争的产物。
    """

    def setUp(self):
        super().setUp()
        self.now = time.time()
        # A 的有效期：3 天前 -> 1 天前；B 从 1 天前开始生效。
        self.a_from = _fmt(self.now - 3 * 86400)
        self.a_until = _fmt(self.now - 86400)
        self.b_from = self.a_until
        self.as_of_inside_a = _fmt(self.now - 2 * 86400)
        # 窗口必须写进 **claim** 字典，不能只写 record 顶层：
        # _normalise_claims 对 claim.valid_until 的兜底是空串（不是 record 的值），
        # 所以只在 record 上声明窗口时，两条 claim 的窗口都为空、被判定成竞争，
        # A 会直接进 superseded——那就变成路径 1 的测试了。
        # 窗口写进 claim 后，_insert_claims 里 "既有 claim 的 valid_until 必须
        # 晚于新 claim 的 valid_from" 这个条件不成立（A 在 B 生效时已失效），
        # 两条 claim 各自 active，不互相取代。
        self.a_id = self.ledger.ingest(_fact(
            "部署端口配置：旧版用 8000", "old",
            claims=[{"subject": "deploy.port", "predicate": "config",
                     "slot_key": "port", "value": {"port": "8000"},
                     "valid_from": self.a_from, "valid_until": self.a_until}],
            valid_from=self.a_from, valid_until=self.a_until)).memory_id
        self.b_id = self.ledger.ingest(_fact(
            "部署端口配置：新版用 9000", "new",
            claims=[{"subject": "deploy.port", "predicate": "config",
                     "slot_key": "port", "value": {"port": "9000"},
                     "valid_from": self.b_from}],
            valid_from=self.b_from)).memory_id

    def test_now_returns_only_the_current_value(self):
        contents = _contents(self.ledger.search("部署端口配置", limit=8))
        self.assertTrue(any("9000" in text for text in contents), contents)
        self.assertFalse(any("8000" in text for text in contents), contents)

    def test_as_of_inside_the_old_window_returns_only_the_old_value(self):
        """问「以前是什么」必须返回 A——这是双时态唯一能自证的地方。"""
        contents = _contents(self.ledger.search(
            "部署端口配置", limit=8, as_of=self.as_of_inside_a))
        self.assertTrue(any("8000" in text for text in contents),
                        "as_of 过去必须查到当时有效的事实: %s" % contents)
        self.assertFalse(any("9000" in text for text in contents),
                         "as_of 过去不得提前泄露未生效事实: %s" % contents)

    def test_as_of_before_both_windows_returns_nothing(self):
        before = _fmt(self.now - 10 * 86400)
        contents = _contents(self.ledger.search("部署端口配置", limit=8, as_of=before))
        self.assertEqual(contents, [])

    def test_as_of_after_the_old_window_closes_still_hides_it(self):
        after_a = _fmt(self.now - 3600)
        contents = _contents(self.ledger.search(
            "部署端口配置", limit=8, as_of=after_a))
        self.assertTrue(any("9000" in text for text in contents), contents)
        self.assertFalse(any("8000" in text for text in contents), contents)

    def test_context_pack_as_of_returns_the_historical_fact(self):
        builder = ContextPackBuilder(self.ledger)
        past = builder.build("部署端口配置", ContextPackOptions(
            max_items=8, as_of=self.as_of_inside_a))
        self.assertIn("8000", str(past.get("text") or ""))
        self.assertNotIn("9000", str(past.get("text") or ""))
        current = builder.build("部署端口配置", ContextPackOptions(max_items=8))
        self.assertIn("9000", str(current.get("text") or ""))
        self.assertNotIn("8000", str(current.get("text") or ""))

    def test_claim_windows_are_preserved_on_both_rows(self):
        claims = {row["memory_id"]: row
                  for row in self.ledger.list_claims(scope="shared")}
        self.assertEqual(claims[self.a_id]["valid_from"], self.a_from)
        self.assertEqual(claims[self.a_id]["valid_until"], self.a_until)
        self.assertEqual(claims[self.b_id]["valid_from"], self.b_from)
        self.assertEqual(claims[self.b_id]["valid_until"], "")


class KnowledgeUpdateTemporalGapTests(_LedgerCase):
    """claim 级「以前是什么」：现在是缺口，修好后必须真的能答。

    体检报告第三节（结构性缺口 1）：claim 被 supersede 时只改 state，不写
    valid_until，于是 as_of 答不了「什么时候失效的」。T2 任务负责修它。

    这些断言刻意写成**状态一致**而不是「当前就是这个行为」：
    无论缺口开着还是已经修好，代码都必须自洽，否则测试失败——

    * 缺口开着（claim.valid_until 为空）-> as_of 必须返回空（不许假装能答）；
    * 缺口修好（claim.valid_until 非空）-> as_of 必须返回 A（不许半修）。

    这样 T2 落地时本文件不会无谓变红，也不会在「测试全绿」里把半成品放过去。
    """

    SLOT = {"subject": "cache.redis", "predicate": "config", "slot_key": "memory"}

    def _supersede(self):
        now = time.time()
        # 旧值必须带**显式过去窗口**：不传 valid_from 时 valid_from = 摄入时刻，
        # 窗口变成 [刚才, 刚才]，任何「过去」的 as_of 都落在窗口之前——那时 A
        # 还不存在，as_of 返回空是**正确**行为，会被误读成「缺口未修」。
        old_id = self.ledger.ingest(_fact(
            "Redis 内存配置 32G", "old", confidence=0.55,
            value={"memory": "32G"},
            valid_from=_fmt(now - 3 * 86400), **self.SLOT)).memory_id
        # 让两条 claim 的窗口边界可区分（否则 A.valid_until == B.valid_from
        # 会落在同一微秒，测试断言的「哪一侧」失去意义）。
        time.sleep(0.05)
        new_id = self.ledger.ingest(_fact(
            "Redis 已扩容到 64G 内存", "new", confidence=0.9,
            value={"memory": "64G"}, **self.SLOT)).memory_id
        return old_id, new_id

    def _as_of_contents(self):
        # 落在 A 的窗口**内部**：A.valid_from = now-3d，A.valid_until = now。
        return _contents(self.ledger.search(
            "Redis 内存配置", limit=8, as_of=_fmt(time.time() - 86400)))

    def test_supersede_records_validity_consistently(self):
        old_id, new_id = self._supersede()
        claims = {row["memory_id"]: row
                  for row in self.ledger.list_claims(scope="shared")}
        self.assertEqual(claims[old_id]["state"], "superseded")
        self.assertEqual(claims[new_id]["state"], "active")
        # 胜者永远不该有失效时间：它是当前事实。
        self.assertEqual(claims[new_id]["valid_until"], "",
                         "active claim 不得带 valid_until")
        contents = self._as_of_contents()
        if claims[old_id]["valid_until"] == "":
            # 缺口仍在：没有失效时间就没有历史切片，as_of 必须诚实返回空。
            self.assertEqual(
                contents, [],
                "claim 没有 valid_until 却能 as_of 召回——时间语义不自洽")
        else:
            # 缺口已修：既然记下了失效时间，as_of 就必须能答「以前是什么」。
            self.assertTrue(
                any("32G" in text for text in contents),
                "claim 已有 valid_until，但 as_of 仍答不出旧值: %s" % contents)
            self.assertFalse(any("64G" in text for text in contents),
                             "as_of 过去不得泄露现在的事实: %s" % contents)

    def test_superseded_memory_keeps_its_window_and_as_of_stays_consistent(self):
        """两种机制叠加：memory 有窗口 + 被 supersede，as_of 必须自洽。

        这是 search 的 as_of 分支与 supersede 语义的交叉点。temporal-engineer
        被授权把 as_of 分支放宽到放行 superseded，所以这里**不能**断言「仍不
        可见」——那会在 T2 落地时因为正确的原因变红。改成状态一致断言：

        * as_of 仍排除 superseded  -> 这两个域名一条都查不到（缺口仍在）；
        * as_of 已放行 superseded  -> 必须查到 docs.old.com（它的窗口覆盖
          as_of），且**不能**查到 docs.new.com（它的 valid_from 更晚）。

        为了让「查不到」不被误读成「as_of 机制坏了」，先放一条**对照组**：
        一条没有被取代、窗口同样覆盖 as_of 的记忆。对照组必须可见，否则本
        测试直接失败——这样空结果只可能来自 superseded 排除，而不是窗口过滤。
        """
        now = time.time()
        inside = _fmt(now - 2 * 86400)
        # 对照组：未参与任何取代，窗口覆盖 as_of。
        self.ledger.ingest(_fact(
            "文档站点备用域名 docs.control.example", "w-control",
            subject="docs.control", predicate="domain", slot_key="host",
            value={"host": "docs.control.example"},
            valid_from=_fmt(now - 3 * 86400),
            valid_until=_fmt(now - 86400)))
        old_id = self.ledger.ingest(_fact(
            "文档站点域名 docs.old.com", "w-old", confidence=0.55,
            subject="docs.site", predicate="domain", slot_key="host",
            value={"host": "docs.old.com"},
            valid_from=_fmt(now - 3 * 86400),
            valid_until=_fmt(now - 86400))).memory_id
        self.ledger.ingest(_fact(
            "文档站点迁移到 docs.new.com", "w-new", confidence=0.9,
            subject="docs.site", predicate="domain", slot_key="host",
            value={"host": "docs.new.com"},
            valid_from=_fmt(now - 86400)))
        row = self.ledger.get_memory(old_id)
        self.assertEqual(row["lifecycle"], "superseded")
        self.assertNotEqual(row["valid_until"], "",
                            "memory 级的窗口仍在（写入方声明的，不随裁决改变）")
        contents = _contents(self.ledger.search("文档站点", limit=8, as_of=inside))
        control_visible = any("docs.control.example" in text for text in contents)
        site_contents = [text for text in contents if "docs." in text
                         and "docs.control.example" not in text]
        if site_contents:
            # as_of 已放行 superseded：真断言。
            self.assertTrue(
                any("docs.old.com" in text for text in site_contents),
                "as_of 放行了 superseded，却没返回窗口覆盖该时刻的旧域名: %s"
                % site_contents)
            self.assertFalse(
                any("docs.new.com" in text for text in site_contents),
                "as_of 在旧窗口内泄露了新域名（valid_from 尚未生效）: %s"
                % site_contents)
        else:
            # as_of 仍排除 superseded：缺口仍在，但对照组的可见性证明
            # as_of 机制本身工作正常，空结果只能归因于 superseded 排除。
            self.assertTrue(
                control_visible,
                "对照组在 as_of 查询中不可见——as_of 机制本身有问题，"
                "空结果不能归因于 superseded 排除")

    def test_once_a_claim_records_valid_until_as_of_must_answer(self):
        """硬门禁（专抓半修）：claim 一旦记下 valid_until，as_of 就必须能答。

        为什么这条要**单独**存在：上一条用 if/else 表达「缺口开着」和「缺口
        修好」两种自洽状态，但当 T2 只修了一半（写入 valid_until，却忘了让
        search 的 as_of 分支放行 superseded）时，分支写法容易让人以为「有分支
        覆盖了」。这里用 skipTest 明确区分三态：

        * claim 无 valid_until  -> skip（T2 未落地，门禁不适用；报告里是 skip，
          不是 pass，不会被当成已验证）；
        * 有 valid_until 且 as_of 答得出 -> pass；
        * 有 valid_until 但 as_of 答不出 -> **fail**（半修）。
        """
        old_id, _new_id = self._supersede()
        claims = {row["memory_id"]: row
                  for row in self.ledger.list_claims(scope="shared")}
        if claims[old_id]["valid_until"] == "":
            # 这个 skip 是**预期状态**，不是待修的 TODO。
            # 若 T2（claim.valid_until 写入 + as_of 放行 superseded）被回滚或
            # 尚未合入，claim 本来就没有失效时间，as_of 答不出旧值是正确行为；
            # 此时本门禁不适用，skip 是唯一诚实的报告方式（pass 会是假绿，
            # fail 会是误伤）。看到这条 skip 时**不要去改测试**——先去确认
            # temporal-engineer 的 T2 是否还在。只有当 claim 已经写了
            # valid_until 却仍答不出时，才说明是半修，那种情况本测试会 fail。
            self.skipTest(
                "claim 尚未记录 valid_until（T2 未合入或被回滚）——这是预期状态，"
                "不是待修问题；本门禁只在 T2 落地后生效")
        contents = self._as_of_contents()
        self.assertTrue(
            any("32G" in text for text in contents),
            "claim 已记录 valid_until，但 as_of 仍答不出旧值——半修: %s"
            % contents)
        self.assertFalse(
            any("64G" in text for text in contents),
            "as_of 过去不得泄露当前事实: %s" % contents)

    def test_as_of_before_window_start_returns_nothing(self):
        """as_of 落在窗口开始之前必须返回空——这不是 bug，是双时态的定义。

        这条测试的价值：它把「空结果」区分为**正确**与**缺口**两种情形。

        不传 valid_from 时，记忆的窗口从摄入时刻开始。问「1 小时前」，那时
        两条记忆都还不存在，as_of 返回空是唯一正确答案。如果没有这条测试，
        将来 valid_from 的写入被改坏（例如写成 epoch 0，或写成了「现在」而
        不是观察时刻），test_supersede_records_validity_consistently 反而会
        因为「as_of 能查到 A」而变绿——错误的方向被当成修复。

        注意断言分两半：先确认**现在**查得到（证明数据真的写进去了，空结果
        不是写失败造成的），再确认**过去**查不到。
        """
        now = time.time()
        self.ledger.ingest(_fact(
            "Redis 内存配置 32G", "bw-old", confidence=0.55,
            value={"memory": "32G"}, **self.SLOT))
        time.sleep(0.05)
        self.ledger.ingest(_fact(
            "Redis 已扩容到 64G 内存", "bw-new", confidence=0.9,
            value={"memory": "64G"}, **self.SLOT))
        # 前半：数据确实在，当前视图看得到新值。
        current = _contents(self.ledger.search("Redis 内存配置", limit=8))
        self.assertTrue(any("64G" in text for text in current),
                        "当前视图看不到新值，说明写入本身有问题: %s" % current)
        # 后半：1 小时前两条记忆都还没生效 -> 空。
        before = _contents(self.ledger.search(
            "Redis 内存配置", limit=8, as_of=_fmt(now - 3600)))
        self.assertEqual(
            before, [],
            "as_of 落在窗口开始之前却返回了记忆——valid_from 语义被改坏了: %s"
            % before)

    def test_claim_level_as_of_never_invents_a_value(self):
        """无论缺口修没修，as_of 都不许把 B 当成「以前」的答案。"""
        self._supersede()
        contents = self._as_of_contents()
        self.assertFalse(
            any("64G" in text for text in contents),
            "as_of 过去返回了当前事实 B，时间语义被破坏: %s" % contents)


if __name__ == "__main__":
    unittest.main()
