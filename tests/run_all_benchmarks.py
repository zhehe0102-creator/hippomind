#!/usr/bin/env python3
"""一键基准汇总（迭代 57）：跑全部基准并生成当日报告索引。

用法：python3 tests/run_all_benchmarks.py [--quick]
产出：docs/benchmarks/<日期>-summary.md + 控制台摘要
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BENCHMARKS = [
    ("HTTP 并发", ["tests/benchmark_daemon_http.py"], True),
    ("并发写入", ["tests/benchmark_concurrent_ingest.py"], True),
    ("规模曲线", ["tests/benchmark_scale.py", "--sizes", "1000,10000,50000",
                  "--q", "40"], False),
    ("检索剖析", ["tests/profile_recall.py", "--n", "300", "--q", "60"], True),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过大规模段")
    args = ap.parse_args()

    date = time.strftime("%Y-%m-%d")
    lines = [f"# 基准汇总 · {date}", "",
             f"生成时间：{datetime.now(timezone.utc).isoformat()}", ""]
    failures = []
    for name, cmd, quick_safe in BENCHMARKS:
        if args.quick and not quick_safe:
            lines.append(f"## {name}\n\n（--quick 跳过）\n")
            continue
        print(f"[run] {name}: {' '.join(cmd)}", flush=True)
        t0 = time.perf_counter()
        proc = subprocess.run([sys.executable, *cmd], capture_output=True,
                              text=True, cwd=str(ROOT))
        elapsed = time.perf_counter() - t0
        tail = "\n".join(proc.stdout.strip().splitlines()[-6:])
        status = "OK" if proc.returncode == 0 else f"FAIL(rc={proc.returncode})"
        if proc.returncode != 0:
            failures.append(name)
            tail += f"\nstderr: {proc.stderr[-400:]}"
        lines.append(f"## {name} —— {status}（{elapsed:.1f}s）\n\n```\n{tail}\n```\n")
        print(f"      -> {status} {elapsed:.1f}s", flush=True)

    # 双语评测质量门
    quality = subprocess.run(
        [sys.executable, "-c",
         "import tempfile, json, sys; sys.path.insert(0, '.');\n"
         "from pathlib import Path\n"
         "from hippomind.ledger import SQLiteLedger\n"
         "from hippomind.retrieval_eval import run_memory_qa_suite, run_english_qa_suite\n"
         "tmp = tempfile.mkdtemp(); n = {'i': 0}\n"
         "def factory():\n"
         "    n['i'] += 1\n"
         "    return SQLiteLedger(Path(tmp) / f'e{n[\"i\"]}.sqlite')\n"
         "print(json.dumps({'zh': run_memory_qa_suite(factory),\n"
         "                  'en': run_english_qa_suite(factory)}))"],
        capture_output=True, text=True, cwd=str(ROOT))
    if quality.returncode == 0:
        lines.append(f"## 检索质量门\n\n```json\n{quality.stdout.strip()}\n```\n")
    else:
        failures.append("质量门")
        lines.append(f"## 检索质量门 —— FAIL\n\n```\n{quality.stderr[-400:]}\n```\n")

    summary = ROOT / "docs" / "benchmarks" / f"{date}-summary.md"
    summary.write_text("\n".join(lines))
    print(f"\nsummary -> {summary}")
    if failures:
        print("失败项:", ", ".join(failures))
        return 1
    print("全部基准通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
