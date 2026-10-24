#!/usr/bin/env python3
"""Small, dependency-free retrieval quality harness.

This is deliberately not a claim of a 95-point product score: it is a
repeatable smoke benchmark that catches the obvious regressions (empty
results, duplicate flooding, slow queries) while a larger labelled set is
built.  Run it after changes:

    python3 scripts/eval_memory.py
    python3 scripts/eval_memory.py --queries my_queries.json --json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger import SQLiteLedger  # noqa: E402

DEFAULT_QUERIES: List[Dict[str, Any]] = [
    {"query": "HippoMind GUI 主题", "expect": ["纸本", "GUI"]},
    {"query": "数据库选型 SQLite", "expect": ["SQLite"]},
    {"query": "Codex compaction 根因", "expect": ["compaction", "压缩"]},
    {"query": "RelayDesk 模型路由", "expect": ["RelayDesk", "路由"]},
    {"query": "自动记忆 提炼 claims", "expect": ["claims", "提炼"]},
    {"query": "daemon 并发限制", "expect": ["daemon", "并发"]},
    {"query": "登录页 验收", "expect": ["登录", "验收"]},
    {"query": "MCP Agent 接入", "expect": ["MCP", "Agent"]},
    {"query": "外置卷 TCC 启动", "expect": ["TCC", "外置"]},
    {"query": "Graph 实体关系", "expect": ["图谱", "关系"]},
]


def _content(item: Dict[str, Any]) -> str:
    return str(item.get("content") or "")


def evaluate(ledger: SQLiteLedger, queries: Sequence[Dict[str, Any]], *,
             limit: int = 8) -> Dict[str, Any]:
    rows = []
    latencies = []
    for spec in queries:
        query = str(spec.get("query", "")).strip()
        expect = [str(term).lower() for term in spec.get("expect", [])]
        started = time.perf_counter()
        hits = ledger.search(query, scope="shared", limit=limit,
                             include_raw_chat=False, require_evidence=True)
        elapsed = (time.perf_counter() - started) * 1000.0
        latencies.append(elapsed)
        contents = [_content(item) for item in hits]
        matched = [term for term in expect if any(term in content.lower() for content in contents)]
        rank = 0
        for index, content in enumerate(contents, 1):
            if any(term in content.lower() for term in expect):
                rank = index
                break
        duplicates = len(contents) - len({content.strip() for content in contents})
        rows.append({
            "query": query,
            "hits": len(hits),
            "matched_terms": matched,
            "term_recall": round(len(matched) / max(1, len(expect)), 3),
            "mrr": round(1.0 / rank, 3) if rank else 0.0,
            "duplicate_rate": round(duplicates / max(1, len(contents)), 3),
            "latency_ms": round(elapsed, 2),
            "top": contents[:2],
        })
    n = max(1, len(rows))
    return {
        "queries": len(rows),
        "term_recall": round(sum(row["term_recall"] for row in rows) / n, 3),
        "mrr": round(sum(row["mrr"] for row in rows) / n, 3),
        "duplicate_rate": round(sum(row["duplicate_rate"] for row in rows) / n, 3),
        "latency_p50_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
        "latency_p95_ms": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 2) if latencies else 0.0,
        "details": rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="HippoMind retrieval smoke benchmark")
    parser.add_argument("--ledger", default=os.path.expanduser(
        "~/Library/Application Support/HippoMind/hippomind.sqlite"))
    parser.add_argument("--queries", help="JSON file with [{query, expect:[...]}, ...]")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    queries = DEFAULT_QUERIES
    if args.queries:
        queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    ledger = SQLiteLedger(args.ledger)
    try:
        result = evaluate(ledger, queries, limit=args.limit)
    finally:
        ledger.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("queries=%d term_recall=%.3f mrr=%.3f dup=%.3f p50=%.1fms p95=%.1fms" % (
            result["queries"], result["term_recall"], result["mrr"],
            result["duplicate_rate"], result["latency_p50_ms"], result["latency_p95_ms"]))
        for row in result["details"]:
            print("  %-28s hits=%d recall=%.2f mrr=%.2f dup=%.2f" % (
                row["query"], row["hits"], row["term_recall"], row["mrr"], row["duplicate_rate"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
