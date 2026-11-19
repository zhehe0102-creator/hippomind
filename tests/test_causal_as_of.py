"""WP-H：因果祖先的 as_of 历史切片（点对点复现与兼容性锁定）。

背景
----
causal_ancestors 原来只用 utc_now() 求值祖先记忆的 valid_from/valid_until：
它能回答「现在哪些祖先可见」，答不了「as_of 当时哪些祖先可见」。历史包因此
两种错法都有：

* 未来泄漏：as_of 之后才生效的祖先（valid_from > as_of）被塞进历史包；
* 历史欠收：as_of 当时有效、现在已 superseded/expired 的祖先被当前态
  lifecycle/quality 条件直接排除。

WP-H 给它加可选 as_of，规则对齐 ledger.search 的 as_of 分支：

* 窗口按 as_of 求值：valid_from<=as_of、valid_until 为空或 >as_of；
* 可见性放宽：quality ∈ {accepted, superseded}，lifecycle ∈
  {active, cold, expired, superseded}；quarantine/conflict 与时间无关，
  任何 as_of 下都不返回；
* as_of 为空 = 现在：SQL、参数与两个 utc_now() 求值点与改造前逐字节相同，
  既有调用方的结果/排序/得分零变化。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import LedgerError, SQLiteLedger  # noqa: E402


def _iso(offset: timedelta) -> str:
    return (datetime.now(timezone.utc) + offset).isoformat()


class CausalAsOfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-causal-asof-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        self.addCleanup(self.ledger.close)
        # 时间轴（相对 now，避免依赖运行日期）：
        #   AS_OF  = now-5d
        #   OLD    = now-10d 生效，一直有效
        #   RECENT = now-1h  生效（as_of 之后才出现 -> 历史包不得泄漏）
        #   FUTURE = now+3d  生效（当前与历史都不可见）
        self.as_of = _iso(timedelta(days=-5))
        self.old_from = _iso(timedelta(days=-10))
        self.recent_from = _iso(timedelta(hours=-1))
        self.future_from = _iso(timedelta(days=3))

    # -- fixtures ---------------------------------------------------------
    def ingest(self, content, *, valid_from, valid_until="", kind="fact",
               scope="shared", raw_chat=False, causal_parents=None,
               event_id=""):
        record = {
            "kind": kind, "scope": scope, "source_agent": "tester",
            "content": content, "evidence": ["e:" + content[:12]],
            "timestamp": valid_from, "valid_from": valid_from,
            "event_id": event_id or ("ev:" + content[:20]),
            "raw_chat": raw_chat,
        }
        if valid_until:
            record["valid_until"] = valid_until
        if causal_parents:
            record["causal_parents"] = causal_parents
        return self.ledger.ingest(record)

    def seed(self, parents, *, content="因果链子节点"):
        return self.ingest(
            content, kind="fact", valid_from=self.old_from,
            causal_parents=[{"memory_id": memory.memory_id,
                             "relation_type": "caused_by"}
                            for memory in parents])

    def ids(self, rows):
        return {row["memory_id"] for row in rows}

    # -- tests ------------------------------------------------------------
    def test_past_as_of_only_returns_ancestors_visible_then(self):
        old = self.ingest("历史祖先 甲", valid_from=self.old_from)
        recent = self.ingest("新近祖先 乙", valid_from=self.recent_from)
        future = self.ingest("未来祖先 丙", valid_from=self.future_from)
        seed = self.seed([old, recent, future])

        past = self.ledger.causal_ancestors([seed.memory_id], as_of=self.as_of)
        self.assertEqual({old.memory_id}, self.ids(past),
                         "as_of 过去只应看见当时已生效的祖先"

                         )
        self.assertNotIn(recent.memory_id, self.ids(past),
                         "as_of 之后才生效的祖先泄漏进了历史切片")
        self.assertNotIn(future.memory_id, self.ids(past))

        current = self.ledger.causal_ancestors([seed.memory_id])
        self.assertEqual({old.memory_id, recent.memory_id}, self.ids(current))
        self.assertNotIn(future.memory_id, self.ids(current),
                         "当前视图也不该包含未生效的祖先")

    def test_as_of_after_window_end_excludes_expired_window(self):
        closed = self.ingest("已闭幕祖先 丁", valid_from=self.old_from,
                             valid_until=_iso(timedelta(days=-2)))
        seed = self.seed([closed])

        past = self.ledger.causal_ancestors([seed.memory_id], as_of=self.as_of)
        self.assertEqual({closed.memory_id}, self.ids(past),
                         "窗口包含 as_of 的祖先必须可见")

        current = self.ledger.causal_ancestors([seed.memory_id])
        self.assertNotIn(closed.memory_id, self.ids(current),
                         "当前窗口已闭合的祖先不得出现在当前视图")

    def test_as_of_recovers_ancestor_superseded_at_current_time(self):
        """历史切片要能看见「当时成立、现在被裁决取代」的祖先。"""
        superseded = self.ingest(
            "被取代的历史祖先 戊", valid_from=self.old_from,
            valid_until=_iso(timedelta(days=-2)))
        with self.ledger._connect(write=True) as conn:
            conn.execute(
                "UPDATE memories SET lifecycle='superseded',"
                "quality_state='superseded' WHERE memory_id=?",
                (superseded.memory_id,))
        seed = self.seed([superseded])

        past = self.ledger.causal_ancestors([seed.memory_id], as_of=self.as_of)
        self.assertEqual({superseded.memory_id}, self.ids(past),
                         "as_of 当时有效的 superseded 祖先必须能作为历史证据返回")
        row = past[0]
        self.assertEqual("superseded", row["quality_state"])
        self.assertEqual("superseded", row["lifecycle"])

        self.assertEqual([], self.ledger.causal_ancestors([seed.memory_id]),
                         "当前视图不得复活 superseded 祖先")

    def test_quality_verdicts_never_visible_at_any_as_of(self):
        """quarantine/conflict 是质量裁决，历史切片不放宽（与 search 一致）。"""
        quarantine = self.ingest("隔离祖先 己", valid_from=self.old_from)
        conflict = self.ingest("冲突祖先 庚", valid_from=self.old_from)
        with self.ledger._connect(write=True) as conn:
            conn.execute(
                "UPDATE memories SET quality_state='quarantine' WHERE memory_id=?",
                (quarantine.memory_id,))
            conn.execute(
                "UPDATE memories SET quality_state='conflict' WHERE memory_id=?",
                (conflict.memory_id,))
        seed = self.seed([quarantine, conflict])

        self.assertEqual(
            [], self.ledger.causal_ancestors([seed.memory_id], as_of=self.as_of),
            "as_of 把 quarantine/conflict 祖先捞回来了")
        self.assertEqual([], self.ledger.causal_ancestors([seed.memory_id]))

    def test_current_view_is_unchanged_without_as_of(self):
        """无 as_of 与 as_of="" 必须逐字节等价（既有调用零行为变化）。"""
        old = self.ingest("兼容祖先 辛", valid_from=self.old_from)
        recent = self.ingest("兼容祖先 壬", valid_from=self.recent_from)
        future = self.ingest("兼容祖先 癸", valid_from=self.future_from)
        seed = self.seed([old, recent, future])

        default = self.ledger.causal_ancestors([seed.memory_id])
        explicit_empty = self.ledger.causal_ancestors([seed.memory_id], as_of="")
        self.assertEqual(default, explicit_empty,
                         "as_of='' 与不传 as_of 的结果必须完全一致")
        self.assertEqual({old.memory_id, recent.memory_id}, self.ids(default))
        # 旧契约的得分/字段形状逐项锁定（改造前 related 边 depth=1 恒 0.45）。
        for row in default:
            self.assertEqual(1, row["causal_depth"])
            self.assertEqual(0.45, row["score"])
            self.assertEqual({"causal": 0.45, "relation_weight": 1.0},
                             row["score_parts"])
            self.assertEqual("caused_by", row["relation_type"])

    def test_as_of_preserves_depth_order_and_score_contract(self):
        grand = self.ingest("远端历史祖先 子", valid_from=self.old_from)
        mid = self.ingest("近端历史祖先 丑", valid_from=self.old_from,
                          causal_parents=[{"memory_id": grand.memory_id,
                                           "relation_type": "caused_by"}])
        seed = self.seed([mid])
        rows = self.ledger.causal_ancestors([seed.memory_id], max_depth=3,
                                            as_of=self.as_of)
        self.assertEqual({mid.memory_id, grand.memory_id}, self.ids(rows))
        by_depth = {row["causal_depth"]: row for row in rows}
        self.assertEqual(0.45, by_depth[1]["score"])
        self.assertAlmostEqual(0.37, by_depth[2]["score"], places=6)
        self.assertEqual([1, 2], [row["causal_depth"] for row in rows],
                         "结果仍按 causal_depth 升序")

    def test_as_of_keeps_scope_evidence_and_raw_contract(self):
        visible = self.ingest("本域祖先 寅", valid_from=self.old_from)
        other_scope = self.ingest("外域祖先 卯", valid_from=self.old_from,
                                  scope="agent:other")
        raw = self.ingest("原始会话祖先 辰", valid_from=self.old_from,
                          kind="raw_chat", raw_chat=True)
        seed = self.seed([visible, other_scope, raw])

        rows = self.ledger.causal_ancestors(
            [seed.memory_id], scope="shared", require_evidence=True,
            include_raw_chat=False, as_of=self.as_of)
        self.assertEqual({visible.memory_id}, self.ids(rows),
                         "as_of 分支不得绕过 scope/evidence/raw 过滤契约")

    def test_as_of_accepts_z_suffix_and_rejects_garbage(self):
        old = self.ingest("时区祖先 巳", valid_from=self.old_from)
        seed = self.seed([old])
        zulu = self.as_of.replace("+00:00", "Z")
        self.assertEqual({old.memory_id},
                         self.ids(self.ledger.causal_ancestors(
                             [seed.memory_id], as_of=zulu)),
                         "带 Z 后缀的 as_of 必须与 +00:00 等价")
        with self.assertRaises(LedgerError):
            self.ledger.causal_ancestors([seed.memory_id], as_of="not-a-date")

    def test_empty_seed_list_short_circuits_with_as_of(self):
        self.assertEqual([],
                         self.ledger.causal_ancestors([], as_of=self.as_of))


if __name__ == "__main__":
    unittest.main()

