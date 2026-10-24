"""Did the injected context actually get used?

An injection trace proves a pack was offered, not that it helped.  This module
closes the loop offline: for every trace entry that recorded memory ids, it
looks at the same agent's assistant messages in the following window and
measures how much of the injected vocabulary reappears.  A high reuse rate is
evidence the gate is surfacing useful context; a low one means the pack is
noise the model ignored.

It is a proxy, not proof of causality: an agent can reuse a term it already
knew.  Reported as a bounded ratio with the sample size, never as a score.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Sequence

from .injection_trace import recent
from .ledger import SQLiteLedger
from .textutil import tokenize_multilingual

WINDOW_MINUTES = 30
MIN_TERM_OVERLAP = 0.15


def _parse(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def evaluate(ledger: SQLiteLedger, traces: Sequence[Mapping[str, Any]], *,
             window_minutes: int = WINDOW_MINUTES) -> Dict[str, Any]:
    """Reuse rate for traced injections that recorded memory ids."""
    rows: List[Dict[str, Any]] = []
    for trace in traces:
        memory_ids = [str(item) for item in trace.get("memory_ids") or [] if str(item)]
        if not memory_ids:
            continue
        agent = str(trace.get("agent") or "")
        started = _parse(str(trace.get("at") or ""))
        ended = started + timedelta(minutes=max(1, int(window_minutes)))
        injected_terms = set()
        for memory_id in memory_ids:
            memory = ledger.get_memory(memory_id)
            if memory:
                injected_terms |= tokenize_multilingual(str(memory.get("content") or ""))
        if not injected_terms:
            continue
        with ledger._reading() as conn:
            replies = conn.execute(
                "SELECT content FROM memories WHERE raw_chat=1 AND role='assistant' "
                "AND source_agent=? AND created_at >= ? AND created_at <= ? "
                "ORDER BY created_at ASC LIMIT 20",
                (agent, started.isoformat(), ended.isoformat())).fetchall()
        reply_terms = set()
        for row in replies:
            reply_terms |= tokenize_multilingual(str(row["content"] or ""))
        if not reply_terms:
            rows.append({"at": trace.get("at"), "agent": agent, "memory_ids": len(memory_ids),
                         "replies": 0, "overlap": 0.0, "used": False})
            continue
        overlap = len(injected_terms & reply_terms) / max(1, len(injected_terms))
        rows.append({"at": trace.get("at"), "agent": agent, "memory_ids": len(memory_ids),
                     "replies": len(replies), "overlap": round(overlap, 3),
                     "used": overlap >= MIN_TERM_OVERLAP})
    sampled = [row for row in rows if row["replies"] > 0]
    used = [row for row in sampled if row["used"]]
    return {
        "traces": len(traces),
        "traced_with_ids": len(rows),
        "sampled": len(sampled),
        "reused": len(used),
        "reuse_rate": round(len(used) / len(sampled), 3) if sampled else None,
        "mean_overlap": (round(sum(row["overlap"] for row in sampled) / len(sampled), 3)
                         if sampled else None),
        "window_minutes": int(window_minutes),
        "min_term_overlap": MIN_TERM_OVERLAP,
        "details": rows[-50:],
    }


def report(ledger: SQLiteLedger, *, limit: int = 500,
           window_minutes: int = WINDOW_MINUTES) -> Dict[str, Any]:
    return evaluate(ledger, recent(limit), window_minutes=window_minutes)
