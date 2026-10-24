#!/usr/bin/env python3
"""Labelled retrieval evaluation and pass/fail gate for HippoMind.

This is the regression gate for memory retrieval quality.  It runs a labelled
query set (a query plus required evidence substrings) against a ledger, reports
recall@k, MRR, duplicate rate and latency percentiles, then compares the
aggregate against configurable thresholds.

The built-in query set is grounded in the production ledger snapshot of
2026-10-23 (distilled claims and consolidated memories; source_ids records
provenance and docs/EVAL.md explains how to refresh it).

Usage:
    python3 scripts/eval_memory.py                 # gate production ledger
    python3 scripts/eval_memory.py --json
    python3 scripts/eval_memory.py --queries my_queries.json
    python3 scripts/eval_memory.py --dump-queries /tmp/queries.json
    python3 scripts/eval_memory.py --no-gate       # report only

Exit codes: 0 = gate passed, 1 = gate failed, 2 = usage/input error.
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

from hippomind.ledger import SQLiteLedger  # noqa: E402

DEFAULT_LEDGER = os.path.expanduser(
    "~/Library/Application Support/HippoMind/hippomind.sqlite")

# Context fields are part of the memory payload a caller receives (the Context
# Pack renders them), so expected evidence may live there as well as in
# content.
CONTEXT_FIELDS = ("goal", "progress", "key_decisions", "related_files",
                  "blockers", "next_steps", "preferences")

# Gate defaults keep a safety margin over the 2026-10-23 production baseline:
# recall@8 1.000 -> 0.90, MRR 0.915 -> 0.75, duplicate rate 0.016 -> 0.10,
# p95 ~10ms -> 250ms.  Every threshold can be overridden per run.
DEFAULT_THRESHOLDS = {
    "min_recall_at_k": 0.90,
    "min_mrr": 0.75,
    "max_duplicate_rate": 0.10,
    "max_p95_ms": 250.0,
    "min_queries": 30,
}

# Labelled queries grounded in the 2026-10-23 production ledger/claims.  Each
# entry has an id, category, query, expected evidence substrings (all must
# appear in the same retrieved memory), an optional project filter, and the
# source memory ids used to ground it.  Query style is keyword-oriented because
# the distilled ledger rows store claim values without their subject/predicate
# context; see docs/EVAL.md for the rationale and refresh procedure.
LABELLED_QUERIES: List[Dict[str, Any]] = [
    {"id": "hippomind-gui-theme", "category": "hippomind",
     "query": "HippoMind GUI 主题 纸本", "expect": ["纸本"],
     "source_ids": ["mem_ac68f39428225fffe4513978e768e72e"]},
    {"id": "hippomind-db", "category": "hippomind",
     "query": "HippoMind 数据库选型", "expect": ["SQLite"],
     "source_ids": ["mem_ac68f39428225fffe4513978e768e72e", "mem_bf9ae505751762e95b19f3a02e772b2c"]},
    {"id": "hippomind-token-budget", "category": "hippomind",
     "query": "HippoMind 默认 token 预算", "expect": ["6000"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457"]},
    {"id": "hippomind-bitemporal", "category": "hippomind",
     "query": "HippoMind schema v4", "expect": ["双时态"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457"]},
    {"id": "hippomind-host", "category": "hippomind",
     "query": "HippoMind 部署主机 mac-mini", "expect": ["mac-mini"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457"]},
    {"id": "hippomind-arch", "category": "hippomind",
     "query": "HippoMind 最终架构 Context Pack", "expect": ["SQLite/WAL"],
     "source_ids": ["mem_f1a56df044a2d59f947bb6f1c180230b"]},
    {"id": "hippomind-agents", "category": "hippomind",
     "query": "Codex Pi Claude Cursor 支持", "expect": ["跨端续接"],
     "source_ids": ["mem_f1a56df044a2d59f947bb6f1c180230b"]},
    {"id": "hippomind-injection", "category": "hippomind",
     "query": "全量归档 注入 策略", "expect": ["最小上下文"],
     "source_ids": ["mem_f1a56df044a2d59f947bb6f1c180230b"]},
    {"id": "hippomind-hardening", "category": "hippomind",
     "query": "召回质量 daemon 可靠性", "expect": ["数据保护"],
     "source_ids": ["mem_3376395075cc60afad34b5a44707f3c3"]},
    {"id": "hippomind-security", "category": "hippomind",
     "query": "来源追溯 隐私隔离 一键启动", "expect": ["提示注入防护"],
     "source_ids": ["mem_f1a56df044a2d59f947bb6f1c180230b"]},
    {"id": "hippomind-longmemeval", "category": "hippomind",
     "query": "下一阶段评估 程序性记忆 ACT-R", "expect": ["LongMemEval"],
     "source_ids": ["mem_3376395075cc60afad34b5a44707f3c3"]},
    {"id": "hippomind-claims", "category": "hippomind",
     "query": "冲突检测 技能记忆", "expect": ["claims"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457", "mem_9d0c63f9a330bc365c36a4766128cda7"]},
    {"id": "hippomind-actr", "category": "hippomind",
     "query": "向量检索 ACT-R 定位", "expect": ["可插拔评测接口"],
     "source_ids": ["mem_eaedfb06fc4bc9da0b8c3c6074bbd457"]},
    {"id": "hippomind-vector-status", "category": "hippomind",
     "query": "记忆系统 vector 激活状态", "expect": ["vector 尚未激活"],
     "source_ids": ["mem_675d2d8f37a6a20f9c08e51144d3cea6"]},
    {"id": "hippomind-audit-issues", "category": "hippomind",
     "query": "误召回 访问控制", "expect": ["漏采"],
     "source_ids": ["mem_3376395075cc60afad34b5a44707f3c3"]},
    {"id": "hippomind-verification", "category": "hippomind",
     "query": "任务文件 可写性 验证", "expect": ["共享账本"],
     "source_ids": ["mem_f8fa04623f5fe9acf8e8ff6a3b182c5d", "mem_d9951bdc1e4074ec8eff8b9037514f4b"]},
    {"id": "zygisk-next-version", "category": "android_ops",
     "query": "Zygisk Next 最新发布版本", "expect": ["v1.5.2"],
     "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"]},
    {"id": "zygisk-next-github", "category": "android_ops",
     "query": "Zygisk Next GitHub 仓库地址", "expect": ["Dr-TSNG/ZygiskNext"],
     "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"]},
    {"id": "zygisk-next-date", "category": "android_ops",
     "query": "Zygisk Next v1.5.2 发布日期", "expect": ["2026-10-15"],
     "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"]},
    {"id": "neozygisk-version", "category": "android_ops",
     "query": "NeoZygisk 运行版本", "expect": ["v2.4-289-08080ef-release"],
     "source_ids": ["mem_21070a16c0d9cfd809f693ef6b62b5c1", "mem_4890ec31c5ddee643a4fc93db4a1bdfe"]},
    {"id": "neozygisk-kernelsu", "category": "android_ops",
     "query": "NeoZygisk KernelSU 兼容性", "expect": ["post-fs-data.sh"],
     "source_ids": ["mem_4890ec31c5ddee643a4fc93db4a1bdfe"]},
    {"id": "adb-serial", "category": "android_ops",
     "query": "Android 设备 序列号", "expect": ["R5CY84B054K"],
     "source_ids": ["mem_6993fd440c644907ade361d819905894", "mem_675d2d8f37a6a20f9c08e51144d3cea6"]},
    {"id": "adb-enumeration", "category": "android_ops",
     "query": "adb 未枚举 排查", "expect": ["adb kill-server"],
     "source_ids": ["mem_4494f3e0b30df83eddd1ac694cbd24cd"]},
    {"id": "zygisk-root", "category": "android_ops",
     "query": "Zygisk 状态 验证 root 权限", "expect": ["必须 root"],
     "source_ids": ["mem_bb32a5931bc722effcb7508f1a8fc4fb", "mem_ac68f39428225fffe4513978e768e72e"]},
    {"id": "zygisk-exit", "category": "android_ops",
     "query": "Zygisk Next 主动退出 状态", "expect": ["User requested to exit"],
     "source_ids": ["mem_70065a6eb19f1850d0ced2e57f2d39a7"]},
    {"id": "zygcheck-script", "category": "android_ops",
     "query": "netproxy-work .tools 助手脚本", "expect": ["zygcheck.sh"],
     "source_ids": ["mem_ac68f39428225fffe4513978e768e72e"]},
    {"id": "ghostsam-terminal", "category": "android_ops",
     "query": "GhostSam 终端页 打开又关闭", "expect": ["系统工具"],
     "source_ids": ["mem_7a003354d0ea303966733de240ed79a0", "mem_b471fc5472cfc268825caf4ed8de8447"]},
    {"id": "ghostsam-background", "category": "android_ops",
     "query": "GhostSam 前台 应用切换", "expect": ["美团"],
     "source_ids": ["mem_cc55cad338bf4eee1218be20f7168d6c", "mem_675d2d8f37a6a20f9c08e51144d3cea6"]},
    {"id": "ui-xml-dump", "category": "android_ops",
     "query": "adb shell cat UI XML dump", "expect": ["/sdcard/"],
     "source_ids": ["mem_7faa121be09ca711f61f0c98969bd437"]},
    {"id": "adb-sandbox", "category": "android_ops",
     "query": "macOS 沙箱 adb 监听端口", "expect": ["gs-exec.sh"],
     "source_ids": ["mem_79aca9c5d02e85a51210e0f5d8bf69b8"]},
    {"id": "zygisk-impact", "category": "android_ops",
     "query": "Zygisk 未注入 受影响 模块", "expect": ["LSPosed"],
     "source_ids": ["mem_4890ec31c5ddee643a4fc93db4a1bdfe"]},
    {"id": "device-uptime", "category": "android_ops",
     "query": "重启成功 uptime 秒", "expect": ["803"],
     "source_ids": ["mem_ac68f39428225fffe4513978e768e72e"]},
    {"id": "relaydesk-amfi", "category": "project",
     "query": "RelayDesk Omni 打不开 启动即杀", "expect": ["AMFI"],
     "source_ids": ["mem_0ce687ef9a28b4c08dffc482f9eca6cb"]},
    {"id": "codex-compaction", "category": "project",
     "query": "Codex 压缩总是失败 根因", "expect": ["remote_compaction"],
     "source_ids": ["mem_c6cd0ca15a5856596d50fd4b5a8ae5a2"]},
    {"id": "relaydesk-effort", "category": "project",
     "query": "RelayDesk Omni 思考强度 模型 档位", "expect": ["DeepSeek 5 档"],
     "source_ids": ["mem_71f38b48e590a5acd9e9340e7265a058"]},
    {"id": "orbit-dual-mode", "category": "project",
     "query": "Orbit AI 前端 双模式", "expect": ["简易/全能"],
     "source_ids": ["mem_e3d2b9bac5eae0cb26b73084e72f63e9"]},
    {"id": "storyfoundry-progress", "category": "project",
     "query": "StoryFoundry 当前进度", "expect": ["SF-V3/R72"],
     "source_ids": ["mem_9eb4f7328d69f1fcab032bd8aa4a2b71"]},
    {"id": "remote-mic-parsing", "category": "project",
     "query": "remote-mic-app 会话格式 解析", "expect": ["防御式解析"],
     "source_ids": ["mem_f4f035087cdc34a03cc7f0799680025e"]},
    {"id": "local-model-gemma", "category": "project",
     "query": "M4 16GB 本地模型推荐", "expect": ["Gemma4-12B-it Q6_K"],
     "source_ids": ["mem_06cc49e04a9918f8d80198580fa4a013"]},
    {"id": "qwen-config", "category": "project",
     "query": "Qwen 27B 量化 显卡", "expect": ["Q4_K_M"],
     "source_ids": ["mem_b903a1ee48e0210c895720afdff331a2"]},
    {"id": "redis-sentinel", "category": "project_filtered",
     "query": "Redis 集群 故障转移", "expect": ["sentinel"],
     "project": "hippomind-e2e", "source_ids": ["mem_b7cfaece8eff65f422fee922fe5cc1eb"]},
    {"id": "kernelsu-samsung", "category": "project_filtered",
     "query": "Samsung tablet root setup", "expect": ["KernelSU"],
     "project": "outside-of-project", "source_ids": ["mem_31060976e3c314acbd52a1d22199c4b1"]},
    {"id": "autocontext-progress", "category": "project_filtered",
     "query": "无感自动上下文 验证事实", "expect": ["结构化进度"],
     "project": "hippomind", "source_ids": ["mem_c0b32e9cf5bb88ce97647c422886dfb4"]},
    {"id": "english-compaction", "category": "english",
     "query": "codex remote compaction failure root cause", "expect": ["got 0"],
     "source_ids": ["mem_c6cd0ca15a5856596d50fd4b5a8ae5a2"]},
    {"id": "english-zygisk", "category": "english",
     "query": "Zygisk Next GitHub repository", "expect": ["Dr-TSNG"],
     "source_ids": ["mem_ee1eb4651cbe19cceb7275948d5e6c2d"]},
    {"id": "english-storyfoundry", "category": "english",
     "query": "StoryFoundry tech stack", "expect": ["FSM stages 1-6"],
     "source_ids": ["mem_9eb4f7328d69f1fcab032bd8aa4a2b71"]},
]


def render_hit(item: Dict[str, Any]) -> str:
    """Text a retrieval result exposes to the caller (content + context)."""
    parts = [str(item.get("content") or "")]
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        context = metadata.get("context")
        if isinstance(context, dict):
            for field in CONTEXT_FIELDS:
                value = context.get(field)
                if isinstance(value, (list, tuple)):
                    parts.extend(str(entry) for entry in value)
                elif value:
                    parts.append(str(value))
    return " ".join(part for part in parts if part)


def is_relevant(item: Dict[str, Any], expect: Sequence[str]) -> bool:
    """A hit is relevant when it contains every expected substring."""
    expect = [str(term) for term in expect]
    if not expect:
        return False
    haystack = render_hit(item).lower()
    return all(term.lower() in haystack for term in expect)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolation percentile (numpy default convention)."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    fraction = min(1.0, max(0.0, float(fraction)))
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def score_hits(hits: Sequence[Dict[str, Any]], expect: Sequence[str], *,
               k: int = 8, latency_ms: float = 0.0) -> Dict[str, Any]:
    """Per-query metrics for one ordered result list."""
    expect = [str(term) for term in expect]
    top = list(hits[:k])
    rendered = [render_hit(item) for item in top]
    rank = 0
    for position, text in enumerate(rendered, 1):
        lowered = text.lower()
        if all(term.lower() in lowered for term in expect):
            rank = position
            break
    contents = [" ".join(str(item.get("content") or "").split()).lower()
                for item in top]
    duplicates = len(contents) - len(set(contents))
    duplicate_rate = duplicates / len(contents) if contents else 0.0
    matched = sum(
        1 for term in expect
        if any(term.lower() in text.lower() for text in rendered))
    return {
        "hits": len(top),
        "rank": rank,
        "relevant": rank > 0,
        "reciprocal_rank": round(1.0 / rank, 4) if rank else 0.0,
        "term_recall": round(matched / len(expect), 4) if expect else 0.0,
        "duplicate_rate": round(duplicate_rate, 4),
        "latency_ms": round(float(latency_ms), 2),
        "top": [text[:160] for text in rendered[:2]],
    }


def validate_queries(raw: Any) -> List[Dict[str, Any]]:
    """Validate a labelled query set and return normalised specs."""
    if not isinstance(raw, list):
        raise ValueError("query set must be a JSON array of objects")
    specs: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError("query #%d must be an object" % index)
        query = str(item.get("query") or "").strip()
        if not query:
            raise ValueError("query #%d has an empty 'query'" % index)
        expect_raw = item.get("expect")
        if not isinstance(expect_raw, list) or not expect_raw:
            raise ValueError(
                "query #%d (%r) needs a non-empty 'expect' list" % (index, query))
        expect: List[str] = []
        for term in expect_raw:
            if not isinstance(term, str) or not term.strip():
                raise ValueError(
                    "query #%d (%r) has a blank/non-string 'expect' entry"
                    % (index, query))
            expect.append(term.strip())
        spec: Dict[str, Any] = {"query": query, "expect": expect}
        query_id = str(item.get("id") or query).strip()
        if query_id in seen_ids:
            raise ValueError("duplicate query id %r" % query_id)
        seen_ids.add(query_id)
        spec["id"] = query_id
        for field in ("category", "project", "scope"):
            value = item.get(field)
            if value:
                spec[field] = str(value).strip()
        source_ids = item.get("source_ids") or []
        if source_ids:
            if (not isinstance(source_ids, list)
                    or not all(isinstance(value, str) for value in source_ids)):
                raise ValueError(
                    "query #%d (%r) 'source_ids' must be a list of strings"
                    % (index, query))
            spec["source_ids"] = [value.strip() for value in source_ids
                                  if value.strip()]
        specs.append(spec)
    if not specs:
        raise ValueError("query set is empty")
    return specs


def load_queries(path: os.PathLike) -> List[Dict[str, Any]]:
    """Load and validate a labelled query set from a JSON file."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("cannot read query file %s: %s" % (path, exc))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON in %s: %s" % (path, exc))
    return validate_queries(raw)


def evaluate(ledger: SQLiteLedger, queries: Sequence[Dict[str, Any]], *,
             limit: int = 8, search=None) -> Dict[str, Any]:
    """Run the labelled set and aggregate retrieval metrics.

    ``search`` is an optional injection point with the signature
    search(query, project, scope, limit) -> hits, used by tests.
    """
    specs = validate_queries(list(queries))
    limit = max(1, int(limit))
    rows: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for spec in specs:
        started = time.perf_counter()
        if search is not None:
            hits = search(spec["query"], spec.get("project", ""),
                          spec.get("scope", "shared"), limit)
        else:
            hits = ledger.search(
                spec["query"],
                scope=spec.get("scope", "shared"),
                project=spec.get("project", ""),
                limit=limit,
                include_raw_chat=False,
                require_evidence=True,
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        latencies.append(elapsed_ms)
        row: Dict[str, Any] = {
            "id": spec["id"],
            "category": spec.get("category", "custom"),
            "query": spec["query"],
            "expect": list(spec["expect"]),
            "project": spec.get("project", ""),
        }
        row.update(score_hits(hits, spec["expect"], k=limit,
                              latency_ms=elapsed_ms))
        rows.append(row)
    count = len(rows)
    metrics = {
        "recall_at_k": round(sum(1 for row in rows if row["relevant"]) / count, 4),
        "mrr": round(sum(row["reciprocal_rank"] for row in rows) / count, 4),
        "duplicate_rate": round(
            sum(row["duplicate_rate"] for row in rows) / count, 4),
        "term_recall_at_k": round(
            sum(row["term_recall"] for row in rows) / count, 4),
        "empty_rate": round(
            sum(1 for row in rows if row["hits"] == 0) / count, 4),
        "latency_p50_ms": round(percentile(latencies, 0.50), 2),
        "latency_p95_ms": round(percentile(latencies, 0.95), 2),
    }
    categories: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["category"], []).append(row)
    for name in sorted(grouped):
        items = grouped[name]
        categories[name] = {
            "queries": len(items),
            "recall_at_k": round(
                sum(1 for item in items if item["relevant"]) / len(items), 4),
            "mrr": round(
                sum(item["reciprocal_rank"] for item in items) / len(items), 4),
            "duplicate_rate": round(
                sum(item["duplicate_rate"] for item in items) / len(items), 4),
        }
    return {
        "queries": count,
        "k": limit,
        "metrics": metrics,
        "by_category": categories,
        "details": rows,
    }


def check_gate(
        result: Dict[str, Any], *,
        min_recall_at_k: float = DEFAULT_THRESHOLDS["min_recall_at_k"],
        min_mrr: float = DEFAULT_THRESHOLDS["min_mrr"],
        max_duplicate_rate: float = DEFAULT_THRESHOLDS["max_duplicate_rate"],
        max_p95_ms: float = DEFAULT_THRESHOLDS["max_p95_ms"],
        min_queries: int = DEFAULT_THRESHOLDS["min_queries"]) -> Dict[str, Any]:
    """Compare aggregated metrics against the pass/fail thresholds."""
    metrics = result["metrics"]
    failures: List[str] = []
    if result["queries"] < min_queries:
        failures.append("queries %d < min_queries %d"
                        % (result["queries"], min_queries))
    if metrics["recall_at_k"] < min_recall_at_k:
        failures.append("recall@%d %.4f < %.4f"
                        % (result["k"], metrics["recall_at_k"],
                           min_recall_at_k))
    if metrics["mrr"] < min_mrr:
        failures.append("mrr %.4f < %.4f" % (metrics["mrr"], min_mrr))
    if metrics["duplicate_rate"] > max_duplicate_rate:
        failures.append("duplicate_rate %.4f > %.4f"
                        % (metrics["duplicate_rate"], max_duplicate_rate))
    if metrics["latency_p95_ms"] > max_p95_ms:
        failures.append("latency_p95_ms %.2f > %.2f"
                        % (metrics["latency_p95_ms"], max_p95_ms))
    return {
        "passed": not failures,
        "failures": failures,
        "thresholds": {
            "min_recall_at_k": min_recall_at_k,
            "min_mrr": min_mrr,
            "max_duplicate_rate": max_duplicate_rate,
            "max_p95_ms": max_p95_ms,
            "min_queries": min_queries,
        },
    }


def _print_report(result: Dict[str, Any]) -> None:
    metrics = result["metrics"]
    stats = result.get("ledger_stats")
    if stats:
        print("ledger: memories=%s raw_chat=%s claims=%s active_claims=%s "
              "schema_version=%s"
              % (stats.get("memories"), stats.get("raw_chat"),
                 stats.get("claims"), stats.get("active_claims"),
                 stats.get("schema_version")))
    print("queries=%d k=%d recall@%d=%.3f mrr=%.3f duplicate_rate=%.3f "
          "term_recall=%.3f empty=%.3f p50=%.1fms p95=%.1fms"
          % (result["queries"], result["k"], result["k"],
             metrics["recall_at_k"], metrics["mrr"],
             metrics["duplicate_rate"], metrics["term_recall_at_k"],
             metrics["empty_rate"], metrics["latency_p50_ms"],
             metrics["latency_p95_ms"]))
    for row in result["details"]:
        status = "PASS" if row["relevant"] else "MISS"
        print("  %-4s %-28s rank=%d rr=%.2f term=%.2f dup=%.2f %7.1fms"
              % (status, row["id"], row["rank"], row["reciprocal_rank"],
                 row["term_recall"], row["duplicate_rate"], row["latency_ms"]))
        if not row["relevant"] and row["top"]:
            print("       top: %s" % row["top"][0][:120])
    print("")
    for name, stats in result["by_category"].items():
        print("  %-16s n=%-3d recall@%d=%.3f mrr=%.3f duplicate_rate=%.3f"
              % (name, stats["queries"], result["k"],
                 stats["recall_at_k"], stats["mrr"],
                 stats["duplicate_rate"]))
    gate = result["gate"]
    thresholds = gate["thresholds"]
    print("")
    print("gate: %s  (min_recall@%d=%.2f, min_mrr=%.2f, "
          "max_duplicate_rate=%.2f, max_p95=%.1fms, min_queries=%d)"
          % ("PASS" if gate["passed"] else "FAIL", result["k"],
             thresholds["min_recall_at_k"], thresholds["min_mrr"],
             thresholds["max_duplicate_rate"], thresholds["max_p95_ms"],
             thresholds["min_queries"]))
    for failure in gate["failures"]:
        print("  failure: %s" % failure)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="HippoMind labelled retrieval evaluation and regression gate")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER,
                        help="SQLite ledger to evaluate")
    parser.add_argument("--queries",
                        help="JSON query file (default: built-in production set)")
    parser.add_argument("--limit", "-k", type=int, default=8,
                        help="results retrieved per query (k)")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--dump-queries", metavar="PATH",
                        help="write the active query set to PATH and exit")
    parser.add_argument("--min-recall", type=float,
                        default=DEFAULT_THRESHOLDS["min_recall_at_k"])
    parser.add_argument("--min-mrr", type=float,
                        default=DEFAULT_THRESHOLDS["min_mrr"])
    parser.add_argument("--max-duplicate-rate", type=float,
                        default=DEFAULT_THRESHOLDS["max_duplicate_rate"])
    parser.add_argument("--max-p95-ms", type=float,
                        default=DEFAULT_THRESHOLDS["max_p95_ms"])
    parser.add_argument("--min-queries", type=int,
                        default=DEFAULT_THRESHOLDS["min_queries"])
    parser.add_argument("--no-gate", action="store_true",
                        help="report metrics but always exit 0")
    parser.add_argument("--ledger-stats", action="store_true",
                        help="include ledger size/claim counts in the report")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    try:
        queries = (load_queries(args.queries) if args.queries
                   else validate_queries(LABELLED_QUERIES))
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.dump_queries:
        path = Path(args.dump_queries)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(queries, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print("wrote %d queries to %s" % (len(queries), path))
        return 0
    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("error: ledger not found: %s" % ledger_path, file=sys.stderr)
        return 2
    ledger = SQLiteLedger(ledger_path)
    try:
        result = evaluate(ledger, queries, limit=args.limit)
        ledger_stats = ledger.stats() if args.ledger_stats else None
    finally:
        ledger.close()
    result["ledger"] = str(ledger_path)
    if ledger_stats is not None:
        result["ledger_stats"] = {
            key: ledger_stats.get(key)
            for key in ("memories", "raw_chat", "active", "claims",
                        "active_claims", "conflict_claims", "schema_version")
        }
    result["gate"] = check_gate(
        result,
        min_recall_at_k=args.min_recall,
        min_mrr=args.min_mrr,
        max_duplicate_rate=args.max_duplicate_rate,
        max_p95_ms=args.max_p95_ms,
        min_queries=args.min_queries,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_report(result)
    if args.no_gate:
        return 0
    return 0 if result["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
