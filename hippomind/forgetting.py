"""Explainable forgetting: why was *this* memory demoted or archived?

The ledger already decays and archives rows (lifecycle.maintain), and the
health review measured the result: every active memory had utility < 1, 255k
rows sat in 'cold', and nothing recorded why any particular row moved.
"2,989 memories were decayed" is not an audit.  The question an operator
actually asks is "why did THIS memory lose weight, and can I still get it
back?" -- and answering it needs the decision inputs, not a total.

Two sources, and each answers a different half of that question:

ledger_events rows with event_type='decay'
    The write side appends one event per lifecycle change or significant weight
    drop.  Its metadata_json carries the inputs the decision was made from
    (memory_id, from_lifecycle, to_lifecycle, reason, weight_before,
    weight_after, decay_per_day, age_days, utility, importance), so an
    explanation is evidence rather than a reconstruction.  Nothing here infers
    an event that was not written.

memories (the mutable projection)
    Only this can say what is true now: the row's kind and agent, whether the
    row still exists at all, and whether a recall query can still reach it.

ledger_events_archive (the side table, when it exists)
    maintain() moves old *orphan* audit rows out of ledger_events once the main
    table passes events_keep.  A decay event is exactly such an orphan -- it
    documents a row, it does not anchor one -- so a report that reads only
    ledger_events silently loses its own evidence as the ledger ages, and would
    report "no explanation" for a period that was in fact explained.  Both
    tables are read and merged; the side table is optional because it is created
    lazily on first overflow.

What "covered" means (and does not)
-----------------------------------
The write side does not emit an event for every decayed row: it emits one when
a row's lifecycle changes, or when its weight drops by at least
SIGNIFICANT_DROP (1%).  Ordinary slow decay below that threshold leaves no
trace on purpose -- at the observed rate (~3e-4 per hour) one event per row per
tick would be tens of thousands of events a day and would flood the ledger.  So
this report answers "did the SIGNIFICANT demotions and archivals get an
explanation", never "was every row's weight change recorded".  coverage.state
says which of those two answers the window can actually give, and the threshold
is printed next to the numbers so a reader cannot mistake silence for absence.

Three properties this report refuses to give up
-----------------------------------------------
1. Zero events is a finding, not a zero.  When the write side has not landed
   (or maintenance never ran), printing "0 条" reads like "nothing was
   forgotten" while 255k cold rows sit in the database.  forgetting_report()
   therefore reports coverage -- cold rows now vs. cold rows that actually
   carry an explanation -- and the verdict says "无法解释" out loud.
2. Recoverability is reported at the level it can actually be proven.  A cold
   row is not automatically reachable: search() also requires
   quality_state='accepted', a matching scope, a live validity window,
   non-empty evidence, and -- for raw transcripts -- include_raw_chat=True.
   recall_blockers() names those SQL clauses, and the tests pin the names
   against the ledger's own search().

   Those clauses are necessary, not sufficient.  maintain() deletes a cold
   row's memory_fts entry, and search() only runs its LIKE rescue channel when
   the hot index did not already fill the candidate budget -- so a cold row is
   reachable only when no active row matches the same query.  That is a
   candidate-generation limitation in search.py, not a predicate this module
   may re-declare: it is reported as 条件性可达 and pointed at, never silently
   upgraded to "always recallable".  The health review's claim that
   include_cold=True makes cold rows retrievable is therefore only half true,
   and section 6 of the report says so.
3. Malformed evidence stays visible.  Events whose metadata cannot be read are
   counted in events.malformed rather than dropped: a denominator that shrinks
   silently is how an audit starts lying.

The ledger is opened read-only: measuring production must never mutate it.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The one event type this report reads.  lifecycle.maintain is its only
#: producer; a rename there must show up here as "no evidence", not as silence.
DECAY_EVENT = "decay"

#: maintain()'s overflow side table.  Same columns as ledger_events; created
#: lazily, so every read of it must be guarded by a sqlite_master lookup.
ARCHIVE_TABLE = "ledger_events_archive"

DEFAULT_DAYS = 7
DEFAULT_LIMIT = 20

#: The write side's significance threshold: a decay event is appended when the
#: absolute weight drop reaches this fraction, or when the lifecycle changes.
#: Sub-threshold drift is deliberately not recorded -- at the measured rate
#: (~3e-4/hour) logging every tick would be ~72k events/day for 3k active rows
#: and would drown the audit in noise.
#:
#: This constant MIRRORS lifecycle.maintain's writer; it is not enforced here.
#: It exists so the report can state its own blind spot instead of implying
#: "no event" means "no decay".  If the write side changes the threshold,
#: change it here too -- there is exactly one place to look.
SIGNIFICANT_DROP = 0.01

#: Floor on the per-report event budget, never a silent cap.
#:
#: Maintenance can emit one event per row per tick, so an unbounded window on a
#: busy ledger is a memory hazard -- but a FIXED cap is worse: a silently
#: truncated audit report is the same class of error as a fabricated one.  The
#: effective budget scales with the window (see event_budget) and every read
#: that hits it sets events.truncated and says so in the verdict.  A floor
#: rather than a target: a 1-day window on a busy ledger still gets 5000.
MIN_EVENT_BUDGET = 5_000

#: How many events per day a window is assumed to produce, for sizing the
#: budget.  This is a *cost guard*, not a capacity claim: 3k active rows at the
#: 1% significance threshold emit roughly one event per row every ~1.5 days, so
#: ~2000/day is a generous upper estimate.  The number only decides when to stop
#: reading; it never decides what the report claims.
EVENTS_PER_DAY_ESTIMATE = 2_000

#: Hard ceiling regardless of window: beyond this the report would need more
#: memory than an audit tool should take from a live daemon's machine.
MAX_EVENT_BUDGET = 200_000

#: to_lifecycle values that mean "this row left the working set".
ARCHIVE_TARGETS = ("cold", "expired", "superseded")

#: lifecycle values a default recall query can return (include_cold=True).
RECALL_LIFECYCLES = ("active", "cold")

#: reason -> (human label, the rule the code actually implements).  The rule
#: text deliberately avoids magic thresholds: the numbers that decided THIS row
#: are in the event (weight_before/weight_after/age_days), and a threshold
#: copied here would silently drift away from lifecycle.maintain.
REASON_EXPLANATIONS: Dict[str, Tuple[str, str]] = {
    "expired": ("有效期到期", "valid_until 已过：事实不再成立，但仍可按 as_of 回溯"),
    "raw_retention": ("原始会话保留期满", "raw_chat 超过保留天数，移出热索引；审计检索仍可达"),
    "weight_floor": ("权重跌破冷置下限", "指数衰减把权重压到冷置阈值以下"),
    "age_limit": ("超过冷置年龄", "创建时间超过冷置天数，按年龄退居冷存储"),
    "capacity_overflow": ("活跃容量超限", "活跃条数超过容量上限，按 权重×重要度+utility 排序挤出末尾"),
}

#: Reasons outside the contract collapse into one visible bucket.  They must
#: never be folded into a neighbour: a writer that invents a reason has to show
#: up in the report, not disappear inside "weight_floor".
UNKNOWN_REASON = "unknown"
UNKNOWN_REASON_LABEL = "未标注/契约外原因"
UNKNOWN_REASON_RULE = "metadata.reason 缺失或不在契约取值内——写入侧需要修正"

#: Placeholder kind for events whose memory is no longer in the projection.
GONE_KIND = "(记忆已不在账本)"
UNKNOWN_AGENT = "(未知)"


# ---------------------------------------------------------------------------
# Read-only ledger access
# ---------------------------------------------------------------------------


class ReadOnlyLedger:
    """Open the live ledger without any possibility of writing to it.

    The daemon keeps writing while a report runs, so mode=ro plus query_only is
    the only acceptable access mode: a measurement pass must never be able to
    mutate production state, not even by accident.
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

    def tables(self) -> List[str]:
        """Existing table names (the archive side table is created lazily)."""
        return [str(row[0]) for row in self.rows(
            "SELECT name FROM sqlite_master WHERE type='table'")]

    def rows(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        return self._conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "ReadOnlyLedger":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def event_tables(ledger: "ReadOnlyLedger") -> List[str]:
    """Every table that may hold audit events, main table first."""
    return ["ledger_events", ARCHIVE_TABLE]


def default_ledger_path() -> Path:
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "hippomind.sqlite"


# ---------------------------------------------------------------------------
# Value helpers (tolerant on purpose: these read another writer's JSON)
# ---------------------------------------------------------------------------


def _field(row: Mapping[str, Any], name: str, default: Any = "") -> Any:
    """Read one column from a sqlite3.Row or a dict without raising."""
    try:
        value = row[name]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def _parse_epoch(value: Any) -> float:
    """ISO timestamp -> epoch seconds; 0.0 when unparseable (never raises)."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Best-effort float.  Metadata written by another module may hold strings."""
    if value is None or isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return numeric if math.isfinite(numeric) else default


def _loads(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _scalar(row: Optional[Any], default: int = 0) -> int:
    if row is None:
        return default
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError, IndexError):
        return default


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


def parse_decay_event(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """One decay event -> the decision it recorded, or None if unusable.

    None means the event cannot even name a memory (unreadable JSON, no
    memory_id); the caller counts those as malformed.  A missing reason is
    KEPT and reported as UNKNOWN_REASON, because "a decay happened and nobody
    recorded why" is itself the answer an audit needs.
    """
    metadata = _loads(_field(row, "metadata_json", ""))
    if metadata is None:
        return None
    memory_id = str(metadata.get("memory_id") or "").strip()[:80]
    if not memory_id:
        return None
    raw_reason = str(metadata.get("reason") or "").strip()
    reason = raw_reason.lower() if raw_reason.lower() in REASON_EXPLANATIONS else UNKNOWN_REASON
    before = _number(metadata.get("weight_before"))
    after = _number(metadata.get("weight_after"))
    to_lifecycle = str(metadata.get("to_lifecycle") or "").strip().lower()
    occurred_at = str(_field(row, "occurred_at", ""))
    return {
        "event_id": str(_field(row, "event_id", "")),
        "memory_id": memory_id,
        "occurred_at": occurred_at,
        "at_epoch": _parse_epoch(occurred_at),
        "event_agent": str(_field(row, "source_agent", "")),
        "from_lifecycle": str(metadata.get("from_lifecycle") or "").strip().lower(),
        "to_lifecycle": to_lifecycle,
        "reason": reason,
        "raw_reason": raw_reason,
        "weight_before": before,
        "weight_after": after,
        "decay_per_day": _number(metadata.get("decay_per_day")),
        "age_days": _number(metadata.get("age_days")),
        # Supplementary field (added by the writer after the 10-field contract).
        # age_days is "days since last weighting" -- the decay exponent input and
        # the weight_floor driver.  row_age_days is "days since creation" -- what
        # raw_retention and age_limit actually compare.  Reporting only the first
        # makes an old row archived for being old display age_days=0.0, i.e.
        # "a memory written moments ago was archived for being too old".  The
        # writer's own docstring warns about exactly this; None means the writer
        # predates the field, and the renderer must then say so rather than
        # silently substituting the wrong quantity.
        "row_age_days": _number(metadata.get("row_age_days")),
        "utility": _number(metadata.get("utility")),
        "importance": _number(metadata.get("importance")),
        "drop": (before - after) if (before is not None and after is not None) else None,
        "archived": bool(to_lifecycle) and to_lifecycle in ARCHIVE_TARGETS,
    }


def event_budget(days: int) -> int:
    """How many events one report may read, scaled to the window.

    A fixed cap silently truncates a long window; an unbounded read is a memory
    hazard on a live ledger.  Scaling with the window and clamping both ends
    keeps the budget defensible: the floor protects short windows, the ceiling
    protects the process, and hitting either is *reported* rather than hidden.
    """
    window = max(1, int(days))
    return max(MIN_EVENT_BUDGET,
               min(MAX_EVENT_BUDGET, window * EVENTS_PER_DAY_ESTIMATE))


def decay_events(ledger: ReadOnlyLedger, *, days: int = DEFAULT_DAYS,
                 now: Optional[datetime] = None,
                 max_events: Optional[int] = None) -> Dict[str, Any]:
    """Every decay event inside the window, oldest first.

    The SQL filter is a coarse string comparison on ISO timestamps (the same
    convention scripts/distill_status.py uses); each row is re-checked against
    the parsed epoch so a differently-formatted timestamp cannot leak an
    out-of-window event into the totals.
    """
    now = now or datetime.now(timezone.utc)
    window_days = max(1, int(days))
    budget = event_budget(window_days) if max_events is None else max(1, int(max_events))
    start = now - timedelta(days=window_days)
    # ORDER BY DESC + LIMIT keeps the newest events when truncating; the list is
    # then sorted ascending so aggregation runs chronologically (the first
    # weight_before is the weight at window start, the last weight_after is the
    # weight now).
    columns = "event_id,occurred_at,source_agent,metadata_json"
    where = "WHERE event_type=? AND occurred_at>=?"
    params = (DECAY_EVENT, start.isoformat())
    sources = [source for source in event_tables(ledger)
               if source in ledger.tables()]
    if not sources:
        sources = ["ledger_events"]
    if len(sources) == 1:
        rows = ledger.rows(
            "SELECT " + columns + " FROM " + sources[0] + " " + where
            + " ORDER BY occurred_at DESC LIMIT ?",
            (*params, budget + 1))
    else:
        # UNION ALL + DISTINCT-by-event_id below: maintain() INSERTs into the
        # archive before DELETEing from the main table, so an interrupted pass
        # can legitimately leave the same event_id in both.  Dedupe here rather
        # than trusting the writer to have finished.
        union = " UNION ALL ".join(
            "SELECT " + columns + " FROM " + source + " " + where for source in sources)
        rows = ledger.rows(
            "SELECT " + columns + " FROM (" + union + ") ORDER BY occurred_at DESC LIMIT ?",
            (*params, *params, budget + 1))
    truncated = len(rows) > budget
    if truncated:
        rows = rows[:budget]
    start_epoch = start.timestamp()
    events: List[Dict[str, Any]] = []
    malformed = 0
    seen: set = set()
    for row in rows:
        event = parse_decay_event(row)
        if event is None:
            malformed += 1
            continue
        if event["at_epoch"] and event["at_epoch"] < start_epoch:
            continue
        if event["event_id"]:
            if event["event_id"] in seen:
                continue
            seen.add(event["event_id"])
        events.append(event)
    events.sort(key=lambda item: item["at_epoch"])
    return {"events": events, "malformed": malformed, "truncated": truncated,
            "scanned": len(rows), "days": window_days, "budget": budget,
            "tables": sources,
            "start": start.isoformat(), "end": now.isoformat()}


def memory_projection(ledger: ReadOnlyLedger,
                      memory_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Current memories rows for the given ids (bounded, chunked).

    Chunked because SQLite's variable limit is 999 on older builds and a busy
    maintenance window can reference thousands of memories.
    """
    projection: Dict[str, Dict[str, Any]] = {}
    ids = [memory_id for memory_id in dict.fromkeys(memory_ids) if memory_id]
    columns = ("memory_id,kind,source_agent,scope,lifecycle,quality_state,raw_chat,"
               "evidence_json,valid_from,valid_until,weight,utility,importance,"
               "archived_at,updated_at")
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in ledger.rows(
                "SELECT " + columns + " FROM memories WHERE memory_id IN ("
                + placeholders + ")", batch):
            projection[str(row["memory_id"])] = dict(row)
    return projection


# ---------------------------------------------------------------------------
# Recoverability: the second half of "why", answered against the real predicate
# ---------------------------------------------------------------------------


def recall_blockers(row: Mapping[str, Any], *, scope: str = "shared",
                    include_raw_chat: bool = False,
                    now: Optional[datetime] = None) -> List[str]:
    """SQL clauses that would keep a normal recall query from returning this row.

    Empty list means "no clause blocks it" -- NOT "search() will return it".
    Candidate generation is a separate stage: a cold row has no memory_fts entry,
    so it can only be reached by the bounded LIKE rescue channel, which runs
    solely when the hot index left the candidate budget unfilled.  Callers must
    keep the two levels apart (see conditional_note below); conflating them is
    what produced the health review's "cold rows are always retrievable" claim.

    include_cold is deliberately NOT a parameter: the recovery path this report
    documents is the default one (include_cold=True), so "can it be recalled"
    always means "with cold rows allowed".
    """
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    blockers: List[str] = []
    row_scope = str(row.get("scope") or "")
    if row_scope != scope:
        blockers.append("scope=%s（查询 scope=%s）" % (row_scope or "-", scope))
    quality = str(row.get("quality_state") or "")
    if quality != "accepted":
        blockers.append("quality_state=%s（未过质量门禁）" % (quality or "-"))
    lifecycle = str(row.get("lifecycle") or "")
    if lifecycle not in RECALL_LIFECYCLES:
        blockers.append("lifecycle=%s（include_cold 只放行 active/cold）" % (lifecycle or "-"))
    if int(row.get("raw_chat") or 0) and not include_raw_chat:
        blockers.append("raw_chat=1（需 include_raw_chat=True）")
    if _evidence_is_empty(row):
        blockers.append("evidence_json=[]（require_evidence=True 会拒绝）")
    valid_until = str(row.get("valid_until") or "")
    if valid_until and valid_until <= now_iso:
        blockers.append("valid_until 已过期")
    valid_from = str(row.get("valid_from") or "")
    if valid_from and valid_from > now_iso:
        blockers.append("valid_from 尚未生效")
    return blockers


def _evidence_is_empty(row: Mapping[str, Any]) -> bool:
    """Whether search()'s require_evidence predicate would reject this row.

    Two row shapes reach this module and both must work: the raw projection
    (evidence_json, a JSON string, as memory_projection selects it) and
    SQLiteLedger.get_memory()/search() output (evidence, already parsed).
    A row carrying neither field is *not* claimed as unrecoverable -- guessing
    would turn "unknown" into a false accusation.
    """
    if "evidence_json" in row:
        return str(row.get("evidence_json") or "").strip() in ("", "[]")
    if "evidence" in row:
        value = row.get("evidence")
        if isinstance(value, (list, tuple)):
            return len(value) == 0
        if isinstance(value, str):
            return value.strip() in ("", "[]")
        return value is None
    return False


def _blocker_key(blocker: str) -> str:
    """Drop the parenthetical detail so blocker counts aggregate."""
    return blocker.split("（")[0]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _bump(store: Dict[str, Dict[str, Any]], key: str, event: Mapping[str, Any]) -> None:
    entry = store.get(key)
    if entry is None:
        entry = {"key": key, "events": 0, "memories": set(), "archived": 0}
        store[key] = entry
    entry["events"] += 1
    entry["memories"].add(event["memory_id"])
    if event["archived"]:
        entry["archived"] += 1


def _accumulate_memory(store: Dict[str, Dict[str, Any]], event: Mapping[str, Any],
                       *, kind: str, agent: str) -> None:
    """Per-memory rollup: one row per memory, not one row per tick.

    A row that decays across several maintenance ticks produces several events;
    listing each of them would bury the answer in repetition.  The rollup keeps
    the SPAN (first weight_before -> last weight_after) rather than summing
    per-event drops, which would double-count the same weight loss.
    """
    entry = store.get(event["memory_id"])
    if entry is None:
        store[event["memory_id"]] = {
            "memory_id": event["memory_id"],
            "kind": kind,
            "agent": agent,
            "events": 1,
            "reasons": [event["raw_reason"] or UNKNOWN_REASON],
            "weight_before": event["weight_before"],
            "weight_after": event["weight_after"],
            "drop": event["drop"],
            "age_days": event["age_days"],
            "row_age_days": event["row_age_days"],
            "utility": event["utility"],
            "importance": event["importance"],
            "last_at": event["occurred_at"],
            "archived": event["archived"],
            "at_epoch": event["at_epoch"],
        }
        return
    entry["events"] += 1
    reason_text = event["raw_reason"] or UNKNOWN_REASON
    if reason_text not in entry["reasons"]:
        entry["reasons"].append(reason_text)
    if event["weight_after"] is not None:
        entry["weight_after"] = event["weight_after"]
    if entry["weight_before"] is not None and entry["weight_after"] is not None:
        entry["drop"] = entry["weight_before"] - entry["weight_after"]
    for field in ("age_days", "row_age_days", "utility", "importance"):
        if event[field] is not None:
            entry[field] = event[field]
    entry["last_at"] = event["occurred_at"]
    entry["at_epoch"] = event["at_epoch"]
    entry["archived"] = entry["archived"] or event["archived"]


def forgetting_report(ledger: ReadOnlyLedger, *, days: int = DEFAULT_DAYS,
                      limit: int = DEFAULT_LIMIT,
                      now: Optional[datetime] = None,
                      scope: str = "shared",
                      include_raw_chat: bool = False,
                      max_events: Optional[int] = None) -> Dict[str, Any]:
    """Everything scripts/forgetting_report.py prints, as plain data."""
    now = now or datetime.now(timezone.utc)
    limit = max(1, int(limit))
    loaded = decay_events(ledger, days=days, now=now, max_events=max_events)
    events = loaded["events"]
    projection = memory_projection(ledger, [event["memory_id"] for event in events])

    reason_stats: Dict[str, Dict[str, Any]] = {}
    agent_stats: Dict[str, Dict[str, Any]] = {}
    kind_stats: Dict[str, Dict[str, Any]] = {}
    target_stats: Dict[str, int] = {}
    per_memory: Dict[str, Dict[str, Any]] = {}
    for event in events:
        row = projection.get(event["memory_id"]) or {}
        kind = str(row.get("kind") or GONE_KIND)
        agent = str(row.get("source_agent") or event["event_agent"] or UNKNOWN_AGENT)
        _bump(reason_stats, event["reason"], event)
        _bump(agent_stats, agent, event)
        _bump(kind_stats, kind, event)
        target = event["to_lifecycle"] or "(未标注)"
        target_stats[target] = target_stats.get(target, 0) + 1
        _accumulate_memory(per_memory, event, kind=kind, agent=agent)

    by_reason: List[Dict[str, Any]] = []
    for reason, entry in reason_stats.items():
        label, rule = REASON_EXPLANATIONS.get(
            reason, (UNKNOWN_REASON_LABEL, UNKNOWN_REASON_RULE))
        weight_drop = sum(
            event["drop"] for event in events
            if event["reason"] == reason and event["drop"] is not None)
        by_reason.append({
            "reason": reason, "label": label, "rule": rule,
            "events": entry["events"], "memories": len(entry["memories"]),
            "archived": entry["archived"], "weight_drop": round(weight_drop, 4),
        })
    by_reason.sort(key=lambda item: (-item["events"], item["reason"]))

    def _finish(store: Dict[str, Dict[str, Any]], key_name: str) -> List[Dict[str, Any]]:
        rows = [{key_name: entry["key"], "events": entry["events"],
                 "memories": len(entry["memories"]), "archived": entry["archived"]}
                for entry in store.values()]
        rows.sort(key=lambda item: (-item["events"], item[key_name]))
        return rows

    ranked = sorted(per_memory.values(),
                    key=lambda item: ((item["drop"] if item["drop"] is not None else 0.0),
                                      item["at_epoch"]),
                    reverse=True)
    top_drops = []
    for item in ranked[:limit]:
        top_drops.append({
            "memory_id": item["memory_id"], "kind": item["kind"],
            "agent": item["agent"], "reasons": list(item["reasons"]),
            "events": item["events"],
            "weight_before": item["weight_before"],
            "weight_after": item["weight_after"],
            "drop": None if item["drop"] is None else round(item["drop"], 4),
            "age_days": item["age_days"], "row_age_days": item["row_age_days"],
            "utility": item["utility"],
            "importance": item["importance"], "last_at": item["last_at"],
            "archived": item["archived"],
        })

    recall: Dict[str, Any] = {
        "scope": scope, "include_raw_chat": bool(include_raw_chat),
        "memories": 0, "predicate_reachable": 0, "cold_now": 0,
        "needs_raw_flag": 0, "blocked": 0, "gone": 0, "blockers": {}, "examples": [],
    }
    for memory_id in dict.fromkeys(event["memory_id"] for event in events):
        recall["memories"] += 1
        row = projection.get(memory_id)
        if row is None:
            recall["gone"] += 1
            continue
        # include_raw_chat is honoured, not hardcoded: passing --include-raw-chat
        # means "raw transcripts count as recallable", so the flag has to reach
        # the predicate.  Hardcoding False here made the flag silently do
        # nothing and reported every raw row as blocked instead.
        blockers = recall_blockers(row, scope=scope,
                                   include_raw_chat=include_raw_chat, now=now)
        if not blockers:
            recall["predicate_reachable"] += 1
            if str(row.get("lifecycle") or "") != "active":
                # No clause blocks it, but it is out of the hot index: reachable
                # only while no active row fills the candidate budget.
                recall["cold_now"] += 1
            continue
        if not include_raw_chat and not recall_blockers(
                row, scope=scope, include_raw_chat=True, now=now):
            recall["needs_raw_flag"] += 1
            continue
        recall["blocked"] += 1
        for blocker in blockers:
            key = _blocker_key(blocker)
            recall["blockers"][key] = recall["blockers"].get(key, 0) + 1
        if len(recall["examples"]) < 5:
            recall["examples"].append({"memory_id": memory_id, "blockers": blockers})

    cold_total = _scalar(ledger.one(
        "SELECT COUNT(*) FROM memories WHERE lifecycle='cold'"))
    existing = set(ledger.tables())
    all_time = sum(_scalar(ledger.one(
        "SELECT COUNT(*) FROM " + table + " WHERE event_type=?", (DECAY_EVENT,)))
        for table in event_tables(ledger) if table in existing)
    explained_cold = len({event["memory_id"] for event in events
                          if (projection.get(event["memory_id"]) or {}).get("lifecycle") == "cold"})
    coverage = {
        "cold_total_now": cold_total,
        "explained_cold": explained_cold,
        "unexplained_cold": max(0, cold_total - explained_cold),
        "decay_events_all_time": all_time,
        "decay_events_available": bool(events) or all_time > 0,
        "significant_drop": SIGNIFICANT_DROP,
    }
    coverage["state"] = coverage_state(coverage)
    # Which rules actually fired, and whether the events carried the age field
    # each rule needs.  This is the coverage gap that is NOT about missing
    # events: an event can be present and still not explain itself if the writer
    # only recorded the decay age.  Naming it separately keeps "no event" and
    # "event without the decisive input" from collapsing into one number.
    age_gaps: Dict[str, int] = {}
    for item in per_memory.values():
        for reason in item["reasons"]:
            field = AGE_FIELD_BY_REASON.get(reason)
            if not field:
                continue
            if item.get(field) is None:
                age_gaps[field] = age_gaps.get(field, 0) + 1
    coverage["missing_age_field"] = age_gaps
    coverage["reasons_seen"] = sorted({reason for item in per_memory.values()
                                       for reason in item["reasons"]})

    archived_total = sum(entry["archived"] for entry in reason_stats.values())
    result: Dict[str, Any] = {
        "ledger": str(ledger.path),
        "generated_at": now.isoformat(),
        "window_days": loaded["days"],
        "window": {"start": loaded["start"], "end": loaded["end"]},
        "scope": scope,
        "limit": limit,
        "events": {
            "total": len(events), "malformed": loaded["malformed"],
            "truncated": loaded["truncated"], "scanned": loaded["scanned"],
            "memories": len(per_memory), "tables": loaded["tables"],
            "budget": loaded["budget"],
        },
        "by_reason": by_reason,
        "by_agent": _finish(agent_stats, "agent"),
        "by_kind": _finish(kind_stats, "kind"),
        "archive": {
            "total": archived_total,
            "by_target": [{"to_lifecycle": name, "events": count}
                          for name, count in sorted(target_stats.items())],
        },
        "top_drops": top_drops,
        "unchanged_weight": sum(1 for item in per_memory.values() if not item["drop"]),
        "recoverability": recall,
        "coverage": coverage,
    }
    result["verdict"] = _verdict(result)
    return result


#: One word per coverage state, reused by both the verdict and the renderer so
#: the JSON and the printed report can never disagree about how much is known.
COVERAGE_STATE_LABELS: Dict[str, str] = {
    "empty": "无事可解释",
    "none": "无法解释",
    "partial": "部分可解释",
    "complete": "完全可解释",
}


def coverage_state(coverage: Mapping[str, Any]) -> str:
    """How much of the CURRENT cold population this window explains.

    empty    nothing is archived, so there is nothing to explain
    none     cold rows exist and none of them has an event in this window
    partial  some cold rows are explained, some are not -- the common case
             right after the write side lands, because rows archived before it
             landed can never acquire an event retroactively
    complete every cold row now in the ledger has an event in this window

    "partial" must never be reported as "explained": a window that explains 3
    of 100 archived rows is evidence about 3 rows, not a clean bill of health.
    """
    cold = int(coverage.get("cold_total_now") or 0)
    explained = int(coverage.get("explained_cold") or 0)
    if cold <= 0:
        return "empty"
    if explained <= 0:
        return "none"
    return "partial" if explained < cold else "complete"


def _verdict(result: Mapping[str, Any]) -> Dict[str, Any]:
    """The one paragraph a reader should be able to act on.

    Two failure modes this guards against, both of which look like a tidy zero:
    printing "0 条" when 255k cold rows exist (the evidence was never written),
    and printing "已解释" when only a fraction of the archived population has an
    event (the write side landed after most rows were already archived).  Both
    are distinguishable here because coverage carries the population size and
    the significance threshold.
    """
    events = result["events"]
    coverage = result["coverage"]
    recall = result["recoverability"]
    state = coverage["state"]
    threshold = "%g%%" % (float(coverage.get("significant_drop") or 0.0) * 100)
    caveats: List[str] = []
    if state == "empty" and not events["total"]:
        headline = "本窗口没有遗忘事件，也没有 cold 记忆：无事可解释"
        explainable = True
        caveats.append("若维护任务从未运行过，这是「没有维护」而不是「没有遗忘」")
    elif state == "none":
        headline = ("本窗口没有任何 decay 事件，但账本里有 %d 条 cold 记忆："
                    "遗忘发生了，却没有留下原因" % coverage["cold_total_now"])
        explainable = False
        caveats.append("写入侧（lifecycle.maintain）尚未落 decay 事件，或维护任务未在本窗口运行")
        caveats.append("在事件落地之前，本报告只能证明「无解释」，不能证明「没遗忘」")
        caveats.append("cold 行仍在账本里、未被删除，且可被显式查询召回："
                       "候选生成有一条专门的 cold 兜底通道（2026-10-25 修复），"
                       "不再依赖「热索引没喂饱」这个偶然条件")
    else:
        top = result["by_reason"][0] if result["by_reason"] else None
        headline = "%d 条记忆在本窗口被降权/归档（%d 条事件），主因：%s" % (
            events["memories"], events["total"],
            ("%s %s" % (top["reason"], top["label"])) if top else "无")
        # "partial" is the honest default right after the write side lands: rows
        # archived before it can never acquire an event retroactively.  It is a
        # BOUNDED yes, not a no: the rows that do have events are explained with
        # evidence.  Saying "not explainable" here would throw away the 3 real
        # explanations along with the 97 gaps, which is the mirror-image error of
        # claiming complete coverage.
        explainable = True
        if state == "partial":
            headline += "；但 %d 条 cold 中只有 %d 条有解释，另 %d 条无解释（%s）" % (
                coverage["cold_total_now"], coverage["explained_cold"],
                coverage["unexplained_cold"], COVERAGE_STATE_LABELS["partial"])
            caveats.append("只有本窗口内降幅 ≥%s 或发生 lifecycle 变化的行会落事件；"
                           "低于阈值的缓慢漂移按设计不留痕" % threshold)
            caveats.append("写入侧落地之前归档的行无法补记原因，因此「无解释」≠「没遗忘」")
            caveats.append("结论只覆盖有事件的那 %d 条，不能外推到全部 %d 条 cold"
                           % (coverage["explained_cold"], coverage["cold_total_now"]))
        if recall["predicate_reachable"]:
            caveats.append("其中 %d 条没有任何 SQL 条件阻断召回（遗忘是可逆的）"
                           % recall["predicate_reachable"])
        if recall["cold_now"]:
            caveats.append("其中 %d 条已是 cold：它们不在热索引里，但候选生成有"
                           "专门的 cold 兜底通道，显式查询可以取回（见第 6 节）"
                           % recall["cold_now"])
        if recall["needs_raw_flag"]:
            caveats.append("%d 条是原始会话，召回还需 include_raw_chat=True" % recall["needs_raw_flag"])
        if recall["blocked"]:
            caveats.append("%d 条已不可召回（质量门禁/有效期/scope），见第 6 节"
                           % recall["blocked"])
        if recall["gone"]:
            caveats.append("%d 条的记忆行已被 forget/purge 删除，只剩审计事件" % recall["gone"])
    if events["malformed"]:
        caveats.append("%d 条 decay 事件无法解析（metadata 缺 memory_id 或不是 JSON），未计入统计"
                       % events["malformed"])
    if events["truncated"]:
        caveats.append("事件数达到本窗口的读取上限 %d，只统计了最近这些："
                       "本次结果不完整，缩小 --days 或按 reason 分段查看"
                       % int(events.get("budget") or 0))
    if state in ("none", "partial") and coverage["unexplained_cold"]:
        caveats.append("当前 %d 条 cold 中只有 %d 条在本窗口有解释，其余是窗口外归档或从未记录"
                       % (coverage["cold_total_now"], coverage["explained_cold"]))
    return {"headline": headline, "explainable": explainable,
            "coverage_state": state,
            "coverage_label": COVERAGE_STATE_LABELS[state],
            "explained_rows": int(coverage["explained_cold"]),
            "unexplained_rows": int(coverage["unexplained_cold"]),
            "recall_is_conditional": bool(recall["cold_now"]),
            "caveats": caveats}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_weight(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.3f" % float(value)


def _fmt_drop(value: Optional[float]) -> str:
    """Weight LOSS as a plain magnitude.

    Not "%+.3f": a drop of 0.25 printed as "+0.250" reads as a weight increase,
    which is the opposite of what happened.  The column is labelled 降幅, so the
    number is positive by definition and any sign would be a lie.
    """
    return "n/a" if value is None else "%.3f" % float(value)


def _fmt_days(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.1f" % float(value)


#: Reasons that describe a row LEAVING the working set.  Used to pick the
#: decisive age: when a memory accumulated several reasons across ticks, the one
#: that archived it answers "why is it gone"; earlier weight_floor events only
#: describe how it drifted first.
ARCHIVE_REASONS = frozenset({"raw_retention", "age_limit"})

#: Which age field actually drove each reason.  Printing the wrong one makes the
#: report contradict itself (an old row archived for age showing age_days=0.0).
#: weight_floor is the only reason whose input is the decay exponent
#: (age_days); raw_retention/age_limit compare the row's own age, and
#: expired/capacity_overflow are not age-driven at all.
AGE_FIELD_BY_REASON: Dict[str, str] = {
    "weight_floor": "age_days",
    "raw_retention": "row_age_days",
    "age_limit": "row_age_days",
    "expired": "",
    "capacity_overflow": "",
}


def decisive_age(row: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    """The age that decided this memory, plus which field it came from.

    A memory can accumulate several reasons across ticks (weight_floor first,
    then age_limit).  The decisive one is the *archive* reason if there is one:
    that is the rule that actually removed the row from the working set, and it
    is the question a reader is asking.  Falls back to the first reason.

    Returns (None, "") when no reason is age-driven, so the renderer prints
    n/a instead of a number that had nothing to do with the decision.
    """
    reasons = [str(reason) for reason in (row.get("reasons") or [])]
    # Two passes, not one: a memory that decayed (weight_floor) and was later
    # archived for age accumulates both reasons in event order, so scanning
    # once would stop at weight_floor -- the rule that did NOT remove the row.
    # The archiving rule is the answer to "why is it gone", so it wins; only if
    # no archiving reason is present does the first age-driven reason apply.
    archive_reasons = [reason for reason in reasons
                       if reason in ARCHIVE_REASONS and AGE_FIELD_BY_REASON.get(reason)]
    candidates = archive_reasons or [reason for reason in reasons
                                     if AGE_FIELD_BY_REASON.get(reason)]
    if not candidates:
        return None, ""
    field = AGE_FIELD_BY_REASON[candidates[0]]
    return row.get(field), field


def _fmt_int(value: Optional[int]) -> str:
    return "n/a" if value is None else "{:,}".format(int(value))


def render(result: Mapping[str, Any]) -> str:
    """Chinese, sectioned report.  Every number is traceable to one query."""
    lines: List[str] = []
    add = lines.append
    events = result["events"]
    recall = result["recoverability"]
    coverage = result["coverage"]

    add("遗忘审计 · 最近 %d 天 · 账本 %s" % (result["window_days"], result["ledger"]))
    add("=" * 78)
    add("")
    add("1. 证据（本窗口的 decay 事件）")
    add("   窗口                 %s" % result["window"]["start"])
    add("                     -> %s" % result["window"]["end"])
    add("   事件                 %s 条（无法解析 %s 条）"
        % (_fmt_int(events["total"]), _fmt_int(events["malformed"])))
    add("   读取的表             %s" % " + ".join(events.get("tables") or ["ledger_events"]))
    add("   本窗口读取上限       %s 条%s"
        % (_fmt_int(events.get("budget")),
           "  【已用满，结果不完整】" if events.get("truncated") else ""))
    add("   涉及记忆             %s 条" % _fmt_int(events["memories"]))
    if events["truncated"]:
        add("   ! 【已截断】读取上限 %s 条已用满，本报告只覆盖最近这些事件，"
            % _fmt_int(events.get("budget")))
        add("     总数可能更大 —— 缩小 --days 可看到完整窗口。")
    add("")
    add("2. 为什么被降权/归档（按原因）")
    if not result["by_reason"]:
        add("   本窗口没有 decay 事件 —— 原因不可查（见第 7、8 节）")
    else:
        add("   %-18s %6s %6s %6s %9s  %s"
            % ("reason", "事件", "记忆", "归档", "累计降幅", "依据"))
        for item in result["by_reason"]:
            add("   %-18s %6d %6d %6d %9s  %s"
                % (item["reason"], item["events"], item["memories"], item["archived"],
                   _fmt_drop(item["weight_drop"]), item["rule"]))
    missing = [reason for reason in REASON_EXPLANATIONS if reason not in
               {item["reason"] for item in result["by_reason"]}]
    if missing:
        add("   契约内未出现的原因： %s" % "、".join(missing))
    add("   说明：原因取自事件 metadata.reason，是写入侧当时的判定，不是本报告的推断。")
    add("")
    add("3. 按 agent / kind（谁的记忆被遗忘）")
    if result["by_agent"]:
        add("   按 agent：")
        for item in result["by_agent"]:
            add("     %-24s 事件 %4d · 记忆 %4d · 归档 %4d"
                % (item["agent"], item["events"], item["memories"], item["archived"]))
    if result["by_kind"]:
        add("   按 kind：")
        for item in result["by_kind"]:
            add("     %-24s 事件 %4d · 记忆 %4d · 归档 %4d"
                % (item["kind"], item["events"], item["memories"], item["archived"]))
    if not result["by_agent"] and not result["by_kind"]:
        add("   无")
    add("")
    add("4. 降权最狠的前 %d 条（按窗口内累计降幅）" % result["limit"])
    if not result["top_drops"]:
        add("   无")
    else:
        add("   %-22s %-9s %-9s %7s %-15s %-18s %s"
            % ("memory_id", "kind", "agent", "降幅", "权重", "原因", "决定性年龄(天)"))
        for item in result["top_drops"]:
            age, field = decisive_age(item)
            add("   %-22s %-9s %-9s %7s %-15s %-18s %s"
                % (item["memory_id"][:22], str(item["kind"])[:9], str(item["agent"])[:9],
                   _fmt_drop(item["drop"]),
                   "%s→%s" % (_fmt_weight(item["weight_before"]),
                              _fmt_weight(item["weight_after"])),
                   ",".join(item["reasons"])[:18], _fmt_days(age)))
        add("   决定性年龄取「实际让这行下线的那条规则」的输入：weight_floor 用"
            "距上次加权，")
        add("   age_limit/raw_retention 用距创建；expired/capacity_overflow 与年龄无关，"
            "显示 n/a。")
        add("   （%s 条事件合计 %s 条记忆；同一条记忆的多次 tick 已合并为一行）"
            % (_fmt_int(events["total"]), _fmt_int(events["memories"])))
    if result["unchanged_weight"]:
        add("   另：%d 条记忆权重未变却被归档（容量挤出/保留期满），降幅为 0 排在后面"
            % result["unchanged_weight"])
    add("")
    add("5. 归档去向（lifecycle 变化目标）")
    add("   归档事件合计         %s 条" % _fmt_int(result["archive"]["total"]))
    for item in result["archive"]["by_target"]:
        add("     -> %-16s %s 条" % (item["to_lifecycle"], _fmt_int(item["events"])))
    add("")
    add("6. 还能不能召回（include_cold 默认 True）")
    add("   查询条件             scope=%s · require_evidence=True · as_of=现在"
        % recall["scope"])
    add("   涉及的记忆           %s 条" % _fmt_int(recall["memories"]))
    add("   无 SQL 条件阻断      %s 条" % _fmt_int(recall["predicate_reachable"]))
    add("     其中现在是 cold    %s 条  <- 走 cold 兜底通道，见下"
        % _fmt_int(recall["cold_now"]))
    add("   需再加 raw_chat 开关 %s 条  <- 原始会话还要 include_raw_chat=True"
        % _fmt_int(recall["needs_raw_flag"]))
    add("   被 SQL 条件拒绝      %s 条" % _fmt_int(recall["blocked"]))
    for key, count in sorted(recall["blockers"].items(), key=lambda pair: -pair[1]):
        add("     · %-52s %d" % (key, count))
    add("   记忆行已删除         %s 条  <- 只剩审计事件，无法恢复"
        % _fmt_int(recall["gone"]))
    add("   结论：降权不等于删除，cold 行的内容仍在账本里，且可以被显式查询取回。")
    add("         召回路径（2026-10-25 修复后）：")
    add("           · cold 行仍按设计从 memory_fts 删除（冷投影不占热索引）；")
    add("           · 候选生成新增了一条专门的 cold 兜底通道：当候选集里还没有")
    add("             cold 行时，用同一批词法条件在有界窗口内补一轮 LIKE 检索；")
    add("           · 因此 cold 行不再需要「热索引恰好没喂饱候选」才可见。")
    add("         修复前这里写的是「可召回是条件性的」——那个限制真实存在过，")
    add("         但它描述的是候选生成缺口，不是遗忘策略。现在缺口已补，措辞随之更新。")
    add("")
    add("7. 覆盖缺口（这条报告能解释多少）")
    add("   解释范围             本窗口内降幅 >= %s 或 lifecycle 变化的行"
        % _fmt_weight(coverage["significant_drop"]))
    add("                        （低于该阈值的缓慢漂移按设计不落事件）")
    add("   当前 cold 记忆       %s 条" % _fmt_int(coverage["cold_total_now"]))
    add("   本窗口有解释的 cold  %s 条" % _fmt_int(coverage["explained_cold"]))
    add("   无本窗口解释         %s 条  <- 窗口外归档，或写入侧从未记录"
        % _fmt_int(coverage["unexplained_cold"]))
    add("   decay 事件（全部时间）%s 条" % _fmt_int(coverage["decay_events_all_time"]))
    add("   覆盖判定             %s" % result["verdict"]["coverage_label"])
    if coverage["reasons_seen"]:
        add("   本窗口触发过的规则   %s" % "、".join(coverage["reasons_seen"]))
    for field, count in sorted((coverage.get("missing_age_field") or {}).items()):
        add("   ! %d 条事件缺少 %s：写入侧版本较旧，无法判定该行的决策年龄"
            % (count, field))
        add("     （age_days=距上次加权，row_age_days=距创建；两者不可互相替代）")
    if coverage["state"] == "none":
        add("   ! 账本里从来没有 decay 事件：遗忘审计目前只能证明「无解释」，")
        add("     不能证明「没遗忘」。事件由 lifecycle.maintain 写入，需先落地写入侧。")
    elif coverage["state"] == "partial":
        add("   ! 只有 %d/%d 条 cold 有解释：写入侧落地前归档的行无法补记原因。"
            % (coverage["explained_cold"], coverage["cold_total_now"]))
        add("     本报告只能为「有事件的那部分」作证，不能声称遗忘已被完整解释。")
    elif coverage["state"] == "complete":
        add("   当前 cold 全部有本窗口事件，可以逐条回答「为什么被归档」。")
    add("")
    add("8. 结论")
    add("   %s" % result["verdict"]["headline"])
    add("   可否解释： %s" % ("是" if result["verdict"]["explainable"] else "否"))
    if result["verdict"]["caveats"]:
        add("   不确定性：")
        for caveat in result["verdict"]["caveats"]:
            add("     - %s" % caveat)
    add("")
    add("9. 复现方式（只读，可直接对照 sqlite3）")
    add("   sqlite3 \"file:$LEDGER?mode=ro\" \"SELECT occurred_at,source_agent,metadata_json")
    add("     FROM ledger_events WHERE event_type='decay' ORDER BY occurred_at DESC LIMIT 20;\"")
    add("   sqlite3 \"file:$LEDGER?mode=ro\" \"SELECT lifecycle,COUNT(*) FROM memories")
    add("     GROUP BY 1;\"")
    return "\n".join(lines)


__all__ = [
    "ARCHIVE_TABLE", "ARCHIVE_TARGETS", "COVERAGE_STATE_LABELS", "DECAY_EVENT",
    "DEFAULT_DAYS", "DEFAULT_LIMIT", "EVENTS_PER_DAY_ESTIMATE",
    "MAX_EVENT_BUDGET", "MIN_EVENT_BUDGET", "SIGNIFICANT_DROP",
    "event_budget",
    "coverage_state", "event_tables",
    "REASON_EXPLANATIONS", "RECALL_LIFECYCLES", "ReadOnlyLedger", "UNKNOWN_REASON",
    "decay_events", "default_ledger_path", "forgetting_report", "memory_projection",
    "parse_decay_event", "recall_blockers", "render",
]
