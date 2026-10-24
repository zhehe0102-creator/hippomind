#!/usr/bin/env python3
"""Injection effectiveness report: did the agent reuse what we injected?

The report is layered on purpose.  The first thing it prints is how many
samples the numbers rest on, because every ratio below it is meaningless
without that context.  When the sample is too small the report says so in
plain language instead of presenting a tidy-looking percentage.

Sections
--------
1. 样本      total traces, traces with memory ids, evaluable samples, skips
2. 指标      overlap / citation / paraphrase / correction with Wilson CIs
3. 对照       matched not-injected arm (observational, never causal)
4. 分层       by agent, by injected-item count, by trace source
5. 诊断       why the sample is the size it is (capture health per agent)
6. 结论        what may and may not be claimed

The ledger is opened read-only: measuring production must never mutate it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.injection_quality import (  # noqa: E402
    MIN_SAMPLES,
    ReadOnlyLedger,
    default_embedder,
    default_ledger_path,
    report,
)


def _fmt(value, spec="%.3f", dash="n/a"):
    if value is None:
        return dash
    try:
        return spec % value
    except (TypeError, ValueError):
        return str(value)


def _fmt_ci(ci, dash="n/a"):
    if not ci:
        return dash
    return "[%.3f, %.3f]" % (ci[0], ci[1])


def _print_sample(result) -> None:
    sample = result["sample"]
    print("1. 样本")
    print("   trace 总数            %d" % result["traces"])
    print("   记录了 memory_ids 的  %d" % result["traced_with_ids"])
    print("   memory 仍存在         %d" % result["traces_with_live_memories"])
    print("   == 可用样本 ==        %d   (阈值 %d)" % (sample["sampled"], sample["min_samples"]))
    if sample["shortfall"]:
        print("   距阈值还差            %d" % sample["shortfall"])
    if sample["skip_reasons"]:
        print("   被跳过的原因：")
        labels = {
            "no_memory_ids": "trace 未记录 memory_ids（早期版本不写该字段）",
            "bad_timestamp": "时间戳无法解析",
            "memories_missing": "注入的 memory 已被遗忘/清除",
            "no_replies": "窗口内没有可读的 assistant 回复",
        }
        for reason, count in sorted(sample["skip_reasons"].items()):
            print("     %-18s %2d  %s" % (reason, count, labels.get(reason, "")))
    print()


def _print_metrics(result) -> None:
    metrics = result["metrics"]
    overlap = metrics["overlap"]
    citation = metrics["citation"]
    para = metrics["paraphrase"]
    corr = metrics["correction"]
    print("2. 指标（分母 = 可用样本 %d）" % result["sampled"])
    print("   词面复用 overlap     %s  95%%CI %s   (复用 %d/%d, 均值 %s)" % (
        _fmt(overlap["rate"]), _fmt_ci(overlap["ci"]),
        overlap["used"], result["sampled"], _fmt(overlap["mean"])))
    print("   信号引用 citation    %s  95%%CI %s   (引用 %d/%d, 均值 %s)" % (
        _fmt(citation["rate"]), _fmt_ci(citation["ci"]),
        citation["cited"], result["sampled"], _fmt(citation["mean"])))
    print("   语义复述 paraphrase  %s  (n=%d, %s)" % (
        _fmt(para["mean"]), para["n"], "已启用 LSA" if para["enabled"] else "未启用（无模型）"))
    print("   纠错 correction      %s  95%%CI %s   (负向信号, %d/%d)" % (
        _fmt(corr["rate"]), _fmt_ci(corr["ci"]), corr["corrected"], result["sampled"]))
    print("   指标定义见 docs/INJECTION.md；所有比率都是观察性代理指标。")
    print()


def _print_control(result) -> None:
    control = result["control"]
    print("3. 对照（观察性，非随机分组）")
    print("   模式                 %s" % control["mode"])
    print("   配对数量             %d" % control["pairs"])
    print("   平均 lift            %s   (注入项信号复用率 - 未注入同类记忆)" % _fmt(control["lift_mean"], "%+.3f"))
    if control["mode"] == "none":
        print("   说明                 无法构造对照臂：没有可比较的未注入记忆配对。")
    print("   %s" % control["note"])
    print()


def _print_strata(result) -> None:
    strata = result.get("strata") or {}
    print("4. 分层")
    for dimension, label in (("by_agent", "按 agent"),
                             ("by_items", "按注入条数"),
                             ("by_source", "按来源")):
        buckets = strata.get(dimension) or {}
        if not buckets:
            continue
        print("   %s：" % label)
        for name, stats in sorted(buckets.items()):
            print("     %-14s trace=%d 可用=%d 复用率=%s 引用率=%s 纠错率=%s" % (
                name, _trace_count(result, dimension, name), stats["sampled"],
                _fmt(stats["reuse_rate"]), _fmt(stats["citation_rate"]),
                _fmt(stats["correction_rate"])))
    print()


def _trace_count(result, dimension, name) -> int:
    """Rows in a stratum (evaluable or not), recomputed from details."""
    total = 0
    for row in result.get("details") or []:
        if dimension == "by_agent" and str(row.get("agent") or "(unknown)") == name:
            total += 1
        elif dimension == "by_source" and str(row.get("source") or "(unknown)") == name:
            total += 1
        elif dimension == "by_items":
            count = int(row.get("memory_ids") or 0)
            bucket = "1" if count <= 1 else ("2-4" if count <= 4 else "5+")
            if bucket == name:
                total += 1
    return total


def _print_diagnostics(result) -> None:
    diagnostics = result.get("diagnostics") or {}
    agents = diagnostics.get("agents") or {}
    print("5. 诊断（为什么样本是这个数）")
    print("   账本最近一条 assistant 消息： %s" % (diagnostics.get("ledger_last_assistant_at") or "无"))
    if not agents:
        print("   没有 agent 出现在 trace 里。")
    for agent, entry in sorted(agents.items()):
        print("   agent=%s" % agent)
        print("     最近注入            %s" % (entry.get("last_injection_at") or "无"))
        print("     最近 assistant 消息  %s" % (entry.get("last_assistant_at") or "从未捕获"))
        print("     该 agent 累计回复    %d 条" % entry.get("assistant_total", 0))
        print("     注入后 24h 内回复    %d 条" % entry.get("assistant_after_injection", 0))
        capture = entry.get("capture") or {}
        print("     游标                 %d 个 · 错误 %d · 未读完 %d · 最近扫描 %s" % (
            capture.get("cursors", 0), capture.get("errors", 0),
            capture.get("backlog", 0), capture.get("last_scan_at") or "无"))
        verdict = entry.get("verdict")
        if verdict == "replies_available":
            print("     结论                窗口内有回复，逐条结果见下")
        elif verdict == "capture_stalled":
            print("     结论                【采集故障】游标记录了错误，需要修 bug；"
                  "在修好之前注入效果无法测量")
        elif verdict == "source_idle":
            print("     结论                【不是故障】采集器在注入之后仍正常扫描，"
                  "只是磁盘上没有新消息 —— 该 agent 近期没被使用。"
                  "要积累样本只能换一个真在用的 agent。")
        elif verdict == "capture_unverified":
            print("     结论                【无法判断】最近一次扫描早于注入，"
                  "不能排除有未读数据；需要确认采集器是否仍在运行")
        else:
            print("     结论                【未知】没有该 agent 的采集游标，采集状态不可判定")
        if capture.get("backlog"):
            print("     %-19s %d 个文件尚未读完（采集器每轮限量读取，属正常节流，不是故障）"
                  % ("未读完提示", capture["backlog"]))
    print()


def _print_verdict(result) -> None:
    verdict = result["verdict"]
    print("6. 结论")
    print("   %s" % verdict["headline"])
    print("   可否下结论： %s" % ("是（观察性）" if verdict["conclusive"] else "否"))
    if verdict.get("caveats"):
        print("   不确定性：")
        for caveat in verdict["caveats"]:
            print("     - %s" % caveat)
    print()


def _print_details(result, limit=12) -> None:
    rows = result.get("details") or []
    if not rows:
        return
    print("7. 逐条明细（最近 %d 条）" % min(limit, len(rows)))
    for row in rows[-limit:]:
        print("   %s agent=%-10s ids=%-2d replies=%-2d/%s overlap=%s cite=%s para=%s corr=%s %s" % (
            row.get("at"), row.get("agent") or "-", int(row.get("memory_ids") or 0),
            int(row.get("replies") or 0), row.get("replies_raw"),
            _fmt(row.get("overlap")), _fmt(row.get("citation_rate")),
            _fmt(row.get("paraphrase")), "Y" if row.get("corrected") else "n",
            ("跳过=" + row["skip_reason"]) if row.get("skip_reason") else ""))
    print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Injection reuse report")
    parser.add_argument("--ledger", default=os.path.expanduser(str(default_ledger_path())))
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--limit", type=int, default=500,
                        help="最多读取多少条 trace（取最近 N 条）")
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    parser.add_argument("--no-embedder", action="store_true",
                        help="跳过语义复述（不加载 LSA 模型）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("账本不存在: %s" % ledger_path, file=sys.stderr)
        return 2
    ledger = ReadOnlyLedger(ledger_path)
    try:
        result = report(ledger, limit=args.limit, window_minutes=args.window_minutes,
                        embedder=(None if args.no_embedder else default_embedder()),
                        min_samples=args.min_samples)
    finally:
        ledger.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("注入效果报告 · 窗口 %d 分钟 · 账本 %s" % (result["window_minutes"], ledger_path))
    print("=" * 78)
    _print_sample(result)
    _print_metrics(result)
    _print_control(result)
    _print_strata(result)
    _print_diagnostics(result)
    _print_verdict(result)
    _print_details(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
