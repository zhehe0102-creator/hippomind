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
   当时这意味着**生产路径实际在走启发式回退**（daemon /stats 显示
   extractor 为 heuristic）。熔断器语义（连续 2 次失败 → 10 分钟冷却）保持不变。
   修好 CLI 后批大小结论仍成立，因为 prompt 和模型都没变。

   > **2026-10-24 更新**：该问题已由第七节解决——提炼改为直连 LLM API，
   > 不再经过 CLI。CLI 本身仍然坏着，它现在只是第二顺位回退。
   > 上面 3.x 的批大小结论依然成立，理由不变：prompt 与模型都没变。

5. **去重的代价**：重复行共享代表行的 claims。如果代表行提炼错了，重复行会一起错。
   缓解手段是 `dedupe_of` 游标——删掉那条游标，该行会重新入队。
   这是刻意的可逆设计，不是隐式丢失。

6. **`distill_status.py` 的去重扫描是全表读**（当前约 4,000 行，实测 < 1s）。
   如果 backlog 涨到十万级，`--scan-limit` 会截断并在输出里明确警告，
   不会静默给出偏低的去重数。

---

## 七、直连 LLM API：从 CLI 挂起到真实模型

### 7.1 问题：生产一直在走启发式

本机 `claude` CLI **完全不可用**：它的默认模型 `zcode/GLM-5.3-Flash[1m]` 不在该版本
的模型目录里，`claude -p` 会挂起或直接失败。后果不是"慢"，而是：

```
ClaudeExtractor.extract() 失败 -> 熔断器打开（连续 2 次 / 冷却 600s）-> _claims_for 回退
```

即 **每一轮提炼都在用启发式**。生产账本的 `metadata.extractor` 分布证实了这一点
（只读副本统计，2026-10-24）：

| extractor | 记忆数 |
| --- | --- |
| claude | 651 |
| **heuristic** | **247** |

而 CLI 背后的**端点本身是好的**。同一套凭据直接 `POST {base}/v1/messages`，
3–6 秒返回正常内容。所以缺的只是一层传输。

### 7.2 做法：只换传输层，不动 prompt

新增 `hippomind/llm_client.py`（纯标准库 `urllib`，零新依赖）：

* 配置优先级：环境变量（`HIPPOMIND_LLM_BASE_URL` / `HIPPOMIND_LLM_API_KEY` /
  `HIPPOMIND_LLM_MODEL`）→ `~/.claude/settings.json` 的 `env` 段 → 不可用。
* `complete()` 任何失败都返回 `None`，**绝不抛异常**——调用方永远能无痛回退。
* `describe()` 只报 base / model / **是否有 key**，不报 key 内容；`LLMConfig.__repr__`
  也把 key 打码；错误信息在进 `last_error` 前先做脱敏。
* 熔断器语义与原来一致（连续 2 次失败 → 600s 冷却）。

`ClaudeExtractor` 的顺序变为 **API → CLI → 启发式**，并且 `name` 会如实报告是
`api` / `cli` 还是 `heuristic`。**prompt、8 条/批、claims 结构、source 编号语义
一律没动**——这正是下面这组对比能成立的前提。

### 7.3 实测对比（只读副本，同样 26 条，同样 batch=8）

副本由 `sqlite3 .backup` 从生产账本复制，全程不写生产库。

| 指标 | heuristic | api |
| --- | --- | --- |
| 条目数 | 26 | 26 |
| 产出 claims | 2 | **28** |
| 条目覆盖率 | 3.8% | **42.3%** |
| claims/条目 | 0.08 | **1.08** |
| 墙钟 | 0.5s | 72.3s |
| 实际使用的 extractor | heuristic ×26 | **api ×26** |

逐条对比：**两边都覆盖 1 条，只有 API 覆盖 10 条，只有启发式覆盖 0 条。**

覆盖率的分母是 26 条待提炼行，其中大量是 15–68 字符的短句（"Bing 可用。读取搜索结果："），
本身几乎没有可提炼内容——所以 42.3% 不是"模型只做对了四成"，而是**这些行确实大半没有稳定事实**。
关键是启发式在同样的行上只认出 2 条。

### 7.4 质量抽查：API 的 claims 是不是真的有用

不能只看条数。人工看了全部 34 条 claims（另一轮跑出的），**32/34 是有意义的**，例如：

```
netproxy | 故障根因 | root_cause   | Worker 带 staging 模块路径启动，目录移动后驻留进程仍读取不存在的路径
netproxy | 修复决定 | fix_decision | Start() 比对驻留进程的 --module-config，不一致时先 Stop
用户     | 使用偏好 | usage_preference | 不打游戏，只追求日常使用最优
手机优化任务 | 执行原则 | execution_principles | 只读探测优先、每步可回滚、改动全部记录
```

**必须诚实指出 2 条退化值**（6%）：`待处理新增需求数量 = "3"`、`Bing 可用 = "可用"`。
这类值信息量很低，启发式反而不会产出（启发式只认决策句）。这是 API 路径的真实代价，
不是可以粉饰的收益。

### 7.5 三个必须记下来的坑

**坑 1：`max_tokens` 太小会让整批静默失效。**
第一版把输出预算设成 2048。实测一个 8 条的密集批次需要约 2,600 token，
响应被**截断在 JSON 中间**，`json.loads` 抛错 → 整批 8 条全部回退启发式，
而调用方只看到"这批没提炼出东西"。CLI 传输没有这个上限，所以低预算会让 API 路径
比 CLI **更差**。现改为 8192，并有回归测试钉住下限。

**坑 2：端点会偶发 `RemoteDisconnected`，而且只在大请求上出现。**
同一个 prompt 可能这次 10s 正常返回，下次直接断连（实测约 1/6 概率）。
小请求（"回复 ok"）10/10 成功，所以容易被误判为"网络没问题"。
没有重试的话，**一个断连就静默废掉一整批**——这正是本节开头那个故障模式的重演。
现在对**连接级**错误重试 2 次（指数退避 1s/2s），但 HTTP 4xx/5xx 和坏 JSON **不重试**
（那是服务器的真实回答，重试只是浪费预算）。加上重试后，同一批从 3.8% 恢复到 42.3%。

**坑 3：一轮 run() 可能混用多种 extractor，只报最后一个会骗人。**
某批的模型回答是**合法的空 claims 数组**时，那一批回退启发式是正确行为。
但 `summary["extractor"]` 只记录最后一批，于是一次"2 批 API + 1 批启发式"的运行
会被显示成 `heuristic`。现在额外输出 `summary["extractors"]` 计数
（如 `{"api": 2, "heuristic": 1}`），`extractor` 保留兼容。

### 7.6 参数与开关

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `HIPPOMIND_LLM_BASE_URL` | 取自 settings.json | 端点 |
| `HIPPOMIND_LLM_API_KEY` | 取自 settings.json | 凭据（**不要写进任何提交的文件**） |
| `HIPPOMIND_LLM_MODEL` | `deepseek-v4.1-flash` | 模型 |
| `HIPPOMIND_DISTILL_TRANSPORT` | `auto` | `auto` / `api` / `cli`；无法识别一律按 `auto` |

```bash
# 端点是否可用（不打印密钥）
python3 -c "from hippomind.llm_client import describe; print(describe())"

# 真实跑一轮，看 extractor 是不是 api
python3 -m hippomind.cli distill --limit 16 --provider claude

# 冲突裁决也走同一个传输层
python3 scripts/conflict_review.py --decider llm --limit 5
```

### 7.7 诚实的边界

1. **API 比启发式慢两个数量级**（72s vs 0.5s，26 条）。这是必然的：
   启发式是纯正则，API 是真的推理。daemon 的 300s tick 装得下，但不要把
   `HIPPOMIND_DISTILL_LIMIT` 调大到一轮跑不完。
2. **42.3% 的覆盖率不等于"剩下 58% 提炼失败"**。见 7.3，很多行本来就没有稳定事实。
   真正的判据是逐条对比：API 覆盖 11 条，启发式 1 条。
3. **6% 的退化值是真的。** 短句上的 API 会给出 `"3"`、`"可用"` 这类低信息量值。
   如果下游对槽位质量敏感，应该在 claim 落库前加一道最小长度/信息量过滤——
   这不在本任务范围内，别假装已经解决。
4. **端点偶发断连没有被根治**，只是用重试掩盖了。如果断连率上升，
   需要看服务端，而不是继续加重试次数。
5. **`claude` CLI 仍然不可用**，它现在只是第二顺位回退。在它修好之前，
   `--provider claude` 的实际含义是"用 API"。

---

## 八、claim 质量门：先量化，再决定拦什么

### 8.1 起因：round 9 的"6% 退化值"

round 9 抽查 34 条 claims，判定 2 条是退化值（`"3"`、`"可用"`），推断短句上模型会说废话。
本节的第一个结论是：**这个判定是错的**。

把这两条按 claim 读一遍：

```
subject=当前任务   slot=pending_new_requirements   value="3"
  -> "当前任务有 3 条待处理的新需求"   —— 这是一个计数
subject=Bing      slot=availability              value="可用"
  -> "Bing 可用"                     —— 这是一个状态
```

两条都**信息完整**，只是**简短**。它们恰恰是任务书警告过的那种"短但有意义"的值。
如果照 round 9 的判断去建门，建出来的就是会误杀 `6000` / `SQLite` / `mac-mini` 的那道门。

### 8.2 先量化：三个样本，共 5,147 条真实 claim

没有凭感觉定阈值。样本全部来自**生产账本只读副本**（`sqlite3 .backup`）：

| 样本 | 来源 | 条数 |
| --- | --- | --- |
| PRODUCTION | 账本里全部 `state=active` 的 claim | 4,464 |
| FRESH-API | 用当前 API 提取器跑 400 条真实 `raw_chat`（40–1500 字） | 374 |
| SHORT-SRC | 同上，但只取 **10–120 字的短行**（round 9 说的问题区） | 309 |

**长度分布（PRODUCTION）**：

| 长度 | 条数 | 占比 |
| --- | --- | --- |
| 1–2 | 71 | 1.6% |
| 3–4 | 108 | 2.4% |
| 5–8 | 167 | 3.7% |
| 9–20 | 971 | 21.8% |
| 21+ | 3,147 | 70.5% |

**字符类别（PRODUCTION）**：`has_cjk` 82.2%，`mixed` 8.6%，`ascii_token` 8.0%，
`pure_number` **0.9%**（42 条），`number_unit` 0.2%，`single_char` **0.1%**（5 条）。

**关键交叉表**（槽位语义 × 值类别，PRODUCTION）：

| 槽位类型 | 裸布尔 | 裸状态词 | 纯数字 | 其它 | n |
| --- | --- | --- | --- | --- | --- |
| generic（statement/note…） | 0 | 0 | 0 | 377 | 377 |
| boolish（`*_match`/`is_*`） | 26 | 0 | 0 | 55 | 81 |
| statusish（`*_status`） | 0 | 14 | 0 | 350 | 364 |
| numeric（`*_count`/`*_budget`） | 0 | 0 | 42 | 321 | 363 |
| ident（`*_name`/`host`） | 5 | 0 | 0 | 367 | 372 |
| neutral | 0 | 10 | 0 | 2,897 | 2,907 |

这张表是整个设计的依据：**裸值只出现在"槽位名已经说明了答案类型"的地方**
（`requires_root=true`、`*_count=42`、`*_status=正常`），而 377 条 generic 槽位里
**一条裸值都没有** —— 它们在 generic 槽位里才是异常的。

### 8.3 判据：看槽位语义，不看长度

`classify_claim_value(slot_key, predicate, value)`，返回稳定 reason code（与
`classify_unusable` 同一套可观测契约），`""` 表示放行：

| reason | 条件 | 作用于 |
| --- | --- | --- |
| `empty_value` | 只有空白 | 所有槽位 |
| `punct_only` | 只有标点/下划线（`-`、`--`、`...`） | 所有槽位 |
| `tautology` | 值只是重复了 **slot 名** | 所有槽位 |
| `generic_single_char` | 单字符 | **仅 generic 槽位** |
| `generic_bare_state` | 裸布尔/裸状态词 | **仅 generic 槽位** |
| `generic_bare_number` | 纯数字 | **仅 generic 槽位** |

**为什么不用长度**：长度分不开 `"6000"`（该记住的 token 预算）和 `"3"`（说不清是什么的计数）。
两者都是短数字。分开它们的唯一信息是**槽位名有没有告诉你这个数字是什么**。
实测代价：374 条 FRESH-API 里有 **82 条（21.9%）长度 ≤ 8**，几乎全部合法
（`SQLite`、`pa3q`、`v2.2`、`6h`、`5s`、`ebpf`、`/dev/tmp`）。任何长度阈值都会先杀掉这些。

### 8.4 实测结果：这道门在真实数据上拦下了 0 条

| 样本 | 条数 | 放行 | 拦下 | 比例 |
| --- | --- | --- | --- | --- |
| PRODUCTION | 4,464 | 4,464 | **0** | 0.00% |
| FRESH-API | 374 | 374 | **0** | 0.00% |
| SHORT-SRC | 309 | 309 | **0** | 0.00% |

**这是必须如实报告的结果：门建好了，但在现有数据上它一条也没拦到。**
原因不是门写错了，是 round 9 那个"6% 退化值"的前提不成立 ——
那些值经复查都是合法 claim。

进一步验证门确实会触发（用构造数据，见 `tests/test_claim_quality.py`）：

```
classify_claim_value("statement", "p", "3")        -> "generic_single_char"   (拦)
classify_claim_value("note",      "p", "可用")      -> "generic_bare_state"     (拦)
classify_claim_value("statement", "p", "-")        -> "punct_only"            (拦)
classify_claim_value("default_token_budget", "p", "6000") -> ""                (放行)
classify_claim_value("task_status", "p", "正常")     -> ""                       (放行)
```

门在 14.63% 的生产 claim 上真的进入了 generic 分支（653 条），只是那些值都有内容。

### 8.5 哪些短值被保留、为什么

这是本任务的核心交付。以下全部来自生产账本实测，**一条都没被杀**：

| 值 | 槽位 | 为什么保留 |
| --- | --- | --- |
| `6000` | `default_token_budget` | 纯数字，但槽位说明了它是 token 预算 |
| `259` | `third_party_app_count` | 计数，槽位说明了计什么 |
| `1` | `wake_lock_count` | 同上；`1` 在这里是事实不是噪音 |
| `120` | `default_timeout_seconds` | 超时预算 |
| `86400` | `update_interval` | 间隔秒数 |
| `37` / `17` | `compile_sdk_version` / `platform_build_version_name` | 版本号 |
| `pa3q` | `device_codename` | 设备代号，短但唯一标识一台机器 |
| `SQLite` / `ebpf` / `walt` | `tech.database` / `active_backend` / `cpu_governor` | 技术选型 |
| `mac-mini` | `host` | 主机名 |
| `v1.5.2` / `v2.2` / `v0.8.3` | `version` / `installed_version` | 版本 |
| `/dev/tmp` / `/bin/zsh` | `worker_tmpdir_value` / `shell` | 路径 |
| `true` / `false` | `bash_requires_approval`、`*_match`、`is_git_repo` | 槽位本身就是一个谓词，布尔就是答案 |
| `正常` / `成功` / `通过` / `无` | `*_status`、`recent_errors` | 状态槽位，状态词就是答案 |
| `6h` / `5s` / `15m` | `worker_permanent_retry_max` / `runtime_verify_timeout` | 时长 |

SHORT-SRC 样本里有 **27 条（8.7%）是裸状态词/布尔**，全部落在 typed 槽位，全部保留。
如果按"裸状态词就杀"的规则，这 27 条会全部丢失 —— 这就是为什么判据必须挂在槽位上。

### 8.6 一个被数据否掉的规则：`value == predicate`

我最初把"值等于 predicate"也算 tautology。实测否掉了它：

| 样本 | `value==predicate` | 实际情况 |
| --- | --- | --- |
| FRESH-API | 1 | `subject=module_scripts_test predicate=passed value=passed` |
| SHORT-SRC | 2 | `subject=审计脚本 predicate=未运行 value=未运行`；`subject=NeoZygisk predicate=未尝试注入 value=未尝试注入` |

3 条里 **3 条都是真实事实**，只是 predicate 写得多余了一点。
"审计脚本未运行"是有效信息，删掉它是拿信息换整洁。
所以规则收窄到**只判 slot 名重复**（`value == slot_tail`），这一条在 5,147 条真实数据上命中 0 次、
误杀 0 次。

### 8.7 可观测性

`run()` 的 summary 新增两个字段，与 `rejected`（整行级）并列但语义不同
（`claims_dropped` 是"模型答了但没说有用的话"）：

```python
{"claims_dropped": {"generic_bare_state": 2}, "claims_dropped_total": 2}
```

某行的 claim 被全部过滤时，该行按"空提炼"处理：打游标、计入 `skipped`，
**不会永远重试**（有测试钉住）。

### 8.8 诚实的边界

1. **这道门目前拦下 0 条真实 claim。** 不要把它当成质量提升来汇报。
   它的价值是"下次真的出现裸值时会拦"，而不是"已经清理了 6%"。
2. **round 9 的 6% 是误判**，已在 8.1 说明。后续如果有人再引用那个数字，请引用本节。
3. **判据依赖槽位名**。如果模型产出一个不在 `GENERIC_SLOT_NAMES` 里、但语义泛化的
   新槽位名（比如 `misc_note`），门会放过它。这是有意的取舍：漏放 > 误杀。
4. **`generic` 名单是硬编码的**。加新槽位名要改代码，不是配置项 —— 因为它需要人判断，
   不能靠猜。
5. 样本 5,147 条覆盖了生产全量 active claim + 683 条新提取结果，但没有覆盖
   未来 API 输出分布的变化。门的行为靠 23 个单测钉住，不靠这批样本钉住。
