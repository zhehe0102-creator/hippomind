"""hippomind explain - explain one Context Pack retrieval, offline.

Why this command exists
-----------------------
When an automatic injection comes back empty the operator currently has to
guess: is nothing indexed, is the query irrelevant, was it filtered as a
duplicate/conflict, or did the relevance gate drop it?  This command builds the
same Context Pack the injection path would build, but with debug=True, and
prints the stage-by-stage picture: loaded components, top candidates with score
parts, the filtered pool grouped by reason, the selected groups with their latest
state line, the empty-pack reason and the relevance gate.

Contract:
  * offline and read-only - it opens the ledger with read_only=True and
    neutralises the accounting touch, so one execution cannot write the ledger
    (the tests compare size/mtime/sha256 before and after);
  * no daemon and no model requirement: the optional embedder / vector index /
    entity index load exactly like the auto-context path, and any failure
    degrades to lexical-only retrieval;
  * human output and --json output are rendered from the same payload, so a
    script and a human see the same numbers.

Usage:
    python3 -m hippomind explain "为什么没注入"
    python3 -m hippomind explain --query "部署窗口" --json
    python3 -m hippomind explain "发布窗口" --scope shared --task orion-rollout
    python3 -m hippomind explain --trace 20
    python3 -m hippomind explain "候选" --trace 5 --agent codex

Exit codes: 0 for a normal run (an empty pack is a valid answer, not an error),
2 for usage/ledger errors (missing ledger, missing query, bad flags).
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Mapping, Tuple

from .. import injection_trace
from ..context_pack import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_TOKENS,
    ContextPackBuilder,
    ContextPackOptions,
)
from ..ledger import LedgerError, SQLiteLedger
from . import _paths
from .common import _print, _reject_restricted_direct

#: Same default as the daemon /context-pack endpoint and auto_context.
DEFAULT_MIN_SCORE = 0.20
#: Candidate rows shown in the payload (the debug list itself is capped at 32).
CANDIDATE_LIMIT = 20
#: Filtered samples per run (grouped counts cover the rest).
FILTERED_SAMPLES = 5
#: Fields worth keeping from an injection_trace record.
TRACE_FIELDS = ("at", "agent", "source", "query_hash", "query_chars", "items",
                "chars", "injected", "memory_ids", "arm", "arm_reason")
#: Score components shown in the compact human line (full dict stays in JSON).
SCORE_PART_HIGHLIGHTS = ("topic", "fts", "coverage", "phrase", "rrf", "time",
                         "weight", "confidence", "quality", "pinned")


def _scalar_stats(stats: Any) -> Dict[str, Any]:
    """Bounded, JSON-safe view of an optional index's stats object."""
    out: Dict[str, Any] = {}
    if not isinstance(stats, Mapping):
        return out
    for key, value in stats.items():
        name = str(key)[:64]
        if value is None or isinstance(value, (str, int, float, bool)):
            out[name] = value
        elif isinstance(value, (list, tuple)):
            out[name] = len(value)
        elif isinstance(value, Mapping):
            out[name] = len(value)
    return out


def _load_components(ledger: SQLiteLedger) -> Tuple[Any, Any, Any, Dict[str, Any]]:
    """Load optional retrieval channels exactly like auto_context does.

    Returns (embedder, vector_index, entity_index, report). Every failure is
    recorded in the report and never raised: explain must still work lexically
    on a machine without an embedding model.
    """
    report: Dict[str, Any] = {}
    embedder = None
    try:
        from ..semantic_space import load_embedder
        embedder = load_embedder()
    except Exception as exc:  # noqa: BLE001 - optional channel must not break explain
        report["embedder"] = {
            "state": "error", "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    else:
        if embedder is None:
            report["embedder"] = {"state": "unavailable"}
        else:
            report["embedder"] = {
                "state": "loaded",
                "model": str(getattr(embedder, "model_id", "") or type(embedder).__name__)[:120],
            }

    vector = None
    try:
        from ..auto_context import _load_vector_index
        vector = _load_vector_index(ledger, embedder)
    except Exception as exc:  # noqa: BLE001
        report["vector"] = {
            "state": "error", "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    else:
        if vector is None:
            report["vector"] = {"state": "unavailable"}
        else:
            try:
                stats = _scalar_stats(getattr(vector, "stats", lambda: {})())
            except Exception:  # noqa: BLE001
                stats = {}
            report["vector"] = {
                "state": "loaded",
                "granularity": str(getattr(vector, "granularity", ""))[:32],
                "stats": stats,
            }

    entity = None
    try:
        from ..entity_index import entity_channel_enabled
        entity_enabled = bool(entity_channel_enabled())
    except Exception:  # noqa: BLE001
        entity_enabled = False
    if not entity_enabled:
        report["entity"] = {"state": "disabled", "env": "HIPPOMIND_ENTITY_CHANNEL"}
    else:
        try:
            from ..auto_context import _load_entity_index
            entity = _load_entity_index(ledger)
        except Exception as exc:  # noqa: BLE001
            report["entity"] = {
                "state": "error", "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
        else:
            if entity is None:
                report["entity"] = {"state": "unavailable"}
            else:
                try:
                    stats = _scalar_stats(getattr(entity, "stats", lambda: {})())
                except Exception:  # noqa: BLE001
                    stats = {}
                report["entity"] = {"state": "loaded", "stats": stats}
    return embedder, vector, entity, report


def _candidate_rows(debug: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in list(debug.get("candidates") or [])[:CANDIDATE_LIMIT]:
        if not isinstance(item, Mapping):
            continue
        parts = item.get("score_parts")
        evidence = item.get("evidence")
        rows.append({
            "memory_id": str(item.get("memory_id", "")),
            "kind": str(item.get("kind", "")),
            "task_id": str(item.get("task_id", "")),
            "project": str(item.get("project", "")),
            "created_at": str(item.get("created_at", "")),
            "score": round(float(item.get("score", 0.0) or 0.0), 4),
            "score_parts": dict(parts) if isinstance(parts, Mapping) else {},
            "evidence": [str(ref)[:120] for ref in list(evidence or [])[:3]],
        })
    return rows


def _filtered_report(debug: Mapping[str, Any]) -> Dict[str, Any]:
    entries = [entry for entry in list(debug.get("filtered") or [])
               if isinstance(entry, Mapping)]
    counts: Dict[str, int] = {}
    samples: List[Dict[str, Any]] = []
    for entry in entries:
        reason = str(entry.get("reason") or "unknown")[:200]
        counts[reason] = counts.get(reason, 0) + 1
        if len(samples) < FILTERED_SAMPLES:
            samples.append({
                "memory_id": str(entry.get("memory_id", "")),
                "reason": reason,
                "score": round(float(entry.get("score", 0.0) or 0.0), 4),
            })
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return {
        "total": len(entries),
        "reasons": [{"reason": reason, "count": count} for reason, count in ordered],
        "samples": samples,
    }


def _selected_rows(debug: Mapping[str, Any], items: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    selected_debug = [entry for entry in list(debug.get("selected") or [])
                      if isinstance(entry, Mapping)]
    for index, entry in enumerate(selected_debug):
        memory_ids = [str(value) for value in list(entry.get("memory_ids") or [])]
        item = items[index] if index < len(items) and isinstance(items[index], Mapping) else {}
        latest: Dict[str, Any] = {}
        for field in ("progress", "blockers", "next_steps"):
            values = item.get(field)
            if isinstance(values, (list, tuple)) and values:
                latest = {"field": field,
                          "text": "；".join(str(value) for value in values)[:300]}
                break
        if not latest:
            compact = entry.get("compact")
            latest_meta = compact.get("latest") if isinstance(compact, Mapping) else {}
            if isinstance(latest_meta, Mapping):
                for field, detail in latest_meta.items():
                    if isinstance(detail, Mapping) and detail.get("memory_id"):
                        latest = {"field": str(field),
                                  "memory_id": str(detail["memory_id"]), "text": ""}
                        break
        parts = entry.get("score_parts")
        rows.append({
            "key": "group-%d" % (index + 1),
            "items": memory_ids,
            "memory_ids": memory_ids,
            "score": round(float(entry.get("score", 0.0) or 0.0), 4),
            "score_parts": dict(parts) if isinstance(parts, Mapping) else {},
            "latest": latest,
            "reason": str(entry.get("reason", "")),
        })
    return rows


def _empty_reason(debug: Mapping[str, Any], *, items: int, candidate_count: int,
                  min_score: float) -> str:
    if items:
        return ""
    raw = debug.get("empty_reason")
    if raw:
        return str(raw)
    if candidate_count <= 0:
        if min_score > 0:
            return ("没有候选：查询未命中任何带证据的记忆，或被 min_score=%.2f 门控丢弃"
                    % min_score)
        return "没有候选：查询未命中任何带证据的记忆"
    if not debug.get("selected"):
        return "候选未通过可靠性、去重或任务分组门槛"
    return "已选分组在预算裁剪后没有可渲染文本"


def _trace_payload(limit: int, agent: str) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    error = ""
    try:
        rows = injection_trace.recent(max(1, int(limit)))
    except Exception as exc:  # noqa: BLE001 - tracing must never break explain
        rows = []
        error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if agent and str(row.get("agent", "")) != agent:
            continue
        records.append({field: row.get(field) for field in TRACE_FIELDS if field in row})
    injected = sum(1 for row in records if row.get("injected"))
    payload: Dict[str, Any] = {
        "requested": int(limit),
        "returned": len(records),
        "agent_filter": agent,
        "injected": injected,
        "not_injected": len(records) - injected,
        "records": records,
    }
    if error:
        payload["error"] = error
    return payload


def _score_line(parts: Mapping[str, Any]) -> str:
    chunks = []
    for key in SCORE_PART_HIGHLIGHTS:
        if key in parts:
            value = parts[key]
            if isinstance(value, bool):
                chunks.append("%s=%s" % (key, "true" if value else "false"))
            elif isinstance(value, float):
                chunks.append("%s=%.3f" % (key, value))
            else:
                chunks.append("%s=%s" % (key, value))
    return " ".join(chunks)


def _render_human(payload: Mapping[str, Any]) -> str:
    lines: List[str] = []
    number = [0]

    def section(title: str) -> None:
        number[0] += 1
        lines.append("")
        lines.append("%d. %s" % (number[0], title))

    section("查询")
    lines.append("   query: %s" % (payload.get("query") or "(空)"))
    lines.append("   scope: %s   project: %s   task: %s   session_id: %s"
                 % (payload.get("scope", ""), payload.get("project", ""),
                    payload.get("task_id", ""), payload.get("session_id", "")))
    if not payload.get("explained"):
        lines.append("   （--trace only：未构建 Context Pack）")

    section("组件（与 auto_context 相同的可选加载；不可用时退化为纯词法）")
    for name in ("embedder", "vector", "entity"):
        detail = (payload.get("components") or {}).get(name) or {}
        suffix = ""
        if detail.get("model"):
            suffix = " model=%s" % detail["model"]
        elif detail.get("granularity"):
            suffix = " granularity=%s" % detail["granularity"]
        elif detail.get("stats"):
            suffix = " stats=%s" % detail["stats"]
        elif detail.get("env"):
            suffix = " env=%s" % detail["env"]
        if detail.get("error"):
            suffix = " error=%s" % detail["error"]
        lines.append("   %-8s %s%s" % (name, detail.get("state", "unknown"), suffix))

    if payload.get("explained"):
        candidates = payload.get("candidates") or []
        section("候选（%d 条，显示最多 %d 条）"
                % (int(payload.get("candidate_count", 0)), CANDIDATE_LIMIT))
        if not candidates:
            lines.append("   （无）")
        for index, row in enumerate(candidates[:10]):
            lines.append("   #%d score=%.4f memory_id=%s kind=%s task=%s project=%s created=%s"
                         % (index + 1, float(row.get("score", 0.0)), row.get("memory_id", ""),
                            row.get("kind", ""), row.get("task_id", ""),
                            row.get("project", ""), row.get("created_at", "")))
            score_line = _score_line(row.get("score_parts") or {})
            if score_line:
                lines.append("       %s" % score_line)
            if row.get("evidence"):
                lines.append("       evidence=%s" % row["evidence"])
        if len(candidates) > 10:
            lines.append("   ... 另有 %d 条" % (len(candidates) - 10))

        filtered = payload.get("filtered") or {}
        section("过滤（%d 条）" % int(filtered.get("total", 0)))
        if not filtered.get("total"):
            lines.append("   （无）")
        for entry in filtered.get("reasons") or []:
            lines.append("   %dx %s" % (int(entry.get("count", 0)), entry.get("reason", "")))
        for sample in filtered.get("samples") or []:
            lines.append("   样例 memory_id=%s score=%.4f reason=%s"
                         % (sample.get("memory_id", ""), float(sample.get("score", 0.0)),
                            sample.get("reason", "")))

        selected = payload.get("selected") or []
        section("选中（%d 组）" % len(selected))
        if not selected:
            lines.append("   （无）")
        for row in selected:
            lines.append("   %s score=%.4f items=%s"
                         % (row.get("key", ""), float(row.get("score", 0.0)),
                            row.get("items")))
            score_line = _score_line(row.get("score_parts") or {})
            if score_line:
                lines.append("       %s" % score_line)
            latest = row.get("latest") or {}
            if latest:
                if latest.get("text"):
                    lines.append("       最近状态行 %s: %s"
                                 % (latest.get("field", ""), latest.get("text")))
                else:
                    lines.append("       最近状态行 %s: memory_id=%s"
                                 % (latest.get("field", ""), latest.get("memory_id", "")))

        section("空包原因")
        if payload.get("empty_reason"):
            lines.append("   %s" % payload["empty_reason"])
        else:
            lines.append("   （未为空包）")

        gate = payload.get("gate") or {}
        section("门控")
        lines.append("   min_score=%.2f passed_candidates=%d"
                     % (float(gate.get("min_score", 0.0)),
                        int(gate.get("passed_candidates", 0))))

        fact_cards = payload.get("fact_cards")
        section("事实卡")
        if isinstance(fact_cards, Mapping):
            lines.append("   开启：count=%s chars=%s skipped_duplicates=%s"
                         % (fact_cards.get("count", 0), fact_cards.get("chars", 0),
                            fact_cards.get("skipped_duplicates", 0)))
        else:
            lines.append("   关闭（未开启 ContextPackOptions.fact_cards / HIPPOMIND_FACT_CARDS）")

        section("延迟")
        lines.append("   latency_ms=%.2f setup_ms=%.2f items=%d text_chars=%d"
                     % (float(payload.get("latency_ms", 0.0)),
                        float(payload.get("setup_ms", 0.0)),
                        int(payload.get("item_count", 0)),
                        int(payload.get("text_chars", 0))))

    trace = payload.get("trace")
    if isinstance(trace, Mapping):
        section("注入轨迹（最近 %d 条，agent=%s）"
                % (int(trace.get("requested", 0)), trace.get("agent_filter") or "全部"))
        if not trace.get("returned"):
            lines.append("   （无记录；injection_trace.recent 返回空）")
        for record in trace.get("records") or []:
            lines.append("   %s agent=%s source=%s injected=%s items=%s chars=%s"
                         % (record.get("at", ""), record.get("agent", ""),
                            record.get("source", ""),
                            "yes" if record.get("injected") else "no",
                            record.get("items", 0), record.get("chars", 0)))
    return "\n".join(lines).lstrip("\n")


def cmd_explain(args) -> int:
    _reject_restricted_direct(args, "explain")
    positional = str(getattr(args, "query", "") or "").strip()
    option_query = str(getattr(args, "query_text", "") or "").strip()
    if positional and option_query and positional != option_query:
        raise LedgerError("同时给出了位置参数 QUERY 与 --query；请只保留一个")
    query = option_query or positional
    trace_limit = max(0, int(getattr(args, "trace", 0) or 0))
    agent = str(getattr(args, "agent", "") or "").strip()
    if not query and trace_limit <= 0:
        raise LedgerError(
            "explain 需要 QUERY（位置参数或 --query），或用 --trace N 只看注入轨迹")
    scope = str(getattr(args, "scope", "shared") or "shared").strip() or "shared"
    project = str(getattr(args, "project", "") or "").strip()
    task_id = str(getattr(args, "task", "") or "").strip()
    session_id = str(getattr(args, "session_id", "") or "").strip()
    max_items = int(getattr(args, "max_items", 8) or 8)
    try:
        min_score = float(getattr(args, "min_score", DEFAULT_MIN_SCORE))
    except (TypeError, ValueError):
        raise LedgerError("--min-score 必须是数字")
    if not math.isfinite(min_score) or min_score < 0.0 or min_score > 1.0:
        raise LedgerError("--min-score 必须在 0.0 到 1.0 之间（有限数）")

    payload: Dict[str, Any] = {
        "query": query,
        "scope": scope,
        "project": project,
        "task_id": task_id,
        "session_id": session_id,
        "components": {},
        "candidates": [],
        "candidate_count": 0,
        "filtered": {"total": 0, "reasons": [], "samples": []},
        "selected": [],
        "empty_reason": "",
        "gate": {"min_score": round(min_score, 4), "passed_candidates": 0},
        "fact_cards": None,
        "latency_ms": 0.0,
        "setup_ms": 0.0,
        "item_count": 0,
        "text_chars": 0,
        "explained": bool(query),
    }

    if query:
        paths = _paths(args)
        if not paths.ledger_path.exists():
            raise LedgerError("账本不存在: %s" % paths.ledger_path)
        ledger = SQLiteLedger(paths.ledger_path, read_only=True)
        try:
            # The pack builder accounts recall through ledger.touch(); explain is a
            # read-only diagnostic, so neutralise that single write hook. The ledger
            # itself is opened read-only, so any other write would still fail loudly.
            ledger.touch = lambda *args, **kwargs: None  # type: ignore[assignment]
            setup_started = time.perf_counter()
            embedder, vector_index, entity_index, components = _load_components(ledger)
            try:
                from ..fact_cards import fact_cards_enabled
                fact_cards_opt = bool(fact_cards_enabled())
            except Exception:  # noqa: BLE001
                fact_cards_opt = False
            builder = ContextPackBuilder(
                ledger,
                embedder=embedder,
                vector_index=vector_index,
                entity_index=entity_index,
            )
            options = ContextPackOptions(
                max_items=max_items,
                max_chars=DEFAULT_MAX_CHARS,
                max_tokens=DEFAULT_MAX_TOKENS,
                raw_chat=False,
                require_evidence=True,
                scope=scope,
                project=project,
                task_id=task_id,
                session_id=session_id,
                debug=True,
                min_score=min_score,
                hybrid_rerank=True,
                fact_cards=fact_cards_opt,
            ).normalise()
            setup_ms = (time.perf_counter() - setup_started) * 1000.0
            build_started = time.perf_counter()
            pack = builder.build(query, options)
            latency_ms = (time.perf_counter() - build_started) * 1000.0
        finally:
            ledger.close()

        debug = pack.get("debug") if isinstance(pack.get("debug"), Mapping) else {}
        items = list(pack.get("items") or [])
        candidates = _candidate_rows(debug)
        candidate_count = int(debug.get("candidate_count") or len(debug.get("candidates") or []))
        payload.update({
            "components": components,
            "candidates": candidates,
            "candidate_count": candidate_count,
            "filtered": _filtered_report(debug),
            "selected": _selected_rows(debug, items),
            "empty_reason": _empty_reason(debug, items=len(items),
                                          candidate_count=candidate_count,
                                          min_score=min_score),
            "gate": {"min_score": round(min_score, 4),
                     "passed_candidates": candidate_count},
            "fact_cards": (dict(pack["fact_cards"]) if isinstance(pack.get("fact_cards"), Mapping)
                           else None),
            "latency_ms": round(latency_ms, 2),
            "setup_ms": round(setup_ms, 2),
            "item_count": len(items),
            "text_chars": len(str(pack.get("text") or "")),
            "entities": debug.get("entities") or {},
        })

    if trace_limit > 0:
        payload["trace"] = _trace_payload(trace_limit, agent)

    if bool(getattr(args, "json", False)):
        _print(payload)
    else:
        print(_render_human(payload))
    return 0
