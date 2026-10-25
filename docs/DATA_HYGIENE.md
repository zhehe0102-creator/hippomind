# 数据卫生（DATA HYGIENE）：identity key 稳定性与重复入库

Status: **active**。本文记录 2026-10-24 修复的一次**数据污染根因**：
ledger 的 identity key 含了会漂移的观测时间，导致采集侧每跑一轮就
把同一条消息重新入库一次。

Owned files：`hippomind/ledger/identity.py`、`tests/test_identity_stable.py`、本文件。

## 结论先说

| 指标 | 修复前（v1） | 修复后（v2） |
| --- | --- | --- |
| 同一条 Gemini 会话摘要 | **141 行** | **1 行** |
| 生产副本连跑两轮采集，新增行 | 每轮 +1 | **0** |
| 历史行（v1 id）是否被识别为重复 | — | **是**（向后兼容） |
| 显式 `valid_from` 的双时态语义 | 保留 | **保留** |

## 一、先诊断：到底哪个字段在变

### 1.1 事故形态（生产账本实测）

```sql
SELECT evidence_json, COUNT(*) n, COUNT(DISTINCT content) nc,
       COUNT(DISTINCT metadata_json) nm
FROM memories WHERE evidence_json LIKE '%gemini%'
GROUP BY evidence_json HAVING n > 1 ORDER BY n DESC LIMIT 1;
```

结果：

```
n=141  content=1  metadata=141
evidence_json = ["gemini:1922574a-8409-405c-9ee8-0a81c9df6402"]
created_at    2026-10-24T04:52:22 .. 2026-10-24T16:01:14
```

**141 行，内容完全相同，evidence 完全相同，metadata 有 141 个版本。**

### 1.2 逐字段取证

把这 141 行的每一个 identity 输入逐列统计不同值个数：

| 字段 | 不同值数 | 判定 |
| --- | --- | --- |
| `kind` | 1（`task`） | 内容固有 |
| `source_agent` | 1（`gemini`） | 内容固有 |
| `session_id` | 1（空） | — |
| `role` | 1（空） | — |
| `scope` | 1（`shared`） | 内容固有 |
| `task_id` | 1（会话 id） | 内容固有 |
| `project` | 1 | 内容固有 |
| `content` | 1 | 内容固有 |
| `valid_until` | 1（空） | — |
| **`valid_from`** | **141** | **← 唯一变量** |
| `memory_id` | 141 | 由上面推出 |

metadata 内部同样在变，但**它不是 identity 的输入**：

| metadata 键 | 不同值数 |
| --- | --- |
| `source_modified_time` | **141** |
| `step_count` | **141** |
| `status` | 2 |
| 其余 6 个键 | 1 |

### 1.3 用公式复算，确认是 `valid_from`（不是 metadata）

拿这 141 行各自的字段重建 v1 identity payload 并重算 sha256：

```
141 行重建校验：v1(含 valid_from) 命中 141/141
v2(剔除 valid_from) 产生的唯一 hash 数：1
```

**141/141 命中**说明公式被完全解释；剔除 `valid_from` 后塌缩成 **1** 个 hash。

再用实验直接排除 metadata：

```python
# 只变 metadata，不变 timestamp
a = led.ingest({**base, "metadata": {"step_count": 1,   "build": "x"}})  # inserted=True
b = led.ingest({**base, "metadata": {"step_count": 999, "build": "y"}})  # inserted=False
# 只变 timestamp
c = led.ingest({**base, "timestamp": "2026-10-24T04:52:22Z"})           # inserted=True
d = led.ingest({**base, "timestamp": "2026-10-24T04:53:18Z"})           # inserted=True  ← 重复入库
```

**结论（与最初假设不同，必须写清楚）**：metadata 从来没有进过 identity hash；
真正漂移的是 `valid_from`。这个区别很重要——如果按"把 metadata 从 hash 里拿掉"
去改，改动会**完全无效**。

### 1.4 为什么 `valid_from` 会漂移

`gemini_capture.py` 的记录里：

```python
source_modified = str(item.get("last_modified_time") or "")[:64]   # 源文件 mtime
timestamp = _iso_timestamp(source_modified)
# ...
"timestamp": timestamp,        # ← 采集侧只传 timestamp，从不传 valid_from
```

而 v1 的 identity 判定是：

```python
"valid_from": (valid_from if (explicit_valid_from or explicit_observed_at) else "")
```

`explicit_observed_at` 在**只要带了 timestamp** 时就为真。于是：

```
源文件 mtime 变 → timestamp 变 → valid_from 变 → identity hash 变 → 新记忆
```

Gemini 的 `last_modified_time` 在会话活跃期间几乎每次读取都变，
所以每一轮采集都产出一个新 hash。141 轮 ≈ 141 行。

## 二、修复

### 2.1 identity v2 规则

identity 只由**内容固有**字段决定。结构化记录：

```
class=structured | kind | source_agent | session_id | external_id
| task_id | project | scope | valid_from* | valid_until | content(规范化)
| identity_version=2
```

`valid_from*` 的星号是本次修复的核心：**只有显式提供的 `valid_from` 才参与**。

### 2.2 那条分界线：有没有"稳定来源锚点"

只把 `explicit_observed_at` 从 hash 里删掉会破坏一个既有契约：
`tests/test_ingest_idempotency.py::test_new_observation_time_creates_new_memory`
要求"同一句话在不同时刻断言 = 两个事件"。

两者都对，区别在于**记录有没有声明一个稳定的来源对象**：

| 记录形态 | 语义 | 派生时间是否进 identity |
| --- | --- | --- |
| 带 `task_id` / `session_id` / `external_id` | "关于该对象的一条陈述" | **否**（重读同一对象仍是同一条） |
| 不带任何锚点 | "某时刻的一次独立断言" | **是**（时间就是唯一区分） |
| 带显式 `valid_from` | 调用方明确声明的生效时间 | **是**（真实双时态） |

采集侧全部落在第一类：`gemini_capture` 用 `task_id=conversation_id`，
`pi_capture` / `transcripts` / `cursor_capture` 用 `session_id`。
所以修复精确命中污染源，而手工 `ingest` 的 append-only 语义不变。

### 2.3 向后兼容：旧 identity 仍被识别

升级不能让历史行被重新入库。`_find_existing_event()` 做三层探测：

1. **当前 identity（v2）**——`content_hash` 是 UNIQUE 索引，一次索引查找。
2. **legacy identity（v1）重算**——覆盖"同一 timestamp 的幂等重放"。
3. **有界语义探测**——关键的一层。v1 行 hash 的是**派生时间**，重观测时
   timestamp 变了，重算的 v1 hash 匹配不到任何历史行；只有按
   `(task_id|session_id, source_agent, scope)` 走索引取出候选、比较
   **规范化内容**、再**重算该候选自己的 v1 hash 确认它确实是 v1 行**，
   才能找到它。

第 3 层的两个安全阀：

* **必须有索引可用的收窄条件**（`task_id` 或 `session_id`），否则直接返回
  None——绝不允许退化成全表扫描。实测两条路径都 < 1ms。
* **必须重算 v1 hash 与库里存的 `content_hash` 相等**才合并。v2 行把
  `identity_version` 放进 payload，永远重建不出自己的 v1 hash，因此
  **两条真实不同的 v2 观测不可能被误合并**（有测试锁定）。

### 2.4 版本标记

```python
IDENTITY_VERSION = 2          # hippomind/ledger/identity.py
IDENTITY_VERSION_KEY = "identity_version"
IDENTITY_VERSION_LEGACY = 1   # 无标记的历史行
```

版本进 payload，所以 v2 hash 与 v1 hash 必然不同——这既是版本隔离，
也是上面第 3 层探测能安全判定的依据。

## 三、验证

### 3.1 生产副本连跑两轮采集

只读打开生产库 → SQLite backup API 做副本 → 在副本上采集（不碰生产）。
每轮都改变 `timestamp` 与 `step_count`，模拟真实采集：

```
副本: .../copy.sqlite
初始行数: 267852
第 1 轮采集: inserted=[False]  新增行=0
第 2 轮采集: inserted=[False]  新增行=0
最终行数: 267852  相对初始新增: 0
```

**第二轮新增重复行 = 0**（验收口径）。

### 3.2 反事实对照：同一副本用 v1 公式

```
第 1 轮 v1 hash=5af15fd0fe89e08c77bb  已存在=False  -> 新增 1 行
第 2 轮 v1 hash=e766692650e89148ef37  已存在=False  -> 新增 1 行
```

同样是两轮采集，v1 各插一行、v2 插 0 行。这就是 141 的来源与它的终止。

### 3.3 141 次采集塌缩成 1 行

`tests/test_identity_stable.py::test_141_observations_collapse_to_one_row`
在干净账本上复刻事故：141 次采集、每次 timestamp 与 step_count 都不同 →
断言最终只有 **1** 行。

## 四、测试

`tests/test_identity_stable.py`（30 条）：

| 组 | 覆盖 |
| --- | --- |
| `VolatileTimestampTests` | timestamp 漂移不产生新行；metadata 漂移不产生新行；显式 valid_from 保留双时态；raw chat 稳定 |
| `IdentityDiscriminatorTests` | 不同 content / task / session / role / scope 必须不同 |
| `AnchorDisambiguationTests` | 无锚点 → append-only 不变；有 task/session 锚点 → 时间不进 identity；显式 valid_from 优先 |
| `BackwardCompatibilityTests` | v1 行重建命中；重采集识别为重复；多轮幂等；不误合并不同内容；不误合并两条 v2 行 |
| `IdentityVersionTests` | 版本常量钉住；版本进 payload |
| `ProductionShapeTests` | 141→1；第二轮 0 新增；批量路径一致 |

## 五、复跑

```bash
# 针对性测试
python3 -m unittest tests.test_identity_stable tests.test_ingest_idempotency -v

# 全量
python3 -m unittest discover -s tests -p "test_*.py"
PATH="/Users/zps/Code/artemis/.venv/bin:$PATH" scripts/lint.sh
```

## 六、遗留与注意

* **存量重复行没有删除**。本次只止住增量；生产库里那 141 行（以及其他
  重复组）仍在。物理删除需要单独决策与备份，本次刻意不做。
* **`_legacy_semantic_probe` 只在有 `task_id` 或 `session_id` 时生效**。
  既无锚点又是 v1 写入的极老行，仍只能靠第 2 层（精确 timestamp 重放）
  命中；这是有意的取舍——宁可漏合并也不做全表扫描。
* **`archive:*` 来源**（`n=293` 的重复组）不走本路径：它们带稳定
  `event_id`，由第 1 层精确命中，且 `distinct_content` 接近 `n`（约 250/293），
  属于真实不同内容而非同一条重复。
* **`identity_version` 目前只进 hash，未落库为列**。需要审计时可从
  `_legacy_identity_hash(row) == content_hash` 判定该行是否为 v1。