# HippoMind 交接说明（新会话直接粘贴用）

> 生成时间：2026-10-24 06:40 PDT
> 工作目录：`/Volumes/移动硬盘/记忆系统重构`
> 当前 HEAD：`a16e00f`（round 8，工作区干净，只有无关的 `系统盘清理仪表盘.html` 未跟踪）

## 一、目标（Goal）

把 HippoMind 从"标记驱动的记忆归档"改造成**装完就忘、全自动、95 分**的本地记忆系统：
自动采集 → 自动提炼 claims/relations → 自动处理冲突 → 联想 → 无感注入 → 自愈 → 可度量。

当前自评 **约 91 分**（上轮 86）。

## 二、系统现状（生产，已验证）

```
账本：~/Library/Application Support/HippoMind/hippomind.sqlite
daemon：127.0.0.1:8765（launchd 自启，内置盘 runtime 副本）
向量索引：~/Library/Application Support/HippoMind/vector_index.npz（6509 条 × 512 维 = 13MB）
ONNX 依赖：~/Library/Application Support/HippoMind/vendor/（onnxruntime 1.19.2 + tokenizers）
模型：~/Library/Application Support/HippoMind/models/bgezh/（bge-small-zh-v1.5, 24MB）

记忆 ~267k · active 6.5k · claims 2362（**conflict 0**，上轮 36）· 关系 246+
health：READY（仅 1 条既有的 privacy WARN）
```

**改完代码必须**：`bash scripts/install-autostart.sh` + `launchctl kickstart -k gui/$(id -u)/com.hippomind.daemon`

## 三、本轮（round 8）最重要的三个认知纠正

### 1. "25 万 raw 未回填"是**误判**

`accepted+cold` 25.3 万行是**归档态**，不是待处理。真实待提炼只有 **3,930 行**
（去重后 1,271 次模型调用），不是 22 天。`python3 scripts/distill_status.py` 可复现。

### 2. 语义 embedding 只在**候选生成**有用，不能用来放宽注入门控

- 向量参与候选生成（RRF）：改写式查询 recall 0.207 → **0.453**，关键词集持平。
- 但把"向量名次<20"当作自动注入的相关性证据 → **"今天天气怎么样"开始被注入**。
  已回退。原因：账本 95% 是重复 raw，任何短提问都有话题近邻；
  **语义近邻证明话题相似，不证明事实相关**。
自动注入的可靠性判定**只认词面证据**（FTS 命中 / coverage≥0.6）。
纯改写提问拿不到上下文是**正确行为**。要解决需要**查询改写**，不是放宽门控。

### 3. 36 条冲突不是语义争议，是**死锁**

21 duplicate / 8 stale / 4 orphan / 3 truncation / **0 genuine**。
`quality_state='conflict'` 是记忆级标志且只有 `resolve_claim` 能清，
而 `repair_claim_states` 只在记忆已 accepted 时才复活孤儿——两边互等。
已用槽位级方法 + 不动点收敛打破。

## 四、已完成的模块

| 模块 | 作用 |
| --- | --- |
| `hippomind/semantic_onnx.py` | ONNX bge-small-zh 编码器，可选依赖，缺失即返回 None |
| `hippomind/vector_index.py` | 常驻内存向量索引，npz 持久化 + 增量 sync |
| `hippomind/ledger/search.py` | 向量作为**第三路候选生成**参与 RRF（默认关闭） |
| `scripts/eval_semantic.py` | 词法 vs 向量 A/B 对照 |
| `scripts/distill_status.py` | 提炼真相报告（cold vs active 区分） |
| `scripts/conflict_review.py` | 冲突裁决（可审计，dry-run/apply） |
| `hippomind/cursor_capture.py` | Cursor 三套存储栈采集，三态契约 |
| `scripts/eval_memory.py` | keyword(46) + semantic(53) 双集门禁，--set |
| `hippomind/injection_quality.py` | citation/paraphrase/correction + 对照臂 + 置信区间 |

## 五、当前指标（可复现）

```bash
python3 -m unittest discover -s tests -p "test_*.py"   # 1003 tests OK
PATH="/Users/zps/Code/artemis/.venv/bin:$PATH" scripts/lint.sh   # lint: OK
python3 -m hippomind.cli doctor                        # READY
python3 scripts/eval_memory.py --set keyword           # recall 1.000 / mrr 0.869 PASS
python3 scripts/eval_memory.py --set semantic          # recall 0.472 / mrr 0.349 PASS
python3 scripts/eval_memory.py --mode pack             # recall 0.957 / mrr 0.957 PASS
python3 scripts/distill_status.py
python3 scripts/conflict_review.py --show-reviews
python3 scripts/injection_report.py
```

| 指标 | 值 |
| --- | --- |
| keyword recall@8 / MRR | 1.000 / 0.869 |
| **semantic recall@8 / MRR** | **0.472 / 0.349**（纯词法时 0.207 / 0.182） |
| pack recall@8 / MRR | 0.957 / 0.957 |
| conflict claims | **0**（上轮 36） |
| 可用注入样本 | **0**（测不到效果，如实报告） |

## 六、下一步优先（按价值排序）

1. **查询改写**：这是把 semantic 0.472 变成自动注入可用的**唯一**正路。
   用模型把口语提问改写成账本词汇后再检索，而不是放宽相关性门控。
2. **注入样本积累**：可用样本仍为 0。codex 已不再使用，claude 每天有回复但从未被注入。
   要拿到样本必须让 claude 走注入路径。
3. **`claude` CLI 挂了**：默认模型 `deepseek-v4.1-flash` 不在其模型目录
   （unrecognized_model），随后 ECONNRESET；直连 API 正常（HTTP 200/7.3s）。
   这导致提炼与冲突裁决都退回启发式/规则。修好它，提炼质量才能真正提升。
4. **Cursor 端到端**：本机未安装。装好后跑 `python3 -m hippomind capture-status`，
   看到 `cursor detected` 且 `recognized >= 1` 即自动生效。
5. **语义查询集分数偏低**（0.472）：hippomind 类最差，因为 claim 值是
   "纸本；SQLite；mac-mini；6000"这种逗号拼接，单个事实几乎无词面可匹配。

## 七、关键设计决策（别重复踩坑）

1. **向量只做候选生成，不做注入门控证据**（见三.2，有实测数据）。
2. **批次大小保持 8**：批 16 会静默丢掉约 11% 的条目（配对实验实测）。
3. **daemon distill tick 保持 300s**，limit=64。tick 降到 120s 会让 210s 的活塞不进去。
4. **任务锚点阈值 0.90**；**相关性门控** min_score=0.20 + 必须有词面证据。
5. **熔断器**：Claude API 连续 2 次失败 → 10 分钟冷却走确定性抽取。
6. **LaunchAgent 必须用内置盘 runtime**（外置卷 TCC 拒绝 launchd）。
7. **expect 必须 SQL 验证真实存在**：我最初写的 `KeepAlive`/`superseded` 在账本里
   根本不存在，那两条查询永远不可能命中。

## 八、工作纪律

每次改完代码：全量测试 + lint，再 `install-autostart.sh` + kickstart。
外置卷上 `write`/`edit` 可能 ENOTSUP，用 bash heredoc 或 python 写文件。
不要提交 `系统盘清理仪表盘.html`。
**诚实优先于好看**：本轮多次出现"指标变好但实际是误注入""backlog 看着巨大其实是归档态"。
凡是要写进文档的数字，先跑一遍可复现的命令。

## 九、子智能体中断续接（用户明确要求）

```bash
python3 scripts/team_resume.py
```

唤醒模板：

> API 中断已恢复。继续 <task-id>：先 team_task_get 看当前进度，再检查你已写入的
> 文件，不要重做已完成部分；完成后 team_task_update action=complete 并给 Lead 发报告。现在继续。
