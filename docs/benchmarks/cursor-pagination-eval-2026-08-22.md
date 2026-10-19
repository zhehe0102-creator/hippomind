# Cursor 分页评估（2026-08-22，迭代 79）

## 结论

1. **发现并修复正确性缺陷**：`search()` 的 FTS 候选池硬上限 1500 行导致
   `offset ≥ 1600` 时静默返回空页（10k 匹配只翻得到 ~1500 行）。修复：候选池
   上限随 `limit+offset` 缩放（FTS 20k / LIKE 8k 天花板），全量翻页 125 页
   不重不漏。
2. **offset 深分页成本为 O(offset)**：Python 评分管道（token_sim/coverage，
   每候选 ~6μs）主导——首页 1.6ms、offset=9000 约 54ms、125 页全量走完 20s。
3. **建议不变**：相关性排序的 search 分页适合前几十页；**穷举遍历用
   keyset 游标或 export_rows**（`created_at > 游标 ORDER BY created_at
   LIMIT n` 恒定 ~0.5ms/页，与深度无关）。

## 测量数据（10k 行库，limit=80）

| 方式 | 成本 | 备注 |
|---|---|---|
| offset=0 首页 | 1.6ms | 修复前后一致 |
| offset=1600（修复前） | **空页** | FTS 池 1500 截断 |
| offset=9000（修复后） | ~54ms | 纯 SQL 取 9080 候选 ≈0ms，Python 管道占 ~100% |
| keyset（created_at 游标） | ~0.5ms | 与深度无关，但不做相关性排序 |

## 决策

- 保留 offset API（兼容既有调用方与 LRU 缓存键），池上限提升修复正确性；
- 不引入 cursor 参数到 search()——keyset 已可经 SQL/export 路径使用，且
  相关性排序没有稳定的"下一页游标"语义（写入会改变排名）；
- 后续若出现真实深分页需求，优先做「FTS 覆盖充分时跳过 Python 重评分」
  的短路优化，而非新 API。

## 回归锁定

`tests/test_deep_pagination.py`：深 offset 返回满页 / 全量翻页不重不漏 /
首页成本上限。
