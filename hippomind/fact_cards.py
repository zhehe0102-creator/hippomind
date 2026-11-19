"""事实卡（fact cards）原型：把每个 claim slot 的当前事实与变更历史压成卡片。

背景（P1-4）
----------
注入链路目前把每条记忆的正文当候选：同一条 slot 的多版事实（旧值已被 supersede、
新值 active）在召回里表现为互不相关的碎片；历史版本要么占用候选预算，要么在热
索引外彻底看不见。事实卡把同一 `(subject, predicate, slot_key)` 的全部 claim 聚合
成一张卡：**当前值 + 有界变更历史**，一次注入就能回答「现在是什么」和「改过没有」。

WP-Q 起本模块已以 opt-in 方式接入 context_pack：默认关闭（HIPPOMIND_FACT_CARDS
未开启且 ContextPackOptions.fact_cards=False 时 pack 输出与旧版本逐字节相同），
开启后在已选分组之后前置一张有界事实卡块。

双时态（as_of）
----------------
as_of 为空 = 现在：只取 claim.state='active' 且记忆 accepted+active、窗口覆盖现在的行
（与 search 默认路径同一可见性集合）。

as_of 非空 = 历史切片：claim.state 放宽到 active/superseded、记忆 quality 放宽到
accepted/superseded、lifecycle 放宽到 active/cold/expired/superseded（与 search 的
as_of 语义一致），再按 claim 与记忆的 valid_from/valid_until 判断 at as_of 是否可见。
quarantine/conflict 与时间无关，任何时刻都不进当前卡。

只读契约：本模块只用 `ledger._reading()`（thread-local 只读连接），不做任何写入；
调用方传普通 SQLiteLedger 或只读包装均可。
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .ledger import utc_now

DEFAULT_SCOPE = "shared"
DEFAULT_LIMIT = 64
DEFAULT_HISTORY_LIMIT = 4
DEFAULT_RENDER_CHARS = 1200

#: 每张卡 evidence 与每条 evidence 的字符上限（注入时不允许无界证据）。
EVIDENCE_LIMIT = 8
EVIDENCE_CHARS = 512
#: 卡片单值最大显示字符：运行时再按整段 max_chars 截断，这里先保证单卡紧凑。
CARD_VALUE_CHARS = 160
#: SQL 行数安全上限：正常账本里 claim 总量远低于此；超出时宁可少卡也不全表加载。
MAX_CLAIM_ROWS = 20_000
#: memory_ids 过滤的输入条数上限（context_pack 只会传已选记忆，正常 <64）。
MAX_MEMORY_IDS = 2_000

#: 自动注入路径的 opt-in 环境开关；显式 API/MCP 默认 False。
ENV_FACT_CARDS = "HIPPOMIND_FACT_CARDS"
MAX_SUBJECTS = 128

#: as_of 历史切片可见的 claim state（与 search 的 as_of 放宽一致）。
CURRENT_STATES_HISTORICAL = frozenset({"active", "superseded"})
#: 变更历史只收已经下线的旧断言；expired 兼容历史数据。
HISTORY_STATES = frozenset({"superseded", "expired"})
ACCEPTED_QUALITIES_HISTORICAL = frozenset({"accepted", "superseded"})
VISIBLE_LIFECYCLES_HISTORICAL = frozenset({"active", "cold", "expired", "superseded"})


def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lower, min(upper, number))


def _short(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit] if limit else text


def _timestamp(value: Any) -> str:
    """与 ledger._helpers._timestamp 同语义的 ISO-8601 UTC 归一化。

    只做本模块需要的子集：as_of 必须能与数据库中已归一化的 valid_* 字符串直接
    比较大小（同一 ISO-8601 UTC 格式下字典序 = 时间序）。
    """
    text = _short(value, 80)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("as_of 必须是 ISO-8601 时间或 Unix 时间戳") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _decode_value(value_json: Any) -> Any:
    """claim.value_json -> Python 值；坏 JSON 保留原始字符串而不是丢事实。"""
    if isinstance(value_json, (dict, list, int, float, bool)) or value_json is None:
        return value_json
    try:
        return json.loads(value_json)
    except (TypeError, ValueError):
        return str(value_json or "")


def _window_covers(valid_from: Any, valid_until: Any, reference: str) -> bool:
    """双时态窗口 [valid_from, valid_until) 是否覆盖 reference。

    空串语义与账本一致：valid_from 空 = 不限起点，valid_until 空 = 仍然有效。
    """
    start = _short(valid_from, 64)
    end = _short(valid_until, 64)
    if start and start > reference:
        return False
    if end and end <= reference:
        return False
    return True


def _evidence_list(evidence_json: Any) -> List[str]:
    try:
        values = json.loads(evidence_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    bounded: List[str] = []
    for item in values:
        text = _short(item, EVIDENCE_CHARS)
        if text and text not in bounded:
            bounded.append(text)
        if len(bounded) >= EVIDENCE_LIMIT:
            break
    return bounded


def fact_cards_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """自动注入路径的 opt-in 开关（默认关闭）。

    只有显式真值（1/true/yes/on，大小写不敏感）才开启；未设置或其它值一律
    关闭，保证默认路径行为零变化。显式调用方仍通过
    ContextPackOptions.fact_cards=True 逐次开启，不受环境变量影响。
    """
    source = os.environ if environ is None else environ
    try:
        raw = source.get(ENV_FACT_CARDS, "")
    except AttributeError:
        return False
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _memory_id_filter(memory_ids: Optional[Iterable[str]]) -> List[str]:
    """已选记忆 id 过滤：非空列表才生效，去重去空并做条数上限。"""
    if memory_ids is None:
        return []
    try:
        values = list(memory_ids)
    except TypeError:
        return []
    cleaned: List[str] = []
    seen = set()
    for item in values:
        text = _short(item, 320)
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
        if len(cleaned) >= MAX_MEMORY_IDS:
            break
    return cleaned


def _subject_filter(subjects: Optional[Iterable[str]]) -> List[str]:
    if subjects is None:
        return []
    try:
        values = list(subjects)
    except TypeError:
        return []
    cleaned: List[str] = []
    for item in values:
        text = _short(item, 256)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= MAX_SUBJECTS:
            break
    return cleaned


def _claim_visibility(row: Any, *, historical: bool, reference: str) -> bool:
    """一行 claim+memory 投影在该时间切片是否进入**当前**卡。"""
    state = _short(row["state"], 32)
    quality = _short(row["quality_state"], 32)
    lifecycle = _short(row["lifecycle"], 32)
    if historical:
        if state not in CURRENT_STATES_HISTORICAL:
            return False
        if quality not in ACCEPTED_QUALITIES_HISTORICAL:
            return False
        if lifecycle not in VISIBLE_LIFECYCLES_HISTORICAL:
            return False
    else:
        if state != "active" or quality != "accepted" or lifecycle != "active":
            return False
    if not _window_covers(row["valid_from"], row["valid_until"], reference):
        return False
    # 记忆投影自身的窗口也算门禁：维护按「现在」标过期，但 as_of 落在窗口内时
    # 历史切片必须仍可见（与 search 的 m.valid_from / m.valid_until 判定同源）。
    return _window_covers(row["memory_valid_from"], row["memory_valid_until"], reference)


def _card_value(value: Any, limit: int = CARD_VALUE_CHARS) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if limit and len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


def _render_card_line(card: Dict[str, Any]) -> str:
    """一卡一行：subject.slot: value（置信度 / 生效时间 / 历史标记）。"""
    subject = _card_value(card.get("subject", ""), 80)
    slot_key = _card_value(card.get("slot_key", ""), 120)
    value = _card_value(card.get("current_value"), CARD_VALUE_CHARS)
    try:
        confidence = float(card.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    valid_from = _short(card.get("valid_from"), 64)
    stamp = valid_from[:10] if len(valid_from) >= 10 else valid_from
    parts = ["%s.%s: %s（置信度 %.2f" % (subject, slot_key, value, confidence)]
    if stamp:
        parts.append(" · 生效 %s" % stamp)
    history = card.get("history") or []
    if history:
        # 历史只保留「曾被 X 个旧值取代」标记：卡片是现值视图，展开旧值会把
        # 注入预算又还回去；需要旧值审计时直接查 claim 表/历史卡。
        parts.append(" · 曾被 %d 个旧值取代" % len(history))
    parts.append("）")
    return "".join(parts)


def build_fact_cards(ledger: Any, *, scope: str = DEFAULT_SCOPE,
                     subjects: Optional[Iterable[str]] = None,
                     as_of: str = "", limit: int = DEFAULT_LIMIT,
                     history_limit: int = DEFAULT_HISTORY_LIMIT,
                     memory_ids: Optional[Iterable[str]] = None
                     ) -> List[Dict[str, Any]]:
    """按 (subject,predicate,slot_key) 聚合 claim，返回当前事实卡列表。

    每卡：::

        {"subject","predicate","slot_key","current_value","confidence",
         "source_trust","valid_from","updated_at","memory_id","evidence",
         "history":[{"value","valid_from","valid_until","state","memory_id"}]}

    * 当前 claim 只取可见集（见模块 docstring 的 as_of 契约）；同 slot 若出现
      多条可见 claim（仲裁残留/历史数据），按 confidence、asserted_at、claim_id
      取确定性最优的一条，其余不进历史（它们不是「被取代」的旧版本）；
    * history 取同组 state in {superseded, expired} 的行（排除当前 claim），按
      valid_from 倒序，最多 history_limit 条；as_of 切片里 valid_from 在 as_of
      之后的「未来旧版本」不泄漏进来；
    * memory_ids 非空时，「当前卡」只从这些 memory_id 的 claim 行产生（用于只给
      已选记忆出卡）；history 仍按整条 slot 聚合，可以来自其它记忆——否则
      「旧版本」会随着旧记忆一起丢失；
    * 只读：仅通过 ledger._reading() 读，不写入。
    """
    reader = getattr(ledger, "_reading", None)
    if not callable(reader):
        raise TypeError("build_fact_cards 需要提供 _reading() 的账本对象")
    scope_text = _short(scope, 128) or DEFAULT_SCOPE
    reference = _timestamp(as_of) if str(as_of or "").strip() else utc_now()
    historical = bool(str(as_of or "").strip())
    subject_values = _subject_filter(subjects)
    memory_values = _memory_id_filter(memory_ids)
    memory_set = set(memory_values)
    card_limit = _bounded_int(limit, DEFAULT_LIMIT, 0, 10_000)
    per_card_history = _bounded_int(history_limit, DEFAULT_HISTORY_LIMIT, 0, 64)
    if card_limit <= 0:
        return []
    sql = (
        "SELECT c.claim_id,c.memory_id,c.subject,c.predicate,c.slot_key,"
        "c.value_json,c.valid_from,c.valid_until,c.asserted_at,c.confidence,"
        "c.source_trust,c.state,m.lifecycle,m.quality_state,m.updated_at,"
        "m.evidence_json,m.valid_from AS memory_valid_from,"
        "m.valid_until AS memory_valid_until "
        "FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id "
        "WHERE c.scope=? AND m.scope=?")
    params: List[Any] = [scope_text, scope_text]
    if subject_values:
        sql += " AND c.subject IN (" + ",".join("?" for _ in subject_values) + ")"
        params.extend(subject_values)
    rows: List[Any] = []
    with reader() as conn:
        if memory_values and not subject_values:
            unique_subjects: List[str] = []
            seen_subj = set()
            for start in range(0, len(memory_values), 400):
                chunk = memory_values[start:start + 400]
                marks = ",".join("?" for _ in chunk)
                for srow in conn.execute(
                    "SELECT DISTINCT subject FROM memory_claims "
                    "WHERE scope=? AND memory_id IN (" + marks + ")",
                    (scope_text, *chunk),
                ).fetchall():
                    subj = str(srow[0] or "")
                    if subj and subj not in seen_subj:
                        seen_subj.add(subj)
                        unique_subjects.append(subj)
            if not unique_subjects:
                return []
            for start in range(0, len(unique_subjects), 400):
                schunk = unique_subjects[start:start + 400]
                smarks = ",".join("?" for _ in schunk)
                rows.extend(conn.execute(
                    sql + " AND c.subject IN (" + smarks + ") "
                    "ORDER BY c.subject,c.predicate,c.slot_key,c.valid_from DESC LIMIT ?",
                    (*params, *schunk, MAX_CLAIM_ROWS),
                ).fetchall())
        else:
            rows = conn.execute(
                sql + " ORDER BY c.subject,c.predicate,c.slot_key,c.valid_from DESC LIMIT ?",
                (*params, MAX_CLAIM_ROWS),
            ).fetchall()
    groups: Dict[Any, List[Any]] = {}
    for row in rows:
        key = (_short(row["subject"], 256), _short(row["predicate"], 256),
               _short(row["slot_key"], 256))
        if not key[0] or not key[1] or not key[2]:
            continue
        groups.setdefault(key, []).append(row)
    cards: List[Dict[str, Any]] = []
    for key in sorted(groups):
        group_rows = groups[key]
        visible = [row for row in group_rows
                   if _claim_visibility(row, historical=historical, reference=reference)]
        if memory_set:
            # 当前卡只认调用方已选的记忆；history 仍然看整组，保留旧版本线索。
            visible = [row for row in visible
                       if str(row["memory_id"] or "") in memory_set]
        if not visible:
            continue
        current = max(visible, key=lambda row: (
            float(row["confidence"] or 0.0), str(row["asserted_at"] or ""),
            str(row["claim_id"] or "")))
        history_rows = [row for row in group_rows
                        if str(row["state"] or "") in HISTORY_STATES
                        and str(row["claim_id"] or "") != str(current["claim_id"] or "")
                        and str(row["quality_state"] or "") != "quarantine"]
        if historical:
            history_rows = [row for row in history_rows
                            if _window_covers(row["valid_from"], "", reference)
                            and str(row["valid_until"] or "").strip()
                            and str(row["valid_until"] or "").strip() <= reference]
        history_rows.sort(key=lambda row: (
            str(row["valid_from"] or ""), str(row["asserted_at"] or ""),
            str(row["claim_id"] or "")), reverse=True)
        if per_card_history:
            history_rows = history_rows[:per_card_history]
        else:
            history_rows = []
        cards.append({
            "subject": key[0],
            "predicate": key[1],
            "slot_key": key[2],
            "current_value": _decode_value(current["value_json"]),
            "confidence": float(current["confidence"] or 0.0),
            "source_trust": float(current["source_trust"] or 0.0),
            "valid_from": _short(current["valid_from"], 64),
            "updated_at": _short(current["updated_at"], 64),
            "memory_id": _short(current["memory_id"], 320),
            "evidence": _evidence_list(current["evidence_json"]),
            "history": [{
                "value": _decode_value(row["value_json"]),
                "valid_from": _short(row["valid_from"], 64),
                "valid_until": _short(row["valid_until"], 64),
                "state": str(row["state"] or ""),
                "memory_id": _short(row["memory_id"], 320),
            } for row in history_rows],
        })
        if len(cards) >= card_limit:
            break
    return cards


def render_fact_cards(cards: Sequence[Dict[str, Any]],
                      *, max_chars: int = DEFAULT_RENDER_CHARS) -> str:
    """把事实卡渲染成紧凑注入文本，**严格**不超过 max_chars 字符。

    每卡一行；历史只显示「曾被 N 个旧值取代」标记，不展开旧值。放不下的卡整张
    丢弃并追加「…（已截断 N 张卡）」标注；标注本身也纳入上限（极端小的 max_chars
    下硬切片，仍保证长度 <= max_chars）。
    """
    limit = _bounded_int(max_chars, DEFAULT_RENDER_CHARS, 0, 10_000_000)
    if limit <= 0 or not cards:
        return ""
    lines: List[str] = []
    used = 0
    dropped = 0
    for index, card in enumerate(cards):
        line = _render_card_line(card)
        addition = ("\n" if lines else "") + line
        if used + len(addition) > limit:
            dropped = len(cards) - index
            break
        lines.append(line)
        used += len(addition)
    if not dropped:
        return "\n".join(lines)
    note = "…（已截断 %d 张卡）" % dropped
    while lines:
        candidate = "\n".join(lines + [note])
        if len(candidate) <= limit:
            return candidate
        lines.pop()
        dropped += 1
        note = "…（已截断 %d 张卡）" % dropped
    return note[:limit]


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 按 1 字 1 token，其余按 4 字符 1 token。"""
    text = str(text or "")
    cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    other = len(text) - cjk
    return int(math.ceil(cjk + other / 4.0))


def card_coverage_stats(cards: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """卡片覆盖统计：分组数 / 有历史卡数 / 平均 token（供后续 A/B 量化）。"""
    items = list(cards or [])
    if not items:
        return {
            "cards": 0, "groups": 0, "subject_count": 0,
            "with_history": 0, "history_items": 0,
            "rendered_chars": 0, "avg_tokens": 0.0, "max_tokens": 0,
        }
    subjects = set()
    with_history = 0
    history_items = 0
    rendered_chars = 0
    tokens: List[int] = []
    for card in items:
        subjects.add(str(card.get("subject") or ""))
        history = card.get("history") or []
        history_items += len(history)
        if history:
            with_history += 1
        line = _render_card_line(card)
        rendered_chars += len(line)
        tokens.append(_estimate_tokens(line))
    return {
        "cards": len(items),
        "groups": len(items),
        "subject_count": len(subjects),
        "with_history": with_history,
        "history_items": history_items,
        "rendered_chars": rendered_chars,
        "avg_tokens": round(sum(tokens) / float(len(tokens)), 2),
        "max_tokens": max(tokens),
    }


__all__ = [
    "DEFAULT_HISTORY_LIMIT", "DEFAULT_LIMIT", "DEFAULT_RENDER_CHARS",
    "DEFAULT_SCOPE", "ENV_FACT_CARDS", "MAX_MEMORY_IDS", "build_fact_cards",
    "card_coverage_stats", "fact_cards_enabled", "render_fact_cards",
]
