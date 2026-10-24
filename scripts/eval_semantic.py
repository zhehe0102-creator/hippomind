#!/usr/bin/env python3
"""语义通道 A/B 对照：词法基线 vs 神经向量参与候选生成（RRF）。

这个脚本回答一个很具体的问题：**加神经向量到底让检索变好还是变差**。
它同时跑两套查询集，因为只跑一套会得出错误结论：

* --set keyword：scripts/eval_memory.py 里的 46 条标注查询（关键词导向）。
  它们是回归基线，任何改动都不该让它们掉太多。
* --set semantic：改写式查询（措辞与目标记忆不重合）。它们度量"换个说法
  还能不能找到"，是语义改造唯一能证明自己的地方。

实测结论（docs/SEMANTIC.md 的「神经向量」章节有完整数字）：向量**只做重排**
会让两套指标都变差；向量**参与候选生成**（top-96 与 FTS 做 RRF，k=60）才让
改写集上升、关键词集轻微下降。所以本脚本比较的是候选生成路径。

用法::

    python3 scripts/eval_semantic.py                    # 两套查询集，两种模式
    python3 scripts/eval_semantic.py --set keyword
    python3 scripts/eval_semantic.py --json
    python3 scripts/eval_semantic.py --ledger /path/to/snapshot.sqlite
    python3 scripts/eval_semantic.py --vector-index /path/vector_index.npz

退出码：0 = 报告完成（默认不断言），1 = 门禁失败（仅 --gate 时）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_memory  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind import semantic_onnx  # noqa: E402
from hippomind.vector_index import load_vector_index  # noqa: E402

DEFAULT_LEDGER = eval_memory.DEFAULT_LEDGER

#: 改写式查询集：措辞刻意与目标记忆**不重合**，考的是"换个说法还能不能找到"。
#: 每条 expect 都先在账本里用 SQL LIKE 验证过真实存在（命中数记在 note 里），
#: 否则会出现"永远不可能命中"的假标注——标注阶段真踩过这个坑：
#: 原始标注里 KeepAlive / superseded 在账本中根本不存在。
#: 这份集合是 task-12（eval_memory.py 的 SEMANTIC_QUERIES）的先行子集，
#: 最终以那边的 40+ 条为准；这里保留 15 条是为了让本脚本自包含可复现。
PARAPHRASE_QUERIES: List[Dict[str, Any]] = [
    {"id": "p-deploy", "query": "上线前应该走什么流程", "expect": ["灰度"],
     "note": "迁移脚本先备份再预发验证最后灰度（LIKE 命中 1）"},
    {"id": "p-zygisk-root", "query": "为什么普通权限查不到注入模块在不在跑",
     "expect": ["root"], "note": "非 root 下 ps/getprop 查不到 zygisk（LIKE 命中 861）"},
    {"id": "p-restart-ui", "query": "设备重启以后界面位置变了怎么办",
     "expect": ["重新观察屏幕"], "note": "重启后 GhostSam 布局变化（LIKE 命中 7）"},
    {"id": "p-adb-missing", "query": "手机连上了但是列表里没有设备",
     "expect": ["adb devices"], "note": "设备重启后先 adb devices 确认枚举（LIKE 命中 42）"},
    {"id": "p-quant", "query": "显存不够怎么把模型压小", "expect": ["Q4_K_M"],
     "note": "32GB 显存 + 7900XT，Q4_K_M 量化（LIKE 命中 10）"},
    {"id": "p-zerotoken", "query": "能不能不用打触发词也自动带上下文",
     "expect": ["触发词"], "note": "自动获得最小 Context Pack，无需触发词（LIKE 命中 7）"},
    {"id": "p-inject-safety", "query": "记忆里夹带恶意指令怎么防",
     "expect": ["提示注入"], "note": "提示注入防护（LIKE 命中 6）"},
    {"id": "p-bitemporal", "query": "怎么记录一条事实是什么时候开始生效的",
     "expect": ["双时态"], "note": "schema v4，双时态（LIKE 命中 30）"},
    {"id": "p-nonroot-check", "query": "怎样确认模块真的挂载成功了",
     "expect": ["zygisk"], "note": "非 root 下查不到 zygisk（LIKE 命中 321）"},
    {"id": "p-review-readonly", "query": "做代码审查时能不能改文件",
     "expect": ["只读"], "note": "代码审查采用只读模式（LIKE 命中 144）"},
    # 原始标注用 KeepAlive，实测账本里不存在（永远不可能命中），改成真实存在的词。
    {"id": "p-crash", "query": "进程被杀掉以后会自己起来吗", "expect": ["重启"],
     "note": "launchd KeepAlive 崩溃重启；expect 用真实存在的 重启（LIKE 命中 227）"},
    {"id": "p-readonly-build", "query": "跑构建和测试会不会动到源码",
     "expect": ["不得修改任何源文件"], "note": "构建测试只读（LIKE 命中 7）"},
    {"id": "p-conflict", "query": "两条互相矛盾的说法怎么定谁对", "expect": ["冲突"],
     "note": "按最新 asserted_at 裁决；expect 用真实存在的 冲突（LIKE 命中 68）"},
    {"id": "p-budget", "query": "一次最多塞多少内容给模型", "expect": ["6000"],
     "note": "默认 token 预算 6000（LIKE 命中 41）"},
    {"id": "p-assoc", "query": "系统怎么把相关但不完全一样的旧事联想起来",
     "expect": ["related_to"], "note": "按共享 claim subject 连接（LIKE 命中 1）"},
]


def _score(ledger: SQLiteLedger, queries: Sequence[Dict[str, Any]], *,
           limit: int, vector_index=None, topn: int = 96) -> Dict[str, Any]:
    """跑一套查询并聚合 recall@k / MRR / 延迟。"""
    rows: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for spec in queries:
        started = time.perf_counter()
        hits = ledger.search(
            spec["query"], scope=spec.get("scope", "shared"),
            project=spec.get("project", ""), limit=limit,
            include_raw_chat=False, require_evidence=True,
            vector_index=vector_index, vector_topn=topn)
        elapsed = (time.perf_counter() - started) * 1000.0
        latencies.append(elapsed)
        scored = eval_memory.score_hits(hits, spec["expect"], k=limit,
                                        latency_ms=elapsed)
        scored["id"] = spec["id"]
        rows.append(scored)
    count = len(rows) or 1
    return {
        "queries": len(rows),
        "recall": round(sum(1 for row in rows if row["relevant"]) / count, 4),
        "mrr": round(sum(row["reciprocal_rank"] for row in rows) / count, 4),
        "p50_ms": round(eval_memory.percentile(latencies, 0.50), 1),
        "p95_ms": round(eval_memory.percentile(latencies, 0.95), 1),
        "misses": [row["id"] for row in rows if not row["relevant"]],
    }


def _query_set(name: str) -> List[Dict[str, Any]]:
    if name == "keyword":
        return eval_memory.validate_queries(eval_memory.LABELLED_QUERIES)
    if name == "semantic":
        return eval_memory.validate_queries(PARAPHRASE_QUERIES)
    raise ValueError("unknown query set %r" % name)


def run(ledger: SQLiteLedger, sets: Sequence[str], *, limit: int,
        index_path: Optional[os.PathLike] = None, topn: int = 96,
        model: Optional[str] = None) -> Dict[str, Any]:
    """两种模式 × 若干查询集的对照报告。"""
    report: Dict[str, Any] = {"k": limit, "topn": topn, "sets": {},
                              "vector": {"available": False}}
    embedder = semantic_onnx.load_onnx_embedder(model)
    vector_index = None
    if embedder is None:
        report["vector"]["reason"] = ("ONNX 向量通道不可用（依赖/模型缺失）："
                                      + semantic_onnx.dependency_error())
    else:
        started = time.perf_counter()
        vector_index = load_vector_index(ledger, embedder, path=index_path)
        build_seconds = time.perf_counter() - started
        if vector_index is None:
            report["vector"]["reason"] = "向量索引构建失败（numpy/账本不可用）"
        else:
            report["vector"] = {"available": True,
                                "build_seconds": round(build_seconds, 1),
                                "stats": vector_index.stats()}
    for name in sets:
        queries = _query_set(name)
        entry: Dict[str, Any] = {"lexical": _score(ledger, queries, limit=limit)}
        if vector_index is not None:
            entry["vector_rrf"] = _score(ledger, queries, limit=limit,
                                         vector_index=vector_index, topn=topn)
        report["sets"][name] = entry
    return report


def _print_report(report: Dict[str, Any]) -> None:
    vector = report.get("vector", {})
    if vector.get("available"):
        stats = vector.get("stats", {})
        print("vector index: docs=%s dim=%s bytes=%s model=%s built_at=%s (%ss)"
              % (stats.get("docs"), stats.get("dim"), stats.get("bytes"),
                 stats.get("model"), stats.get("built_at"),
                 vector.get("build_seconds")))
    else:
        print("vector index: UNAVAILABLE — %s" % vector.get("reason", "unknown"))
    for name, entry in report["sets"].items():
        print("")
        print("== set=%s ==" % name)
        print("  %-12s %-8s %-8s %-9s %-9s %s"
              % ("mode", "recall", "mrr", "p50", "p95", "misses"))
        for mode in ("lexical", "vector_rrf"):
            data = entry.get(mode)
            if data is None:
                continue
            print("  %-12s %-8.3f %-8.3f %-9s %-9s %s"
                  % (mode, data["recall"], data["mrr"],
                     "%.1fms" % data["p50_ms"], "%.1fms" % data["p95_ms"],
                     ",".join(data["misses"][:8]) or "-"))
        baseline = entry.get("lexical")
        candidate = entry.get("vector_rrf")
        if baseline and candidate:
            print("  delta        recall %+.4f  mrr %+.4f  p95 %+.1fms"
                  % (candidate["recall"] - baseline["recall"],
                     candidate["mrr"] - baseline["mrr"],
                     candidate["p95_ms"] - baseline["p95_ms"]))


def gate(report: Dict[str, Any], *, min_keyword_recall: float = 0.95,
         min_semantic_gain: float = 0.0) -> Dict[str, Any]:
    """验收门禁：关键词集不掉太多，改写集必须有提升（向量可用时）。"""
    failures: List[str] = []
    vector = report.get("vector", {})
    if not vector.get("available"):
        failures.append("向量通道不可用：%s" % vector.get("reason", "unknown"))
    keyword = report["sets"].get("keyword", {})
    for mode in ("lexical", "vector_rrf"):
        data = keyword.get(mode)
        if data and data["recall"] < min_keyword_recall:
            failures.append("keyword/%s recall %.4f < %.4f"
                            % (mode, data["recall"], min_keyword_recall))
    semantic = report["sets"].get("semantic", {})
    baseline, candidate = semantic.get("lexical"), semantic.get("vector_rrf")
    if baseline and candidate:
        gain = candidate["recall"] - baseline["recall"]
        if gain < min_semantic_gain:
            failures.append("semantic recall 无提升（%+.4f < %+.4f）"
                            % (gain, min_semantic_gain))
    return {"passed": not failures, "failures": failures,
            "thresholds": {"min_keyword_recall": min_keyword_recall,
                           "min_semantic_gain": min_semantic_gain}}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="神经向量通道 vs 词法基线的 A/B 对照报告")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--set", dest="sets", default="all",
                        choices=("keyword", "semantic", "all"))
    parser.add_argument("--limit", "-k", type=int, default=8)
    parser.add_argument("--topn", type=int, default=96,
                        help="向量通道参与 RRF 时取多少条")
    parser.add_argument("--model", default=None,
                        help="模型名（默认 $HIPPOMIND_ONNX_MODEL 或 bgezh）")
    parser.add_argument("--vector-index", default=None,
                        help="索引文件路径（默认 $HIPPOMIND_HOME/vector_index.npz）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--gate", action="store_true",
                        help="按验收阈值判定（关键词 >= 0.95，改写集有提升）")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("error: ledger not found: %s" % ledger_path, file=sys.stderr)
        return 2
    sets = ["keyword", "semantic"] if args.sets == "all" else [args.sets]
    index_path = Path(args.vector_index).expanduser() if args.vector_index else None
    ledger = SQLiteLedger(ledger_path)
    try:
        report = run(ledger, sets, limit=args.limit, index_path=index_path,
                     topn=args.topn, model=args.model)
    finally:
        ledger.close()
    report["ledger"] = str(ledger_path)
    report["gate"] = gate(report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
        result = report["gate"]
        print("")
        print("gate: %s" % ("PASS" if result["passed"] else "FAIL"))
        for failure in result["failures"]:
            print("  failure: %s" % failure)
    if args.gate:
        return 0 if report["gate"]["passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
