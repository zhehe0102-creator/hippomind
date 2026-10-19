# HippoMind 深度代码审查报告（2026-08-22，前景轮九）

> 五遍审查，每遍不同侧重面。基线：396 测试绿、基准全过。
> 产出修复 3 项、性能优化 2 项、记录不修 2 项。

## 第 1 遍：正确性（边界条件/状态机/事务）

探针：`_clamp_int/_clamp_float` 边界（limit=-5/10¹²、signal=99/'abc'/None）、
claims 状态机（active→conflict→裁决）、forget 幂等与级联、pin 存在性校验、
空/超长查询、双时态窗口。

| 发现 | 处置 |
|---|---|
| feedback signal=99 → 钳到 1.0；'abc'/None → 0.0 | ✅ 行为正确（_clamp_float 默认值语义） |
| forget 同条重复执行第二次 forgotten=0 | ✅ 幂等 |
| claims 冲突：同主语异值双双进 conflict，facts 只显活跃视图 | ✅ 设计如此，冲突走 list_conflicts/resolve |
| pin 不存在记忆 → LedgerError | ✅ |
| query>4000 字符 → LedgerError | ✅ |

**结论：无缺陷。**

## 第 2 遍：并发安全与资源泄漏

探针：8 线程×30 直写账本、6×25 HTTP 混合读写、200 次检索后 fd 计数、
写后读缓存可见性。

| 发现 | 处置 |
|---|---|
| 直写 240/240、HTTP 150 请求零错误、fd 无泄漏 | ✅ |
| 写后读立即可见（缓存键含写代数 generation，写事务自增即整体失效） | ✅ 设计正确 |
| **8 客户端 ×8000 条压测第 4 轮起出现 `Connection reset by peer`**（128 条丢失） | 🔧 **已修**：见修复 #2 |

## 第 3 遍：错误处理与输入验证

探针：畸形 JSON/空 body/数组 body/缺 evidence/1MB 超长/错误 Content-Type。

| 发现 | 处置 |
|---|---|
| 前 5 项全部 400，服务保持健康 | ✅ |
| **超长 body 服务端回 413 但客户端收到连接重置**——413 响应在排空请求体前关闭连接，客户端永远读不到 | 🔧 **已修**：见修复 #1 |

## 第 4 遍：性能热点剖析

工具：tests/profile_recall.py（300 语料/60 查询）。

| 热点 | 优化 | 数字 |
|---|---|---|
| `copy.deepcopy` 于 search 缓存存取两路（110,530 次调用，占剖析时间 ~35%） | **优化 #1**：定向浅拷贝 `_copy_rows`（dict+三个已知嵌套容器），免递归/memo | 剖析总耗时 0.136s→0.091s（**-33%**） |
| `_query_coverage` 每行重做查询侧正则/词表/短语展开；`token_sim` 每行重切常量查询 | **优化 #2**：拆成 prepare（循环外一次）+ apply（逐行）；查询 token 提升 | 0.091s→0.077s（两轮累计 **-43%**），函数调用 372k→299k |

评分等价性由双语 QA 基准锁定（全 1.0 保持）。

## 第 5 遍：API 一致性与文档承诺对齐

| 发现 | 处置 |
|---|---|
| 返回形态不一致：search/list_facts/recent_subjects 返回裸 list，list_conflicts/entity_graph 返回带元数据 dict | 📝 记录不修：各消费方（CLI/MCP/GUI）已按各自形态适配，统一收益低、破坏性风险高 |
| README 写「10 个工具」，MCP 实际已有 11 个（hippomind_graph） | 🔧 **已修**：见修复 #3 |

## 修复清单（均附回归测试）

1. **413 排空**（daemon.py `_read_json`）：超长请求先排空 body（上限
   MAX_REQUEST_BYTES×8 防伪造 Content-Length）再回 413+Connection: close。
   测试：`tests/test_review_fixes.py::test_oversized_body_client_receives_413_not_reset`。
2. **监听队列容量**（MemoryHubHTTPServer）：request_queue_size 5→128。
   默认 backlog=5 在多 agent keep-alive 下溢出，内核 reset 新连接。
   压测验证：修复前第 4 轮必现 reset（丢 128 条）；修复后 6 轮×8000=48000 行
   零错误零丢失。测试：`test_listen_backlog_sized_for_concurrency`。
3. **README 工具数漂移**：10→11 并点名 hippomind_graph。

## 回归

- `python3 -m unittest discover -s tests`：398 全绿（新增 2 项修复回归）
- `python3 tests/run_all_benchmarks.py --quick`：全部通过
