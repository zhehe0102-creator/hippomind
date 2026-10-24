# HippoMind 交接说明（新会话直接粘贴用）

> 生成时间：2026-10-24 04:26 PDT
> 工作目录：`/Volumes/移动硬盘/记忆系统重构`
> 当前 HEAD：`1c35b6d`（工作区干净，只有一个无关的 `系统盘清理仪表盘.html` 未跟踪）

## 一、目标（Goal）

把 HippoMind 从"标记驱动的记忆归档"改造成**装完就忘、全自动、95 分**的本地记忆系统：

1. 所有支持的 Agent 会话自动采集（无需 `记忆：` 标记）
2. 自动提炼结构化 facts / claims / relations
3. 自动处理冲突
4. 像人脑一样联想（关联边 + 图扩展）
5. 无感注入相关上下文
6. 自愈（崩溃重启、配置漂移修复）
7. 用可度量的评估证明效果

当前自评 **约 86 分**（6 个维度加权），未达 95。

## 二、系统现状（生产）

```
账本：~/Library/Application Support/HippoMind/hippomind.sqlite
daemon：127.0.0.1:8765（launchd 自启，内置盘 runtime 副本）
  ~/Library/LaunchAgents/com.hippomind.daemon.plist
  ~/Library/Application Support/HippoMind/runtime/   ← 运行的是这个副本，不是仓库！

记忆 266,539 · raw 259,266 · claims 2,167（active 2,033 / conflict 36）
关系 215 · 会话摘要 983（全部会话已处理）· 自动提炼 389
health：11 项检查，10 ok + 1 privacy warn，ready=True
```

**重要**：改完代码必须 `bash scripts/install-autostart.sh` 刷新 runtime 副本 + `launchctl kickstart -k gui/$(id -u)/com.hippomind.daemon`，否则 8765 跑的还是旧代码。

## 三、已完成的模块

| 模块 | 作用 |
| --- | --- |
| `hippomind/transcripts.py` | Claude Code / Codex JSONL 增量采集（游标、坏行跳过、半行安全） |
| `hippomind/gemini_capture.py` | Antigravity `conversation_summaries.db` 采集 |
| `hippomind/pi_capture.py` | Pi/PiDeck 会话采集（136 文件/342MB，含 compaction 摘要） |
| `hippomind/session_digest.py` | 按会话生成摘要，一次模型调用消化整段会话（983/983 已清空） |
| `hippomind/distill.py` | claims 提炼、双队列、熔断器、冲突裁决、联想边 |
| `hippomind/semantic_space.py` | 账本自拟合 LSA（numpy SVD，4000 词/128 维/4MB） |
| `hippomind/context_pack.py` | 相关性门控、任务锚点（0.90）、联想扩展、token 缓存 |
| `hippomind/injection_trace.py` | 注入 trace（agent/query hash/items/chars/memory_ids） |
| `hippomind/injection_quality.py` | 注入复用率代理指标（词面重合） |
| `hippomind/ledger/lifecycle.py` | `repair_claim_states()`：孤儿冲突重激活 |
| `hippomind/ledger/graph.py` | `add_relations()`：幂等联想边 |
| `scripts/install-autostart.sh` | 内置盘 runtime + LaunchAgent（KeepAlive 崩溃重启） |
| `scripts/eval_memory.py` | 46 条标注查询门禁（search / pack 两种模式） |
| `scripts/team_resume.py` | 子智能体中断续接清单 |

## 四、当前指标（可复现）

```bash
python3 -m unittest discover -s tests -p "test_*.py"   # 746 tests OK
PATH="/Users/zps/Code/artemis/.venv/bin:$PATH" scripts/lint.sh   # 90/90 OK
python3 scripts/eval_memory.py                          # search 模式
python3 scripts/eval_memory.py --mode pack              # 注入内容模式
python3 scripts/team_resume.py                          # 续接清单
python3 -m hippomind.cli doctor
```

| 指标 | search 模式 | pack 模式（模型实际收到的） |
| --- | --- | --- |
| recall@8 | 1.000 | 0.957 |
| MRR | 0.867 | 0.957 |
| duplicate | 0.019 | 0.000 |
| p50 / p95 | 23ms / 93ms | 123ms / 351ms |
| gate | PASS | PASS |

## 五、关键设计决策（别重复踩坑）

1. **任务锚点阈值 0.90**：0.60 时宽泛查询会被收窄到单一无关任务，pack recall 掉到 0.587。
2. **相关性门控**：`min_score=0.20` + 必须有 FTS 命中或 coverage≥0.6；实测"今天天气怎么样"不注入。
3. **语义通道只在 rerank 阶段用**：搜索阶段用向量通道会让 pack 慢 800ms，且不提升 recall。
4. **会话摘要 importance=7**：否则 2 条消息的 harness 会话会靠 recency 挤掉真正的交接摘要。
5. **摘要质量门**：剥离宿主标签/逐轮句子；纯 harness 流量、自我指涉的提炼器会话、被注入记忆主导的会话全部跳过。
6. **熔断器**：Claude API 连续 2 次失败 → 10 分钟冷却走确定性抽取。API 经常中断，这是必需的。
7. **冲突策略**：置信度接近时最新优先；两条都 ≥0.9 的矛盾留人工；孤儿冲突自动重激活。
8. **LaunchAgent 必须用内置盘 runtime**：外置卷 TCC 会拒绝 launchd 后台任务。

## 六、还没做完（下一轮优先）

1. **真实语义 embedding**：现在是账本自拟合 LSA（词共现），不是神经 embedding；受限于本机没有 torch/onnx/ollama。
2. **Cursor 采集**：本机未安装 Cursor，无持久会话库；`docs/AGENT_CAPTURE_OVERAGE.md` 记录了补齐条件。
3. **注入 reuse 样本**：机制已就绪（trace 带 memory_ids + `scripts/injection_report.py`），但要等真实使用积累样本。
4. **36 条真实冲突**：需要人工或更强模型裁决。
5. **历史 raw 回填**：会话摘要已清空 983 个会话，但 per-message 提炼仍在按 16 条/5 分钟推进。

## 七、子智能体中断续接（用户明确要求）

API 中断会让子智能体停在 `in_progress`，不会自己继续。恢复流程：

```bash
python3 scripts/team_resume.py
```

它会列出所有 `in_progress` 任务、owner、写范围，并打印可直接粘贴的 `send_message` 唤醒文案。
唤醒消息模板：

> API 中断已恢复。继续 <task-id>：先 team_task_get 看当前进度，再检查你已写入的文件，不要重做已完成部分；完成后 team_task_update action=complete 并给 Lead 发报告。现在继续。

本轮实际唤醒过 memory-capture（2 次）、memory-eval（1 次）、memory-semantics（1 次），全部完成。

## 八、工作纪律

- 每次改完代码：跑全量测试 + lint，再 `install-autostart.sh` + kickstart。
- 提交信息用中文或英文都行，但要说清"改了什么/为什么/怎么验证"。
- 不要提交 `系统盘清理仪表盘.html`（无关文件）。
- 外置卷上 `write`/`edit` 工具可能 ENOTSUP，用 `/tmp/hm-tools/ws_write.py` 或 bash heredoc。
- 长任务优先派子智能体（并行），但写范围要互斥；中断后按第七节唤醒。HM_EOF
wc -l HANDOFF.md
git add HANDOFF.md
git -c user.name='HippoMind' -c user.email='hippomind@localhost' commit -q -m "docs: handoff note for switching to a fresh conversation" 2>&1 | tail -2
git log --oneline -1
