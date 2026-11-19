"""Feedback, decay, retention, consolidation, pinning, forgetting, vacuum.

Part of the :mod:`hippomind.ledger` package (mechanically split from the
former single ``ledger.py``; behaviour and wire formats are unchanged).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from ..schema import fts_indexed_predicate
from ._helpers import (
    CONTEXT_FIELDS,
    DEFAULT_AUTO_VACUUM_FREELIST_RATIO,
    DEFAULT_AUTO_VACUUM_MIN_BYTES,
    DEFAULT_RAW_ARCHIVE_MAX_BYTES,
    DEFAULT_RAW_CAPACITY,
    DEFAULT_RAW_RETENTION_DAYS,
    IngestResult,
    LedgerError,
    MAX_EVIDENCE,
    _clamp_float,
    _clamp_int,
    _epoch,
    _fts_text,
    _fts_tokenizer,
    _hash,
    _json,
    _short,
    _strings,
    _text,
    _timestamp,
    utc_now,
)

#: maintain() 落 decay 事件的最小绝对降幅（权重是 0..1 的尺度）。
#:
#: 任务草稿写的是 abs(delta) > 1e-9 —— 那是「UPDATE 要不要写」的阈值，不是
#: 「要不要落审计事件」的阈值。实测：默认 decay_per_day=0.008、增量窗口
#: 1 小时，一条 weight=0.5 的行每 tick 掉 1.7e-4，远超 1e-9；3,000 条活跃行
#: 会产出约 72,000 条事件/天，一两天就把 ledger_events 的 100k 保留窗口挤满，
#: 随后被 maintain 自己的归档逻辑移进 ledger_events_archive —— 而 T5 的
#: forgetting.py 只读 ledger_events，报告会变成「没有任何 decay 事件」。
#: 1 个百分点/行的粒度让 weight=0.5 的行约 2.5 天一条、weight=0.1 的行约
#: 12 天一条，既保留「为什么掉」，又不会把账本淹掉。
DECAY_EVENT = "decay"
DECAY_EVENT_MIN_DROP = 0.01


def _supersede_window_end(conn: sqlite3.Connection, loser_valid_from: str,
                          winner_memory_id: str, now: str) -> str:
    """败者 claim 的 valid_until：优先取胜者的 valid_from，否则退回 now。

    为什么不是直接写 now（任务草稿的第一版写法）：A 在 2026-01 生效、B 在
    2026-06 生效，若 A.valid_until=now(2026-10)，则 as_of=2026-08 会**同时**
    返回 A 和 B —— 双时态又变成假的。取胜者 valid_from 让两个窗口首尾相接：
    A=[01,06)、B=[06,∞)，任意时刻恰好一条事实。

    退回 now 的两种情况：
    * 收敛路径（repair_claim_states / _converge_claim_memory）根本没有胜者，
      只是把「记忆已不可召回」的陈旧 claim 退休；
    * 胜者 valid_from 早于败者 valid_from（乱序回填/时钟回拨），此时取胜者
      会得到 valid_until < valid_from 的空窗口甚至负窗口，比 now 更假。

    返回空串表示「无从得知 / 不臆造」——调用方用 CASE WHEN valid_until=''
    保护，已非空的窗口一律不覆盖（用户显式登记的失效时间优先于派生值）。

    **不变式**：返回值要么为空串，要么严格大于败者自己的 valid_from。
    零宽窗口 [X, X) 会让事实在任何 as_of 下都不可见，是错误而非边界；
    这条不变式把该错误挡在全部 6 个调用点之外，而不是靠每个调用点自觉。

    **同秒精度**：utc_now() 带微秒，但显式登记的 valid_from / 归档时间戳
    经常只有秒。原文的 now > loser_from 字符串比较在两者同一秒时为假，
    于是返回空串——窗口永不闭合，as_of=未来会同时看见败者和胜者，双时态
    又变成假的。这里用「败者 valid_from 的下一秒」闭合：窗口 [X, X+1s)
    非零，是秒级精度下唯一不臆造更细时刻的诚实选择。只有无法构造非零窗口
    （败者起点在未来且跨秒、时钟回拨）才继续返回空串。
    """
    winner_memory_id = _short(winner_memory_id, 80)
    loser_from = str(loser_valid_from or "")
    if winner_memory_id:
        row = conn.execute("SELECT valid_from FROM memories WHERE memory_id=?",
                           (winner_memory_id,)).fetchone()
        winner_from = str(row["valid_from"] or "") if row is not None else ""
        if winner_from and winner_from > loser_from:
            return winner_from
    # 不变式：返回的 valid_until 必须**严格大于**败者自己的 valid_from。
    # 零宽窗口 [X, X) 不是「边界情况」而是错误：它让这条事实在**任何**
    # as_of 下都不可见，而且看起来像「有精确时间戳的确定结论」——比留空
    # （语义「仍然有效」）更糟。生产账本实测有 1,715 条 superseded claim
    # 的 valid_from 与 asserted_at 在同一秒，用后者冒充失效时间就会批量
    # 制造零宽窗口。
    #
    # now 通常满足不变式；不满足只可能是败者的 valid_from 在未来（时钟
    # 回拨或提前登记的事实被取代）。那种情况下任何「过去的」失效时刻都
    # 会让窗口倒置，所以返回空串——调用方一律写成
    # CASE WHEN valid_until='' THEN ? ELSE valid_until END，空串是空操作，
    # 即「不臆造窗口，留给后续裁决」。宁可没有答案，也不要一个错的答案。
    if not loser_from:
        # 没有起点就无所谓「零宽」：now 是唯一可用的失效时刻。
        return now
    try:
        loser_dt = datetime.fromisoformat(loser_from.replace("Z", "+00:00"))
        now_dt = (datetime.fromisoformat(str(now).replace("Z", "+00:00"))
                  if now else None)
    except (TypeError, ValueError):
        loser_dt = now_dt = None
    if loser_dt is not None and now_dt is not None:
        if loser_dt.tzinfo is None:
            loser_dt = loser_dt.replace(tzinfo=timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        if now_dt > loser_dt:
            # 正常路径：精确到微秒的 now 比「下一秒」更准确。
            return now
        if (loser_dt.replace(microsecond=0)
                == now_dt.replace(microsecond=0)):
            # 同一秒（含 now == loser_from 的等值情形，以及同秒内格式
            # 混用导致 now < loser_from 的情形）：下一秒闭合，严格非零。
            return (loser_dt.replace(microsecond=0)
                    + timedelta(seconds=1)).isoformat()
        # 败者 valid_from 在未来（时钟回拨 / 提前登记后被取代），且跨秒：
        # 任何「过去的」失效时刻都会造成负宽窗口，留空让后续裁决接手。
        return ""
    # 无法解析的旧格式时间串：退回改造前的字符串比较语义。
    if now and now > loser_from:
        return now
    return ""


def _append_decay_event(conn: sqlite3.Connection, *, now: str, memory_id: str,
                        scope: str, task_id: str, project: str,
                        from_lifecycle: str, to_lifecycle: str, reason: str,
                        weight_before: float, weight_after: float,
                        decay_per_day: float, age_days: float,
                        utility: float, importance: int,
                        row_age_days: float = 0.0) -> int:
    """一条 decay 审计事件；返回是否真的写入（INSERT OR IGNORE）。

    字段名是 T5 遗忘审计报告（hippomind/forgetting.py）的读取契约，必须
    逐字照抄：memory_id/from_lifecycle/to_lifecycle/reason/weight_before/
    weight_after/decay_per_day/age_days/utility/importance。改任何一个名字，
    报告侧就只剩「未标注/契约外原因」，而那是审计最不该出现的状态。

    两个年龄字段不是冗余，它们回答不同的问题，而且只有一个是本事件的决策
    输入：
    * age_days —— 距上次加权（last_weighted_at）的天数，是衰减指数公式的
      输入，也就是 weight_floor 判定的依据；
    * row_age_days —— 距创建（created_at）的天数，是 raw_retention 与
      age_limit 两条规则实际比较的量。
    只记前者时，一条 2000 年创建、因超龄冷置的记忆会显示 age_days=0.0，
    审计报告读起来像「刚写的记忆因为太老被归档」——自相矛盾。前者是任务
    契约字段，后者是补充字段（报告侧可选读）。

    event_id 里带 memory_id + 本次 tick 时间 + 目标态 + 原因，所以同一条记忆
    在同一个 tick 里不会重复（主键去重），跨 tick 的多次衰减各记一条——
    forgetting.py 按记忆汇总时会取首尾跨度，不需要这里合并。
    """
    digest = _hash(DECAY_EVENT, memory_id, now, to_lifecycle, reason,
                   "%.12f" % float(weight_after))
    payload = {
        "memory_id": memory_id,
        "from_lifecycle": from_lifecycle,
        "to_lifecycle": to_lifecycle,
        "reason": reason,
        "weight_before": round(float(weight_before), 12),
        "weight_after": round(float(weight_after), 12),
        "decay_per_day": float(decay_per_day),
        "age_days": round(float(age_days), 6),
        "row_age_days": round(float(row_age_days), 6),
        "utility": round(float(utility), 6),
        "importance": int(importance),
    }
    cursor = conn.execute("""
        INSERT OR IGNORE INTO ledger_events(
            event_id,event_type,occurred_at,ingested_at,source_agent,session_id,
            task_id,project,role,scope,evidence_json,content,content_hash,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, ("evt_" + digest[:32], DECAY_EVENT, now, now, "system", "",
          _short(task_id, 256), _short(project, 512), "system",
          _short(scope, 128) or "shared",
          _json(["memory:" + memory_id], "evidence"), "", digest,
          _json(payload, "metadata")))
    return 1 if cursor.rowcount else 0


class LifecycleMixin:
    """Feedback, decay, retention, consolidation, pinning, forgetting, vacuum."""

    def feedback(self, memory_id: str, signal: float, note: str = "",
                 source_agent: str = "", feedback_id: str = "") -> bool:
        memory_id = _short(memory_id, 80)
        if not memory_id:
            raise LedgerError("memory_id 不能为空")
        signal = _clamp_float(signal, 0.0, -1.0, 1.0)
        note = _text(note, 4_000, "反馈说明")
        source_agent = _short(source_agent, 128)
        now = utc_now()
        base = feedback_id or _hash(memory_id, signal, note, source_agent, now)
        feedback_id = "fb_" + _short(base, 128).replace(" ", "_")
        content_hash = _hash("feedback", memory_id, feedback_id, signal, note, source_agent)

        def write(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
            if row is None:
                raise LedgerError(f"未找到记忆: {memory_id}")
            if conn.execute("SELECT 1 FROM memory_feedback WHERE content_hash=?", (content_hash,)).fetchone():
                return False
            utility = max(-1.0, min(1.0, 0.8 * float(row["utility"]) + 0.2 * signal))
            weight = max(0.02, min(1.0, float(row["weight"]) + 0.16 * signal))
            # 负反馈扣 utility/weight，但不该提升 confidence：置信度衡量的
            # 是「这条记忆有多可信」，一次失败不能让它更可信。旧代码用
            # abs(signal) 让 signal=-1 与 +1 同样 +0.04，等于「指出它错了
            # 反而更可信」——方向反了。只有正信号提升，负信号保持不变。
            confidence = float(row["confidence"])
            if signal > 0:
                confidence += 0.04 * signal
            confidence = max(0.0, min(1.0, confidence))
            conn.execute("""
                INSERT INTO memory_feedback(feedback_id,memory_id,signal,note,created_at,source_agent,content_hash)
                VALUES(?,?,?,?,?,?,?)
            """, (feedback_id, memory_id, signal, note, now, source_agent, content_hash))
            conn.execute("""
                UPDATE memories SET utility=?,weight=?,confidence=?,updated_at=?,
                success_count=success_count+?,failure_count=failure_count+? WHERE memory_id=?
            """, (utility, weight, confidence, now, int(signal > 0), int(signal < 0), memory_id))
            event_hash = _hash("feedback-event", content_hash)
            conn.execute("""
                INSERT OR IGNORE INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,task_id,
                    project,role,scope,evidence_json,content,content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ("evt_" + event_hash[:32], "feedback", now, now, source_agent, "", "", "",
                  "feedback", row["scope"], _json(["memory:" + memory_id], "evidence"), note,
                  event_hash, _json({"memory_id": memory_id, "signal": signal}, "metadata")))
            return True

        return self._transaction(write)

    def touch(self, memory_ids: Iterable[str]) -> int:
        if getattr(self, "read_only", False):
            return 0
        ids = sorted({_short(mid, 80) for mid in memory_ids if _short(mid, 80)})[:128]
        if not ids:
            return 0
        now = utc_now()

        def write(conn: sqlite3.Connection) -> int:
            placeholders = ",".join("?" for _ in ids)
            result = conn.execute(
                # Touch is a retrieval signal, not fresh source evidence. Do
                # not reset updated_at/last_weighted_at merely because an old
                # record was recalled; otherwise hot history never decays.
                f"UPDATE memories SET touch_count=touch_count+1,last_access_at=? "
                f"WHERE memory_id IN ({placeholders})", (now, *ids)
            )
            return result.rowcount

        return self._transaction(write, bump=False)

    def supersede(self, old_memory_id: str, new_memory_id: str,
                  source_agent: str = "") -> bool:
        old_memory_id = _short(old_memory_id, 80)
        new_memory_id = _short(new_memory_id, 80)
        if not old_memory_id or not new_memory_id or old_memory_id == new_memory_id:
            raise LedgerError("替代关系需要两个不同的 memory_id")
        now = utc_now()

        def write(conn: sqlite3.Connection) -> bool:
            old = conn.execute("SELECT * FROM memories WHERE memory_id=?", (old_memory_id,)).fetchone()
            new = conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (new_memory_id,)).fetchone()
            if old is None or new is None:
                raise LedgerError("替代关系引用了不存在的记忆")
            if old["superseded_by"] == new_memory_id:
                return False
            # 双时态：让位的同时闭合窗口（valid_until = 失效时刻）。只改 state
            # 的话，系统能答「何时开始」却答不了「何时失效」，as_of 历史切片
            # 就没有判据（体检第三节）。
            #
            # 记忆行和 claim 都要写：search() 过滤的是 memories.valid_until，
            # 只写 claim 的话历史切片永远拿不回败者 —— 而 claim 的窗口是
            # 「这条事实何时不再成立」的原始记录，两者必须同步，否则两层
            # 对同一段历史给出不同答案。
            window_end = _supersede_window_end(conn, str(old["valid_from"] or ""),
                                               new_memory_id, now)
            conn.execute("UPDATE memories SET superseded_by=?,lifecycle='superseded',"
                         "valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END,"
                         "updated_at=? WHERE memory_id=?",
                         (new_memory_id, window_end, now, old_memory_id))
            conn.execute("UPDATE memory_claims SET state='superseded',"
                         "valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END "
                         "WHERE memory_id=? AND state='active'",
                         (window_end, old_memory_id))
            if window_end:
                conn.execute("UPDATE memory_relations SET valid_until=? "
                             "WHERE (parent_memory_id=? OR child_memory_id=?) "
                             "AND valid_until='' AND relation_type!='supersedes' "
                             "AND (valid_from='' OR valid_from<?)",
                             (window_end, old_memory_id, old_memory_id, window_end))
            conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (old_memory_id,))
            digest = _hash("supersede", old_memory_id, new_memory_id)
            conn.execute("""
                INSERT OR IGNORE INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,task_id,
                    project,role,scope,evidence_json,content,content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ("evt_" + digest[:32], "supersede", now, now, _short(source_agent, 128), "",
                  old["task_id"], old["project"], "system", old["scope"],
                  _json(["memory:" + old_memory_id, "memory:" + new_memory_id], "evidence"),
                  "", digest, _json({"old_memory_id": old_memory_id, "new_memory_id": new_memory_id}, "metadata")))
            return True

        return self._transaction(write)

    def forget(self, *, scope: str = "", before_time: str = "",
               memory_ids: Optional[Sequence[str]] = None,
               source_agent: str = "local-admin",
               purge: bool = False,
               import_batch: str = "",
               dry_run: bool = False) -> Dict[str, Any]:
        """遗忘请求：移除记忆投影（软遗忘）或连同事件内容一并清除（purge）。

        软遗忘：删除 memories 及其 claims/relations/feedback/FTS 行；
        ``ledger_events`` 保留（append-only 审计），并追加一条不含内容的
        forget 审计事件。``purge=True`` 同时删除对应 ledger_events 行——
        真正的内容擦除，需显式调用。至少提供 scope/memory_ids/import_batch
        之一。``import_batch`` 按导入批次回滚（迭代 56 的批次戳）。
        ``dry_run=True`` 只预览将被遗忘的记忆清单与影响面，不执行删除、
        不写审计事件——危险操作前的标准确认步骤。
        """
        scope = _short(scope, 128)
        before_iso = _timestamp(before_time) if before_time else ""
        ids = [_short(value, 80) for value in (memory_ids or []) if _short(value, 80)]
        batch = _short(import_batch, 160)
        if not scope and not ids and not batch:
            raise LedgerError("遗忘请求需要 --scope/--memory-id/--batch 之一")
        if batch:
            with self._reading() as conn:
                rows = conn.execute(
                    "SELECT memory_id FROM memories WHERE metadata_json LIKE ?",
                    (f'%{{"import_batch": "{batch}"%',)).fetchone()
                # LIKE 只能做前缀匹配兜底；精确匹配走 json_extract。
                rows = conn.execute(
                    "SELECT memory_id FROM memories WHERE "
                    "json_extract(metadata_json, '$.import_batch') = ?",
                    (batch,)).fetchall()
            ids = sorted({*(ids or []), *(r["memory_id"] for r in rows)})
            if not ids:
                return {"forgotten": 0, "batch": batch,
                        "note": "该批次没有可回滚的记忆"}
        now = utc_now()

        if dry_run:
            # 预览走只读连接：不进事务、不 bump generation、不写审计。
            d_clauses: List[str] = []
            d_params: List[object] = []
            if scope:
                d_clauses.append("scope=?")
                d_params.append(scope)
            if before_iso:
                d_clauses.append("created_at<=?")
                d_params.append(before_iso)
            if ids:
                d_clauses.append("memory_id IN (" +
                                 ",".join("?" for _ in ids) + ")")
                d_params.extend(ids)
            with self._reading() as conn:
                rows = conn.execute(
                    "SELECT memory_id,created_at,scope FROM memories WHERE " +
                    (" AND ".join(d_clauses) if d_clauses else "1=1"),
                    d_params).fetchall()
            created = sorted(row["created_at"] for row in rows
                             if row["created_at"])
            scopes: Dict[str, int] = {}
            for row in rows:
                scopes[row["scope"] or "shared"] = \
                    scopes.get(row["scope"] or "shared", 0) + 1
            return {"dry_run": True, "would_forget": len(rows), "purged": 0,
                    "time_from": created[0] if created else "",
                    "time_to": created[-1] if created else "",
                    "scopes": scopes,
                    "preview": [{"memory_id": row["memory_id"],
                                 "created_at": row["created_at"],
                                 "scope": row["scope"]} for row in rows[:50]],
                    "note": ("预览仅前 50 条；去掉 --dry-run 执行"
                             if len(rows) > 50 else "")}

        def write(conn: sqlite3.Connection) -> Dict[str, Any]:
            clauses: List[str] = []
            params: List[object] = []
            if scope:
                clauses.append("scope=?")
                params.append(scope)
            if before_iso:
                clauses.append("created_at<=?")
                params.append(before_iso)
            if ids:
                clauses.append("memory_id IN (" + ",".join("?" for _ in ids) + ")")
                params.extend(ids)
            targets = conn.execute(
                "SELECT memory_id,origin_event_id,created_at,scope FROM memories WHERE " +
                " AND ".join(clauses), params).fetchall()
            if not targets:
                return {"forgotten": 0, "purged": int(purge)}
            # 影响面摘要（迭代 81）：时间窗与 scope 分布，供审计报表。
            created = sorted(row["created_at"] for row in targets if row["created_at"])
            scope_counts: Dict[str, int] = {}
            for row in targets:
                scope_counts[row["scope"] or "shared"] = \
                    scope_counts.get(row["scope"] or "shared", 0) + 1
            impact = {"time_from": created[0] if created else "",
                      "time_to": created[-1] if created else "",
                      "scopes": scope_counts}
            target_ids = [row["memory_id"] for row in targets]
            event_ids = [row["origin_event_id"] for row in targets]
            id_placeholders = ",".join("?" for _ in target_ids)
            # 子表先行（foreign_keys=ON，引用必须先清）
            conn.execute(f"DELETE FROM memory_claims WHERE memory_id IN ({id_placeholders})",
                         target_ids)
            conn.execute(f"DELETE FROM memory_relations WHERE parent_memory_id IN ({id_placeholders}) "
                         f"OR child_memory_id IN ({id_placeholders})", (*target_ids, *target_ids))
            conn.execute(f"DELETE FROM memory_feedback WHERE memory_id IN ({id_placeholders})",
                         target_ids)
            conn.execute(f"DELETE FROM memory_fts WHERE memory_id IN ({id_placeholders})",
                         target_ids)
            conn.execute(f"DELETE FROM memories WHERE memory_id IN ({id_placeholders})",
                         target_ids)
            purged_events = 0
            if purge and event_ids:
                evt_placeholders = ",".join("?" for _ in event_ids)
                cur = conn.execute(
                    f"DELETE FROM ledger_events WHERE event_id IN ({evt_placeholders})",
                    event_ids)
                purged_events = cur.rowcount
            digest = _hash("forget", scope, before_iso, len(target_ids), purge, now)
            result_payload = {"forgotten": len(target_ids),
                              "purged": int(purged_events) if purge else int(purge)}
            conn.execute("""
                INSERT OR IGNORE INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,
                    task_id,project,role,scope,evidence_json,content,content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ("evt_" + digest[:32], "forget", now, now,
                  _short(source_agent, 128), "", "", "", "system", scope or "shared",
                  _json([], "evidence"), "", digest,
                  _json({"count": len(target_ids), "scope": scope,
                         "before": before_iso, "purge": bool(purge),
                         "purged_events": purged_events}, "metadata")))
            return {"forgotten": len(target_ids), "purged": int(purge),
                    "purged_events": purged_events, "impact": impact}

        return self._transaction(write)

    def pin_memory(self, memory_id: str, pinned: bool = True,
                   *, source: str = "local-admin") -> Dict[str, Any]:
        """手动置顶/取消置顶。

        置顶只在「已被查询词命中为候选」的记忆之间提升排名（有界加分），
        不把无关记忆强行塞进结果——覆盖度与可靠性门槛不变。
        """
        mid = _short(memory_id, 80)
        if not mid:
            raise LedgerError("memory_id 不能为空")
        now = utc_now()

        operator = _short(source, 128) or "local-admin"
        event_type = "pin" if pinned else "unpin"

        def write(conn: sqlite3.Connection) -> Dict[str, Any]:
            cur = conn.execute("UPDATE memories SET pinned=?, updated_at=? WHERE memory_id=?",
                               (1 if pinned else 0, now, mid))
            if cur.rowcount == 0:
                raise LedgerError(f"记忆不存在: {mid}")
            # 审计事件不含记忆内容（与 forget 同一隐私取向），仅记录操作本身。
            event_id = "evt-" + hashlib.sha256(
                f"{event_type}:{mid}:{now}".encode()).hexdigest()[:24]
            conn.execute("""
                INSERT INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,
                    task_id,project,role,scope,evidence_json,content,content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (event_id, event_type, now, now, operator, "", "", "", "",
                   "shared", json.dumps([f"{event_type}:{mid}"]), "",
                   "sha256:" + hashlib.sha256(
                       f"{event_type}:{mid}:{now}:{operator}".encode()).hexdigest(),
                   "{}"))
            return {"memory_id": mid, "pinned": bool(pinned), "updated_at": now,
                    "event_id": event_id}

        return self._transaction(write)

    def list_pinned(self, *, scope: str = "shared", limit: int = 100) -> List[Dict[str, Any]]:
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT memory_id,content,kind,scope,pinned,updated_at FROM memories "
                "WHERE pinned=1 AND scope=? ORDER BY updated_at DESC LIMIT ?",
                (_short(scope, 128) or "shared", _clamp_int(limit, 100, 1, 1000)),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_claim(self, claim_id: str, winner_memory_id: str,
                      *, source_agent: str = "local-admin") -> Dict[str, Any]:
        """人工裁决 Slot-Value 冲突：指定胜者，同槽位其余让位（谱系保留）。

        自 WP-C 起本方法**委托** settle_claim_slot：裁决的作用域就是传入
        claim 所在的 (scope, subject, predicate, slot_key) 槽位。旧实现会把
        败者记忆的**全部** claim 一起下线（单槽裁决=整条记忆失忆），并在
        败者仍持有其它 active claim 时把 memory 标成 superseded——这是审计
        里的「爆炸半径」缺陷。

        现在的范围契约：

        * 只让位同槽位的 live claim；无关槽位保持原状态（被 memory 级
          conflict flag 连带打成 conflict、且本槽位无竞争者的会收敛回
          active）；
        * 败者记忆只有在裁决后不再持有任何 active/conflict claim 时才整体
          superseded；否则保留其它事实、仍可召回；
        * 翻案重裁语义不变：组查询不限状态，胜者的 lifecycle / superseded_by
          / valid_until 一并恢复，可以反复改判。

        返回字段保持向后兼容：resolved（= claim_id）、winner、losers
        （败者记忆 id 列表），并合并 settle_claim_slot 的明细字段
        （crowned / retired / demoted / loser_memories / settled / ...）。
        """
        claim_id = _short(claim_id, 80)
        winner_memory_id = _short(winner_memory_id, 80)
        if not claim_id or not winner_memory_id:
            raise LedgerError("裁决需要 claim_id 与 winner_memory_id")
        # 既有入参校验保持不变，且必须在写事务之前抛错：未知 claim、
        # 未知胜者、scope 不一致、胜者不在冲突组内都沿用旧错误语义。
        # 真正的裁决动作统一委托给 settle_claim_slot。
        with self._reading() as conn:
            claim = conn.execute("SELECT * FROM memory_claims WHERE claim_id=?",
                                 (claim_id,)).fetchone()
            if claim is None:
                raise LedgerError(f"未找到 claim: {claim_id}")
            winner = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                                  (winner_memory_id,)).fetchone()
            if winner is None:
                raise LedgerError("未找到胜者记忆")
            if winner["scope"] != claim["scope"]:
                raise LedgerError("胜者与 claim 的 scope 不一致")
            in_group = conn.execute(
                "SELECT 1 FROM memory_claims WHERE scope=? AND subject=? "
                "AND predicate=? AND slot_key=? AND memory_id=? LIMIT 1",
                (claim["scope"], claim["subject"], claim["predicate"],
                 claim["slot_key"], winner_memory_id)).fetchone()
            if in_group is None:
                raise LedgerError("胜者不在该冲突组内")
        result = self.settle_claim_slot(claim_id, winner_memory_id,
                                        source_agent=source_agent)
        # 兼容字段：旧调用方（CLI/distill）读 resolved/winner/losers。
        result["resolved"] = claim_id
        result["losers"] = list(result.get("loser_memories") or [])
        return result

    def repair_claim_states(self) -> Dict[str, int]:
        """Converge claim states with their memory's quality/lifecycle.

        The invariant has **two** directions and this method repairs both.

        claim -> memory
            A claim may be active only while its memory is accepted+active.  A
            memory that left recall (superseded / expired / quarantine / cold)
            must not keep holding a slot: the slot would make a fresh claim
            look like a conflict.

        memory -> claim
            A memory that *is* recallable must not keep a claim in superseded
            when no winner took it over.  Without this direction a row demoted
            during a transient state never comes back: the 2026-10-25 ledger
            held 41 such claims, 20 of which have no resolve/release audit
            event at all -- only a repair pass that ran while the memory was
            briefly unreachable can explain them.

        Safe to run repeatedly and it never changes a ruling.  The reverse
        direction is deliberately narrow, because a false reactivation is
        worse than a stale demotion: it either resurrects a superseded fact or
        manufactures a conflict that costs both sides their recall.
        """
        def write(conn):
            now = utc_now()
            # 不变式（正向）：claim 的 state 必须与它所属记忆的**可召回性**一致。
            # 记忆一旦离开 accepted+active（被 superseded / expired / quarantine
            # / cold 归档），它的 claim 就不可能再被召回；继续标 active 只会
            # 占着 slot，让新记忆的 claim 被误判成"与它冲突"。
            #
            # 2026-10-24 实测：data_hygiene 把 4,543 条重复记忆降级为
            # quarantine 后，它们的 1,535 条 claim 仍是 active，health 报
            # "活跃 claim 4486 条，悬空 1535 条"。旧条件只覆盖 superseded/
            # expired，恰好漏掉 quarantine —— 而 quarantine 正是清理用的状态。
            #
            # 注意 WHERE NOT (<predicate>) 的括号不能省：predicate 是
            # "a AND b" 两个条件，写成 "a AND b IS 0" 时 IS 0 只作用于 b，
            # 整个条件退化成 a AND (NOT b)，会漏掉一大批行（实测只修掉 136/1671）。
            # 这条路径没有胜者（记忆可能被 quarantine/cold/superseded），
            # 失效时刻只能是 now —— 收敛发现它已不可召回的那一刻。
            superseded = conn.execute(
                "UPDATE memory_claims SET state='superseded',"
                "valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END "
                "WHERE state='active' "
                "AND memory_id IN (SELECT memory_id FROM memories "
                "WHERE NOT (" + fts_indexed_predicate() + "))", (now,)
            ).rowcount
            conflicted = conn.execute(
                "UPDATE memory_claims SET state='conflict' WHERE state='active' "
                "AND memory_id IN (SELECT memory_id FROM memories WHERE quality_state='conflict')"
            ).rowcount
            # A conflict is only meaningful while a competing claim exists.  When
            # the other side was superseded by a later ruling the surviving claim
            # stayed in conflict forever, which is how 73 claims sat in the
            # review queue with no counterpart.  Reactivate the last one standing
            # if its memory is still accepted+active.
            orphaned = conn.execute("""
                UPDATE memory_claims SET state='active'
                WHERE state='conflict'
                  AND memory_id IN (SELECT memory_id FROM memories
                                    WHERE " + fts_indexed_predicate() + ")
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_claims other
                      WHERE other.scope = memory_claims.scope
                        AND other.subject = memory_claims.subject
                        AND other.predicate = memory_claims.predicate
                        AND other.slot_key = memory_claims.slot_key
                        AND other.claim_id != memory_claims.claim_id
                        AND other.state IN ('active','conflict')
                  )
            """).rowcount
            # ------------------------------------------------------------------
            # 不变式（反向）：memory 可召回时，它**没有胜者**的 claim 不该是
            # superseded。上面几条只保证「claim 跟随 memory」一个方向。
            #
            # 2026-10-25 生产账本实测 41 条落在这一侧：memory 是
            # accepted+active（完全可召回），claim 却是 superseded 且
            # superseded_by=''——没有胜者说明它不是被裁决取代的。其中 20 条在
            # ledger_events 里找不到任何 resolve/release 记录，只有「memory
            # 短暂离开可召回态时被扫成 superseded，memory 后来恢复了，claim
            # 没跟着回来」能解释。
            #
            # 判据必须四条同时满足，少一条都会造成比现在更糟的状态：
            #
            #   1. superseded_by='' —— 有胜者就是真让位，复活等于推翻裁决。
            #   2. memory accepted+active —— 否则 claim 一活过来就是悬空 claim。
            #      round 12 修掉的 1,535 条事故正是这个形状：那 1,535 条
            #      quarantine+active 的记忆不可召回，它们的 claim 本来就该是
            #      superseded，绝不能进这条路径（判据里写 accepted 而不是只看
            #      lifecycle，就是为此——生产上 1,576 条里有 1,535 条是
            #      quarantine+active，只看 lifecycle 会把它们全放进来）。
            #   3. 同 slot 无 active/conflict —— 有竞争者时恢复 = 同一 slot 两条
            #      active claim，会立刻被判 conflict 然后两条一起掉出召回，
            #      纯负收益。生产实测 41 条里 32 条属于这种，只有 9 条可恢复。
            #   4. valid_until='' —— 窗口已闭合是时间语义（这条事实确实过期了），
            #      不是状态错乱；恢复它会让一条已失效的事实重新可召回。
            #
            # 条件 4 同时保证恢复时不需要动 valid_until：它本来就是空串。
            reactivated_superseded = conn.execute("""
                UPDATE memory_claims SET state='active'
                WHERE state='superseded'
                  AND valid_until=''
                  AND memory_id IN (SELECT memory_id FROM memories m
                                    WHERE m.superseded_by=''
                                      AND m.quality_state='accepted'
                                      AND m.lifecycle='active')
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_claims other
                      WHERE other.scope = memory_claims.scope
                        AND other.subject = memory_claims.subject
                        AND other.predicate = memory_claims.predicate
                        AND other.slot_key = memory_claims.slot_key
                        AND other.claim_id != memory_claims.claim_id
                        AND other.state IN ('active','conflict')
                  )
            """).rowcount
            return {"superseded": int(superseded or 0), "conflict": int(conflicted or 0),
                    "reactivated": int(orphaned or 0),
                    "reactivated_superseded": int(reactivated_superseded or 0)}

        return self._transaction(write)

    def _converge_claim_memory(self, conn: sqlite3.Connection, memory_ids: Sequence[str],
                               now: str) -> Dict[str, Any]:
        """Re-establish the claims invariant for the given memories.

        The invariant (checked by ``claims_consistency_report``) is:

            an ``active`` claim implies its memory is ``accepted``;
            a ``conflict`` claim implies its memory is flagged ``conflict``.

        ``quality_state='conflict'`` is a *memory-level* flag, so one disputed
        slot takes a memory's unrelated claims down with it.  Those collateral
        claims have no competitor of their own - they are orphans - and nothing
        ever released them, because ``repair_claim_states`` only reactivates an
        orphan when its memory is already accepted+active, while the memory
        could not become accepted while it still carried conflict claims.
        That circular wait is what parked 36 claims in the production queue.

        Convergence here is a fixed point, in two steps per memory:

        1. release every conflict claim that has no live competitor in its own
           slot (it is collateral, not a disagreement);
        2. recompute the flag from what is left - still disputed means the
           flag stays and no claim of that memory may be active; otherwise the
           flag clears to ``accepted``.

        Memories that are not on the active lifecycle are skipped entirely:
        their flags belong to the supersede/expire machinery, not to this one.
        """
        released: List[str] = []
        cleared: List[str] = []
        reflagged: List[str] = []
        retired: List[str] = []
        for memory_id in dict.fromkeys(memory_ids):
            row = conn.execute(
                "SELECT quality_state,lifecycle FROM memories WHERE memory_id=?",
                (memory_id,)).fetchone()
            if row is None:
                continue
            if str(row["lifecycle"]) != "active":
                # The memory is already out of recall (superseded/expired), so
                # its conflict claims are stale bookkeeping: neither side can be
                # crowned and neither can be released into an active slot.
                # Retire them so the review queue reflects only live disputes.
                # This changes no recall decision - a claim on a superseded
                # memory was never recallable either way - and the lineage
                # stays intact (the claim row is updated, never deleted).
                # 陈旧 conflict 退休同样没有胜者：记忆已经离开召回面，
                # 失效时刻记 now。
                stale = conn.execute(
                    "UPDATE memory_claims SET state='superseded',"
                    "valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END "
                    "WHERE memory_id=? AND state='conflict'",
                    (now, memory_id)).rowcount
                if stale:
                    retired.append(memory_id)
                continue
            conflicts = conn.execute(
                "SELECT claim_id, scope, subject, predicate, slot_key "
                "FROM memory_claims WHERE memory_id=? AND state='conflict'",
                (memory_id,)).fetchall()
            for claim in conflicts:
                rival = conn.execute(
                    "SELECT 1 FROM memory_claims WHERE claim_id!=? AND state IN "
                    "('active','conflict') AND scope=? AND subject=? AND "
                    "predicate=? AND slot_key=? LIMIT 1",
                    (claim["claim_id"], claim["scope"], claim["subject"],
                     claim["predicate"], claim["slot_key"])).fetchone()
                if rival is None:
                    conn.execute(
                        "UPDATE memory_claims SET state='active' "
                        "WHERE claim_id=?", (claim["claim_id"],))
                    released.append(str(claim["claim_id"]))
            remaining = int(conn.execute(
                "SELECT COUNT(*) FROM memory_claims "
                "WHERE memory_id=? AND state='conflict'",
                (memory_id,)).fetchone()[0])
            if remaining:
                if str(row["quality_state"]) != "conflict":
                    conn.execute(
                        "UPDATE memories SET quality_state='conflict',"
                        "updated_at=? WHERE memory_id=?", (now, memory_id))
                moved = conn.execute(
                    "UPDATE memory_claims SET state='conflict' "
                    "WHERE memory_id=? AND state='active'",
                    (memory_id,)).rowcount
                if moved:
                    reflagged.append(memory_id)
                continue
            if str(row["quality_state"]) != "conflict":
                continue
            conn.execute(
                "UPDATE memories SET quality_state='accepted',updated_at=? "
                "WHERE memory_id=?", (now, memory_id))
            cleared.append(memory_id)
        # 统一收敛 memory_fts 不变量：恢复为 accepted+active 的记忆补进热索引，
        # 离开可召回状态的记忆从热索引移除，确保 settle / release 零漂移。
        indexed_pred = fts_indexed_predicate()
        for mid in dict.fromkeys(memory_ids):
            mem = conn.execute("SELECT * FROM memories WHERE memory_id=?", (mid,)).fetchone()
            if mem is None:
                continue
            should_index = conn.execute(
                "SELECT 1 FROM memories WHERE memory_id=? AND " + indexed_pred,
                (mid,)).fetchone() is not None
            in_fts = conn.execute(
                "SELECT 1 FROM memory_fts WHERE memory_id=?", (mid,)).fetchone() is not None
            if should_index and not in_fts:
                try:
                    meta = json.loads(mem["metadata_json"] or "{}")
                except (TypeError, ValueError):
                    meta = {}
                conn.execute(
                    "INSERT INTO memory_fts(memory_id,content,task_id,project,"
                    "source_agent,kind) VALUES(?,?,?,?,?,?)",
                    (mid, _fts_text(mem["content"], meta if isinstance(meta, Mapping) else {}),
                     mem["task_id"], mem["project"], mem["source_agent"], mem["kind"]))
            elif not should_index and in_fts:
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (mid,))
        return {"orphans_released": released, "quality_cleared": cleared,
                "reflagged": reflagged, "stale_retired": retired}

    def _slot_claims(self, conn: sqlite3.Connection,
                     slot: Sequence[str]) -> List[sqlite3.Row]:
        """Every claim in one (scope, subject, predicate, slot_key) tuple."""
        return conn.execute("""
            SELECT c.claim_id, c.memory_id, c.state, c.valid_from, m.quality_state,
                   m.lifecycle, m.task_id, m.project, m.content, m.metadata_json,
                   m.source_agent, m.kind
            FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id
            WHERE c.scope=? AND c.subject=? AND c.predicate=? AND c.slot_key=?
        """, tuple(slot)).fetchall()

    def _claim_slot(self, conn: sqlite3.Connection,
                    claim_id: str) -> Tuple[sqlite3.Row, Tuple[str, str, str, str]]:
        claim = conn.execute("SELECT * FROM memory_claims WHERE claim_id=?",
                             (claim_id,)).fetchone()
        if claim is None:
            raise LedgerError(f"未找到 claim: {claim_id}")
        return claim, (claim["scope"], claim["subject"], claim["predicate"],
                       claim["slot_key"])

    def settle_claim_slot(self, claim_id: str, winner_memory_id: str = "",
                          *, source_agent: str = "local-admin") -> Dict[str, Any]:
        """Settle one conflict slot without the memory-wide blast radius.

        resolve_claim used to demote *every* claim of a losing memory, so
        crowning a winner on one slot silently deleted that memory's unrelated
        facts from recall.  On the production ledger that would have cost up to
        five unrelated active claims per ruling.

        This is the same decision scoped to the exact
        (scope, subject, predicate, slot_key) tuple:

        * the winner's claim in this slot becomes active;
        * every other live claim in this slot becomes superseded;
        * a losing memory's claims in *other* slots are collateral damage when
          identity flags the whole memory as conflict: after this ruling those
          conflict claims with no live competitor in their own slot are
          released back to active (_converge_claim_memory), so one ruling
          never costs a memory its unrelated facts;
        * a losing memory is marked superseded only when no active/conflict
          claim is left *anywhere* - otherwise it keeps its other facts;
        * winner_memory_id="" retires the whole slot (no side survives).

        Lineage is preserved either way: nothing is deleted and a superseded
        memory still points at its superseded_by.  settled in the result says
        whether the crowned claim really ended up active - it is False when the
        winning memory still holds conflicts in *other* slots, which must be
        settled before this slot can take effect.  loser_memories lists every
        memory on the losing side of this slot (demoted or kept alive);
        demoted narrows that to the ones that actually left recall.
        resolve_claim maps loser_memories onto its legacy losers field.
        """
        claim_id = _short(claim_id, 80)
        winner_memory_id = _short(winner_memory_id, 80)
        if not claim_id:
            raise LedgerError("settle_claim_slot 需要 claim_id")
        now = utc_now()

        def write(conn: sqlite3.Connection) -> Dict[str, Any]:
            claim, slot = self._claim_slot(conn, claim_id)
            rows = self._slot_claims(conn, slot)
            live = [r for r in rows if str(r["state"]) in ("active", "conflict")]
            group_ids = {str(r["memory_id"]) for r in rows}
            if winner_memory_id and winner_memory_id not in group_ids:
                raise LedgerError("胜者不在该冲突组内")
            loser_memories = sorted(
                mid for mid in group_ids
                if not winner_memory_id or mid != winner_memory_id)
            retired: List[str] = []
            for row in live:
                mid = str(row["memory_id"])
                if winner_memory_id and mid == winner_memory_id:
                    continue
                window_end = _supersede_window_end(
                    conn, str(row["valid_from"] or ""), winner_memory_id, now)
                conn.execute(
                    "UPDATE memory_claims SET state='superseded',"
                    "valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END "
                    "WHERE claim_id=?", (window_end, str(row["claim_id"])))
                retired.append(str(row["claim_id"]))
            crowned: List[str] = []
            if winner_memory_id:
                winner = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                                      (winner_memory_id,)).fetchone()
                if winner is None:
                    raise LedgerError("未找到胜者记忆")
                # Restore the winner to a recallable state, mirroring the
                # historical resolve_claim contract (lifecycle AND quality).
                conn.execute(
                    "UPDATE memories SET quality_state='accepted',"
                    "lifecycle=CASE WHEN lifecycle='superseded' THEN 'active' "
                    "ELSE lifecycle END,"
                    "superseded_by=CASE WHEN lifecycle='superseded' THEN '' "
                    "ELSE superseded_by END,"
                    "valid_until=CASE WHEN lifecycle='superseded' THEN '' "
                    "ELSE valid_until END,updated_at=? WHERE memory_id=?",
                    (now, winner_memory_id))
                conn.execute(
                    "UPDATE memory_claims SET state='active',valid_until='' "
                    "WHERE memory_id=? AND state!='active' "
                    "AND scope=? AND subject=? AND predicate=? AND slot_key=?",
                    (winner_memory_id, *slot))
                # 重新读取本槽位内胜者的 claim：翻案重裁时胜者原有 claim
                # 可能已是 superseded，只有 UPDATE 之后才是 active；
                # crowned 必须是「本次裁决真正立起来的 claim」。
                crowned = [str(r[0]) for r in conn.execute(
                    "SELECT claim_id FROM memory_claims WHERE memory_id=? "
                    "AND state='active' AND scope=? AND subject=? AND predicate=? "
                    "AND slot_key=?", (winner_memory_id, *slot)).fetchall()]
                has_fts = conn.execute(
                    "SELECT 1 FROM memory_fts WHERE memory_id=?",
                    (winner_memory_id,)).fetchone()
                should_index = conn.execute(
                    "SELECT 1 FROM memories WHERE memory_id=? AND "
                    + fts_indexed_predicate(), (winner_memory_id,)).fetchone() is not None
                if should_index and has_fts is None:
                    fts_text = _fts_text(winner["content"],
                                         json.loads(winner["metadata_json"] or "{}"))
                    conn.execute(
                        "INSERT INTO memory_fts(memory_id,content,task_id,project,"
                        "source_agent,kind) VALUES(?,?,?,?,?,?)",
                        (winner_memory_id, fts_text, winner["task_id"],
                         winner["project"], winner["source_agent"], winner["kind"]))
            # 先收敛、再判定「整体下线」。一次裁决会把败者其它槽位的 claim
            # 连带打成 conflict（identity 的 memory 级 flag）；收敛会把
            # **本槽位无竞争者**的连带 conflict 释放回 active，保留真正
            # 的其它冲突。旧顺序（先 demote 后 converge）在剩余 claim 仅
            # 剩「即将被释放的连带 conflict」的形态下会把仍可召回的败者
            # 整体下线——这是 WP-C 的爆炸半径之二。
            touched = [str(r["memory_id"]) for r in rows]
            state = self._converge_claim_memory(conn, touched, now)
            demoted: List[str] = []
            for row in live:
                mid = str(row["memory_id"])
                if winner_memory_id and mid == winner_memory_id:
                    continue
                if str(row["quality_state"]) in ("quarantine", "expired"):
                    continue
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM memory_claims "
                    "WHERE memory_id=? AND state IN ('active','conflict')",
                    (mid,)).fetchone()[0]
                if int(remaining):
                    continue
                demoted_end = _supersede_window_end(
                    conn, str(row["valid_from"] or ""), winner_memory_id, now)
                conn.execute(
                    "UPDATE memories SET quality_state='superseded',"
                    "lifecycle='superseded',superseded_by=?,"
                    "valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END,"
                    "updated_at=? WHERE memory_id=?",
                    (winner_memory_id, demoted_end, now, mid))
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (mid,))
                demoted.append(mid)
            # 收敛后仍可召回的败者必须留在热索引里：conflict 期间索引行
            # 通常还在，但历史降级路径可能已把它删掉。这里只补不删，降级
            # 由上方的显式 DELETE 负责，保住 accepted+active <=> memory_fts
            # 的单一不变量（health_overview 的 drift 判据）。
            for mid in loser_memories:
                if mid in demoted:
                    continue
                indexed = conn.execute(
                    "SELECT 1 FROM memories WHERE memory_id=? AND "
                    + fts_indexed_predicate(), (mid,)).fetchone()
                if indexed is None:
                    continue
                if conn.execute("SELECT 1 FROM memory_fts WHERE memory_id=?",
                                (mid,)).fetchone() is not None:
                    continue
                memory_row = conn.execute("SELECT * FROM memories WHERE memory_id=?",
                                          (mid,)).fetchone()
                if memory_row is None:
                    continue
                fts_text = _fts_text(memory_row["content"],
                                     json.loads(memory_row["metadata_json"] or "{}"))
                conn.execute(
                    "INSERT INTO memory_fts(memory_id,content,task_id,project,"
                    "source_agent,kind) VALUES(?,?,?,?,?,?)",
                    (mid, fts_text, memory_row["task_id"], memory_row["project"],
                     memory_row["source_agent"], memory_row["kind"]))
            # settled 的判据是「胜者在该槽位是否真的立住了」：翻案重裁时
            # 胜者的旧 claim 可能原本是 superseded，必须用裁决后的状态问，
            # 不能只看裁决前 live 行里的 claim。
            settled = False
            if winner_memory_id:
                settled = conn.execute(
                    "SELECT 1 FROM memory_claims WHERE memory_id=? AND state='active' "
                    "AND scope=? AND subject=? AND predicate=? AND slot_key=? "
                    "AND (valid_until='' OR valid_until>?)",
                    (winner_memory_id, *slot, now)).fetchone() is not None
            digest = _hash("settle-slot", claim_id, winner_memory_id, *sorted(retired))
            conn.execute("""
                INSERT OR IGNORE INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,session_id,
                    task_id,project,role,scope,evidence_json,content,content_hash,
                    metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ("evt_" + digest[:32], "resolve", now, now,
                  _short(source_agent, 128), "", "", "", "system", claim["scope"],
                  _json(["claim:" + claim_id], "evidence"), "", digest,
                  _json({"claim_id": claim_id, "mode": "slot",
                         "winner": winner_memory_id, "crowned": crowned,
                         "retired": retired, "demoted": demoted,
                         "loser_memories": loser_memories,
                         "settled": settled,
                         "quality_cleared": state["quality_cleared"],
                         "subject": claim["subject"],
                         "predicate": claim["predicate"],
                         "slot_key": claim["slot_key"]}, "metadata")))
            return {"claim_id": claim_id, "winner": winner_memory_id,
                    "crowned": crowned, "retired": retired, "demoted": demoted,
                    "loser_memories": loser_memories,
                    "settled": settled,
                    "quality_cleared": state["quality_cleared"],
                    "reflagged": state["reflagged"]}

        return self._transaction(write)

    def release_claim_slot(self, claim_id: str, *,
                           source_agent: str = "local-admin") -> Dict[str, Any]:
        """Clear one slot's conflict without crowning a winner (keep-both).

        ``resolve_claim`` always picks a winner, which is wrong for a slot whose
        two sides are both legitimate: two different projects sharing a
        predicate, or a fact plus its own refinement.  Deleting either side
        would lose information, so this releases every live claim in the slot
        back to ``active`` and leaves the memories alone.

        Safety: nothing is retired - no memory changes lifecycle, no claim is
        deleted, both values stay recallable.  A claim whose memory is out of
        recall for an unrelated reason (superseded/expired) is refused rather
        than activated, and a memory that still holds conflicts in *other*
        slots cannot have active claims at all, so its release is reported as
        ``blocked`` instead of producing a dangling claim.

        Slot isolation: the release is scoped to the exact
        (scope, subject, predicate, slot_key) tuple, so a memory holding other
        facts keeps them.
        """
        claim_id = _short(claim_id, 80)
        if not claim_id:
            raise LedgerError("release_claim_slot 需要 claim_id")
        now = utc_now()

        def write(conn: sqlite3.Connection) -> Dict[str, Any]:
            claim, slot = self._claim_slot(conn, claim_id)
            rows = self._slot_claims(conn, slot)
            released: List[str] = []
            blocked: List[str] = []
            for row in rows:
                if str(row["state"]) != "conflict":
                    continue
                mid = str(row["memory_id"])
                if str(row["lifecycle"]) != "active":
                    blocked.append(str(row["claim_id"]))
                    continue
                others = int(conn.execute(
                    "SELECT COUNT(*) FROM memory_claims WHERE memory_id=? "
                    "AND state='conflict' AND NOT (scope=? AND subject=? "
                    "AND predicate=? AND slot_key=?)",
                    (mid, *slot)).fetchone()[0])
                if others:
                    blocked.append(str(row["claim_id"]))
                    continue
                conn.execute(
                    "UPDATE memory_claims SET state='active',valid_until='' "
                    "WHERE claim_id=?", (str(row["claim_id"]),))
                released.append(str(row["claim_id"]))
            touched = [str(r["memory_id"]) for r in rows]
            state = self._converge_claim_memory(conn, touched, now)
            retired = state.get("stale_retired") or []
            if not released:
                # The slot may still have been cleared by retiring stale
                # conflicts on dead memories; report that honestly instead of
                # claiming nothing happened.
                return {"released": [], "blocked": blocked, "claim_id": claim_id,
                        "slot_cleared": bool(retired),
                        "quality_cleared": state["quality_cleared"],
                        "stale_retired": retired}
            digest = _hash("release-slot", claim_id, *sorted(released))
            conn.execute("""
                INSERT OR IGNORE INTO ledger_events(
                    event_id,event_type,occurred_at,ingested_at,source_agent,
                    session_id,task_id,project,role,scope,evidence_json,content,
                    content_hash,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, ("evt_" + digest[:32], "release", now, now,
                  _short(source_agent, 128), "", "", "", "system", claim["scope"],
                  _json(["claim:" + claim_id], "evidence"), "", digest,
                  _json({"claim_id": claim_id, "released": released,
                         "blocked": blocked,
                         "quality_cleared": state["quality_cleared"],
                         "subject": claim["subject"],
                         "predicate": claim["predicate"],
                         "slot_key": claim["slot_key"]}, "metadata")))
            return {"released": released, "blocked": blocked,
                    "claim_id": claim_id, "slot_cleared": True,
                    "quality_cleared": state["quality_cleared"],
                    "stale_retired": retired}

        return self._transaction(write)

    def maintain(self, active_limit: int = 2_000, cold_after_days: int = 90,
                 decay_per_day: float = 0.008, raw_active_limit: int = DEFAULT_RAW_CAPACITY,
                 raw_after_days: int = DEFAULT_RAW_RETENTION_DAYS, *,
                 raw_ttl_days: Optional[int] = None,
                 purge_raw: bool = False,
                 raw_capacity: Optional[int] = None,
                 raw_archive_dir: Optional[os.PathLike] = None,
                 raw_archive_max_bytes: int = DEFAULT_RAW_ARCHIVE_MAX_BYTES,
                 archive_raw: bool = True,
                 auto_vacuum_min_bytes: int = DEFAULT_AUTO_VACUUM_MIN_BYTES,
                 events_keep: int = 100_000) -> Dict[str, Any]:
        """Decay projections, expire facts and cold-archive low-value rows.

        It intentionally only changes ``memories``.  The corresponding entries
        in ``ledger_events`` remain durable audit evidence.
        """
        active_limit = _clamp_int(active_limit, 2_000, 32, 100_000)
        cold_after_days = _clamp_int(cold_after_days, 90, 1, 36500)
        decay_per_day = _clamp_float(decay_per_day, 0.008, 0.0, 1.0)
        raw_active_limit = _clamp_int(raw_active_limit, DEFAULT_RAW_CAPACITY, 100, 1_000_000)
        raw_after_days = _clamp_int(raw_after_days, DEFAULT_RAW_RETENTION_DAYS, 1, 36500)
        # ``raw_ttl_days`` is the *deletion* horizon and is intentionally separate
        # from ``raw_after_days`` (the point at which a raw row stops being hot).
        # Before this existed, "retention" only demoted rows to cold and the file
        # grew without bound: 1.47M raw messages, 4.48GB, on a 2-month-old ledger.
        #
        # ``purge_raw`` defaults to False: demoting to ``cold`` keeps a row
        # auditable through ``search(include_cold=True)``, which is the documented
        # storage contract and what existing callers assert.  Long-running
        # deployments that must bound file growth (the daemon) opt in explicitly.
        raw_ttl_days = _clamp_int(
            raw_ttl_days if raw_ttl_days is not None else max(raw_after_days, 90),
            max(raw_after_days, 90), 1, 36500)
        if raw_capacity is not None:
            # ``raw_capacity`` is the public policy name; retain the older
            # ``raw_active_limit`` argument for callers of the v0.7 API.
            raw_active_limit = _clamp_int(raw_capacity, raw_active_limit, 1, 1_000_000)
        raw_archive_max_bytes = _clamp_int(raw_archive_max_bytes,
                                           DEFAULT_RAW_ARCHIVE_MAX_BYTES, 1 << 20,
                                           2 * 1024 * 1024 * 1024)
        now = utc_now()
        now_epoch = _epoch(now)

        def write(conn: sqlite3.Connection) -> Dict[str, int]:
            # Index convergence: memory_fts must hold *exactly* the rows that
            # satisfy schema.FTS_INDEXED_PREDICATE (accepted + active).  Older
            # releases only pruned ``lifecycle!='active'``, so quarantined,
            # conflicted and superseded-but-active leftovers survived every
            # maintenance pass forever (live ledger: 3,851 quarantined+active
            # rows out of 5,571 index rows, of which only 392 were recallable).
            # The same pass also drops orphan index rows whose memory is gone.
            # Idempotent, so a long-running ledger converges without a rebuild.
            indexed_m = fts_indexed_predicate("m")
            legacy_stale_fts = int(conn.execute(
                "SELECT count(*) FROM memory_fts f WHERE NOT EXISTS ("
                "SELECT 1 FROM memories m WHERE m.memory_id=f.memory_id AND "
                + indexed_m + ")").fetchone()[0])
            conn.execute(
                "DELETE FROM memory_fts WHERE NOT EXISTS ("
                "SELECT 1 FROM memories m WHERE m.memory_id=memory_fts.memory_id AND "
                + indexed_m + ")")
            # Duplicate index rows: ``memory_id`` is declared UNINDEXED, so the
            # same memory can be inserted twice (an interrupted redact/repair
            # pass is the observed cause).  They cost space and make
            # ``fts_rows`` disagree with ``expected`` forever, so the same pass
            # collapses each memory_id to its first rowid.
            legacy_stale_fts += int(conn.execute(
                "SELECT COALESCE(SUM(c - 1), 0) FROM ("
                "SELECT COUNT(*) AS c FROM memory_fts GROUP BY memory_id"
                ")").fetchone()[0])
            conn.execute(
                "DELETE FROM memory_fts WHERE rowid NOT IN ("
                "SELECT MIN(rowid) FROM memory_fts GROUP BY memory_id)")
            # 审计事件压缩归档：主表超 events_keep 时把最旧行移入侧表。
            # ledger_events 仍是审计事实来源——侧表同构保留全部列，可查证。
            events_archived = 0
            keep = _clamp_int(events_keep, 100_000, 100, 10_000_000)
            # 以「无投影锚定的孤儿审计行」为治理对象：有投影的事件被外键
            # 钉住属永久居民，不计入阈值（否则永远超限、幂等性破坏）。
            orphan_events = int(conn.execute(
                "SELECT COUNT(*) FROM ledger_events WHERE event_id NOT IN "
                "(SELECT origin_event_id FROM memories)").fetchone()[0])
            total_events = orphan_events
            if orphan_events > keep:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS ledger_events_archive (
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        ingested_at TEXT NOT NULL,
                        source_agent TEXT NOT NULL,
                        session_id TEXT NOT NULL DEFAULT '',
                        task_id TEXT NOT NULL DEFAULT '',
                        project TEXT NOT NULL DEFAULT '',
                        role TEXT NOT NULL DEFAULT '',
                        scope TEXT NOT NULL,
                        evidence_json TEXT NOT NULL DEFAULT '[]',
                        content TEXT NOT NULL,
                        content_hash TEXT NOT NULL UNIQUE,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        asserted_at TEXT NOT NULL DEFAULT ''
                    )
                """)
                # 只归档无投影锚定的纯审计事件（feedback/pin/unpin/forget 等）：
                # 有记忆投影的事件被 memories.origin_event_id 外键钉住，必须
                # 留在主表以维持引用完整性。
                overflow = min(total_events - keep, 50_000)
                conn.execute(f"""
                    INSERT OR IGNORE INTO ledger_events_archive
                    SELECT * FROM ledger_events
                    WHERE event_id IN (
                        SELECT event_id FROM ledger_events
                        WHERE event_id NOT IN (
                            SELECT origin_event_id FROM memories
                        )
                        ORDER BY occurred_at ASC LIMIT {int(overflow)}
                    )
                """)
                events_archived = conn.execute("SELECT changes()").fetchone()[0]
                if events_archived:
                    conn.execute("""
                        DELETE FROM ledger_events WHERE event_id NOT IN (
                            SELECT origin_event_id FROM memories
                        ) AND event_id IN (
                            SELECT event_id FROM ledger_events_archive
                        )
                    """)
            protected_parents = {
                str(row[0]) for row in conn.execute("""
                    SELECT DISTINCT r.parent_memory_id
                    FROM memory_relations r JOIN memories child ON child.memory_id=r.child_memory_id
                    WHERE child.lifecycle='active'
                """).fetchall()
            }
            # 增量维护：只取「距上次加权超过重加权阈值」「有效期即将到来/
            # 已到」或「源年龄已越过冷置保留线」的行。刚维护过的行其状态
            # 条件以天为单位变化，一小时内不可能翻转；这样每个 tick 的扫描
            # 量与活跃库大小解耦，长跑不卡顿。
            reweight_cutoff = datetime.fromtimestamp(
                now_epoch - 3_600.0, tz=timezone.utc).isoformat()
            expiry_horizon = datetime.fromtimestamp(
                now_epoch + 3_600.0, tz=timezone.utc).isoformat()
            age_horizon = datetime.fromtimestamp(
                now_epoch - min(cold_after_days, raw_after_days) * 86_400.0,
                tz=timezone.utc).isoformat()
            rows = conn.execute("""
                SELECT memory_id,weight,last_weighted_at,valid_until,created_at,raw_chat,
                       scope,task_id,project,utility,importance
                FROM memories
                WHERE lifecycle='active'
                  AND (last_weighted_at<=? OR (valid_until!='' AND valid_until<=?)
                       OR created_at<=?)
                ORDER BY last_weighted_at ASC
                LIMIT 8000
            """, (reweight_cutoff, expiry_horizon, age_horizon)).fetchall()
            decayed = expired = archived = raw_archived = 0
            decay_events = 0
            for row in rows:
                lifecycle = "active"
                # 为什么这次降级，写进事件的 reason 字段。取值是 T5 遗忘审计
                # 报告（hippomind/forgetting.py REASON_EXPLANATIONS）的契约：
                # expired | raw_retention | weight_floor | age_limit。
                # 空串 = 本次没有发生 lifecycle 变化（只是权重重算）。
                reason = ""
                weight = float(row["weight"])
                last = _epoch(row["last_weighted_at"]) or now_epoch
                age_days = max(0.0, now_epoch - last) / 86400.0
                # Watcher ticks are normally seconds/minutes apart. Rewriting
                # every row for a numerically invisible decay wastes IO and
                # defeats the intended event-driven idle profile.
                reweight = age_days >= (1.0 / 24.0)
                next_weight = (max(0.02, min(1.0, weight * math.exp(-decay_per_day * age_days)))
                               if reweight else weight)
                valid_until = _epoch(row["valid_until"])
                created = _epoch(row["created_at"])
                # 行龄（距创建），与上面 age_days（距上次加权）是两个量：
                # raw_retention / age_limit 比较的是这个。
                row_age_days = (max(0.0, now_epoch - created) / 86400.0
                                if created else 0.0)
                if valid_until and valid_until <= now_epoch:
                    lifecycle = "expired"
                    reason = "expired"
                    expired += 1
                elif row["raw_chat"]:
                    # Chat transcripts are immutable evidence, not permanent
                    # hot working memory. Keep them auditable in the ledger,
                    # but move old projections out of the active/FTS path.
                    if created and now_epoch - created > raw_after_days * 86400:
                        lifecycle = "cold"
                        reason = "raw_retention"
                        raw_archived += 1
                elif row["memory_id"] in protected_parents:
                    # A live child keeps its causal decision/progress parents
                    # available even when their standalone activation decays.
                    lifecycle = "active"
                elif next_weight < 0.12 or (created and now_epoch - created > cold_after_days * 86400):
                    lifecycle = "cold"
                    # 两个条件同时成立时按「先撞到的那条」归因：权重跌破下限
                    # 是衰减直接造成的，年龄只是兜底；把它们混成一个 reason
                    # 会让报告里的 weight_floor 统计悄悄失真。
                    reason = ("weight_floor" if next_weight < 0.12 else "age_limit")
                    archived += 1
                changed_weight = reweight and abs(next_weight - weight) > 1e-9
                if changed_weight or lifecycle != "active":
                    conn.execute("UPDATE memories SET weight=?,last_weighted_at=?,lifecycle=?,"
                                 "archived_at=CASE WHEN ?='cold' THEN ? ELSE archived_at END,updated_at=? "
                                 "WHERE memory_id=?",
                                 (next_weight, now, lifecycle, lifecycle, now, now, row["memory_id"]))
                    if lifecycle != "active":
                        # Cold/expired projections remain auditable in SQLite, but must
                        # leave the hot FTS index. Search keeps bounded LIKE fallbacks
                        # for cold/audit requests.
                        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
                    decayed += 1
                    # 只有「真的发生了值得审计的事」才落事件：lifecycle 变了，
                    # 或权重降幅达到 DECAY_EVENT_MIN_DROP。NOT 是每次 UPDATE。
                    #
                    # 任务草稿写的是 abs(delta)>1e-9，那是「要不要写 UPDATE」的
                    # 阈值。用它落事件会产出约 72,000 条/天（3,000 活跃行 ×
                    # 24 tick，默认 decay_per_day=0.008 每 tick 掉约 1.7e-4），
                    # 一两天就撑满 ledger_events 的 100k 保留窗口，随后被
                    # maintain 自己的归档逻辑移进 ledger_events_archive ——
                    # 而 forgetting.py 只读 ledger_events，T5 的报告会变成
                    # 「账本里从来没有 decay 事件」。1% 绝对降幅让 weight=0.5
                    # 的行约 2.5 天一条，既留住「为什么掉」，又不淹没账本。
                    if reason or (changed_weight and abs(next_weight - weight) >= DECAY_EVENT_MIN_DROP):
                        decay_events += _append_decay_event(
                            conn, now=now, memory_id=str(row["memory_id"]),
                            scope=str(row["scope"]), task_id=str(row["task_id"]),
                            project=str(row["project"]),
                            from_lifecycle="active", to_lifecycle=lifecycle,
                            reason=reason or "weight_floor",
                            weight_before=weight, weight_after=next_weight,
                            decay_per_day=decay_per_day, age_days=age_days,
                            row_age_days=row_age_days,
                            utility=float(row["utility"]),
                            importance=int(row["importance"]))
            active_rows = conn.execute("""
                SELECT memory_id,weight,importance,utility,scope,task_id,project,
                       created_at,last_weighted_at
                FROM memories WHERE lifecycle='active' AND raw_chat=0
                    AND quality_state='accepted'
                ORDER BY (weight * (0.5 + importance / 20.0) + utility * 0.15) ASC, created_at ASC
            """).fetchall()
            eligible_rows = [row for row in active_rows if str(row["memory_id"]) not in protected_parents]
            overflow = max(0, len(eligible_rows) - active_limit)
            for row in eligible_rows[:overflow]:
                conn.execute("UPDATE memories SET lifecycle='cold',archived_at=?,updated_at=? WHERE memory_id=?",
                             (now, now, row["memory_id"]))
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
                # 容量挤出也是遗忘的一种，且是**唯一**无法从单行数据反推的
                # 一种（行本身权重/年龄都没问题，是被排序挤出去的）。不记
                # 事件的话，T5 报告会把这些行归成「没有解释」。
                decay_events += _append_decay_event(
                    conn, now=now, memory_id=str(row["memory_id"]),
                    scope=str(row["scope"]), task_id=str(row["task_id"]),
                    project=str(row["project"]),
                    from_lifecycle="active", to_lifecycle="cold",
                    reason="capacity_overflow",
                    weight_before=float(row["weight"]),
                    weight_after=float(row["weight"]),
                    decay_per_day=decay_per_day,
                    age_days=max(0.0, now_epoch - (_epoch(row["last_weighted_at"]) or now_epoch)) / 86400.0,
                    utility=float(row["utility"]),
                    importance=int(row["importance"]))
            archived += overflow
            raw_rows = conn.execute("""
                SELECT memory_id,weight,importance,utility,scope,task_id,project,
                       created_at,last_weighted_at
                FROM memories WHERE lifecycle='active' AND raw_chat=1
                ORDER BY created_at ASC
            """).fetchall()
            raw_overflow = max(0, len(raw_rows) - raw_active_limit)
            for row in raw_rows[:raw_overflow]:
                conn.execute("UPDATE memories SET lifecycle='cold',archived_at=?,updated_at=? WHERE memory_id=?",
                             (now, now, row["memory_id"]))
                conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (row["memory_id"],))
                decay_events += _append_decay_event(
                    conn, now=now, memory_id=str(row["memory_id"]),
                    scope=str(row["scope"]), task_id=str(row["task_id"]),
                    project=str(row["project"]),
                    from_lifecycle="active", to_lifecycle="cold",
                    reason="capacity_overflow",
                    weight_before=float(row["weight"]),
                    weight_after=float(row["weight"]),
                    decay_per_day=decay_per_day,
                    age_days=max(0.0, now_epoch - (_epoch(row["last_weighted_at"]) or now_epoch)) / 86400.0,
                    utility=float(row["utility"]),
                    importance=int(row["importance"]))
            raw_archived += raw_overflow
            # 维护末尾同步收敛已经离开 accepted+active（如刚被转为 cold/expired）
            # 的记忆上残留的 active claim，避免产生悬空 active claim。
            conn.execute(
                "UPDATE memory_claims SET state='superseded',"
                "valid_until=CASE WHEN valid_until='' THEN ? ELSE valid_until END "
                "WHERE state='active' AND memory_id IN ("
                "SELECT m.memory_id FROM memories m WHERE NOT (" + indexed_m + "))",
                (now,))
            return {"decayed": decayed, "expired": expired, "archived": archived,
                    "raw_archived": raw_archived, "decay_events": decay_events,
                    "active_limit": active_limit,
                    "raw_active_limit": raw_active_limit,
                    "fts_pruned": legacy_stale_fts,
                    "events_archived": events_archived}

        result = self._transaction(write)
        # Compression is deliberately outside the SQLite transaction. A slow
        # filesystem or a denied TCC path must not hold the WAL write lock;
        # evidence remains searchable in the ledger and the next maintenance
        # cycle retries the archive operation. Background callers can defer
        # this more expensive filesystem pass while retaining normal decay.
        if archive_raw:
            try:
                result.update(self.archive_raw_evidence(
                    before_days=raw_after_days, archive_dir=raw_archive_dir,
                    max_bytes=raw_archive_max_bytes,
                ))
            except (OSError, sqlite3.Error, LedgerError) as exc:
                result.update({"raw_compressed": 0, "raw_archive_bytes": 0,
                               "raw_archive_error": str(exc)[:300]})
        else:
            result.update({"raw_compressed": 0, "raw_archive_deferred": True})
        # Second pass: physically drop raw evidence past the deletion horizon.
        # Runs outside the write transaction (file IO must not hold the WAL lock)
        # and is fail-closed: nothing is deleted unless the archive verified.
        if purge_raw:
            try:
                result["raw_purged"] = self.purge_expired_raw(
                    before_days=raw_ttl_days,
                    archive_dir=raw_archive_dir,
                    max_bytes=raw_archive_max_bytes,
                )
            except (OSError, sqlite3.Error, LedgerError) as exc:
                result["raw_purged"] = {"purged": 0, "error": str(exc)[:300]}
        else:
            result["raw_purged"] = {"purged": 0, "deferred": True}
        # Reclaim file space when a large deletion just happened.  Skipping this
        # left 98.8% of a 754MB file as empty pages in the earlier audit: SQLite
        # moves freed pages to the freelist but never returns them to the OS
        # unless VACUUM runs, so a ledger could delete 1.4M rows and stay 4.48GB.
        result["storage"] = self._storage_report()
        result["auto_vacuum"] = self._maybe_auto_vacuum(
            result["storage"],
            min_reclaimable_bytes=_clamp_int(
                auto_vacuum_min_bytes, DEFAULT_AUTO_VACUUM_MIN_BYTES, 0, 64 * 1024 * 1024 * 1024),
            # Only the tier that can actually free pages is allowed to reclaim
            # them.  A full VACUUM is a whole-file rewrite; running it on a
            # maintenance tick that deleted nothing rewrites a multi-GB file for
            # no reason and starves concurrent readers.  ``purge_raw`` is the
            # delete-capable pass, so it owns the shrink.
            allow=bool(purge_raw))
        if result["auto_vacuum"].get("vacuumed"):
            # Re-measure: the point of the pass was to change these numbers.
            result["storage"] = self._storage_report()
        result["raw_retention_days"] = raw_after_days
        result["raw_ttl_days"] = raw_ttl_days
        result["raw_capacity"] = raw_active_limit
        return result

    def _storage_report(self) -> Dict[str, int]:
        """Page/freelist accounting used to decide whether shrinking is worth it."""
        conn = self._connect()
        try:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        return {"page_size": page_size, "page_count": page_count,
                "freelist": freelist, "auto_vacuum": auto_vacuum,
                "db_bytes": page_size * page_count,
                "reclaimable_bytes": page_size * freelist}

    def _maybe_auto_vacuum(self, storage: Mapping[str, Any], *,
                           min_reclaimable_bytes: int = DEFAULT_AUTO_VACUUM_MIN_BYTES,
                           freelist_ratio: float = DEFAULT_AUTO_VACUUM_FREELIST_RATIO,
                           allow: bool = True,
                           dry_run: bool = False) -> Dict[str, Any]:
        """Shrink the database file when deleted pages have piled up.

        A retention policy that deletes rows but never returns the pages is only
        half a policy: the ledger keeps its 4.48GB footprint while holding a
        fraction of the data.  This runs at most one shrink per maintenance pass,
        and only past a threshold, so small ledgers do not pay a full table copy
        on every tick.

        Two mechanisms, picked by schema state:

        * ``auto_vacuum=FULL/INCREMENTAL`` databases can return pages with a
          bounded ``PRAGMA incremental_vacuum`` — no full rewrite, safe to run
          often.  Turning ``auto_vacuum`` ON for an existing database requires a
          full VACUUM anyway, so we never flip it implicitly.
        * Otherwise a plain ``VACUUM``, which is a full rewrite.  It cannot run
          inside a transaction, so it uses its own connection and any failure is
          reported rather than raised: a locked or read-only database must not
          break maintenance.

        Returns a metrics record only — safe to log, no content.
        """
        summary: Dict[str, Any] = {
            "vacuumed": False, "reason": "", "reclaimed_bytes": 0,
            "before_bytes": int(storage.get("db_bytes", 0) or 0),
            "after_bytes": int(storage.get("db_bytes", 0) or 0),
            "mode": "none", "dry_run": bool(dry_run),
        }
        if not allow:
            summary["reason"] = "no delete pass this cycle"
            return summary
        if not storage:
            summary["reason"] = "storage unavailable"
            return summary
        reclaimable = int(storage.get("reclaimable_bytes", 0) or 0)
        page_count = int(storage.get("page_count", 0) or 0)
        freelist = int(storage.get("freelist", 0) or 0)
        ratio = (freelist / page_count) if page_count else 0.0
        # Either absolute waste or a high fraction justifies the pass.  A 4.48GB
        # ledger with 10% free space is worth shrinking; a 2MB ledger with the
        # same fraction is not.
        if reclaimable < min_reclaimable_bytes and ratio < freelist_ratio:
            summary["reason"] = "below threshold"
            return summary
        if dry_run:
            summary["reason"] = "dry run"
            return summary
        default_mode = 1 if int(storage.get("auto_vacuum", 0) or 0) else 0
        conn = self._connect(write=True)
        try:
            if default_mode:
                # Bounded: one page per step, capped so a huge freelist cannot
                # stall the maintenance tick indefinitely.
                steps = min(freelist, 100_000)
                conn.execute(f"PRAGMA incremental_vacuum({int(steps)})")
                summary["mode"] = "incremental"
            else:
                conn.execute("VACUUM")
                summary["mode"] = "full"
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            summary["vacuumed"] = True
        except sqlite3.Error as exc:
            # Locked/read-only/busy: report and let the next cycle retry.
            summary["reason"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            return summary
        finally:
            conn.close()
        after = self._storage_report()
        if after:
            summary["after_bytes"] = int(after.get("db_bytes", 0) or 0)
            summary["reclaimed_bytes"] = max(0, summary["before_bytes"] - summary["after_bytes"])
        summary["reason"] = "threshold exceeded"
        return summary

    def vacuum(self) -> Dict[str, Any]:
        """VACUUM 整理库文件，返回前后体积（调用方负责先备份）。"""
        path = Path(self.path)
        before = path.stat().st_size if path.exists() else 0
        conn = self._connect(write=True)
        try:
            # 先截断 WAL，否则空闲页仍在 WAL 里，主文件不会缩小。
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        after = path.stat().st_size if path.exists() else 0
        return {"before_bytes": before, "after_bytes": after,
                "reclaimed_bytes": max(0, before - after)}

    def repair_fts(self) -> Dict[str, Any]:
        """一键重建 FTS 投影（checkup --repair 的后端）。

        采用 DROP+CREATE 结构性重建：外部工具直接改动 FTS 表可能留下
        残缺的内部结构（integrity_check 报 fts5 corruption），仅 DELETE+重插
        不保证清除。DROP 后按当前分词器原样建表并回填。

        回填集合**不是**全部投影，而是 ``schema.FTS_INDEXED_PREDICATE``
        （accepted + active）——与写路径、``_rebuild_fts``、``fts_sync_report``、
        ``health_overview`` 和 ``maintain`` 的清理共用同一个判定。修复前这里
        回填「除 raw 且非 active 外全部行」，于是 repair 会把隔离/冲突/被取代
        行重新写回索引：干净库上实测 ``stale`` 从 0 变 1，索引垃圾越修越多。
        冷/审计行不重建索引，仍可经 ``search(include_cold=True)`` 的 LIKE
        回退通道检索。
        """
        with self._local_lock:
            conn = self._connect(write=True)
            try:
                before = conn.execute(
                    "SELECT COUNT(*) FROM memory_fts").fetchone()[0]
                conn.execute("DROP TABLE IF EXISTS memory_fts")
                conn.execute(
                    "CREATE VIRTUAL TABLE memory_fts USING fts5("
                    "memory_id UNINDEXED, content, task_id, project, "
                    "source_agent, kind, tokenize='" + _fts_tokenizer() + "')")
                self._rebuild_fts(conn)
                after = conn.execute(
                    "SELECT COUNT(*) FROM memory_fts").fetchone()[0]
                self._generation += 1
                return {"rebuilt": True, "fts_rows_before": int(before),
                        "fts_rows_after": int(after)}
            finally:
                conn.close()

    def consolidate(self, task_id: str = "", limit: int = 64) -> List[IngestResult]:
        """Create small evidence-backed task summaries from structured records only."""
        task_id = _short(task_id, 256)
        limit = _clamp_int(limit, 64, 1, 512)
        clauses = ["raw_chat=0", "kind!='semantic'", "lifecycle IN ('active','cold')",
                   "quality_state='accepted'", "evidence_json!='[]'"]
        params: List[object] = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        sql = ("SELECT * FROM memories WHERE " + " AND ".join(clauses) +
               " ORDER BY updated_at DESC LIMIT ?")
        params.append(limit)
        with self._reading() as conn:
            rows = conn.execute(sql, params).fetchall()
        by_task: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            key = row["task_id"] or ("project:" + row["project"] if row["project"] else "")
            if key:
                by_task.setdefault(key, []).append(row)
        results: List[IngestResult] = []
        for key, group in by_task.items():
            if len(group) < 2:
                continue
            context: Dict[str, List[str]] = {}
            evidence: List[str] = []
            for row in group:
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                for field, values in (metadata.get("context", {}) if isinstance(metadata, dict) else {}).items():
                    if field in CONTEXT_FIELDS:
                        context.setdefault(field, []).extend(_strings(values, 12, 4_000, field))
                evidence.extend(_strings(json.loads(row["evidence_json"]), MAX_EVIDENCE, 512, "evidence"))
            if not context:
                continue
            goal = (context.get("goal") or [f"任务 {key}"])[0]
            summary = f"任务 {key} 的已证据化摘要：{goal}"
            sources = sorted({str(row["memory_id"]) for row in group})
            # ------------------------------------------------------------------
            # Idempotent consolidation
            # ------------------------------------------------------------------
            # ``consolidate`` used to re-ingest a freshly built summary whose
            # metadata embedded the source ids *in row order*.  A stable content
            # key therefore still differed on almost every idle pass, so the
            # ledger accumulated 56,010 ``semantic`` rows holding just 142 distinct
            # texts (one repeated 5,423 times).  The projection is now keyed by the
            # source set, written once, and refreshed only when that set changes.
            fingerprint = _hash("consolidate", key, group[0]["scope"], *sources)[:32]
            marker = f"consolidation:{fingerprint}"
            task_val = key if not key.startswith("project:") else ""
            scope_val = str(group[0]["scope"])
            project_val = str(group[0]["project"])
            with self._reading() as conn:
                already = conn.execute(
                    "SELECT memory_id FROM memories WHERE kind='semantic' "
                    "AND lifecycle IN ('active','cold') AND evidence_json LIKE ? "
                    "AND quality_state='accepted' LIMIT 1",
                    (f"%{marker}%",),
                ).fetchone()
                if already is not None:
                    continue
                prior_ids = [
                    str(r["memory_id"]) for r in conn.execute(
                        "SELECT memory_id FROM memories WHERE kind='semantic' "
                        "AND source_agent='hippomindd' AND scope=? "
                        "AND task_id=? AND project=? AND lifecycle='active' "
                        "AND quality_state='accepted' AND evidence_json LIKE '%consolidation:%'",
                        (scope_val, task_val, project_val),
                    ).fetchall()
                ]
            result = self.ingest({
                "kind": "semantic", "content": summary, "source_agent": "hippomindd",
                "task_id": task_val,
                "project": project_val, "scope": scope_val,
                "evidence": [marker] + sources[:MAX_EVIDENCE - 1],
                "context": context,
                "confidence": min(0.95, 0.5 + 0.08 * len(group)), "importance": 7,
                "valid_from": group[0]["created_at"] if "created_at" in group[0].keys() else "",
                "metadata": {
                    "consolidated_from": sources,
                    "consolidation_fingerprint": fingerprint,
                },
            })
            for old_id in prior_ids:
                if old_id != result.memory_id:
                    try:
                        self.supersede(old_id, result.memory_id, source_agent="hippomindd")
                    except (LedgerError, sqlite3.Error):
                        pass
            results.append(result)
        return results
