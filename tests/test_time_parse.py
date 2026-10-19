"""时间短语解析测试：单元模式匹配 + Context Pack 端到端过滤。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
from hippomind.ledger import SQLiteLedger
from hippomind.time_parse import parse_temporal_expression

NOW = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone.utc)  # 周五


class TimeParseUnitTests(unittest.TestCase):
    def test_yesterday(self):
        start, end = parse_temporal_expression("昨天的部署进展", now=NOW)
        self.assertEqual(start, "2026-08-20T00:00:00+00:00")
        self.assertTrue(end.startswith("2026-08-20T23:59:59"))

    def test_today(self):
        start, end = parse_temporal_expression("今天上线了什么", now=NOW)
        self.assertEqual(start, "2026-08-21T00:00:00+00:00")

    def test_day_before_yesterday(self):
        start, _ = parse_temporal_expression("前天的评审", now=NOW)
        self.assertEqual(start, "2026-08-19T00:00:00+00:00")

    def test_last_week(self):
        start, end = parse_temporal_expression("上周的发布记录", now=NOW)
        # NOW 是周五：本周一 = 08-17，上周一 = 08-10
        self.assertEqual(start, "2026-08-10T00:00:00+00:00")
        self.assertTrue(end.startswith("2026-08-16T23:59:59"))

    def test_this_week(self):
        start, end = parse_temporal_expression("本周的变更", now=NOW)
        self.assertEqual(start, "2026-08-17T00:00:00+00:00")
        self.assertEqual(end, NOW.isoformat())

    def test_recent_days_numeric_and_chinese(self):
        start, end = parse_temporal_expression("最近3天的事故", now=NOW)
        self.assertEqual(start, "2026-08-18T15:00:00+00:00")
        self.assertEqual(end, NOW.isoformat())
        start2, _ = parse_temporal_expression("近三天的新增用户", now=NOW)
        self.assertEqual(start2, "2026-08-18T15:00:00+00:00")

    def test_last_month(self):
        start, end = parse_temporal_expression("上个月的账单", now=NOW)
        self.assertEqual(start, "2026-07-01T00:00:00+00:00")
        self.assertTrue(end.startswith("2026-07-31T23:59:59"))

    def test_recent_week_and_month(self):
        start, _ = parse_temporal_expression("最近一周的告警", now=NOW)
        self.assertEqual(start, "2026-08-14T15:00:00+00:00")
        start2, _ = parse_temporal_expression("最近一个月的成本", now=NOW)
        self.assertEqual(start2, "2026-07-22T15:00:00+00:00")

    def test_no_temporal_phrase_returns_empty(self):
        self.assertEqual(parse_temporal_expression("部署进展如何", now=NOW), ("", ""))
        self.assertEqual(parse_temporal_expression("", now=NOW), ("", ""))


class TimeParseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = SQLiteLedger(Path(self.tmp.name) / "hub.sqlite")
        real_now = datetime.now(timezone.utc)
        yesterday = (real_now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        # 「上周」解析为上一个日历周（周一起算）。固定取上周三正午，
        # 无论测试在哪一天运行都必然落在该区间内（now-8d 在周一会
        # 掉到上上周日，导致时间依赖性失败）。
        prev_week_wed = (real_now - timedelta(days=real_now.weekday())
                         - timedelta(days=5)).replace(hour=12, minute=0,
                                                      second=0, microsecond=0)
        last_week = prev_week_wed.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        self.ledger.ingest({"kind": "progress", "scope": "shared",
                            "source_agent": "eval",
                            "content": "昨日要点：完成部署流水线优化",
                            "evidence": ["e:d1"], "timestamp": yesterday})
        self.ledger.ingest({"kind": "progress", "scope": "shared",
                            "source_agent": "eval",
                            "content": "上周要点：发布 v1.2 版本",
                            "evidence": ["e:w1"], "timestamp": last_week})
        self.builder = ContextPackBuilder(self.ledger)

    def tearDown(self):
        self.tmp.cleanup()

    def test_yesterday_query_filters_to_recent_record(self):
        pack = self.builder.build("昨天的流水线进展", ContextPackOptions(max_items=8))
        text = str(pack.get("text", ""))
        self.assertIn("流水线", text)
        self.assertNotIn("v1.2", text)

    def test_last_week_query_filters_to_older_record(self):
        pack = self.builder.build("上周的 发布 记录", ContextPackOptions(max_items=8))
        text = str(pack.get("text", ""))
        self.assertIn("v1.2", text)
        self.assertNotIn("流水线", text)


class EnglishTemporalTests(unittest.TestCase):
    """迭代 46：英文时间短语解析。"""

    def _parse(self, q):
        from hippomind.time_parse import parse_temporal_expression
        return parse_temporal_expression(
            q, now=datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc))

    def test_yesterday(self):
        f, t = self._parse("what did we deploy yesterday")
        self.assertEqual(f[:10], "2026-08-20")
        self.assertEqual(t[:10], "2026-08-20")  # 昨天整天，不含今天

    def test_last_week(self):
        f, t = self._parse("incident postmortem last week")
        self.assertEqual(f[:10], "2026-08-10")
        self.assertEqual(t[:10], "2026-08-16")

    def test_last_n_days_variants(self):
        for q in ("errors in the last 3 days", "errors last 3 days",
                  "errors past 3 days"):
            f, t = self._parse(q)
            self.assertEqual(f[:10], "2026-08-18", q)

    def test_this_month_and_no_phrase(self):
        f, t = self._parse("backup completed this month")
        self.assertEqual(f[:10], "2026-08-01")
        self.assertEqual(self._parse("no phrase here"), ("", ""))


if __name__ == "__main__":
    unittest.main()
