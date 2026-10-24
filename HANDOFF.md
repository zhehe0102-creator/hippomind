# HippoMind 交接说明（新会话直接粘贴用）

> 生成时间：2026-10-24 10:15 PDT
> 工作目录：`/Volumes/移动硬盘/记忆系统重构`
> 当前 HEAD：`7b19809`（round 10，工作区干净，只有无关的 `系统盘清理仪表盘.html` 未跟踪）

## 一、目标（Goal）

把 HippoMind 从"标记驱动的记忆归档"改造成**装完就忘、全自动、95 分**的本地记忆系统。

当前自评 **约 93 分**（round 8 是 86，round 9 是 89）。**仍未到 95**，差距见第六节。

## 二、系统现状（生产，已验证）

```
账本：~/Library/Application Support/HippoMind/hippomind.sqlite
daemon：127.0.0.1:8765（launchd 自启，内置盘 runtime 副本）
claim 向量索引：~/Library/Application Support/HippoMind/vector_index_claims.npz（2793 claims）
memory 向量索引：~/Library/Application Support/HippoMind/vector_index.npz（7062 docs）
ONNX：vendor/（onnxruntime+tokenizers）+ models/bgezh/（bge-small-zh 512 维）

可召回记忆 2,945（清理前 7,474）· 冲突 0 · health READY
```

**改完代码必须**：`bash scripts/install-autostart.sh` + `launchctl kickstart -k gui/$(id -u)/com.hippomind.daemon`

## 三、当前指标（全部 PASS）

| 指标 | 值 | 阈值 |
| --- | --- | --- |
| keyword recall@8 / MRR / dup | **0.957 / 0.817 / 0.015** | 0.90 / 0.75 / 0.10 |
| semantic recall@8 / MRR | **0.509 / 0.350** | 0.17 / 0.14 |
| **pack recall@8 / MRR** | **1.000 / 1.000** | 0.90 / 0.75 |
| 测试 / lint | **1151 tests OK / lint: OK** | — |

```bash
python3 -m unittest discover -s tests -p "test_*.py"
PATH="/Users/zps/Code/artemis/.venv/bin:$PATH" scripts/lint.sh
python3 scripts/eval_memory.py --set keyword   # 也可 --set semantic / --mode pack
python3 scripts/eval_semantic.py --gate        # 词法 vs claim 向量 A/B
python3 scripts/data_hygiene.py --dry-run      # 数据卫生（默认 dry-run）
python3 -m hippomind.cli doctor
```

## 四、本轮（round 9-10）的三个关键突破

### 1. 语义召回的真正瓶颈是**索引粒度**，不是模型、不是查询

一条提炼记忆把 5-10 个互不相关的事实塞进 ~550 字符正文，整体 embedding 被稀释。
改成 claim 粒度建索引后：semantic recall 0.200 → **0.467**（纯索引），
pipeline 0.067 → **0.733**。**不要和 memory 级做 RRF 融合**（实测融合更差）。

试过并否定的：查询改写（LLM 改写成关键词）0.226 → 0.189 更差；
把向量名次当注入门控证据 → "今天天气怎么样"被注入，已回退。

### 2. claude CLI 已坏，改直连 API 后提炼质量提升 11 倍

CLI 默认模型 `zcode/GLM-5.3-Flash[1m]` 不在其模型目录，调用挂起。
`hippomind/llm_client.py` 直连 `https://ai.max66.xyz`（模型 `deepseek-v4.1-flash`）：
claims 2→28、覆盖率 3.8%→**42.3%**。生产已切（/stats extractor=api）。

### 3. 数据污染是评测退化的真因，已止血

daemon 曾每 100 秒写 21 条 distill 产物、**51% 重复**。
入库质量门 + 存量清理（quarantine，非 cold）后：duplicate_rate 0.131 → **0.015**。

## 五、关键设计决策（别重复踩坑）

1. **降级到 `cold` 对检索无效**：`search.py` 的 `include_cold` 默认 True。
   要让记忆移出检索必须用 `quarantine` 或 `superseded`。
2. **`eval_memory.py` 传 `include_raw_chat=False`**：raw_chat 的污染 eval 看不见，
   别用它来判断"污染是否影响评测"。
3. **批次大小保持 8**：批 16 会静默丢掉约 11% 的条目（配对实验实测）。
4. **distill tick 保持 300s、limit=64**。tick 降到 120s 会让 210s 的活塞不进去。
   `HIPPOMIND_DISTILL_LIMIT` 环境变量可临时节流（止血用）。
5. **向量只做候选生成，不做注入门控证据**（语义近邻证明话题相似，不证明事实相关）。
6. **任务锚点阈值 0.90**；相关性门控 min_score=0.20 + 必须有词面证据。
7. **expect 必须 SQL 验证真实存在**：早期写的 `KeepAlive`/`superseded` 账本里没有。
8. **LaunchAgent 必须用内置盘 runtime**（外置卷 TCC 拒绝 launchd）。

## 六、离 95 分还差什么（诚实评估）

| 维度 | 权重 | 分数 | 卡在哪 |
| --- | --- | --- | --- |
| 采集覆盖 | 15% | 88 | Cursor 未安装未实测；采集侧仍会写重复行 |
| 自动提炼 | 25% | 92 | API 已通、去重已加；但 6% 退化值未解决，identity key 根因未修 |
| 检索质量 | 20% | 93 | claim 级大幅改善；semantic 0.509 仍受限于语料是逗号拼接 |
| 注入质量 | 15% | 92 | pack 1.000/1.000；但纯改写提问仍返回空（正确行为） |
| 可靠性 | 10% | 95 | 熔断、自愈、health、回滚 SQL 齐备 |
| 使用成本 | 15% | 90 | 零标记 + 自启；代码更新仍需手动刷新 runtime |

**加权约 93 分。** 要上 95，最该做的三件事：
1. **修 identity key**（含可变 metadata → 重复入库的根因），覆盖采集侧。风险高但收益最大。
2. **6% 退化 claim 值**：API 在短句上产出低信息量值，需要入库前的最小信息量过滤。
3. **注入样本**：可用样本仍为 0，注入效果至今测不到。

## 七、工作纪律

- 每次改完代码：全量测试 + lint，再 `install-autostart.sh` + kickstart。
- 外置卷上 `write`/`edit` 会 ENOTSUP，用 bash heredoc 或先写 /tmp 再 cp。
- 不要提交 `系统盘清理仪表盘.html`。
- **诚实优先于好看**：本轮出现三次"我判断错了、被实测推翻"（空壳是主因、
  降级到 cold 有效、查询改写有用），每次都记录在 commit 与文档里。
  凡是要写进文档的数字，先跑一遍可复现的命令。

## 八、子智能体中断续接

```bash
python3 scripts/team_resume.py
```

> API 中断已恢复。继续 <task-id>：先 team_task_get 看当前进度，再检查你已写入的
> 文件，不要重做已完成部分；完成后 team_task_update action=complete 并给 Lead 发报告。现在继续。
