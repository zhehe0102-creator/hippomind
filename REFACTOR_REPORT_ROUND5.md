# Round 5 — 自动记忆 v2 + 子智能体续接

日期：2026-10-24
目标：把记忆系统从"标记驱动"升级为"装完就忘"的自动闭环，并解决 API 中断后
子智能体无法自动续接的问题。

## 1. 自动采集（覆盖 5 类 Agent）

| Agent | 来源 | 实现 | 实测 |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` | `transcripts.py` | 增量、游标、坏行跳过 |
| Codex CLI | `~/.codex/sessions/**/*.jsonl` | `transcripts.py` | 同上 |
| Gemini / Antigravity | `conversation_summaries.db` | `gemini_capture.py` | 2 个库、182 行 |
| Pi / PiDeck | `~/.pi/agent/sessions/**/*.jsonl` | `pi_capture.py` | 136 文件/342MB，117 messages/115 inserted |
| DeepSeek Harness | Cordis 插件 | 已有 | 会话结束结构化写回 |

Cursor：本机未安装、无持久会话存储，`docs/AGENT_CAPTURE_COVERAGE.md` 记录证据
与补齐条件，不发布猜测式 adapter。

daemon 每 60 秒跑一轮采集，`/stats.health.capture` 暴露 files/messages/
inserted/duplicate/errors 及 gemini/pi 子统计。

## 2. 自动提炼

- 每 5 分钟取 16 条未提炼记忆，用本机 `claude -p` 抽 claims；失败回退确定性抽取。
- 候选双队列：结构化 50% + 最新 raw 50%，避免 25 万 raw 饿死结构化记忆。
- 提炼记忆保留 source content、task_id、project、session_id，不再产出孤立的
  "提炼：value" 行。
- 同一 subject 的连续提炼建立 `related_to` 关系，图谱开始有边。
- **熔断器**：连续 2 次失败开 10 分钟冷却，期间走确定性抽取，任何成功解析即复位；
  超时从 90s 降到 45s。API 抖动不再把提炼循环卡死几分钟。

## 3. 冲突与一致性

- 确定性抽取的 subject 优先用 task_id，slot_key 按 memory 作用域，避免不同会话
  被误判为互相矛盾。
- 模糊冲突（置信度 <0.9 或相等）按最新 asserted_at 裁决，败者 superseded 并保留
  superseded_by；两条同为 ≥0.9 的矛盾仍留人工。
- 每次裁决后跑 `repair_claim_states`；生产库曾修掉 37 条冲突、16 条悬空 claim，
  当前 health `claims` 检查悬空 0。

## 4. 无标记注入 + 相关性门控

- Codex Hook 用 `--trigger-mode always`；DSH 插件 `triggerMode: always`；
  MCP Agent 规则默认 `--trigger-mode always`。`记忆：` 保留为强制查询。
- `ContextPackOptions.min_score`（自动路径 0.20）先过滤弱候选，再加**词法证据要求**：
  必须真有 FTS 命中或 coverage ≥0.6。
  实测："今天天气怎么样""1+1等于几""写一首诗" 现在**不注入**；
  "继续优化记忆系统的 GUI""HippoMind GUI 主题 纸本" 仍正常注入。
- `injection_trace.py` 记录 agent/query hash/items/chars/injected，不存原文；
  GUI 体检页显示最近注入。

## 5. 系统环境

- `scripts/install-autostart.sh`：把 Python 包同步到内置盘
  `~/Library/Application Support/HippoMind/runtime`，写 LaunchAgent
  （绕过外置卷 TCC），登录后后台跑 daemon。已实测 launchd PID 从内置 runtime 启动。
- **注意**：改代码后必须重跑该脚本 + `launchctl kickstart`，否则 8765 仍是旧副本。

## 6. 检索质量与门禁

- `scripts/eval_memory.py`：46 条标注查询（5 类）、recall@k/MRR/duplicate/p50/p95，
  可配置阈值与 exit code。
- 基线：**recall@8 1.000、MRR 0.893、duplicate 0.022、p50 11.2ms、p95 67.5ms，
  gate PASS**。
- 语义通道改造（`embeddings.py`）：特征族加权 + 亚线性 TF，语义通道单独排序
  recall@8 0.7826→0.8043、MRR 0.5949→0.6444；生产融合路径持平。
  增益方向一致但 bootstrap 区间刚跨 0，`docs/SEMANTIC.md` 如实记录。

## 7. GUI

体检页新增"自动记忆环路"三段管线（采集/提炼/注入，含节奏与可复制修复命令）；
脉搏页与端点页各加紧凑 automation 条带。旧 daemon 缺字段时降级为"未报告"，
不谎报 0。截图 console=0 / http≥400=0，覆盖种子库、生产库副本、760px 窄屏、
旧 daemon 降级四种场景。

## 8. 子智能体续接（本次用户明确要求）

API 中断会让子智能体停在 `in_progress`，不会自己继续。新增：

- `hippomind/team_state.py`：从 DSH session projection cache 读 `agentTeam` 板，
  合并 members/tasks、ownerId→名字、新板覆盖旧板。
- `scripts/team_resume.py`：列出所有 `in_progress` 任务、owner、写范围，并直接
  打印可直接粘贴的 `send_message` 唤醒文案。

实测：task-7（memory-capture）、task-8（memory-eval）、task-9（memory-semantics）
都在中断后被唤醒并完成；task-10 亦完成。

## 9. 当前分数（自评，按第 95 分表）

| 维度 | 权重 | 现状 | 说明 |
| --- | --- | --- | --- |
| 采集覆盖 | 15% | 85 | Claude/Codex/Gemini/Pi/DSH 已自动；Cursor 缺 |
| 自动提炼 | 25% | 70 | 有 claims/关系/冲突裁决；吞吐 16 条/5min，25 万 raw 未回填完 |
| 检索质量 | 20% | 80 | recall@8 1.0、MRR 0.89；仍是词法为主 |
| 注入质量 | 15% | 80 | 无标记 + 相关性门控 + trace；缺每轮注入后的效果评估 |
| 可靠性 | 10% | 85 | 熔断器、claim 修复、health 检查；缺自动重启验证 |
| 使用成本 | 15% | 85 | 登录自启 + 零标记；代码更新需手动刷新 runtime |

加权约 **79 分**。距离 95 的硬差距：真实语义检索、raw 全量回填吞吐、
Cursor 覆盖、注入效果闭环评估。

## 10. 验证命令

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # 729 tests OK
PATH="/Users/zps/Code/artemis/.venv/bin:$PATH" scripts/lint.sh   # OK
python3 scripts/eval_memory.py                          # gate PASS
python3 scripts/team_resume.py                          # 续接清单
python3 -m hippomind.cli agent-status
curl -s -H "X-HippoMind-Token: $(cat ~/Library/Application\ Support/HippoMind/hippomindd.token)" \
  http://127.0.0.1:8765/api/v1/health | python3 -m json.tool
```

---

# Round 6 — 注入质量、会话摘要与联想边

日期：2026-10-24

## 1. 注入质量首次被真实测量

之前只测 ledger.search（候选排序），没测模型实际收到的 Context Pack。
新增 python3 scripts/eval_memory.py --mode pack 后发现两个真问题：

| 问题 | 现象 | 修复 |
| --- | --- | --- |
| 任务锚点过激 | coverage >= 0.60 就把整包收窄到单一任务，"HippoMind GUI 主题 纸本" 丢掉含答案的记忆 | 阈值提到 0.90；真正的任务标题（coverage 1.0）仍锚定 |
| 构建过慢 | 每条候选重复分词 + 每行重复分词，单查询 4.5s、p95 1.2s，超过 DSH 700ms 超时 | token 集合缓存 + 候选只分词一次 + 门控开启时取数深度收到 384 |

结果（46 查询）：pack recall@8 0.587 -> 0.957，MRR 0.587 -> 0.957，
p50 ~120ms、p95 ~250ms；search 模式不变（recall 1.000、p95 45ms）。
daemon 启动时预热 SQLite 与 builder，首个真实请求从 650ms 降到 ~500ms，
重复请求 2-3ms。

## 2. 会话摘要：25 万 raw 的吞吐路径

hippomind/session_digest.py 按会话聚合 raw 消息，生成一段提取式摘要
（开场请求 + 决策密集行 + 收尾），一次模型调用消化整段会话，
digest:<session> 游标保证幂等。

- 质量门：剥离宿主注入的标签与逐轮句子；纯 harness 流量、自我指涉的提炼器
  会话、被注入记忆上下文主导的会话直接跳过。
- hippomind digest --drain 循环清空积压；--min-messages 控制最短会话。
- 生产实测：983/983 会话全部处理（945 条保留，218 条自我指涉与
  134 条纯上下文被清洗），单批 50 会话约 0.7s（确定性抽取）。
- 摘要 importance=7，确保交接摘要排在其来源 raw 消息之前。

## 3. 联想边

提炼只在单批次内连接同 subject 的记忆（映射每轮重置），生产库曾有
1,847 claims 但只有 41 条关系。新增 associate() 遍历近期提炼记忆，
按共享 claim subject 连接最多 3 条更早记忆（无向 related_to，幂等）；
ledger 新增 add_relations() 复用同样的环检测与 INSERT OR IGNORE。

生产：关系数 41 -> 151。3 个测试覆盖连接、幂等、无关 subject 不连。

## 4. 当前分数（更新）

| 维度 | 权重 | 上轮 | 本轮 | 说明 |
| --- | --- | --- | --- | --- |
| 采集覆盖 | 15% | 85 | 85 | 5 类自动；Cursor 未安装 |
| 自动提炼 | 25% | 70 | 80 | 会话摘要清空 983 会话积压；claims 1,800+ |
| 检索质量 | 20% | 80 | 82 | search recall 1.0 / MRR 0.87 |
| 注入质量 | 15% | 80 | 90 | pack recall 0.587->0.957，有独立门禁 |
| 可靠性 | 10% | 85 | 88 | 熔断器、claim 修复、启动预热、health 检查 |
| 使用成本 | 15% | 85 | 85 | 登录自启 + 零标记 |

加权约 84 分。仍差：真实语义 embedding、Cursor、注入效果闭环。

## 5. 复现

    python3 scripts/eval_memory.py                 # search 模式门禁
    python3 scripts/eval_memory.py --mode pack     # 注入内容门禁
    python3 -m hippomind.cli digest --drain --provider heuristic
    python3 -m unittest discover -s tests -p 'test_*.py'

---

# Round 7 — 自愈、冲突收敛与注入效果闭环

日期：2026-10-24

## 1. 崩溃自愈（实测）

LaunchAgent 之前是 KeepAlive=false：daemon 被杀后不会自己起来。改为
KeepAlive{SuccessfulExit:false} 后实测：

    kill -9 <pid>  ->  launchd 在 9.5 秒内拉起新 pid，/health 恢复 ok

## 2. 冲突收敛

repair_claim_states() 增加"孤儿冲突"修复：一个 conflict claim 如果同 slot
下已经没有其它 active/conflict claim，且它的 memory 是 accepted+active，
就重新激活。生产实测：37 条重新激活，冲突从 73 降到 36（真实分歧），
悬空 0。

## 3. 注入效果闭环（第一版）

之前只记录"注入了多少条"，没有"注入有没有用"。现在：

- trace 记录被注入的 memory_ids（最多 8 个，不存正文）；
- injection_quality.py 用注入后同一 Agent 的 assistant 消息做词面重合度，
  输出 reuse_rate / mean_overlap / sampled；
- scripts/injection_report.py 打印报告。

明确标注：这是代理指标，不是因果证明（Agent 可能本来就知道那个词），
所以报告永远同时打印样本量。

## 4. 当前生产库

    记忆 266,525 · raw 259,266 · claims 2,119（active 1,985 / conflict 36）
    关系 199 · 会话摘要 983 · 自动提炼 389

## 5. 分数（更新）

| 维度 | 权重 | 上轮 | 本轮 |
| --- | --- | --- | --- |
| 采集覆盖 | 15% | 85 | 85 |
| 自动提炼 | 25% | 80 | 82 |
| 检索质量 | 20% | 82 | 82 |
| 注入质量 | 15% | 90 | 92 |
| 可靠性 | 10% | 88 | 93 |
| 使用成本 | 15% | 85 | 85 |

加权约 86 分。

## 6. 仍差

1. 真实语义 embedding（无本地模型运行时；当前是词法 + 字符 n-gram）。
2. Cursor 未安装、无持久会话库。
3. 注入 reuse 样本要等真实使用积累；目前只有 1 条带 id 的 trace。
4. 冲突仍有 36 条真实分歧需要人工/更强模型裁决。
