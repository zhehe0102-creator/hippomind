"""hippomind — 给 Agent 搭建的海马体外部记忆系统。

核心思想：大模型是"大脑皮层"，但没有海马体，学不会经验。
本库实现一个类海马体的外部记忆：

  * 情景记忆（Episodic）：动作条件化存储，按外部反馈升权 / 降权
  * 语义记忆（Semantic）：重复经验自动巩固成稳定知识（皮层固化）
  * 冷归档（Archive）：弱记忆退出活跃池，但保留恢复与审计能力
  * 联想网络（Association）：有界稀疏连接，支持受门控的联想扩散
  * 回忆（Recall）：相关性、情感、时间、触碰、证据与效用的混合检索

用法:
    from hippomind import Hippocampus
    h = Hippocampus()
    mid = h.store("看到了苹果", action="伸手", valence=1.0)
    hits = h.recall("看到苹果")
    h.reinforce(mid, +0.8)
    h.consolidate()
    h.save("memory.json"); h.load("memory.json")
"""

from .memory import EpisodicMemory, SemanticMemory, Association
from .hippocampus import Hippocampus
from .drives import DriveEngine, DRIVE_LABELS
from .mind import Mind
from .grid_world import GridWorld
from .agent import MemoryAgent

__version__ = "0.9.2"
__all__ = [
    "Hippocampus",
    "Mind",
    "DriveEngine",
    "DRIVE_LABELS",
    "EpisodicMemory",
    "SemanticMemory",
    "Association",
    "GridWorld",
    "MemoryAgent",
    "MemoryCommons",
    "CommonsHit",
    "CommonsMessage",
    "SharedMemory",
    "SQLiteLedger",
    "ContextPackBuilder",
    "ContextPackOptions",
    "ArchiveWatcher",
    "MemoryHubAgentAdapter",
    "AgentIdentity",
]


def __getattr__(name):
    """延迟加载共通层，避免 `python -m hippomind.commons` 的模块预载警告。"""
    if name in {"MemoryCommons", "CommonsHit", "CommonsMessage", "SharedMemory"}:
        from . import commons
        return getattr(commons, name)
    if name == "SQLiteLedger":
        from .ledger import SQLiteLedger
        return SQLiteLedger
    if name in {"ContextPackBuilder", "ContextPackOptions"}:
        from . import context_pack
        return getattr(context_pack, name)
    if name == "ArchiveWatcher":
        from .watcher import ArchiveWatcher
        return ArchiveWatcher
    if name in {"MemoryHubAgentAdapter", "AgentIdentity"}:
        from . import agent_adapter
        return getattr(agent_adapter, name)
    raise AttributeError(name)
