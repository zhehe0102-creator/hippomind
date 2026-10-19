# 基准报告索引

| 报告 | 一句话结论 |
|---|---|
| [2026-08-21-round.md](2026-08-21-round.md) | 对比 mem0/zep/letta：本地优先+审计粒度是差异化优势 |
| [recall-profile-2026-08-21.md](recall-profile-2026-08-21.md) | 读连接缓存后 231.6 qps（+25%），瓶颈从连接转为评分管道 |
| [scale-curve-2026-08-21.md](scale-curve-2026-08-21.md) | 1k/10k/50k 曲线：trigram 后 search p50 23.6/92.8ms |
| [roundtrip-2026-08-21.md](roundtrip-2026-08-21.md) | 10k 行导出 0.12s / 导入 0.76s，重导完全幂等 |
| [cjk-tokenization-eval-2026-08-21.md](cjk-tokenization-eval-2026-08-21.md) | jieba/n-gram 收益不抵成本；LIKE 救援裁剪至 92ms |
| [2026-08-22-summary.md](2026-08-22-summary.md) | 当日汇总：HTTP 95.5 RPS / 并发写 11,014 RPS 零错误 |
| [cursor-pagination-eval-2026-08-22.md](cursor-pagination-eval-2026-08-22.md) | 深分页空页缺陷修复；穷举遍历建议 keyset/export |
| [ledger-open-2026-08-22.md](ledger-open-2026-08-22.md) | 打开 ~0.9ms/MB；20MB 肥 WAL 冷启动仅 +2.5ms |

复现入口：`python3 tests/run_all_benchmarks.py --quick`
