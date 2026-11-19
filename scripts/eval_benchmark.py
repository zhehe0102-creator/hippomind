#!/usr/bin/env python3
"""Benchmark-style retrieval evaluation and quality gate for HippoMind.

The existing gates answer narrow questions.  scripts/eval_memory.py proves the
production ledger still retrieves its own labelled set; its fixtures were
harvested from that ledger, so they say little about capability dimensions.
scripts/eval_refusal.py proves questions with no evidence come back empty.
Neither is an external score.

This script adds a dimension-aligned benchmark:

* a self-contained synthetic fixture (tests/fixtures/retrieval/
  benchmark_zh_v1.json) that ships its own memories and queries, modelled on
  the capability dimensions of LongMemEval: fact recall, knowledge update,
  temporal reasoning, multi-session continuity and abstention.  It is authored
  in this repository and is NOT an official LongMemEval score;
* an adapter for a user-supplied official LongMemEval JSON/JSONL file, so the
  methodology can be replicated against the real dataset without bundling it
  (licence and size).  The supported record subset is documented in
  LONG_MEMEVAL_SCHEMA below and in --help;
* a committed baseline (tests/fixtures/retrieval/benchmark_baseline.json) that
  stores the measured numbers and conservative thresholds.  The default run is
  a gate: any metric below its floor, or fewer queries than the baseline
  recorded, exits non-zero.  This is what makes retrieval quality -- not just
  code correctness -- part of the release gate.

Metrics: overall and per-category recall@k, MRR, duplicate_rate, update_hit
(knowledge update: the new value is retrieved AND the old value is absent from
the top-k), abstain_accuracy (unrelated question gets an empty context pack),
temporal/continuity accuracy, and latency p95.

Usage:
    python3 scripts/eval_benchmark.py                 # gate the bundled fixture
    python3 scripts/eval_benchmark.py --json          # machine-readable detail
    python3 scripts/eval_benchmark.py --no-gate       # report only
    python3 scripts/eval_benchmark.py --update-baseline   # re-pin thresholds
    python3 scripts/eval_benchmark.py --format longmemeval --data FILE [--json]

LongMemEval data (NOT bundled here: licence and size):
    https://github.com/xiaowu0162/LongMemEval
    Download longmemeval_s.json (or the _m/_oracle variants) from the upstream
    release assets, then:
        python3 scripts/eval_benchmark.py --format longmemeval \\
            --data /path/to/longmemeval_s.json --json
    Grading is substring based, so only questions whose expected answer (or
    evidence span) literally appears in the supplied sessions are supported.
    Records outside that subset are rejected with their question_id instead of
    being silently guessed.

Exit codes: 0 = pass or report-only, 1 = quality gate failed, 2 = usage,
input or fixture/adapter schema error.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions  # noqa: E402
from hippomind.ledger import (  # noqa: E402
    LedgerError,
    SQLiteLedger,
    normalise_for_dedup,
)

DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "retrieval" / "benchmark_zh_v1.json"
DEFAULT_BASELINE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "retrieval" / "benchmark_baseline.json"
DEFAULT_K = 8
CATEGORIES = ("fact_recall", "knowledge_update", "temporal", "continuity", "abstention")
SEARCH_CATEGORIES = ("fact_recall", "knowledge_update", "temporal", "continuity")
CATEGORY_MODE = {"abstention": "pack"}
CONTEXT_FIELDS = ("goal", "progress", "key_decisions", "related_files", "blockers",
                  "next_steps", "preferences")

# Conservative ratchet margins used by --update-baseline: the committed floor is
# what was measured minus a small allowance, never what the code "should" do.
RECALL_MARGIN = 0.05
MRR_MARGIN = 0.10
ACCURACY_MARGIN = 0.10
DUPLICATE_MARGIN = 0.10
PER_CATEGORY_MARGIN = 0.10
P95_MULTIPLIER = 3.0
P95_MIN_FLOOR_MS = 50.0

LONG_MEMEVAL_SCHEMA = """\
Accepted LongMemEval subset (JSON array / object / JSONL).  Required per record:
  question_id    string, unique
  question       string
  memory source  exactly one of:
    haystack_sessions: list[list[turn]] where turn = {role, content}
                       (optional haystack_session_ids: list[str] of the same length)
    evidence:          list[str] (each string becomes one fact memory)
    memories:          list[str|object] (ingested as-is; strings become facts)
  expected-answer source (first match wins):
    expect:            list[str]  (explicit, substring expectations)
    evidence:          list[str]  (only when haystack_sessions is the memory source)
    answer:            non-empty string (substring expectation)
  optional:
    question_type      mapped onto the benchmark categories
    expect_absent      list[str] that must not appear in the top-k
    as_of / project / task_id / scope   passed through as retrieval filters
This adapter rejects any record whose expectations cannot be verified as
substrings of the supplied memory text, printing the question_id and this schema
instead of guessing."""

TYPE_TO_CATEGORY = {
    "single-session-user": "fact_recall",
    "single-session-assistant": "fact_recall",
    "single-session-preference": "fact_recall",
    "multi-session": "continuity",
    "temporal-reasoning": "temporal",
    "knowledge-update": "knowledge_update",
    "abstention": "abstention",
}

QUERY_KEYS = {
    "id", "category", "query", "expect", "expect_absent", "k", "mode",
    "min_score", "scope", "project", "task_id", "source_agents", "roles",
    "from_time", "to_time", "as_of", "include_raw_chat", "require_evidence",
    "note",
}
FILTER_KEYS = ("scope", "project", "task_id", "source_agents", "roles",
               "from_time", "to_time", "as_of", "include_raw_chat",
               "require_evidence")


class BenchmarkInputError(ValueError):
    """Usage/input/schema problem: exit code 2, never a silent partial run."""


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile (same convention as eval_memory.py)."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * min(1.0, max(0.0, float(fraction)))
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(float(value) for value in values) / float(len(values))


def _round(value: Any, digits: int = 4) -> float:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Fixture loading and validation
# ---------------------------------------------------------------------------

def render_hit(item: Mapping[str, Any]) -> str:
    """Searchable text of one ledger hit: content plus rendered context."""
    parts: List[str] = [str(item.get("content") or "")]
    context = item.get("context")
    if not isinstance(context, Mapping):
        metadata = item.get("metadata")
        context = metadata.get("context") if isinstance(metadata, Mapping) else None
    if isinstance(context, Mapping):
        for key in CONTEXT_FIELDS:
            value = context.get(key)
            if isinstance(value, (list, tuple)):
                parts.extend(str(entry) for entry in value)
            elif isinstance(value, str) and value:
                parts.append(value)
    return "\n".join(part for part in parts if part)


def _fixture_text(fixture: Mapping[str, Any]) -> str:
    """All searchable fixture memory text, for self-consistency checks."""
    blocks: List[str] = []
    for memory in fixture.get("memories") or []:
        if isinstance(memory, Mapping):
            blocks.append(render_hit(memory))
    return "\n".join(blocks).lower()


def _coerce_str_list(value: Any, field: str, query_id: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise BenchmarkInputError("query %s: %s 必须是字符串数组" % (query_id, field))
    result: List[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise BenchmarkInputError("query %s: %s 只能包含字符串" % (query_id, field))
        entry = entry.strip()
        if entry:
            result.append(entry)
    return result


def validate_fixture(fixture: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate shape and self-consistency; raise BenchmarkInputError on drift."""
    if not isinstance(fixture, Mapping):
        raise BenchmarkInputError("fixture 根节点必须是 JSON 对象")
    memories = fixture.get("memories")
    queries = fixture.get("queries")
    if not isinstance(memories, list) or not memories:
        raise BenchmarkInputError("fixture.memories 必须是非空数组")
    if not isinstance(queries, list) or not queries:
        raise BenchmarkInputError("fixture.queries 必须是非空数组")
    for index, memory in enumerate(memories):
        if not isinstance(memory, Mapping):
            raise BenchmarkInputError("memories[%d] 必须是对象" % index)
        if not str(memory.get("content") or "").strip():
            raise BenchmarkInputError("memories[%d].content 不能为空" % index)
        if not memory.get("evidence"):
            raise BenchmarkInputError("memories[%d].evidence 不能为空" % index)

    text = _fixture_text(fixture)
    seen_ids = set()
    category_counts: Dict[str, int] = {}
    normalised: List[Dict[str, Any]] = []
    for index, raw in enumerate(queries):
        if not isinstance(raw, Mapping):
            raise BenchmarkInputError("queries[%d] 必须是对象" % index)
        query_id = str(raw.get("id") or "").strip()
        if not query_id:
            raise BenchmarkInputError("queries[%d].id 不能为空" % index)
        if query_id in seen_ids:
            raise BenchmarkInputError("query id 重复: %s" % query_id)
        seen_ids.add(query_id)
        category = str(raw.get("category") or "")
        if category not in CATEGORIES:
            raise BenchmarkInputError(
                "query %s: category 必须是 %s 之一（实际: %r）"
                % (query_id, ", ".join(CATEGORIES), category))
        query = str(raw.get("query") or "").strip()
        if not query:
            raise BenchmarkInputError("query %s: query 不能为空" % query_id)
        unknown = sorted(set(raw) - QUERY_KEYS)
        if unknown:
            raise BenchmarkInputError("query %s: 未知字段 %s" % (query_id, unknown))
        expect = _coerce_str_list(raw.get("expect"), "expect", query_id)
        expect_absent = _coerce_str_list(raw.get("expect_absent"), "expect_absent", query_id)
        k_raw = raw.get("k", DEFAULT_K)
        try:
            k = int(k_raw)
        except (TypeError, ValueError):
            raise BenchmarkInputError("query %s: k 必须是整数" % query_id)
        if k < 1 or k > 64:
            raise BenchmarkInputError("query %s: k 必须在 1..64" % query_id)
        mode = str(raw.get("mode") or CATEGORY_MODE.get(category, "search"))
        if mode not in {"search", "pack"}:
            raise BenchmarkInputError("query %s: mode 必须是 search/pack" % query_id)
        if category == "abstention":
            if expect:
                raise BenchmarkInputError(
                    "query %s: abstention 类不应有 expect（弃权语义是空包）" % query_id)
            if not expect_absent:
                raise BenchmarkInputError(
                    "query %s: abstention 类必须给出 expect_absent 作为无关性自检词" % query_id)
            leaked = [term for term in expect_absent if term.lower() in text]
            if leaked:
                raise BenchmarkInputError(
                    "query %s: 弃权记忆池含无关性词 %s（fixture 自洽性失败）"
                    % (query_id, leaked))
        else:
            if not expect:
                raise BenchmarkInputError("query %s: expect 不能为空" % query_id)
            missing = [term for term in expect if term.lower() not in text]
            if missing:
                raise BenchmarkInputError(
                    "query %s: expect 未出现在任何 memory 中: %s（fixture 自洽性失败）"
                    % (query_id, missing))
        normalised.append({
            "id": query_id,
            "category": category,
            "query": query,
            "expect": expect,
            "expect_absent": expect_absent,
            "k": k,
            "mode": mode,
            **{key: raw[key] for key in FILTER_KEYS if key in raw},
            **({"min_score": _round(raw["min_score"], 4)} if "min_score" in raw else {}),
        })
        category_counts[category] = category_counts.get(category, 0) + 1

    declared = fixture.get("category_counts")
    if isinstance(declared, Mapping):
        for category, expected in declared.items():
            if int(expected) != category_counts.get(str(category), 0):
                raise BenchmarkInputError(
                    "fixture.category_counts 与实际不符: %s 声明 %s、实际 %s"
                    % (category, expected, category_counts.get(str(category), 0)))
    search_options = fixture.get("search_options") or {}
    if not isinstance(search_options, Mapping):
        raise BenchmarkInputError("fixture.search_options 必须是对象")
    for key in search_options:
        if key not in {"include_raw_chat", "pack_min_score", "require_evidence"}:
            raise BenchmarkInputError("fixture.search_options 未知字段: %s" % key)
    result = dict(fixture)
    result["queries"] = normalised
    result["category_counts"] = category_counts
    return result


def load_fixture(path: Any) -> Dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkInputError("无法读取 fixture %s: %s" % (path, exc))
    try:
        fixture = json.loads(raw)
    except ValueError as exc:
        raise BenchmarkInputError("fixture 不是合法 JSON（%s）: %s" % (path, exc))
    return validate_fixture(fixture)



# ---------------------------------------------------------------------------
# LongMemEval adapter
# ---------------------------------------------------------------------------

def _read_records(path: Any) -> List[Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkInputError("无法读取 --data 文件 %s: %s" % (path, exc))
    stripped = raw.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if data is not None:
            if isinstance(data, list):
                return data
            if isinstance(data, Mapping):
                for key in ("instances", "data", "questions", "records"):
                    if isinstance(data.get(key), list):
                        return list(data[key])
                if "question" in data:
                    return [data]
            raise BenchmarkInputError(
                "--data 的 JSON 顶层必须是数组或含 instances/data/questions 的对象")
    records: List[Any] = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError as exc:
            raise BenchmarkInputError(
                "--data 既不是 JSON 也不是合法 JSONL（第 %d 行）: %s" % (number, exc))
    if not records:
        raise BenchmarkInputError("--data 文件为空: %s" % path)
    return records


def _session_memories(record: Mapping[str, Any], question_id: str) -> List[Dict[str, Any]]:
    sessions = record.get("haystack_sessions")
    if not isinstance(sessions, list) or not sessions:
        raise BenchmarkInputError("question %s: haystack_sessions 必须是非空数组" % question_id)
    session_ids = record.get("haystack_session_ids")
    if session_ids is not None:
        if not isinstance(session_ids, list) or len(session_ids) != len(sessions):
            raise BenchmarkInputError(
                "question %s: haystack_session_ids 必须与 haystack_sessions 等长" % question_id)
    memories: List[Dict[str, Any]] = []
    for session_index, session in enumerate(sessions):
        if not isinstance(session, list) or not session:
            raise BenchmarkInputError(
                "question %s: haystack_sessions[%d] 必须是非空 turn 数组"
                % (question_id, session_index))
        session_id = str(session_ids[session_index]) if session_ids else "session-%d" % session_index
        for turn_index, turn in enumerate(session):
            if not isinstance(turn, Mapping):
                raise BenchmarkInputError(
                    "question %s: session %s turn[%d] 必须是 {role, content} 对象"
                    % (question_id, session_id, turn_index))
            content = str(turn.get("content") or "").strip()
            if not content:
                continue
            memories.append({
                "content": content,
                "kind": "raw_chat",
                "raw_chat": True,
                "scope": "shared",
                "source_agent": "longmemeval",
                "session_id": session_id[:256],
                "role": str(turn.get("role") or "")[:64],
                "evidence": ["longmemeval:%s:%s:%d" % (question_id, session_id, turn_index)],
            })
    if not memories:
        raise BenchmarkInputError(
            "question %s: haystack_sessions 中没有任何非空 content" % question_id)
    return memories


def _evidence_memories(entries: Sequence[Any], question_id: str) -> List[Dict[str, Any]]:
    memories: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, Mapping):
            record = dict(entry)
            record.setdefault("kind", "fact")
            record.setdefault("scope", "shared")
            record.setdefault("source_agent", "longmemeval")
            record.setdefault("evidence", ["longmemeval:%s:evidence:%d" % (question_id, index)])
            memories.append(record)
        elif isinstance(entry, str) and entry.strip():
            memories.append({
                "content": entry.strip(),
                "kind": "fact",
                "scope": "shared",
                "source_agent": "longmemeval",
                "evidence": ["longmemeval:%s:evidence:%d" % (question_id, index)],
            })
        else:
            raise BenchmarkInputError(
                "question %s: evidence[%d] 必须是字符串或 ingest 记录对象"
                % (question_id, index))
    if not memories:
        raise BenchmarkInputError("question %s: evidence 不能为空" % question_id)
    return memories


def _longmemeval_expected(record: Mapping[str, Any], question_id: str,
                          memory_source: str) -> List[str]:
    explicit = record.get("expect")
    if explicit is not None:
        expect = _coerce_str_list(explicit, "expect", question_id)
        if expect:
            return expect
    evidence = record.get("evidence")
    if memory_source == "haystack_sessions" and isinstance(evidence, list):
        spans = _coerce_str_list(evidence, "evidence", question_id)
        if spans:
            return spans
    answer = record.get("answer")
    if isinstance(answer, str) and answer.strip():
        return [answer.strip()]
    raise BenchmarkInputError(
        "question %s: 无法确定 expect（需要 expect/evidence/answer 之一）；\n%s"
        % (question_id, LONG_MEMEVAL_SCHEMA))


def load_longmemeval(path: Any, *, k: int = DEFAULT_K, limit: int = 0) -> Dict[str, Any]:
    """Convert the supported LongMemEval subset into the fixture schema."""
    records = _read_records(path)
    if limit and limit > 0:
        records = records[:limit]
    memories: List[Dict[str, Any]] = []
    queries: List[Dict[str, Any]] = []
    texts: List[str] = []
    errors: List[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            errors.append("record[%d]: 必须是 JSON 对象" % index)
            continue
        question_id = str(record.get("question_id") or "").strip()
        question = str(record.get("question") or "").strip()
        if not question_id or not question:
            errors.append("record[%d]: question_id/question 不能为空" % index)
            continue
        try:
            if isinstance(record.get("haystack_sessions"), list):
                source = "haystack_sessions"
                record_memories = _session_memories(record, question_id)
                memory_source = "haystack_sessions"
            elif isinstance(record.get("evidence"), list) and record.get("evidence"):
                source = "evidence"
                record_memories = _evidence_memories(record["evidence"], question_id)
                memory_source = "evidence"
            elif isinstance(record.get("memories"), list) and record.get("memories"):
                source = "memories"
                record_memories = _evidence_memories(record["memories"], question_id)
                memory_source = "memories"
            else:
                raise BenchmarkInputError(
                    "question %s: 需要 haystack_sessions 或 evidence/memories；\n%s"
                    % (question_id, LONG_MEMEVAL_SCHEMA))
            expect = _longmemeval_expected(record, question_id, memory_source)
            joined = "\n".join(render_hit(memory) for memory in record_memories).lower()
            missing = [term for term in expect if term.lower() not in joined]
            if missing:
                raise BenchmarkInputError(
                    "question %s: expect %s 在提供的记忆文本中找不到；本适配器只支持"
                    "可用子串验证的题目（%s 来源）" % (question_id, missing, source))
            expect_absent = _coerce_str_list(record.get("expect_absent"), "expect_absent", question_id)
            category = TYPE_TO_CATEGORY.get(str(record.get("question_type") or ""), "fact_recall")
            query: Dict[str, Any] = {
                "id": question_id,
                "category": category,
                "query": question,
                "expect": expect,
                "expect_absent": expect_absent,
                "k": k,
                "mode": CATEGORY_MODE.get(category, "search"),
            }
            for key in ("as_of", "project", "task_id", "scope"):
                if isinstance(record.get(key), str) and record[key].strip():
                    query[key] = record[key].strip()
            queries.append(query)
            memories.extend(record_memories)
            texts.append(joined)
        except BenchmarkInputError as exc:
            errors.append(str(exc))
    if errors:
        preview = "\n".join(errors[:10])
        more = "" if len(errors) <= 10 else "\n... 另有 %d 条错误" % (len(errors) - 10)
        raise BenchmarkInputError(
            "LongMemEval 适配失败（%d/%d 条记录）：\n%s%s\n\n%s"
            % (len(errors), len(records), preview, more, LONG_MEMEVAL_SCHEMA))
    if not queries:
        raise BenchmarkInputError("LongMemEval 文件没有可用记录: %s" % path)
    if len({str(query["id"]) for query in queries}) != len(queries):
        raise BenchmarkInputError("LongMemEval 的 question_id 必须唯一")
    return {
        "set_version": "longmemeval:%s" % Path(path).stem,
        "name": "LongMemEval subset (%s)" % Path(path).name,
        "note": "官方数据由调用方提供；本仓库不内置，分数仅对可子串验证的子集有效",
        "memories": memories,
        "queries": queries,
        "search_options": {"include_raw_chat": True},
        "category_counts": {},
    }


# ---------------------------------------------------------------------------
# Evaluation core
# ---------------------------------------------------------------------------

def _query_options(query: Mapping[str, Any], defaults: Mapping[str, Any]) -> Dict[str, Any]:
    options: Dict[str, Any] = {}
    for key in FILTER_KEYS:
        if key in query:
            options[key] = query[key]
    options["scope"] = str(options.get("scope", "shared"))
    options["include_raw_chat"] = bool(
        options.get("include_raw_chat", defaults.get("include_raw_chat", False)))
    options["require_evidence"] = bool(
        options.get("require_evidence", defaults.get("require_evidence", True)))
    return options


def _duplicate_count(texts: Sequence[str]) -> int:
    seen = set()
    duplicates = 0
    for text in texts:
        key = normalise_for_dedup(str(text))
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def _run_query(ledger: SQLiteLedger, builder: ContextPackBuilder,
               query: Mapping[str, Any], defaults: Mapping[str, Any]) -> Dict[str, Any]:
    started = time.perf_counter()
    category = str(query["category"])
    mode = str(query.get("mode") or CATEGORY_MODE.get(category, "search"))
    options = _query_options(query, defaults)
    expect = list(query["expect"])
    expect_absent = list(query["expect_absent"])
    k = int(query["k"])
    items: List[Any] = []
    hit_texts: List[str] = []
    pack_text = ""
    try:
        if mode == "search":
            hits = ledger.search(
                str(query["query"]), limit=k,
                scope=options["scope"], project=options.get("project", ""),
                task_id=options.get("task_id", ""),
                source_agents=options.get("source_agents"),
                roles=options.get("roles"),
                from_time=options.get("from_time", ""),
                to_time=options.get("to_time", ""),
                include_raw_chat=options["include_raw_chat"],
                require_evidence=options["require_evidence"],
                as_of=options.get("as_of", ""),
            )
            items = list(hits)
            hit_texts = [render_hit(hit) for hit in hits]
        else:
            pack_options = ContextPackOptions(
                max_items=k,
                min_score=float(query.get("min_score", defaults.get("pack_min_score", 0.20))),
                raw_chat=options["include_raw_chat"],
                require_evidence=options["require_evidence"],
                scope=options["scope"],
                project=options.get("project", ""),
                task_id=options.get("task_id", ""),
                from_time=options.get("from_time", ""),
                to_time=options.get("to_time", ""),
                as_of=options.get("as_of", ""),
                source_agents=tuple(options.get("source_agents") or ()),
                roles=tuple(options.get("roles") or ()),
            )
            pack = builder.build(str(query["query"]), pack_options)
            items = list(pack.get("items") or [])
            pack_text = str(pack.get("text") or "")
            hit_texts = [block for block in pack_text.split("\n\n") if block.strip()]
    except (LedgerError, ValueError) as exc:
        return {
            "id": str(query["id"]), "category": category, "mode": mode,
            "query": str(query["query"]), "expect": expect,
            "expect_absent": expect_absent, "passed": False, "expect_hit": False,
            "absents_found": list(expect_absent), "rank": 0,
            "reciprocal_rank": 0.0, "returned": 0, "duplicates": 0,
            "latency_ms": _round((time.perf_counter() - started) * 1000.0, 3),
            "top": [], "reason": "evaluation error: %s: %s" % (type(exc).__name__, exc),
        }
    latency_ms = (time.perf_counter() - started) * 1000.0
    joined = "\n".join(str(text or "").lower() for text in hit_texts)
    lowered_expect = [term.lower() for term in expect]
    lowered_absent = [term.lower() for term in expect_absent]
    if mode == "pack" and category != "abstention":
        joined = pack_text.lower()

    if category == "abstention":
        absents_found = [term for term in lowered_absent if term in pack_text.lower()]
        passed = (not items) and not absents_found
        expect_hit = not items
        rank = 0
        reciprocal_rank = 0.0
        reason = "" if passed else (
            "abstention 失败: pack 返回 %d 项%s"
            % (len(items), (", 泄漏词 %s" % absents_found) if absents_found else ""))
    else:
        missing = [term for term in lowered_expect if term not in joined]
        absents_found = [term for term in lowered_absent if term in joined]
        expect_hit = not missing
        passed = expect_hit and not absents_found
        rank = 0
        if lowered_expect:
            for index, text in enumerate(
                    [str(t or "").lower() for t in (hit_texts if mode == "search" else [pack_text])]):
                if lowered_expect[0] in text:
                    rank = index + 1
                    break
        reciprocal_rank = (1.0 / rank) if rank else 0.0
        problems: List[str] = []
        if missing:
            problems.append("expect 缺失 %s" % missing)
        if absents_found:
            problems.append("旧值/禁忌值仍在 top-k %s" % absents_found)
        reason = "; ".join(problems)

    top: List[Dict[str, Any]] = []
    if mode == "search":
        for hit in items[:5]:
            if isinstance(hit, Mapping):
                top.append({
                    "memory_id": str(hit.get("memory_id") or ""),
                    "snippet": str(render_hit(hit) or "")[:120],
                })
    else:
        for block in hit_texts[:5]:
            top.append({"memory_id": "", "snippet": str(block)[:120]})
    return {
        "id": str(query["id"]),
        "category": category,
        "mode": mode,
        "query": str(query["query"]),
        "expect": expect,
        "expect_absent": expect_absent,
        "passed": bool(passed),
        "expect_hit": bool(expect_hit),
        "absents_found": [term for term in expect_absent
                          if term.lower() in (joined if category != "abstention" else pack_text.lower())],
        "rank": rank,
        "reciprocal_rank": _round(reciprocal_rank, 4),
        "returned": len(items),
        "duplicates": _duplicate_count(hit_texts),
        "latency_ms": _round(latency_ms, 3),
        "top": top,
        "reason": reason,
    }


def _category_row(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    category = str(rows[0]["category"]) if rows else ""
    query_count = len(rows)
    returned = sum(int(row["returned"]) for row in rows)
    duplicates = sum(int(row["duplicates"]) for row in rows)
    row: Dict[str, Any] = {
        "queries": query_count,
        "accuracy": _round(_mean([1.0 if row["passed"] else 0.0 for row in rows])),
        "returned": returned,
        "duplicates": duplicates,
    }
    if category == "abstention":
        row["abstain_accuracy"] = row["accuracy"]
        row["recall_at_k"] = None
        row["mrr"] = None
    else:
        row["recall_at_k"] = _round(_mean([1.0 if row["expect_hit"] else 0.0 for row in rows]))
        row["mrr"] = _round(_mean([float(row["reciprocal_rank"]) for row in rows]))
        if category == "knowledge_update":
            row["update_hit"] = row["accuracy"]
    return row


def aggregate(per_query: Sequence[Mapping[str, Any]], k: int) -> Dict[str, Any]:
    rows = list(per_query)
    non_abstain = [row for row in rows if row["category"] != "abstention"]
    result: Dict[str, Any] = {
        "queries": len(rows),
        "k": k,
        "recall_at_k": _round(_mean([1.0 if row["expect_hit"] else 0.0 for row in non_abstain])),
        "mrr": _round(_mean([float(row["reciprocal_rank"]) for row in non_abstain])),
        "duplicate_rate": _round(
            sum(int(row["duplicates"]) for row in rows) /
            max(1, sum(int(row["returned"]) for row in rows))),
        "update_hit": _round(_mean([
            1.0 if row["passed"] else 0.0 for row in rows
            if row["category"] == "knowledge_update"])),
        "abstain_accuracy": _round(_mean([
            1.0 if row["passed"] else 0.0 for row in rows
            if row["category"] == "abstention"])),
        "p95_ms": _round(percentile([float(row["latency_ms"]) for row in rows], 0.95), 3),
        "per_category": {},
    }
    for category in CATEGORIES:
        category_rows = [row for row in rows if row["category"] == category]
        if category_rows:
            result["per_category"][category] = _category_row(category_rows)
    for category in SEARCH_CATEGORIES:
        row = result["per_category"].get(category)
        if row is not None:
            result["%s_accuracy" % category] = row["accuracy"]
    return result


def build_ledger(fixture: Mapping[str, Any], workdir: Path) -> SQLiteLedger:
    ledger = SQLiteLedger(workdir / "benchmark.sqlite")
    try:
        ledger.ingest_many(list(fixture["memories"]))
    except Exception:
        ledger.close()
        raise
    return ledger


def evaluate(fixture: Mapping[str, Any], *, k: int = 0) -> Dict[str, Any]:
    defaults = dict(fixture.get("search_options") or {})
    with tempfile.TemporaryDirectory(prefix="hippomind-benchmark-") as tmp:
        ledger = build_ledger(fixture, Path(tmp))
        try:
            builder = ContextPackBuilder(ledger)
            per_query = [
                _run_query(ledger, builder, query, defaults)
                for query in fixture["queries"]
            ]
        finally:
            try:
                ledger.close()
            except Exception:
                pass
    effective_k = int(k) if k else int(fixture["queries"][0].get("k", DEFAULT_K))
    return {
        "per_query": per_query,
        "measured": aggregate(per_query, effective_k),
    }


# ---------------------------------------------------------------------------
# Baseline + gate
# ---------------------------------------------------------------------------

def build_thresholds(measured: Mapping[str, Any]) -> Dict[str, Any]:
    per_category: Dict[str, float] = {}
    for category, row in (measured.get("per_category") or {}).items():
        recall = row.get("recall_at_k")
        if recall is None or category == "abstention":
            continue
        per_category[str(category)] = max(0.0, _round(float(recall) - PER_CATEGORY_MARGIN))
    return {
        "min_queries": int(measured.get("queries", 0)),
        "min_recall_at_k": max(0.0, _round(measured.get("recall_at_k", 0.0) - RECALL_MARGIN)),
        "min_mrr": max(0.0, _round(measured.get("mrr", 0.0) - MRR_MARGIN)),
        "max_duplicate_rate": _round(measured.get("duplicate_rate", 0.0) + DUPLICATE_MARGIN),
        "min_update_hit": max(0.0, _round(measured.get("update_hit", 0.0) - ACCURACY_MARGIN)),
        "min_abstain_accuracy": max(0.0, _round(measured.get("abstain_accuracy", 0.0) - ACCURACY_MARGIN)),
        "max_p95_ms": _round(max(P95_MIN_FLOOR_MS,
                                 float(measured.get("p95_ms", 0.0)) * P95_MULTIPLIER), 1),
        "per_category_min_recall": per_category,
    }


def load_baseline(path: Any) -> Dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkInputError(
            "无法读取 baseline %s: %s（先运行 --update-baseline 生成）" % (path, exc))
    try:
        baseline = json.loads(raw)
    except ValueError as exc:
        raise BenchmarkInputError("baseline 不是合法 JSON: %s" % exc)
    if not isinstance(baseline, Mapping) or not isinstance(baseline.get("thresholds"), Mapping):
        raise BenchmarkInputError("baseline 必须包含 thresholds 对象: %s" % path)
    return dict(baseline)


def write_baseline(path: Any, document: Mapping[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8")
    temporary.replace(destination)
    return destination


def gate_failures(measured: Mapping[str, Any], thresholds: Mapping[str, Any],
                  *, set_version: str, baseline_set_version: str, query_count: int) -> List[str]:
    failures: List[str] = []
    if baseline_set_version and set_version and baseline_set_version != set_version:
        failures.append(
            "set_version 不匹配: baseline=%r fixture=%r（fixture 变更后请 --update-baseline）"
            % (baseline_set_version, set_version))
    minimum = int(thresholds.get("min_queries", 0) or 0)
    if query_count < minimum:
        failures.append("queries %d < min_queries %d" % (query_count, minimum))
    checks: Tuple[Tuple[str, str], ...] = (
        ("recall_at_k", "min_recall_at_k"),
        ("mrr", "min_mrr"),
        ("update_hit", "min_update_hit"),
        ("abstain_accuracy", "min_abstain_accuracy"),
    )
    for metric, floor_key in checks:
        floor = thresholds.get(floor_key)
        if floor is None:
            continue
        value = float(measured.get(metric, 0.0) or 0.0)
        if value < float(floor):
            failures.append("%s %.4f < %s %.4f" % (metric, value, floor_key, float(floor)))
    ceiling = thresholds.get("max_duplicate_rate")
    if ceiling is not None and float(measured.get("duplicate_rate", 0.0)) > float(ceiling):
        failures.append("duplicate_rate %.4f > max_duplicate_rate %.4f"
                        % (float(measured.get("duplicate_rate", 0.0)), float(ceiling)))
    p95_ceiling = thresholds.get("max_p95_ms")
    if p95_ceiling is not None and float(measured.get("p95_ms", 0.0)) > float(p95_ceiling):
        failures.append("p95_ms %.3f > max_p95_ms %.1f"
                        % (float(measured.get("p95_ms", 0.0)), float(p95_ceiling)))
    per_category = thresholds.get("per_category_min_recall") or {}
    if isinstance(per_category, Mapping):
        for category, floor in per_category.items():
            row = (measured.get("per_category") or {}).get(str(category))
            if row is None or row.get("recall_at_k") is None:
                failures.append("per_category_min_recall: 缺少分类 %s 的测量值" % category)
                continue
            value = float(row["recall_at_k"])
            if value < float(floor):
                failures.append("category %s recall@k %.4f < %.4f" % (category, value, float(floor)))
    return failures


def query_failures(per_query: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    failures = []
    for row in per_query:
        if row.get("passed"):
            continue
        failures.append({
            "id": row["id"],
            "category": row["category"],
            "mode": row["mode"],
            "query": row["query"],
            "reason": row.get("reason") or "failed",
            "expect": row.get("expect"),
            "expect_absent": row.get("expect_absent"),
            "top": row.get("top"),
        })
    return failures


# ---------------------------------------------------------------------------
# Reporting + CLI
# ---------------------------------------------------------------------------

def render_human(payload: Mapping[str, Any]) -> str:
    measured = payload["measured"]
    lines = [
        "HippoMind retrieval benchmark: %s" % payload.get("set_version", ""),
        "  format: %s   queries: %s   k: %s"
        % (payload.get("format", ""), measured.get("queries"), measured.get("k")),
        "  %-16s %4s %10s %8s %8s" % ("category", "n", "recall@k", "MRR", "accuracy"),
    ]
    for category in CATEGORIES:
        row = (measured.get("per_category") or {}).get(category)
        if not row:
            continue
        lines.append("  %-16s %4d %10s %8s %8s" % (
            category, int(row["queries"]),
            "-" if row.get("recall_at_k") is None else "%.3f" % float(row["recall_at_k"]),
            "-" if row.get("mrr") is None else "%.3f" % float(row["mrr"]),
            "%.3f" % float(row["accuracy"])))
    lines.append(
        "  overall: recall@k=%.3f mrr=%.3f duplicate_rate=%.3f update_hit=%.3f "
        "abstain_accuracy=%.3f p95=%.1fms" % (
            float(measured.get("recall_at_k", 0.0)),
            float(measured.get("mrr", 0.0)),
            float(measured.get("duplicate_rate", 0.0)),
            float(measured.get("update_hit", 0.0)),
            float(measured.get("abstain_accuracy", 0.0)),
            float(measured.get("p95_ms", 0.0))))
    gate = payload.get("gate") or {}
    if gate.get("enabled"):
        lines.append("  gate: %s (%s)" % ("PASS" if gate.get("passed") else "FAIL",
                                          gate.get("baseline", "")))
        for failure in gate.get("failures") or []:
            lines.append("    - %s" % failure)
    else:
        lines.append("  gate: disabled")
    failures = payload.get("failures") or []
    if failures:
        lines.append("  failing queries (%d):" % len(failures))
        for failure in failures[:20]:
            lines.append("    - %s [%s] %s" % (failure["id"], failure["category"], failure["reason"]))
        if len(failures) > 20:
            lines.append("    ... 另有 %d 条" % (len(failures) - 20))
    if payload.get("baseline_written"):
        lines.append("  baseline written -> %s" % payload["baseline_written"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HippoMind retrieval benchmark and quality gate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=LONG_MEMEVAL_SCHEMA + """

LongMemEval acquisition (data is not bundled: licence and size):
  https://github.com/xiaowu0162/LongMemEval
  Download longmemeval_s.json (or _m/_oracle) from the upstream release assets:
    python3 scripts/eval_benchmark.py --format longmemeval --data FILE --json
""",
    )
    parser.add_argument("--format", choices=("fixture", "longmemeval"), default="fixture")
    parser.add_argument("--data", default="", help="LongMemEval JSON/JSONL file (with --format longmemeval)")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--baseline", default=None,
                        help="baseline JSON (default: the committed fixture baseline)")
    parser.add_argument("--k", type=int, default=0, help="top-k override (0 = per-query/default 8)")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    parser.add_argument("--update-baseline", action="store_true",
                        help="re-pin thresholds from this run (measured - margin)")
    parser.add_argument("--no-gate", action="store_true", help="report only, always exit 0")
    parser.add_argument("--gate", action="store_true",
                        help="for --format longmemeval: compare against --baseline too")
    parser.add_argument("--limit", type=int, default=0, help="LongMemEval: cap instances")
    return parser


def _error(message: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    else:
        sys.stderr.write("eval_benchmark: %s\n" % message)
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    as_json = bool(args.json)
    if args.format == "longmemeval" and not args.data:
        return _error("--format longmemeval 需要 --data <file>", as_json)
    if args.format == "fixture" and args.data:
        return _error("--data 只能与 --format longmemeval 一起使用", as_json)
    if args.update_baseline and args.format != "fixture":
        return _error("--update-baseline 只适用于内置 fixture（官方数据没有可复用基线）", as_json)
    try:
        if args.format == "longmemeval":
            fixture = load_longmemeval(args.data, k=args.k or DEFAULT_K, limit=args.limit)
            baseline_path: Optional[Path] = Path(args.baseline) if args.baseline else None
        else:
            fixture = load_fixture(Path(args.fixture))
            baseline_path = Path(args.baseline) if args.baseline else DEFAULT_BASELINE
    except BenchmarkInputError as exc:
        return _error(str(exc), as_json)

    try:
        result = evaluate(fixture, k=args.k)
    except (BenchmarkInputError, LedgerError) as exc:
        return _error("fixture 建账本/评测失败: %s: %s" % (type(exc).__name__, exc), as_json)
    measured = result["measured"]
    per_query = result["per_query"]
    set_version = str(fixture.get("set_version") or fixture.get("name") or args.fixture)

    baseline_written: Optional[str] = None
    thresholds: Dict[str, Any] = {}
    if args.update_baseline:
        thresholds = build_thresholds(measured)
        document = {
            "set_version": set_version,
            "measured": measured,
            "thresholds": thresholds,
        }
        destination = Path(args.baseline) if args.baseline else DEFAULT_BASELINE
        try:
            baseline_written = str(write_baseline(destination, document))
        except OSError as exc:
            return _error("写入 baseline 失败: %s" % exc, as_json)
        thresholds = document["thresholds"]

    gate_enabled = False
    gate_passed = True
    gate_reasons: List[str] = []
    baseline_info: Dict[str, Any] = {}
    if not args.update_baseline and not args.no_gate:
        if args.format == "fixture" or args.gate:
            if baseline_path is None:
                return _error("--format longmemeval --gate 需要显式 --baseline <file>", as_json)
            try:
                baseline = load_baseline(baseline_path)
            except BenchmarkInputError as exc:
                return _error(str(exc), as_json)
            gate_enabled = True
            thresholds = dict(baseline.get("thresholds") or {})
            gate_reasons = gate_failures(
                measured, thresholds,
                set_version=set_version,
                baseline_set_version=str(baseline.get("set_version") or ""),
                query_count=int(measured.get("queries", 0)))
            gate_passed = not gate_reasons
            baseline_info = {"path": str(baseline_path),
                             "set_version": baseline.get("set_version", "")}

    payload: Dict[str, Any] = {
        "ok": (not gate_enabled) or gate_passed,
        "format": args.format,
        "set_version": set_version,
        "fixture": str(args.fixture) if args.format == "fixture" else str(args.data),
        "measured": measured,
        "per_query": per_query,
        "failures": query_failures(per_query),
        "gate": {
            "enabled": gate_enabled,
            "passed": gate_passed,
            "baseline": baseline_info.get("path", ""),
            "baseline_set_version": baseline_info.get("set_version", ""),
            "thresholds": thresholds,
            "failures": gate_reasons,
        },
        "baseline_written": baseline_written,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_human(payload))
    if args.update_baseline:
        return 0
    return 1 if (gate_enabled and not gate_passed) else 0


if __name__ == "__main__":
    sys.exit(main())
