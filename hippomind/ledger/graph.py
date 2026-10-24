"""Claims, causality, entities, skill/fact views, cursors, duplicate audit.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Sequence
from ._helpers import (
    LedgerError,
    _clamp_int,
    _hash,
    _json,
    _mapping,
    _short,
    utc_now,
)

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
                      parents: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
        """Insert causal/associative parent edges for an existing memory.

        ``ingest`` can only attach relations at creation time, so a later
        association pass had no way to link memories that were written
        separately.  This keeps the same cycle guard and ``INSERT OR IGNORE``
        idempotence, and never rewrites an existing edge.
        """
        child_memory_id = _short(child_memory_id, 80)
        if not child_memory_id:
            raise LedgerError("child_memory_id 不能为空")
        prepared = []
        for parent in parents:
            parent_id = _short(parent.get("memory_id", ""), 80)
            if not parent_id or parent_id == child_memory_id:
                continue
            prepared.append((parent_id,
                             _short(parent.get("relation_type", "related_to"), 64) or "related_to",
                             float(parent.get("confidence", 0.5) or 0.5)))
        if not prepared:
            return {"created": 0, "skipped": 0}
        now = utc_now()

        def write(conn: sqlite3.Connection) -> Dict[str, int]:
            created = 0
            skipped = 0
            for parent_id, relation_type, confidence in prepared:
                if conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (parent_id,)).fetchone() is None:
                    skipped += 1
                    continue
                if self._relation_would_cycle(conn, parent_id, child_memory_id):
                    skipped += 1
                    continue
                relation_id = "rel_" + _hash(parent_id, child_memory_id, relation_type)[:32]
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO memory_relations(
                        relation_id,parent_memory_id,child_memory_id,relation_type,confidence,created_at
                    ) VALUES(?,?,?,?,?,?)
                """, (relation_id, parent_id, child_memory_id, relation_type,
                      max(0.0, min(1.0, confidence)), now))
                created += 1 if cursor.rowcount else 0
            return {"created": created, "skipped": skipped}

        return self._transaction(write)

    def causal_ancestors(self, memory_ids: Sequence[str], *, max_depth: int = 2,
                         limit: int = 32, scope: str = "", require_evidence: bool = False,
                         include_raw_chat: bool = True) -> List[Dict[str, Any]]:
        """Return a bounded, visibility-filtered parent closure.

        Causal provenance is useful only when it observes the same visibility
        and evidence contract as the Context Pack that requested it.
        """
        ids = [_short(value, 80) for value in memory_ids if _short(value, 80)]
        if not ids:
            return []
        max_depth = _clamp_int(max_depth, 2, 1, 4)
        limit = _clamp_int(limit, 32, 1, 128)
        scope = _short(scope, 128)
        placeholders = ",".join("?" for _ in ids)
        filters = ["m.lifecycle IN ('active','cold')", "m.quality_state='accepted'",
                   "(m.valid_until='' OR m.valid_until>?)",
                   "(m.valid_from='' OR m.valid_from<=?)"]
        params: List[object] = [*ids, max_depth, utc_now(), utc_now()]
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
                WITH RECURSIVE ancestors(memory_id,depth) AS (
                    SELECT parent_memory_id,1 FROM memory_relations
                    WHERE child_memory_id IN ({placeholders})
                    UNION
                    SELECT r.parent_memory_id,a.depth+1 FROM memory_relations r JOIN ancestors a
                      ON r.child_memory_id=a.memory_id WHERE a.depth<?
                ), unique_ancestors AS (
                    SELECT memory_id,MIN(depth) AS causal_depth FROM ancestors GROUP BY memory_id
                )
                SELECT m.*,a.causal_depth
                FROM unique_ancestors a CROSS JOIN memories m
                WHERE m.memory_id=a.memory_id
                  AND {' AND '.join(filters)}
                ORDER BY a.causal_depth ASC, m.updated_at DESC LIMIT ?
            """, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = self._row_dict(row)
            item["causal_depth"] = int(row["causal_depth"] or 1)
            item["score"] = max(0.0, 0.45 - 0.08 * (item["causal_depth"] - 1))
            item["score_parts"] = {"causal": item["score"]}
            result.append(item)
        return result

    def entity_graph(self, subject: str, *, scope: str = "shared",
                     hops: int = 1, limit: int = 200) -> Dict[str, Any]:
        """实体图谱：claim 主语为节点，memory_relations 因果/关联为边。

        从指定主语出发做有界 BFS（1–2 跳）。节点带活跃 claim 数与记忆数；
        边保留关系类型与置信度。匹配大小写不敏感；生产级别名合并留待
        后续（当前精确规范化匹配）。
        """
        subject = _short(subject, 256)
        if not subject:
            raise LedgerError("entity_graph 需要 subject")
        hops = _clamp_int(hops, 1, 1, 2)
        limit = _clamp_int(limit, 200, 10, 2_000)
        key = subject.strip().lower()
        with self._reading() as conn:
            # 图谱可见 claim：active + conflict（冲突实体必须可见，不能假装
            # 不存在）；superseded/retired 仍排除。节点带 conflict 计数。
            subj_rows = conn.execute(
                "SELECT c.memory_id, c.subject, c.state FROM memory_claims c "
                "WHERE c.scope=? AND c.state IN ('active','conflict')",
                (_short(scope, 128) or "shared",)).fetchall()
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
        # 关系边（仅两端都有活跃 claim 的记忆对）
        with self._reading() as conn:
            rel_rows = conn.execute(
                "SELECT r.parent_memory_id,r.child_memory_id,"
                "r.relation_type,r.confidence "
                "FROM memory_relations r LIMIT 20000").fetchall()
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
                    rec = edge_records.get(ek)
                    if rec is None or float(row["confidence"]) > rec["confidence"]:
                        edge_records[ek] = {
                            "from": display[a], "to": display[b],
                            "relation_type": str(row["relation_type"]),
                            "confidence": round(float(row["confidence"]), 3)}
                    adj.setdefault(a, {}).setdefault(b, 0.0)
                    adj[a][b] = max(adj[a].get(b, 0.0), float(row["confidence"]))
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
                        collected_edges.append(edge_records[ek])
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

    def recent_subjects(self, *, scope: str = "shared",
                        limit: int = 400) -> List[Dict[str, Any]]:
        """最近活跃的 claim 主语聚合（GUI 实体图层索引用，只读有界）。"""
        limit = _clamp_int(limit, 400, 1, 2000)
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT subject, COUNT(*) AS claims, "
                "SUM(CASE WHEN state='conflict' THEN 1 ELSE 0 END) AS conflicts "
                "FROM memory_claims WHERE scope=? AND state IN ('active','conflict') "
                "GROUP BY subject ORDER BY MAX(rowid) DESC LIMIT ?",
                (_short(scope, 128) or "shared", limit),
            ).fetchall()
        return [dict(r) for r in rows]

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
        clauses = ["c.scope=?", "c.state='active'",
                   "m.lifecycle='active'", "m.quality_state='accepted'"]
        params: List[object] = [scope]
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
        with self._reading() as conn:
            rows = conn.execute("""
                SELECT memory_id,content,metadata_json,confidence,weight,
                       source_agent,created_at
                FROM memories
                WHERE kind='procedural' AND scope=? AND lifecycle='active'
                  AND quality_state='accepted'
                ORDER BY updated_at DESC LIMIT ?
            """, (scope, limit)).fetchall()
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
