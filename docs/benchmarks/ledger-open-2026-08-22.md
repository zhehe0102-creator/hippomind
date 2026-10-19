# Ledger 打开耗时剖析（2026-08-22，迭代 87）

## 结论

1. **打开+首查成本随库体积线性增长，约 0.9ms/MB**：主因是首个 FTS 查询的
   页缓存预热，不是进程启动本身。
2. **肥 WAL 不是威胁**：20.6MB 未合并 WAL 的冷启动仅比已 checkpoint 库慢
   ~2.5ms——SQLite 的 WAL 恢复设计高效。异常断电后无需手动干预。
3. **同进程重复打开收益有限**（warm ≈ cold − 解释器启动 ~2.5ms）：每次
   `SQLiteLedger(path)` 都要重新预热页缓存；长驻 daemon 路径（本系统默认）
   天然规避此成本。

## 测量数据

| 场景 | 行数 | 库体积 | WAL | 冷启动(子进程) | 热打开(同进程中位) |
|---|---|---|---|---|---|
| 小库·已checkpoint | 1k | 1.6MB | 0 | 11.9ms | 8.4ms |
| 大库·已checkpoint | 50k | 69.5MB | 0 | 64.1ms | 62.0ms |
| 大库·肥WAL快照 | 50k | 69.5MB | 20.6MB | 66.6ms | 62.7ms |

方法：cold=min(3 次独立子进程，含解释器与 import)；warm=同进程 20 次中位；
肥 WAL 用 `wal_autocheckpoint=0` 独立连接全表触碰制造，并在持有连接期间
快照 `.sqlite/-wal/-shm` 三件套，保证被测进程见到真实未合并状态。

## 运维建议

- 常规使用无需关心 WAL 大小；`maintain --vacuum` 与优雅停机的
  checkpoint 是空间整理手段而非性能急救。
- 若嵌入脚本频繁开关账本（每条命令一次），50k 级库单次 ~60ms 可感知，
  批量操作请复用单个 SQLiteLedger 实例或走 daemon。

## 复现

```bash
python3 tests/profile_ledger_open.py
```
