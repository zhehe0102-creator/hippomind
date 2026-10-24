"""Lexical retrieval: FTS5, LIKE rescue, coverage scoring, ranking, paging.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import json
import math
import time as _time_mod
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from ._helpers import (
    CROSS_LANGUAGE_TERMS,
    DEFAULT_AGENT_TRUST,
    LOW_SIGNAL_CJK_QUERY_TERMS,
    LOW_SIGNAL_QUERY_TERMS,
    MAX_QUERY_CHARS,
    _FTS_POOL_CEILING,
    _LIKE_POOL_CEILING,
    _LIKE_SCAN_WINDOW,
    _RAW_LIKE_SCAN_WINDOW,
    _clamp_float,
    _clamp_int,
    _context_search_text,
    _epoch,
    _short,
    _sim_tokens,
    _strings,
    _text,
    _timestamp,
    utc_now,
)

#: RRF 融合常数（Reciprocal Rank Fusion，k=60 是通行取值）。
#: 之前 fts/like 两个通道的 1/(60+rank) 是硬编码字面量；提成常量后默认值
#: 与字面量完全相同（60.0），生产排序逐字节不变（有测试锁定）。
_RRF_K = 60.0

#: 向量通道参与候选生成时，各通道各取多少条做 RRF 融合。
#: 96 来自实测：15 条改写式查询 recall 0.333 -> 0.400，46 条关键词 1.000 -> 0.978。
_DEFAULT_VECTOR_TOPN = 96


class SearchMixin:
    """Lexical retrieval: FTS5, LIKE rescue, coverage scoring, ranking, paging."""

    def _search_cached(self, key: Tuple[Any, ...]) -> Optional[List[Dict[str, Any]]]:
        hit = self._search_cache.get(key)
        if hit is None:
            return None
        stamp, rows = hit
        if time.monotonic() - stamp > self._search_cache_ttl:
            self._search_cache.pop(key, None)
            return None
        return self._copy_rows(rows)

    def _search_store(self, key: Tuple[Any, ...], rows: List[Dict[str, Any]]) -> None:
        self._search_cache[key] = (time.monotonic(), self._copy_rows(rows))
        if len(self._search_cache) > 128:
            oldest = min(self._search_cache.items(), key=lambda kv: kv[1][0])[0]
            self._search_cache.pop(oldest, None)

    @staticmethod
    def _fts_terms(query: str) -> List[str]:
        raw_terms = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]{2,}", query.lower())
        terms = [term for term in raw_terms
                 if term not in LOW_SIGNAL_QUERY_TERMS and term not in LOW_SIGNAL_CJK_QUERY_TERMS]
        # A bare product/task name such as ``Memory Hub`` can consist entirely
        # of otherwise low-signal words. Retain it only when no stronger term
        # exists; downstream coverage still requires the complete phrase.
        if not terms and len(raw_terms) >= 2:
            terms = raw_terms
        # trigram tokenizer（v6 起）需要 >=3 字的词项；对长 CJK 段补充滑动窗口，
        # 让部分短语也能经 FTS 子串匹配命中（整段词项保留为最严格通道）。
        windowed: List[str] = []
        for term in terms:
            windowed.append(term)
            if len(term) > 6 and re.fullmatch(r"[\u3400-\u9fff]+", term):
                for i in range(len(term) - 4 + 1):
                    window = term[i:i + 5]
                    if window not in windowed:
                        windowed.append(window)
        seen: List[str] = []
        for term in windowed:
            if term not in seen:
                seen.append(term)
        return seen[:12]

    @staticmethod
    def _expansion_terms(query: str) -> List[str]:
        """Strongest entity words for the sparse-candidate expansion pass."""

        query = str(query or "").lower()
        words = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]{2,}", query)
        unique: List[str] = []
        for word in sorted(set(words), key=len, reverse=True):
            if word not in unique:
                unique.append(word)
        return unique[:6]

    @staticmethod
    def _bigram_terms(query: str) -> List[str]:
        """查询 CJK 段的二元碎片：最后回退通道专用（仅共享短词也能触达）。"""

        text = str(query or "").lower()
        terms: List[str] = []
        for segment in re.findall(r"[\u3400-\u9fff]{2,}", text):
            if segment in LOW_SIGNAL_CJK_QUERY_TERMS:
                continue
            for index in range(len(segment) - 1):
                gram = segment[index:index + 2]
                if gram not in terms and gram not in LOW_SIGNAL_CJK_QUERY_TERMS:
                    terms.append(gram)
        return terms[:12]

    @staticmethod
    def _cross_language_terms(query: str) -> List[str]:
        """中英技术词映射：查询含映射表词时给出对侧翻译词。"""
        lowered = str(query or "").lower()
        out: List[str] = []
        for cn, english in CROSS_LANGUAGE_TERMS.items():
            if cn in lowered:
                out.extend(english)
                continue
            for en in english:
                if en in lowered:
                    out.append(cn)
                    break
        seen: List[str] = []
        for term in out:
            if term.lower() not in seen:
                seen.append(term.lower())
        return seen[:6]

    @staticmethod
    def _like_terms(query: str) -> List[str]:
        """Return bounded phrase fragments for the non-FTS CJK candidate path."""

        query = str(query or "").lower()
        terms: List[str] = re.findall(r"[a-z0-9_]{2,}", query)
        for segment in re.findall(r"[\u3400-\u9fff]{2,}", query):
            if segment in LOW_SIGNAL_CJK_QUERY_TERMS:
                continue
            if len(segment) < 4:
                terms.append(segment)
                continue
            # 三字碎片已足够判别（如「端口是」「负责人」），四字起会漏掉
            # 词序不同的自然问句；容量仍受 [:64] 上限约束。
            for size in range(min(8, len(segment)), 2, -1):
                terms.extend(segment[index:index + size] for index in range(len(segment) - size + 1))
        seen: List[str] = []
        for term in terms:
            if term and term not in seen:
                seen.append(term)
        # LIKE 回退是全库扫描：每个词项都是一行上的 OR 谓词。长窗口判别力
        # 最高（误报最少），裁剪到前 8 项可把扫描成本近乎减半而不损召回——
        # 实测 50k 行救援路径 ~160ms，词项数是其主要成本项。
        return seen[:8]

    def _vector_candidates(self, query: str, vector_index, clauses: Sequence[str],
                           params: Sequence[object], *, topn: int = _DEFAULT_VECTOR_TOPN,
                           include_cold: bool = True, as_of: str = ""
                           ) -> Tuple[Dict[str, int], Dict[str, sqlite3.Row]]:
        """神经向量候选通道：返回 (id->名次, id->账本行)。

        与 embedder 的"重排"角色不同，这里向量**生成候选**——词法完全没命中的
        改写式查询也能被召回，这是本次改造唯一的正收益来源（实测见
        docs/SEMANTIC.md 的「神经向量」章节）。

        三个必须做对的地方：

        1. **过滤必须在 SQL 里重放**：向量索引覆盖全库 accepted+active，而本次
           检索可能带 scope/project/task/时间窗口/raw_chat 过滤。只按 memory_id
           取行而不重放 clauses，会让跨 scope 的记忆悄悄进入结果——那是越权
           召回，不是"召回率提升"。
        2. **按过滤损耗超额取数**：索引里 top-N 有一部分会被过滤掉，所以先取
           topn * 4 再裁剪回 topn，避免过滤后通道过窄。
        3. **永不抛异常**：索引/embedder 任何失败都返回空，调用方继续走纯词法
           路径。检索不能因为可选组件而失败。
        """
        if vector_index is None or not str(query or "").strip():
            return {}, {}, {}
        try:
            fetch = max(1, int(topn)) * 4
            hits = vector_index.search_text(query, topn=fetch)
        except Exception:
            return {}, {}, {}
        if not hits:
            return {}, {}, {}
        ranked: List[Tuple[str, int]] = []
        # 余弦值必须一起带出来：下游的相关性门控（context_pack）要判断
        # "这条是可靠的语义命中，还是仅仅话题相近"，只看名次不够。
        similarity: Dict[str, float] = {}
        seen = set()
        for position, entry in enumerate(hits):
            try:
                memory_id = str(entry[0])
            except (IndexError, TypeError):
                continue
            if memory_id and memory_id not in seen:
                seen.add(memory_id)
                ranked.append((memory_id, position))
                try:
                    similarity[memory_id] = float(entry[1])
                except (IndexError, TypeError, ValueError):
                    similarity[memory_id] = 0.0
        if not ranked:
            return {}, {}, {}
        ids = [memory_id for memory_id, _position in ranked]
        rows: Dict[str, sqlite3.Row] = {}
        # SQLite 的变量上限（老版本 999）必须尊重：分批查询而不是一次塞几千个。
        chunk = 500
        try:
            with self._reading() as conn:
                for start in range(0, len(ids), chunk):
                    batch = ids[start:start + chunk]
                    where = ["m.memory_id IN (" + ",".join("?" for _ in batch) + ")",
                             *clauses]
                    for row in conn.execute(
                            "SELECT m.* FROM memories m WHERE " + " AND ".join(where),
                            (*batch, *params)).fetchall():
                        rows[row["memory_id"]] = row
        except sqlite3.DatabaseError:
            return {}, {}, {}
        if not rows:
            return {}, {}, {}
        # 名次按"过滤后仍然存在"重新紧凑编号：否则被过滤掉的 top 行会留下空洞，
        # 让 RRF 的 1/(k+rank) 系统性偏小。
        order: Dict[str, int] = {}
        kept: Dict[str, sqlite3.Row] = {}
        for memory_id in ids:
            row = rows.get(memory_id)
            if row is None or memory_id in order:
                continue
            order[memory_id] = len(order)
            kept[memory_id] = row
            if len(order) >= max(1, int(topn)):
                break
        # 余弦随候选一起返回，而不是挂在 self 上：daemon 是多线程的，
        # 实例属性会在并发查询之间互相污染。
        return order, kept, {memory_id: similarity.get(memory_id, 0.0)
                             for memory_id in order}

    @staticmethod
    def _query_coverage(query: str, corpus: str) -> Tuple[float, bool]:
        """Measure meaningful textual overlap independently of FTS rank.

        SQLite builds differ in their CJK token boundaries. A broad query can
        still find a record that shares only a common short fragment. BM25 is
        useful for candidate generation but cannot by itself prove that a
        record covers the current task.
        """
        prepared = SearchMixin._prepare_query_coverage(query)
        return SearchMixin._apply_query_coverage(prepared, corpus)

    @staticmethod
    def _prepare_query_coverage(query: str) -> Dict[str, Any]:
        """查询侧分析只做一次（多行评分循环外提升，前景轮九性能优化）。"""

        query = str(query or "").lower()
        raw_ascii_terms = [
            term for term in re.findall(r"[a-z0-9_]{2,}", query)
            if not term.startswith("__hm_")
        ]
        ascii_terms = [
            term for term in raw_ascii_terms
            if term not in LOW_SIGNAL_QUERY_TERMS and not term.startswith("__hm_")
        ]
        if not ascii_terms and len(raw_ascii_terms) >= 2:
            ascii_terms = raw_ascii_terms
        cjk_segments = [segment for segment in re.findall(r"[\u3400-\u9fff]{2,}", query)
                        if segment not in LOW_SIGNAL_CJK_QUERY_TERMS]
        cjk_bigrams = [segment[index:index + 2] for segment in cjk_segments
                       for index in range(len(segment) - 1)]
        terms = list(dict.fromkeys(ascii_terms + cjk_bigrams))[:64]
        phrase_terms = list(ascii_terms)
        # A useful identifier can consist solely of otherwise common words:
        # "Memory Hub" must remain a reliable phrase even though either word
        # alone is too broad to select an unrelated task.
        for size in range(min(4, len(raw_ascii_terms)), 1, -1):
            phrase_terms.extend(
                " ".join(raw_ascii_terms[index:index + size])
                for index in range(len(raw_ascii_terms) - size + 1)
            )
        for segment in cjk_segments:
            # A full sentence usually does not occur verbatim in a memory.
            # Four-or-more-character fragments preserve useful CJK retrieval
            # such as "自动上下文" without accepting a lone "上下文" overlap.
            for size in range(min(8, len(segment)), 3, -1):
                phrase_terms.extend(segment[index:index + size] for index in range(len(segment) - size + 1))
        return {"terms": terms, "phrase_terms": phrase_terms}

    @staticmethod
    def _apply_query_coverage(prepared: Dict[str, Any], corpus: str) -> Tuple[float, bool]:
        """对单条候选应用已准备的查询词表（corpus 侧逐行执行）。"""

        terms = prepared["terms"]
        phrase_terms = prepared["phrase_terms"]
        if not terms and not phrase_terms:
            return (0.0, False)
        corpus = str(corpus or "").lower()
        matched = sum(1 for term in terms if term in corpus)
        phrase_corpus = re.sub(r"[-_]+", " ", corpus)
        phrase_hit = any(term in phrase_corpus for term in phrase_terms)
        return (matched / len(terms) if terms else 0.0, phrase_hit)

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        for key in ("evidence_json", "tags_json", "metadata_json"):
            try:
                data[key[:-5]] = json.loads(data.pop(key) or ("{}" if key == "metadata_json" else "[]"))
            except (TypeError, ValueError):
                data[key[:-5]] = {} if key == "metadata_json" else []
        return data

    def _record_search_latency(self, seconds: float) -> None:
        """慢查询哨兵采样：线程安全地记录延迟并累计超阈值次数。"""
        with self._search_lock:
            self._search_durations.append(seconds)
            if seconds >= self._slow_search_threshold:
                self._slow_search_count += 1

    def search(self, query: str, *, scope: str = "shared", task_id: str = "",
               project: str = "", source_agents: Optional[Sequence[str]] = None,
               roles: Optional[Sequence[str]] = None, from_time: str = "", to_time: str = "",
               include_raw_chat: bool = False, require_evidence: bool = True,
               include_cold: bool = True, limit: int = 80, offset: int = 0,
               embedder=None, as_of: str = "",
               vector_index=None, vector_topn: int = _DEFAULT_VECTOR_TOPN,
               rrf_k: float = _RRF_K) -> List[Dict[str, Any]]:
        started = _time_mod.perf_counter()
        try:
            return self._search_impl(
                query, scope=scope, task_id=task_id, project=project,
                source_agents=source_agents, roles=roles, from_time=from_time,
                to_time=to_time, include_raw_chat=include_raw_chat,
                require_evidence=require_evidence, include_cold=include_cold,
                limit=limit, offset=offset, embedder=embedder, as_of=as_of,
                vector_index=vector_index, vector_topn=vector_topn, rrf_k=rrf_k)
        finally:
            self._record_search_latency(_time_mod.perf_counter() - started)

    def _search_impl(self, query: str, *, scope: str = "shared", task_id: str = "",
                     project: str = "", source_agents: Optional[Sequence[str]] = None,
                     roles: Optional[Sequence[str]] = None, from_time: str = "", to_time: str = "",
                     include_raw_chat: bool = False, require_evidence: bool = True,
                     include_cold: bool = True, limit: int = 80, offset: int = 0,
                     embedder=None, as_of: str = "",
                     vector_index=None, vector_topn: int = _DEFAULT_VECTOR_TOPN,
                     rrf_k: float = _RRF_K) -> List[Dict[str, Any]]:
        """embedder：可选向量通道（EmbeddingAdapter）。仅做重排融合，
        不参与候选生成；候选数超过 600 时自动跳过以约束成本。

        vector_index：可选神经向量索引（vector_index.VectorIndex）。与 embedder
        的分工是本次改造的核心实测结论——**向量只进重排会让指标变差**（46 条
        关键词查询 recall 1.000 -> 0.85~0.91，因为 phrase 命中时 lexical 恒为
        1.0，0.2 权重的向量压不过它）；让向量**参与候选生成**（向量 top-N 与
        FTS/LIKE 做 RRF）才有正收益（改写集 0.333 -> 0.400）。因此这里把向量
        召回当作第三路候选来源，并按名次参与既有 RRF 打分。

        **默认关闭**：vector_index=None 时本函数行为与改造前逐字节相同（有测试
        锁定）。索引打不开 / 查询向量为空 / 维度不符时静默退化为纯词法路径，
        绝不抛异常——检索不能因为可选组件而失败。

        as_of：双时态历史切片（对齐 zep/Graphiti point-in-time 语义）——
        给定 ISO 时间点时，valid_from/valid_until 窗口按该时刻求值，
        可回答"当时系统认为什么为真"；默认空 = 现在。"""
        query = _text(query, MAX_QUERY_CHARS, "query")
        scope = _short(scope, 128) or "shared"
        task_id = _short(task_id, 256)
        project = _short(project, 512)
        source_agents = _strings(source_agents, 64, 128, "source_agents")
        roles = _strings(roles, 16, 64, "roles")
        from_time = _timestamp(from_time) if from_time else ""
        as_of = _timestamp(as_of) if as_of else ""
        to_time = _timestamp(to_time) if to_time else ""
        limit = _clamp_int(limit, 80, 1, 1_000)
        offset = _clamp_int(offset, 0, 0, 100_000)
        clauses = ["m.scope=?"]
        params: List[object] = [scope]
        if task_id:
            clauses.append("m.task_id=?")
            params.append(task_id)
        if project:
            clauses.append("m.project=?")
            params.append(project)
        if source_agents:
            clauses.append("m.source_agent IN (" + ",".join("?" for _ in source_agents) + ")")
            params.extend(source_agents)
        if roles:
            clauses.append("m.role IN (" + ",".join("?" for _ in roles) + ")")
            params.extend(roles)
        if from_time:
            clauses.append("m.created_at>=?")
            params.append(from_time)
        if to_time:
            clauses.append("m.created_at<=?")
            params.append(to_time)
        if not include_raw_chat:
            clauses.append("m.raw_chat=0")
        if require_evidence:
            clauses.append("m.evidence_json!='[]'")
        # Quarantined, conflicting and superseded claims are audit-visible but
        # must not silently enter a normal Context Pack.
        clauses.append("m.quality_state='accepted'")
        if include_cold:
            clauses.append("m.lifecycle IN ('active','cold')")
        else:
            clauses.append("m.lifecycle='active'")
        if as_of:
            # point-in-time 语义：维护任务按"现在"把过期事实标为 expired，
            # 但其窗口可能包含 as_of 时刻——历史切片必须仍可见
            # （superseded 属冲突语义，保持排除）。
            clauses.append("m.lifecycle IN ('active','cold','expired')")
        # Expiry is a recall constraint, not only a maintenance concern.  A
        # daemon can be queried before its next maintenance tick, so a fact
        # with an elapsed validity window must never re-enter a Context Pack.
        # as_of 给定时，窗口按历史时刻求值（point-in-time 查询）。
        now_iso = as_of or utc_now()
        clauses.append("(m.valid_until='' OR m.valid_until>?)")
        params.append(now_iso)
        # 双时态窗口的另一侧：提前登记的定时事实在生效前不得进入 Context Pack。
        clauses.append("(m.valid_from='' OR m.valid_from<=?)")
        params.append(now_iso)
        cache_key = (self.generation, query, scope, task_id, project,
                     tuple(source_agents), tuple(roles), from_time, to_time,
                     include_raw_chat, require_evidence, include_cold,
                     limit, offset, embedder is not None, as_of,
                     # 向量通道必须进缓存键：否则开关它时同一 query 会命中上一次
                     # 的缓存页，A/B 对比直接失真（context_pack 的
                     # fold/hybrid_rerank 开关踩过同一个坑）。
                     id(vector_index) if vector_index is not None else None,
                     int(vector_topn) if vector_index is not None else None,
                     float(rrf_k) if vector_index is not None else None)
        cached = self._search_cached(cache_key) if query else None
        if cached is not None:
            return cached
        terms = self._fts_terms(query)
        fts_scores: Dict[str, float] = {}
        # RRF 融合：记录每条候选在各召回通道中的名次，双通道同时命中的
        # 记忆获得有界加分（比单通道 max 归一化更稳，见混合检索通行做法）。
        fts_order: Dict[str, int] = {}
        like_order: Dict[str, int] = {}
        candidate_rows: List[sqlite3.Row] = []
        if terms:
            expression = " OR ".join('"' + term.replace('"', '') + '"' for term in terms)
            try:
                with self._reading() as conn:
                    # Apply every structured filter before FTS candidate
                    # limiting. Otherwise a noisy raw transcript, unrelated
                    # scope, or another project can consume the whole FTS
                    # candidate budget and hide the only allowed memory.
                    fts_where = ["memory_fts MATCH ?", *clauses]
                    fts_params: List[object] = [expression, *params]
                    fts_rows = conn.execute(
                        "SELECT m.*,bm25(memory_fts) AS _fts_rank FROM memory_fts "
                        "JOIN memories m ON m.memory_id=memory_fts.memory_id WHERE " +
                        " AND ".join(fts_where) +
                        " ORDER BY bm25(memory_fts) ASC LIMIT ?",
                        (*fts_params,
                         min(_FTS_POOL_CEILING, max(limit + offset, limit * 20))),
                    ).fetchall()
                for position, row in enumerate(fts_rows):
                    fts_scores[row["memory_id"]] = max(0.0, -float(row["_fts_rank"]))
                    fts_order[row["memory_id"]] = position
                candidate_rows.extend(fts_rows)
            except sqlite3.DatabaseError:
                # FTS is an optimisation, not a reason to return unrelated history.
                fts_scores = {}
                fts_order = {}
                candidate_rows = []
        # CJK token boundaries vary by SQLite build. Add a bounded phrase-LIKE
        # candidate path even when FTS returned something, then let coverage
        # and MMR reject weak overlaps. This avoids a few noisy FTS candidates
        # hiding the exact phrase the user actually asked to continue.
        like_terms = self._like_terms(query)
        # LIKE 回退是 O(全库) 扫描（50k 行实测 584ms）。FTS 切 trigram 后中文
        # 查询已能经子串匹配命中；仅当 FTS 候选稀疏（低于召回预算）时才值得
        # 付出扫描成本做救援——富余时直接跳过。
        like_gate = max(limit * 2, 48)
        if query and like_terms and len(candidate_rows) < like_gate:
            # trigram FTS 本身支持子串 MATCH：≥3 字符的救援词走索引短语
            # 查询，语义等价于 LIKE '%term%'，但免去 O(全库) 表扫描
            # （69 万行实测 11s → 索引路径 <10ms）。关键推论：若短语
            # MATCH 已返回空，同样的 ≥3 字符词做表扫描也不可能命中
            # （trigram 覆盖全部内容且大小写不敏感）——此时仅当存在
            # <3 字符短词且候选完全为空时，才做有界窗口扫描兜底。
            phrase_terms = [term for term in like_terms if len(term) >= 3]
            short_terms = [term for term in like_terms if len(term) < 3]
            like_rows: List[sqlite3.Row] = []
            if phrase_terms:
                phrase_expr = " OR ".join(
                    '"' + term.replace('"', '') + '"' for term in phrase_terms)
                try:
                    with self._reading() as conn:
                        phrase_where = ["memory_fts MATCH ?", *clauses]
                        like_rows = conn.execute(
                            "SELECT m.*,bm25(memory_fts) AS _fts_rank FROM memory_fts "
                            "JOIN memories m ON m.memory_id=memory_fts.memory_id "
                            "WHERE " + " AND ".join(phrase_where) +
                            # bm25 序而非 updated_at：后者会诱导规划器沿
                            # updated_at 索引全表游走（77 万行 ≈1s）来喂排序；
                            # bm25 序留在 FTS 空间内，救援名次本就按相关性融合。
                            " ORDER BY bm25(memory_fts) ASC LIMIT ?",
                            (phrase_expr, *params,
                             min(_LIKE_POOL_CEILING,
                                 max(limit + offset, limit * 20, 200))),
                        ).fetchall()
                except sqlite3.DatabaseError:
                    like_rows = []
            if not like_rows and short_terms and not candidate_rows:
                like_clauses = [*clauses,
                                "(" + " OR ".join("LOWER(m.content) LIKE ?"
                                                  for _ in short_terms) + ")"]
                like_params: List[object] = [*params,
                                             *("%" + term + "%" for term in short_terms)]
                # 有界窗口：只在新度最高的行里救援，最坏成本有上限。
                with self._reading() as conn:
                    like_rows = conn.execute(
                        "SELECT * FROM ("
                        "SELECT m.* FROM memories m WHERE " + " AND ".join(like_clauses[:-1]) +
                        " ORDER BY m.updated_at DESC LIMIT ?"
                        ") AS m WHERE " + like_clauses[-1],
                        (*params,
                         min(_LIKE_SCAN_WINDOW,
                             max(limit + offset, limit * 20, 200)),
                         *like_params[len(params):]),
                    ).fetchall()
            present = {row["memory_id"] for row in candidate_rows}
            for position, row in enumerate(like_rows):
                if row["memory_id"] not in present:
                    like_order[row["memory_id"]] = position
                    candidate_rows.append(row)
        # Archived raw chat is deliberately removed from FTS during maintenance.
        # Explicit raw audit must still be complete, so add a scoped LIKE path
        # without changing the default raw_chat=false safety boundary. Low
        # signal queries can yield no FTS terms; skip instead of emitting an
        # invalid empty OR group (the generic LIKE fallback below still runs).
        if query and include_raw_chat and terms:
            # raw 归档可能占库容 99%+（本机实测 77 万行）。LIKE 审计通道
            # 用新度有界窗口（默认最新 5 万行 raw 行），否则一次显式审计
            # 查询要付出 6-11s 全表扫描；小库（<窗口）行为完全不变。
            raw_like = [*clauses, "m.raw_chat=1"]
            raw_like_group = "(" + " OR ".join("LOWER(m.content) LIKE ?" for _ in terms) + ")"
            raw_params: List[object] = [*params, *("%" + term.lower() + "%" for term in terms)]
            with self._reading() as conn:
                raw_rows = conn.execute(
                    "SELECT * FROM ("
                    "SELECT m.* FROM memories m WHERE " + " AND ".join(raw_like) +
                    " ORDER BY m.updated_at DESC LIMIT ?"
                    ") AS m WHERE " + raw_like_group,
                    (*params,
                     min(_RAW_LIKE_SCAN_WINDOW, max(limit + offset, limit * 20, 400)),
                     *raw_params[len(params):]),
                ).fetchall()
            present = {row["memory_id"] for row in candidate_rows}
            candidate_rows.extend(row for row in raw_rows if row["memory_id"] not in present)
        # 实体扩展二次检索（multi-pass entity-aware retrieval）：主查询候选
        # 稀疏时，用查询中未参与 FTS 的最强实体词补一轮受限 OR 检索；结果
        # 仍要经过同一 coverage/可靠性门槛，不会引入弱相关噪声。
        if query and len(candidate_rows) < max(limit * 2, 24):
            expansion = [term for term in self._expansion_terms(query) if term not in terms][:3]
            # 跨语言改写：中文查询补英文技术词（反之亦然）——混合语言团队的
            # 归档常用另一种语言书写，扩展通道让两侧都能触达。
            existing_lower = {term.lower() for term in terms}
            cross = [term for term in self._cross_language_terms(query)
                     if term.lower() not in existing_lower][:3]
            expansion = (expansion + cross)[:4]
            if expansion:
                exp_expression = " OR ".join('"' + term.replace('"', '') + '"' for term in expansion)
                try:
                    with self._reading() as conn:
                        exp_where = ["memory_fts MATCH ?", *clauses]
                        exp_rows = conn.execute(
                            "SELECT m.*,bm25(memory_fts) AS _fts_rank FROM memory_fts "
                            "JOIN memories m ON m.memory_id=memory_fts.memory_id WHERE " +
                            " AND ".join(exp_where) +
                            " ORDER BY bm25(memory_fts) ASC LIMIT ?",
                            (*[exp_expression, *params], min(400, max(limit + offset, limit * 8))),
                        ).fetchall()
                    present = {row["memory_id"] for row in candidate_rows}
                    candidate_rows.extend(row for row in exp_rows if row["memory_id"] not in present)
                except sqlite3.DatabaseError:
                    pass
        if query and not candidate_rows:
            # 2 字词回退：主通道（FTS/LIKE≥3字/实体扩展）全部落空时，用查询
            # CJK 段的二元碎片做最后一次有界尝试——词序不同、仅共享短词的
            # 自然问句也能触达记忆。候选仍要过评分与可靠性门槛，弱匹配不会
            # 进入 Context Pack（弃权契约不变）。
            bigram_terms = self._bigram_terms(query)
            if bigram_terms:
                bg_clauses = [*clauses, "(" + " OR ".join(
                    "LOWER(m.content) LIKE ?" for _ in bigram_terms) + ")"]
                bg_params = [*params, *("%" + term + "%" for term in bigram_terms)]
                with self._reading() as conn:
                    # 有界窗口：二元词精度低、命中面大，全表扫描在 69 万行
                    # 库上实测 0.5-1s+；只在新度最高的窗口行内尝试，
                    # 小库（<窗口）行为不变。
                    candidate_rows = conn.execute(
                        "SELECT * FROM ("
                        "SELECT m.* FROM memories m WHERE " + " AND ".join(bg_clauses[:-1]) +
                        " ORDER BY m.updated_at DESC LIMIT ?"
                        ") AS m WHERE " + bg_clauses[-1],
                        (*params,
                         min(_LIKE_SCAN_WINDOW, max(limit + offset, limit * 8)),
                         *bg_params[len(params):]),
                    ).fetchall()
        # 神经向量候选通道（可选，默认关闭）。与 embedder 的"重排"角色不同：
        # 这里向量**生成候选**，因此词法完全没命中的改写式查询也能被召回。
        # 实测（docs/SEMANTIC.md「神经向量」）：15 条改写式查询 recall
        # 0.333 -> 0.400；46 条关键词 1.000 -> 0.978（可接受的轻微下降）。
        #
        # 顺序很关键：向量候选在**词法候选生成全部跑完之后**才追加，且下面
        # 仅剩的词法回退用 lexical_empty（而非 not candidate_rows）判断。
        # 否则"向量先找到东西"会让 LIKE 救援通道被跳过，开启向量反而改变了
        # 词法行为——那就不是叠加，而是替换了。
        lexical_empty = not candidate_rows
        vector_order: Dict[str, int] = {}
        vector_rows: Dict[str, sqlite3.Row] = {}
        vector_similarity: Dict[str, float] = {}
        if query and vector_index is not None:
            vector_order, vector_rows, vector_similarity = self._vector_candidates(
                query, vector_index, clauses, params,
                topn=vector_topn, include_cold=include_cold, as_of=as_of)
            if vector_rows:
                present = {row["memory_id"] for row in candidate_rows}
                candidate_rows.extend(row for memory_id, row in vector_rows.items()
                                      if memory_id not in present)
        if query and lexical_empty:
            terms_like = self._like_terms(query)
            if not terms_like:
                return []
            clauses.append("(" + " OR ".join("LOWER(m.content) LIKE ?" for _ in terms_like) + ")")
            params.extend(["%" + term.lower() + "%" for term in terms_like])
        if candidate_rows:
            rows = candidate_rows
        else:
            sql = "SELECT m.* FROM memories m WHERE " + " AND ".join(clauses) + " ORDER BY m.updated_at DESC LIMIT ?"
            params.append(min(5_000, max(limit + offset, limit * 12)))
            with self._reading() as conn:
                rows = conn.execute(sql, params).fetchall()
        now = time.time()
        max_fts = max(fts_scores.values(), default=0.0)
        # 查询侧常量工作提到行循环外：coverage 词表与查询 token 只算一次
        # （此前每行重复正则/切分，剖析显示占检索耗时显著份额）。
        qprep = self._prepare_query_coverage(query) if query else None
        qtokens = _sim_tokens(query) if query else None
        # 可选向量通道：查询向量只算一次；候选集过大时跳过（成本约束），
        # 小权重融合进总分，不替换关键词通道。
        query_vec = embedder.embed(query) if (embedder is not None and query) else None
        use_vector = query_vec is not None and len(rows) <= 600
        results: List[Dict[str, Any]] = []
        for row in rows:
            item = self._row_dict(row)
            context_text = _context_search_text(item.get("metadata", {}))
            corpus = " ".join([item["content"], context_text, item["task_id"],
                                item["project"], item["kind"]])
            if qprep is not None:
                ctokens = _sim_tokens(corpus)
                union = len(qtokens | ctokens)
                topic = (len(qtokens & ctokens) / union) if union else 0.0
                coverage, phrase_hit = self._apply_query_coverage(qprep, corpus)
            else:
                topic = 0.7
                coverage, phrase_hit = (1.0, True)
            fts = fts_scores.get(item["memory_id"], 0.0)
            # Rank selects candidates; coverage prevents a single shared CJK
            # character from being promoted to a full-strength FTS hit.
            fts = (fts / max_fts if max_fts else 0.0) * coverage
            # RRF 双通道融合加分：FTS 与 LIKE 各按名次贡献 1/(60+rank)，
            # 归一化后作为小权重信号——两路独立命中的证据更可信。
            # 向量通道加入后归一化分母同步变化：三通道各贡献 1/(k+0)=1/k，
            # 归一化后仍是 0..1，因此既有两通道的相对强弱不被改写。
            channel_count = 3 if vector_order else 2
            rrf = 0.0
            position = fts_order.get(item["memory_id"])
            if position is not None:
                rrf += 1.0 / (rrf_k + position)
            position = like_order.get(item["memory_id"])
            if position is not None:
                rrf += 1.0 / (rrf_k + position)
            position = vector_order.get(item["memory_id"])
            if position is not None:
                rrf += 1.0 / (rrf_k + position)
            rrf_norm = min(1.0, rrf / (channel_count / (rrf_k + 1.0)))
            vec_sim = 0.0
            if use_vector:
                corpus_vec = embedder.embed(corpus)
                vec_sim = max(0.0, float(embedder.cosine(query_vec, corpus_vec)))
            # ``updated_at`` changes when feedback or maintenance touches the
            # mutable projection. Source freshness must track the immutable
            # event timestamp instead, otherwise old hot memories outrank a
            # newer project update merely because they were maintained often.
            source_epoch = _epoch(item.get("created_at", "")) or _epoch(item["updated_at"])
            age_days = max(0.0, now - source_epoch) / 86400.0
            recency = 1.0 / (1.0 + age_days / 60.0)
            access_epoch = _epoch(item.get("last_access_at", "")) or source_epoch
            access_age_days = max(0.0, now - access_epoch) / 86400.0
            touch = (min(1.0, math.log1p(int(item["touch_count"])) / math.log(11.0))
                     / (1.0 + access_age_days / 14.0))
            utility = (float(item["utility"]) + 1.0) / 2.0
            task_scope = 1.0 if (task_id and item["task_id"] == task_id) else (
                1.0 if project and item["project"] == project else 0.0)
            structured = 1.0 if (not item.get("raw_chat") and item.get("kind") in {
                "task", "goal", "progress", "decision", "blocker", "next_step", "result", "semantic",
                "procedural",
            }) else 0.0
            trust = _clamp_float(item.get("agent_trust", DEFAULT_AGENT_TRUST), DEFAULT_AGENT_TRUST, 0.0, 1.0)
            quality = _clamp_float(item.get("quality_score", 1.0), 1.0, 0.0, 1.0)
            # Keyword/FTS selects evidence first; task scope, immutable source
            # freshness, confidence, importance and actual rendered touches
            # then settle ties. Raw chat is filtered by default before this
            # path, so it cannot crowd out structured continuation state.
            pinned = 1 if int(item.get("pinned", 0) or 0) else 0
            score = (0.47 * max(topic, fts) + 0.14 * recency + 0.025 * touch +
                     0.04 * float(item["weight"]) + 0.09 * float(item["confidence"]) +
                     0.06 * utility + 0.04 * int(item["importance"]) / 10.0 +
                     0.07 * task_scope + 0.05 * structured + 0.02 * trust + 0.02 * quality +
                     0.05 * rrf_norm + 0.04 * vec_sim + 0.06 * pinned)
            item["score"] = min(1.0, score)
            item["score_parts"] = {"topic": round(topic, 4), "fts": round(fts, 4),
                                   "coverage": round(coverage, 4), "phrase": phrase_hit,
                                   "rrf": round(rrf_norm, 4),
                                   "time": round(recency, 4), "touch": round(touch, 4),
                                   "weight": round(float(item["weight"]), 4),
                                   "confidence": round(float(item["confidence"]), 4),
                                   "trust": round(trust, 4), "quality": round(quality, 4),
                                   "utility": round(utility, 4),
                                   "task_scope": round(task_scope, 4),
                                   "structured": round(structured, 4),
                                   "pinned": pinned}
            if use_vector:
                item["score_parts"]["vec"] = round(vec_sim, 4)
            if vector_order:
                position = vector_order.get(item["memory_id"])
                if position is not None:
                    item["score_parts"]["vec_rank"] = position
                    # 语义余弦必须写进 score_parts：下游相关性门控靠它区分
                    # "可靠的语义命中"与"仅仅话题相近"，只看名次不够。
                    item["score_parts"]["vec"] = round(
                        float(vector_similarity.get(item["memory_id"], 0.0)), 4)
            results.append(item)
        results.sort(key=lambda item: (item["score"], item.get("created_at", ""), item["updated_at"]), reverse=True)
        page = results[offset:offset + limit]
        if query:
            self._search_store(cache_key, page)
        return page

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        with self._reading() as conn:
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (_short(memory_id, 80),)).fetchone()
        return self._row_dict(row) if row is not None else None
