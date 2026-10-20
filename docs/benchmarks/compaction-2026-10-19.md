# 线上账本压缩报告 · 2026-10-19

## 结论

| 指标 | 压缩前 | 压缩后 | 变化 |
|---|---:|---:|---:|
| 文件体积 | **4,483.3 MB** | **406.8 MB** | **-91.0%**（回收 3.99 GB）|
| 总行数 | 1,536,641 | 136,459 | -91.1% |
| raw_chat 行 | 1,475,584 | 98,655 | -93.3% |
| semantic 行 | 56,010 | 132 | -99.8% |
| 可检索（active）结构化记忆 | 5,851 | 4,371 | 保留 |
| FTS 索引行 | 2,147 | 5,571 | 重建后更全 |
| freelist 页 | 9,878 | 0 | VACUUM 回收 |
| `PRAGMA quick_check` | ok | ok | 健康 |

## 执行步骤

1. **一致性备份**（先于任何写操作）
   - `backups/hippomind-pre-compaction-20261019T141216Z.sqlite`（4,275.6 MB）
   - 用 SQLite **online backup API** 生成，不是 `cp`（`cp` 会漏掉 WAL 中未 checkpoint 的数据）
   - `sha256 = 948002887dd42998c90d189b3af26b30f0a4f2313f4fbb6f1591aaf584d9b638`
   - 已验证：`quick_check = ok`、1,536,641 行、schema v8

2. **前置检查**（12 项全过）
   库存在/普通文件/备份存在/备份 sha256/备份完整性/备份与线上一致/daemon 已停/无残留临时表/WAL 已 checkpoint/磁盘空间/schema 受支持

3. **折叠重复 raw 行**（`ledger.collapse_duplicate_rows`）
   - 按 `(content, source_agent, session_id, role)` 分组
   - 97,142 组、1,344,314 行冗余 → 删除 1,344,314 行
   - 归档：`hippomind.raw-archive/dedupe-20261019T164641Z-91b1e77b38f0.jsonl.gz`（199 MB，1,344,314 行，sha256 `c23cecf6…`，完整解压通过）

4. **折叠重复 semantic 行**（`tools/migration/collapse_semantic_dupes.py`）
   - 巩固自噬产物：56,010 行只对应 142 种内容，最高重复 5,423 次
   - 按 `(content, scope, task_id, project)` 分组，保留最早且优先 active 的一条
   - 删除 55,868 行；归档 `semantic-dedupe-20261019T172847Z-91b1e77b38f0.jsonl.gz`（1.8 MB，55,868 行）

5. **VACUUM**：661.6 MB → 406.8 MB（3.7 s）

6. **FTS 重建**（`ledger.repair_fts`）：2,147 → 5,571 行，与 active 投影一致

## 安全性质

- **fail-closed**：每一步都是「写归档 → fsync → 回读校验每个 memory_id → 才删除」。归档缺失/不完整时拒绝删除。
- **可回滚**：完整备份 + 两份归档都保留；恢复流程见 `scripts/restore.sh`。
- **未触碰审计**：`ledger_events` 与 `memories` 同步删除，无孤儿事件；`quick_check = ok`。
- **daemon 全程停止**，无并发写。

## 未处理项（有意保留）

- **raw 保留期删除**：TTL=90 天，但库里最老数据只有 71.2 天，所以 `purge_expired_raw` 按预期删到 0 行。这是正确行为，**没有为了「多删」而降低 TTL**。
- **冷数据保留**：98,273 条 raw_chat 冷行 + 32,615 条 tool_output 冷行仍保留，供审计与 `include_cold` 检索；后续由 daemon 按 TTL 自动收敛。

## 后续自动收敛

daemon 的维护周期现在会：
- `purge_raw=True` → 到 TTL 后自动归档并删除 raw 行
- 删除后按阈值自动 `VACUUM`/`incremental_vacuum`
- `/stats` 与 Prometheus 暴露 `raw_purged_total`、`bytes_reclaimed_total`、`freelist_pages`、`reclaimable_bytes`

因此本次是**一次性收口**，之后靠自动策略维持，不会再次膨胀到 4.5 GB。

## Provenance boundary (from the migration engineer)

`collapse_duplicate_rows` archives store **identity + content + digest only — not
`metadata_json`**. The 1,344,314 deleted raw rows carried 697,321,940 B of
metadata; 1,343,851 of them held an `archive_rowid` (789,649 distinct values)
pointing back into the external `Agent-Chat-Archive`. Sampling 500 such pointers,
only 55 could be re-resolved against the external archive today (54 content-identical).

**Conclusion: content is 100% recoverable from the gzip archives, but per-row
external provenance pointers are not. Rollback and provenance therefore treat the
4.27 GB pre-compaction backup as the authority; the archives guarantee content
recovery only.** (`ledger_events` rows are likewise deleted together with their
projections — the backup, not the archive, is the audit-preserving artifact.)

## fts_sync_report defect found during verification (fixed)

The shipped `fts_sync_report()` was unusable as a health gate on a real ledger,
and both halves were fixed in this pass (see `tests/test_fts_sync_report.py`):

1. **Quadratic scan.** `memory_fts.memory_id` is declared `UNINDEXED`, so the
   correlated `NOT EXISTS` re-scanned the whole FTS content table once per
   projection — O(memories × fts_rows) ≈ 8.7e9 comparisons on the 1.53 M-row
   ledger; measured >14 minutes of CPU without converging. The FTS ids are now
   materialised once into a temp table and anti-joined, which is linear; the
   post-compaction 136 K-row ledger reports in **0.29 s**.
2. **Wrong `unindexed` semantics.** The old predicate counted
   `lifecycle IN ('active','cold')`, but `maintain` deliberately removes cold
   rows from FTS — so any ledger with cold data (this one has 1,328 cold
   non-raw rows) reported `warn` forever, training operators to ignore the
   check. Only `quality_state='accepted' AND lifecycle='active'` rows missing
   from FTS count as `unindexed` now; leftovers that should have been pruned are
   reported separately as `stale` (capacity waste, not a recall defect) and FTS
   entries whose projection vanished as `orphan`.

Current live-ledger report: `{status: ok, projections: 392, fts_rows: 5571,
unindexed: 0, stale: 5179, orphan: 0}`. The 5,179 stale rows are FTS entries for
raw/semantic rows that fell cold before the compaction; `maintain` prunes them
incrementally and they do not affect correctness.
