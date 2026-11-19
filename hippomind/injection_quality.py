"""Did the injected context actually get used - and can we even tell?

An injection trace proves a pack was *offered*, not that it helped.  This
module closes the loop offline, but it is deliberately conservative about what
that loop is allowed to claim.

What is measured
----------------
For every trace entry that recorded memory ids, the same agent's assistant
messages inside a short window are collected and compared against the injected
memories on four axes:

overlap         Token coverage: what share of the injected memory vocabulary
                reappears at all (the original proxy, kept for continuity).
citation        IDF-weighted coverage of *distinctive* signals (paths, file
                names, identifiers, versions, commands, quoted names).  A rare
                token that reappears is far stronger evidence than a common
                word, so common tokens are down-weighted by a background
                corpus built from the ledger's own assistant messages.
paraphrase      Best semantic cosine between an injected memory and a reply,
                using the ledger-native LSA model when one is installed.
correction      Whether a reply both mentions an injected signal and carries a
                contradiction marker.  This is a *negative* signal: the pack
                may have been actively wrong.

Arms (since the claude hook, 2026-10-25)
--------------------------------------
Traces may carry arm (treatment/control).  The claude hook runs a
*matched* randomised experiment: both arms retrieve, both record the memory ids
that were offered, and only delivery differs.  When both arms have enough
evaluable traces, the report compares them directly instead of relying on the
post-hoc "matched but not injected" control, which is still computed for older
traces that have no arm.

The distinction matters for what may be said.  The post-hoc control answers
"do injected memories get reused more than similar non-injected ones?" - an
association.  The arm comparison answers "does delivering the pack change reuse,
compared with not delivering the same pack?" - still not a proof, but it holds
the candidate set fixed, which is the part that was previously unknowable.

What is NOT claimed
-------------------
None of this is causal.  An agent can reuse a term it already knew, the reply
window may contain unrelated work, and the "no injection" arm usually cannot be
constructed at all because the hook only writes a trace when it injects
something.  The module therefore:

* always reports the sample size next to every ratio;
* refuses to state a conclusion below MIN_SAMPLES evaluable traces;
* builds a matched "not injected" control arm when it can, and labels that
  comparison observational rather than causal;
* reports Wilson intervals next to point estimates;
* records *why* each trace could not be evaluated, so a zero sample is
  explainable rather than mysterious.

A clean "insufficient evidence" is the correct output far more often than a
number that looks like a result.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .injection_trace import recent
from .schema import fts_indexed_predicate
from .textutil import is_noise_text, tokenize_multilingual

WINDOW_MINUTES = 30
MIN_TERM_OVERLAP = 0.15

#: Minimum evaluable traces before any aggregate is reported as a conclusion.
#: 30 is the conventional floor for a proportion to have a 95% half-width
#: below ~0.2; it is a *reporting* gate, not a statistical proof.
MIN_SAMPLES = 30

#: A trace counts as "cited" when this share of its IDF-weighted signals
#: reappears in the reply window.
MIN_CITATION_RATE = 0.20

#: Bounded background corpus used to weight signal rarity.
BACKGROUND_DOCS = 1200
BACKGROUND_CHARS = 400

#: Per-trace reply scan cap (unchanged from the original implementation).
MAX_REPLIES = 20

#: Per-trace matched control candidates.
MAX_CONTROL = 8

#: Detail rows retained in the report payload.
MAX_DETAIL = 50

#: Backtick, spelled out so this source has no markdown ambiguity.
_TICK = "\x60"


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------

_ISO_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse a trace/ledger timestamp into an aware UTC datetime.

    injection_trace writes %z offsets without a colon
    (2026-10-24T04:02:05-0700), which datetime.fromisoformat rejects on
    Python 3.9.  Silently falling back to "now" - as the first version did -
    puts the reply window at the wrong time, so an unparseable timestamp is
    reported instead of guessed.
    """
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    text = _ISO_OFFSET_RE.sub(r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Distinctive signal extraction
# ---------------------------------------------------------------------------

#: Machine-checkable tokens.  Ordinary prose is deliberately *not* a signal:
#: a shared Chinese bigram means almost nothing, a shared file path means a lot.
_SIGNAL_PATTERNS: Tuple[Tuple[str, Any], ...] = (
    ("code", re.compile(_TICK + r"([^" + _TICK + r"\n]{2,60})" + _TICK)),
    ("url", re.compile(r"https?://[^\s)\]>'" + _TICK + r"]+")),
    ("path", re.compile(r"(?<![\w/])(?:[\w.+-]+/)+[\w.+-]+")),
    ("file", re.compile(
        r"(?<![\w.-])[\w-]{1,60}\.(?:py|js|mjs|ts|tsx|jsx|json|jsonl|md|sqlite|db|sh|bash"
        r"|yml|yaml|toml|ini|html|css|rs|go|java|kt|swift|onnx|npz|log|txt|csv|pdf|patch|diff)\b")),
    ("version", re.compile(r"\b[vV]?\d+(?:\.\d+){1,3}\b")),
    ("quoted", re.compile(r"[《「『]([^》」』\n]{2,40})[》」』]")),
    ("ident", re.compile(
        r"\b(?:[A-Z][a-z0-9]+){2,}\b"
        r"|\b[a-zA-Z_][a-zA-Z0-9]*_[a-zA-Z0-9_]+\b"
        r"|\b[A-Z]{2,}[A-Z0-9_]*\b")),
    ("num", re.compile(r"\b\d{3,}\b")),
)

_STRIP_CHARS = ".,;:!?()[]{}<>\"'" + _TICK


def extract_signals(text: str, *, limit: int = 4000) -> Dict[str, str]:
    """Return {lowercased token: kind} of distinctive tokens in text."""
    payload = str(text or "")[:max(1, int(limit))]
    found: Dict[str, str] = {}
    for kind, pattern in _SIGNAL_PATTERNS:
        for match in pattern.finditer(payload):
            token = match.group(1) if match.groups() else match.group(0)
            token = str(token or "").strip().strip(_STRIP_CHARS).strip()
            if len(token) < 3 or len(token) > 80:
                continue
            if kind == "num" and not token.isdigit():
                continue
            if kind == "version" and "." not in token:
                continue
            found.setdefault(token.lower(), kind)
    return found


def _background(conn: sqlite3.Connection, *,
                docs: int = BACKGROUND_DOCS) -> Tuple[Dict[str, int], int]:
    """Document frequency of signals over recent assistant messages."""
    try:
        rows = conn.execute(
            "SELECT substr(content,1,?) AS c FROM memories "
            "WHERE raw_chat=1 AND role='assistant' AND content!='' "
            "ORDER BY created_at DESC LIMIT ?",
            (BACKGROUND_CHARS, int(docs))).fetchall()
    except sqlite3.Error:
        return {}, 0
    df: Dict[str, int] = {}
    for row in rows:
        for key in extract_signals(str(row["c"] or "")):
            df[key] = df.get(key, 0) + 1
    return df, len(rows)


def _idf(key: str, df: Mapping[str, int], docs: int) -> float:
    """Rarity weight; unseen signals weigh most."""
    if docs <= 0:
        return 1.0
    return math.log((docs + 1.0) / (df.get(key, 0) + 1.0)) + 1.0


def _weighted_hits(signals: Mapping[str, str], reply_text: str,
                   df: Mapping[str, int], docs: int) -> Tuple[float, int, float]:
    """IDF-weighted coverage of signals inside reply_text."""
    if not signals:
        return 0.0, 0, 0.0
    lowered = str(reply_text or "").lower()
    total = 0.0
    hit = 0.0
    for key in signals:
        weight = _idf(key, df, docs)
        total += weight
        if key in lowered:
            hit += weight
    if total <= 0.0:
        return 0.0, 0, 0.0
    return hit / total, len(signals), total


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def wilson(successes: int, total: int, z: float = 1.96) -> Optional[List[float]]:
    """Wilson score interval for a proportion, or None with no samples."""
    if total <= 0:
        return None
    p = max(0.0, min(1.0, successes / float(total)))
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denom
    margin = z * math.sqrt(p * (1.0 - p) / total + z * z / (4.0 * total * total)) / denom
    return [round(max(0.0, centre - margin), 3), round(min(1.0, centre + margin), 3)]


def _mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 3) if values else None


# ---------------------------------------------------------------------------
# Reply window
# ---------------------------------------------------------------------------

_HARNESS_NOISE = (
    re.compile(r"^\s*API Error", re.IGNORECASE),
    re.compile(r"^\s*\{\s*\"claims\"\s*:"),
    re.compile(r"^\s*\[Request interrupted", re.IGNORECASE),
)


def _usable_reply(text: str) -> bool:
    """Drop harness chatter, structured probe replies and near-empty turns."""
    probe = str(text or "").strip()
    if not probe:
        return False
    if any(pattern.match(probe) for pattern in _HARNESS_NOISE):
        return False
    return not is_noise_text(probe)


def _replies(conn: sqlite3.Connection, agent: str, start: datetime,
             end: datetime) -> Tuple[List[str], int]:
    """Assistant messages for agent in [start, end]: (usable, raw)."""
    try:
        rows = conn.execute(
            "SELECT content FROM memories WHERE raw_chat=1 AND role='assistant' "
            "AND source_agent=? AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (agent, start.isoformat(), end.isoformat(), MAX_REPLIES)).fetchall()
    except sqlite3.Error:
        return [], 0
    texts = [str(row["content"] or "") for row in rows]
    return [text for text in texts if _usable_reply(text)], len(texts)


# ---------------------------------------------------------------------------
# Matched control arm
# ---------------------------------------------------------------------------

#: The control arm samples exactly what the recall path can serve, so it uses
#: the single shared FTS predicate rather than restating the rule.
_CONTROL_SQL = (
    "SELECT memory_id, kind, substr(content,1,?) AS c, created_at FROM memories "
    "WHERE raw_chat=0 AND " + fts_indexed_predicate() + " "
    "AND content!='' ORDER BY updated_at DESC LIMIT ?"
)


def _control_pool(conn: sqlite3.Connection, exclude: Sequence[str], *,
                  limit: int = 400) -> List[Dict[str, Any]]:
    """Recently-usable memories that were *not* injected in this trace."""
    blocked = {str(item) for item in exclude}
    try:
        rows = conn.execute(_CONTROL_SQL, (BACKGROUND_CHARS, int(limit))).fetchall()
    except sqlite3.Error:
        return []
    pool: List[Dict[str, Any]] = []
    for row in rows:
        memory_id = str(row["memory_id"] or "")
        if not memory_id or memory_id in blocked:
            continue
        pool.append({"memory_id": memory_id, "kind": str(row["kind"] or ""),
                     "content": str(row["c"] or ""),
                     "created_at": str(row["created_at"] or "")})
    return pool


def _match_controls(pool: Sequence[Mapping[str, Any]],
                    injected: Sequence[Mapping[str, Any]],
                    *, limit: int = MAX_CONTROL) -> List[Dict[str, Any]]:
    """Pair each injected memory with an unused, same-kind, time-nearest one."""
    used = set()
    matched: List[Dict[str, Any]] = []
    for item in list(injected)[:limit]:
        kind = str(item.get("kind") or "")
        when = _parse_time(item.get("created_at"))
        best = None
        best_gap = None
        for candidate in pool:
            if candidate["memory_id"] in used:
                continue
            if kind and candidate["kind"] != kind:
                continue
            gap = 0.0
            other = _parse_time(candidate.get("created_at"))
            if when is not None and other is not None:
                gap = abs((other - when).total_seconds())
            if best is None or gap < (best_gap or 0.0):
                best, best_gap = candidate, gap
        if best is None:
            continue
        used.add(best["memory_id"])
        matched.append(best)
    return matched


# ---------------------------------------------------------------------------
# Correction markers
# ---------------------------------------------------------------------------

_CORRECTION_MARKERS = (
    "不对", "不是这样", "已过时", "过期", "改为", "改回", "推翻", "纠正",
    "实际上", "错误的", "更正", "不再适用",
    "instead", "actually", "no longer", "deprecated", "obsolete", "revert",
    "wrong", "incorrect", "superseded", "outdated",
)


def _corrected(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _CORRECTION_MARKERS)


# ---------------------------------------------------------------------------
# Per-trace evaluation
# ---------------------------------------------------------------------------


def _trace_row(ledger, conn, trace: Mapping[str, Any], *, window_minutes: int,
               embedder, df: Mapping[str, int], docs: int, controls: bool,
               injected_anywhere: Sequence[str] = ()) -> Dict[str, Any]:
    at_raw = trace.get("at")
    agent = str(trace.get("agent") or "")
    memory_ids = [str(item) for item in (trace.get("memory_ids") or []) if str(item)]
    row: Dict[str, Any] = {
        "at": at_raw, "agent": agent, "source": str(trace.get("source") or ""),
        "items": int(trace.get("items") or 0), "memory_ids": len(memory_ids),
        "replies": 0, "replies_raw": 0, "overlap": 0.0, "used": False,
        "signals": 0, "citation_rate": None, "cited": False,
        "paraphrase": None, "corrected": False,
        "control_rate": None, "control_pairs": 0, "lift": None,
        "arm": str(trace.get("arm") or "") or "unknown",
        "delivered": bool(trace.get("injected", True)),
        "evaluable": False, "skip_reason": "",
    }
    if not memory_ids:
        row["skip_reason"] = "no_memory_ids"
        return row
    started = _parse_time(at_raw)
    if started is None:
        row["skip_reason"] = "bad_timestamp"
        return row
    ended = started + timedelta(minutes=max(1, int(window_minutes)))

    memories: List[Dict[str, Any]] = []
    for memory_id in memory_ids:
        try:
            memory = ledger.get_memory(memory_id)
        except Exception:
            memory = None
        if memory:
            memories.append(memory)
    if not memories:
        row["skip_reason"] = "memories_missing"
        return row

    replies, raw_count = _replies(conn, agent, started, ended)
    row["replies"] = len(replies)
    row["replies_raw"] = raw_count
    if not replies:
        row["skip_reason"] = "no_replies"
        return row

    joined = "\n".join(replies)

    # --- overlap (original proxy) -----------------------------------------
    injected_terms = set()
    for memory in memories:
        injected_terms |= tokenize_multilingual(str(memory.get("content") or ""))
    reply_terms = tokenize_multilingual(joined)
    if injected_terms:
        overlap = len(injected_terms & reply_terms) / max(1, len(injected_terms))
        row["overlap"] = round(overlap, 3)
        row["used"] = overlap >= MIN_TERM_OVERLAP

    # --- citation (IDF-weighted distinctive signals) -----------------------
    signals: Dict[str, str] = {}
    for memory in memories:
        for key, kind in extract_signals(str(memory.get("content") or "")).items():
            signals.setdefault(key, kind)
    rate, signal_count, _weight = _weighted_hits(signals, joined, df, docs)
    row["signals"] = signal_count
    if signal_count:
        row["citation_rate"] = round(rate, 3)
        row["cited"] = rate >= MIN_CITATION_RATE

    # --- paraphrase (semantic, optional) -----------------------------------
    if embedder is not None:
        best = 0.0
        try:
            reply_vectors = [embedder.embed(text) for text in replies]
            for memory in memories:
                left = embedder.embed(str(memory.get("content") or ""))
                for vector in reply_vectors:
                    best = max(best, float(embedder.cosine(left, vector)))
        except Exception:
            best = 0.0
        row["paraphrase"] = round(best, 3)

    # --- correction (negative signal) --------------------------------------
    row["corrected"] = any(
        _corrected(text) and any(key in text.lower() for key in signals)
        for text in replies)

    # --- matched control arm ----------------------------------------------
    if controls:
        # The control arm must never contain anything this system injected in
        # *any* trace: an injected memory reused elsewhere is exactly the
        # effect being measured, so leaking it into the control would silently
        # shrink every lift toward zero.
        excluded = set(memory_ids) | {str(item) for item in injected_anywhere}
        pool = _control_pool(conn, sorted(excluded))
        matched = _match_controls(pool, memories)
        row["control_pairs"] = len(matched)
        if matched:
            control_signals: Dict[str, str] = {}
            for memory in matched:
                for key, kind in extract_signals(str(memory.get("content") or "")).items():
                    control_signals.setdefault(key, kind)
            control_rate, control_count, _ = _weighted_hits(
                control_signals, joined, df, docs)
            if control_count:
                row["control_rate"] = round(control_rate, 3)
                row["lift"] = round(rate - control_rate, 3)

    row["evaluable"] = True
    return row


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _arm_split(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Compare the two arms of the matched experiment.

    Both arms are counted on the *same* metrics, so the difference is a
    like-for-like comparison rather than two separately-computed rates.  The
    result is None-heavy by design: with a handful of control traces per week,
    "not enough control samples" is the normal answer and must stay visible.
    """
    # 只保留「两臂本来都能拿到东西」的 trace。
    #
    # 一次检索返回 0 条（empty_pack）时，两个臂都没有东西可投递——
    # 对照组不是因为「被扣留」才空，而是本来就没有。把它算进对照臂，
    # 等于把「检索没命中」记成「注入无效」，会让注入效果被系统性低估。
    # 实测生产 trace：16 条被 A/B 分流的里有 13 条是 empty_pack，
    # 如果不排除，对照臂会被这类噪声完全主导。
    informative = [row for row in rows
                   if row.get("evaluable") and int(row.get("items") or 0) > 0]
    sampled = informative
    treated = [row for row in sampled if row.get("delivered")]
    control = [row for row in sampled if not row.get("delivered")]

    def _rate(group: Sequence[Mapping[str, Any]]) -> Optional[float]:
        if not group:
            return None
        return round(sum(1 for row in group if row.get("cited")) / len(group), 3)

    treatment_rate = _rate(treated)
    control_rate = _rate(control)
    return {"treatment_n": len(treated), "control_n": len(control),
            "treatment_citation_rate": treatment_rate,
            "control_citation_rate": control_rate,
            "treatment_ci": wilson(sum(1 for row in treated if row.get("cited")),
                                   len(treated)),
            "control_ci": wilson(sum(1 for row in control if row.get("cited")),
                                 len(control)),
            "difference": (None if treatment_rate is None or control_rate is None
                           else round(treatment_rate - control_rate, 3)),
            "matched": bool(treated) and bool(control),
            # 被排除的空包 trace 数：样本为什么这么小，必须能解释。
            "excluded_empty_pack": int(sum(
                1 for row in rows if row.get("evaluable")
                and int(row.get("items") or 0) == 0))}


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    sampled = [row for row in rows if row.get("evaluable")]
    cited = [row for row in sampled if row.get("cited")]
    corrected = [row for row in sampled if row.get("corrected")]
    paraphrases = [row["paraphrase"] for row in sampled
                   if isinstance(row.get("paraphrase"), (int, float))]
    overlaps = [row["overlap"] for row in sampled]
    lifts = [row["lift"] for row in sampled if isinstance(row.get("lift"), (int, float))]
    reused = sum(1 for row in sampled if row.get("used"))
    return {
        "sampled": len(sampled),
        "reused": reused,
        "reuse_rate": (round(reused / len(sampled), 3) if sampled else None),
        "reuse_rate_ci": wilson(reused, len(sampled)),
        "mean_overlap": _mean(overlaps),
        "cited": len(cited),
        "citation_rate": (round(len(cited) / len(sampled), 3) if sampled else None),
        "citation_rate_ci": wilson(len(cited), len(sampled)),
        "citation_mean": _mean([row["citation_rate"] for row in sampled
                                if isinstance(row.get("citation_rate"), (int, float))]),
        "corrected": len(corrected),
        "correction_rate": (round(len(corrected) / len(sampled), 3) if sampled else None),
        "correction_rate_ci": wilson(len(corrected), len(sampled)),
        "paraphrase_mean": _mean(paraphrases),
        "paraphrase_n": len(paraphrases),
        "control_n": len(lifts),
        "control_lift_mean": _mean(lifts),
        "arms": _arm_split(sampled),
    }


def _bucket_items(count: int) -> str:
    if count <= 1:
        return "1"
    if count <= 4:
        return "2-4"
    return "5+"


def _strata(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "by_agent": {}, "by_items": {}, "by_source": {},
    }
    for row in rows:
        groups["by_agent"].setdefault(str(row.get("agent") or "(unknown)"), []).append(row)
        groups["by_items"].setdefault(_bucket_items(int(row.get("memory_ids") or 0)), []).append(row)
        groups["by_source"].setdefault(str(row.get("source") or "(unknown)"), []).append(row)
    out: Dict[str, Dict[str, Any]] = {}
    for dimension, buckets in groups.items():
        out[dimension] = {name: _aggregate(bucket) for name, bucket in sorted(buckets.items())}
    return out


def _verdict(sampled: int, aggregate: Mapping[str, Any], *, min_samples: int,
             control_mode: str) -> Dict[str, Any]:
    """Turn the numbers into an explicit, honest statement of what is known."""
    caveats = [
        "观察性证据：注入与复用之间的相关不等于因果；Agent 可能本来就知道这些内容。",
        "回复窗口内的其他工作会稀释或抬高所有指标，窗口越宽越不可信。",
        "对照臂是「同一时间点可检索但未被注入的记忆」，不是随机分组，仍可能残留选择偏差。",
        "生产 Hook 只在真正注入时写 trace，因此「无注入」自然实验通常无法构造。",
    ]
    if sampled == 0:
        return {"code": "no_samples", "conclusive": False,
                "headline": "没有可用样本：注入 trace 里没有一条同时满足"
                            "「记录了 memory_ids」和「窗口内有可读的 assistant 回复」。"
                            "本报告不给任何效果结论。",
                "caveats": caveats}
    if sampled < min_samples:
        return {"code": "insufficient_sample", "conclusive": False,
                "headline": ("样本量 %d < 阈值 %d，不足以支撑结论。下面的比率仅作"
                             "逐条排查线索，不能当作注入有效或无效的证据。"
                             % (sampled, min_samples)),
                "caveats": caveats}
    arms = aggregate.get("arms") or {}
    if arms.get("matched") and min(int(arms.get("treatment_n") or 0),
                                   int(arms.get("control_n") or 0)) >= min_samples:
        difference = float(arms.get("difference") or 0)
        code = "arm_positive" if difference > 0 else "arm_no_lift"
        headline = ("随机分组对照：注入臂复用率 %s (n=%d)，对照组 %s (n=%d)，差 %+.3f。"
                    "两组检索同一批候选，差别只在是否投递；这比观察性对照强得多，"
                    "但仍是单账户数据，不是绝对因果证明。"
                    % (arms.get("treatment_citation_rate"), arms.get("treatment_n"),
                       arms.get("control_citation_rate"), arms.get("control_n"),
                       difference))
        return {"code": code, "conclusive": True, "headline": headline,
                "caveats": caveats + ["随机分组对照仍是单账户数据，不能排除时间趋势。"]}
    lift = aggregate.get("control_lift_mean")
    if control_mode == "none":
        return {"code": "no_control", "conclusive": False,
                "headline": ("样本量达标（%d），但无法构造对照臂，只有单臂描述统计，"
                             "不能据此判断注入是否带来增益。" % sampled),
                "caveats": caveats}
    if isinstance(lift, (int, float)) and lift <= 0:
        return {"code": "no_lift", "conclusive": True,
                "headline": ("样本量达标（%d），但注入项的信号复用率并不高于未注入对照"
                             "（平均 lift=%+.3f）。没有证据支持注入有效。" % (sampled, lift)),
                "caveats": caveats}
    return {"code": "observational_positive", "conclusive": True,
            "headline": ("样本量达标（%d），注入项复用率高于对照（平均 lift=%+.3f）。"
                         "这是观察性关联，不是因果证明。" % (sampled, lift)),
            "caveats": caveats}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: Transcript cursors are keyed "transcript:<agent>:<path-hash>".
_TRANSCRIPT_PREFIX = "transcript:"


def _cursor_health(conn: sqlite3.Connection, agent: str) -> Dict[str, Any]:
    """Capture health for one agent, read from its transcript cursors.

    Two situations need opposite responses but look identical from the reply
    table alone: *the collector is broken* (fix a bug) and *the user stopped
    using this agent* (find a different source of samples).  The cursor table
    separates them without touching the filesystem.

    Only a recorded last_error proves the collector failed.  A cursor whose
    offset trails the file size is *not* a failure: transcripts.sync()
    deliberately reads at most max_per_file messages per cycle, so a large file
    legitimately sits behind for several cycles.  That backlog is reported as
    an informational counter so it stays visible without being mistaken for a
    fault.  What remains after ruling out errors is only a question of *when*
    the last successful scan happened relative to the injection.
    """
    plain = _TRANSCRIPT_PREFIX + str(agent) + ":"
    health: Dict[str, Any] = {
        "cursors": 0, "errors": 0, "backlog": 0,
        "last_scan_at": None, "status": "unknown",
    }
    try:
        rows = conn.execute(
            "SELECT source_id, cursor, updated_at, last_error FROM source_cursors "
            "WHERE source_id LIKE ?", (plain + "%",)).fetchall()
    except sqlite3.Error:
        return health
    rows = [row for row in rows if str(row["source_id"] or "").startswith(plain)]
    health["cursors"] = len(rows)
    if not rows:
        return health
    newest = None
    for row in rows:
        if str(row["last_error"] or "").strip():
            health["errors"] += 1
        try:
            payload = json.loads(str(row["cursor"] or "") or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, Mapping):
            payload = {}
        try:
            offset = int(payload.get("offset") or 0)
            size = int(payload.get("size") or 0)
        except (TypeError, ValueError):
            offset = size = 0
        if size > offset:
            health["backlog"] += 1
        stamp = _parse_time(row["updated_at"])
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    health["last_scan_at"] = newest.isoformat() if newest is not None else None
    if health["errors"]:
        health["status"] = "stalled"
    else:
        health["status"] = "current" if newest is not None else "unknown"
    return health


def _diagnostics(conn: sqlite3.Connection,
                 traces: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Why the sample is the size it is.

    A zero sample is only useful if it is explainable, and *which* explanation
    holds changes what should be done about it.  For every agent that appears
    in a trace this reports reply availability and, separately, whether the
    transcript collector is keeping up:

    replies_available    the window has readable replies; metrics apply
    capture_stalled      a cursor recorded a real error: a bug to fix
    source_idle          the collector ran *after* the injection and had
                         nothing new to read: the agent simply was not used
    capture_unverified   the newest scan predates the injection, so unread
                         data cannot be ruled out
    capture_unknown      no cursor rows at all: capture state is unknown
    """
    agents = sorted({str(trace.get("agent") or "") for trace in traces
                     if str(trace.get("agent") or "")})
    latest: Dict[str, Optional[datetime]] = {}
    for trace in traces:
        agent = str(trace.get("agent") or "")
        when = _parse_time(trace.get("at"))
        if agent and when is not None:
            if latest.get(agent) is None or when > latest[agent]:
                latest[agent] = when

    report: Dict[str, Any] = {"agents": {}}
    for agent in agents:
        when = latest.get(agent)
        entry: Dict[str, Any] = {"last_injection_at": None, "last_assistant_at": None,
                                 "assistant_total": 0, "assistant_after_injection": 0,
                                 "verdict": "capture_unknown"}
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(created_at) AS last FROM memories "
                "WHERE raw_chat=1 AND role='assistant' AND source_agent=?",
                (agent,)).fetchone()
            entry["assistant_total"] = int(row["n"] or 0)
            entry["last_assistant_at"] = str(row["last"] or "") or None
        except sqlite3.Error:
            pass
        if when is not None:
            entry["last_injection_at"] = when.isoformat()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE raw_chat=1 AND role='assistant' "
                    "AND source_agent=? AND created_at >= ? AND created_at <= ?",
                    (agent, when.isoformat(),
                     (when + timedelta(hours=24)).isoformat())).fetchone()
                entry["assistant_after_injection"] = int(row["n"] or 0)
            except sqlite3.Error:
                pass

        capture = _cursor_health(conn, agent)
        # A caught-up scan only explains an empty window if it happened after
        # the injection; an older scan cannot rule out unread data.
        if capture["status"] == "current" and when is not None:
            scanned = _parse_time(capture["last_scan_at"])
            if scanned is None or scanned < when:
                capture["status"] = "stale"
        entry["capture"] = capture

        if entry["assistant_after_injection"] > 0:
            entry["verdict"] = "replies_available"
        elif capture["status"] == "stalled":
            entry["verdict"] = "capture_stalled"
        elif capture["status"] == "current":
            entry["verdict"] = "source_idle"
        elif capture["status"] == "stale":
            entry["verdict"] = "capture_unverified"
        else:
            entry["verdict"] = "capture_unknown"
        report["agents"][agent] = entry

    try:
        row = conn.execute(
            "SELECT MAX(created_at) AS last FROM memories "
            "WHERE raw_chat=1 AND role='assistant'").fetchone()
        report["ledger_last_assistant_at"] = str(row["last"] or "") or None
    except sqlite3.Error:
        report["ledger_last_assistant_at"] = None
    return report


def evaluate(ledger, traces: Sequence[Mapping[str, Any]], *,
             window_minutes: int = WINDOW_MINUTES, embedder=None,
             background: bool = True, controls: bool = True,
             min_samples: int = MIN_SAMPLES) -> Dict[str, Any]:
    """Measure what the traced injections did (and did not) leave behind."""
    window = max(1, int(window_minutes))
    entries = [trace for trace in (traces or []) if isinstance(trace, Mapping)]
    rows: List[Dict[str, Any]] = []
    skip: Dict[str, int] = {}
    with ledger._reading() as conn:
        df, docs = _background(conn) if background else ({}, 0)
        diagnostics = _diagnostics(conn, entries)
        injected_anywhere = sorted({
            str(item) for trace in entries
            for item in (trace.get("memory_ids") or []) if str(item)})
        for trace in entries:
            row = _trace_row(ledger, conn, trace, window_minutes=window,
                             embedder=embedder, df=df, docs=docs, controls=controls,
                             injected_anywhere=injected_anywhere)
            rows.append(row)
            if row["skip_reason"]:
                skip[row["skip_reason"]] = skip.get(row["skip_reason"], 0) + 1

    aggregate = _aggregate(rows)
    with_ids = [row for row in rows if row["memory_ids"]]
    live = [row for row in with_ids if row["skip_reason"] != "memories_missing"]
    sampled = aggregate["sampled"]
    control_mode = "matched_not_injected" if aggregate["control_n"] else "none"
    verdict = _verdict(sampled, aggregate, min_samples=int(min_samples),
                       control_mode=control_mode)
    shortfall = max(0, int(min_samples) - sampled)

    return {
        "traces": len(entries),
        "traced_with_ids": len(with_ids),
        "traces_with_live_memories": len(live),
        "sampled": sampled,
        "reused": aggregate["reused"],
        "reuse_rate": aggregate["reuse_rate"],
        "mean_overlap": aggregate["mean_overlap"],
        "window_minutes": window,
        "min_term_overlap": MIN_TERM_OVERLAP,
        "min_samples": int(min_samples),
        "sample_shortfall": shortfall,
        "conclusive": bool(verdict["conclusive"]),
        "verdict": verdict,
        "sample": {"min_samples": int(min_samples), "sampled": sampled,
                   "shortfall": shortfall, "conclusive": bool(verdict["conclusive"]),
                   "skip_reasons": dict(sorted(skip.items()))},
        "metrics": {
            "overlap": {"used": aggregate["reused"],
                        "rate": aggregate["reuse_rate"],
                        "ci": aggregate["reuse_rate_ci"],
                        "mean": aggregate["mean_overlap"]},
            "citation": {"cited": aggregate["cited"],
                         "rate": aggregate["citation_rate"],
                         "ci": aggregate["citation_rate_ci"],
                         "mean": aggregate["citation_mean"]},
            "paraphrase": {"mean": aggregate["paraphrase_mean"],
                           "n": aggregate["paraphrase_n"],
                           "enabled": embedder is not None},
            "correction": {"corrected": aggregate["corrected"],
                           "rate": aggregate["correction_rate"],
                           "ci": aggregate["correction_rate_ci"]},
        },
        "arms": aggregate.get("arms") or _arm_split([]),
        "control": {
            "mode": control_mode,
            "pairs": aggregate["control_n"],
            "lift_mean": aggregate["control_lift_mean"],
            "observational": True,
            "causal": False,
            "note": ("对照 = 同一时间点可检索、同类但未被注入的记忆在同一回复窗口内的"
                     "信号复用率；属于观察性证据，不是随机对照实验。"),
        },
        "strata": _strata(rows),
        "diagnostics": diagnostics,
        "background_docs": docs,
        "details": rows[-MAX_DETAIL:],
    }


def default_embedder() -> Any:
    """Ledger-native LSA embedder, or None when no model is installed."""
    try:
        from .semantic_space import load_embedder
        return load_embedder()
    except Exception:
        return None


def report(ledger, *, limit: int = 500,
           window_minutes: int = WINDOW_MINUTES,
           traces: Optional[Sequence[Mapping[str, Any]]] = None,
           embedder: Any = "auto", background: bool = True, controls: bool = True,
           min_samples: int = MIN_SAMPLES) -> Dict[str, Any]:
    """Full report over the trace file (or an explicit trace list)."""
    entries = list(traces) if traces is not None else recent(max(1, int(limit)))
    if embedder == "auto":
        embedder = default_embedder()
    return evaluate(ledger, entries, window_minutes=window_minutes,
                    embedder=embedder, background=background,
                    controls=controls, min_samples=min_samples)


# ---------------------------------------------------------------------------
# Read-only ledger access
# ---------------------------------------------------------------------------


class ReadOnlyLedger:
    """Minimal read-only ledger view for reporting against a live database.

    The production ledger is being written by the daemon while a report runs.
    Opening it with mode=ro (plus PRAGMA query_only) makes it impossible for a
    measurement pass to mutate production state, and it exposes only the two
    members evaluate() needs.
    """

    def __init__(self, path: os.PathLike):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError("账本不存在: %s" % self.path)
        self._conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
        except sqlite3.DatabaseError:
            pass

    @contextlib.contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        yield self._conn

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE memory_id=?", (str(memory_id)[:80],)).fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "ReadOnlyLedger":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def default_ledger_path() -> Path:
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "hippomind.sqlite"


__all__ = [
    "MIN_CITATION_RATE", "MIN_SAMPLES", "MIN_TERM_OVERLAP", "WINDOW_MINUTES",
    "ReadOnlyLedger", "default_embedder", "default_ledger_path",
    "evaluate", "extract_signals", "report", "wilson",
]
