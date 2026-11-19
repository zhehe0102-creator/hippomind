#!/usr/bin/env python3
"""Injection-effect A/B for HippoMind.

Measures whether putting the *relevant* Context Pack in front of a model changes
its answers, against two baselines:
  treatment : the query's own pack
  placebo   : another query's pack (deterministic permutation, seed 42)
  control   : empty memory block
  gold      : optional oracle pack (fixture memories that contain the expected
              evidence), only for non-abstention queries

Scoring is deterministic: an answer is correct when every fixture "expect"
substring appears in it; abstention queries are correct when the model prints
UNKNOWN. No LLM judge is involved.

The harness never touches a real ledger or a real agent config; it builds a
throwaway ledger from the self-contained retrieval fixture and calls the model
with --bare so installed hooks cannot contaminate the control arm.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import eval_benchmark as eb  # noqa: E402

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions  # noqa: E402

PROMPT = """你只根据下面的记忆回答问题。如果记忆里没有答案，只输出 UNKNOWN。用一句话回答。

<记忆>
{pack}
</记忆>

问题：{query}
"""

ABSTAIN_MARK = "UNKNOWN"
MODEL_CMD = [
    "claude", "-p", "--bare", "--tools", "", "--strict-mcp-config",
    "--no-session-persistence", "--output-format", "json",
]


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


def wilson(success: int, total: int) -> List[float]:
    if total <= 0:
        return [0.0, 0.0]
    p = success / total
    z = 1.959963984540054
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    span = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return [round(max(0.0, centre - span), 4), round(min(1.0, centre + span), 4)]


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p for discordant counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2.0 ** n)
    return round(min(1.0, 2.0 * tail), 6)


def call_model(prompt: str, timeout: float) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            MODEL_CMD, input=prompt, capture_output=True, text=True,
            timeout=timeout, cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    stdout = proc.stdout or ""
    if proc.returncode != 0:
        return {"ok": False, "error": "exit_%s: %s" % (proc.returncode, (proc.stderr or stdout)[:200]),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}\s*$", stdout, re.S)
        if not match:
            return {"ok": False, "error": "unparsable: %s" % stdout[:200],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
        payload = json.loads(match.group(0))
    answer = str(payload.get("result") or "")
    usage = payload.get("usage") or {}
    return {
        "ok": True,
        "answer": answer,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "cost_usd": float(payload.get("total_cost_usd") or 0.0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "model": ",".join((payload.get("modelUsage") or {}).keys()),
    }


def build_packs(fixture: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    tmpdir = tempfile.mkdtemp(prefix="hippo-ab-")
    ledger = eb.build_ledger(fixture, Path(tmpdir))
    builder = ContextPackBuilder(ledger)
    defaults = dict(fixture.get("search_options") or {})
    packs: Dict[str, Dict[str, Any]] = {}
    try:
        for query in fixture["queries"]:
            options = eb._query_options(query, defaults)
            q = dict(query)
            pack_options = ContextPackOptions(
                max_items=int(q.get("k", 8)),
                min_score=float(q.get("min_score", defaults.get("pack_min_score", 0.20))),
                raw_chat=bool(options["include_raw_chat"]),
                require_evidence=bool(options["require_evidence"]),
                scope=str(options["scope"]),
                project=str(options.get("project", "")),
                task_id=str(options.get("task_id", "")),
                from_time=str(options.get("from_time", "")),
                to_time=str(options.get("to_time", "")),
                as_of=str(options.get("as_of", "")),
                source_agents=tuple(options.get("source_agents") or ()),
                roles=tuple(options.get("roles") or ()),
            )
            pack = builder.build(str(q["query"]), pack_options)
            packs[str(q["id"])] = {
                "text": str(pack.get("text") or ""),
                "items": len(list(pack.get("items") or [])),
                "query": q,
            }
    finally:
        try:
            ledger.close()
        except Exception:
            pass
    return packs


def gold_pack(fixture: Mapping[str, Any], query: Mapping[str, Any]) -> str:
    if not query.get("expect"):
        return ""
    hits: List[str] = []
    for memory in fixture.get("memories") or []:
        content = str(memory.get("content") if isinstance(memory, Mapping) else memory)
        lowered = content.lower()
        if any(term.lower() in lowered for term in query["expect"]):
            if content not in hits:
                hits.append(content)
    return "\n\n".join(hits)


def retrieval_hit(pack_text: str, query: Mapping[str, Any]) -> Optional[bool]:
    """True when the pack carries the evidence needed to answer correctly."""
    expect = list(query.get("expect") or [])
    absent = list(query.get("expect_absent") or [])
    lowered = pack_text.lower()
    if not expect:
        return not any(term.lower() in lowered for term in absent)
    return all(term.lower() in lowered for term in expect)


def score(answer: str, query: Mapping[str, Any]) -> Dict[str, Any]:
    norm = normalise(answer)
    expect = list(query.get("expect") or [])
    if expect:
        correct = all(term.lower() in norm.lower() for term in expect)
        return {
            "correct": bool(correct),
            "abstained": False,
            "strict_unknown": ABSTAIN_MARK in norm.upper(),
        }
    unknown = ABSTAIN_MARK in norm.upper()
    return {"correct": bool(unknown), "abstained": bool(unknown), "strict_unknown": bool(unknown)}


def build_tasks(fixture: Mapping[str, Any], packs: Mapping[str, Dict[str, Any]],
                arms: Sequence[str], seed: int) -> List[Dict[str, Any]]:
    queries = [dict(q) for q in fixture["queries"]]
    ids = [str(q["id"]) for q in queries]
    place_map: Dict[str, str] = {}
    rng = random.Random(seed)
    pool = list(ids)
    rng.shuffle(pool)
    for index, qid in enumerate(ids):
        for offset in range(1, len(pool)):
            candidate = pool[(index + offset) % len(pool)]
            if candidate != qid:
                place_map[qid] = candidate
                break
    tasks: List[Dict[str, Any]] = []
    for query in queries:
        qid = str(query["id"])
        bodies = {
            "treatment": packs[qid]["text"],
            "placebo": packs[place_map[qid]]["text"] if place_map.get(qid) else "",
            "control": "",
            "gold": gold_pack(fixture, query),
        }
        for arm in arms:
            body = bodies.get(arm)
            if body is None:
                continue
            text = PROMPT.format(pack=body if body else "（无）", query=query["query"])
            tasks.append({
                "query_id": qid,
                "category": str(query["category"]),
                "arm": arm,
                "query": query["query"],
                "expect": list(query.get("expect") or []),
                "expect_absent": list(query.get("expect_absent") or []),
                "pack_text": body or "",
                "pack_items": packs[qid]["items"] if arm != "placebo" else packs[place_map[qid]]["items"],
                "prompt": text,
                "retrieval_hit": retrieval_hit(body or "", query),
            })
    return tasks


def load_done(path: Path) -> set:
    done = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not row.get("error"):
            done.add((row.get("query_id"), row.get("arm")))
    return done


def run_tasks(tasks: Sequence[Dict[str, Any]], args) -> List[Dict[str, Any]]:
    done = load_done(Path(args.raw)) if args.resume else set()
    pending = [task for task in tasks if (task["query_id"], task["arm"]) not in done]
    lock = threading.Lock()
    results: List[Dict[str, Any]] = []
    raw_path = Path(args.raw)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    def one(task: Dict[str, Any]) -> Dict[str, Any]:
        outcome = call_model(task["prompt"], timeout=args.timeout)
        row = {
            "query_id": task["query_id"],
            "category": task["category"],
            "arm": task["arm"],
            "query": task["query"],
            "answer": outcome.get("answer", ""),
            "latency_ms": outcome.get("latency_ms", 0.0),
            "cost_usd": outcome.get("cost_usd", 0.0),
            "input_tokens": outcome.get("input_tokens", 0),
            "output_tokens": outcome.get("output_tokens", 0),
            "error": outcome.get("error", ""),
            **score(outcome.get("answer", ""), task),
            "retrieval_hit": task["retrieval_hit"],
            "pack_chars": len(task["pack_text"]),
            "pack_items": task["pack_items"],
            "model": outcome.get("model", ""),
        }
        with lock:
            with raw_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    if pending:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(one, task): task for task in pending}
            for future in as_completed(futures):
                row = future.result()
                results.append(row)
                if len(results) % 24 == 0:
                    print("progress %d/%d" % (len(results), len(pending)), file=sys.stderr, flush=True)
    return results


def read_raw(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def arm_summary(rows: Sequence[Dict[str, Any]], arm: str, n_expected: int) -> Dict[str, Any]:
    arm_rows = [row for row in rows if row.get("arm") == arm]
    ok = [row for row in arm_rows if not row.get("error")]
    correct = sum(1 for row in ok if row.get("correct"))
    total = len(ok)
    return {
        "n": len(arm_rows),
        "n_ok": total,
        "n_expected": n_expected,
        "completion": round(total / n_expected, 4) if n_expected else None,
        "accuracy": round(correct / total, 4) if total else None,
        "correct": correct,
        "wilson95": wilson(correct, total),
        "retrieval_coverage": round(
            sum(1 for row in ok if row.get("retrieval_hit")) / total, 4) if total else None,
        "abstain_rate": round(sum(1 for row in ok if row.get("abstained")) / total, 4) if total else None,
        "errors": len(arm_rows) - total,
        "latency_p50_ms": round(sorted(row["latency_ms"] for row in ok)[len(ok) // 2], 1) if ok else None,
        "mean_cost_usd": round(sum(row.get("cost_usd") or 0 for row in ok) / total, 5) if total else None,
        "total_cost_usd": round(sum(row.get("cost_usd") or 0 for row in ok), 4),
        "mean_answer_chars": round(sum(len(str(row.get("answer") or "")) for row in ok) / total, 1) if total else None,
        "mean_pack_chars": round(sum(row.get("pack_chars") or 0 for row in ok) / total, 1) if total else None,
    }


def paired(left_rows: Sequence[Dict[str, Any]], right_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    left = {row["query_id"]: row for row in left_rows if not row.get("error")}
    right = {row["query_id"]: row for row in right_rows if not row.get("error")}
    pairs = sorted(set(left) & set(right))
    b = sum(1 for qid in pairs if left[qid].get("correct") and not right[qid].get("correct"))
    c = sum(1 for qid in pairs if not left[qid].get("correct") and right[qid].get("correct"))
    both = sum(1 for qid in pairs if left[qid].get("correct") and right[qid].get("correct"))
    neither = len(pairs) - b - c - both
    return {
        "pairs": len(pairs),
        "both_correct": both,
        "neither_correct": neither,
        "b_left_only": b,
        "c_right_only": c,
        "diff_pp": round((b - c) / len(pairs) * 100, 2) if pairs else 0.0,
        "mcnemar_p": mcnemar_exact(b, c),
    }


def per_category(rows: Sequence[Dict[str, Any]], arms: Sequence[str],
                 queries: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for query in queries:
        category = str(query["category"])
        entry = out.setdefault(category, {"n_queries": 0})
        entry["n_queries"] += 1
        for arm in arms:
            arm_rows = [row for row in rows if row.get("arm") == arm
                        and row.get("category") == category and not row.get("error")]
            entry.setdefault(arm, {"n": 0, "correct": 0})
            entry[arm]["n"] += len(arm_rows)
            entry[arm]["correct"] += sum(1 for row in arm_rows if row.get("correct"))
    for category, entry in out.items():
        for arm in arms:
            cell = entry.get(arm) or {"n": 0, "correct": 0}
            cell["accuracy"] = round(cell["correct"] / cell["n"], 4) if cell["n"] else None
    return out


def decide(arms: Mapping[str, Dict[str, Any]], pairs: Mapping[str, Dict[str, Any]],
           completed: bool) -> Dict[str, Any]:
    if not completed:
        return {"code": "degraded", "reason": "completion below 95%",
                "next_action": "rerun missing calls once, then report degraded"}
    treatment = arms.get("treatment") or {}
    control = arms.get("control") or {}
    placebo = arms.get("placebo") or {}
    t = treatment.get("accuracy")
    c = control.get("accuracy")
    p = placebo.get("accuracy")
    if t is None or c is None:
        return {"code": "degraded", "reason": "missing treatment or control arm",
                "next_action": "collect both arms"}
    diff = t - c
    vs_control = pairs.get("treatment_vs_control") or {}
    vs_placebo = pairs.get("treatment_vs_placebo") or {}
    if diff >= 0.30 and vs_control.get("mcnemar_p", 1.0) < 0.01 and t >= 0.80:
        placebo_gain = (t - p) if p is not None else 0.0
        placebo_p = (vs_placebo.get("mcnemar_p", 1.0) if p is not None else 1.0)
        if placebo_gain >= 0.15 and placebo_p < 0.05:
            return {"code": "A",
                    "reason": "relevant memory raises accuracy %.1fpp over control and %.1fpp over placebo" % (diff * 100, placebo_gain * 100),
                    "next_action": "keep funding: fix production daemon, then collect real-traffic samples"}
        return {"code": "B",
                "reason": "accuracy rises %.1fpp over control but only %.1fpp over placebo; extra text may explain the gain" % (diff * 100, placebo_gain * 100),
                "next_action": "tighten relevance gating / pack compression before funding again"}
    if diff >= 0.10:
        return {"code": "C",
                "reason": "weak signal: %.1fpp over control (McNemar p=%.4f), underpowered at n=48" % (diff * 100, vs_control.get("mcnemar_p", 1.0)),
                "next_action": "extend to n=96 with a holdout fixture before deciding"}
    return {"code": "D",
            "reason": "no measurable benefit: %.1fpp over control (McNemar p=%.4f)" % (diff * 100, vs_control.get("mcnemar_p", 1.0)),
            "next_action": "stop this experiment; redirect budget to legacy dedupe cleanup and a real task suite"}


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Injection-effect A/B - %s" % report["run_id"])
    lines.append("")
    lines.append("Fixture: %s (set_version=%s, %d queries); model: %s; k=%d; seed=%d." %
                 (report["fixture"], report["fixture_set_version"], report["n_queries"],
                  report["model"], report["k"], report["seed"]))
    lines.append("")
    lines.append("## Arms")
    lines.append("")
    lines.append("| arm | n_ok | accuracy | Wilson 95% | retrieval coverage | abstain rate | p50 ms | pack chars | cost USD |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for arm in report["arms_order"]:
        row = report["arms"].get(arm) or {}
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            arm, row.get("n_ok"), row.get("accuracy"), row.get("wilson95"),
            row.get("retrieval_coverage"), row.get("abstain_rate"),
            row.get("latency_p50_ms"), row.get("mean_pack_chars"), row.get("total_cost_usd")))
    lines.append("")
    lines.append("## Paired comparisons")
    lines.append("")
    lines.append("| pair | pairs | both correct | left only | right only | diff (pp) | McNemar p |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for name, pair in report["paired"].items():
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            name, pair.get("pairs"), pair.get("both_correct"), pair.get("b_left_only"),
            pair.get("c_right_only"), pair.get("diff_pp"), pair.get("mcnemar_p")))
    lines.append("")
    lines.append("## Per category")
    lines.append("")
    header = "| category | queries | " + " | ".join(report["arms_order"]) + " |"
    lines.append(header)
    lines.append("| --- | --- | " + " | ".join("---" for _ in report["arms_order"]) + " |")
    for category, entry in sorted(report["per_category"].items()):
        cells = []
        for arm in report["arms_order"]:
            cell = entry.get(arm) or {}
            cells.append(str(cell.get("accuracy")))
        lines.append("| %s | %s | %s |" % (category, entry.get("n_queries"), " | ".join(cells)))
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    verdict = report["verdict"]
    lines.append("- code: **%s**" % verdict.get("code"))
    lines.append("- reason: %s" % verdict.get("reason"))
    lines.append("- next action: %s" % verdict.get("next_action"))
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append("- Synthetic, self-contained fixture (fictional facts); measures whether relevant context helps one model answer, not real user task gain.")
    lines.append("- One model (%s), one fixed prompt, no temperature control." % report["model"])
    lines.append("- N=48 per arm; abstention queries have no evidence to retrieve, so treatment equals control there by construction.")
    lines.append("- Placebo is another query's real pack, not random text.")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="测注入对模型答案的因果增益（确定性评分）")
    parser.add_argument("--fixture", default=str(ROOT / "tests/fixtures/retrieval/benchmark_zh_v1.json"))
    parser.add_argument("--arms", default="treatment,placebo,control")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--raw", default="output/eval/injection-effect/raw.jsonl")
    parser.add_argument("--out", default="output/eval/injection-effect/report.json")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-model", action="store_true", help="仅测检索覆盖率，不调用模型（降级路径）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个 query（smoke）")
    parser.add_argument("--run-id", default="injection-effect")
    args = parser.parse_args(argv)
    arms = [arm.strip() for arm in args.arms.split(",") if arm.strip()]

    fixture = eb.load_fixture(args.fixture)
    queries = list(fixture["queries"])
    if args.limit:
        queries = queries[:args.limit]
        fixture = dict(fixture, queries=queries)
    packs = build_packs(fixture)
    tasks = build_tasks(fixture, packs, arms, args.seed)

    if args.dry_run:
        non_abstain = [task for task in tasks if task["arm"] == "treatment" and task["expect"]]
        coverage = sum(1 for task in non_abstain if task["retrieval_hit"]) / len(non_abstain)
        control_empty = all(len(task["pack_text"]) == 0 for task in tasks if task["arm"] == "control")
        placebo_nonempty = all(len(task["pack_text"]) > 0 for task in tasks if task["arm"] == "placebo")
        treatment_nonempty = all(len(task["pack_text"]) > 0 for task in tasks
                                 if task["arm"] == "treatment" and task["expect"])
        report = {
            "tasks": len(tasks), "queries": len(queries),
            "treatment_retrieval_coverage": round(coverage, 4),
            "control_packs_empty": control_empty,
            "placebo_packs_nonempty": placebo_nonempty,
            "treatment_packs_nonempty": treatment_nonempty,
            "empty_treatment_packs": [task["query_id"] for task in tasks
                                       if task["arm"] == "treatment" and not task["pack_text"]],
            "treatment_coverage_misses": [task["query_id"] for task in non_abstain
                                           if not task["retrieval_hit"]],
            "fixture_set_version": fixture.get("set_version"),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        ok = coverage >= 0.90 and control_empty and placebo_nonempty and treatment_nonempty
        return 0 if ok else 1

    if args.no_model:
        rows = []
        for task in tasks:
            rows.append({
                "query_id": task["query_id"], "category": task["category"], "arm": task["arm"],
                "query": task["query"], "correct": bool(task["retrieval_hit"]),
                "abstained": False, "retrieval_hit": task["retrieval_hit"],
                "pack_chars": len(task["pack_text"]), "pack_items": task["pack_items"],
                "latency_ms": 0.0, "cost_usd": 0.0, "error": "", "model": "none",
            })
    else:
        run_tasks(tasks, args)
        rows = read_raw(Path(args.raw))

    n_queries = len(queries)
    arms_summary = {arm: arm_summary(rows, arm, n_queries) for arm in arms}
    by_arm = {arm: [row for row in rows if row.get("arm") == arm] for arm in arms}
    pairs = {}
    if "treatment" in arms and "control" in arms:
        pairs["treatment_vs_control"] = paired(by_arm["treatment"], by_arm["control"])
    if "treatment" in arms and "placebo" in arms:
        pairs["treatment_vs_placebo"] = paired(by_arm["treatment"], by_arm["placebo"])
    if "gold" in arms and "treatment" in arms:
        pairs["gold_vs_treatment"] = paired(by_arm["gold"], by_arm["treatment"])

    expected_calls = len(tasks)
    ok_calls = sum(1 for row in rows if not row.get("error"))
    completion = ok_calls / expected_calls if expected_calls else 0.0
    verdict = decide(arms_summary, pairs, completion >= 0.95)
    model = "unknown"
    for row in rows:
        if row.get("model"):
            model = str(row["model"])
            break

    report = {
        "run_id": args.run_id,
        "fixture": str(args.fixture),
        "fixture_set_version": fixture.get("set_version"),
        "model": model,
        "n_queries": n_queries,
        "k": int(queries[0].get("k", 8)),
        "seed": args.seed,
        "arms_order": arms,
        "expected_calls": expected_calls,
        "ok_calls": ok_calls,
        "completion": round(completion, 4),
        "arms": arms_summary,
        "paired": pairs,
        "per_category": per_category(rows, arms, queries),
        "verdict": verdict,
        "total_cost_usd": round(sum(row.get("cost_usd") or 0 for row in rows), 4),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_path.with_suffix(".md")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in
                      ("ok_calls", "expected_calls", "completion", "verdict", "total_cost_usd")},
                     ensure_ascii=False, indent=2))
    print("report -> %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

