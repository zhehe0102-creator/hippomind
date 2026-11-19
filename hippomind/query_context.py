"""Session-aware follow-up query disambiguation (WP-M).

用户高频输入「继续 / 再优化 / 同上 / go on」时，检索如果只按这一句话本身
取词，几乎必然召回为空或召回无关历史：那句话没有可用于词法匹配的内容。
这个模块把「当前会话最近的真实任务消息」变成检索锚点，交由
ContextPackBuilder 在严格守卫下使用：

* 只有 follow-up 查询才允许锚定（普通查询路径零额外查询、输出逐字节不变）；
* 有内容词的短查询（如「再优化 GUI」）必须让候选至少命中一个原查询词，
  否则丢弃锚点、用原查询重跑（宁可救不回来，也不能注入无关记忆）；
* 锚点来源限定同 scope、同 session（无 session 时限定时间窗），默认窗口
  180 分钟、最多 2 条真实消息、锚点文本上限 400 字符；
* HIPPOMIND_QUERY_CONTEXT=0/off/false/no 全局关闭；per-request 由
  ContextPackOptions.query_context 关闭。

模块本身不读缓存、不写账本：所有查询都是一次只读的 ledger._reading()
有界查询（LIMIT 30），非 follow-up 路径不会产生任何查询。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

#: 锚点时间窗（分钟）。同 session 命中同样受它约束，避免把半天前的会话
#: 尾巴当作「当前任务」；跨会话兜底路径更是必须靠它收敛。
CONTINUATION_WINDOW_MINUTES = 180
#: 有界扫描的最近用户消息条数。
MAX_ANCHOR_SCAN = 30
#: 参与拼锚点的真实任务消息条数上限。
MAX_ANCHOR_MESSAGES = 2
#: 单条消息 / 整段锚点文本的字符上限。
ANCHOR_MESSAGE_MAX_CHARS = 400
ANCHOR_TEXT_MAX_CHARS = 400
#: 短句判定阈值（strip 后字符数）。
SHORT_QUERY_MAX_CHARS = 16

#: 全局开关；默认开启，0/off/false/no/disabled 关闭。
ENV_SWITCH = "HIPPOMIND_QUERY_CONTEXT"
_OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})

#: DSH/auto_context 追加的工作区提示词后缀标记。
_WORKSPACE_HINT_RE = re.compile(r"(?:\r?\n|\s)+__hm_workspace_hint__\b.*$", re.S | re.I)

# ---------------------------------------------------------------------------
# 延续词表
# ---------------------------------------------------------------------------
# 剥离用：这些词只表达「沿着上一轮继续」，本身不是检索内容。中文多字延续
# 词按子串剥离（长的在前），单字延续助词只允许剥离句首位置（避免在句子
# 中间挖空「偏好 / 运行 / 再现」），英文按词边界剥离。
_CONTENT_STRIP = (
    # 只剥离「延续虚词」本身，不剥离它修饰的实义动词：「再优化 GUI」的
    # content_terms 必须是 {优化, gui}（>=1 即可触发 original-overlap 守卫）。
    # 把「再优化」整词剥掉会只剩 {gui}。
    "再继续", "再接着",
    "继续", "接着", "下一步", "往下", "同上", "如上", "同前",
    "刚才那个", "刚才", "这个", "那个",
    "好的", "好吧", "嗯嗯", "可以", "收到", "明白", "了解",
    "same as above", "as before", "keep going", "go on", "continue",
    "more", "again", "next step", "next", "ok", "okay",
)
_LEADING_SINGLE_STRIP_RE = re.compile(r"^(?:再(?!次|见|三|现|生)|好|嗯|行)(?:\s+|$)")
#: content_terms 的停用词（保留少量高价值实词，避免把请求词一并删掉）。
_STOPWORDS = frozenset({
    "的", "了", "吗", "呢", "吧", "啊", "呀", "哦", "哈",
    "和", "与", "及", "或", "是", "在", "我", "你", "他", "她", "它",
    "我们", "你们", "请", "帮", "帮我", "麻烦", "一下", "一些",
    "什么", "怎么", "如何", "为什么", "就", "也", "都", "还", "又",
    "很", "太", "给", "把", "被", "让", "用", "对", "从", "到", "去",
    "the", "a", "an", "to", "of", "and", "or", "is", "are", "was", "be",
    "it", "this", "that", "these", "those", "please", "can", "could",
    "would", "should", "you", "we", "i", "me", "my", "our", "your",
    "for", "with", "on", "in", "at", "by", "from", "as", "into", "then",
    "just", "also", "do", "does",
})
_STRIP_SORTED = tuple(sorted(_CONTENT_STRIP, key=len, reverse=True))

#: 纯延续：整句只是「继续/同上/好的」之类，没有任务内容。
_PURE_RE = re.compile(
    r"^(?:"
    r"继续|接着|再继续|再接着|继续优化|继续改进|继续完善|继续做|继续弄|"
    r"接着优化|接着做|接着弄|再优化|再改|再弄|再跑|再看|再实现|再完善|"
    r"再检查|再修复|再部署|再试(?:一次|一下)?|"
    r"同上|如上|同前|刚才(?:那个|说的)?|这个|那个|往下|下一步|"
    r"好的|好吧|好|嗯嗯|嗯|行|可以|收到|明白|了解|"
    r"go\s+on|continue|keep\s+going|same\s+as\s+above|as\s+before|"
    r"more|again|next(?:\s+step)?|okay|ok"
    r")(?:吧|啊|呀|了|啦)?[\s\W_]*$",
    re.I,
)

#: 前缀延续：「继续 <内容>」「再优化 <内容>」这类既有延续信号又有真实目标。
#: 单独的「再次/再见/再三」及英文单词前缀（如 Next.js / against）不算延续。
_PREFIX_RE = re.compile(
    r"^(?:"
    r"继续|接着|再继续|再接着|继续优化|接着优化|再优化|再改|再弄|再跑|"
    r"再看|再实现|再完善|再检查|再修复|再部署|再试|下一步|往下|同上|"
    r"再(?!次|见|三|现|生|平衡|分配|保险)|"
    r"(?:continue|keep\s+going|go\s+on|more|next|again)(?![a-z0-9_])"
    r")",
    re.I,
)


def enabled() -> bool:
    """全局开关（默认开启）。非法值按开启处理，关闭必须显式。"""
    return os.environ.get(ENV_SWITCH, "").strip().lower() not in _OFF_VALUES


def anchor_window_minutes() -> int:
    """锚点时间窗（分钟），允许运维用环境变量收紧/放宽。"""
    try:
        value = int(os.environ.get("HIPPOMIND_QUERY_CONTEXT_WINDOW_MINUTES", ""))
    except (TypeError, ValueError):
        return CONTINUATION_WINDOW_MINUTES
    return max(1, min(24 * 60, value))


def strip_workspace_hint(query: object) -> str:
    """剥离 DSH/auto_context 追加的 ``__hm_workspace_hint__`` 后缀。"""
    text = str(query or "")
    if "__hm_workspace_hint__" not in text.lower():
        return text
    return _WORKSPACE_HINT_RE.sub("", text).strip()


def _normalise_query(query: object) -> str:
    text = strip_workspace_hint(query).strip().lower()
    # \W 不包含 CJK（Python3 的 \w 覆盖汉字），这里只剥两端标点/空白。
    text = re.sub(r"^[\W_]+|[\W_]+$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_continuation(text: str) -> str:
    lowered = str(text or "").lower().strip()
    lowered = _LEADING_SINGLE_STRIP_RE.sub(" ", lowered).strip()
    if lowered.startswith("再") and not re.match(r"^再(?:次|见|三|现|生|平衡|分配|保险)", lowered):
        lowered = lowered[1:].strip()
    for phrase in _STRIP_SORTED:
        if phrase.isascii():
            lowered = re.sub(
                r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])",
                " ", lowered)
        else:
            lowered = lowered.replace(phrase, " ")
    return lowered


def content_terms(text: object) -> List[str]:
    """CJK bigram + ASCII 词，剔除延续词/停用词。

    返回去重后的排序列表；它是「这句话自己说了什么」的最小口径，用于
    follow-up 判定与 original-overlap 守卫。规则保持本地实现（不改
    ledger/search.py 的 coverage 词表），但风格与之一致：CJK 取 bigram，
    ASCII 取长度 >=2 的字母数字词。
    """
    cleaned = _strip_continuation(_normalise_query(text))
    terms: List[str] = []
    seen = set()
    for run in re.findall(r"[\u3400-\u9fff]+", cleaned):
        if len(run) == 1:
            candidates = [run]
        else:
            candidates = [run[index:index + 2] for index in range(len(run) - 1)]
        for term in candidates:
            if term and term not in _STOPWORDS and term not in seen:
                seen.add(term)
                terms.append(term)
    for token in re.findall(r"[a-z0-9][a-z0-9_+#.-]*", cleaned):
        term = token.rstrip(".-")
        if len(term) >= 2 and term not in _STOPWORDS and term not in seen:
            seen.add(term)
            terms.append(term)
    return sorted(terms)


def is_followup(query: object) -> bool:
    """这句话是不是「沿着上一轮继续」？

    安全约束（WP-M 第二轮收紧）：命中强延续模式，或 content_terms 为 0 且
    strip 后 <=16 字符。不能把「跑测试 / 部署 / 回滚」这类有 1 个内容词的
    短请求判成 follow-up——它们是独立的新请求，锚定会带入上一轮无关任务。
    """
    text = _normalise_query(query)
    if not text:
        return False
    if _PURE_RE.match(text) or _PREFIX_RE.match(text):
        return True
    if len(text) > SHORT_QUERY_MAX_CHARS:
        return False
    return not content_terms(text)


def is_pure_continuation(text: object) -> bool:
    """会话消息本身是不是纯延续（拼接锚点时应跳过）。"""
    normalised = _normalise_query(text)
    if not normalised:
        return True
    if _PURE_RE.match(normalised):
        return True
    return not content_terms(normalised)


def _coerce_now(now: Any) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        value = now
    elif isinstance(now, (int, float)):
        value = datetime.fromtimestamp(float(now), tz=timezone.utc)
    else:
        try:
            value = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def anchor_context(ledger, query: str, *, scope: str = "shared",
                   session_id: str = "", now: Any = None,
                   scopes: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """最近会话消息 -> 检索锚点（无锚点时返回 None）。

    查询条件：raw_chat=1 AND role='user' AND scope=?，最近 MAX_ANCHOR_SCAN
    条；session_id 非空时按会话过滤，否则只靠时间窗（默认 180 分钟）兜底。
    当调用方显式传入受策略授权的 ``scopes`` 列表且给定 ``session_id`` 时，
    按列表中的 scope 依次查找该会话的锚点（绝不越权跨未授权 scope）。
    纯延续消息（「继续/好的」）不作为锚点文本，但会被跳过继续向前找真实
    任务消息。

    返回 {query, anchor_text, source_memory_ids, source_session_id,
    age_minutes}；query = 锚点文本 + 空格 + 原查询。
    """
    query_text = strip_workspace_hint(query).strip()
    reference = _coerce_now(now)
    cutoff = (reference - timedelta(minutes=anchor_window_minutes())).isoformat()
    scope_text = str(scope or "shared").strip()[:128] or "shared"
    session_text = str(session_id or "").strip()[:256]
    allowed_scopes = [scope_text]
    if scopes and session_text:
        for item in scopes:
            s = str(item or "").strip()[:128]
            if s and s not in allowed_scopes:
                allowed_scopes.append(s)
    scope_marks = ",".join("?" for _ in allowed_scopes)
    clauses = ["raw_chat=1", "role='user'", f"scope IN ({scope_marks})",
               "created_at>=?", "lifecycle IN ('active','cold')"]
    params: List[object] = [*allowed_scopes, cutoff]
    if session_text:
        clauses.append("session_id=?")
        params.append(session_text)
    params.append(MAX_ANCHOR_SCAN)
    sql = ("SELECT memory_id,content,session_id,created_at FROM memories "
           "WHERE " + " AND ".join(clauses) +
           " ORDER BY created_at DESC, memory_id DESC LIMIT ?")
    try:
        with ledger._reading() as conn:  # noqa: SLF001 - read-only bounded scan
            rows = conn.execute(sql, tuple(params)).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    picked: List[Dict[str, Any]] = []
    for row in rows:
        content = " ".join(str(row["content"] or "").split())
        if not content or is_pure_continuation(content):
            continue
        picked.append({
            "memory_id": str(row["memory_id"]),
            "content": content[:ANCHOR_MESSAGE_MAX_CHARS],
            "session_id": str(row["session_id"] or ""),
            "created_at": str(row["created_at"] or ""),
        })
        if len(picked) >= MAX_ANCHOR_MESSAGES:
            break
    if not picked:
        return None
    picked.reverse()  # 时间正序：先看到的上下文，再是本轮
    anchor_text = " ".join(item["content"] for item in picked).strip()
    anchor_text = anchor_text[:ANCHOR_TEXT_MAX_CHARS].strip()
    if not anchor_text:
        return None
    ages: List[float] = []
    for item in picked:
        try:
            created = datetime.fromisoformat(
                item["created_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = (reference - created.astimezone(timezone.utc)).total_seconds()
        ages.append(max(0.0, delta / 60.0))
    return {
        "query": (anchor_text + " " + query_text).strip()[:4_000],
        "anchor_text": anchor_text,
        "source_memory_ids": [item["memory_id"] for item in picked],
        "source_session_id": picked[-1]["session_id"] if picked else "",
        "age_minutes": round(min(ages), 1) if ages else 0.0,
    }


def original_overlap(candidate: Mapping[str, Any], query: str) -> bool:
    """候选是否命中原查询的至少一个 content term（original-overlap 守卫）。

    检查范围：候选正文 + metadata.context 的 goal/progress/key_decisions/
    related_files/blockers/next_steps/preferences。无内容词的纯延续查询
    直接返回 True（守卫不适用）。
    """
    terms = content_terms(query)
    if not terms:
        return True
    texts: List[str] = [str(candidate.get("content", ""))]
    metadata = candidate.get("metadata", {})
    if isinstance(metadata, Mapping):
        context = metadata.get("context", {})
        if isinstance(context, Mapping):
            for value in context.values():
                if isinstance(value, str):
                    texts.append(value)
                elif isinstance(value, (list, tuple)):
                    texts.extend(str(entry) for entry in value)
    blob = " ".join(texts).lower()
    return any(term in blob for term in terms)


def anchor_digest(anchor_text: object) -> str:
    """锚点文本的短摘要，进 Context Pack 缓存键（不暴露正文）。"""
    value = str(anchor_text or "")
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]

