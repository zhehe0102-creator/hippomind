# hippomind 使用说明与讲解指南

> v0.9.2 提供长期运行的本机 Memory Hub。原有 Grid World、JSON 和记忆可视化操作仍在本文；daemon、Context Pack、隐私、迁移备份和 Agent 接入请优先阅读 [MEMORY_HUB_GUIDE.md](MEMORY_HUB_GUIDE.md)。

跨电脑安装、导出、恢复和安全边界见 [docs/PORTABILITY.md](docs/PORTABILITY.md)。

当前发布契约：Context Pack 最多 8 项、默认 6000 token、16000 字符运输上限；raw chat 默认不注入，账本 Schema v5 为现行版本。

```bash
# 默认只启动/复用后台服务和低频 watcher
./start-hippomind.command

# 按需打开 GUI
./start-hippomind.command --gui

# 一次性安装各 Agent 的显式记忆接入；Codex 会启用原生提交 Hook
python3 -m hippomind install-agents --agent all
```

Codex 的 UserPromptSubmit Hook 只在普通用户文本包含记忆：或记忆: 时检索最小 Context Pack；标记可在自然语言任意位置，查询内容取标记之后。没有标记、引用、代码块、行内代码和空标记都不会读库、启动 daemon 或注入上下文。Pi、Claude、Cursor、Gemini、DeepSeek 和 local Agent 采用同一门控。首次安装或升级规则后，重载对应 Agent 或新建任务；一个已经打开的对话无法被后台服务追溯地改写。

本项目把大模型视为“皮层”，把可审计的外部记忆视为“海马体”：

~~~
观察/聊天 -> 情景记忆 -> 检索 -> 行动 -> 外部反馈 -> 强化或抑制
                    |                              |
                    +-> 巩固为语义规则 -> 共享语义账本
~~~

驱力只影响注意力、探索和召回排序；它不改变事实的来源、有效期或真假。模型预测也不等于
经验，只有环境、用户或评测器给出的外部反馈才会显著更新记忆效用。

## 一键开启

在 macOS Finder 中双击项目根目录的 `start-hippomind.command`。它会启动或复用本机
`hippomindd` 与低频 watcher，并幂等刷新各 Agent 的显式记忆接入，不再让 GUI 承担后台服务职责。添加 `--gui` 时才启动全屏、
简体中文的层状记忆网络：

~~~bash
./start-hippomind.command
./start-hippomind.command --gui
~~~

旧实验的 JSON 仍通过 `python3 -m hippomind.main --memory /path/to/my-memory.json` 使用；
Memory Hub 不会恢复、覆盖或清空 `hippocampus_memory.json`。找不到聊天归档时 daemon 会
报告来源错误并继续提供本地结构化写入和检索。

## 常用命令

~~~bash
# 普通 GUI：网格世界、记忆网络、驱力条和权重曲线
python3 -m hippomind.main --mode gui

# 全屏层状网络：驱力 -> 域 -> 情景 -> 语义 -> 归档 -> 共通库
python3 -m hippomind.main --mode network --shared --fullscreen

# 可复现的无界面回归实验
python3 -m hippomind.main --mode headless --steps 3000 --seed 42 --no-persist

# 只查看全部 Agent 聊天和共享规则的统计
python3 -m hippomind.commons stats

# 按需搜索原始聊天证据与共享语义规则
python3 -m hippomind.commons search "部署失败的经验" --limit 10

# 初始化共享语义账本，不会修改原始聊天索引
python3 -m hippomind.commons init
~~~

--memory 用于为不同项目或不同 Agent 指定独立本地文件。不要让两个运行中的实例同时
持久化同一个 JSON；保存是原子替换，不会产生半个文件，但最后保存者会覆盖较早的状态。

## 界面操作

普通 GUI：

- Space 开始或暂停学习
- C 巩固，P 剪枝，M 手动回忆，D 梦境消化
- R 重置，S 保存，L 读取
- 点击节点检查详情；右键取消选择；滚轮缩放，拖拽平移

全屏层状网络：

- Space 学习开关，E 巩固，P 剪枝，M 回忆，D 梦境
- I 初始化共享账本，Y 同步最近的带来源共享规则
- S 保存，L 读取；节点点击、缩放和平移与普通 GUI 相同

曲线连线来自真实亲和、标签、联想和来源关系。它们不是为了视觉效果随机生成的边。

## 全电脑 Agent 记忆互通

系统把“全量原始聊天”和“可复用共享规则”分开：

| 层 | 用途 | 是否自动写入本地活跃池 |
|---|---|---|
| Agent-Chat-Archive | 全部 Agent 的原始聊天与任务证据，只读按需检索 | 否 |
| hippomind-commons.sqlite | 带 source_ref 的共享语义规则、效用反馈和替代关系 | 否 |
| 本地 hippocampus_memory.json | 当前 Agent 的情景、语义、归档、联想与驱力 | 仅显式提升或本地经验 |

推荐工作流：

1. 用 hippomind.commons search 找到原始证据；
2. 只有确实对当前任务有用时，调用 Mind.promote_common() 提升到当前 Agent；
3. 由多次本地经验巩固为语义规则；
4. 使用 Mind.publish_commons() 发布带来源引用的规则；
5. 在真实结果出来后用 reinforce() 或共享账本 feedback() 更新效用；
6. 新事实替代旧事实时调用 supersede()，不要删除旧证据。

共享规则的 source_ref 格式为 <来源>:<稳定标识>，例如 chat:event-123 或
hippomind:42。格式校验能防止无来源文本混进共享账本，但不等同于密码学签名。

## 给 LLM 接入

~~~bash
export OPENAI_API_KEY='...'
export OPENAI_BASE_URL='https://api.deepseek.com/v1'  # 任意 OpenAI 兼容端点
export OPENAI_MODEL='deepseek-chat'

python3 -m hippomind.main --llm --shared --task "继续处理之前的部署故障"
~~~

默认行为是将本地记忆和有来源的共享规则作为**非可信参考数据**发送给模型；原始跨 Agent
聊天不会自动出站。只有在确认聊天内容可发送给该模型服务商时才显式增加：

~~~bash
python3 -m hippomind.main --llm --shared --allow-raw-chat-context --task "..."
~~~

模型回答只生成待验证动作。实际结果必须由调用者提交：

~~~python
from hippomind import Mind
from hippomind.llm_agent import LLMHippoAgent

mind = Mind(enable_commons=True)
agent = LLMHippoAgent(mind)
reply = agent.run_task("部署服务")
agent.record_feedback(-0.8, outcome="健康检查失败", lesson="先跑集成测试")
mind.save("hippocampus_memory.json")
~~~

record_feedback() 之前不会把模型自评的 good/bad 当作真实长期经验。

## 数据生命周期

- **情景记忆**：保存情境、行动、结果、情感、标签、来源与有效期。
- **语义记忆**：由重复、相近的情景形成规则；保留支持数和 episode 谱系。
- **冷归档**：弱、过期、被替代或超预算的情景退出常规检索，但仍可审计和恢复。
- **联想**：每个节点最大度数受限，扩散结果仍须通过直接相关性门槛。
- **梦境**：只做一次性展示/消化，不会被写回检索池或增强源记忆。

默认活跃预算是 180 条情景、96 条语义规则和 600 条归档。它们是上限，不是建议值。

## 安全与治理

本轮已实施的保护：

- 输入数值、特征、标签、元数据、驱力与持久化 JSON 会拒绝或归一化 NaN、Infinity 和异常结构；
- JSON 写入使用唯一临时文件、fsync 和原子替换，文件权限尽量限制为当前用户；
- 共享账本写入使用 SQLite BEGIN IMMEDIATE，并保留来源和反馈的合并语义；
- 共享规则要求来源引用格式；原始聊天默认过滤工具输出、审批转录和运行时样板；
- LLM 采用系统级不信任规则、隔离数据边界和大小限制，默认不出站原始跨 Agent 对话。

仍需接受的边界：

- 同一 macOS 账号下有文件系统权限的进程仍可读写共享账本；当前不是带签名、ACL 和多租户隔离的服务。
- 来源引用是可追溯标识，不是对内容真伪的证明。关键结论必须回到原始证据或外部系统核验。
- 提示注入无法仅靠提示词完全消除。涉及执行工具、转账、删除、发布或发送数据时，必须保留人或策略层审批。
- 原始归档可能含敏感数据。接入任何云 LLM 前，先确认数据范围、保留期和服务商政策；默认不要使用
  --allow-raw-chat-context。
- 当前文本检索是无依赖的关键词/CJK bigram 通道，复杂同义改写会漏召回。生产化升级可接入本地 embedding、
  BM25 和重排，但应保留当前来源、时间和效用门控。

## 验证与排障

~~~bash
# 自动化测试（115 项）：生命周期、持久化、共享账本、LLM 反馈、watcher
# 阻塞恢复、真实 MCP 注册握手和受控自动安装
python3 -m unittest discover          # 两种写法等价，均发现全部测试
python3 -m unittest discover -s tests -v

# 语法检查
python3 -m py_compile hippomind/*.py

# 固定种子基线
python3 -m hippomind.main --mode headless --steps 3000 --seed 42 --no-persist
~~~

预期基线中，seed 42 的记忆 Agent 应获得约 42 次觅食、1 次毒药；等步数随机策略约为
10 次觅食、9 次毒药和 576 次撞墙。若偏差很大，先确认没有加载旧记忆（使用
--no-persist）以及 Python/matplotlib 版本是否一致。
