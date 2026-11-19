"""中文时间短语解析：把自然语言时间范围转成账本可用的 from/to 过滤。

设计原则：
* 只做确定性规则匹配，不猜测语义；未命中返回 ("", "")，调用方保持原行为。
* 精确日（昨天/前天）优先于相对区间（最近N天）；周/月命名短语优先于数字天。
* 全部基于 UTC；调用方传入 ``now`` 可冻结时钟做确定性测试。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_Handler = Callable[[datetime], Tuple[datetime, datetime]]


def _num(value: str) -> int:
    value = (value or "").strip()
    if value.isdigit():
        return max(1, int(value))
    if "十" in value:
        parts = value.split("十", 1)
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if parts[1] else 0
        return max(1, tens * 10 + ones)
    total = 0
    for ch in value:
        total += _CN_NUM.get(ch, 0)
    return max(1, total)


def _day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _day_end(dt: datetime) -> datetime:
    return _day_start(dt) + timedelta(days=1) - timedelta(microseconds=1)


def _week_start(dt: datetime) -> datetime:
    return _day_start(dt - timedelta(days=dt.weekday()))


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _relative_days(count: str) -> _Handler:
    def handler(now: datetime) -> Tuple[datetime, datetime]:
        return (now - timedelta(days=_num(count)), now)
    return handler


# 顺序即优先级：精确日 > 命名周月 > 数字天数。
# handler 为 None 的条目表示「天数取自第 1 个捕获组」。
_PATTERNS: List[Tuple[re.Pattern, Optional[_Handler]]] = [
    (re.compile(r"前天"),
     lambda n: (_day_start(n - timedelta(days=2)), _day_end(n - timedelta(days=2)))),
    (re.compile(r"昨天|昨日"),
     lambda n: (_day_start(n - timedelta(days=1)), _day_end(n - timedelta(days=1)))),
    (re.compile(r"今天|今日"), lambda n: (_day_start(n), _day_end(n))),
    (re.compile(r"上个月|上月"),
     lambda n: (_month_start(_month_start(n) - timedelta(microseconds=1)),
                _month_start(n) - timedelta(microseconds=1))),
    (re.compile(r"本月|这个月"), lambda n: (_month_start(n), n)),
    (re.compile(r"上周"),
     lambda n: (_week_start(n) - timedelta(days=7),
                _week_start(n) - timedelta(microseconds=1))),
    (re.compile(r"本周|这周"), lambda n: (_week_start(n), n)),
    (re.compile(r"最近(一个)?月|近(一个)?月|(一个)?月内"),
     _relative_days("30")),
    (re.compile(r"最近半(个)?月|近半(个)?月"), _relative_days("15")),
    (re.compile(r"最近一?周|近一?周|一周内"), _relative_days("7")),
    (re.compile(r"(?:(?:最近|近|过去)\s*(\d+|[一二两三四五六七八九十]+)\s*天(?:之?内|前|来)?|"
                r"(?<![第每])\b(\d+|[一二两三四五六七八九十]+)\s*天(?:之?内|前|来))"),
     None),
]


_EN_PATTERNS: List[Tuple[re.Pattern, _Handler]] = [
    (re.compile(r"\bday before yesterday\b"),
     lambda n: (_day_start(n - timedelta(days=2)), _day_end(n - timedelta(days=2)))),
    (re.compile(r"\byesterday\b"),
     lambda n: (_day_start(n - timedelta(days=1)), _day_end(n - timedelta(days=1)))),
    (re.compile(r"\btoday\b"), lambda n: (_day_start(n), _day_end(n))),
    (re.compile(r"\blast month\b"),
     lambda n: (_month_start(_month_start(n) - timedelta(microseconds=1)),
                _month_start(n) - timedelta(microseconds=1))),
    (re.compile(r"\bthis month\b"), lambda n: (_month_start(n), n)),
    (re.compile(r"\blast week\b"),
     lambda n: (_week_start(n) - timedelta(days=7),
                _week_start(n) - timedelta(microseconds=1))),
    (re.compile(r"\bthis week\b"), lambda n: (_week_start(n), n)),
    (re.compile(r"\bin the last (\d+) days?\b|\blast (\d+) days?\b"
                r"|\bpast (\d+) days?\b"), None),
]


def parse_temporal_expression(query: str, *,
                              now: Optional[datetime] = None) -> Tuple[str, str]:
    """解析查询中的中文时间短语，返回 ``(from_iso, to_iso)``。

    未命中任何短语时返回 ``("", "")``——调用方应保持原有不过滤行为。
    """
    text = str(query or "")
    if not text:
        return "", ""
    moment = now if now is not None else datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    for compiled, handler in _PATTERNS:
        match = compiled.search(text)
        if match is None:
            continue
        if handler is None:
            days = next((g for g in match.groups() if g), "1")
            handler = _relative_days(days)
        start, end = handler(moment)
        if end < start:
            start, end = end, start
        return start.isoformat(), end.isoformat()
    for compiled, handler in _EN_PATTERNS:
        match = compiled.search(text.lower())
        if match is None:
            continue
        if handler is None:
            days = next(g for g in match.groups() if g)
            handler = _relative_days(days)
        start, end = handler(moment)
        if end < start:
            start, end = end, start
        return start.isoformat(), end.isoformat()
    return "", ""
