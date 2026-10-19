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

导入策略（重要）
----------------
本包同时承载两件事：**记忆中枢（hub）** 和 **网格世界演示（demo）**。
两者以前都在这里急切导入，导致 ``import hippomind.ledger`` 会顺带加载
1.4k 行的海马体演示、驱力引擎和网格世界。更糟的是存储层曾反向
``from .hippocampus import token_sim``，形成循环依赖。

现在 ``__init__`` 只做**惰性**导出：所有名字在首次访问时才解析，因此

  * ``import hippomind.ledger`` 只加载存储层（毫秒级）；
  * ``from hippomind import Hippocampus`` 仍然可用（向后兼容）；
  * 不存在 hub → demo 的反向依赖。

公共 API 见 ``__all__``。
"""

from __future__ import annotations

from typing import Any

__version__ = "1.0.0"

#: 源码支持的账本 schema 版本。
#:
#: 单一事实来源是 :mod:`hippomind.ledger`；这里**延迟**转发，绝不复制字面量。
#: 曾经源码树停留在 v5、生产库已到 v8，导致 150 万条记忆没有任何代码能打开，
#: 而两处各自维护版本号正是让这种漂移无法被 CI 发现的直接原因。
def __getattr_schema_version() -> int:
    from .ledger import SCHEMA_VERSION as _version

    return _version

#: ``__all__`` 同时列出 hub 与 demo 的名字，保证 ``import *`` 与文档一致。
__all__ = [
    # --- demo / 记忆模型 ---
    "Hippocampus",
    "Mind",
    "DriveEngine",
    "DRIVE_LABELS",
    "EpisodicMemory",
    "SemanticMemory",
    "Association",
    "GridWorld",
    "MemoryAgent",
    # --- hub ---
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

#: ``name -> (submodule, attribute)``.  ``attribute`` may equal ``name`` for
#: plain module members.  Everything here is resolved on first access only.
_LAZY_EXPORTS = {
    # demo package
    "Hippocampus": ("hippocampus", None),
    "Mind": ("mind", None),
    "DriveEngine": ("drives", None),
    "DRIVE_LABELS": ("drives", None),
    "EpisodicMemory": ("memory", None),
    "SemanticMemory": ("memory", None),
    "Association": ("memory", None),
    "GridWorld": ("grid_world", None),
    "MemoryAgent": ("agent", None),
    # hub package
    "MemoryCommons": ("commons", None),
    "CommonsHit": ("commons", None),
    "CommonsMessage": ("commons", None),
    "SharedMemory": ("commons", None),
    "SQLiteLedger": ("ledger", None),
    "ContextPackBuilder": ("context_pack", None),
    "ContextPackOptions": ("context_pack", None),
    "ArchiveWatcher": ("watcher", None),
    "MemoryHubAgentAdapter": ("agent_adapter", None),
    "AgentIdentity": ("agent_adapter", None),
}


def __getattr__(name: str) -> Any:
    """Resolve a public export lazily on first attribute access.

    Raises :class:`AttributeError` (not :class:`ImportError`) for unknown names so
    that ``hasattr``/``getattr`` probing behaves normally.
    """
    if name == "SCHEMA_VERSION":
        value = __getattr_schema_version()
        globals()["SCHEMA_VERSION"] = value
        return value
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name = target[0]
    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name if target[1] is None else target[1])
    # Cache on the module object so subsequent lookups skip __getattr__.
    globals()[name] = value
    return value


def __dir__() -> list:
    """Expose both real globals and the lazy exports for IDE/tab completion."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS) | {"SCHEMA_VERSION"})
