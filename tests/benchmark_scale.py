#!/usr/bin/env python3
"""大库性能曲线：1k/10k/50k 行下的 ingest 与 recall 吞吐退化测量。

用法：python3 tests/benchmark_scale.py [--sizes 1000,10000,50000] [--q 40]
产出：控制台表格 + docs/benchmarks/scale-curve-<日期>.md
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
from hippomind.ledger import SQLiteLedger

QUERIES = [
    "生产数据库端口是多少", "缓存服务部署在哪台机器", "CI 构建超时时间",
    "API 网关限流配置", "kafka 分区数量", "审计日志保留多久",
    "支付接口当前路径", "redis 内存大小", "生产环境在哪个区域",
    "媒体文件存储桶", "sso 登录配置", "备份策略是怎样的",
]

CORPUS_TMPL = [
    "生产 PostgreSQL 数据库运行在 {i} 号主机 db-{i:02d}，端口 5432",
    "Redis 缓存部署在 cache-{i:02d}，分配内存 32G",
    "部署流水线使用 GitHub Actions，构建超时 {i} 分钟",
    "API 网关限流为每分钟 {i}00 次请求",
    "kafka 集群共 {i} 个分区，副本因子 3",
    "应用日志保留 {i} 天，审计日志保留 365 天",
    "对象存储桶 media-prod 位于 east-{i} 区域",
    "SSO 使用 OIDC 协议，issuer 为 sso-{i}.example.com",
    "全量备份每周日执行，增量备份每日执行，保留 {i} 天",
    "用户服务处理登录与注册，负责人 alice-{i}",
]


def make_rows(n: int) -> list:
    rows = []
    for i in range(n):
        tmpl = CORPUS_TMPL[i % len(CORPUS_TMPL)]
        j = i // len(CORPUS_TMPL)
        rows.append({
            "kind": "fact", "scope": "shared", "source_agent": f"scale-{j % 4}",
            "content": tmpl.format(i=j), "evidence": [f"scale:{i}"],
        })
    return rows


def bench_scale(n: int, q: int, root: Path) -> dict:
    led = SQLiteLedger(root / f"scale-{n}.sqlite")
    rows = make_rows(n)

    t0 = time.perf_counter()
    for r in rows:
        led.ingest(r)
    ingest_s = time.perf_counter() - t0

    builder = ContextPackBuilder(led)
    opts = ContextPackOptions()
    search_ms, pack_ms = [], []
    # 预热一轮
    led.search(QUERIES[0], limit=8)
    builder.build(QUERIES[0], options=opts)
    for k in range(q):
        query = QUERIES[k % len(QUERIES)]
        t1 = time.perf_counter()
        led.search(query, limit=8)
        search_ms.append((time.perf_counter() - t1) * 1000)
        t2 = time.perf_counter()
        builder.build(query, options=opts)
        pack_ms.append((time.perf_counter() - t2) * 1000)

    def pct(vals, p):
        s = sorted(vals)
        return s[min(len(s) - 1, int(len(s) * p))]

    return {
        "rows": n,
        "ingest_rps": round(n / ingest_s, 1),
        "search_p50_ms": round(statistics.median(search_ms), 2),
        "search_p95_ms": round(pct(search_ms, 0.95), 2),
        "pack_p50_ms": round(statistics.median(pack_ms), 2),
        "pack_p95_ms": round(pct(pack_ms, 0.95), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1000,10000,50000")
    ap.add_argument("--q", type=int, default=40)
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="hippo-scale-")
    root = Path(tmp)
    results = []
    for n in (int(x) for x in args.sizes.split(",")):
        row = bench_scale(n, args.q, root)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False))

    lines = ["# 大库性能曲线（ingest/recall 随行数退化）", "",
             "| 行数 | ingest RPS | search p50/p95 ms | context-pack p50/p95 ms |",
             "|---|---|---|---|"]
    for r in results:
        lines.append(
            f"| {r['rows']} | {r['ingest_rps']} | "
            f"{r['search_p50_ms']} / {r['search_p95_ms']} | "
            f"{r['pack_p50_ms']} / {r['pack_p95_ms']} |")
    report = Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / \
        f"scale-curve-{time.strftime('%Y-%m-%d')}.md"
    report.write_text("\n".join(lines) + "\n")
    print(f"report -> {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
