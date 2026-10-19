# export/import 往返基准（10k 行，2026-08-21）

| 阶段 | 耗时 | 吞吐 |
|---|---|---|
| 逐条 ingest 10k（对照） | 22.25s | ~449/s |
| export_rows → JSONL | 0.12s | ~83k 行/s |
| import 10k（ingest_many 批量） | 0.76s | **13,176 行/s** |
| 重导同文件（幂等复证） | 0.51s | inserted=0 / duplicate=10000 |

结论：跨机迁移 1 万条记忆 <1 秒；content_hash 幂等复证通过；
导入库检索正常。批量通道比逐条快约 29 倍——大批量写入应走 ingest_many。
