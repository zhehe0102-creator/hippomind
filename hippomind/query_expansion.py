"""查询扩展：把「改写式提问」桥接到账本里的术语（别名表）。

背景
----
scripts/eval_memory.py 的 semantic 集测的是「换个说法问同一件事」：

    「记忆里夹带恶意指令怎么防」 -> 期望命中「提示注入防护」
    「数据是怎么存的断电会不会丢」 -> 期望命中「SQLite/WAL」
    「这套系统跑在哪台机器上」   -> 期望命中「mac-mini」

实体通道（entity_index）对这个集合**零提升**，原因是结构性的：实体通道的
匹配前提是「查询里出现实体名本身」，而改写式查询按定义不写实体名。同理，FTS
也拿不到词面重叠。缺的那一环是一张「说法 -> 术语」的映射表。

本模块只做这件事：给定查询，返回可以补进检索的账本术语。**不做 NER、不做
模型推理**——只查一张人可以读、可以改、可以审计的表。

三条设计契约（都有测试锁定）
----------------------------

1. **默认关闭**。HIPPOMIND_QUERY_EXPANSION 非显式开启值时 expand() 恒返回
   空列表，ledger.search 因此与改造前逐字节相同（score_parts 键集合、rrf
   公式、候选集全部不变）。

2. **每个扩展词都能说出理由**。返回值不是 List[str] 而是带 term / reason /
   source 的记录。没有理由的扩展等于给检索灌噪声，而且出了问题无法归因——
   本仓库的实体通道与向量通道都因为「命中来源进 score_parts」这条规矩才可被
   审计。

3. **永不抛异常**。别名表损坏 / 路径不可读 / 任何异常 -> 返回空列表，检索
   继续走纯词法路径。检索不能因为可选组件而失败。

关于「能不能测出提升」——重要，读之前先看
------------------------------------------
**本模块不提供内置别名表，这不是偷懒，是诚实。**

scripts/eval_memory.py 的 semantic 集是这个仓库里「口语说法」的**唯一**来源。
任何人工写的别名表都是读着那 53 条查询写出来的，因此在那个集合上测出的提升
**无法与「抄答案」区分**。实测佐证：作者第一版手工别名表有 38/103 条条目的
值与 expect 词重叠（36.9%），剔除后 semantic recall 从 0.7736 掉到 0.6038；
再剔除 key 本身抄自查询措辞的部分，干净留出集上只剩 0.4783 -> 0.5652，而这
仍然含有同一作者的先验。

要真测这个功能，需要一个**从未见过评测集**的别名来源：真实用户查询日志，或
由没看过 semantic 集的人整理的术语表。本模块把表放在**外部 JSON 文件**里正是
为了这件事——用户按自己的说法填，填出来的表天然与评测集无关。

因此本文件**不提供**内置别名表。docs/SEMANTIC.md 的「为什么我们没有报这个
提升」一节记录了完整的测量与排除过程。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: 环境开关。只有显式真值才开启；其余（含拼错）一律当作关闭。
#: 默认关是硬要求：关闭时 ledger.search 的行为必须与改造前逐字节相同。
ENV_SWITCH = "HIPPOMIND_QUERY_EXPANSION"

#: 单次查询最多补多少个扩展词。
#: 上限的作用不是省算力，而是**限制一条坏条目能造成的破坏范围**：别名表是
#: 人写的，一条过于宽泛的条目（比如把「系统」映射到一堆词）如果无界展开，
#: 会把候选集撑爆并让排序失去意义。8 与 CROSS_LANGUAGE_TERMS 的量级一致。
DEFAULT_MAX_TERMS = 8

#: 别名表文件名（放在 HIPPOMIND_HOME 下，与 injection-ab.json 同级）。
#: 用文件而不是环境变量：真正要跑这个功能的是被 Claude Code / Codex 拉起的
#: hook 进程，登录 shell 里的 export 到不了那里（injection_ab 踩过同一个坑）。
GLOSSARY_FILENAME = "query-expansion.json"

#: 别名表体积上限。别名表是人写的，超过这个量级几乎一定是写错了文件。
MAX_GLOSSARY_BYTES = 1_000_000


def _truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "on", "true", "yes", "enabled"}


def enabled() -> bool:
    """查询扩展是否开启（默认关闭）。

    只认显式真值。空 / 0 / off / 拼错 都等于关闭——一个开关的默认值必须
    是「不改变既有行为」，否则它会变成一次未经评审的生产行为变更。
    """
    return _truthy(os.environ.get(ENV_SWITCH, ""))


def glossary_path() -> Path:
    """别名表位置：$HIPPOMIND_HOME/query-expansion.json。"""
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / GLOSSARY_FILENAME


#: 解析后的别名表缓存。key = (路径, mtime_ns, size)。
#:
#: 为什么按文件元数据而不是按 ledger.generation：别名表与账本无关，它是人改的
#: 文件。用 generation 做键会让「改了表但账本没写」的进程继续用旧表——那正是
#: 用户最困惑的情形（我明明改了）。
_CACHE: Dict[Tuple[str, int, int], "Glossary"] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 4


class Glossary:
    """一份已解析的别名表：有序的 (说法, 术语列表) 条目。"""

    __slots__ = ("entries", "path", "error")

    def __init__(self, entries: List[Tuple[str, List[str]]], *,
                 path: str = "", error: str = ""):
        self.entries = entries
        self.path = path
        self.error = error

    def __len__(self) -> int:
        return len(self.entries)

    def stats(self) -> Dict[str, Any]:
        return {"entries": len(self.entries), "path": self.path,
                "error": self.error}


def _normalise_entry(raw: Any) -> Optional[Tuple[str, List[str]]]:
    """一条别名条目 -> (说法, [术语])；不合法返回 None。

    支持两种写法，都要求「说法」非空：

        {"when": "断电", "add": ["持久化", "wal"]}
        {"断电": ["持久化", "wal"]}          # 简写，一条目一个键

    术语里的空白会被丢弃；重复术语去重并保序（顺序影响 max_terms 裁剪，
    先写的更可信）。
    """
    when = ""
    add: Any = None
    if isinstance(raw, Mapping):
        if "when" in raw or "add" in raw:
            when = str(raw.get("when") or "").strip()
            add = raw.get("add")
        elif len(raw) == 1:
            when, add = next(iter(raw.items()))
            when = str(when or "").strip()
    if not when:
        return None
    if isinstance(add, str):
        add = [add]
    if not isinstance(add, (list, tuple)):
        return None
    terms: List[str] = []
    for item in add:
        term = str(item or "").strip()
        if term and term.lower() not in {t.lower() for t in terms}:
            terms.append(term)
    return (when, terms) if terms else None


def parse_glossary(payload: Any, *, path: str = "") -> Glossary:
    """把 JSON 载荷解析成 Glossary；格式错误 -> 空表 + error（不抛）。"""
    entries: List[Tuple[str, List[str]]] = []
    raw_entries: Any = None
    if isinstance(payload, Mapping):
        raw_entries = payload.get("entries")
        if raw_entries is None:
            # 顶层直接是 {说法: [术语]} 的写法
            raw_entries = [{k: v} for k, v in payload.items()
                           if k not in {"version", "note"}]
    elif isinstance(payload, list):
        raw_entries = payload
    if not isinstance(raw_entries, (list, tuple)):
        return Glossary([], path=path, error="entries must be a list")
    for raw in raw_entries:
        entry = _normalise_entry(raw)
        if entry is not None:
            entries.append(entry)
    return Glossary(entries, path=path)


def load_glossary(path: Optional[os.PathLike] = None) -> Glossary:
    """读取并缓存别名表。文件缺失 / 损坏 / 过大都返回空表（不抛）。

    失败方向必须是「空表」而不是「抛异常」：检索路径上任何可选组件的失败都
    不该让一次搜索变成 500。
    """
    target = Path(path) if path is not None else glossary_path()
    try:
        stat = target.stat()
        key = (str(target), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return Glossary([], path=str(target))
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    if stat.st_size > MAX_GLOSSARY_BYTES:
        glossary = Glossary([], path=str(target),
                            error="glossary larger than %d bytes"
                                  % MAX_GLOSSARY_BYTES)
    else:
        try:
            payload = json.loads(target.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            glossary = Glossary([], path=str(target), error=str(exc))
        else:
            glossary = parse_glossary(payload, path=str(target))
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            # 简单清空而不是 LRU：文件一改旧 key 就永远不可能再命中。
            _CACHE.clear()
        _CACHE[key] = glossary
    return glossary


def reset_cache() -> None:
    """清空别名表缓存（测试与「我改了文件想立刻生效」用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def expand(query: str, *, limit: int = DEFAULT_MAX_TERMS,
           glossary: Optional[Glossary] = None) -> List[Dict[str, str]]:
    """查询 -> 扩展词记录 [{"term", "reason", "source"}]。

    匹配规则：条目的「说法」作为**子串**出现在查询里就触发（中文没有词边界，
    子串是唯一正确的判定；这一点与 entity_index 的 CJK 规则一致）。

    只返回**查询里还没有**的术语：查询已经写了的词再补一遍没有意义，只会
    让 max_terms 的预算被浪费掉。

    关闭时恒返回 []——调用方不需要自己判断开关。
    """
    if not enabled():
        return []
    text = str(query or "")
    if not text.strip():
        return []
    table = glossary if glossary is not None else load_glossary()
    if not len(table):
        return []
    lowered = text.lower()
    out: List[Dict[str, str]] = []
    seen = set()
    budget = max(1, int(limit))
    for when, terms in table.entries:
        needle = when.lower().strip()
        if not needle:
            continue
        if needle.isascii() and len(needle) <= 4:
            if not re.search(r"(?<![a-z0-9_])" + re.escape(needle) + r"(?![a-z0-9_])", lowered):
                continue
        elif needle not in lowered:
            continue
        for term in terms:
            key = term.lower()
            if key in seen or key in lowered:
                continue
            seen.add(key)
            out.append({"term": term,
                        # 理由必须能指回具体条目：出了问题要能删掉那一行。
                        "reason": "alias %r matched query" % when,
                        "source": table.path})
            if len(out) >= budget:
                return out
    return out


def expansion_terms(query: str, *, limit: int = DEFAULT_MAX_TERMS,
                    glossary: Optional[Glossary] = None) -> List[str]:
    """只要词面的便捷入口（保持顺序、去重）。"""
    return [item["term"] for item in expand(query, limit=limit, glossary=glossary)]


def explain(query: str, *, limit: int = DEFAULT_MAX_TERMS) -> Dict[str, Any]:
    """人类可读的审计视图：开关状态、表位置、命中条目与理由。

    给 CLI / 排查用。扩展是**可解释的**这条契约靠它兑现：每个词都能说出
    来自哪条别名条目。
    """
    table = load_glossary()
    items = expand(query, limit=limit, glossary=table)
    return {
        "enabled": enabled(),
        "switch": ENV_SWITCH,
        "glossary": table.stats(),
        "query": str(query or ""),
        "expansions": items,
        "terms": [item["term"] for item in items],
    }

