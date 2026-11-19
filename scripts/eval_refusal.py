#!/usr/bin/env python3
"""拒答集评测与门禁：账本里根本没有的东西，检索必须返回空而不是硬凑。

LongMemEval（arXiv 2410.10813）把「拒答」列为长期记忆的五项能力之一。HippoMind
此前只做过 3 条手测（「今天天气」「1+1」），没有任何可复现的度量——本脚本补上这一项。

## 为什么是独立脚本而不是 eval_memory.py 的第三套 query set

eval_memory.py 的两套集度量的是 recall@k / MRR：答案存在时能不能找到。
拒答度量的是相反的问题：**答案不存在时会不会编**。两者没有共同的分母
（拒答集没有 expect，只有 absent_terms），把 leak_rate 塞进 check_gate 的
recall 阈值里只会得到一条永远为 1.0 的假指标。所以指标、阈值、退出码都独立，
只复用 eval_memory.render_hit 的渲染口径与 percentile。

scripts/eval_memory.py --set refusal 会委托到本脚本（eval_memory 里门禁是默认行为，
只有 --no-gate；本脚本反过来，--gate 才判退出码）；--include-refusal 可以把
这一路并进 --set all 的合并门禁。

## 「真的拒答」和「返回了低分噪音」怎么区分

实测（2026-10-25 生产账本）：「今天天气怎么样」返回 5 条记忆，最高分 0.394——
**高于** context_pack.MIN_RELIABLE_SCORE（0.22），但 fts=0.000 / coverage=0.167，
词面证据为零。这 0.39 分全部来自 recency/touch/weight 这类与查询无关的项。
所以「分数低于某个阈值就算拒答」是**错的规则**：无关查询的分数地板就有约 0.37。

真正决定「会不会被注入」的是生产自动注入路径的那道门：
context_pack._passes_relevance_gate(candidate, min_score) —— 先过 min_score
（默认 0.20，与 auto_context / daemon 的 /context-pack 一致），再要求
fts > 0 或 coverage >= 0.6。因此每条查询的判定分三档：

    empty    ledger.search 一条都没返回——最干净的拒答。
    noise    返回了若干条，但没有任何一条能过注入门（pack 为空）——**可接受的
             拒答**：系统没有编答案，只是把低分邻居摆在了候选里，它们不会进 prompt。
    leak     至少一条能过注入门——**失败**：无关提问会被注入一段看似相关、
             实则无关的历史。

门禁只对 leak 报错：refusal_rate = (empty + noise) / n 必须为 1.0，
即 leak_rate <= max_leak_rate（默认 0.0）。noise_rate 是诊断量，不是阈值——
把它做成阈值等于要求检索器「连候选都不许返回」，那是另一个（更贵的）目标。

## 标注规则（absent_terms）

每条查询声明一组 absent_terms：这些子串在**可召回集**里必须一条都不存在。
可召回集与 ledger.search 的默认过滤逐条对齐：

    scope = 该查询的 scope（默认 shared）
    raw_chat = 0                       -- include_raw_chat=False
    quality_state = 'accepted'
    lifecycle IN ('active','cold')     -- include_cold=True
    evidence_json != '[]'              -- require_evidence=True
    content LIKE '%<absent_term>%'     -- 0 行

这组 SQL 就是「账本里根本没有」的可执行定义。它同时是**标注质量的护栏**：
如果某个 term 其实存在（标注写错、或账本后来真的学到了这个事实），
--audit-grounding（默认开）会把它报出来，此时正确的动作是**换查询**，
而不是放宽阈值——和 eval_memory 里 expect_hits >= 1 是同一类护栏。

注意：absent_terms 只保证「事实不在账本里」，不保证「这些字一个字都没出现过」。
例如「我的驾驶证号是多少」里的「我的」到处都有，拿它当 term 会立刻违规。
所以每个 term 都是该查询的**主题词**，且已逐条跑过上面的 SQL（见 docs/EVAL.md）。

## 用法

    python3 scripts/eval_refusal.py                  # 报告（默认不判退出门禁）
    python3 scripts/eval_refusal.py --gate           # 门禁：leak>0 或标注不干净 -> 退出 1
    python3 scripts/eval_refusal.py --json
    python3 scripts/eval_refusal.py --ledger /path/to/snapshot.sqlite
    python3 scripts/eval_refusal.py --queries my_refusal.json
    python3 scripts/eval_refusal.py --no-audit       # 跳过标注审计（更快，但少了护栏）
    python3 scripts/eval_refusal.py --dump-queries /tmp/refusal.json

账本以**只读**方式打开（mode=ro + PRAGMA query_only=ON）：本脚本不含任何写路径，
--ledger 指向生产库时不可能改到它。缺文件直接退出 2，绝不隐式创建。

退出码：0 = 报告完成或门禁通过，1 = 门禁失败（仅 --gate），2 = 用法/输入错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_memory  # noqa: E402
from hippomind.context_pack import (  # noqa: E402
    ContextPackBuilder,
    ContextPackOptions,
    _passes_relevance_gate,
)
from hippomind.ledger import SQLiteLedger  # noqa: E402
from hippomind.ledger._helpers import LedgerError  # noqa: E402

DEFAULT_LEDGER = eval_memory.DEFAULT_LEDGER

# 生产自动注入路径的门控参数。这两项必须与 auto_context.build_auto_pack /
# daemon 的 _options 保持一致，否则本脚本测的门不是线上那道门。
DEFAULT_MIN_SCORE = 0.20

# 可召回集谓词。与 ledger.search 的默认过滤逐条对齐（见模块 docstring）。
RECALLABLE_WHERE = (
    "scope=? AND raw_chat=0 AND quality_state='accepted' "
    "AND lifecycle IN ('active','cold') AND evidence_json!='[]' "
    "AND content LIKE ?"
)

# 阈值只表达一件事：**不许有泄漏**。min_queries 防的是集合被悄悄删小。
# 没有 recall 类阈值——拒答集没有 expect，recall 在这里无定义。
REFUSAL_THRESHOLDS: Dict[str, Any] = {
    "max_leak_rate": 0.0,
    "min_queries": 20,
    # 标注审计必须干净：term 若真的存在于可召回集，这条查询就不再是「账本里没有」。
    "require_grounded": True,
}

# 2026-10-25 生产账本实测基线（267,914 记忆 / 11,986 active）。
#
# 采样基准（重测时用它做对比锚点）：18 次，2026-10-25，生产账本
# ~267.9k 记忆 / ~12.0k active。
# **leak 恒为 0，但 empty/noise 的切分是漂移的**：18 次采样里 empty 在
# 22-29 之间、noise 在 9-16 之间来回移动。原因是候选生成在跑的同时账本本身
# 也在被 daemon 写入——`ledger.search` 的 FTS/LIKE 候选池与 coverage 分母
# 都会随之变化，于是同一条无关查询这次返回 0 条、下次返回 1-6 条。所以基线
# 记的是**区间**，不是点：
#
#   empty 22-29 / noise 9-16 / leak 0（refusal_rate 恒为 1.000）
#
# 门禁只钉 leak（恒 0），不钉 empty/noise 的切分——把它钉成点会让一条无关
# 查询因为「邻居多了一条」而失败，那不是回归，是账本在长大。max_top_score
# 同理只记录量级，不进门禁。
REFUSAL_BASELINE: Dict[str, Any] = {
    "date": "2026-10-25",
    "queries": 38,
    "empty": 27,
    "noise": 11,
    "leak": 0,
    "refusal_rate": 1.0,
    "noise_rate": 0.2895,
    "empty_range": [22, 29],
    "noise_range": [9, 16],
    "max_top_score_range": [0.377, 0.394],
    "samples": 18,
}

# REFUSAL_QUERIES：每条都是「账本里根本没有」的中文提问，按类别分组。
# absent_terms 用 SQL 逐条验证过（见 docs/EVAL.md 的刷新步骤）：
# 在生产账本的可召回集里命中 0 行。term 选的是**主题词**而不是整句——
# 整句做 LIKE 必然 0 行，那不能证明主题不存在（反而掩盖标注错误）。
REFUSAL_QUERIES: List[Dict[str, Any]] = [
    # ---- 日常/实时（任何本地记忆系统都不该知道） ----
    {"id": "weather-today", "category": "everyday",
     "query": "今天天气怎么样", "absent_terms": ["天气"],
     "note": "实时信息；「天气」全库 7 行，可召回集 0 行"},
    {"id": "weather-rain", "category": "everyday",
     "query": "明天会下雨吗", "absent_terms": ["下雨"],
     "note": "实时预报；「下雨」全库 2 行，可召回集 0 行"},
    # ---- 算术/常识（模型自己会算，记忆系统不该插话） ----
    {"id": "arith-one", "category": "arithmetic",
     "query": "1+1等于几", "absent_terms": ["等于几"],
     "note": "纯算术；用「等于几」而非「1+1」，后者在 11 行代码片段里出现"},
    {"id": "arith-pi", "category": "arithmetic",
     "query": "圆周率的第一千位是多少", "absent_terms": ["圆周率"],
     "note": "数学常数"},
    # ---- 虚构实体（最容易被「话题相近」的向量邻居硬凑） ----
    {"id": "mars-oxygen", "category": "fiction",
     "query": "火星基地的氧气配额是多少", "absent_terms": ["火星基地", "氧气配额"],
     "note": "虚构场景；「火星」全库 6 行但可召回集 0 行，故用更具体的复合词"},
    {"id": "moon-elevator", "category": "fiction",
     "query": "月球电梯的年度维护预算", "absent_terms": ["月球电梯"],
     "note": "虚构工程"},
    {"id": "unicorn-speed", "category": "fiction",
     "query": "蓝色独角兽的飞行速度", "absent_terms": ["独角兽"],
     "note": "虚构生物"},
    {"id": "purple-triangle", "category": "nonsense",
     "query": "紫色的三角形会唱歌吗", "absent_terms": ["紫色的三角形"],
     "note": "无意义提问，测的是「别把不相干的历史当答案」"},
    # ---- 外部世界事实 ----
    {"id": "stock-tomorrow", "category": "finance",
     "query": "明天股市会涨吗", "absent_terms": ["股市"],
     "note": "预测类，账本无金融数据"},
    {"id": "halving-date", "category": "finance",
     "query": "比特币下一次减半时间", "absent_terms": ["比特币"],
     "note": "加密资产行情"},
    {"id": "capital-au", "category": "geography",
     "query": "澳大利亚的首都是哪里", "absent_terms": ["澳大利亚的首都"],
     "note": "世界地理常识"},
    {"id": "mariana-depth", "category": "geography",
     "query": "马里亚纳海沟的最深处深度", "absent_terms": ["马里亚纳"],
     "note": "自然地理"},
    {"id": "fuji-eruption", "category": "geography",
     "query": "富士山上次喷发是哪一年", "absent_terms": ["富士山"],
     "note": "地理史"},
    {"id": "nobel-list", "category": "history",
     "query": "诺贝尔文学奖得主名单", "absent_terms": ["诺贝尔"],
     "note": "公共知识"},
    {"id": "rome-fall", "category": "history",
     "query": "古罗马帝国灭亡的具体年份", "absent_terms": ["古罗马"],
     "note": "世界史"},
    {"id": "iss-altitude", "category": "space",
     "query": "国际空间站的轨道高度", "absent_terms": ["国际空间站"],
     "note": "航天常识"},
    {"id": "light-speed", "category": "physics",
     "query": "光速在真空中的精确数值", "absent_terms": ["光速"],
     "note": "物理常数"},
    {"id": "water-boil", "category": "physics",
     "query": "水的沸点在标准大气压下", "absent_terms": ["沸点"],
     "note": "物理常识"},
    {"id": "genome-size", "category": "biology",
     "query": "人类基因组的碱基对总数", "absent_terms": ["基因组"],
     "note": "生物常识"},
    {"id": "ibuprofen-dose", "category": "medical",
     "query": "布洛芬的成人单次剂量", "absent_terms": ["布洛芬"],
     "note": "用药剂量——这类问题答错有实际危害，必须拒答"},
    # ---- 个人事实（账本里确实没有这个人的这些信息） ----
    {"id": "driver-license", "category": "personal",
     "query": "我的驾驶证号是多少", "absent_terms": ["驾驶证"],
     "note": "个人证件号；term 用「驾驶证」而非「我的」——后者满库都是"},
    {"id": "passport-expiry", "category": "personal",
     "query": "我的护照到期时间", "absent_terms": ["护照"],
     "note": "个人证件有效期"},
    {"id": "daughter-birthday", "category": "personal",
     "query": "我女儿的生日是哪天", "absent_terms": ["女儿"],
     "note": "亲属个人信息"},
    # ---- 生活/兴趣（账本里没有这类语料） ----
    {"id": "movie-scifi", "category": "recommendation",
     "query": "推荐一部科幻电影", "absent_terms": ["科幻电影"],
     "note": "推荐类，无片单语料"},
    {"id": "braised-pork", "category": "cooking",
     "query": "红烧肉要炖多久", "absent_terms": ["红烧肉"],
     "note": "菜谱；「红烧肉」全库 1 行，可召回集 0 行"},
    {"id": "tiramisu-ratio", "category": "cooking",
     "query": "提拉米苏的配方比例", "absent_terms": ["提拉米苏"],
     "note": "烘焙配方"},
    {"id": "iceland-aurora", "category": "travel",
     "query": "去冰岛看极光的最佳季节", "absent_terms": ["极光"],
     "note": "旅行攻略；「极光」全库 1 行，可召回集 0 行"},
    {"id": "chengdu-hotpot", "category": "travel",
     "query": "成都最好的火锅店", "absent_terms": ["火锅"],
     "note": "本地生活；「成都」全库 4 行但可召回集 0 行"},
    {"id": "corgi-care", "category": "pets",
     "query": "养一只柯基需要注意什么", "absent_terms": ["柯基"],
     "note": "宠物饲养"},
    {"id": "succulent-water", "category": "plants",
     "query": "多肉植物多久浇一次水", "absent_terms": ["多肉"],
     "note": "园艺"},
    {"id": "ev-battery", "category": "cars",
     "query": "电动汽车电池的保养周期", "absent_terms": ["电动汽车"],
     "note": "汽车养护"},
    # ---- 文化/规则 ----
    {"id": "marathon-record", "category": "sports",
     "query": "马拉松世界纪录保持者是谁", "absent_terms": ["马拉松"],
     "note": "体育记录"},
    {"id": "beethoven-premiere", "category": "music",
     "query": "贝多芬第九交响曲的首演地点", "absent_terms": ["贝多芬"],
     "note": "音乐史"},
    {"id": "oscar-rules", "category": "film",
     "query": "奥斯卡最佳影片的评选规则", "absent_terms": ["奥斯卡"],
     "note": "影视奖项规则"},
    {"id": "chess-opening", "category": "games",
     "query": "国际象棋的开局原则", "absent_terms": ["国际象棋"],
     "note": "棋类"},
    {"id": "kana-memory", "category": "language",
     "query": "日语五十音图的记忆方法", "absent_terms": ["五十音"],
     "note": "语言学习"},
    {"id": "annual-leave", "category": "law",
     "query": "劳动法规定的年假天数", "absent_terms": ["年假"],
     "note": "法规常识"},
    {"id": "tax-threshold", "category": "law",
     "query": "个税起征点是多少", "absent_terms": ["个税起征点"],
     "note": "税务；「个人所得税」全库 13 行但可召回集 0 行，故用复合词"},
]


# ---------------------------------------------------------------------------
# 只读账本
# ---------------------------------------------------------------------------


class ReadOnlyLedger(SQLiteLedger):
    """一个只能读的 SQLiteLedger。

    为什么继承而不是重写：检索与 ContextPack 走的是 ledger 的完整实现
    （search / _reading / get_memory / causal_ancestors / generation），
    任何「精简只读视图」都得重新实现这些语义，评测就测不到线上那条路径了。
    这里只把**会写**的四个入口关掉：

    * _bootstrap：父类会 PRAGMA journal_mode=WAL 并跑迁移，全是写操作；
    * _connect：改 mode=ro + PRAGMA query_only，连误写都进不去；
    * _transaction：任何写事务直接抛 LedgerError；
    * touch：ContextPack 命中后会调它加热度，评测不该改生产账本。
    """

    def _bootstrap(self) -> None:
        if not self.path.is_file():
            raise LedgerError("账本不存在: %s" % self.path)

    def _connect(self, write: bool = False, *,
                 check_same_thread: bool = True) -> sqlite3.Connection:
        if write:
            raise LedgerError("拒答评测使用只读账本，不允许写连接")
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True,
                               timeout=15.0, isolation_level=None,
                               check_same_thread=check_same_thread)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=15000")
        except sqlite3.DatabaseError:
            pass
        return conn

    def _transaction(self, callback, *, bump: bool = True):
        raise LedgerError("拒答评测使用只读账本，不允许写事务")

    def touch(self, memory_ids) -> int:
        return 0


# ---------------------------------------------------------------------------
# 查询集校验与标注审计
# ---------------------------------------------------------------------------


def validate_refusal_queries(raw: Any) -> List[Dict[str, Any]]:
    """校验拒答查询集并返回规范化 spec。

    与 eval_memory.validate_queries 的差别只有一处：没有 expect（拒答集
    没有「应该命中什么」），取而代之的是非空 absent_terms。其余规则相同
    ——id 唯一、query 非空、类型严格——因为一条写错的标注会让指标直接撒谎。
    """
    if not isinstance(raw, list):
        raise ValueError("refusal query set must be a JSON array of objects")
    specs: List[Dict[str, Any]] = []
    seen_ids = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError("query #%d must be an object" % index)
        query = str(item.get("query") or "").strip()
        if not query:
            raise ValueError("query #%d has an empty 'query'" % index)
        terms_raw = item.get("absent_terms")
        if not isinstance(terms_raw, list) or not terms_raw:
            raise ValueError(
                "query #%d (%r) needs a non-empty 'absent_terms' list"
                % (index, query))
        terms: List[str] = []
        for term in terms_raw:
            if not isinstance(term, str) or not term.strip():
                raise ValueError(
                    "query #%d (%r) has a blank/non-string 'absent_terms' entry"
                    % (index, query))
            term = term.strip()
            if len(term) < 2:
                # 单字 term 在任何中文账本里都必然存在，用它做「不存在」证明
                # 只会得到一条永远失败的标注。
                raise ValueError(
                    "query #%d (%r) has a 1-character absent term %r"
                    % (index, query, term))
            terms.append(term)
        query_id = str(item.get("id") or query).strip()
        if query_id in seen_ids:
            raise ValueError("duplicate query id %r" % query_id)
        seen_ids.add(query_id)
        spec: Dict[str, Any] = {"id": query_id, "query": query,
                                "absent_terms": terms}
        category = item.get("category")
        if category:
            spec["category"] = str(category).strip()
        scope = item.get("scope")
        if scope:
            spec["scope"] = str(scope).strip()
        note = item.get("note")
        if note:
            spec["note"] = str(note).strip()
        specs.append(spec)
    if not specs:
        raise ValueError("refusal query set is empty")
    return specs


def load_refusal_queries(path: os.PathLike) -> List[Dict[str, Any]]:
    """从 JSON 文件载入拒答查询集。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("cannot read query file %s: %s" % (path, exc))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON in %s: %s" % (path, exc))
    return validate_refusal_queries(raw)


def audit_grounding(ledger: SQLiteLedger,
                    specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """确认每条 absent_terms 在可召回集里真的一条都没有。

    这是标注质量的护栏，不是检索质量的度量：它跑的是 SQL，与检索器无关。
    如果某个 term 命中 > 0，说明要么标注写错（term 太泛，例如「我的」），
    要么账本后来真的学到了这个事实——两种情况下这条查询都不该继续待在
    拒答集里，否则 leak 会把标注 bug 报成检索 bug（eval_memory 的
    expect_hits >= 1 是同一个坑的另一面）。
    """
    checked = 0
    violations: List[Dict[str, Any]] = []
    with ledger._reading() as conn:
        for spec in specs:
            scope = spec.get("scope", "shared")
            for term in spec["absent_terms"]:
                checked += 1
                row = conn.execute(
                    "SELECT count(*) FROM memories WHERE " + RECALLABLE_WHERE,
                    (scope, "%" + term + "%")).fetchone()
                count = int(row[0]) if row is not None else 0
                if count:
                    violations.append({"id": spec["id"], "term": term,
                                       "scope": scope, "recallable": count})
    return {
        "checked_terms": checked,
        "queries": len(specs),
        "violations": violations,
        "clean": not violations,
    }


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------


def _default_search(ledger: SQLiteLedger):
    def search(query: str, scope: str, limit: int) -> List[Dict[str, Any]]:
        return ledger.search(query, scope=scope, limit=limit,
                             include_raw_chat=False, require_evidence=True)
    return search


def _default_pack(builder: ContextPackBuilder, min_score: float):
    def pack(query: str, scope: str) -> Dict[str, Any]:
        return builder.build(query, ContextPackOptions(
            max_items=8, scope=scope or "shared", min_score=min_score))
    return pack


def classify_hits(hits: Sequence[Dict[str, Any]], *,
                  min_score: float = DEFAULT_MIN_SCORE) -> Dict[str, Any]:
    """把一次检索结果判成 empty / noise / leak 三档（见模块 docstring）。

    判定只用生产注入门 _passes_relevance_gate，不用原始 score：
    无关查询的 score 地板约 0.37（recency/touch 贡献），任何「低于阈值即拒答」
    的规则都会被这个地板骗过。
    """
    top = list(hits)
    gated = [item for item in top if _passes_relevance_gate(item, min_score)]
    parts = [item.get("score_parts") or {} for item in top]
    verdict = "leak" if gated else ("noise" if top else "empty")
    return {
        "verdict": verdict,
        "hits": len(top),
        "injectable": len(gated),
        "top_score": round(max((float(item.get("score", 0.0)) for item in top),
                               default=0.0), 4),
        "max_fts": round(max((float(part.get("fts", 0.0) or 0.0)
                              for part in parts), default=0.0), 4),
        "max_coverage": round(max((float(part.get("coverage", 0.0) or 0.0)
                                   for part in parts), default=0.0), 4),
    }


def evaluate_refusal(ledger: SQLiteLedger,
                     specs: Sequence[Dict[str, Any]], *,
                     limit: int = 8,
                     min_score: float = DEFAULT_MIN_SCORE,
                     search: Optional[Callable[..., List[Dict[str, Any]]]] = None,
                     pack: Optional[Callable[..., Dict[str, Any]]] = None,
                     ) -> Dict[str, Any]:
    """跑完整拒答集，返回逐条判定与聚合指标。

    search / pack 是测试注入点：
    search(query, scope, limit) -> hits、pack(query, scope) -> pack。
    默认分别走 ledger.search（与 eval_memory 同一组过滤参数）和
    ContextPackBuilder.build(min_score=...)（与自动注入同一条路径）。
    """
    specs = validate_refusal_queries(list(specs))
    limit = max(1, int(limit))
    if pack is None:
        pack = _default_pack(ContextPackBuilder(ledger), min_score)
    if search is None:
        search = _default_search(ledger)
    rows: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for spec in specs:
        scope = spec.get("scope", "shared")
        started = time.perf_counter()
        hits = search(spec["query"], scope, limit)
        verdict = classify_hits(hits, min_score=min_score)
        built = pack(spec["query"], scope)
        latencies.append((time.perf_counter() - started) * 1000.0)
        row: Dict[str, Any] = {
            "id": spec["id"],
            "category": spec.get("category", "custom"),
            "query": spec["query"],
            "absent_terms": list(spec["absent_terms"]),
            "pack_items": len(built.get("items") or []),
        }
        row.update(verdict)
        # pack 为空但存在可注入 hit 时，说明是分组/预算把它挡下来了，而不是
        # 相关性门。这种「靠运气」的拒答必须被看见，所以单独记一个标记。
        row["pack_agrees"] = (row["pack_items"] > 0) == (row["verdict"] == "leak")
        row["latency_ms"] = round(latencies[-1], 2)
        rows.append(row)
    count = len(rows)
    empty = sum(1 for row in rows if row["verdict"] == "empty")
    noise = sum(1 for row in rows if row["verdict"] == "noise")
    leak = sum(1 for row in rows if row["verdict"] == "leak")
    metrics = {
        "empty": empty,
        "noise": noise,
        "leak": leak,
        "empty_rate": round(empty / count, 4),
        "noise_rate": round(noise / count, 4),
        "leak_rate": round(leak / count, 4),
        # 拒答率 = 没有泄漏的查询占比。noise 也算拒答（不会进 prompt）。
        "refusal_rate": round((empty + noise) / count, 4),
        "max_top_score": round(max((row["top_score"] for row in rows), default=0.0), 4),
        "pack_disagreements": sum(1 for row in rows if not row["pack_agrees"]),
        "latency_p50_ms": round(eval_memory.percentile(latencies, 0.50), 2),
        "latency_p95_ms": round(eval_memory.percentile(latencies, 0.95), 2),
    }
    by_category: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["category"], []).append(row)
    for name in sorted(grouped):
        items = grouped[name]
        by_category[name] = {
            "queries": len(items),
            "leak": sum(1 for item in items if item["verdict"] == "leak"),
            "empty": sum(1 for item in items if item["verdict"] == "empty"),
        }
    return {
        "queries": count,
        "k": limit,
        "min_score": min_score,
        "metrics": metrics,
        "by_category": by_category,
        "details": rows,
    }


def check_refusal_gate(
        result: Dict[str, Any], *,
        max_leak_rate: float = REFUSAL_THRESHOLDS["max_leak_rate"],
        min_queries: int = REFUSAL_THRESHOLDS["min_queries"],
        require_grounded: bool = REFUSAL_THRESHOLDS["require_grounded"],
        grounding: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """拒答门禁：零泄漏、集合不缩水、标注干净。

    只有这三条。没有 recall/MRR 阈值（拒答集没有 expect），也没有
    noise_rate 阈值（noise 是允许的拒答形态，把它设成阈值等于要求检索器
    连候选都不许返回）。grounding 为 None 表示本次没跑审计（--no-audit），
    此时 require_grounded 自动失效——但门禁报告会显式标注「未审计」，
    不让跳过审计看起来像通过审计。
    """
    metrics = result["metrics"]
    failures: List[str] = []
    if result["queries"] < min_queries:
        failures.append("queries %d < min_queries %d"
                        % (result["queries"], min_queries))
    if metrics["leak_rate"] > max_leak_rate:
        leaked = [row["id"] for row in result["details"]
                  if row["verdict"] == "leak"]
        failures.append("leak_rate %.4f > %.4f (leaked: %s)"
                        % (metrics["leak_rate"], max_leak_rate,
                           ", ".join(leaked[:8])))
    audited = grounding is not None
    if require_grounded and audited and not grounding["clean"]:
        bad = ", ".join("%s/%s=%d" % (item["id"], item["term"],
                                      item["recallable"])
                        for item in grounding["violations"][:8])
        failures.append("grounding violations: %s" % bad)
    return {
        "passed": not failures,
        "failures": failures,
        "audited": audited,
        "thresholds": {
            "max_leak_rate": max_leak_rate,
            "min_queries": min_queries,
            "require_grounded": require_grounded,
        },
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _print_grounding(grounding: Optional[Dict[str, Any]]) -> None:
    if grounding is None:
        print("grounding: SKIPPED (--no-audit；标注未经 SQL 复核)")
        return
    if grounding["clean"]:
        print("grounding: OK (%d queries, %d absent_terms 全部 0 行)"
              % (grounding["queries"], grounding["checked_terms"]))
        return
    print("grounding: VIOLATIONS (%d/%d terms 在可召回集中存在)"
          % (len(grounding["violations"]), grounding["checked_terms"]))
    for item in grounding["violations"][:20]:
        print("  %-22s %-14s scope=%-8s recallable=%d"
              % (item["id"], item["term"], item["scope"], item["recallable"]))


def _print_report(result: Dict[str, Any],
                  grounding: Optional[Dict[str, Any]] = None) -> None:
    metrics = result["metrics"]
    print("== set: refusal ==")
    print("queries=%d k=%d min_score=%.2f empty=%d noise=%d leak=%d "
          "refusal_rate=%.3f noise_rate=%.3f leak_rate=%.3f "
          "max_top_score=%.3f p50=%.1fms p95=%.1fms"
          % (result["queries"], result["k"], result["min_score"],
             metrics["empty"], metrics["noise"], metrics["leak"],
             metrics["refusal_rate"], metrics["noise_rate"],
             metrics["leak_rate"], metrics["max_top_score"],
             metrics["latency_p50_ms"], metrics["latency_p95_ms"]))
    for row in result["details"]:
        status = "FAIL" if row["verdict"] == "leak" else "PASS"
        print("  %-4s %-20s %-5s hits=%d pack=%d top=%.3f fts=%.3f cov=%.3f"
              % (status, row["id"], row["verdict"], row["hits"],
                 row["pack_items"], row["top_score"], row["max_fts"],
                 row["max_coverage"]))
        if row["verdict"] == "leak":
            print("       injected: %s" % " ".join(row["absent_terms"]))
    print("")
    _print_grounding(grounding)
    for name, stats in result["by_category"].items():
        print("  %-16s n=%-3d empty=%-3d leak=%d"
              % (name, stats["queries"], stats["empty"], stats["leak"]))


def _print_gate(gate: Dict[str, Any], k: int) -> None:
    thresholds = gate["thresholds"]
    print("gate: %s  k=%d (max_leak_rate=%.2f, min_queries=%d, grounded=%s%s)"
          % ("PASS" if gate["passed"] else "FAIL", k,
             thresholds["max_leak_rate"], thresholds["min_queries"],
             "on" if thresholds["require_grounded"] else "off",
             "" if gate["audited"] else ", 未审计"))
    for failure in gate["failures"]:
        print("  failure: %s" % failure)


def _dump(specs: Sequence[Dict[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(specs), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print("wrote %d refusal queries to %s" % (len(specs), path))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="HippoMind 拒答集评测：账本里没有的东西必须返回空而不是硬凑")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER,
                        help="SQLite ledger to evaluate (opened read-only)")
    parser.add_argument("--queries",
                        help="JSON refusal query file (overrides the built-in set)")
    parser.add_argument("--limit", "-k", type=int, default=8,
                        help="results retrieved per query (k)")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                        help="注入门的 min_score（默认 %.2f，与自动注入一致）"
                             % DEFAULT_MIN_SCORE)
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--gate", action="store_true",
                        help="门禁模式：泄漏或标注不干净时退出 1")
    parser.add_argument("--no-audit", action="store_true",
                        help="跳过 absent_terms 的 SQL 审计（更快，但少了护栏）")
    parser.add_argument("--max-leak-rate", type=float, default=None,
                        help="default: 0.0")
    parser.add_argument("--min-queries", type=int, default=None,
                        help="default: 20")
    parser.add_argument("--dump-queries", metavar="PATH",
                        help="write the active refusal set to PATH and exit")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    try:
        specs = (load_refusal_queries(args.queries) if args.queries
                 else validate_refusal_queries(REFUSAL_QUERIES))
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.dump_queries:
        return _dump(specs, Path(args.dump_queries))
    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("error: ledger not found: %s" % ledger_path, file=sys.stderr)
        return 2
    ledger = ReadOnlyLedger(ledger_path)
    try:
        grounding = None if args.no_audit else audit_grounding(ledger, specs)
        result = evaluate_refusal(ledger, specs, limit=args.limit,
                                  min_score=args.min_score)
    finally:
        ledger.close()
    result["set"] = "refusal"
    result["ledger"] = str(ledger_path)
    result["grounding"] = grounding
    overrides: Dict[str, Any] = {}
    if args.max_leak_rate is not None:
        overrides["max_leak_rate"] = args.max_leak_rate
    if args.min_queries is not None:
        overrides["min_queries"] = args.min_queries
    result["gate"] = check_refusal_gate(result, grounding=grounding, **overrides)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_report(result, grounding)
        print("")
        _print_gate(result["gate"], result["k"])
    if not args.gate:
        return 0
    return 0 if result["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

