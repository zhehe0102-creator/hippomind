#!/usr/bin/env python3
"""Injection A/B report: per-arm delivery, cost and reuse.

Reads the trace file with a bounded tail read (injection_trace.recent) and opens
the ledger read-only (mode=ro via injection_quality.ReadOnlyLedger).  Per-trace
reuse/citation matching is delegated to injection_quality.evaluate() - the same
term-overlap and reply-window logic scripts/injection_report.py uses - so this
script cannot drift from the production measurement code.

The report separates:
  1. experiment switch and sample arithmetic;
  2. per-arm delivery counts, item counts and server latency;
  3. per-arm reuse/overlap/citation over the evaluable subset;
  4. an explicit conclusion, or the exact number of extra samples/prompts needed
     before a conclusion is allowed (MIN_SAMPLES = 30 per arm).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import injection_ab, injection_trace  # noqa: E402
from hippomind.injection_quality import (  # noqa: E402
    MIN_SAMPLES,
    ReadOnlyLedger,
    default_ledger_path,
    evaluate,
)

ARMS = (injection_ab.TREATMENT, injection_ab.CONTROL, "unknown")


def _mean(values: Sequence[float]) -> Optional[float]:
    clean = [float(value) for value in values
             if isinstance(value, (int, float))]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 3)


def _rate(rows: Sequence[Dict[str, Any]], key: str) -> Optional[float]:
    if not rows:
        return None
    return round(sum(1 for row in rows if row.get(key)) / len(rows), 3)


def _arm(trace: Dict[str, Any]) -> str:
    value = str(trace.get("arm") or "")
    return value if value in ARMS else "unknown"


def arm_stats(traces: Sequence[Dict[str, Any]],
              details: Sequence[Dict[str, Any]], *,
              min_samples: int) -> Dict[str, Dict[str, Any]]:
    """Delivery/cost from raw trace rows; reuse from evaluate() details."""
    out: Dict[str, Dict[str, Any]] = {}
    for arm in ARMS:
        raw = [trace for trace in traces if _arm(trace) == arm]
        kept = [trace for trace in raw if trace.get("injected")]
        detail_rows = [row for row in details
                       if str(row.get("arm") or "unknown") == arm]
        evaluable = [row for row in detail_rows if row.get("evaluable")]
        latencies = [trace.get("latency_ms") for trace in raw
                     if isinstance(trace.get("latency_ms"), (int, float))]
        out[arm] = {
            "traces": len(raw),
            "injected": len(kept),
            "injection_rate": (round(len(kept) / len(raw), 3) if raw else None),
            "mean_items": _mean([int(trace.get("items") or 0) for trace in raw]),
            "latency_ms_mean": _mean(latencies),
            "latency_samples": len(latencies),
            "memory_ids_recorded": sum(1 for trace in raw if trace.get("memory_ids")),
            "evaluable": len(evaluable),
            "reuse_rate": _rate(evaluable, "used"),
            "mean_overlap": _mean([row["overlap"] for row in evaluable]),
            "citation_rate": _rate(evaluable, "cited"),
            "corrected": sum(1 for row in evaluable if row.get("corrected")),
            "evaluable_shortfall": max(0, int(min_samples) - len(evaluable)),
        }
    return out


def _needed(arms: Dict[str, Dict[str, Any]], config: Dict[str, Any],
            min_samples: int) -> Dict[str, Any]:
    rate = float(config.get("rate") or 0.0)
    treatment_short = int(arms[injection_ab.TREATMENT]["evaluable_shortfall"])
    control_short = int(arms[injection_ab.CONTROL]["evaluable_shortfall"])
    needed: Dict[str, Any] = {
        "min_samples": int(min_samples),
        "treatment_shortfall": treatment_short,
        "control_shortfall": control_short,
        "prompts_needed_for_treatment_samples": (
            int(round(treatment_short / max(rate, 1e-9))) if treatment_short else 0),
        "prompts_needed_for_control_samples": (
            int(round(control_short / rate)) if control_short and rate > 0 else None),
    }
    return needed


def _conclusion(arms: Dict[str, Dict[str, Any]], config: Dict[str, Any],
                needed: Dict[str, Any], min_samples: int,
                recorded_arms: bool = False) -> Dict[str, Any]:
    treatment = arms[injection_ab.TREATMENT]
    control = arms[injection_ab.CONTROL]
    if not config.get("enabled") and not recorded_arms:
        return {"code": "ab_disabled", "conclusive": False,
                "headline": ("A/B 实验未开启：所有 trace 都是 treatment/unknown，"
                             "没有随机对照样本。开启方式：python3 scripts/ab_control.py on"),
                "caveats": ["关闭状态下不存在对照组，任何「注入有效」结论都不成立。"]}
    caveats = ["关闭状态下不存在对照组，任何「注入有效」结论都不成立。"]
    if not config.get("enabled"):
        caveats = ["当前实验开关已关闭；以下样本来自开关开启期间记录的历史 trace。"]
    if treatment["evaluable"] == 0 and control["evaluable"] == 0:
        return {"code": "no_samples", "conclusive": False,
                "headline": ("两臂都还没有可评估样本（trace 需记录 memory_ids，且注入后 "
                             "窗口内同 agent 有 assistant 回复）。"),
                "needed": needed,
                "caveats": ["没有样本时不要解释注入率或复用率。"] + caveats}
    if min(treatment["evaluable"], control["evaluable"]) < int(min_samples):
        binding_arm = ("control" if control["evaluable"] <= treatment["evaluable"]
                       else "treatment")
        short = (needed["control_shortfall"] if binding_arm == "control"
                 else needed["treatment_shortfall"])
        prompts = (needed["prompts_needed_for_control_samples"]
                   if binding_arm == "control"
                   else needed["prompts_needed_for_treatment_samples"])
        return {"code": "insufficient_sample", "conclusive": False,
                "headline": ("样本不足：treatment=%d / control=%d，阈值 %d 每臂；"
                             "至少还差 %d 条可评估 %s 样本%s。"
                             % (treatment["evaluable"], control["evaluable"],
                                int(min_samples), short, binding_arm,
                                "（按当前 control_rate 约需 %d 条 prompt）" % prompts
                                if prompts else "")),
                "needed": needed,
                "caveats": ["样本不足时只把读数当排查线索，不能作为效果结论。"]}
    difference = None
    if treatment["reuse_rate"] is not None and control["reuse_rate"] is not None:
        difference = round(treatment["reuse_rate"] - control["reuse_rate"], 3)
    return {"code": "conclusive",
            "conclusive": True,
            "headline": ("随机分组配对比较：treatment 复用率 %s（n=%d） vs control %s"
                         "（n=%d），差值 %s。"
                         % (treatment["reuse_rate"], treatment["evaluable"],
                            control["reuse_rate"], control["evaluable"],
                            difference)),
            "difference": difference,
            "caveats": [
                "两臂都检索同一候选集、只有投递不同，差异可作因果估计。",
                "单臂样本刚过阈值时置信区间仍宽；复用率是词面重叠代理指标，"
                "不代表回答质量。",
                "回复窗口内的其他工作会稀释两臂指标。"]}


def build_report(ledger, *, traces: Sequence[Dict[str, Any]],
                 window_minutes: int = 30, min_samples: int = MIN_SAMPLES,
                 now: Optional[float] = None) -> Dict[str, Any]:
    evaluation = evaluate(ledger, list(traces),
                          window_minutes=window_minutes, embedder=None,
                          background=False, controls=False,
                          min_samples=min_samples)
    details = evaluation.get("details") or []
    arms = arm_stats(traces, details, min_samples=min_samples)
    config = injection_ab.explain(now=now)
    needed = _needed(arms, config, min_samples)
    recorded_arms = any(str(trace.get("arm") or "")
                        in (injection_ab.TREATMENT, injection_ab.CONTROL)
                        for trace in traces)
    return {
        "generated_at": None,
        "window_minutes": evaluation.get("window_minutes", window_minutes),
        "traces": len(traces),
        "min_samples": int(min_samples),
        "experiment": config,
        "arms": arms,
        "needed": needed,
        "conclusion": _conclusion(arms, config, needed, min_samples,
                                  recorded_arms=recorded_arms),
        "recorded_arms": recorded_arms,
        "evaluation": {
            "sampled": evaluation.get("sampled"),
            "reuse_rate": evaluation.get("reuse_rate"),
            "mean_overlap": evaluation.get("mean_overlap"),
            "sample": evaluation.get("sample"),
        },
        "notes": [
            "trace 读取是有界 tail（injection_trace.TAIL_READ_BYTES），不是全文件扫描。",
            "账本以 mode=ro 打开，测量过程不会写入生产库。",
        ],
    }


def _fmt(value, spec="%s") -> str:
    if value is None:
        return "n/a"
    try:
        return spec % value
    except (TypeError, ValueError):
        return str(value)


def _print_human(report: Dict[str, Any], ledger_path: Path) -> None:
    config = report["experiment"]
    arms = report["arms"]
    needed = report["needed"]
    conclusion = report["conclusion"]
    print("注入 A/B 报告 · 窗口 %d 分钟 · 账本 %s"
          % (report["window_minutes"], ledger_path))
    print("=" * 78)
    print("1. 实验开关")
    print("   状态                %s" % ("已开启" if config["enabled"] else "已关闭（全部注入）"))
    print("   对照比例            %.2f" % float(config.get("rate") or 0.0))
    print("   阈值                %d 条可评估样本/臂" % report["min_samples"])
    print("   trace 读取          %d 条（有界尾部）" % report["traces"])
    if config.get("enabled") and config.get("prompts_needed_for_min_samples"):
        print("   凑够对照样本约需    %d 条 prompt"
              % config["prompts_needed_for_min_samples"])
    print()
    print("2. 每臂指标（可评估 = trace 有 memory_ids 且窗口内有 reply）")
    header = "   %-12s %6s %6s %8s %8s %8s %8s %8s"
    print(header % ("arm", "traces", "注入", "注入率", "条目均", "延迟ms",
                    "可评估", "复用率"))
    for arm in ARMS:
        row = arms[arm]
        print(header % (arm, row["traces"], row["injected"],
                        _fmt(row["injection_rate"], "%.2f"),
                        _fmt(row["mean_items"], "%.2f"),
                        _fmt(row["latency_ms_mean"], "%.1f"),
                        row["evaluable"],
                        _fmt(row["reuse_rate"], "%.3f")))
    print("   注入率 = injected / traces；复用率 = 词面重叠达阈值的可评估样本占比。")
    print("   control 臂仍检索并记录 memory_ids，仅投递被扣留（配对对照）。")
    print()
    print("3. 样本缺口")
    print("   treatment 还差      %d 条可评估样本" % needed["treatment_shortfall"])
    print("   control 还差        %d 条可评估样本" % needed["control_shortfall"])
    if needed["prompts_needed_for_control_samples"]:
        print("   对照样本所需 prompt %d 条（按当前对照比例）"
              % needed["prompts_needed_for_control_samples"])
    print()
    print("4. 结论")
    print("   [%s] %s" % (conclusion["code"], conclusion["headline"]))
    for caveat in conclusion.get("caveats") or []:
        print("   - %s" % caveat)
    print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Injection A/B report")
    parser.add_argument("--ledger", default=str(default_ledger_path()))
    parser.add_argument("--limit", type=int, default=500,
                        help="最多读取多少条 trace（有界尾部）")
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("账本不存在: %s" % ledger_path, file=sys.stderr)
        return 2
    traces = injection_trace.recent(max(1, int(args.limit)))
    ledger = ReadOnlyLedger(ledger_path)
    try:
        report = build_report(ledger, traces=traces,
                              window_minutes=args.window_minutes,
                              min_samples=args.min_samples)
    finally:
        ledger.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    _print_human(report, ledger_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
