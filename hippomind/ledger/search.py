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
from .. import query_expansion

#: RRF 融合常数（Reciprocal Rank Fusion，k=60 是通行取值）。
#: 之前 fts/like 两个通道的 1/(60+rank) 是硬编码字面量；提成常量后默认值
#: 与字面量完全相同（60.0），生产排序逐字节不变（有测试锁定）。
_RRF_K = 60.0

#: 向量通道参与候选生成时，各通道各取多少条做 RRF 融合。
#: 96 来自实测：15 条改写式查询 recall 0.333 -> 0.400，46 条关键词 1.000 -> 0.978。
_DEFAULT_VECTOR_TOPN = 96

#: 实体通道参与候选生成时取多少条记忆。与向量通道同量级（通道太窄则融合
#: 没有贡献，太宽则把噪声灌进候选集）；实体命中本来就比向量稀疏，96 已
#: 覆盖生产库上全部实体命中的绝大多数。
_DEFAULT_ENTITY_TOPN = 96


class SearchMixin:
    """Lexical retrieval: FTS5, LIKE rescue, coverage scoring, ranking, paging."""

    def _search_cached(self, key: Tuple[Any, ...]) -> Optional[List[Dict[str, Any]]]:
        with self._search_lock:
            hit = self._search_cache.get(key)
            if hit is None:
                return None
            stamp, rows = hit
            if time.monotonic() - stamp > self._search_cache_ttl:
                self._search_cache.pop(key, None)
                return None
            snapshot = list(rows)
        return self._copy_rows(snapshot)

    def _search_store(self, key: Tuple[Any, ...], rows: List[Dict[str, Any]]) -> None:
        copied = self._copy_rows(rows)
        with self._search_lock:
            self._search_cache[key] = (time.monotonic(), copied)
            if len(self._search_cache) > 128:
                oldest = min(self._search_cache.items(), key=lambda kv: kv[1][0])[0]
                self._search_cache.pop(oldest, None)

    @staticmethod
    def _clean_terms_query(query: str) -> str:
        return re.sub(r"\b__hm_[a-z0-9_]+\b", " ", str(query or ""), flags=re.I)

    @classmethod
    def _fts_terms(cls, query: str) -> List[str]:
        cleaned = cls._clean_terms_query(query)
        raw_terms = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]{2,}", cleaned.lower())
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
        # [:12] 是**原始查询词**的预算，先按原样裁剪，保证关闭扩展时逐字节不变。
        bounded = seen[:12]
        # 查询扩展（默认关闭）：扩展词**追加在预算之外**。
        #
        # 这里踩过一个坑，别改回去：最初把扩展词 append 进 seen 再一起 [:12]，
        # 结果是长 CJK 查询的滑动窗口已经把 12 个名额吃满，扩展词一个都进不了
        # OR 组——功能写了却永远不生效，而单测只要断言 expand() 有返回值就会
        # "通过"。实测差异：semantic recall 0.6226（被截断）vs 0.7736（不截断）。
        #
        # 扩展词只负责"把行放进候选集"与 coverage 的排序乘数，不参与
        # score_parts 的可靠性判定（见 _prepare_query_coverage 的说明）。
        for term in (query_expansion.expansion_terms(query) if query else []):
            if term not in bounded:
                bounded.append(term)
        return bounded

    @classmethod
    def _expansion_terms(cls, query: str) -> List[str]:
        """Strongest entity words for the sparse-candidate expansion pass."""

        query = cls._clean_terms_query(query).lower()
        words = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]{2,}", query)
        unique: List[str] = []
        for word in sorted(set(words), key=len, reverse=True):
            if word not in unique:
                unique.append(word)
        return unique[:6]

    @classmethod
    def _bigram_terms(cls, query: str) -> List[str]:
        """查询 CJK 段的二元碎片：最后回退通道专用（仅共享短词也能触达）。"""

        text = cls._clean_terms_query(query).lower()
        segments = re.findall(r"[\u3400-\u9fff]{2,}", text)
        terms: List[str] = []
        for segment in segments:
            if segment in LOW_SIGNAL_CJK_QUERY_TERMS:
                continue
            for index in range(len(segment) - 1):
                gram = segment[index:index + 2]
                if gram not in terms and gram not in LOW_SIGNAL_CJK_QUERY_TERMS:
                    terms.append(gram)
        if not terms and len(segments) >= 2:
            for segment in segments:
                for index in range(max(1, len(segment) - 1)):
                    gram = segment[index:index + 2] if len(segment) > 2 else segment
                    if gram not in terms:
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

    @classmethod
    def _like_terms(cls, query: str) -> List[str]:
        """Return bounded phrase fragments for the non-FTS CJK candidate path."""

        cleaned = cls._clean_terms_query(query).lower()
        raw_ascii = re.findall(r"[a-z0-9_]{2,}", cleaned)
        terms: List[str] = [t for t in raw_ascii if t not in LOW_SIGNAL_QUERY_TERMS]
        cjk_segments = re.findall(r"[\u3400-\u9fff]{2,}", cleaned)
        kept_cjk = [s for s in cjk_segments if s not in LOW_SIGNAL_CJK_QUERY_TERMS]
        if not terms and not kept_cjk:
            # 双低信号词保底，与 _fts_terms 保持一致（如「修复 问题」）。
            if len(raw_ascii) + len(cjk_segments) >= 2:
                terms = list(raw_ascii)
                kept_cjk = list(cjk_segments)
        for segment in kept_cjk:
            if len(segment) <= 4:
                terms.append(segment)
                continue
            # 先取最具判别力的 3-4 字滑窗，再保留整段，防止长句前面的 6-8 字碎片占满 [:8] 预算。
            terms.append(segment)
            for size in (3, 4):
                for index in range(len(segment) - size + 1):
                    terms.append(segment[index:index + size])
        seen: List[str] = []
        for term in terms:
            if term and term not in seen:
                seen.append(term)
        bounded = seen[:8]
        # 追加查询扩展词（尤其是 <3 字的中文同义词在 trigram FTS 无法命中，必须靠 LIKE）。
        for term in (query_expansion.expansion_terms(query) if query else []):
            lowered = term.lower()
            if lowered and lowered not in bounded:
                bounded.append(lowered)
        return bounded

    @staticmethod
    def _like_match_group(terms: Sequence[str], *, include_structured: bool = True) -> Tuple[str, List[object]]:
        """构造多字段 LIKE 匹配谓词及对应绑定参数（内容 + 结构化上下文 + task/project）。"""
        parts: List[str] = []
        like_params: List[object] = []
        for term in terms:
            pattern = "%" + str(term).lower() + "%"
            if include_structured:
                parts.append(
                    "(LOWER(m.content) LIKE ? OR LOWER(COALESCE(json_extract(m.metadata_json, '$.context'), '')) LIKE ? "
                    "OR LOWER(m.task_id) LIKE ? OR LOWER(m.project) LIKE ?)"
                )
                like_params.extend([pattern, pattern, pattern, pattern])
            else:
                parts.append("LOWER(m.content) LIKE ?")
                like_params.append(pattern)
        return "(" + " OR ".join(parts) + ")", like_params

    def _vector_candidates(self, query: str, vector_index, clauses: Sequence[str],
                           params: Sequence[object], *, topn: int = _DEFAULT_VECTOR_TOPN,
                           include_cold: bool = True, as_of: str = ""
                           ) -> Tuple[Dict[str, int], Dict[str, sqlite3.Row],
                                      Dict[str, float], Dict[str, str]]:
        """神经向量候选通道：返回 (id->名次, id->账本行, id->余弦, id->claim)。

        支持两种索引粒度（memory 级 VectorIndex / claim 级 ClaimVectorIndex）：
        两者都实现 `search_text(query, topn) -> [(memory_id, cosine, ...)]`，
        即**记忆级去重已经在索引里做完**，本方法只做 SQL 过滤重放与名次紧凑化。
        claim 级条目多一个第三元 `claim_id`，这里顺手记进 `claim_of`，供
        `score_parts["vec_claim"]` 审计"是哪条 claim 把这条记忆拉上来的"。

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
            return {}, {}, {}, {}
        try:
            fetch = max(1, int(topn)) * 4
            hits = vector_index.search_text(query, topn=fetch)
        except Exception:
            return {}, {}, {}, {}
        if not hits:
            return {}, {}, {}, {}
        ranked: List[Tuple[str, int]] = []
        # 余弦值必须一起带出来：下游的相关性门控（context_pack）要判断
        # "这条是可靠的语义命中，还是仅仅话题相近"，只看名次不够。
        similarity: Dict[str, float] = {}
        # claim 级索引的条目是 (memory_id, cosine, claim_id)：第三个元素只用于
        # 审计，不进排序。memory 级索引没有它，这里是可选的。
        claim_of: Dict[str, str] = {}
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
                try:
                    claim_id = str(entry[2])
                except (IndexError, TypeError):
                    claim_id = ""
                if claim_id:
                    claim_of[memory_id] = claim_id
        if not ranked:
            return {}, {}, {}, {}
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
            return {}, {}, {}, {}
        if not rows:
            return {}, {}, {}, {}
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
        return (order, kept,
                {memory_id: similarity.get(memory_id, 0.0) for memory_id in order},
                {memory_id: claim_of[memory_id] for memory_id in order
                 if memory_id in claim_of})


    def _entity_candidates(self, query: str, entity_index, clauses: Sequence[str],
                           params: Sequence[object], *,
                           topn: int = _DEFAULT_ENTITY_TOPN
                           ) -> Tuple[Dict[str, int], Dict[str, sqlite3.Row],
                                      Dict[str, float], Dict[str, str]]:
        """实体候选通道（第三路，默认关闭）：返回 (id->名次, id->行, id->分数, id->实体)。

        与 :meth:`_vector_candidates` 完全同形：索引侧已经做完记忆级去重
        （``EntityIndex.search_text`` 返回 ``(memory_id, score, entity)``），
        这里只做**过滤重放**与名次紧凑化，第三元进 ``score_parts["entity"]``
        供审计「是哪条实体把这条记忆拉上来的」。

        ## 为什么过滤必须在 SQL 里重放

        实体倒排覆盖全库 accepted+active，而本次检索可能带 scope / project /
        task / 时间窗口 / raw_chat 过滤。只按 memory_id 取行而不重放 clauses，
        会让跨 scope 的记忆悄悄进入结果——那是越权召回，不是「召回率提升」。

        ## 候选生成是「多条通路」的集合——这里最容易踩的坑

        检索的候选集由**若干条各自独立生成**的通路合并而成：FTS、短语 LIKE、
        有界 LIKE 救援、2 字回退、向量、以及这里的实体。任何「放宽过滤条件」
        的改动，如果**不同时**扩候选生成，就不会生效——因为被放宽的行从来
        没有进入候选集。

        这不是假设，是本仓库真实踩过的坑：``include_cold=True`` 只放宽了 SQL 的
        ``m.lifecycle IN ('active','cold')``，但 maintain 会把 cold 行从
        ``memory_fts`` 删掉，而 FTS 是候选生成的主通路——于是 cold 行只有在
        热行完全没命中时才可达。同一形态的另一个实例是 ``as_of``：它的解法
        不是继续放宽 clauses，而是让 as_of 查询**绕开**热索引、改由有界 LIKE
        扫描生成候选（见 ``if terms and not as_of`` 那一段的注释）。

        结论：动这里之前先问一句——「这条新候选通路真的把行放进 candidate_rows
        了吗，还是只改了 WHERE？」

        ## 永不抛异常

        索引/账本任何失败都返回空，调用方继续走既有通道。检索不能因为可选
        组件而失败。
        """
        if entity_index is None or not str(query or "").strip():
            return {}, {}, {}, {}
        try:
            hits = entity_index.search_text(query, topn=max(1, int(topn)) * 4)
        except Exception:
            return {}, {}, {}, {}
        if not hits:
            return {}, {}, {}, {}
        ranked: List[Tuple[str, int]] = []
        score_of: Dict[str, float] = {}
        entity_of: Dict[str, str] = {}
        seen = set()
        for position, entry in enumerate(hits):
            try:
                memory_id = str(entry[0])
            except (IndexError, TypeError):
                continue
            if not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            ranked.append((memory_id, position))
            try:
                score_of[memory_id] = float(entry[1])
            except (IndexError, TypeError, ValueError):
                score_of[memory_id] = 0.0
            try:
                entity_of[memory_id] = str(entry[2])
            except (IndexError, TypeError):
                pass
        if not ranked:
            return {}, {}, {}, {}
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
            return {}, {}, {}, {}
        if not rows:
            return {}, {}, {}, {}
        # 名次按「过滤后仍然存在」重新紧凑编号：否则被过滤掉的 top 行会留下空洞，
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
        return (order, kept,
                {memory_id: score_of.get(memory_id, 0.0) for memory_id in order},
                {memory_id: entity_of[memory_id] for memory_id in order
                 if memory_id in entity_of})

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
        """查询侧分析只做一次（多行评分循环外提升，前景轮九性能优化）。

        查询扩展（query_expansion，默认关闭）在这里接入，并且**只影响排序**：

        terms / phrase_terms 是**排序**用的覆盖率词表，扩展词追加在其后；
        gate_terms / gate_phrase_terms 是**原始**词表，只用于写回
        score_parts["coverage"] 与 ["phrase"]。

        为什么必须分开（这是本仓库真实的安全边界，不是洁癖）：
        context_pack._passes_relevance_gate 判 fts>0 or coverage>=0.6，
        ContextPackBuilder._reliable 判 phrase or coverage>=0.35 or ...。
        如果把扩展词直接算进 score_parts 的 coverage，那么一张写坏的别名表
        （把「天气」映射到「系统」之类）就能让无关提问**越过注入门控**——那
        不是召回提升，是把「宁可少注入，也不注入错的」这条约束拆掉了。
        向量通道已经踩过同一个坑：把「向量名次<20」当相关性证据后，
        「今天天气怎么样」「1+1等于几」立刻开始被注入。

        因此：扩展词只让**本该被召回的行**在候选池里排上来，不让它变成
        新的「可靠性证据」。
        """

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
        gate_terms = list(terms)
        gate_phrase_terms = list(phrase_terms)
        # 查询扩展（默认关闭）：只追加到**排序**用词表，门控词表保持原样。
        # 关闭时 expansion 恒为 []，返回结构与改造前逐字节相同。
        expansion = query_expansion.expansion_terms(query) if query else []
        if expansion:
            for term in expansion:
                lowered_term = term.lower()
                if lowered_term not in terms:
                    terms.append(lowered_term)
                if lowered_term not in phrase_terms:
                    phrase_terms.append(lowered_term)
        return {"terms": terms, "phrase_terms": phrase_terms,
                "gate_terms": gate_terms,
                "gate_phrase_terms": gate_phrase_terms,
                "expansion": expansion}

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
               rrf_k: float = _RRF_K, entity_index=None,
               entity_topn: int = _DEFAULT_ENTITY_TOPN) -> List[Dict[str, Any]]:
        started = _time_mod.perf_counter()
        try:
            return self._search_impl(
                query, scope=scope, task_id=task_id, project=project,
                source_agents=source_agents, roles=roles, from_time=from_time,
                to_time=to_time, include_raw_chat=include_raw_chat,
                require_evidence=require_evidence, include_cold=include_cold,
                limit=limit, offset=offset, embedder=embedder, as_of=as_of,
                vector_index=vector_index, vector_topn=vector_topn, rrf_k=rrf_k,
                entity_index=entity_index, entity_topn=entity_topn)
        finally:
            self._record_search_latency(_time_mod.perf_counter() - started)

    def _search_impl(self, query: str, *, scope: str = "shared", task_id: str = "",
                     project: str = "", source_agents: Optional[Sequence[str]] = None,
                     roles: Optional[Sequence[str]] = None, from_time: str = "", to_time: str = "",
                     include_raw_chat: bool = False, require_evidence: bool = True,
                     include_cold: bool = True, limit: int = 80, offset: int = 0,
                     embedder=None, as_of: str = "",
                     vector_index=None, vector_topn: int = _DEFAULT_VECTOR_TOPN,
                     rrf_k: float = _RRF_K, entity_index=None,
                     entity_topn: int = _DEFAULT_ENTITY_TOPN
                     ) -> List[Dict[str, Any]]:
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

        entity_index：可选实体倒排（entity_index.EntityIndex），**第三路**候选
        来源。它与向量通道彼此独立：向量回答「意思像不像」，实体回答「说的是
        不是同一个东西」。两者都关闭时行为与改造前逐字节相同（channel_count、
        rrf 公式、score_parts 的键集合全部不变，有测试锁定）。

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
        #
        # as_of 历史切片是唯一的例外，而且只放宽 superseded 一项：
        # 「被后来的事实取代」是**时间**语义（当时成立、后来不成立了），
        # 历史切片必须还能看到它；quarantine（质量不合格）和 conflict
        # （无法裁决）与时间无关，任何时刻都不该进 Context Pack。
        if as_of:
            clauses.append("(m.quality_state='accepted' "
                           "OR m.quality_state='superseded')")
        else:
            clauses.append("m.quality_state='accepted'")
        # 生命周期条件必须是三选一，不能先按 include_cold 加一条、再给
        # as_of 追加一条更宽的：两条 AND 在一起时宽的那条永远不起作用，
        # 历史切片会被窄的那条（'active','cold'）挡死。这个 bug 在 as_of
        # 上线时就存在，只是当时的测试用例没有跑 maintain()，expired 行
        # 的 lifecycle 还停在 'active'，所以一直没暴露。
        if as_of:
            # point-in-time 语义：维护任务按"现在"把过期事实标为 expired，
            # 但其窗口可能包含 as_of 时刻——历史切片必须仍可见。superseded
            # 同理：它按"现在"下线，但 as_of 可能落在它的有效窗口内。
            clauses.append("m.lifecycle IN ('active','cold','expired','superseded')")
        elif include_cold:
            clauses.append("m.lifecycle IN ('active','cold')")
        else:
            clauses.append("m.lifecycle='active'")
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
                     float(rrf_k) if vector_index is not None else None,
                     # 实体通道同样必须进缓存键：cache_key 是位置元组，只能
                     # **追加**在末尾——插入会静默改变所有既有键的语义（旧缓存
                     # 全部失效且不可解释）。漏掉它的后果与向量通道一样：切换
                     # 开关时同一 query 命中上一次的缓存页，A/B 对比直接失真。
                     id(entity_index) if entity_index is not None else None,
                     int(entity_topn) if entity_index is not None else None)
        # 查询扩展开启时**绕开**查询缓存。
        #
        # 为什么不把扩展身份追加进 cache_key：cache_key 是位置元组，
        # tests/test_entity_channel.py 用 captured[0][:-2] 断言「实体字段总是
        # 最后两位、前缀逐字节不变」。追加第三位会打破那条断言，而那个文件
        # 不在本任务的写范围内。
        #
        # 绕开缓存反而更彻底：别名表是人改的文件，任何「键里放 mtime」的方案
        # 都有窗口（改表后旧键仍可能命中）。直接不读缓存，改表立刻生效，
        # 也彻底消除「先跑关闭臂把空页写进缓存、开启臂拿到同一条空页」这个
        # A/B 失真陷阱（向量通道与实体通道都踩过）。
        #
        # 代价只落在 opt-in 的开启路径上：多一次全量候选计算（实测 p50
        # 45ms -> 45ms，缓存本就常在跨 query 时不命中）。默认关闭时
        # expansion_on 恒为 False，本段与改造前逐字节相同。
        expansion_on = query_expansion.enabled()
        cached = (None if expansion_on else self._search_cached(cache_key)) if query else None
        if cached is not None:
            return cached
        terms = self._fts_terms(query)
        fts_scores: Dict[str, float] = {}
        # RRF 融合：记录每条候选在各召回通道中的名次，双通道同时命中的
        # 记忆获得有界加分（比单通道 max 归一化更稳，见混合检索通行做法）。
        fts_order: Dict[str, int] = {}
        like_order: Dict[str, int] = {}
        candidate_rows: List[sqlite3.Row] = []
        # as_of 历史切片跳过热索引。memory_fts 按设计只装「当前可召回」的
        # 集合（维护会把 cold/superseded 行从索引里删掉），历史切片要的是
        # 当时的集合，两者不是一回事。留在 FTS 路径上的后果是实测过的：
        # 败者不在索引里 → 组合查询（A、B 文本都命中）在 as_of=过去 时
        # 直接返回 []，双时态仍然只在 LIKE 回退路径上成立。
        # 跳过后的候选由下面 lexical_empty 分支的有界 LIKE 扫描生成
        # （clauses 已含 as_of 的窗口条件，扫描范围仍受 _LIKE_SCAN_WINDOW
        # 约束）；as_of 是低频历史查询，这个成本可以接受。
        if terms and not as_of:
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
            # as_of 下短语 MATCH 必然查不到东西：热索引只装当前可召回集合，
            # 历史行（superseded/expired/cold）已经被维护删出索引。直接落到
            # 下面的有界 LIKE 扫描，别再浪费一次注定为空的 FTS 查询。
            if phrase_terms and not as_of:
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
            # 普通查询只在「短语 MATCH 落空 + 存在 <3 字短词」时才做表扫描；
            # as_of 没有短语通道可用（见上），所以用完整的 like_terms 兜底。
            # 内层先按 _LIKE_SCAN_WINDOW 界定扫描范围，外层再施加池上限 LIMIT。
            scan_terms = like_terms if as_of else short_terms
            if not like_rows and scan_terms and not candidate_rows:
                like_group, like_params = self._like_match_group(scan_terms)
                pool_limit = min(_LIKE_POOL_CEILING, max(limit + offset, limit * 20, 200))
                with self._reading() as conn:
                    like_rows = conn.execute(
                        "SELECT * FROM ("
                        "SELECT m.* FROM memories m WHERE " + " AND ".join(clauses) +
                        " ORDER BY m.updated_at DESC LIMIT ?"
                        ") AS m WHERE " + like_group + " LIMIT ?",
                        (*params, _LIKE_SCAN_WINDOW, *like_params, pool_limit),
                    ).fetchall()
            present = {row["memory_id"] for row in candidate_rows}
            for position, row in enumerate(like_rows):
                mid = row["memory_id"]
                like_order.setdefault(mid, position)
                if mid not in present:
                    present.add(mid)
                    candidate_rows.append(row)
        # Archived raw chat is deliberately removed from FTS during maintenance.
        # Explicit raw audit must still be complete, so add a scoped LIKE path
        # without changing the default raw_chat=false safety boundary.
        if query and include_raw_chat and terms:
            raw_like = [*clauses, "m.raw_chat=1"]
            raw_like_group, raw_params = self._like_match_group(terms, include_structured=False)
            raw_pool = min(_LIKE_POOL_CEILING, max(limit + offset, limit * 20, 400))
            with self._reading() as conn:
                raw_rows = conn.execute(
                    "SELECT * FROM ("
                    "SELECT m.* FROM memories m WHERE " + " AND ".join(raw_like) +
                    " ORDER BY m.updated_at DESC LIMIT ?"
                    ") AS m WHERE " + raw_like_group + " LIMIT ?",
                    (*params, _RAW_LIKE_SCAN_WINDOW, *raw_params, raw_pool),
                ).fetchall()
            present = {row["memory_id"] for row in candidate_rows}
            candidate_rows.extend(row for row in raw_rows if row["memory_id"] not in present)
        # 冷投影兜底通道（include_cold=True 时）：内层窗口 _LIKE_SCAN_WINDOW，外层池上限。
        if (query and include_cold and like_terms
                and not any(str(row["lifecycle"]) == "cold"
                            for row in candidate_rows)):
            cold_clauses = [*clauses, "m.lifecycle='cold'"]
            cold_group, cold_like_params = self._like_match_group(like_terms)
            cold_pool = min(_LIKE_POOL_CEILING, max(limit + offset, limit * 20, 200))
            try:
                with self._reading() as conn:
                    cold_rows = conn.execute(
                        "SELECT * FROM ("
                        "SELECT m.* FROM memories m WHERE " + " AND ".join(cold_clauses) +
                        " ORDER BY m.updated_at DESC LIMIT ?"
                        ") AS m WHERE " + cold_group + " LIMIT ?",
                        (*params, _LIKE_SCAN_WINDOW, *cold_like_params, cold_pool),
                    ).fetchall()
                present = {row["memory_id"] for row in candidate_rows}
                for row in cold_rows:
                    if row["memory_id"] not in present:
                        candidate_rows.append(row)
            except sqlite3.DatabaseError:
                pass
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
            # CJK 段的二元碎片做最后一次有界尝试；内层使用 _LIKE_SCAN_WINDOW。
            bigram_terms = self._bigram_terms(query)
            if bigram_terms:
                bg_group, bg_params = self._like_match_group(bigram_terms)
                bg_pool = min(_LIKE_POOL_CEILING, max(limit + offset, limit * 8, 100))
                with self._reading() as conn:
                    candidate_rows = conn.execute(
                        "SELECT * FROM ("
                        "SELECT m.* FROM memories m WHERE " + " AND ".join(clauses) +
                        " ORDER BY m.updated_at DESC LIMIT ?"
                        ") AS m WHERE " + bg_group + " LIMIT ?",
                        (*params, _LIKE_SCAN_WINDOW, *bg_params, bg_pool),
                    ).fetchall()
        # 纯词法最后兜底放在向量与实体通道之前：确保「开启向量/实体」不会跳过
        # 词法 LIKE 回退，且当向量/实体召回非空时不会被空 LIKE 误杀清空。
        if query and not candidate_rows:
            terms_like = self._like_terms(query)
            if terms_like:
                fb_group, fb_params = self._like_match_group(terms_like)
                fb_pool = min(5_000, max(limit + offset, limit * 12))
                with self._reading() as conn:
                    candidate_rows = conn.execute(
                        "SELECT * FROM ("
                        "SELECT m.* FROM memories m WHERE " + " AND ".join(clauses) +
                        " ORDER BY m.updated_at DESC LIMIT ?"
                        ") AS m WHERE " + fb_group + " LIMIT ?",
                        (*params, _LIKE_SCAN_WINDOW, *fb_params, fb_pool),
                    ).fetchall()
        vector_order: Dict[str, int] = {}
        vector_rows: Dict[str, sqlite3.Row] = {}
        vector_similarity: Dict[str, float] = {}
        vector_claim: Dict[str, str] = {}
        if query and vector_index is not None:
            (vector_order, vector_rows, vector_similarity,
             vector_claim) = self._vector_candidates(
                query, vector_index, clauses, params,
                topn=vector_topn, include_cold=include_cold, as_of=as_of)
            if vector_rows:
                present = {row["memory_id"] for row in candidate_rows}
                candidate_rows.extend(row for memory_id, row in vector_rows.items()
                                      if memory_id not in present)
        entity_order: Dict[str, int] = {}
        entity_rows: Dict[str, sqlite3.Row] = {}
        entity_score: Dict[str, float] = {}
        entity_name: Dict[str, str] = {}
        if query and entity_index is not None:
            (entity_order, entity_rows, entity_score,
             entity_name) = self._entity_candidates(
                query, entity_index, clauses, params, topn=entity_topn)
            if entity_rows:
                present = {row["memory_id"] for row in candidate_rows}
                candidate_rows.extend(row for memory_id, row in entity_rows.items()
                                      if memory_id not in present)
        if candidate_rows:
            rows = candidate_rows
        elif query:
            return []
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
                # 排序用覆盖率：含扩展词（qprep 已追加）。
                coverage, phrase_hit = self._apply_query_coverage(qprep, corpus)
                # 门控用覆盖率：**只算原始查询词**。扩展词不得成为「这条记忆
                # 可靠」的新证据，否则别名表就变成了注入开关（详见
                # _prepare_query_coverage 的注释）。
                gate_terms = qprep.get("gate_terms")
                if gate_terms is None:
                    gate_coverage, gate_phrase = coverage, phrase_hit
                else:
                    gate_coverage, gate_phrase = self._apply_query_coverage(
                        {"terms": gate_terms,
                         "phrase_terms": qprep.get("gate_phrase_terms") or []},
                        corpus)
            else:
                topic = 0.7
                coverage, phrase_hit = (1.0, True)
                gate_coverage, gate_phrase = 1.0, True
            fts = fts_scores.get(item["memory_id"], 0.0)
            # Rank selects candidates; coverage prevents a single shared CJK
            # character from being promoted to a full-strength FTS hit.
            fts = (fts / max_fts if max_fts else 0.0) * coverage
            # RRF 双通道融合加分：FTS 与 LIKE 各按名次贡献 1/(60+rank)，
            # 归一化后作为小权重信号——两路独立命中的证据更可信。
            # 向量通道加入后归一化分母同步变化：三通道各贡献 1/(k+0)=1/k，
            # 归一化后仍是 0..1，因此既有两通道的相对强弱不被改写。
            # 通道计数只在通道**真的有候选**时增加：否则归一化分母会被一个
            # 空通道抬高，让所有既有通道的 RRF 分数被无端稀释——那等于
            # 「打开了开关但没命中」反而让排序变差。
            channel_count = 2
            if vector_order:
                channel_count += 1
            if entity_order:
                channel_count += 1
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
            position = entity_order.get(item["memory_id"])
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
                                   # coverage / phrase 写**门控**值（只含原始查询词）：
                                   # context_pack 的注入可靠性判定读的就是这两个键，
                                   # 扩展词不得抬高它们。排序用的是含扩展词的
                                   # coverage，它已经乘进 fts 与 score，无需另存。
                                   "coverage": round(gate_coverage, 4),
                                   "phrase": gate_phrase,
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
                    # claim 级索引额外记下命中的 claim：审计"是哪条 claim 把
                    # 这条记忆拉上来的"，也让"粒度到底改了名次"可被逐条核对。
                    claim_id = vector_claim.get(item["memory_id"])
                    if claim_id:
                        item["score_parts"]["vec_claim"] = claim_id
            if entity_order:
                position = entity_order.get(item["memory_id"])
                if position is not None:
                    item["score_parts"]["entity_rank"] = position
                    # 命中的实体名进 score_parts：审计「是哪个实体把这条记忆
                    # 拉上来的」。没有它，实体通道的效果只能靠猜。
                    matched = entity_name.get(item["memory_id"])
                    if matched:
                        item["score_parts"]["entity"] = matched
                    item["score_parts"]["entity_score"] = round(
                        float(entity_score.get(item["memory_id"], 0.0)), 4)
            results.append(item)
        results.sort(key=lambda item: (item["score"], item.get("created_at", ""), item["updated_at"]), reverse=True)
        page = results[offset:offset + limit]
        if query and not expansion_on:
            # 同上：扩展开启时不写缓存，避免把「带扩展的页」留给关闭臂命中。
            self._search_store(cache_key, page)
        return page

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        with self._reading() as conn:
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (_short(memory_id, 80),)).fetchone()
        return self._row_dict(row) if row is not None else None
