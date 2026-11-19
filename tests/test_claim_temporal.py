"""T2 双时态：claim/memory 的 valid_until 写入、as_of 历史切片、decay 事件契约。

## 这一组测试锁定的真实缺口（体检 docs/HEALTH_REVIEW.md 第三节）

生产账本实测：claims 4,771 条，valid_from 非空 4,771（100%），
valid_until 非空 **0**（0%）。系统能回答「什么时候开始的」，答不了
「什么时候失效的」，于是 ledger.search(as_of=...) 是假的。

## 为什么 valid_until 写「胜者的 valid_from」而不是「现在」

最直觉的写法是 supersede 时写 valid_until=now。它是错的，而且错得很隐蔽：
A 在 2026-01 生效、B 在 2026-06 生效，若 A.valid_until=now(2026-10)，
则 as_of=2026-08 会**同时**返回 A 和 B —— 两个窗口重叠，历史切片又变成
假的，只是从「总是空」变成「总是多」。

取胜者 valid_from 让窗口首尾相接：A=[01,06)、B=[06,∞)，任意时刻恰好一条
事实。这条规则只有一处实现（lifecycle._supersede_window_end），
identity._insert_claims 的高分覆盖路径 import 的是同一个函数——散在多个
写入点里，任何一处漏掉都会静默产生重叠窗口。

## as_of 真正跑起来需要三件事同时成立（缺一不可）

1. **窗口**：memories/claims 的 valid_until 真的被写入了（上面那条）；
2. **门禁**：search 的 quality_state 与 lifecycle 条件在 as_of 下必须放行
   superseded/expired。原代码把 if include_cold 和 if as_of 两条 lifecycle
   条件 **AND** 在一起，宽的那条永远不起作用——所以 as_of 对 expired 也
   从来没生效过（tests/test_as_of.py 一直绿，是因为它没调 maintain()，
   expired 行的 lifecycle 还停在 'active'）。
   test_as_of_sees_row_that_maintenance_already_expired 钉住这一条。
3. **候选生成**：过滤条件放宽了，候选生成路径必须跟上。热索引 memory_fts
   按设计只装「当前可召回」集合，维护会把 cold/superseded 行从索引里删掉；
   if terms: -> memory_fts MATCH 这条通路因此永远看不见历史行。as_of 必须
   跳过它、走已有的有界 LIKE 扫描
   （test_as_of_reaches_history_that_is_out_of_the_hot_index）。

第 3 条是**一类** bug，不止 as_of 有：include_cold=True（默认值！）同样只
影响 SQL 条件、影响不了 FTS 候选通路，所以一条 lifecycle='cold' 且已移出
索引的记忆，默认查询照样找不到它。将来修 include_cold 可以照抄 as_of 的
模式（跳过索引 + 有界扫描），但那是**改默认检索行为**，需要单独评估，
不在 T2 范围内。

## 为什么「没有显式 valid_from」的用例返回空是正确的

不传 valid_from 时它 = 摄入时刻 = 刚才。as_of=1 小时前 落在这条记忆的窗口
**之前**，所以它不该可见——这不是 bug，是双时态的定义。
test_as_of_before_a_fresh_row_is_correctly_empty 钉住这个区别，免得后来人
把正确行为误判成回归。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hippomind.ledger import SQLiteLedger

#: 两个固定时刻，让窗口边界与「现在」彻底解耦（测试不依赖运行时间）。
T_A = "2026-01-01T00:00:00+00:00"
T_B = "2026-06-01T00:00:00+00:00"
#: 落在 A 的窗口内、B 生效之前。
AS_OF_INSIDE_A = "2026-03-01T00:00:00+00:00"
#: 落在 B 的窗口内。
AS_OF_INSIDE_B = "2026-08-01T00:00:00+00:00"
#: 早于 A 生效。
AS_OF_BEFORE_A = "2025-12-01T00:00:00+00:00"


def _claim_record(content: str, marker: str, confidence: float, valid_from: str,
                  *, subject: str = "service.db", slot_key: str = "port") -> dict:
    """一条带结构化 claim 的事实；claim 的 slot 冲突由 confidence 决定胜负。"""
    return {
        "kind": "fact", "scope": "shared", "source_agent": "tester",
        "content": content, "evidence": ["evidence:" + marker],
        "subject": subject, "predicate": "config", "slot_key": slot_key,
        "value": {"port": marker}, "confidence": confidence,
        "valid_from": valid_from, "timestamp": valid_from,
    }


class _TemporalBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hippo-temporal-")
        self.addCleanup(self._tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self._tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)

    def _rows(self, sql, params=()):
        with self.ledger._reading() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def _memories(self):
        return {row["memory_id"]: row for row in
                self._rows("SELECT * FROM memories")}

    def _claims(self):
        return {row["claim_id"]: row for row in
                self._rows("SELECT * FROM memory_claims")}

    def _contents(self, rows):
        return sorted(row["content"] for row in rows)

    def _supersede_pair(self):
        """A 低分先写、B 高分后写同一 slot -> A 被顶掉（identity 覆盖路径）。"""
        a = self.ledger.ingest(_claim_record(
            "数据库端口配置甲 1111", "1111", 0.50, T_A))
        b = self.ledger.ingest(_claim_record(
            "数据库端口配置乙 2222", "2222", 0.95, T_B))
        return a, b


class ValidUntilWriteTests(_TemporalBase):
    """验收 1：supersede 时必须闭合窗口（claim 和 memory 两层）。"""

    def test_superseded_claim_gets_valid_until_and_winner_stays_open(self):
        a, b = self._supersede_pair()
        claims = self._claims()
        loser = next(c for c in claims.values() if c["memory_id"] == a.memory_id)
        winner = next(c for c in claims.values() if c["memory_id"] == b.memory_id)

        self.assertEqual("superseded", loser["state"])
        self.assertEqual(T_B, loser["valid_until"],
                         "败者的窗口必须闭合在胜者生效时刻，不是写入时刻")
        self.assertEqual("active", winner["state"])
        self.assertEqual("", winner["valid_until"], "仍有效的 claim 窗口必须为空")

    def test_superseded_memory_gets_valid_until(self):
        """search 过滤的是 memories.valid_until，只写 claim 层门禁不生效。"""
        a, b = self._supersede_pair()
        memories = self._memories()
        self.assertEqual(T_B, memories[a.memory_id]["valid_until"])
        self.assertEqual("", memories[b.memory_id]["valid_until"])
        # 两层必须给出同一段历史，否则审计和对检索会互相矛盾。
        claims = self._claims()
        loser_claim = next(c for c in claims.values() if c["memory_id"] == a.memory_id)
        self.assertEqual(memories[a.memory_id]["valid_until"],
                         loser_claim["valid_until"])

    def test_existing_valid_until_is_never_overwritten(self):
        """用户显式登记的失效时间优先于派生值。"""
        a = self.ledger.ingest(_claim_record("数据库端口配置甲 1111", "1111", 0.50, T_A))
        explicit = "2026-02-01T00:00:00+00:00"
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET valid_until=? WHERE memory_id=?",
                         (explicit, a.memory_id))
            conn.execute("UPDATE memory_claims SET valid_until=? WHERE memory_id=?",
                         (explicit, a.memory_id))
        self.ledger.ingest(_claim_record("数据库端口配置乙 2222", "2222", 0.95, T_B))
        memories = self._memories()
        self.assertEqual(explicit, memories[a.memory_id]["valid_until"],
                         "显式登记的窗口被派生值覆盖了")
        claims = self._claims()
        loser_claim = next(c for c in claims.values() if c["memory_id"] == a.memory_id)
        self.assertEqual(explicit, loser_claim["valid_until"])

    def test_resolve_claim_closes_loser_window(self):
        """近似平局 -> 双 conflict -> 人工裁决，败者窗口同样要闭合。"""
        a = self.ledger.ingest(_claim_record("数据库端口配置甲 1111", "1111", 0.50, T_A))
        b = self.ledger.ingest(_claim_record("数据库端口配置乙 2222", "2222", 0.52, T_B))
        conflicts = self.ledger.list_claims(state="conflict", scope="shared")
        self.assertEqual(2, len(conflicts), "夹具没有造出 conflict")
        claim_a = next(c["claim_id"] for c in conflicts if c["memory_id"] == a.memory_id)
        self.ledger.resolve_claim(claim_a, b.memory_id)

        memories = self._memories()
        self.assertEqual(T_B, memories[a.memory_id]["valid_until"])
        claims = self._claims()
        loser = next(c for c in claims.values() if c["memory_id"] == a.memory_id)
        self.assertEqual(T_B, loser["valid_until"])
        # 胜者必须保持开窗，否则它自己也变成「已失效」。
        self.assertEqual("", memories[b.memory_id]["valid_until"])

    def test_recrown_clears_the_window_again(self):
        """翻案重裁：胜者上一次被写下的失效窗口必须撤销。

        不撤销的后果是胜者 claim 带着「已失效」的窗口复活：identity 的冲突
        探测要求 valid_until 未过，as_of 也按窗口求值——两处都看不见它，
        裁决等于没生效（与 lifecycle 不恢复是同一类缺陷）。
        """
        a = self.ledger.ingest(_claim_record("数据库端口配置甲 1111", "1111", 0.50, T_A))
        b = self.ledger.ingest(_claim_record("数据库端口配置乙 2222", "2222", 0.52, T_B))
        conflicts = self.ledger.list_claims(state="conflict", scope="shared")
        claim_a = next(c["claim_id"] for c in conflicts if c["memory_id"] == a.memory_id)
        claim_b = next(c["claim_id"] for c in conflicts if c["memory_id"] == b.memory_id)
        self.ledger.resolve_claim(claim_a, b.memory_id)   # B 胜，A 窗口闭合
        self.ledger.resolve_claim(claim_b, a.memory_id)   # 翻案：A 胜

        memories = self._memories()
        self.assertEqual("active", memories[a.memory_id]["lifecycle"])
        self.assertEqual("", memories[a.memory_id]["valid_until"],
                         "重裁胜者仍带着上一次裁决写下的失效窗口")
        claims = self._claims()
        winner_claim = next(c for c in claims.values() if c["memory_id"] == a.memory_id)
        self.assertEqual("active", winner_claim["state"])
        self.assertEqual("", winner_claim["valid_until"])

    def test_repair_claim_states_closes_window_with_now(self):
        """收敛路径没有胜者，失效时刻只能是「发现它不可召回」的那一刻。"""
        result = self.ledger.ingest(_claim_record(
            "缓存 TTL 配置 300", "300", 0.9, T_A))
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET quality_state='quarantine' "
                         "WHERE memory_id=?", (result.memory_id,))
        report = self.ledger.repair_claim_states()
        self.assertGreaterEqual(report["superseded"], 1)
        claims = self._claims()
        claim = next(c for c in claims.values() if c["memory_id"] == result.memory_id)
        self.assertEqual("superseded", claim["state"])
        self.assertNotEqual("", claim["valid_until"], "收敛退休的 claim 没有闭合窗口")


if __name__ == "__main__":
    unittest.main()

class AsOfGateTests(_TemporalBase):
    """验收 3.2：as_of 历史切片必须回答「当时系统认为什么为真」。"""

    def test_as_of_returns_the_fact_that_held_then(self):
        a, b = self._supersede_pair()
        hits = self.ledger.search("数据库端口配置", scope="shared", limit=10,
                                  as_of=AS_OF_INSIDE_A)
        self.assertEqual(["数据库端口配置甲 1111"], self._contents(hits),
                         "as_of 落在 A 的窗口内，必须只返回 A")

    def test_now_returns_only_the_current_fact(self):
        a, b = self._supersede_pair()
        hits = self.ledger.search("数据库端口配置", scope="shared", limit=10)
        self.assertEqual(["数据库端口配置乙 2222"], self._contents(hits),
                         "默认查询必须只返回当前有效的事实")

    def test_as_of_inside_winner_window_returns_winner(self):
        """A 的窗口闭合在 B 生效时刻，所以 as_of=B 之后只该看见 B。"""
        a, b = self._supersede_pair()
        hits = self.ledger.search("数据库端口配置", scope="shared", limit=10,
                                  as_of=AS_OF_INSIDE_B)
        self.assertEqual(["数据库端口配置乙 2222"], self._contents(hits))

    def test_windows_do_not_overlap(self):
        """双时态的核心不变式：任意时刻至多一条事实成立。

        valid_until=now 的写法会让 as_of=2026-08 同时返回 A 和 B —— 从
        「总是空」变成「总是多」，历史切片仍然是假的。这条测试直接枚举
        时间轴，任何窗口重叠都会被抓到。
        """
        a, b = self._supersede_pair()
        for stamp, expected in (
            (AS_OF_BEFORE_A, []),
            (AS_OF_INSIDE_A, ["数据库端口配置甲 1111"]),
            (AS_OF_INSIDE_B, ["数据库端口配置乙 2222"]),
        ):
            hits = self.ledger.search("数据库端口配置", scope="shared", limit=10,
                                      as_of=stamp)
            self.assertEqual(expected, self._contents(hits),
                             "as_of=%s 的切片不对（窗口重叠或缺口）" % stamp)

    def test_as_of_sees_row_that_maintenance_already_expired(self):
        """钉住被 AND 死的 lifecycle 条件（既有 bug，as_of 上线时就存在）。

        原代码先按 include_cold 加一条 m.lifecycle IN ('active','cold')，
        再给 as_of 追加一条更宽的 IN (...'expired')——两条 AND 在一起，
        宽的那条永远不起作用。tests/test_as_of.py 之所以一直绿，是因为它
        从没调用 maintain()：expired 行的 lifecycle 还停在 'active'。
        这里让维护真的把行打成 expired，再验历史切片能看见它。
        """
        result = self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "临时维护窗口事实 7777", "evidence": ["evidence:7777"],
            "event_id": "temporal-expired", "timestamp": T_A,
            "valid_until": "2026-02-01T00:00:00+00:00",
        })
        report = self.ledger.maintain(archive_raw=False)
        self.assertGreaterEqual(report["expired"], 1)
        self.assertEqual("expired", self._memories()[result.memory_id]["lifecycle"],
                         "夹具没有真的让 maintain 把行打成 expired")

        # as_of 落在窗口内 -> 必须可见（这条在修复前返回空）。
        inside = self.ledger.search("临时维护窗口事实", scope="shared", limit=10,
                                    as_of="2026-01-15T00:00:00+00:00")
        self.assertEqual(["临时维护窗口事实 7777"], self._contents(inside),
                         "as_of 看不见已被维护标为 expired 的行（lifecycle 条件被 AND 死）")
        # 默认查询（现在已过期）-> 必须不可见。
        self.assertEqual([], self.ledger.search("临时维护窗口事实", scope="shared",
                                                limit=10))

    def test_as_of_reaches_history_that_is_out_of_the_hot_index(self):
        """钉住候选生成路径：热索引只装可召回集合，历史行不在里面。

        supersede 时败者会被 DELETE FROM memory_fts。as_of 如果仍走
        memory_fts MATCH 生成候选，组合查询（A、B 文本都命中）就永远看不见
        A——过滤条件放宽了但候选通路没跟上，as_of 只在 LIKE 回退路径上
        成立，仍是假双时态。
        """
        a, b = self._supersede_pair()
        with self.ledger._reading() as conn:
            indexed = conn.execute("SELECT 1 FROM memory_fts WHERE memory_id=?",
                                   (a.memory_id,)).fetchone()
        self.assertIsNone(indexed, "夹具前提不成立：败者仍在热索引里")

        hits = self.ledger.search("数据库端口配置", scope="shared", limit=10,
                                  as_of=AS_OF_INSIDE_A)
        self.assertEqual(["数据库端口配置甲 1111"], self._contents(hits),
                         "as_of 没能在热索引之外找到历史行")

    def test_as_of_never_revives_quarantine_or_conflict(self):
        """历史切片是「当时为真」，不是「当时存在」。

        放宽 quality_state 只为 superseded（时间语义）；quarantine（质量不
        合格）和 conflict（无法裁决）与时间无关，任何时刻都不该进 Context
        Pack。这条防止将来有人为了「让 as_of 更宽」把整个条件拆掉。
        """
        quarantined = self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "低质量隔离事实 8888", "evidence": ["evidence:8888"],
            "event_id": "temporal-quarantine", "timestamp": T_A,
        })
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memories SET quality_state='quarantine' "
                         "WHERE memory_id=?", (quarantined.memory_id,))
        hits = self.ledger.search("低质量隔离事实", scope="shared", limit=10,
                                  as_of=AS_OF_INSIDE_A)
        self.assertEqual([], self._contents(hits), "as_of 把隔离行捞回来了")

    def test_as_of_before_a_fresh_row_is_correctly_empty(self):
        """没有显式 valid_from 时，valid_from = 摄入时刻 = 刚才。

        as_of=1 小时前 落在这条记忆的窗口**之前**，所以它不该可见——这是
        双时态的定义，不是 bug。写下来免得后来人把它当回归修掉。

        注意夹具不能带 event_id：identity._prepare_ingest 对「有 external_id
        但没时间戳」的归档行用 1970-01-01 哨兵作 occurred_at，而
        valid_from 默认继承 occurred_at —— 那条行会变成「自 1970 年起有效」，
        as_of 怎么看都可见。这是归档重放路径的正确设计（缺时间戳的旧数据
        不能被误判成新事实），但会掩盖这里要验的窗口语义。
        """
        self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "刚刚写入的事实 6666", "evidence": ["evidence:6666"],
        })
        memory = next(iter(self._memories().values()))
        self.assertNotEqual("", memory["valid_from"], "valid_from 必须已回填")
        self.assertGreater(memory["valid_from"], T_B,
                           "无显式 valid_from 时应取摄入时刻（远晚于夹具时刻）")

        an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertEqual([],
                         self._contents(self.ledger.search(
                             "刚刚写入的事实", scope="shared", limit=10,
                             as_of=an_hour_ago)),
                         "as_of 早于这条记忆生效，正确地应为空")
        # 窗口内（含未来一点）必须可见——证明上面的空是窗口语义而非查询失灵。
        just_after = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        self.assertEqual(["刚刚写入的事实 6666"],
                         self._contents(self.ledger.search(
                             "刚刚写入的事实", scope="shared", limit=10,
                             as_of=just_after)))
        self.assertEqual(["刚刚写入的事实 6666"],
                         self._contents(self.ledger.search(
                             "刚刚写入的事实", scope="shared", limit=10)),
                         "默认查询必须看得见刚写入的事实")

    def test_archived_row_without_timestamp_is_valid_since_epoch(self):
        """归档重放行的 1970 哨兵必须让它在任意 as_of 下可见。

        与上一条互补：external_id 存在但无时间戳 = 历史数据重放，缺时间戳
        不能被误判成「刚刚发生」。这条钉住这个区别，免得有人把哨兵「修」
        成 utc_now()，那会让整批归档在 as_of 历史切片里凭空消失。
        """
        self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "无时间戳的归档重放行 9999", "evidence": ["evidence:9999"],
            "event_id": "temporal-replay",
        })
        memory = next(iter(self._memories().values()))
        self.assertEqual("1970-01-01T00:00:00+00:00", memory["valid_from"])
        self.assertEqual(["无时间戳的归档重放行 9999"],
                         self._contents(self.ledger.search(
                             "无时间戳的归档重放行", scope="shared", limit=10,
                             as_of=AS_OF_INSIDE_A)),
                         "缺时间戳的归档行必须落在所有窗口内")


    def test_default_query_is_unchanged_without_as_of(self):
        """默认路径（as_of=''）必须与改动前等价。

        as_of 的全部改动都以 as_of 为条件；as_of 为空时三个 if 分支都走
        原来的分支。这条用一组查询把默认行为钉住。
        """
        self._supersede_pair()
        self.ledger.ingest({
            "kind": "decision", "scope": "shared", "source_agent": "tester",
            "content": "无关的部署决策 systemd", "evidence": ["evidence:sys"],
            "event_id": "temporal-other", "timestamp": T_B,
        })
        # 败者不在默认结果里；存活的两条都在。
        default_hits = self.ledger.search("端口配置 部署决策", scope="shared", limit=10)
        contents = self._contents(default_hits)
        self.assertNotIn("数据库端口配置甲 1111", contents, "默认查询漏出了败者")
        self.assertIn("数据库端口配置乙 2222", contents)
        self.assertIn("无关的部署决策 systemd", contents)
        # include_cold=False 的窄路径也不受影响。
        self.assertEqual(
            self._contents(self.ledger.search("端口配置", scope="shared", limit=10,
                                              include_cold=False)),
            self._contents(self.ledger.search("端口配置", scope="shared", limit=10)),
            "include_cold=False 在无冷数据时结果应与默认一致")


class WindowInvariantTests(_TemporalBase):
    """_supersede_window_end 的不变式：返回值要么空，要么严格大于败者 valid_from。

    这条不变式比任何单点测试都值钱：它把「零宽窗口」挡在全部 6 个调用点
    之外，而不是靠每个调用点自觉。
    """

    def test_winner_later_than_loser_wins(self):
        a, b = self._supersede_pair()
        from hippomind.ledger.lifecycle import _supersede_window_end
        with self.ledger._connect(write=True) as conn:
            self.assertEqual(T_B, _supersede_window_end(conn, T_A, b.memory_id, "2026-10-01T00:00:00+00:00"))

    def test_winner_earlier_than_loser_falls_back_to_now(self):
        """倒置时不能取胜者（会造负宽窗口），退回 now。"""
        a, b = self._supersede_pair()
        from hippomind.ledger.lifecycle import _supersede_window_end
        now = "2026-12-01T00:00:00+00:00"
        with self.ledger._connect(write=True) as conn:
            got = _supersede_window_end(conn, T_B, b.memory_id, now)
        self.assertEqual(now, got)
        self.assertGreater(got, T_B, "返回的窗口端点必须严格晚于败者 valid_from")

    def test_now_before_loser_returns_empty_rather_than_inverting(self):
        """败者 valid_from 在未来（时钟回拨）时宁可返回空，也不倒置。"""
        from hippomind.ledger.lifecycle import _supersede_window_end
        future_loser = "2030-01-01T00:00:00+00:00"
        now = "2026-12-01T00:00:00+00:00"
        with self.ledger._connect(write=True) as conn:
            self.assertEqual("", _supersede_window_end(conn, future_loser, "", now))

    def test_no_winner_uses_now(self):
        from hippomind.ledger.lifecycle import _supersede_window_end
        now = "2026-12-01T00:00:00+00:00"
        with self.ledger._connect(write=True) as conn:
            self.assertEqual(now, _supersede_window_end(conn, T_A, "", now))

    def test_same_second_folds_to_the_next_second(self):
        """WP-C：now 与败者 valid_from 同秒时，旧实现返回空串（零宽/永不闭合）。

        修复后的口径：返回败者 valid_from 的下一秒（ISO），窗口 [X, X+1s)
        严格非零；这保持「空串仅当无法构造非零窗口」的不变式。
        """
        from hippomind.ledger.lifecycle import _supersede_window_end
        with self.ledger._connect(write=True) as conn:
            got = _supersede_window_end(conn, T_A, "", T_A)
        self.assertEqual("2026-01-01T00:00:01+00:00", got)
        self.assertGreater(got, T_A, "同秒也必须产出严格非零窗口")


class SameSecondSupersedeTests(_TemporalBase):
    """WP-C：秒级时间戳下的 supersede 窗口闭合。

    生产账本实测 1,715 条 superseded claim 的 valid_from 与 asserted_at 在
    同一秒。旧 _supersede_window_end 用字符串比较 now > loser_valid_from，
    同秒时为假 -> 返回空串 -> 窗口永不闭合 -> as_of=未来同时看见败者与
    胜者，双时态又变成假的（与 valid_until=now 的窗口重叠同类，只是方向
    相反）。
    """

    @staticmethod
    def _record(marker: str, slot: str, value: str) -> dict:
        # 两个记忆各带一个 **不同槽位** 的 claim：既让 claim 层窗口可断言，
        # 又不会在摄入时互相冲突（冲突会换成 conflict 路径，夹具就偏了）。
        return {
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "数据库端口配置" + marker, "evidence": ["e:same-" + marker],
            "event_id": "same-sec-" + marker, "timestamp": T_A,
            "valid_from": T_A,
            "claims": [{"subject": "svc", "predicate": "p", "slot_key": slot,
                        "value": value, "confidence": 0.9}],
        }

    def _pair(self):
        a = self.ledger.ingest(self._record("甲 1111", "port", "1111"))
        b = self.ledger.ingest(self._record("乙 2222", "host", "2222"))
        return a, b

    def test_same_second_supersede_closes_a_nonzero_window(self):
        a, b = self._pair()
        with mock.patch("hippomind.ledger.lifecycle.utc_now",
                        return_value=T_A):
            self.assertTrue(self.ledger.supersede(a.memory_id, b.memory_id))
        memories = self._memories()
        self.assertEqual("2026-01-01T00:00:01+00:00",
                         memories[a.memory_id]["valid_until"],
                         "同秒 supersede 没有闭合出非零窗口")
        self.assertGreater(memories[a.memory_id]["valid_until"],
                           memories[a.memory_id]["valid_from"])
        claims = self._claims()
        loser = next(c for c in claims.values()
                     if c["memory_id"] == a.memory_id)
        self.assertEqual(memories[a.memory_id]["valid_until"],
                         loser["valid_until"],
                         "claim 与 memory 两层窗口必须给出同一段历史")
        self.assertEqual("", memories[b.memory_id]["valid_until"],
                         "胜者窗口必须保持敞开")

    def test_future_as_of_does_not_show_both_sides(self):
        """窗口闭合后，as_of 落在未来时不得同时返回败者与胜者。"""
        a, b = self._pair()
        with mock.patch("hippomind.ledger.lifecycle.utc_now",
                        return_value=T_A):
            self.ledger.supersede(a.memory_id, b.memory_id)
        future = "2026-01-01T00:01:00+00:00"
        hits = self.ledger.search("数据库端口配置", scope="shared", limit=10,
                                  as_of=future)
        self.assertEqual(["数据库端口配置乙 2222"], self._contents(hits),
                         "as_of=未来同时看见败者与胜者：窗口没有闭合")


class FeedbackConfidenceTests(_TemporalBase):
    """WP-C：负反馈不得提升 confidence（旧代码用 abs(signal)）。

    confidence 衡量的是「这条记忆有多可信」。旧实现里 signal=-1 与 +1 一样
    加 0.04（abs），等于「指出它错了反而让它更可信」——方向反了，而且失败
    验收会系统性地抬高错误记忆的置信度。修复后只有正信号提升。
    """

    def _ingest(self):
        return self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "被反馈的部署事实", "evidence": ["e:feedback"],
            "event_id": "feedback-conf", "timestamp": T_A,
        })

    def test_negative_signal_does_not_raise_confidence(self):
        result = self._ingest()
        before = self.ledger.get_memory(result.memory_id)["confidence"]
        self.assertTrue(self.ledger.feedback(
            result.memory_id, -1.0, "验收失败", "tester", "fb-neg"))
        after = self.ledger.get_memory(result.memory_id)["confidence"]
        self.assertLessEqual(after, before,
                             "负反馈把 confidence 抬高了（abs(signal) 旧缺陷）")

    def test_positive_signal_still_raises_confidence(self):
        result = self._ingest()
        before = self.ledger.get_memory(result.memory_id)["confidence"]
        self.assertTrue(self.ledger.feedback(
            result.memory_id, 1.0, "验收通过", "tester", "fb-pos"))
        after = self.ledger.get_memory(result.memory_id)["confidence"]
        self.assertGreater(after, before, "正反馈应当提升 confidence")


class DecayEventTests(_TemporalBase):
    """验收 4：maintain() 为每次 lifecycle 变化落一条 decay 事件。

    这条事件是 T5 遗忘审计报告（hippomind/forgetting.py）的唯一数据源，
    metadata 的字段名是读取契约，必须逐字一致。
    """

    REQUIRED_FIELDS = ("memory_id", "from_lifecycle", "to_lifecycle", "reason",
                       "weight_before", "weight_after", "decay_per_day",
                       "age_days", "utility", "importance")

    def _decay_events(self):
        rows = self._rows("SELECT * FROM ledger_events WHERE event_type='decay' "
                          "ORDER BY occurred_at")
        return [json.loads(row["metadata_json"]) for row in rows]

    def test_expired_row_emits_decay_event_with_full_contract(self):
        result = self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "会过期的临时事实 5555", "evidence": ["evidence:5555"],
            "event_id": "decay-expired", "timestamp": T_A,
            "valid_until": "2026-02-01T00:00:00+00:00",
        })
        self.ledger.maintain(archive_raw=False)

        events = self._decay_events()
        self.assertEqual(1, len(events), "过期应当且只应当落一条 decay 事件")
        payload = events[0]
        for field in self.REQUIRED_FIELDS:
            self.assertIn(field, payload, "decay 事件缺少契约字段 %s" % field)
        self.assertEqual(result.memory_id, payload["memory_id"])
        self.assertEqual("expired", payload["reason"])
        self.assertEqual("active", payload["from_lifecycle"])
        self.assertEqual("expired", payload["to_lifecycle"])

    def test_raw_retention_and_age_limit_reasons(self):
        self.ledger.ingest({
            "kind": "raw_chat", "raw_chat": True, "content": "很老的会话原文",
            "source_agent": "tester", "event_id": "decay-raw",
            "timestamp": "2000-01-01T00:00:00+00:00", "scope": "shared",
            "role": "assistant", "evidence": ["evidence:raw"],
        })
        self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "很老的结构化记忆", "evidence": ["evidence:old"],
            "event_id": "decay-old", "timestamp": "2000-01-01T00:00:00+00:00",
            "weight": 0.1,
        })
        self.ledger.maintain(cold_after_days=1, raw_after_days=1, archive_raw=False)

        by_reason = {event["reason"]: event for event in self._decay_events()}
        self.assertIn("raw_retention", by_reason,
                      "原始会话超保留期应记为 raw_retention")
        self.assertEqual("cold", by_reason["raw_retention"]["to_lifecycle"])
        # weight=0.1 低于 0.12 冷置下限 -> weight_floor 优先于 age_limit 归因。
        self.assertIn("weight_floor", by_reason, "权重跌破下限应记为 weight_floor")

    def test_slow_decay_does_not_flood_the_ledger(self):
        """1e-9 阈值会把账本淹掉，1% 绝对降幅不会——这条钉住后者。

        默认 decay_per_day=0.008、增量窗口 1 小时，一条 weight=0.5 的行
        每 tick 掉约 1.7e-4，远超 1e-9。按 1e-9 落事件，3,000 条活跃行
        会产出约 72,000 条/天，一两天撑满 ledger_events 的 100k 保留窗口，
        随后被 maintain 自己的归档逻辑移走——而 forgetting.py 只读主表，
        T5 的报告会变成「账本里从来没有 decay 事件」。
        """
        for index in range(20):
            self.ledger.ingest({
                "kind": "fact", "scope": "shared", "source_agent": "tester",
                "content": "缓慢衰减样本 %02d" % index,
                "evidence": ["evidence:slow%d" % index],
                "event_id": "decay-slow-%02d" % index,
                "timestamp": T_A, "weight": 0.5,
            })
        # 第一次维护：行龄已远超冷置线，会真的归档（属于 lifecycle 变化，
        # 应该落事件）。之后的行都在冷置态，不再产生事件。
        first = self.ledger.maintain(cold_after_days=1, archive_raw=False)
        self.assertGreaterEqual(first["decay_events"], 20)
        # 纯权重重算（没有 lifecycle 变化、降幅 < 1%）不得落事件。
        before = len(self._decay_events())
        second = self.ledger.maintain(cold_after_days=1, archive_raw=False)
        self.assertEqual(0, second["decay_events"],
                         "纯权重重算不应产生 decay 事件")
        self.assertEqual(before, len(self._decay_events()))

    def test_no_events_for_healthy_rows(self):
        """健康行的维护 tick 不该产生任何 decay 事件（增量维护性质）。"""
        self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "健康的活跃事实 4444", "evidence": ["evidence:4444"],
            "event_id": "decay-healthy", "timestamp": T_A, "weight": 0.9,
        })
        report = self.ledger.maintain(cold_after_days=36500, archive_raw=False)
        self.assertEqual(0, report["decay_events"])
        self.assertEqual([], self._decay_events())

    def test_events_are_readable_by_the_forgetting_contract(self):
        """写入侧和读取侧（T5）必须真的对得上，不能只靠字段名约定。"""
        from hippomind import forgetting

        self.ledger.ingest({
            "kind": "fact", "scope": "shared", "source_agent": "tester",
            "content": "审计报告可读性样本 3333", "evidence": ["evidence:3333"],
            "event_id": "decay-readable", "timestamp": T_A,
            "valid_until": "2026-02-01T00:00:00+00:00",
        })
        self.ledger.maintain(archive_raw=False)

        reader = forgetting.ReadOnlyLedger(self.ledger.path)
        self.addCleanup(reader.close)
        loaded = forgetting.decay_events(reader, days=36500)
        self.assertEqual(0, loaded["malformed"],
                         "写入侧的事件无法被读取侧解析")
        self.assertEqual(1, len(loaded["events"]))
        event = loaded["events"][0]
        self.assertEqual("expired", event["reason"],
                         "reason 不在读取侧的契约取值里，会被归成 unknown")
        self.assertNotEqual("unknown", event["reason"])
        self.assertTrue(event["archived"])


class BackfillTests(_TemporalBase):
    """验收 3.3 + 回填脚本：规则正确、幂等、默认 dry-run、apply 前备份。"""

    def _script(self):
        import sys
        root = Path(__file__).resolve().parents[1]
        if str(root / "scripts") not in sys.path:
            sys.path.insert(0, str(root / "scripts"))
        import backfill_valid_until
        return backfill_valid_until

    @staticmethod
    def _run_quietly(fn, argv):
        # 脚本的 main() 会打印整份报告；测试只关心返回码与账本状态，
        # 不静音的话 27 项测试会往 CI 日志里灌几百行。
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            return fn(argv)

    def _seed_legacy_superseded(self):
        """造出「写入侧修复之前」的历史形态：superseded 但窗口未闭合。

        B 高分覆盖 A 之后，把 A 的 valid_until 清空，模拟旧账本。
        """
        a, b = self._supersede_pair()
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memory_claims SET valid_until='' WHERE memory_id=?",
                         (a.memory_id,))
            conn.execute("UPDATE memories SET valid_until='' WHERE memory_id=?",
                         (a.memory_id,))
        return a, b

    def test_scan_proposes_winner_valid_from(self):
        backfill = self._script()
        a, b = self._seed_legacy_superseded()
        conn = backfill.open_readonly(Path(self.ledger.path))
        self.addCleanup(conn.close)
        rows = backfill.scan(conn)
        self.assertEqual(1, len(rows))
        self.assertEqual(T_B, rows[0]["proposed_valid_until"])
        self.assertEqual("winner", rows[0]["basis"])

    def test_never_creates_a_zero_width_window(self):
        """无胜者的 superseded claim 必须跳过，不能拿 asserted_at 冒充失效时间。

        生产账本实测：1,715 条 superseded claim 的 superseded_by 为空
        （它们不是被裁决取代的，是被容量/年龄挤出成 cold 的），且
        valid_from 与 asserted_at 在同一秒。用 asserted_at 当 valid_until
        会造出 [X, X) 的零宽窗口——那不是「近似不准」，而是一个更强也更假
        的断言：

            valid_until=''   -> 「仍然有效，只是没有历史」
            valid_until=X    -> 「可证明从未为真」

        留空的语义是「由后续裁决或维护决定」，这是诚实的那一侧。
        """
        backfill = self._script()
        result = self.ledger.ingest(_claim_record("孤立冲突事实 2222", "2222", 0.9, T_A))
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memory_claims SET state='superseded',valid_until='' "
                         "WHERE memory_id=?", (result.memory_id,))
            conn.execute("UPDATE memories SET lifecycle='superseded',quality_state='superseded' "
                         "WHERE memory_id=?", (result.memory_id,))
        conn = backfill.open_readonly(Path(self.ledger.path))
        self.addCleanup(conn.close)
        rows = backfill.scan(conn)
        self.assertEqual(1, len(rows))
        self.assertEqual("", rows[0]["proposed_valid_until"],
                         "无胜者的行被回填了，会造出零宽窗口")
        self.assertEqual("no_winner", rows[0]["basis"])

        summary = backfill.summarise(rows)
        self.assertEqual(0, summary["actionable"])
        self.assertEqual(1, summary["skipped_by_reason"]["no_winner"])

        # apply 也真的不写。
        backup_dir = Path(self._tmp.name) / "bk"
        self._run_quietly(backfill.main, ["--ledger", str(self.ledger.path),
                                          "--apply", "--backup-dir", str(backup_dir)])
        claims = self._claims()
        claim = next(c for c in claims.values() if c["memory_id"] == result.memory_id)
        self.assertEqual("", claim["valid_until"], "跳过的行竟然被写入了")
        self.assertFalse(backup_dir.exists() and list(backup_dir.glob("*.sqlite")),
                         "没有可写内容时不该产生备份")

    def test_inverted_winner_time_is_skipped_not_written(self):
        """胜者 valid_from 早于败者时也必须跳过（同一种零宽/倒置错误形态）。

        理论上不该发生，但时钟回拨和乱序导入都会造出它。写下去就是负宽窗口：
        这条事实在任何 as_of 下都不可见，而且和零宽一样看起来「精确」。
        """
        backfill = self._script()
        a = self.ledger.ingest(_claim_record("时间倒置的败者 3333", "3333", 0.9, T_B))
        b = self.ledger.ingest(_claim_record("时间更早的胜者 4444", "4444", 0.95, T_A))
        with self.ledger._connect(write=True) as conn:
            # 造出「败者 valid_from=T_B、胜者 valid_from=T_A」的倒置
            conn.execute("UPDATE memories SET superseded_by=? WHERE memory_id=?",
                         (b.memory_id, a.memory_id))
            conn.execute("UPDATE memory_claims SET state='superseded',valid_until='' "
                         "WHERE memory_id=?", (a.memory_id,))
        conn = backfill.open_readonly(Path(self.ledger.path))
        self.addCleanup(conn.close)
        rows = backfill.scan(conn)
        self.assertEqual(1, len(rows))
        self.assertEqual("inverted", rows[0]["basis"])
        self.assertEqual("", rows[0]["proposed_valid_until"])

    def test_dry_run_is_the_default_and_writes_nothing(self):
        backfill = self._script()
        a, b = self._seed_legacy_superseded()
        code = self._run_quietly(backfill.main, ["--ledger", str(self.ledger.path)])
        self.assertEqual(0, code)
        claims = self._claims()
        loser = next(c for c in claims.values() if c["memory_id"] == a.memory_id)
        self.assertEqual("", loser["valid_until"], "dry-run 竟然写入了")

    def test_apply_backfills_claim_and_memory_then_is_idempotent(self):
        backfill = self._script()
        a, b = self._seed_legacy_superseded()
        backup_dir = Path(self._tmp.name) / "backups"

        first = self._run_quietly(backfill.main, [
            "--ledger", str(self.ledger.path), "--apply",
            "--backup-dir", str(backup_dir)])
        self.assertEqual(0, first)
        claims = self._claims()
        loser = next(c for c in claims.values() if c["memory_id"] == a.memory_id)
        self.assertEqual(T_B, loser["valid_until"], "回填没写 claim 窗口")
        self.assertEqual(T_B, self._memories()[a.memory_id]["valid_until"],
                         "回填没写 memory 窗口，as_of 门禁不会生效")
        # apply 前必须留下一致性备份
        backups = list(backup_dir.glob("valid-until-backup-*.sqlite"))
        self.assertEqual(1, len(backups), "apply 前没有做备份")
        self.assertGreater(backups[0].stat().st_size, 0)

        # 幂等：第二次没有候选、不再写、也不再新增备份。
        snapshot = dict((cid, c["valid_until"]) for cid, c in claims.items())
        second = self._run_quietly(backfill.main, [
            "--ledger", str(self.ledger.path), "--apply",
            "--backup-dir", str(backup_dir)])
        self.assertEqual(0, second)
        claims_again = self._claims()
        self.assertEqual(snapshot,
                         dict((cid, c["valid_until"]) for cid, c in claims_again.items()))
        self.assertEqual(1, len(list(backup_dir.glob("valid-until-backup-*.sqlite"))),
                         "幂等的 apply 不该再产生备份")

    def test_backfill_makes_as_of_work_for_history(self):
        """回填的最终判据：as_of 能回答「当时是什么」。"""
        backfill = self._script()
        a, b = self._seed_legacy_superseded()
        # 回填前：A 的窗口敞开，as_of 落在 B 之后时会同时返回两条。
        before = self._contents(self.ledger.search(
            "数据库端口配置", scope="shared", limit=10, as_of=AS_OF_INSIDE_B))
        self.assertIn("数据库端口配置甲 1111", before,
                      "夹具前提：未回填时 A 的窗口确实敞开（历史切片多返回一条）")

        self._run_quietly(backfill.main, [
            "--ledger", str(self.ledger.path), "--apply",
            "--backup-dir", str(Path(self._tmp.name) / "b")])
        # 回填脚本用裸 sqlite3 连接写入（生产里它是独立进程），不会 bump 本
        # 进程的 _generation，所以本实例的 search 缓存仍持有回填前的页。
        # 生产里 daemon 的缓存靠 5s TTL 自然失效；测试里显式清掉，模拟那个
        # 时刻。不清的话这里断言的是缓存而不是账本。
        self.ledger._search_cache.clear()
        after = self._contents(self.ledger.search(
            "数据库端口配置", scope="shared", limit=10, as_of=AS_OF_INSIDE_B))
        self.assertEqual(["数据库端口配置乙 2222"], after,
                         "回填后 as_of=B 生效之后仍返回败者")
        inside_a = self._contents(self.ledger.search(
            "数据库端口配置", scope="shared", limit=10, as_of=AS_OF_INSIDE_A))
        self.assertEqual(["数据库端口配置甲 1111"], inside_a,
                         "回填后 as_of 落在 A 的窗口内应只返回 A")

    def test_never_overwrites_an_existing_window(self):
        backfill = self._script()
        a, b = self._supersede_pair()   # 写入侧已经闭合了窗口（T_B）
        with self.ledger._connect(write=True) as conn:
            conn.execute("UPDATE memory_claims SET valid_until='2026-04-01T00:00:00+00:00' "
                         "WHERE memory_id=?", (a.memory_id,))
        conn = backfill.open_readonly(Path(self.ledger.path))
        self.addCleanup(conn.close)
        self.assertEqual([], backfill.scan(conn),
                         "已有窗口的 claim 不该出现在候选里")


if __name__ == "__main__":
    unittest.main()
