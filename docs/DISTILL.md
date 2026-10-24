# 自动提炼：真实积压、吞吐与参数依据

> 最后实测：2026-10-24 · 账本 `~/Library/Application Support/HippoMind/hippomind.sqlite`
> 复现：`python3 scripts/distill_status.py`（只读，不改账本）

本文只写**实测**结论。凡是估算，都标明是怎么算的、样本有多大。

---

## 一、先纠正一个误判：cold 不是积压

上一份交接文档（HANDOFF）写着「25 万 raw 消息未回填」，据此推算清完要 22 天。
这个结论是错的，根因是把 **归档态** 当成了 **待处理**。

账本里 `memories` 有两个独立的状态列，它们回答的是不同问题：

| 列 | 取值 | 含义 |
| --- | --- | --- |
| `quality_state` | `accepted` / `quarantine` | 入库时质量门是否放行 |
| `lifecycle` | `active` / `cold` / `superseded` | 这条记忆现在还在不在检索池里 |

关键组合（2026-10-24 快照）：

```
quality_state  lifecycle   raw_chat        行数    是不是积压
-------------  ---------   --------   ---------  ------------
accepted       cold             1       252,904   否，已归档
accepted       active           1         4,832   是，真正在用
quarantine     active           0         4,323   否，质量门已拒
accepted       active           0         1,655   是，结构化待提炼
quarantine     cold             1         1,405   否，已归档且被拒
quarantine     cold             0           820   否
accepted       cold             0           508   否
quarantine     active           1           168   否
superseded     superseded       0            23   否
conflict       active           0             2   否（走冲突裁决）
```

`accepted` + `cold` 是保留期淘汰的原始证据，留着只为可追溯，
**永远不该被提炼**——它们已经不在检索池里，提炼出来也没人用。
把 25 万行算成待办，等于把工作量放大了约 60 倍。

> 规则：判断「还剩多少活」，只看 `accepted AND active`。

---

## 二、真实 backlog（2026-10-24 实测）

```
accepted + active 合计              6,487
  raw_chat=1（原始会话）             4,832
  raw_chat=0（结构化）               1,655

distill 游标已覆盖                 2,077  (34.1%)
  其中产出了 claims                426
  其中确认无可提炼内容             1,651   <- 已正确标记，不是积压

真正剩余待提炼（行）               4,019
  raw_chat=1                        3,657
  raw_chat=0                          362
按内容去重后（模型调用次数）        1,337
  raw 去重后                          975
  结构化去重后                        362
```

注意最后一行：**行数 4,019，但不同内容只有 1,337 条**。
原始会话被跨会话重复采集，同一个 content 在队列里出现多次。
按行付模型调用，等于为同一个答案付三次钱。

去重用的是 `hippomind.textutil.content_fingerprint`——和入库去重是同一个
规范化函数（剥空白、时间戳、runner 包裹块）。`COUNT(DISTINCT content)` 会高估，
因为它分不清「只差一个采集时间戳」的两行。

> 数字会漂（daemon 一直在跑）。`scripts/distill_status.py` 每次重算，
> 并把口径写进输出第 8 节，可以直接和 `sqlite3` 对账。

---

## 三、吞吐：实测，不是推算

### 3.1 改动前的真实速率

改动前 daemon 配置是 `limit=16` / `tick_seconds=300` / `MAX_BATCH=8`，
即每 5 分钟最多 16 行、2 次模型调用。实测（从 `source_cursors.updated_at`
直接回放——这是唯一诚实的时间来源：提炼产出的 memory 是成批创建的，
daemon health 计数器每轮被覆盖，两者都无法回放）：

```
窗口 6 小时 · 样本 957 条游标 · 有效跨度 5.95 小时
实测速率 160.9 行/小时
```

为什么是 160 而不是理论值 192（16 行 × 12 轮/小时）：daemon 每轮还要跑
会话摘要、联想边、冲突收敛，且模型调用失败时会走启发式回退。
**任何「理论吞吐」都不该被当成承诺，这里只报实测。**

### 3.2 批大小：一个反直觉的结论

先做的是聚合扫描（每个 size 3 个 block，不同条目集）：

| 批大小 | 每次调用耗时 | 每条耗时 | 条目覆盖率 | claims/条 |
| --- | --- | --- | --- | --- |
| 8 | 13.6s | 1.70s | 45.8% | 0.83 |
| 16 | 18.4s | 1.15s | 47.9% | 0.71 |
| 24 | 22.8s | 0.95s | 41.7% | 0.62 |
| 32 | 31.7s | 0.99s | 34.4% | 0.47 |
| 48 | 35.1s | 0.73s | 27.8% | 0.42 |

> 注意：这些耗时来自小样本、空闲机器，**低估了真实值**（见 3.5 的端到端实测，
> 真实单次调用 25–38s）。这张表只用来看**相对趋势**，不要拿绝对值推算时间。

这张表看起来在说「16 最好」。**它是错的**，因为每个 size 用的是不同条目集，
而条目之间可提炼内容密度差异极大——「覆盖率」里混进了条目本身的差异。

所以补做了配对实验：同一批 16 条，一条臂一次 size-16 调用，
另一条臂两次 size-8 调用，逐条比较「这一条有没有拿到有效 claim」。
9 个 block、144 条：

```
size-16 墙钟 173.0s   size-8 墙钟 289.9s   加速 1.68x
覆盖条目数：size-16 64 条 / size-8 72 条
size-16 相对 size-8 的条目召回率：88.9%
size-8 找到、size-16 丢掉的条目：18 条（反过来只多拿到 10 条）
```

**结论：批 16 会静默丢掉约 11% 的条目。** 它不报错，只是把这些行标成
「无可提炼内容」然后跳过。用加速换「悄悄少干活」，正是纪律里禁止的那种提速。

### 3.3 所以默认批大小仍然是 8

`DEFAULT_BATCH_SIZE = 8`，没有改。`HIPPOMIND_DISTILL_BATCH` 仍然可以调到 16/24，
但那是给「自己测过自己的语料并接受这个取舍」的人用的，不是默认值。

### 3.4 那提速从哪来

| 手段 | 效果 | 牺牲质量吗 | 依据 |
| --- | --- | --- | --- |
| 批内内容去重 | 模型调用 4,019 → 1,337（×0.33） | 否 | 见下 |
| 每轮 limit 16 → 64 | 每轮行数 ×4 | 否 | 一轮 8 次调用而非 2 次 |

去重是最大的一项，也是唯一**完全不牺牲质量**的一项：同一段内容只问模型一次，
但**每一行仍然各自生成一条提炼记录、各自带自己的 evidence 链接、各自被标记游标**。
溯源完整性和「每行都被处理」都没有让步。

limit 提高同样不碰质量：它只决定一轮处理多少行，不改变每行分到多少模型注意力。

---

## 四、参数与开关

全部可配，默认值就是上面实测选出来的值。环境变量读不到或越界时**回落到默认值**，
不会静默产生模型处理不了的批。

| 环境变量 | 默认 | 范围 | 作用 |
| --- | --- | --- | --- |
| `HIPPOMIND_DISTILL_BATCH` | 8 | 1–48 | 单次模型调用的条数 |
| `HIPPOMIND_DISTILL_LIMIT` | 64 | 1–200 | 一轮 run() 认领多少行 |
| `HIPPOMIND_DISTILL_PROVIDER` | auto | auto/claude/heuristic/off | 提炼后端 |

代码内对应 `default_batch_size()` / `default_run_limit()`，
`run(limit=, batch_size=, dedupe=)` 也都能直接传，测试和脚本用显式参数。

### daemon 侧的推荐调用（daemon.py 由 Lead 统一改）

`start_distill_loop` 现在是：

```python
summary = self.distiller.run(limit=16)
...
self._stop_event.wait(300)
```

### 3.5 端到端实测（真实账本副本 + 真实后端）

前面 3.2 的单次调用耗时（13.6s）来自**小样本、空闲机器**的测量。
用生产账本的一致性副本跑完整 `run()` 后，真实耗时明显更高：

```
OLD  limit=16  墙钟 82.1s   处理 64 行   2 次调用   单次均值 38.4s   56 claims
NEW  limit=64  墙钟 210.1s  处理 256 行  8 次调用   单次均值 25.5s  115 claims
```

（副本由 `sqlite3 .backup` 从生产账本只读复制，全程不写生产库。
两轮都跑 `batch_size=8`、`dedupe=True`。）

两个必须诚实指出的点：

1. **单次调用真实耗时是 25–38s，不是 13.6s。** 3.2 的小样本低估了，
   因为真实条目更长、输出 token 更多，且后端有波动。凡是拿 13.6s 推算的时间
   都偏乐观，本文后续一律用这次端到端实测的 25.5s。
2. **两轮 `duplicates` 都是 0。** 说明这两个 limit 下取到的窗口里没有重复内容——
   去重的收益取决于窗口里恰好有多少重复，不是恒定 ×0.33。全库口径的
   4,019 行 → 1,319 条（去重率 67%）是**全量扫描**的结果；
   单轮 64 行的窗口里可能一条重复都没有。**去重是"有就省"，不是"一定省"。**

按实测重算 daemon 建议：

| 配置 | 每轮处理 | 墙钟 | tick | 占空比 | 吞吐（受 tick 约束） |
| --- | --- | --- | --- | --- | --- |
| limit=16 / tick=300s（现状） | 64 行 | 82s | 300s | 27% | ~768 行/小时 |
| limit=64 / tick=120s | 256 行 | 210s | 120s | **175%（超载）** | 不可用 |

**所以 `tick_seconds=120` 是错的**：210s 的活塞进 120s 的 tick 会背靠背连跑，
实际退化成"不停跑"，既没有余量也会让冲突收敛占比上升。修正建议：

```python
summary = self.distiller.run(limit=64)      # 一轮 256 行 / 8 次调用 / 实测 210s
...
self._stop_event.wait(300)                  # 保持 300s，占空比 70%
```

依据：210s 墙钟 + 300s tick → 占空比 70%，留 90s 余量给会话摘要与冲突收敛。
吞吐 ≈ 256 行 / 300s ≈ **3,072 行/小时**，相对现状 768 行/小时是 **×4**。
这正是 `limit` 单独带来的收益，不需要动 tick。

若还想更快，唯一安全的下一步是把 tick 降到 240s（占空比 87.5%），
吞吐约 3,840 行/小时；不建议低于 240s。

预期清完时间（**按实测速率推算**）：3,991 行 ÷ 3,072 行/小时 ≈ **1.3 小时**。
对比改动前实测 160.9 行/小时 → 24.9 小时。

---

## 五、怎么验证

```bash
# 1. 真相报告（只读，随时可跑）
python3 scripts/distill_status.py
python3 scripts/distill_status.py --json | python3 -m json.tool

# 2. 全量测试
python3 -m unittest discover -s tests -p 'test_*.py'

# 3. lint
PATH=/Users/zps/Code/artemis/.venv/bin:$PATH scripts/lint.sh

# 4. 提速参数自检
HIPPOMIND_DISTILL_BATCH=16 python3 -c 'from hippomind.distill import default_batch_size; print(default_batch_size())'

# 5. 手动跑一轮看真实耗时
python3 -m hippomind.cli distill --limit 64 --provider claude
```

`scripts/distill_status.py` 第 8 节直接打印它用的 SQL，可以粘到 `sqlite3` 里对账——
报告和账本对不上就是 bug。

---

## 六、诚实的边界

1. **批大小没有提速，这是刻意的。** 配对实验证明批 16 丢 11% 条目，
   所以退回了 8。如果后续想要单次调用喂更多，必须先用配对实验证明不丢条目，
   不能看聚合覆盖率——聚合覆盖率会被条目密度差异污染（见 3.2）。

2. **「每条耗时」这个指标本身有偏。** 内容密集的条目产出的 claims 多、
   输出 token 多、耗时长；批里混入稀疏条目会拉低平均。判断批大小好不好，
   看的是条目覆盖率，不是耗时。

3. **吞吐数字会漂。** daemon 在跑，backlog 一直在变。本文数字都是 2026-10-24 快照，
   `distill_status.py` 每次重算。

4. **本机 claude CLI 实测不可用**（`claude -p` 无输出挂起 180s，最终 `ECONNRESET`）。
   批大小实验走的是 CLI 配置的同一个后端（`ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY`，
   来自 `~/.claude/settings.json`），prompt 与 `ClaudeExtractor.extract` 逐字一致。
   这意味着**生产路径目前实际在走启发式回退**（daemon /stats 显示
   extractor 为 heuristic）。熔断器语义（连续 2 次失败 → 10 分钟冷却）保持不变。
   修好 CLI 后批大小结论仍成立，因为 prompt 和模型都没变。

5. **去重的代价**：重复行共享代表行的 claims。如果代表行提炼错了，重复行会一起错。
   缓解手段是 `dedupe_of` 游标——删掉那条游标，该行会重新入队。
   这是刻意的可逆设计，不是隐式丢失。

6. **`distill_status.py` 的去重扫描是全表读**（当前约 4,000 行，实测 < 1s）。
   如果 backlog 涨到十万级，`--scan-limit` 会截断并在输出里明确警告，
   不会静默给出偏低的去重数。
