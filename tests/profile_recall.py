#!/usr/bin/env python3
"""检索延迟剖析：cProfile 包裹真实 recall 工作负载，输出热点报告。

用法：python3 tests/profile_recall.py [--n 300] [--q 60]
产出：控制台 top-25 累计热点 + docs/benchmarks/recall-profile-<日期>.md
"""
import argparse
import cProfile
import io
import pstats
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
    "用户服务负责人是谁", "部署流水线用什么工具", "数据库迁移到哪个端口",
    "媒体文件存储桶", "sso 登录配置", "备份策略是怎样的",
    "业务部署在哪个区域", "上周的部署记录", "昨天的告警",
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


def build_workload(n: int, root: Path) -> SQLiteLedger:
    led = SQLiteLedger(root / "profile.sqlite")
    rows = []
    for i in range(max(1, n // len(CORPUS_TMPL))):
        for j, tmpl in enumerate(CORPUS_TMPL):
            text = tmpl.format(i=i)
            rows.append({
                "kind": "fact", "scope": "shared", "source_agent": f"profiler-{j % 3}",
                "content": text, "evidence": [f"profile:{i}:{j}"],
            })
    led.ingest_many(rows)
    return led


def run_queries(led: SQLiteLedger, rounds: int) -> None:
    builder = ContextPackBuilder(led)
    opts = ContextPackOptions()
    for r in range(rounds):
        q = QUERIES[r % len(QUERIES)]
        led.search(q, limit=8)
        builder.build(q, options=opts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300, help="语料条数")
    ap.add_argument("--q", type=int, default=60, help="查询轮数")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="hippo-profile-")
    led = build_workload(args.n, Path(tmp))

    # 预热（连接/缓存/FTS）
    run_queries(led, 3)

    prof = cProfile.Profile()
    t0 = time.perf_counter()
    prof.enable()
    run_queries(led, args.q)
    prof.disable()
    wall = time.perf_counter() - t0

    buf = io.StringIO()
    st = pstats.Stats(prof, stream=buf)
    st.sort_stats("cumulative").print_stats(25)
    out = buf.getvalue()

    print(f"workload: {args.n} 条语料 / {args.q} 轮查询 / 墙钟 {wall:.2f}s "
          f"({args.q / wall:.1f} qps)")
    print(out)

    # 落盘报告
    bench = Path(__file__).resolve().parents[1] / "docs" / "benchmarks"
    bench.mkdir(parents=True, exist_ok=True)
    report = bench / f"recall-profile-{time.strftime('%Y-%m-%d')}.md"
    report.write_text(
        "# recall 路径 cProfile 热点报告\n\n"
        f"- 语料：{args.n} 条；查询：{args.q} 轮；墙钟：{wall:.2f}s"
        f"（{args.q / wall:.1f} qps）\n\n```\n{out}\n```\n")
    print(f"report -> {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
