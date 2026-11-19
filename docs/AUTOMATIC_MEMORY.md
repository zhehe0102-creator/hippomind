# HippoMind 自动记忆环路（v2）

Status: **active** — 本文件是"装完就忘"的自动闭环的运维说明：
采集 → 提炼 → 相关性门控注入 → 冲突裁决 → 健康自检，全程不需要用户写
`记忆：` 标记。实现代码见 `hippomind/transcripts.py`、`gemini_capture.py`、
`pi_capture.py`、`distill.py`、`context_pack.py`、`injection_trace.py`、
`codex_hook.py` 与 `integrations/dsh-plugin/`；本文件只做文档，不改代码。

## 0. 一句话

装好并登录自启后，各 Agent 的会话会被**自动采集**进账本，daemon 每 5 分钟把
新证据**自动提炼**成带证据的 claims，下一轮普通对话开始前按**相关性门控**
注入最小 Context Pack，冲突按"最新优先"自动裁决，留痕可审计、可翻案。

## 1. 自动采集（daemon 每 60 秒一轮）

`hippomindd` 启动时调用 `start_capture_loop(tick_seconds=60)`，每轮依次执行
三个采集器；任何一个失败只写进 health，不会拖垮 daemon，也不会阻塞其它来源。

| Agent | 本机来源 | 实现 | 增量状态 |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` | `transcripts.py` | 游标 = 字节偏移 + **绝对行号** + session/cwd |
| Codex CLI | `~/.codex/sessions/**/*.jsonl` | `transcripts.py` | 同上；`session_meta` 可出现在任意批次，跨批次不会丢 session/project |
| Gemini / Antigravity | `conversation_summaries.db`（2 个库） | `gemini_capture.py` | 复合 keyset `(last_modified_time, conversation_id)`，同时间戳不会漏采 |
| Pi / PiDeck | `~/.pi/agent/sessions/**/*.jsonl` | `pi_capture.py` | `transcript:pi:<sha1>` 偏移 + 行号 |
| DeepSeek Harness | Cordis 插件 | `integrations/dsh-plugin/` | 会话结束按 `seq_span` 写回 |
| Cursor | 条件适配器：3 套本地存储栈（`cursorDiskKV` 等） | `cursor_capture.py` | 按会话 updatedAt/末条 id 增量；本机未安装时显式 `state=not_installed`，不谎报 0 条 |

每轮预算：Claude/Codex `max_messages=100`；Gemini `max_rows=50`；Pi
`max_messages=100`（每文件每轮 ≤2MB / 40 行，单条消息截断 4000 字符）。
采集只写 `raw_chat=1` 的原始证据（Pi 的 compaction 摘要例外，写成
`kind=task`），raw 默认不进 Context Pack。

安全与健壮性：

- 只消费以换行结尾的完整行；写者半行时游标不前进，下一轮重读（中断安全）。
- 单行 JSON 解析失败只跳过该行；文件被截断/替换（size < offset）时从头重扫。
- Pi 的 `deleted` 墓碑、`thinking`/`toolCall`/`image` block、harness 注入消息
  一律跳过；DSH 会话里由本库自己注入的记忆包永不回灌（防检索反馈循环）。
- 符号链接与非普通文件忽略；单文件损坏只影响该文件。

健康字段：`/stats` 与 `GET /api/v1/health` 的 `capture`（files / messages /
inserted / duplicate / errors / bytes / elapsed_ms，以及 `gemini`、`pi` 子统计）。

## 2. 自动提炼（daemon 每 5 分钟一轮）

`start_distill_loop(tick_seconds=300)` 每轮调用 `DistillEngine.run(limit=64)`
（`HIPPOMIND_DISTILL_LIMIT` 可覆盖；批次大小仍保持 8，放大批次会静默丢条目），
并在同一轮里跑 `SessionDigester.run(limit=2)` 做会话级摘要：

- **双队列**：一半候选来自结构化投影（task/progress/decision/semantic/fact/
  preference/procedural），一半来自最新 raw chat。纯 `created_at` 排序会让 25 万
  raw 饿死结构化记忆，纯结构化优先又会让新会话延迟数天。
- **提取器**：默认用本机 `claude -p`（print 模式，45 秒超时）抽
  subject/predicate/slot_key/value/confidence/source；不可用或解析失败时回退到
  确定性抽取（context 字段 + 决策句式）。
- **熔断器**：连续 2 次失败进入 10 分钟冷却，期间直接走确定性抽取；任何一次成功
  解析立即复位。API 抖动不会把提炼循环卡住几分钟。
- **产物**：`kind=semantic`、`source_agent=hippomind-distill`、`evidence=["memory:<源>"]`、
  `metadata.source_memory_ids`、随行 claims；同一 subject 的连续提炼自动建
  `related_to` 因果边。`distill:<memory_id>` 游标保证幂等，重复轮次不会重复写。
- **隔离**：提炼结果继承来源 raw 记忆的 scope（私有 `agent:*` 不会因为提炼就变成 `shared`），
  并在 metadata 写入 `derived_from_raw_chat=true` / `untrusted_source=true` / `source_scope`；
  要跨 scope 共享必须走显式提升，而不是靠默认值泄漏。
- **摘要刷新**：会话摘要游标记录消息数与最后事件时间，会话新增消息会重新排队，
  新摘要 `supersede` 旧摘要（旧摘要保留谱系），不再出现“摘要永久停在第一版”。
- **收尾**：每轮结束跑 `resolve_conflicts()` + `repair_claim_states()`，让 claim
  状态与记忆的 quality/lifecycle 收敛。

健康字段：`distill`（candidates / batches / claims / distilled / skipped / errors /
extractor / conflicts_resolved / elapsed_ms）。

## 3. 相关性门控与注入 trace

### 3.1 无标记注入路径

| 接入 | 机制 | 默认 |
| --- | --- | --- |
| Codex | 受信任 UserPromptSubmit 原生 Hook，命令带 `--trigger-mode always` | 每轮普通文本都预取 |
| DSH | Cordis 插件 `agent/pre-step`，`triggerMode: always` | 每轮普通文本都预取；子代理不注入 |
| Pi / Claude / Cursor / Gemini / local | 规则块调用 `auto-context --stdin --trigger-mode always` | 每轮预取 |

`记忆：<问题>` 仍保留为**强制指定查询**的入口（`--trigger-mode anywhere` 语义）；
代码块、引用行、行内代码和空标记不会触发。


### 3.1b 短追问消歧（会话锚定）

用户高频输入“继续 / 同上 / 好的，接着弄”时，单句检索没有可用的语义。
`hippomind/query_context.py` 会把这类查询锚定到**同一会话**最近的真实用户消息
（最多 2 条、默认 180 分钟窗口），再交给 Context Pack 检索；锚点查询只读
`memories` 的 raw_chat=user 行，实测 p95 < 1ms，非 follow-up 请求不增加任何查询。

安全边界（默认开启，逐条有测试锁定）：

- 短但有自己的内容词的请求（跑测试 / 部署 / 回滚）**不锚定**；
- 原查询含 ≥2 个内容词时，候选必须至少命中 1 个原词，否则整池丢弃并回退原查询；
- session_id 可用时严格按会话隔离；宿主不传时才退化为同 agent + 时间窗；
- `as_of` 历史切片从不锚定（会话窗口属于“现在”）；
- 普通查询在开关开/关下 pack JSON 逐字节相等（含 debug 字段）。

关闭方式：进程级 `HIPPOMIND_QUERY_CONTEXT=0`，或请求级 `options.query_context=false`。
已验证效果（28 条 follow-up 评测夹具）：follow-up 类命中率 5/18 → 18/18，
safety/normal 类 7 条锚定 0 次、pack 变化 0 条。
### 3.2 门控规则

自动路径的 `ContextPackOptions` 固定 `max_items=8`、`max_tokens=6000`、
`max_chars=16000`、`raw_chat=false`、`require_evidence=true`，并额外做两道相关性过滤：

1. **分数门**：`min_score` 默认 `0.20`（`HIPPOMIND_MIN_SCORE`；`POST /context-pack`
   同默认，显式调用方可传 0 关闭）。
2. **词法证据门**：候选必须在 `score_parts` 里真有 FTS 命中（`fts > 0`）或
   `coverage >= 0.6`。否则"最近发生过"的无关记忆会凭时间/触碰分混进来。

实测（2026-10-24 生产库）："今天天气怎么样""1+1等于几""写一首诗" **不注入**；
"HippoMind 数据库选型"注入 1 项 / 629 字符；"继续优化记忆系统的 GUI" 正常注入。

### 3.3 注入 trace

`hippomind/injection_trace.py` 每次自动注入追加一行 JSONL 到
`~/Library/Application Support/HippoMind/injection.jsonl`：

```json
{"at":"2026-10-24T00:16:24-0700","agent":"codex","source":"codex-hook",
 "query_hash":"9e1364431c24c7d5","query_chars":13,"items":1,"chars":629,"injected":true,
 "memory_ids":["mem_..."],"latency_ms":42.1,"scope":"shared","task_id":"","empty_reason":""}
```

只记 agent、来源、query 的 16 位哈希、条目数、字符数、是否注入、被注入的 memory_id、
空结果原因与服务端延迟，**不存原文/正文**。HTTP `/context-pack` 路径失败或空结果时
也会写 `injected=false`，这样“有注入 vs 未注入”才有对照组；`/stats` 的
`injection_trace` 窗口只读文件尾部 256 KiB（`/api/v1/health` 的 injection 检查同理），
不随 trace 增长而变慢。文件权限 0600，超过 8 MiB 轮转为 `.1`；
`HIPPOMIND_INJECTION_TRACE=0` 可关闭。

Trace 还支持按时间保留：`HIPPOMIND_TRACE_RETENTION_DAYS`（默认 30 天，0 关闭），
每小时最多清理一次，剔除超过保留期的行（坏行/缺 at 行原样保留），
唯一临时文件 + 原子替换并保持 0600；`.1` 轮转历史不参与清理。

只记 agent、来源、query 的 16 位哈希、条目数、字符数和是否注入，**不存原文**。
`HIPPOMIND_INJECTION_TRACE=0` 可关闭。`summary()` 被 `/api/v1/health` 的
`injection` 检查与 GUI 体检页"最近注入"使用。

## 4. 冲突策略（newest-wins + 强分歧保护）

1. **写入时**：新 claim 按 `(subject, predicate, slot_key)` 找活跃 claim；值相同直接跳过；
   双方分数 = `confidence × source_trust`，领先 >0.05 时直接裁决——败者
   `state='superseded'`、memory `lifecycle='superseded'`、`superseded_by` 指向胜者，
   并移出 FTS。
2. **提炼侧 `resolve_conflicts()`**：分数差 <0.10 且领先方 `confidence < 0.9` 时
   才进入"模糊冲突"分支：
   - 若组内 **≥0.9 的强断言存在多个不同值** → 视为真实分歧，**留人工**（不自动裁决）。
   - 否则按最新 `asserted_at` 胜出（事实在演进，新的观察覆盖旧的）。
3. **收敛**：`repair_claim_states()` 保证"active claim 必须指向 accepted+active 记忆"；
   每次裁决后都跑，避免悬空 claim。
4. **可审计、可翻案**：败者与谱系都保留；`hippomind conflicts` 看谱系，
   `hippomind resolve --claim <id> --winner <memory_id>` 人工改判。
5. **现状（2026-10-24 生产库）**：active claim 902 条、conflict 73 条、悬空 0 条。

## 5. 如何验证

按顺序执行，任何一步失败再往下查：

```bash
# 1) daemon 是否在跑
python3 -m hippomind status

# 2) 就绪自检（有 FAIL 时 exit 1，可作脚本门）
python3 -m hippomind doctor

# 3) 接入核验：规则、凭据、原生 Hook/插件的真实运行态
python3 -m hippomind agent-status --agent all

# 4) 健康检查（capture / distill / injection 三个 id 就是自动环路）
TOKEN="$(cat "$HOME/Library/Application Support/HippoMind/hippomindd.token")"
curl -sS -H "X-HippoMind-Token: $TOKEN" \
  http://127.0.0.1:8765/api/v1/health | python3 -m json.tool

# 5) 手工补采一次 / 手工提炼一轮
python3 -m hippomind sync-transcripts
python3 -m hippomind distill --limit 16 --dry-run
python3 -m hippomind distill --limit 16

# 6) 注入留痕
tail -n 5 "$HOME/Library/Application Support/HippoMind/injection.jsonl"

# 7) 检索质量门禁（46 条标注查询，FAIL 时 exit 1）
python3 scripts/eval_memory.py

# 8) 自动环路单测（临时账本，不碰生产库）
python3 -m unittest tests.test_automatic_loop tests.test_distill_breaker \
  tests.test_injection_trace tests.test_transcripts tests.test_pi_capture -v
```

`/api/v1/health` 里与自动环路相关的 check id：

| id | 含义 | warn 时怎么办 |
| --- | --- | --- |
| `capture` | 文件数 / 消息数 / 新增数；error 时显示错误 | `hippomind sync-transcripts`；检查来源目录与权限 |
| `distill` | 候选数 / claims 数 / 提炼数；error 时显示错误 | `hippomind distill --limit 16`；看 extractor 是否退化 |
| `injection` | 最近 N 次注入 / 实际注入次数 / 最近时间 | 见 §7「采集正常但注入停止」「Hook 静默不注入」两条 |
| `claims` | 活跃 claim 与悬空数 | `hippomind checkup --repair` |
| `fts` | 索引行 / 应索引 / 缺失 / 陈旧 | `hippomind maintain` |

## 6. 可调环境变量

daemon 由 launchd 启动时，变量必须写进 LaunchAgent plist 或启动脚本；
改完要重启 daemon 才生效（`hippomind start --restart`，或重跑自启脚本）。

| 变量 | 默认 | 作用 | 什么时候改 |
| --- | --- | --- | --- |
| `HIPPOMIND_MIN_SCORE` | `0.20` | 自动注入的分数下限 | 注入太多噪声时调高（0.25–0.35）；漏注入时调低 |
| `HIPPOMIND_DISTILL_PROVIDER` | `auto` | `auto`=`claude -p`（失败回退）、`heuristic`/`off`=只用确定性抽取 | 没有 Claude CLI、或想省 API 成本时设 `heuristic` |
| `HIPPOMIND_INJECTION_TRACE` | `1` | 写 `injection.jsonl` | 隐私敏感环境设 `0`（体检页会显示 0 次） |
| `HIPPOMIND_AUTO_INJECT` | `anywhere` | **仅当 Hook 未显式传 `--trigger-mode` 时生效**：`always`=无标记，`anywhere`=恢复"`记忆：`"标记门，`off`=关闭 | 想回到旧的手动标记行为 |
| `HIPPOMIND_TRANSCRIPT_ROOTS` | `~/.claude/projects` + `~/.codex/sessions` | `os.pathsep` 分隔的采集根 | 多机/自定义安装路径 |
| `HIPPOMIND_PI_SESSION_ROOTS` | `~/.pi/agent/sessions` | Pi 会话根 | Pi 装在别处或做测试隔离 |

相关但非自动环路专用：`HIPPOMIND_HOME`（运行目录，默认
`~/Library/Application Support/HippoMind`，决定 `injection.jsonl` 位置）、
`HIPPOMIND_SEMANTIC_VECTORS`（可选本地向量文件，见 [SEMANTIC.md](SEMANTIC.md)）、
`DSH_SESSIONS_ROOT`（DSH 会话归档根）。

注意：`install-agents` 写入的 Codex Hook 与 DSH 插件补丁里是
`--trigger-mode always` / `triggerMode: always`，因此**日常路径不读
`HIPPOMIND_AUTO_INJECT`**；该变量只影响未显式传参的调用。

## 7. 故障模式与恢复

| 症状 | 判定 | 恢复 |
| --- | --- | --- |
| `capture` 长期 `inserted=0` | 会话没增长，或来源目录变了 | `hippomind sync-transcripts` 看 inserted；核对 `~/.claude`、`~/.codex`、`~/.pi` 是否存在 |
| `capture` 有 error | 单文件/单库损坏或权限不足 | 错误信息含路径；修权限或忽略该文件（其它来源不受影响） |
| `distill.extractor=heuristic` 且 `elapsed_ms` 接近 45s | Claude CLI 不可用/未登录/超时；熔断器已开 10 分钟 | 修好 `claude` 或设 `HIPPOMIND_DISTILL_PROVIDER=heuristic` 显式降级；熔断会自动复位 |
| 采集正常但注入停止 | `injection.jsonl` 无新行 | ① `hippomind inject status --agent all` 是否被关；② `agent-status` 的 `runtime.found/ready`；③ `HIPPOMIND_MIN_SCORE` 是否过高；④ Agent 是否重启过 |
| Hook 静默不注入、`runtime.found=false` | Hook 命令里的 `--agent-config` 指向**已失效的临时 runtime 目录**（本机实测过：`/var/folders/.../tmpXXXX/agents/codex.json`，路径不存在时凭据加载失败，Hook fail-open 直接返回空） | `hippomind install-agents --agent all` 用真实 runtime 目录重写路径 → 重启 Codex；DSH 同理重写 `cordis.patch.yml` 后重启 DSH |
| Codex Hook 已安装但 `ready=false` | 信任状态未批准或 `enabled=false` | 在 Codex 里批准该 Hook（`hooks.state` 条目），或重新安装后按提示确认 |
| `claims` 检查报悬空 | 裁决后状态未收敛 | `hippomind checkup --repair` |
| `fts` 缺失/陈旧 | 投影与索引不同步 | `hippomind maintain` |
| 改了代码但 8765 行为没变 | launchd 跑的是内置盘 runtime 副本 | 重跑 `scripts/install-autostart.sh`，再 `launchctl kickstart -k gui/$(id -u)/com.hippomind.daemon` |
| WAL/库持续增长 | 长跑正常现象，超阈值才需处理 | `hippomind maintain`（含 WAL 检查点）；备份用 `hippomind backup` |

原则：**采集与注入互不牵连**。注入失败不会停止采集/提炼；采集失败也不会让
daemon 退出。任何一环出问题先看 `/api/v1/health` 对应 check，再用上表恢复。

## 8. 尚未覆盖（诚实清单）

- **Cursor**：本机未安装，`cursor_capture.py` 已按 3 套公开存储栈实现条件适配器
  并用合成 fixture 验证；真实 Cursor 端到端仍未实测，证据与补齐条件见
  [AGENT_CAPTURE_COVERAGE.md](AGENT_CAPTURE_COVERAGE.md)。
- **语义仍偏词法**：检索主通道是 FTS5/LIKE + 覆盖率/时间/触碰混合分；向量通道
  是可选项且默认关闭（见 [SEMANTIC.md](SEMANTIC.md)）。同义改写、跨语言长问句
  仍可能漏召回。
- **历史 raw 回填**：不再把 `cold` 归档行算成待办（见 [DISTILL.md](DISTILL.md)）；
  真实剩余量以 `python3 scripts/distill_status.py` 的 `accepted AND active` 口径为准，
  双队列 + 会话级摘要保证结构化记忆不被饿死。
- **注入效果闭环仍不完整**：`injection.jsonl` 现在有是否注入、空结果原因、
  memory_ids 与服务端延迟，可支撑“有注入 vs 未注入”的对照分析；但“注入后是否
  真的被用上/是否改善结果”仍只有离线复用率报告，没有线上因果证明。
  还没有自动化验证。

## 9. 相关文档

- [../REFACTOR_REPORT_ROUND5.md](../REFACTOR_REPORT_ROUND5.md) — 本轮自动记忆 v2 的实现与实测数据
- [EVAL.md](EVAL.md) — 46 条标注查询的检索质量门禁
- [SEMANTIC.md](SEMANTIC.md) — 向量通道的实测与边界
- [AGENT_CAPTURE_COVERAGE.md](AGENT_CAPTURE_COVERAGE.md) — 各 Agent 的持久存储与采集覆盖
- [../MEMORY_HUB_GUIDE.md](../MEMORY_HUB_GUIDE.md) — 安装、接入、HTTP API 与排障总览

