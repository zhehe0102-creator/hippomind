"""实体倒排索引：把 claim 的 subject / slot_key 变成检索的**第三路信号**。

背景（HEALTH_REVIEW 第四节缺口 2，对照 mem0 v3 的 entity linking）
------------------------------------------------------------------
HippoMind 的检索原本是两路候选生成（词法 FTS/LIKE + 神经向量），实体只用于
**事后**图扩展。mem0 的做法是 entity 也参与检索打分。本模块补上那个缺失的
候选来源。

关键判断：**不需要 NER**。``memory_claims.subject`` 已经是人工/模型抽好的实体
名（生产库 1,184 个不同 subject），``slot_key`` 是同一件事的机器可读槽位
（2,573 个）。这是本系统相对通用 RAG 的结构优势——实体名是既有的、已落库的、
有证据的，不是现场猜的。

设计契约（每条都有测试锁定，改之前先读 tests/test_entity_channel.py）
--------------------------------------------------------------------

* **零硬依赖、零异常**：账本读失败 / 表缺失 / 任何异常 → 工厂函数返回 None，
  调用方（ledger.search）静默退回纯词法路径。检索不能因为可选组件而失败。
* **不落盘、每次从账本重建。** 向量索引要落盘是因为全量 encode 实测 56-86s；
  实体倒排只是两条 SQL + 内存字典（生产 4,771 条 claim，实测 < 20ms）。
  刻意不持久化：一份**便宜的**派生索引如果落盘，唯一的实际后果是「索引比账本
  旧」——那是正确性风险，不是性能收益。可重建性就是它的持久化方案。
* **与 FTS/向量覆盖同一个记忆集合**（复用 ``schema.fts_indexed_predicate``）。
  三条候选生成路径必须看见同一批行，否则「某条路径能找到、另一条找不到」
  会让 A/B 对比失去意义。
* **实体名归一**：小写 + 折叠内部空白。不做别名合并（生产级别名是另一个
  任务）；大小写不同的同名实体天然合并，因为它们归一到同一个 key。
* **短实体丢弃**：纯 ASCII 实体要求 >= 3 字符（否则 "ai" 会命中全库），CJK
  实体要求 >= 2 字。这不是精度调优，是防止倒排退化成「全库都是候选」。
* **查询侧匹配按脚本分两种规则**（实测踩过的坑，改之前先看）
  「ADB 设备连接排查怎么做」必须命中实体「ADB 设备连接排查」——中文没有
  词边界，子串匹配是唯一正确的做法。但 ASCII 实体**必须**按词边界匹配：
  无边界子串会让实体 anti 命中查询 quantization、实体 mac 命中 macabre，
  而这类假命中会把完全无关的记忆拉进候选集（在 semantic 集上实测到过）。
  规则：含 CJK 的实体用子串；纯 ASCII 实体用
  (?<![0-9a-z])实体(?![0-9a-z]) 的词边界匹配（因此 mac 仍能命中
  mac-mini / macOS，那正是期望行为）。
* **打分用 idf，不用命中个数**：命中的实体越稀有越有判别力。HippoMind
  出现在几百条记忆里，NeoZygisk 只有几条——后者才是真正的定位信号。

生产实测（2026-10-25，只读快照，2,815 条可索引 claim / 1,278 条有 claim 的记忆）
--------------------------------------------------------------------------------

**机器 id 过滤是必须的**。第一版没有过滤，倒排 8,836 个实体里 4,930 个
（55.8%）是 UUID / 长十六进制 / UUID 碎片；最大的倒排项 `digest` 覆盖 593 条
记忆（46%），命中等同于「随便给一批」。加过滤后：

| | 过滤前 | 过滤后 |
| --- | --- | --- |
| 实体数 | 8,836 | **2,490** |
| 覆盖记忆 | 1,274 | **1,130** |
| 机器 id 实体 | 4,930 (55.8%) | **0** |
| 有实体信号的 claim | 2,815 | **1,687** |

**覆盖率必须说清楚**：2,989 条 accepted+active 记忆里，实体通道能触达 1,130
条 = **37.8%**；剩下 1,859 条（62.2%）完全没有实体信号（其中 1,128 条 claim
的 subject 与 slot_key **同时**是机器 id，主要是 raw_chat 提炼出来的行）。
对那 62.2% 的记忆，实体通道完全无效——它只能让已有 claim 的记忆更容易被
命中，不能凭空给没有 claim 的记忆造一个实体。

**subject 不分词、slot_key 分词**。这个不对称是实测结论：subject 分词会多出
168 个实体（`omni` / `com` / `chat` 这类到处出现的修饰词），两个查询集 recall
不变而 keyword MRR 略降（0.7810 -> 0.7774）；slot_key 分词是中文查询命中
英文槽位的唯一入口（`storage_backend` -> `storage`），不能砍。

**DF 上限 30% + 绝对下限 8**。`status` 覆盖 130 条、`task` 101 条，在 1,130 条
记忆的库上仍有判别力；但小账本上按比例算上限会归零（2 条记忆 -> 上限 0），
所以有 MIN_ENTITY_DF_CEILING=8 兜底。

复现这两个数字（2026-10-25 实测，实体通道 task-3）
------------------------------------------------

**必须用同一份账本快照跑两臂**，否则 daemon 的写入会让两次运行不可比。
生产库是 append-only 且 daemon 一直在写，53 条查询里差一两条就动 0.02。

```bash
# 1) 取一份一致的只读快照（SQLite backup API，WAL 安全）
python3 - <<'PY'
import sqlite3, os, time
src = os.path.expanduser("~/Library/Application Support/HippoMind/hippomind.sqlite")
dst = "/tmp/eval-snapshot-%s.sqlite" % time.strftime("%Y%m%dT%H%M%S")
s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
d = sqlite3.connect(dst)
s.backup(d)
d.close(); s.close()
print(dst)
PY

# 2) 基线：实体通道关闭（默认），向量粒度用 memory（= eval_memory.py 的配置）
python3 scripts/eval_memory.py --set semantic --ledger /tmp/eval-snapshot-XXX.sqlite --no-gate
#   -> recall@8=0.4906  mrr=0.3367

# 3) 开启实体通道：同一快照、同一向量粒度
HIPPOMIND_ENTITY_CHANNEL=1 python3 scripts/eval_memory.py --set semantic \
    --ledger /tmp/eval-snapshot-XXX.sqlite --no-gate
#   -> recall@8=0.4906  mrr=0.3367   （semantic 集：零变化，方法边界见下）

# keyword 集同样两臂：
python3 scripts/eval_memory.py --set keyword --ledger ... --no-gate   # 0.9348 / 0.8250
HIPPOMIND_ENTITY_CHANNEL=1 python3 scripts/eval_memory.py --set keyword ... # 0.9783 / 0.8354
```

**注意**：`scripts/eval_memory.py` 本身没有 `--entity-channel` 参数，它读的是
`HIPPOMIND_ENTITY_CHANNEL` 环境变量吗？——**不是**。它的 `evaluate()` 只透传
`vector_index`，不读实体开关。上面第 3 步真正生效的前提是 `eval_memory.py`
已把 `entity_index` 接进 `evaluate()`；在写下这段时它还没有（那是另一个任务
的 write scope）。当时的实测是用一个薄封装复用了 `eval_memory.py` 的
`validate_queries` / `score_hits` / `percentile` / 查询集，只替换每条调用，
并已交叉验证与 `eval_memory.evaluate()` 逐条一致（recall 0.4906 / mrr 0.3367，
无一条 relevant 判断不同）。要让上面第 3 步的命令直接可用，需要给
`eval_memory.evaluate()` 加一个 `entity_index=None` 参数并透传。

**为什么 semantic 集零变化**（这是结论，不是缺陷）：semantic 集测的是「换个
说法问同一件事」，而实体通道的匹配前提是**查询里出现实体名本身**。改写式查询
按定义不写实体名，所以实体通道对它天然无效。实测佐证：8/53 条 semantic 查询
确实触发了实体通道（score_parts 出现 entity_rank），最大 |score delta| 0.0103，
但没有任何一条把 ground truth 从 top-8 外拉进来。keyword 集则相反——查询里
直接写组件名/项目名，实体通道 gained 2 lost 0。

**不要把 latency 数字当作收益**：两臂跑在同一进程，第二臂命中 OS 页缓存与
SQLite 页缓存，p50 会看起来降了 30%+。那是测量假象。

**默认关闭的理由不是性能**：`EntityIndex.build()` 暖态实测 30ms（生产 2,815 条
claim / 2,490 个实体），构建成本可以忽略。默认关闭的唯一理由是**行为等价性**
——既有测试钉住了 `score_parts` 的键集合与 `channel_count`，改默认值属于改变
生产检索行为，必须单独评估。
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .schema import fts_indexed_predicate

#: 参与检索的实体数上限。超过它只做 token 精确匹配（不做子串扫描），
#: 因为「对每个实体做一次子串检查」是 O(实体数 x 查询长度) 的。
#: 生产库 1,184 subject + 2,573 slot_key，离这个上限很远；它只是防病态库。
MAX_SCAN_ENTITIES = 200_000

#: 查询侧默认取多少条实体命中记忆参与 RRF 融合。与向量通道同量级：
#: 通道太窄则融合没贡献，太宽则把噪声灌进候选集。
DEFAULT_ENTITY_TOPN = 96

#: 纯 ASCII 实体的最小长度。2 字符的 ASCII 实体（ai/ok/v1）几乎必然是全库
#: 噪声源，参与倒排只会把候选集撑爆。
MIN_ASCII_ENTITY_CHARS = 3
#: CJK 实体的最小长度：两个字已经足够指代一个专有名词（端口/部署）。
MIN_CJK_ENTITY_CHARS = 2

_SLOT_SPLIT = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")


def normalise_entity(value: Any) -> str:
    """实体名归一：小写 + 折叠空白。空/非字符串一律返回空串。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"\s+", " ", text)


def is_indexable_entity(value: Any) -> bool:
    """短实体不进倒排（见模块 docstring 的「短实体丢弃」）。"""
    text = normalise_entity(value)
    if not text or is_machine_id(text):
        return False
    if re.fullmatch(r"[0-9a-z_./\-]+", text):
        return len(text) >= MIN_ASCII_ENTITY_CHARS
    return len(text) >= MIN_CJK_ENTITY_CHARS


def entity_terms(subject: Any, slot_key: Any) -> List[str]:
    """一条 claim 贡献的实体词。

    **subject 整串保留、不分词；slot_key 整串 + 分词都收。**

    这个不对称是实测结论，不是随手写的：

    * subject 是人/模型写好的实体名（RelayDesk Omni、NeoZygisk）。整串才是
      真正的实体；拆开会产出 RelayDesk + Omni，其中一半是「Omni」这种到处
      都能出现的修饰词。实测：subject 不分词会少 168 个实体，两个查询集的
      recall 不变而 keyword MRR 略升（0.7774 -> 0.7810）。
    * slot_key 是机器可读槽位（storage_backend、core_capabilities），中文查询
      几乎不可能整串命中，必须靠分词后的 storage / backend 才能触达。这是
      实体通道对中文查询唯一有效的入口，不能砍。

    机器 id（UUID / mem_xxx / 十六进制碎片 / 纯数字）整串丢弃，**且不分词**：
    UUID 按连字符切出来的 9ee8 / 8409 看着像词，实际是纯噪声。
    """
    terms: List[str] = []
    name = normalise_entity(subject)
    if name and not is_machine_id(name) and is_indexable_entity(name):
        terms.append(name)
    slot = normalise_entity(slot_key)
    if slot and not is_machine_id(slot):
        if is_indexable_entity(slot):
            terms.append(slot)
        for piece in _SLOT_SPLIT.split(slot):
            if piece and is_indexable_entity(piece) and piece not in terms:
                terms.append(piece)
    return terms

def _claim_rows(ledger: Any) -> List[Tuple[str, str, str]]:
    """应当被索引的 claim：state 为 active 且所属记忆 accepted+active。

    与 vector_index._claim_rows 同一集合，判定复用
    schema.fts_indexed_predicate（字面量只允许出现在 schema.py，
    tests/test_fts_invariant.py 逐字扫描 hippomind/）。
    """
    query = ("SELECT c.memory_id, c.subject, c.slot_key "
             "FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id "
             "WHERE c.state='active' AND " + fts_indexed_predicate("m"))
    try:
        with ledger._reading() as conn:
            rows = conn.execute(query).fetchall()
    except Exception:
        return []
    out: List[Tuple[str, str, str]] = []
    for row in rows:
        try:
            memory_id = str(row["memory_id"])
            subject = row["subject"]
            slot_key = row["slot_key"]
        except (KeyError, IndexError, TypeError):
            continue
        if memory_id:
            out.append((memory_id, subject, slot_key))
    return out


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
#: 机器 id 的形态。这些**不是实体名**，建进倒排只会制造噪声：生产库实测
#: 倒排 8,836 个实体里有 4,930 个（55.8%）是机器 id 或其碎片。
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PREFIX_ID_RE = re.compile(r"^(mem|evt|rel|cl|sha|cf)[_-]")
#: 纯十六进制 >= 4 位。4 是刻意的下限：`9ee8` / `8409` / `1922574a` 都是
#: UUID 按连字符切出来的碎片，看着像词，实际判别力为零（而且能被任何含该
#: 片段的查询命中）。真正的实体名几乎不会是纯十六进制。
_HEX_ID_RE = re.compile(r"^[0-9a-f]{4,}$")
_DIGITS_RE = re.compile(r"^[0-9]+$")


def is_machine_id(value: Any) -> bool:
    """该字符串是不是机器生成的标识符（UUID / mem_xxx / 十六进制碎片 / 纯数字）。

    纯数字也算：subject 里的 0123 是编号不是实体名，而且任何含数字的查询
    （版本号、端口、行号）都能命中它，判别力为零。
    """
    text = str(value or "").strip().lower()
    if not text:
        return False
    return bool(_UUID_RE.match(text) or _PREFIX_ID_RE.match(text)
                or _HEX_ID_RE.match(text) or _DIGITS_RE.match(text))


#: 高文档频率上限：df / N 超过它就不进倒排。
#: `digest` 覆盖 46% 的可索引记忆——一个词出现在半个库里，命中等同于
#: 「随便给一批」。idf 会把它压到接近 0，但更彻底的做法是**根本不生成
#: 候选**：它带来的只有噪声候选和一次多余的 SQL 重放。
MAX_ENTITY_DF_RATIO = 0.30
#: DF 上限的绝对下限：小库上比例没有意义。2 条记忆的库按 30% 算上限是 0，
#: 会把所有实体都删掉（测试与真实的小账本都踩到过）。
MIN_ENTITY_DF_CEILING = 8


_boundary_cache: Dict[str, Any] = {}
_boundary_lock = threading.Lock()


def _matches_in_text(entity: str, text: str) -> bool:
    """实体名是否作为**独立的词**出现在文本里。

    含 CJK 的实体：直接子串匹配。中文没有词边界，子串就是唯一正确的判定
    （「ADB 设备连接排查」出现在「ADB 设备连接排查怎么做」里）。

    纯 ASCII 实体：按词边界匹配。无边界子串是错的，而且错得很隐蔽——实测
    实体 anti 会命中 quantization、mac 会命中 macabre，把完全无关的记忆拉进
    候选集。词边界用 (?<![0-9a-z]) ... (?![0-9a-z])，因此 mac 仍能命中
    mac-mini / macOS / mac_mini（那些都是期望行为）。
    """
    if _CJK_RE.search(entity):
        return entity in text
    with _boundary_lock:
        pattern = _boundary_cache.get(entity)
        if pattern is None:
            pattern = re.compile(r"(?<![0-9a-z])" + re.escape(entity)
                                 + r"(?![0-9a-z])")
            if len(_boundary_cache) < 50_000:
                _boundary_cache[entity] = pattern
    return pattern.search(text) is not None

class EntityIndex:
    """实体 -> memory_ids 倒排 + idf 打分。

    数据结构刻意用两个普通 dict，不用 numpy：这是几千到几万量级的映射，
    dict 的构建与查找比矩阵更省内存也更快，而且没有 numpy 依赖——
    「实体通道在没装 numpy 的机器上也能用」是它相对向量通道的一个真实优势。
    """

    def __init__(self, postings: Mapping[str, Sequence[str]], *,
                 display: Optional[Mapping[str, str]] = None,
                 built_at: float = 0.0, claims: int = 0):
        self.postings: Dict[str, Tuple[str, ...]] = {
            str(entity): tuple(dict.fromkeys(str(mid) for mid in memory_ids))
            for entity, memory_ids in postings.items() if memory_ids}
        self.display: Dict[str, str] = {
            str(key): str(value) for key, value in (display or {}).items()}
        self.built_at = float(built_at or time.time())
        self.claims = int(claims or 0)
        self._memories = len({mid for ids in self.postings.values() for mid in ids})
        self._lock = threading.RLock()
        # idf 预计算：查询侧每次都要用，避免逐次取 log。
        total = max(1, self._memories)
        self._idf: Dict[str, float] = {
            entity: math.log(1.0 + total / max(1, len(ids)))
            for entity, ids in self.postings.items()}

    # -- 元信息 -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self.postings)

    @property
    def granularity(self) -> str:
        return "entity"

    def stats(self) -> Dict[str, Any]:
        return {"granularity": "entity",
                "entities": len(self.postings),
                "memories": self._memories,
                "claims": self.claims,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                          time.localtime(self.built_at)),
                "path": ""}

    # -- 查询侧 -----------------------------------------------------------
    def match_entities(self, query: str,
                       *, limit: int = 32) -> List[Tuple[str, float]]:
        """查询里出现的实体：[(entity, idf)]，按 idf 降序。

        匹配规则见模块 docstring：>= 3 字符的实体做子串匹配（中文查询里实体
        往往被上下文包着），更短的只做精确 token 匹配（子串匹配短实体等于
        噪声）。命中数有界，避免一个超长查询把所有实体都算进来。
        """
        text = normalise_entity(query)
        if not text:
            return []
        tokens = set()
        for piece in _SLOT_SPLIT.split(text):
            if piece:
                tokens.add(piece)
        for piece in re.findall(r"[\u3400-\u9fff]{2,}", text):
            tokens.add(piece)
        scan = len(self.postings) <= MAX_SCAN_ENTITIES
        hits: List[Tuple[str, float]] = []
        with self._lock:
            for entity in self.postings:
                if entity in tokens:
                    hits.append((entity, self._idf.get(entity, 0.0)))
                    continue
                if not scan:
                    continue
                min_len = (MIN_CJK_ENTITY_CHARS if _CJK_RE.search(entity)
                           else MIN_ASCII_ENTITY_CHARS)
                if len(entity) < min_len:
                    continue
                if _matches_in_text(entity, text):
                    hits.append((entity, self._idf.get(entity, 0.0)))
        hits.sort(key=lambda item: (-item[1], item[0]))
        return hits[:max(1, int(limit))]

    def search_text(self, query: str,
                    topn: int = DEFAULT_ENTITY_TOPN
                    ) -> List[Tuple[str, float, str]]:
        """实体命中记忆：[(memory_id, score, entity)]，按 score 降序。

        score = 该记忆命中的全部实体 idf 之和。用和而不是命中个数：
        命中一个稀有实体比命中三个烂大街的词更能定位一条记忆。

        与 ClaimVectorIndex.search_text 同一签名形状（三元组的第三元是
        「哪条实体把这条记忆拉上来的」，供 score_parts 审计），因此
        ledger.search 的两条可选通道可以共用同一套融合代码。
        """
        if not str(query or "").strip() or not self.postings:
            return []
        matches = self.match_entities(query)
        if not matches:
            return []
        scores: Dict[str, float] = {}
        best: Dict[str, Tuple[float, str]] = {}
        for entity, idf in matches:
            for memory_id in self.postings.get(entity, ()):
                scores[memory_id] = scores.get(memory_id, 0.0) + idf
                current = best.get(memory_id)
                if current is None or idf > current[0]:
                    best[memory_id] = (idf, entity)
        if not scores:
            return []
        want = max(1, int(topn or DEFAULT_ENTITY_TOPN))
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:want]
        return [(memory_id, round(score, 6), best[memory_id][1])
                for memory_id, score in ordered]

    # -- 构建 -------------------------------------------------------------
    @classmethod
    def build(cls, ledger: Any) -> "EntityIndex":
        """从账本全量重建（唯一构建入口）。任何异常由调用方兜住。"""
        rows = _claim_rows(ledger)
        postings: Dict[str, List[str]] = {}
        display: Dict[str, str] = {}
        seen_pairs: Dict[str, set] = {}
        for memory_id, subject, slot_key in rows:
            for term in entity_terms(subject, slot_key):
                seen = seen_pairs.setdefault(term, set())
                if memory_id not in seen:
                    seen.add(memory_id)
                    postings.setdefault(term, []).append(memory_id)
                if term not in display:
                    # 展示名保留原始大小写（HippoMind 而不是 hippomind）：
                    # 它进 score_parts 供人读，不是匹配键。
                    for raw in (subject, slot_key):
                        if normalise_entity(raw) == term:
                            display[term] = str(raw).strip()
                            break
                    else:
                        display[term] = term
        # 高文档频率实体不进倒排：按去重后的记忆文档频率计数，避免单记忆多 claim 被误杀。
        total = len({mid for ids in postings.values() for mid in ids}) or 1
        ceiling = max(MIN_ENTITY_DF_CEILING, int(total * MAX_ENTITY_DF_RATIO))
        kept = {entity: ids for entity, ids in postings.items()
                if len(ids) <= ceiling}
        return cls(kept, display=display, claims=len(rows))


#: 进程内索引缓存：key = (账本路径, generation)。ledger.generation 每次写事务
#: 自增，所以缓存天然随账本变化失效——与 ledger 的 search 结果缓存同一机制。
#:
#: **不能用 id(ledger) 做 key**：对象被回收后地址会被复用，新账本可能命中旧
#: 账本的索引（实测：同一进程里连续建账本的测试会间歇性拿到上一个账本的实体）。
#: 路径 + generation 是稳定且足够的身份。
#:
#: 为什么要缓存：注入路径是**每个请求一个进程**时重建只要 30ms（暖态），但在
#: daemon 这种长跑进程里每请求重建 30ms 是纯浪费；而冷启动那一次实测 573ms，
#: 占掉 700ms 注入预算的 80%。
_CACHE: Dict[Tuple[str, int], "EntityIndex"] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 8


def _ledger_key(ledger: Any) -> Tuple[str, int]:
    """账本身份：路径 + 写代数。两者任一变化都必须重建。"""
    try:
        path = str(getattr(ledger, "path", "") or "")
    except Exception:
        path = ""
    try:
        generation = int(getattr(ledger, "generation", 0) or 0)
    except Exception:
        generation = 0
    return (path, generation)


def _cached_build(ledger: Any) -> Optional[EntityIndex]:
    """按 (账本路径, 写代数) 缓存的构建；任何异常返回 None。"""
    key = _ledger_key(ledger)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        index = EntityIndex.build(ledger)
    except Exception:
        return None
    if not len(index):
        return None
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            # 简单淘汰：清空而不是 LRU。账本代数一变旧条目就永远不可能再命中，
            # 留着只是内存泄漏；清空后重建成本也只有几十毫秒。
            _CACHE.clear()
        _CACHE[key] = index
    return index


def load_entity_index(ledger: Any, *, build: bool = True) -> Optional[EntityIndex]:
    """便捷入口：任何异常都返回 None（检索路径不得因可选组件失败而报错）。

    build=False 时直接返回 None——本索引**不落盘**（见模块 docstring），
    没有「只读盘」这个模式。保留这个参数是为了与
    vector_index.load_granular_index 的调用形状一致，让调用方可以用同一段
    代码切换通道。
    """
    if not build:
        return None
    return _cached_build(ledger)


def entity_channel_enabled() -> bool:
    """$HIPPOMIND_ENTITY_CHANNEL 开关（默认**关闭**）。

    默认关是硬要求：开关关闭时 ledger.search 的行为必须与改造前逐字节相同
    （既有测试钉住了 score_parts 的键集合与排序）。打开方式：
    HIPPOMIND_ENTITY_CHANNEL=1（或 on/true/yes/enabled）。
    """
    raw = str(os.environ.get("HIPPOMIND_ENTITY_CHANNEL", "") or "").strip().lower()
    return raw in {"1", "on", "true", "yes", "enabled"}

