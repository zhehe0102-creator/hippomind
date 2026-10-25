# HippoMind 交接说明（新会话直接粘贴用）

> 生成时间：2026-10-24 22:12 PDT
> 工作目录：`/Volumes/移动硬盘/记忆系统重构`
> 当前 HEAD：`84bdd12`（round 11，工作区干净，只有无关的 `系统盘清理仪表盘.html` 未跟踪）

## 一、目标（Goal）

把 HippoMind 从"标记驱动的记忆归档"改造成**装完就忘、全自动、95 分**的本地记忆系统。

当前自评 **约 94 分**（round 10 是 93）。

## 二、系统现状（生产，已验证）

```
账本：~/Library/Application Support/HippoMind/hippomind.sqlite
daemon：127.0.0.1:8765（launchd 自启，内置盘 runtime 副本）
claim 向量索引：vector_index_claims.npz（2815 claims，512 维）
ONNX：vendor/（onnxruntime+tokenizers）+ models/bgezh/
agent 凭据：agents/*.json（13 个 agent）

注入面 23/23 健康 · 13 个 agent 端到端通过 · 冲突 0 · health READY
```

**改完代码必须**：`bash scripts/install-autostart.sh`（会自动校准注入面）
+ `launchctl kickstart -k gui/$(id -u)/com.hippomind.daemon`

## 三、本轮（round 11）最重要的一件事：注入面几乎全是坏的

首次审计发现 **23 个注入面里只有 1 个是好的**。7 个 agent 的接入全部指向
一个 `/var/folders/.../tmpXXXX/hub.sqlite` 的**临时测试库（0 条记忆）**，
而生产库有 26.7 万条。它们「看起来装好了」——配置里有 hippomind 字样、
状态命令报「已安装」——但没有一个能真正读到记忆。

| 问题 | 数量 | 原因 |
| --- | --- | --- |
| cwd/包路径在可移动卷 | 6 | launchd 后台任务被 macOS TCC 拒绝 |
| 指向失效临时路径 | 4 | 安装时的临时目录，重启即失效 |
| 根本没装 | 12 | opencode/qoder/zcode/workbuddy/antigravity/dsh |

## 四、交付的自动检测与自动修复

| 模块 | 作用 |
| --- | --- |
| `hippomind/injection_audit.py` | 判据是「这条路径现在能不能读到生产账本」，六种状态 |
| `hippomind/injection_repair.py` | 只改 hippomind 自己的条目，先备份再原子写入 |
| `hippomind/rule_repair.py` | 规则文件**整段重新生成**，不做路径字符串修补 |
| `scripts/injection_doctor.py` | 默认审计；`--fix` 预览；`--fix --apply` 落盘 |
| `scripts/install-autostart.sh` | 同步 runtime 后**自动校准所有 agent** |

Agent 覆盖从 7 个扩到 **13 个**：新增 opencode / qoder / zcode /
workbuddy / antigravity / dsh。没有实测证据的 agent 不进列表。

## 五、当前指标（全部 PASS）

| 指标 | 值 |
| --- | --- |
| keyword recall@8 / MRR / dup | **0.957 / 0.817 / 0.015** |
| semantic recall@8 / MRR | **0.509 / 0.348** |
| **pack recall@8 / MRR** | **1.000 / 1.000** |
| 注入面 / 端到端 | **23/23 · 13/13** |
| 测试 / lint | **1226 tests OK / lint: OK** |

```bash
python3 -m unittest discover -s tests -p "test_*.py"
PATH="/Users/zps/Code/artemis/.venv/bin:$PATH" scripts/lint.sh
python3 scripts/injection_doctor.py            # 注入面审计
python3 scripts/eval_memory.py --set keyword   # 也可 --set semantic / --mode pack
python3 scripts/eval_semantic.py --gate
python3 scripts/data_hygiene.py --dry-run
python3 -m hippomind.cli doctor
```

## 六、本轮三次自我纠错（都写进了代码注释与测试）

1. **解释器路径在可移动卷上不是故障**。本机 Python 装在
   `/Volumes/XcodeDev/Xcode.app` 里，判成 volatile 是假阳性。
2. **按空白切路径会切出半截**。`Application Support` 含空格，
   `/Users/x/Library/Application` 当然不存在 → 正常 hook 被误报 stale。
3. **用正则修补路径会越修越坏**。`--agent-config [^\s]+` 只匹配到第一个空格，
   残尾留在原地，产出 `.../dsh.json Support/HippoMind/agents/dsh.json'` 这种坏命令，
   而且每跑一次多叠一层。改为**整段重新生成**后彻底消除，并加了幂等性测试。

另外还发现并修掉一个产品缺陷：`daemon.reload()`（HTTP `/reload` 端点）触发
auto_install 时不传 home，会写进用户真实的 `~/.claude/CLAUDE.md`。
现在两处 auto_install 都尊重 `HIPPOMIND_HOME`，跑完全量测试后用户配置逐字节不变。

## 七、关键设计决策（别重复踩坑）

1. **后台进程必须用内置盘 runtime**。cwd / 包路径指向 `/Volumes/` 会被 TCC 拒绝，
   而且失败是静默的——看起来像「配置没生效」。
2. **审计判据是「能否读到生产账本」**，不是「文件里有没有 hippomind 字样」。
3. **规则文件只改托管段**（`<!-- HIPPO-MIND-AUTO-CONTEXT -->` 之间），
   段外是用户内容，一个字节都不动。
4. **改完代码要跑 install-autostart.sh**，它会同步 runtime 并自动校准注入面。
5. **向量只做候选生成，不做注入门控证据**（语义近邻证明话题相似，不证明事实相关）。
6. **降级到 `cold` 对检索无效**（`include_cold` 默认 True），要用 `quarantine`。
7. **批次大小保持 8**：批 16 会静默丢掉约 11% 的条目。
8. **distill tick 保持 300s、limit=64**。

## 八、离 95 分还差什么

| 维度 | 权重 | 分数 | 卡在哪 |
| --- | --- | --- | --- |
| 采集覆盖 | 15% | 90 | Cursor 未安装未实测；采集侧仍可能写重复行 |
| 自动提炼 | 25% | 93 | API 已通、去重已加；identity 根因已修但存量未清 |
| 检索质量 | 20% | 94 | claim 级大幅改善；semantic 0.509 受限于语料形态 |
| 注入质量 | 15% | 96 | 23/23 健康、13/13 端到端、自动校准 |
| 可靠性 | 10% | 95 | 熔断、自愈、health、回滚 SQL、测试隔离齐备 |
| 使用成本 | 15% | 93 | 零标记 + 自启 + 装完即生效 |

**加权约 94 分。** 要上 95，最该做的两件事：
1. **存量重复行清理**：identity 已修（增量止住），但生产库里的历史重复未删。
2. **注入效果样本**：可用样本仍为 0，注入有没有用至今测不到。

## 九、工作纪律

- 每次改完代码：全量测试 + lint，再 `install-autostart.sh` + kickstart。
- 外置卷上 `write`/`edit` 会 ENOTSUP，用 bash heredoc 或先写 /tmp 再 cp。
- 不要提交 `系统盘清理仪表盘.html`。
- **诚实优先于好看**：本轮多次出现「我以为的根因是错的」——
  metadata 不进 identity、6% 退化值是误判、注入面看似装好实则全坏。
  凡是要写进文档的数字，先跑一遍可复现的命令。

## 十、子智能体中断续接

```bash
python3 scripts/team_resume.py
```

> API 中断已恢复。继续 <task-id>：先 team_task_get 看当前进度，再检查你已写入的
> 文件，不要重做已完成部分；完成后 team_task_update action=complete 并给 Lead 发报告。现在继续。
