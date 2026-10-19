"""Mind —— 海马体 + 心潮驱力引擎的融合心智。

对应"心潮·念"的融合思想：**欲望影响记忆影响行动**闭环。

  * 驱力偏置召回：当前强驱力让相关记忆更容易浮现（心潮的 drive bias）
  * 记忆共振：浮现的记忆按 domain 回推驱力（心潮的 memory resonance）
  * 互动消解：行动结果消解/增加驱力（心潮的 interaction effects）
  * 记忆库：四维检索 + 升权/降权 + 巩固 + 梦境消化（海马体 + Ombre Brain）

对外接口与 Hippocampus 一致（recall/store/reinforce/tick/consolidate/
prune/dream/save/load/stats_summary/...），可直接替换使用。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .hippocampus import Hippocampus, _atomic_json_write, _read_json
from .drives import DriveEngine


class Mind:
    def __init__(self, seed: Optional[int] = None, commons_path: Optional[str] = None,
                 enable_commons: bool = False, **cfg):
        # 拆分配置：带 hippo_ / memory_ 前缀的给海马体，带 drive_ 前缀的给驱力引擎
        hippo_cfg = {k[5:]: v for k, v in cfg.items() if k.startswith("hippo_")}
        drive_cfg = {k[6:]: v for k, v in cfg.items() if k.startswith("drive_")}
        self.seed = seed
        self.hippo = Hippocampus(seed=seed, **hippo_cfg)
        self.drives = DriveEngine(seed=seed, **drive_cfg)
        self.learning_enabled = True
        self.commons = None
        if enable_commons:
            self.attach_commons(commons_path)

    # ------------------------------------------------------------------
    # 记忆接口（代理到海马体，同时加入驱力闭环）
    # ------------------------------------------------------------------

    @property
    def episodic(self): return self.hippo.episodic

    @property
    def semantic(self): return self.hippo.semantic

    @property
    def archive(self): return self.hippo.archive

    @property
    def assoc(self): return self.hippo.assoc

    @property
    def dreams_history(self): return self.hippo.dreams_history

    @property
    def time(self): return self.hippo.time

    @property
    def stats(self): return self.hippo.stats

    @property
    def cfg(self): return self.hippo.cfg

    def all_memories(self): return self.hippo.all_memories()

    def _get(self, mid): return self.hippo._get(mid)

    def note_outcome(self, v, event="move"): self.hippo.note_outcome(v, event)

    @property
    def accuracy(self): return self.hippo.accuracy

    # ------------------------------------------------------------------

    def store(self, situation: str, action: str = "", outcome: str = "",
              valence: float = 0.0, arousal: float = 0.3, lesson: str = "",
              tags: Optional[List[str]] = None, features: Optional[List[float]] = None,
              importance: Optional[int] = None, **kwargs) -> int:
        return self.hippo.store(situation=situation, action=action, outcome=outcome,
                                valence=valence, arousal=arousal, lesson=lesson,
                                tags=tags, features=features, importance=importance,
                                **kwargs)

    def recall(self, query: Optional[str] = None, features: Optional[List[float]] = None,
               k: Optional[int] = None, tags: Optional[List[str]] = None,
               include_semantic: bool = True,
               emotion: Optional[Tuple[float, float]] = None,
               scope: Optional[str] = None,
               record_access: bool = True) -> List[Tuple[float, object]]:
        """带驱力闭环的回忆：强驱力偏置相关记忆，浮现的记忆回推驱力。"""
        def drive_boost(m):
            return self.drives.bias_for(list(m.tags))

        hits = self.hippo.recall(query=query, features=features, k=k, tags=tags,
                                 include_semantic=include_semantic,
                                 emotion=emotion, drive_boost=drive_boost,
                                 scope=scope, record_access=record_access)
        # 记忆共振：浮现记忆的 domain 回推驱力
        for _, m in hits[: k or self.hippo.cfg["recall_k"]]:
            self.drives.resonance(list(m.tags))
        return hits

    def attach_commons(self, archive_path: Optional[str] = None):
        """连接全电脑 Agent 聊天归档和共享语义账本。"""
        from .commons import MemoryCommons
        self.commons = MemoryCommons(archive_path)
        return self.commons

    def recall_all(self, query: str, k: Optional[int] = None,
                   agents: Optional[List[str]] = None,
                   roles: Optional[List[str]] = None,
                   include_local: bool = True,
                   include_raw_chats: bool = True):
        """统一召回当前 Agent 经验、全机聊天证据和共享语义记忆。"""
        from .commons import CommonsHit
        k = k or self.hippo.cfg["recall_k"]
        merged = []
        if include_local:
            local = self.recall(query=query, k=k)
            peak = local[0][0] if local else 1.0
            merged.extend(CommonsHit(score=score / peak if peak else 0.0,
                                     memory=memory, origin="local")
                          for score, memory in local)
        if self.commons is not None:
            shared_roles = roles if roles is not None else ("user", "assistant")
            merged.extend(self.commons.recall(query, limit=k, agents=agents,
                                              roles=shared_roles,
                                              include_chats=include_raw_chats))
        merged.sort(key=lambda hit: hit.score, reverse=True)
        results = merged[:k]
        for hit in results:
            if hit.origin != "local":
                self.drives.resonance(list(getattr(hit.memory, "tags", [])))
        return results

    def promote_common(self, hit, importance: int = 5) -> int:
        if self.commons is None:
            raise RuntimeError("尚未连接 Agent Memory Commons")
        return self.commons.promote_to(self, hit, importance=importance)

    def publish_commons(self, source_agent: str, source_session: str = "") -> List[str]:
        if self.commons is None:
            raise RuntimeError("尚未连接 Agent Memory Commons")
        return self.commons.publish_from_mind(self, source_agent, source_session)

    def reinforce(self, memory_id: int, delta: float, note: str = "") -> None:
        self.hippo.reinforce(memory_id, delta, note=note)

    def interact(self, kind: str, strength: float = 1.0) -> List[str]:
        """行动结果影响驱力（心潮 interaction effects）。"""
        return self.drives.interact(kind, strength)

    def tick(self, dt: float = 1.0) -> None:
        """同时推进记忆时钟（遗忘/巩固/梦境）与驱力时钟（增长/松弛）。"""
        if not self.learning_enabled:
            return
        self.hippo.tick(dt)
        self.drives.tick(dt)

    def consolidate(self) -> int:
        return self.hippo.consolidate()

    def dream(self, **kw) -> List[str]:
        return self.hippo.dream(**kw)

    def prune(self, weak_threshold: Optional[float] = None) -> int:
        return self.hippo.prune(weak_threshold)

    def restore(self, memory_id: int) -> bool:
        return self.hippo.restore(memory_id)

    def supersede(self, old_id: int, new_id: int) -> bool:
        return self.hippo.supersede(old_id, new_id)

    def clear(self) -> None:
        self.hippo.clear()
        self.drives.values = {d: 0.0 for d in self.drives.drives}
        self.drives.elapsed = 0.0
        self.drives.history = {d: [] for d in self.drives.drives}

    def stats_summary(self) -> Dict:
        s = self.hippo.stats_summary()
        s["drives"] = self.drives.summary()
        s["drive_hottest"] = self.drives.hottest()
        return s

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        data = {
            "mind_version": 3,
            "hippo": self.hippo.__class__.__name__,
            "drives": self.drives.to_dict(),
            "commons_path": (str(self.commons.archive_root) if self.commons else ""),
            "hippo_data": self.hippo.to_dict(),
        }
        _atomic_json_write(path, data)

    def load(self, path: str) -> None:
        data = _read_json(path)
        hippo_data = data.get("hippo_data", data)
        if not isinstance(hippo_data, dict):
            raise ValueError("Mind 记忆文件缺少有效的 hippo_data")
        # Hippocampus.load_dict is transactional; only update drives after its
        # validation succeeds, so a damaged file cannot half-restore the Mind.
        self.hippo.load_dict(hippo_data)
        if isinstance(data.get("drives"), dict):
            self.drives.from_dict(data["drives"])
        commons_path = data.get("commons_path")
        if isinstance(commons_path, str) and commons_path.strip():
            try:
                self.attach_commons(commons_path)
            except (FileNotFoundError, OSError):
                self.commons = None
