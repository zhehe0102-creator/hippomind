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

**粒度**（2026-10-24，本脚本的 `--granularity` 参数）：真正的瓶颈是索引粒度。
记忆正文把 5-10 个事实塞进一条 550 字符，整体 embedding 被稀释。按 claim 建
索引后两套查询集都大幅提升，而 claim+memory 的 RRF 融合**反而更差**——所以
本脚本比较的是"同一路向量召回换粒度"，不是"多路融合"。

用法::

    python3 scripts/eval_semantic.py                    # 两套查询集 × 三种模式
    python3 scripts/eval_semantic.py --set keyword
    python3 scripts/eval_semantic.py --granularity claim
    python3 scripts/eval_semantic.py --granularity all  # claim vs memory vs 融合
    python3 scripts/eval_semantic.py --json
    python3 scripts/eval_semantic.py --ledger /path/to/snapshot.sqlite
    python3 scripts/eval_semantic.py --vector-index /path/vector_index.npz
    python3 scripts/eval_semantic.py --claim-index /path/vector_index_claims.npz

退出码：0 = 报告完成（默认不断言），1 = 门禁失败（仅 --gate 时）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_memory  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind import semantic_onnx  # noqa: E402
from hippomind.vector_index import (  # noqa: E402
    load_claim_vector_index, load_vector_index)

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


class _FusedIndex:
    """**仅用于测量**的 claim+memory 融合索引：RRF(k=60) 合并两路名次。

    生产代码里刻意没有这个东西：实测融合比 claim 级单独用**更差**
    （semantic 0.415 -> 0.302，keyword 0.826 -> 0.761）。原因很直接——两路各自
    的第一名互相稀释：claim 命中 A 记忆、memory 命中 B 记忆时，融合把两条都
    拉到中间名次，而真正对的那条（claim 找到的）丢了它应得的第一名。
    保留在这里只为让 `--granularity all` 能复现这张对照表，避免下一个人再试。
    """

    granularity = "claim_memory_fused"

    def __init__(self, claim_index, memory_index, k: float = 60.0):
        self.claim_index = claim_index
        self.memory_index = memory_index
        self.k = float(k)

    def search_text(self, query: str, topn: int = 96):
        scores: Dict[str, float] = {}
        best: Dict[str, Tuple[float, str]] = {}
        for index in (self.claim_index, self.memory_index):
            if index is None:
                continue
            try:
                hits = index.search_text(query, topn=topn)
            except Exception:
                continue
            for rank, entry in enumerate(hits):
                memory_id = str(entry[0])
                scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (self.k + rank)
                try:
                    similarity = float(entry[1])
                except (IndexError, TypeError, ValueError):
                    similarity = 0.0
                claim_id = ""
                try:
                    claim_id = str(entry[2])
                except (IndexError, TypeError):
                    claim_id = ""
                if memory_id not in best or similarity > best[memory_id][0]:
                    best[memory_id] = (similarity, claim_id)
        ordered = sorted(scores.items(), key=lambda pair: -pair[1])
        return [(memory_id, best.get(memory_id, (0.0, ""))[0],
                 best.get(memory_id, (0.0, ""))[1])
                for memory_id, _score_value in ordered][:max(1, int(topn))]

    def stats(self) -> Dict[str, Any]:
        return {"granularity": self.granularity,
                "docs": (len(self.claim_index) if self.claim_index else 0)
                + (len(self.memory_index) if self.memory_index else 0)}


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


#: 模式 → 用哪个索引（None 表示纯词法）。顺序即报告顺序。
#: `*_raw` 是**纯索引检索**：直接取索引 top-k 作为结果，不经过 ledger.search
#: 的词法评分与 RRF。它是"粒度本身"的干净度量——pipeline 模式的数字里混着
#: FTS/LIKE 通道的贡献，看不出粒度到底改变了多少向量召回。
MODES = ("lexical", "claim", "memory", "fused",
         "claim_raw", "memory_raw", "fused_raw")

#: 纯索引模式：`claim_raw` -> 用哪个索引。
RAW_MODES = {"claim_raw": "claim", "memory_raw": "memory", "fused_raw": "fused"}


def _score_raw(ledger: SQLiteLedger, queries: Sequence[Dict[str, Any]], *,
               index, limit: int, topn: int) -> Dict[str, Any]:
    """纯索引检索：索引 top-`limit` 直接作为结果，按记忆内容打分。

    刻意**不**重放 scope/project 过滤：这里度量的是"索引粒度"这一件事，
    加过滤会把另一个变量混进来。生产路径（pipeline 模式）的过滤重放有独立
    测试锁定，不靠这个函数保证。
    """
    cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def row_of(memory_id: str) -> Optional[Dict[str, Any]]:
        if memory_id in cache:
            return cache[memory_id]
        data = ledger.get_memory(memory_id)
        if data is not None:
            data = dict(data)
            for key in ("evidence", "tags", "metadata"):
                if data.get(key) is None:
                    raw = data.get(key + "_json")
                    try:
                        data[key] = json.loads(raw) if raw else (
                            {} if key == "metadata" else [])
                    except (TypeError, ValueError):
                        data[key] = {} if key == "metadata" else []
        cache[memory_id] = data
        return data

    rows: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for spec in queries:
        started = time.perf_counter()
        hits = index.search_text(spec["query"], topn=topn) if index is not None else []
        elapsed = (time.perf_counter() - started) * 1000.0
        latencies.append(elapsed)
        items: List[Dict[str, Any]] = []
        for entry in hits:
            data = row_of(str(entry[0]))
            if data is not None:
                items.append(data)
            if len(items) >= limit:
                break
        scored = eval_memory.score_hits(items, spec["expect"], k=limit,
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


def run(ledger: SQLiteLedger, sets: Sequence[str], *, limit: int,
        index_path: Optional[os.PathLike] = None, topn: int = 96,
        model: Optional[str] = None, granularity: str = "all",
        claim_index_path: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """若干模式 × 若干查询集的对照报告。

    `granularity`：`claim`（默认生产路径）/ `memory`（改造前）/ `all`（三路
    对照，含"融合更差"的测量行）。lexical 行永远保留，它是词法基线。
    """
    report: Dict[str, Any] = {"k": limit, "topn": topn, "sets": {},
                              "granularity": granularity,
                              "vector": {"available": False}, "indexes": {}}
    embedder = semantic_onnx.load_onnx_embedder(model)
    claim_index = None
    memory_index = None
    if embedder is None:
        report["vector"]["reason"] = ("ONNX 向量通道不可用（依赖/模型缺失）："
                                      + semantic_onnx.dependency_error())
    else:
        wanted = MODES if granularity == "all" else (
            "claim" if granularity == "claim" else "memory")
        if "claim" in wanted or "fused" in wanted:
            started = time.perf_counter()
            claim_index = load_claim_vector_index(
                ledger, embedder,
                path=Path(claim_index_path).expanduser() if claim_index_path else None)
            report["indexes"]["claim"] = {
                "build_seconds": round(time.perf_counter() - started, 1),
                "stats": claim_index.stats() if claim_index is not None else None,
            }
        if "memory" in wanted or "fused" in wanted:
            started = time.perf_counter()
            memory_index = load_vector_index(
                ledger, embedder,
                path=Path(index_path).expanduser() if index_path else None)
            report["indexes"]["memory"] = {
                "build_seconds": round(time.perf_counter() - started, 1),
                "stats": memory_index.stats() if memory_index is not None else None,
            }
        fused = (_FusedIndex(claim_index, memory_index)
                 if (claim_index is not None and memory_index is not None) else None)
        indexes = {"claim": claim_index, "memory": memory_index, "fused": fused}
        if claim_index is not None or memory_index is not None:
            report["vector"] = {"available": True, "indexes": report["indexes"]}
        else:
            report["vector"]["reason"] = "向量索引构建失败（numpy/账本不可用）"
        report["_indexes"] = indexes
    for name in sets:
        queries = _query_set(name)
        entry: Dict[str, Any] = {"lexical": _score(ledger, queries, limit=limit)}
        indexes = report.get("_indexes", {})
        for mode in MODES[1:]:
            if mode in RAW_MODES:
                index = indexes.get(RAW_MODES[mode])
                if index is None:
                    continue
                entry[mode] = _score_raw(ledger, queries, index=index,
                                         limit=limit, topn=max(topn, limit * 8))
                continue
            index = indexes.get(mode)
            if index is None:
                continue
            entry[mode] = _score(ledger, queries, limit=limit,
                                 vector_index=index, topn=topn)
        report["sets"][name] = entry
    report.pop("_indexes", None)
    return report


def _print_report(report: Dict[str, Any]) -> None:
    vector = report.get("vector", {})
    if vector.get("available"):
        for name, entry in sorted((vector.get("indexes") or {}).items()):
            stats = entry.get("stats")
            if not stats:
                print("vector index (%s): UNAVAILABLE" % name)
                continue
            print("vector index (%s): docs=%s dim=%s bytes=%s model=%s built_at=%s (%ss)"
                  % (name, stats.get("docs"), stats.get("dim"), stats.get("bytes"),
                     stats.get("model"), stats.get("built_at"),
                     entry.get("build_seconds")))
            if name == "claim":
                print("                    memories=%s (claims 折叠后的记忆数)"
                      % stats.get("memories"))
    else:
        print("vector index: UNAVAILABLE — %s" % vector.get("reason", "unknown"))
    for name, entry in report["sets"].items():
        print("")
        print("== set=%s ==" % name)
        print("  %-10s %-8s %-8s %-9s %-9s %s"
              % ("mode", "recall", "mrr", "p50", "p95", "misses"))
        for mode in MODES:
            data = entry.get(mode)
            if data is None:
                continue
            print("  %-10s %-8.3f %-8.3f %-9s %-9s %s"
                  % (mode, data["recall"], data["mrr"],
                     "%.1fms" % data["p50_ms"], "%.1fms" % data["p95_ms"],
                     ",".join(data["misses"][:8]) or "-"))
        baseline = entry.get("lexical")
        for mode in ("claim", "memory", "fused"):
            candidate = entry.get(mode)
            if not baseline or not candidate:
                continue
            print("  delta/%-4s recall %+.4f  mrr %+.4f  p95 %+.1fms"
                  % (mode, candidate["recall"] - baseline["recall"],
                     candidate["mrr"] - baseline["mrr"],
                     candidate["p95_ms"] - baseline["p95_ms"]))


#: pipeline 模式的噪声容差：允许比 lexical 基线低这么多（同分排序抖动、
#: 新增记忆改变 RRF 名次都可能造成 0.00x 的波动）。
DEFAULT_REGRESSION_TOLERANCE = 0.005


def gate(report: Dict[str, Any], *,
         regression_tolerance: float = DEFAULT_REGRESSION_TOLERANCE,
         min_semantic_gain: float = 0.05,
         min_claim_semantic_recall: float = 0.40,
         min_claim_keyword_recall: float = 0.80) -> Dict[str, Any]:
    """验收门禁：**相对**回归检查 + claim 纯索引的绝对下限。

    为什么是相对门禁（2026-10-24 修正）
    -----------------------------------
    第一版把 `keyword recall >= 0.95` 当硬阈值。它**不是**本次改动的产物，
    而是账本漂移的产物，实测证据（Lead 独立复核）：

    * 用已提交的 HEAD 代码（git stash 掉全部未提交改动）+ 关闭向量跑 keyword
      集，同样是 recall 0.935 / gate FAIL；
    * 账本在 2 小时内新增 1310 条 accepted+active 记忆（6509 -> 7026），
      新记忆稀释了 top-8 的名次，`hippomind-vector-status` 这类查询被挤出。

    绝对基线在生产账本上**天然不可靠**：它度量的是"账本当前长什么样"，不是
    "代码有没有退化"。所以这里改成：

    1. **pipeline 模式不得低于 lexical 基线**（容差 `regression_tolerance`）。
       lexical 自身是多少就接受多少——它是同一次运行、同一份账本上的对照，
       两边一起漂移，差值才是代码的贡献。
    2. **semantic 集必须有提升**（相对 lexical，默认 >= +0.05）。
       阈值刻意取正值而不是 0：`gain >= 0` 会让"完全没提升"也通过
       （`0.0 < 0.0` 为假），而"向量通道没起作用"正是最该被抓住的退化。
       实测提升是 +0.67，+0.05 只是"通道确实在工作"的下限，不是性能目标。
    3. **claim 纯索引的绝对下限保留**：`claim_raw` 不经过词法通道，它的绝对
       值就是"索引本身有多好"，这正是 task-18 要保证的东西。阈值按**当前**
       账本快照校准（见下），不是历史数字。

    阈值快照
    --------
    下列 claim_raw 阈值来自 **2026-10-24 08:20 的生产账本**：
    7026 条 accepted+active 记忆 / 3487 条 active claim / bge-small-zh 512 维，
    实测 keyword 0.826、semantic 0.467。阈值取 0.80 / 0.40 留约 3-7% 边际：
    够宽到能吸收账本漂移，又够窄到能抓住"claim 索引退化成 memory 级"
    （那会掉到 0.67 / 0.20）。
    """
    failures: List[str] = []
    vector = report.get("vector", {})
    if not vector.get("available"):
        failures.append("向量通道不可用：%s" % vector.get("reason", "unknown"))
    # `*_raw` 是纯索引检索，没有词法通道，天然低于 pipeline，不参与
    # "不得低于 lexical" 的比较（它们有自己的绝对下限）。
    pipeline = tuple(mode for mode in MODES if mode not in RAW_MODES)
    keyword = report["sets"].get("keyword", {})
    semantic = report["sets"].get("semantic", {})

    for name, entry in (("keyword", keyword), ("semantic", semantic)):
        baseline = entry.get("lexical")
        if not baseline:
            continue
        floor = baseline["recall"] - regression_tolerance
        for mode in pipeline:
            data = entry.get(mode)
            if data is None:
                continue
            if data["recall"] < floor:
                failures.append(
                    "%s/%s recall %.4f 低于 lexical 基线 %.4f（容差 %.3f）"
                    % (name, mode, data["recall"], baseline["recall"],
                       regression_tolerance))
            elif mode != "lexical":
                gain = data["recall"] - baseline["recall"]
                if gain < min_semantic_gain and name == "semantic":
                    failures.append("%s/%s recall 无提升（%+.4f < %+.4f）"
                                    % (name, mode, gain, min_semantic_gain))

    # 绝对下限只针对 claim 纯索引（见 docstring 的阈值快照）。
    claim_semantic = semantic.get("claim_raw")
    if claim_semantic and claim_semantic["recall"] < min_claim_semantic_recall:
        failures.append("semantic/claim_raw recall %.4f < %.4f"
                        % (claim_semantic["recall"], min_claim_semantic_recall))
    claim_keyword = keyword.get("claim_raw")
    if claim_keyword and claim_keyword["recall"] < min_claim_keyword_recall:
        failures.append("keyword/claim_raw recall %.4f < %.4f"
                        % (claim_keyword["recall"], min_claim_keyword_recall))
    return {"passed": not failures, "failures": failures,
            "thresholds": {"regression_tolerance": regression_tolerance,
                           "min_semantic_gain": min_semantic_gain,
                           "min_claim_semantic_recall": min_claim_semantic_recall,
                           "min_claim_keyword_recall": min_claim_keyword_recall},
            "baselines": {name: entry.get("lexical", {}).get("recall")
                          for name, entry in (("keyword", keyword),
                                              ("semantic", semantic))}}


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
                        help="memory 级索引路径（默认 $HIPPOMIND_HOME/vector_index.npz）")
    parser.add_argument("--claim-index", default=None,
                        help="claim 级索引路径（默认 $HIPPOMIND_HOME/vector_index_claims.npz）")
    parser.add_argument("--granularity", default="all",
                        choices=("claim", "memory", "all"),
                        help="索引粒度对照：claim（生产默认）/ memory / all（三路）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--regression-tolerance", type=float,
                        default=DEFAULT_REGRESSION_TOLERANCE,
                        help="pipeline 模式允许比 lexical 基线低多少"
                             "（默认 %.3f）" % DEFAULT_REGRESSION_TOLERANCE)
    parser.add_argument("--min-semantic-gain", type=float, default=0.05,
                        help="semantic 集相对 lexical 的最小提升（默认 0.05）")
    parser.add_argument("--gate", action="store_true",
                        help="按验收阈值判定（pipeline 模式不得低于 lexical 基线；"
                             "claim_raw semantic >= 0.40 / keyword >= 0.80）")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("error: ledger not found: %s" % ledger_path, file=sys.stderr)
        return 2
    sets = ["keyword", "semantic"] if args.sets == "all" else [args.sets]
    index_path = Path(args.vector_index).expanduser() if args.vector_index else None
    claim_index_path = (Path(args.claim_index).expanduser()
                        if args.claim_index else None)
    ledger = SQLiteLedger(ledger_path)
    try:
        report = run(ledger, sets, limit=args.limit, index_path=index_path,
                     topn=args.topn, model=args.model,
                     granularity=args.granularity,
                     claim_index_path=claim_index_path)
    finally:
        ledger.close()
    report["ledger"] = str(ledger_path)
    report["gate"] = gate(report,
                          regression_tolerance=args.regression_tolerance,
                          min_semantic_gain=args.min_semantic_gain)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
        result = report["gate"]
        print("")
        baselines = result.get("baselines", {})
        print("baselines (lexical recall): %s"
              % ", ".join("%s=%s" % (name, value)
                          for name, value in baselines.items()))
        print("gate: %s" % ("PASS" if result["passed"] else "FAIL"))
        for failure in result["failures"]:
            print("  failure: %s" % failure)
    if args.gate:
        return 0 if report["gate"]["passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
