"""Claims, causality, entities, skill/fact views, cursors, duplicate audit.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..entity_index import is_machine_id
from ._helpers import (
    LedgerError,
    _clamp_int,
    _hash,
    _json,
    _mapping,
    _short,
    _timestamp,
    utc_now,
)

# ---------------------------------------------------------------------------
# 关系类型词表与联想权重（task-3 / HEALTH_REVIEW 第四节缺口 2）
# ---------------------------------------------------------------------------
# 生产账本 2026-10-25 实测：memory_relations 2,023 行，relation_type 只有
# related_to 一种。无差别扩展的后果是「同一任务里两条无关进展」和「一条事实
# 被后继事实取代」在联想扩展里权重相同——前者只是时间上挨着，后者才是真正的
# 语义接续。
#
# 向后兼容契约（tests/test_relation_types.py 逐条锁定，改之前先读）：
#   * 存量 related_to **不迁移、不删除、不改写**；它的权重恒为 1.0，因此既有
#     账本上的 causal_ancestors / entity_graph 得分逐字节不变。
#   * 新类型只影响**新写入**的边，以及显式跑 scripts/relation_reclassify.py
#     --apply 之后的边。重分类是运维动作，不是自动行为。
#   * 未知类型（历史遗留或未来扩展）同样按 1.0 处理：绝不因为类型不认识就
#     把边丢掉或降权——丢边等于丢证据。
RELATION_SAME_SUBJECT = "same_subject"
RELATION_SUPERSEDES = "supersedes"
RELATION_DERIVED_FROM = "derived_from"
RELATION_SAME_TASK = "same_task"
RELATION_RELATED_TO = "related_to"

#: 写入侧认识的规范类型（顺序即证据强度由强到弱，供 classify 使用）。
RELATION_TYPES = (
    "caused_by",
    RELATION_SUPERSEDES,
    RELATION_DERIVED_FROM,
    RELATION_SAME_SUBJECT,
    RELATION_SAME_TASK,
    RELATION_RELATED_TO,
    "causes",
    "follows",
)

#: recent_subjects 的过取倍数与扫描上限（见该方法 docstring）。
#: 4 倍起步覆盖生产库 18.5% 的机器 id 密度；2000 是 _clamp_int 的上界，
#: 也就是「一次查询最多看多少行」的硬顶。
_SUBJECT_SCAN_FACTORS = (4, 16)
_SUBJECT_SCAN_CAP = 2000

#: 常见别名 -> 规范类型。只做拼写归一，不做语义猜测。
_RELATION_ALIASES = {
    "related": RELATION_RELATED_TO,
    "related-to": RELATION_RELATED_TO,
    "relatedto": RELATION_RELATED_TO,
    "same-subject": RELATION_SAME_SUBJECT,
    "samesubject": RELATION_SAME_SUBJECT,
    "same-task": RELATION_SAME_TASK,
    "sametask": RELATION_SAME_TASK,
    "derived-from": RELATION_DERIVED_FROM,
    "derivedfrom": RELATION_DERIVED_FROM,
    "supersede": RELATION_SUPERSEDES,
    "superseded": RELATION_SUPERSEDES,
    "superseded_by": RELATION_SUPERSEDES,
    "caused-by": "caused_by",
}

#: 对称关系：A->B 与 B->A 语义等价。写入侧**不**做方向规范化（保持调用方给的
#: 方向，存量 related_to 的方向与得分因此不变）；重分类脚本据此识别「反向边
#: 已存在」并去重，避免同一条无向关系被记两次。
SYMMETRIC_RELATION_TYPES = (RELATION_SAME_SUBJECT, RELATION_SAME_TASK,
                            RELATION_RELATED_TO)

#: 联想扩展权重：乘在因果/图谱边上，**不是**置信度。
#:
#: 只有两个**新增**的结构类型带非 1.0 权重，其余（caused_by / causes /
#: follows / related_to / supersedes / derived_from / 未知类型）全部是 1.0——
#: 这是「存量边逐字节不变」的实现方式：旧账本里只可能出现这些类型，权重恒
#: 为 1.0 时 score 公式退化成改造前那个字面量。
#:
#: same_subject(0.9)：同主语只能证明「在谈同一件事」，比显式因果弱一档；
#: same_task(0.7)：同一任务里本来就有大量无关记忆，是最弱的一层关联。
RELATION_WEIGHTS = {
    "caused_by": 1.0,
    "causes": 1.0,
    RELATION_SUPERSEDES: 1.0,
    RELATION_DERIVED_FROM: 1.0,
    "follows": 1.0,
    RELATION_SAME_SUBJECT: 0.9,
    RELATION_SAME_TASK: 0.7,
    RELATION_RELATED_TO: 1.0,
}

#: 未知类型的权重：保持既有行为（1.0），不因类型不认识而降权。
DEFAULT_RELATION_WEIGHT = 1.0


def normalise_relation_type(value: Any) -> str:
    """把调用方给的关系类型归一到规范拼写；未知类型原样保留。

    刻意**不**把未知类型改写成 related_to：那会静默抹掉调用方的意图，而且
    旧账本里已经存在 caused_by/follows 这类「非四类」的合法取值。
    """
    text = str(value or "").strip()
    if not text:
        return RELATION_RELATED_TO
    lowered = text.lower()
    if lowered in _RELATION_ALIASES:
        return _RELATION_ALIASES[lowered]
    return lowered if lowered in RELATION_TYPES else text


def relation_weight(value: Any) -> float:
    """关系类型 -> 联想扩展权重（历史/未知类型恒为 1.0，见上方兼容契约）。"""
    return float(RELATION_WEIGHTS.get(normalise_relation_type(value),
                                      DEFAULT_RELATION_WEIGHT))


def relation_id_for(parent_id: str, child_id: str, relation_type: str) -> str:
    """关系主键的唯一推导处（graph 写入与重分类脚本必须一致）。

    relation_id 由 (parent, child, type) 哈希而来，所以**改类型必须同时改
    relation_id**，否则 INSERT OR IGNORE 的幂等性会失效、同一条边能被写两次。
    """
    return "rel_" + _hash(parent_id, child_id, relation_type)[:32]


def classify_relation_type(*, supersedes: bool = False, derived: bool = False,
                           shared_subjects: int = 0, same_task: bool = False
                           ) -> Tuple[str, str]:
    """按证据强度给一条存量边最可能的类型，附带可解释理由。

    顺序即优先级：取代 > 派生 > 同主语 > 同任务 > 无更强证据。
    先判「取代」是因为它是唯一带方向性结论的证据（新事实顶替旧事实）；派生
    次之（child 的证据链里有显式 memory: 指针）；同主语再次（结构上确定，
    但只能证明「在谈同一件事」）；同任务最弱（同一任务里本来就有大量无关记忆）。
    """
    if supersedes:
        return (RELATION_SUPERSEDES,
                "一侧的 superseded_by 指向另一侧（新事实顶替旧事实）")
    if derived:
        return (RELATION_DERIVED_FROM,
                "child 的 evidence/metadata 显式指向 parent")
    if int(shared_subjects or 0) > 0:
        return (RELATION_SAME_SUBJECT,
                "共享 %d 个活跃 claim 主语" % int(shared_subjects))
    if same_task:
        return RELATION_SAME_TASK, "同一 task_id（无共享主语）"
    return RELATION_RELATED_TO, "无更强证据，保留 related_to"


class GraphMixin:
    """Claims, causality, entities, skill/fact views, cursors, duplicate audit."""

    def list_claims(self, *, memory_id: str = "", scope: str = "shared",
                    state: str = "", subject: str = "",
                    limit: int = 200) -> List[Dict[str, Any]]:
        clauses = ["scope=?"]
        params: List[object] = [_short(scope, 128) or "shared"]
        if memory_id:
            clauses.append("memory_id=?")
            params.append(_short(memory_id, 80))
        if state:
            clauses.append("state=?")
            params.append(_short(state, 32))
        if subject:
            clauses.append("subject=?")
            params.append(_short(subject, 256))
        limit = _clamp_int(limit, 200, 1, 2_000)
        with self._reading() as conn:
            rows = conn.execute("SELECT * FROM memory_claims WHERE " + " AND ".join(clauses) +
                                " ORDER BY asserted_at DESC LIMIT ?", (*params, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["value"] = json.loads(item.pop("value_json") or "null")
            except (TypeError, ValueError):
                item["value"] = None
                item.pop("value_json", None)
            result.append(item)
        return result

    def add_relations(self, child_memory_id: str,
                      parents: Sequence[Mapping[str, Any]], *,
                      default_relation_type: str = RELATION_RELATED_TO
                      ) -> Dict[str, int]:
        """Insert causal/associative parent edges for an existing memory.

        ``ingest`` can only attach relations at creation time, so a later
        association pass had no way to link memories that were written
        separately.  This keeps the same cycle guard and ``INSERT OR IGNORE``
        idempotence, and never rewrites an existing edge.

        ``default_relation_type`` 只影响**没带 relation_type 的 parent 条目**，
        默认 related_to（与改造前逐字节一致）。类型经
        :func:`normalise_relation_type` 归一：只修拼写，不猜语义，未知类型
        原样落库。
        """
        child_memory_id = _short(child_memory_id, 80)
        if not child_memory_id:
            raise LedgerError("child_memory_id 不能为空")
        fallback = normalise_relation_type(default_relation_type)
        prepared = []
        for parent in parents:
            parent_id = _short(parent.get("memory_id", ""), 80)
            if not parent_id or parent_id == child_memory_id:
                continue
            prepared.append((parent_id,
                             _short(normalise_relation_type(
                                 parent.get("relation_type") or fallback), 64)
                             or RELATION_RELATED_TO,
                             float(parent.get("confidence", 0.5) or 0.5),
                             _timestamp(parent["valid_from"]) if parent.get("valid_from") else "",
                             _timestamp(parent["valid_until"]) if parent.get("valid_until") else ""))
        if not prepared:
            return {"created": 0, "skipped": 0}
        now = utc_now()

        def write(conn: sqlite3.Connection) -> Dict[str, int]:
            created = 0
            skipped = 0
            for parent_id, relation_type, confidence, rel_from, rel_until in prepared:
                if conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (parent_id,)).fetchone() is None:
                    skipped += 1
                    continue
                if self._relation_would_cycle(conn, parent_id, child_memory_id):
                    skipped += 1
                    continue
                relation_id = relation_id_for(parent_id, child_memory_id,
                                              relation_type)
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO memory_relations(
                        relation_id,parent_memory_id,child_memory_id,relation_type,confidence,created_at,valid_from,valid_until
                    ) VALUES(?,?,?,?,?,?,?,?)
                """, (relation_id, parent_id, child_memory_id, relation_type,
                      max(0.0, min(1.0, confidence)), now, rel_from, rel_until))
                created += 1 if cursor.rowcount else 0
            return {"created": created, "skipped": skipped}

        return self._transaction(write)

    def causal_ancestors(self, memory_ids: Sequence[str], *, max_depth: int = 2,
                         limit: int = 32, scope: str = "", require_evidence: bool = False,
                         include_raw_chat: bool = True,
                         as_of: str = "") -> List[Dict[str, Any]]:
        """Return a bounded, visibility-filtered parent closure.

        Causal provenance is useful only when it observes the same visibility
        and evidence contract as the Context Pack that requested it.

        as_of：双时态历史切片（与 ledger.search 的 as_of 同一口径）。
        给定 ISO 时间点时，每条**祖先记忆**的 valid_from/valid_until 窗口按
        该时刻求值（valid_from<=as_of、valid_until 为空或 >as_of），可见性
        放宽到历史状态（accepted+superseded；active/cold/expired/superseded）
        ——当时成立、后来被取代或过期的祖先仍要能作为历史证据出现。
        quarantine/conflict 是质量裁决，不是时间语义，任何 as_of 下都不返回。

        窗口/可见性过滤只作用于**祖先记忆本身**；调用方传入的 memory_ids
        视为已知存在的起点（调用方已经决定了它们该不该被看到）。

        空串 = 现在：此时 SQL 与参数和改造前逐字节相同（两个 utc_now()
        求值点也保留），既有调用方的结果、排序与得分零变化。

        关系类型（task-3）：每条结果带 ``relation_type`` / ``relation_weight``，
        得分 = 距离衰减 x 类型权重，其中权重取**最短距离那一层**里最强的类型。

        两个刻意的选择，改之前先读：

        * **距离优先，类型只在同一层内区分强弱。** 结果本来就按
          ``causal_depth`` 升序排列；若让远处的强边压过近处的弱边，会出现
          「后面那行的 score 比前面那行高」，score 与排序自相矛盾。
        * **同层取最强，不取平均。** 平均会让「一条强边 + 一条弱边」比「只有
          一条强边」得分更低——多一条证据反而更不可信，那是错的。

        历史类型（related_to / caused_by / follows / 未知）权重恒为 1.0，此时
        得分退化成改造前那个字面量 ``max(0, 0.45 - 0.08*(depth-1))``，既有
        账本的排序逐字节不变。
        """
        ids = [_short(value, 80) for value in memory_ids if _short(value, 80)]
        if not ids:
            return []
        max_depth = _clamp_int(max_depth, 2, 1, 4)
        limit = _clamp_int(limit, 32, 1, 128)
        scope = _short(scope, 128)
        placeholders = ",".join("?" for _ in ids)
        as_of_iso = _timestamp(as_of) if as_of else ""
        if as_of_iso:
            # 历史切片：可见性与窗口规则和 ledger.search 的 as_of 分支一致。
            # 不放宽 quarantine/conflict——那是质量裁决，与「当时是否成立」
            # 无关（search 的 as_of 也只放宽 superseded 一项）。
            ref_time = as_of_iso
            filters = ["m.quality_state IN ('accepted','superseded')",
                       "m.lifecycle IN ('active','cold','expired','superseded')",
                       "(m.valid_until='' OR m.valid_until>?)",
                       "(m.valid_from='' OR m.valid_from<=?)"]
            params: List[object] = [
                *ids, ref_time, ref_time,
                max_depth, ref_time, ref_time,
                ref_time, ref_time,
            ]
        else:
            # 当前视图：保持改造前的 SQL 与两个 utc_now() 求值点，逐字节兼容。
            now_iso = utc_now()
            ref_time = now_iso
            filters = ["m.lifecycle IN ('active','cold')", "m.quality_state='accepted'",
                       "(m.valid_until='' OR m.valid_until>?)",
                       "(m.valid_from='' OR m.valid_from<=?)"]
            params = [
                *ids, ref_time, ref_time,
                max_depth, ref_time, ref_time,
                now_iso, now_iso,
            ]
        if scope:
            filters.append("m.scope=?")
            params.append(scope)
        if require_evidence:
            filters.append("m.evidence_json!='[]'")
        if not include_raw_chat:
            filters.append("m.raw_chat=0")
        params.append(limit)
        with self._reading() as conn:
            rows = conn.execute(f"""
                WITH RECURSIVE ancestors(memory_id,depth,relation_type) AS (
                    SELECT parent_memory_id,1,relation_type FROM memory_relations
                    WHERE child_memory_id IN ({placeholders})
                      AND (valid_until='' OR valid_until>?)
                      AND (valid_from='' OR valid_from<=?)
                    UNION
                    SELECT r.parent_memory_id,a.depth+1,r.relation_type
                    FROM memory_relations r JOIN ancestors a
                      ON r.child_memory_id=a.memory_id WHERE a.depth<?
                      AND (r.valid_until='' OR r.valid_until>?)
                      AND (r.valid_from='' OR r.valid_from<=?)
                ), unique_ancestors AS (
                    SELECT memory_id,MIN(depth) AS causal_depth FROM ancestors GROUP BY memory_id
                ), depth_types AS (
                    -- 每个 (祖先, 深度) 一组的类型集合。只在**最短距离那一层**
                    -- 取类型：一条远处的强边不该把一条近处的弱边抬到全权重，
                    -- 否则 score 会和按 causal_depth 的排序互相矛盾。
                    SELECT memory_id,depth,
                           GROUP_CONCAT(DISTINCT relation_type) AS relation_types
                    FROM ancestors GROUP BY memory_id,depth
                )
                SELECT m.*,a.causal_depth,d.relation_types
                FROM unique_ancestors a
                JOIN depth_types d ON d.memory_id=a.memory_id AND d.depth=a.causal_depth
                CROSS JOIN memories m
                WHERE m.memory_id=a.memory_id
                  AND {' AND '.join(filters)}
                ORDER BY a.causal_depth ASC, m.updated_at DESC LIMIT ?
            """, tuple(params)).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = self._row_dict(row)
            item["causal_depth"] = int(row["causal_depth"] or 1)
            types = [part for part in str(row["relation_types"] or "").split(",")
                     if part]
            # SQL 中间列只是聚合产物，不进结果字典（结果字典的形状是既有契约
            # 的一部分，只允许新增有语义的字段）。
            item.pop("relation_types", None)
            weight = max((relation_weight(kind) for kind in types),
                         default=DEFAULT_RELATION_WEIGHT)
            item["relation_type"] = (max(types, key=relation_weight) if types
                                     else RELATION_RELATED_TO)
            item["relation_weight"] = weight
            item["score"] = (max(0.0, 0.45 - 0.08 * (item["causal_depth"] - 1))
                              * weight)
            item["score_parts"] = {"causal": item["score"],
                                   "relation_weight": weight}
            result.append(item)
        return result

    def entity_graph(self, subject: str, *, scope: str = "shared",
                     hops: int = 1, limit: int = 200,
                     as_of: str = "") -> Dict[str, Any]:
        """实体图谱：claim 主语为节点，memory_relations 因果/关联为边。

        支持双时态切片 ``as_of``：给定 ISO 时间点时，同时按 claim、memory 与
        relation 边三者的 (valid_from, valid_until) 窗口还原当时的实体拓扑。
        """
        subject = _short(subject, 256)
        if not subject:
            raise LedgerError("entity_graph 需要 subject")
        hops = _clamp_int(hops, 1, 1, 2)
        limit = _clamp_int(limit, 200, 10, 2_000)
        key = subject.strip().lower()
        scope_val = _short(scope, 128) or "shared"
        as_of_iso = _timestamp(as_of) if str(as_of or "").strip() else ""
        ref_time = as_of_iso or utc_now()
        with self._reading() as conn:
            if as_of_iso:
                subj_rows = conn.execute(
                    "SELECT c.memory_id, c.subject, c.state FROM memory_claims c "
                    "JOIN memories m ON m.memory_id = c.memory_id "
                    "WHERE c.scope=? AND c.state IN ('active','conflict','superseded') "
                    "AND (c.valid_from='' OR c.valid_from<=?) "
                    "AND (c.valid_until='' OR c.valid_until>?) "
                    "AND m.quality_state IN ('accepted','conflict','superseded') "
                    "AND m.lifecycle IN ('active','cold','expired','superseded') "
                    "AND (m.valid_from='' OR m.valid_from<=?) "
                    "AND (m.valid_until='' OR m.valid_until>?)",
                    (scope_val, as_of_iso, as_of_iso, as_of_iso, as_of_iso)).fetchall()
            else:
                subj_rows = conn.execute(
                    "SELECT c.memory_id, c.subject, c.state FROM memory_claims c "
                    "JOIN memories m ON m.memory_id = c.memory_id "
                    "WHERE c.scope=? AND c.state IN ('active','conflict') "
                    "AND m.lifecycle='active' AND m.quality_state IN ('accepted','conflict')",
                    (scope_val,)).fetchall()
        mem_subjects: Dict[str, set] = {}
        display: Dict[str, str] = {}
        conflicts: Dict[str, int] = {}
        for row in subj_rows:
            s = str(row["subject"] or "")
            if not s:
                continue
            k = s.strip().lower()
            mem_subjects.setdefault(row["memory_id"], set()).add(k)
            display.setdefault(k, s)
            if str(row["state"]) == "conflict":
                conflicts[k] = conflicts.get(k, 0) + 1
        if key not in display:
            return {"subject": subject, "found": False, "hops": hops,
                    "nodes": [], "edges": []}
        # 关系边（限定当前 scope 活跃边，叠加边双时态窗口，按置信度倒序截断）
        rel_state_clause = (
            "c.state IN ('active','conflict','superseded')" if as_of_iso
            else "c.state IN ('active','conflict')"
        )
        with self._reading() as conn:
            rel_rows = conn.execute(
                "SELECT r.parent_memory_id,r.child_memory_id,"
                "r.relation_type,r.confidence "
                "FROM memory_relations r "
                "WHERE (r.valid_from='' OR r.valid_from<=?) "
                "  AND (r.valid_until='' OR r.valid_until>?) "
                "  AND EXISTS (SELECT 1 FROM memory_claims c "
                "              WHERE c.scope=? AND " + rel_state_clause + " "
                "                AND c.memory_id IN (r.parent_memory_id, r.child_memory_id)) "
                "ORDER BY r.confidence DESC, r.created_at DESC LIMIT 20000",
                (ref_time, ref_time, scope_val)).fetchall()
        adj: Dict[str, Dict[str, Dict[str, float]]] = {}
        edge_records: Dict[tuple, Dict[str, Any]] = {}
        for row in rel_rows:
            ps = mem_subjects.get(str(row["parent_memory_id"]), set())
            cs = mem_subjects.get(str(row["child_memory_id"]), set())
            if not ps or not cs:
                continue
            for a in ps:
                for b in cs:
                    if a == b:
                        continue
                    ek = (a, b)
                    # 边强度 = 置信度 x 类型权重：同一条边可能同时以
                    # related_to 与 same_subject 存在（重分类前后的重复边），
                    # 取更强的那个作为这条边的代表。历史类型权重恒为 1.0，
                    # 因此旧账本的 BFS 邻接顺序与改造前完全一致。
                    weight = relation_weight(row["relation_type"])
                    strength = float(row["confidence"]) * weight
                    rec = edge_records.get(ek)
                    if rec is None or strength > rec["strength"]:
                        edge_records[ek] = {
                            "from": display[a], "to": display[b],
                            "relation_type": str(row["relation_type"]),
                            "confidence": round(float(row["confidence"]), 3),
                            "weight": round(weight, 3),
                            "strength": strength}
                    adj.setdefault(a, {}).setdefault(b, 0.0)
                    adj[a][b] = max(adj[a].get(b, 0.0), strength)
        # BFS
        visited = {key}
        frontier = [key]
        node_claims: Dict[str, int] = {}
        node_memories: Dict[str, set] = {}
        for mid, ks in mem_subjects.items():
            for k in ks:
                node_claims[k] = node_claims.get(k, 0) + 1
                node_memories.setdefault(k, set()).add(mid)
        collected_edges: List[Dict[str, Any]] = []
        seen_edges: set = set()
        for _ in range(hops):
            next_frontier: List[str] = []
            for node in frontier:
                for nb in sorted(adj.get(node, {}), key=lambda x: -adj[node][x]):
                    ek = (node, nb)
                    if ek not in seen_edges:
                        seen_edges.add(ek)
                        collected = dict(edge_records[ek])
                        collected.pop("strength", None)
                        collected_edges.append(collected)
                    if nb not in visited and len(visited) < limit:
                        visited.add(nb)
                        next_frontier.append(nb)
            frontier = next_frontier
            if not frontier:
                break
        nodes = [{"subject": display[k],
                  "claims": node_claims.get(k, 0),
                  "conflicts": conflicts.get(k, 0),
                  "memories": len(node_memories.get(k, set())),
                  "hop": 0 if k == key else None}
                 for k in sorted(visited)]
        return {"subject": display[key], "found": True, "hops": hops,
                "nodes": nodes, "edges": collected_edges[:limit]}

    def _recent_subject_rows(self, scope: str, scan: int) -> List[Any]:
        """最近活跃主语的前 scan 行（未过滤，按 MAX(rowid) 倒序）。"""
        with self._reading() as conn:
            return conn.execute(
                "SELECT subject, COUNT(*) AS claims, "
                "SUM(CASE WHEN state='conflict' THEN 1 ELSE 0 END) AS conflicts "
                "FROM memory_claims WHERE scope=? AND state IN ('active','conflict') "
                "GROUP BY subject ORDER BY MAX(rowid) DESC LIMIT ?",
                (scope, scan),
            ).fetchall()

    def recent_subjects(self, *, scope: str = "shared",
                        limit: int = 400) -> List[Dict[str, Any]]:
        """最近活跃的 claim 主语聚合（GUI 实体图层索引用，只读有界）。

        **机器 id 不是实体名，必须在这里滤掉。** /api/v1/subjects 是图谱
        「实体索引」面板的唯一来源；生产库实测 400 条里 74 条（18.5%）是
        UUID / mem_xxx，用户看到的就是一列 01a0744d-180c-739e-b9e0-...，
        真正的实体（HippoMind、ADB 设备连接排查）被挤到下面。

        判据复用 hippomind.entity_index.is_machine_id（UUID / mem_ 等前缀 /
        十六进制碎片 / 纯数字），那套形态判定已经被 tests/test_entity_index.py
        逐条锁定。**不要**在这里另写一套：两套判据只会随时间漂移，而漂移的
        表现是「检索侧滤掉了、图谱侧没滤」，正是这个 bug 的成因。
        """
        limit = _clamp_int(limit, 400, 1, 2000)
        # 过滤发生在 SQL 之后，机器 id 会先占满 LIMIT 再被扔掉；按 limit 原样
        # 取会返回不足一页（生产库实测丢掉 18.5%）。逐级过取，但上限固定：
        # 一个「最近主语」列表没有理由无界扫描整张 memory_claims。
        scope = _short(scope, 128) or "shared"
        kept: List[Dict[str, Any]] = []
        for factor in _SUBJECT_SCAN_FACTORS:
            scan = min(_SUBJECT_SCAN_CAP, limit * factor)
            kept = [dict(row) for row in self._recent_subject_rows(scope, scan)
                    if not is_machine_id(row["subject"])]
            if len(kept) >= limit or scan >= _SUBJECT_SCAN_CAP:
                break
        return kept[:limit]

    def list_conflicts(self, *, scope: str = "shared",
                       limit: int = 100) -> Dict[str, Any]:
        """冲突与取代谱系：全部非 active 状态的 claim（人工裁决入口）。"""
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT c.claim_id,c.subject,c.predicate,c.slot_key,c.state,"
                "c.confidence,c.memory_id,m.content,m.quality_state "
                "FROM memory_claims c LEFT JOIN memories m ON m.memory_id=c.memory_id "
                "WHERE c.scope=? AND c.state IN ('conflict','superseded') "
                "ORDER BY c.rowid DESC LIMIT ?",
                (_short(scope, 128) or "shared", _clamp_int(limit, 100, 1, 1000)),
            ).fetchall()
        items = [dict(r) for r in rows]
        return {"scope": scope, "total": len(items), "items": items}

    def list_facts(self, *, scope: str = "shared", subject: str = "",
                   limit: int = 200) -> List[Dict[str, Any]]:
        """事实投影：活跃 claim 的结构化视图（subject/predicate/slot/value）。"""
        scope = _short(scope, 128) or "shared"
        subject = _short(subject, 256)
        limit = _clamp_int(limit, 200, 1, 2_000)
        now = utc_now()
        clauses = [
            "c.scope=?", "c.state='active'",
            "m.lifecycle='active'", "m.quality_state='accepted'",
            "(m.valid_until='' OR m.valid_until>?)",
            "(m.valid_from='' OR m.valid_from<=?)",
            "(c.valid_until='' OR c.valid_until>?)",
            "(c.valid_from='' OR c.valid_from<=?)",
        ]
        params: List[object] = [scope, now, now, now, now]
        if subject:
            clauses.append("c.subject=?")
            params.append(subject)
        with self._reading() as conn:
            rows = conn.execute(f"""
                SELECT c.claim_id,c.subject,c.predicate,c.slot_key,c.value_json,
                       c.confidence,c.source_trust,c.asserted_at,
                       m.memory_id,m.content,m.source_agent
                FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id
                WHERE {' AND '.join(clauses)}
                ORDER BY c.asserted_at DESC LIMIT ?
            """, (*params, limit)).fetchall()
        facts: List[Dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["value_json"])
            except (TypeError, ValueError):
                value = None
            facts.append({
                "claim_id": row["claim_id"], "subject": row["subject"],
                "predicate": row["predicate"], "slot_key": row["slot_key"],
                "value": value, "confidence": float(row["confidence"]),
                "source_trust": float(row["source_trust"]),
                "asserted_at": row["asserted_at"],
                "memory_id": row["memory_id"], "content": row["content"],
                "source_agent": row["source_agent"],
            })
        return facts

    def list_skills(self, *, scope: str = "shared",
                    limit: int = 100) -> List[Dict[str, Any]]:
        """技能投影：procedural 记忆的结构化视图（触发/前置/校验/步骤）。"""
        scope = _short(scope, 128) or "shared"
        limit = _clamp_int(limit, 100, 1, 1_000)
        skills: List[Dict[str, Any]] = []
        now = utc_now()
        with self._reading() as conn:
            rows = conn.execute("""
                SELECT memory_id,content,metadata_json,confidence,weight,
                       source_agent,created_at
                FROM memories
                WHERE kind='procedural' AND scope=? AND lifecycle='active'
                  AND quality_state='accepted'
                  AND (valid_until='' OR valid_until>?)
                  AND (valid_from='' OR valid_from<=?)
                ORDER BY updated_at DESC LIMIT ?
            """, (scope, now, now, limit)).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            procedural = (metadata.get("procedural")
                          if isinstance(metadata, Mapping) else {})
            procedural = procedural if isinstance(procedural, Mapping) else {}
            skills.append({
                "memory_id": row["memory_id"], "content": row["content"],
                "trigger_pattern": list(procedural.get("trigger_pattern", [])),
                "preconditions": list(procedural.get("preconditions", [])),
                "verification_assertion": list(procedural.get("verification_assertion", [])),
                "execution_pipeline": list(procedural.get("execution_pipeline", [])),
                "confidence": float(row["confidence"]),
                "weight": float(row["weight"]),
                "source_agent": row["source_agent"],
                "created_at": row["created_at"],
            })
        return skills

    @staticmethod
    def _dup_fingerprint(content: str) -> str:
        """归一化：仅去空白与标点；保留数字——「端口5432」≠「端口6432」。"""
        import re as _re
        normalized = _re.sub(r"[\s\W]+", "", str(content or "").lower(), flags=_re.UNICODE)
        return normalized

    def duplicate_candidates(self, *, limit: int = 50, sample: int = 4000,
                             min_similarity: float = 0.85) -> Dict[str, Any]:
        """近似重复记忆对报告（只读，供人工合并决策）。

        指纹分桶（归一化内容前 24 字符）后桶内做字符 Jaccard 相似度，
        避免全库两两比较。证据不同导致 content_hash 不同、得以共存的
        重复陈述会被标出。
        """
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT memory_id,content,kind,scope,created_at FROM memories "
                "WHERE quality_state='accepted' AND lifecycle IN ('active','cold') "
                "ORDER BY updated_at DESC LIMIT ?",
                (_clamp_int(sample, 4000, 100, 50_000),),
            ).fetchall()
        buckets: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            fp = self._dup_fingerprint(row["content"])
            if len(fp) < 8:
                continue
            buckets.setdefault(fp[:24], []).append(row)
        pairs: List[Dict[str, Any]] = []

        def jaccard(a: str, b: str) -> float:
            sa = {a[i:i + 3] for i in range(max(1, len(a) - 2))}
            sb = {b[i:i + 3] for i in range(max(1, len(b) - 2))}
            union = sa | sb
            return len(sa & sb) / len(union) if union else 0.0

        for group in buckets.values():
            if len(group) < 2 or len(pairs) >= limit:
                continue
            if len(group) > 200:
                # 病态大桶（如模板化日志）只抽样比较，避免 O(n²) 爆炸。
                group = group[:200]
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if len(pairs) >= limit:
                        break
                    sim = jaccard(self._dup_fingerprint(group[i]["content"]),
                                  self._dup_fingerprint(group[j]["content"]))
                    if sim >= min_similarity:
                        pairs.append({
                            "a": group[i]["memory_id"], "b": group[j]["memory_id"],
                            "similarity": round(sim, 4),
                            "preview": str(group[i]["content"])[:60],
                        })
        return {"pairs": pairs, "scanned": len(rows), "suggestions": len(pairs)}

    def list_cursors(self, *, prefix: str = "",
                     limit: int = 10_000) -> List[Dict[str, Any]]:
        """枚举游标行（source_id 前缀过滤），供维护性清理使用。"""
        limit = _clamp_int(limit, 10_000, 1, 100_000)
        with self._reading() as conn:
            if prefix:
                rows = conn.execute(
                    "SELECT source_id,metadata_json FROM source_cursors "
                    "WHERE source_id LIKE ? ORDER BY source_id LIMIT ?",
                    (prefix + "%", limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT source_id,metadata_json FROM source_cursors "
                    "ORDER BY source_id LIMIT ?", (limit,)).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            result.append({"source_id": row["source_id"],
                           "metadata": metadata if isinstance(metadata, dict) else {}})
        return result

    def delete_cursor(self, source_id: str) -> bool:
        """删除一条游标行（源文件已消失时的维护清理）。"""
        source_id = _short(source_id, 256)
        if not source_id:
            return False

        def write(conn: sqlite3.Connection) -> bool:
            cur = conn.execute("DELETE FROM source_cursors WHERE source_id=?",
                               (source_id,))
            return cur.rowcount > 0

        return self._transaction(write)

    def get_cursor(self, source_id: str) -> Dict[str, Any]:
        source_id = _short(source_id, 256)
        with self._reading() as conn:
            row = conn.execute("SELECT * FROM source_cursors WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            return {"source_id": source_id, "cursor": "", "updated_at": "", "last_success_at": "",
                    "last_error": "", "metadata": {}}
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        except (TypeError, ValueError):
            data["metadata"] = {}
        return data

    def set_cursor(self, source_id: str, cursor: str, *, error: str = "",
                   metadata: Optional[Mapping[str, Any]] = None) -> None:
        source_id = _short(source_id, 256)
        if not source_id:
            raise LedgerError("source_id 不能为空")
        cursor = _short(cursor, 4_000)
        error = _short(error, 2_000)
        now = utc_now()
        metadata_json = _json(_mapping(metadata or {}), "cursor metadata")

        def write(conn: sqlite3.Connection) -> None:
            conn.execute("""
                INSERT INTO source_cursors(source_id,cursor,updated_at,last_success_at,last_error,metadata_json)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at,
                last_success_at=CASE WHEN excluded.last_error='' THEN excluded.updated_at ELSE source_cursors.last_success_at END,
                last_error=excluded.last_error,metadata_json=excluded.metadata_json
            """, (source_id, cursor, now, now if not error else "", error, metadata_json))

        self._transaction(write)
