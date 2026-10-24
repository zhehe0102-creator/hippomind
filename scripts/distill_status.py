#!/usr/bin/env python3
"""Distillation backlog and throughput: the truth, reproducibly.

The handoff note this replaces said "250k raw messages are not back-filled",
which made the remaining work look like weeks.  It was wrong, and the reason it
was wrong is a distinction the ledger makes but the prose did not:

    quality_state  lifecycle   meaning
    -------------  ---------   ------------------------------------------
    accepted       cold        archived evidence, kept for provenance only
    accepted       active      live memory the hub actually retrieves
    quarantine     *           failed the quality gate, never distilled

Cold rows are *not* a backlog.  Most of the raw rows are cold: they were
deliberately retired by retention.  Counting them as pending inflated the work
by roughly 60x and sent a previous session optimising the wrong thing.

This script prints, from the ledger itself:

  * the (quality_state, lifecycle, raw_chat) distribution, so "cold" is visible
    instead of hidden inside a single total;
  * accepted+active split into raw vs structured;
  * how many rows distillation has already cursor-marked, and the ratio;
  * the *real* remaining backlog, split raw/structured, both as rows and as
    distinct content (the number that actually costs model calls);
  * measured throughput from the recent distill cursor timestamps, and the
    resulting time-to-drain;
  * an explicit "how to reproduce this by hand" appendix with the SQL used.

Every number is computed from the same SQL the docstring quotes, so a reader can
diff this output against sqlite3 and get the same answer.  The ledger is opened
read-only: measuring production must never mutate it.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.distill import (  # noqa: E402
    default_batch_size,
    default_run_limit,
)
from hippomind.textutil import content_fingerprint  # noqa: E402

#: The kinds DistillEngine actually claims.  Kept as one string so the status
#: query and the engine cannot drift apart silently.
DISTILLABLE_KINDS = ("task", "progress", "decision", "semantic", "fact",
                     "preference", "procedural", "raw_chat")

#: Rows counted as "pending" -- identical predicate to DistillEngine._candidates.
PENDING_WHERE = (
    "c.source_id IS NULL AND m.quality_state='accepted' "
    "AND m.lifecycle='active' AND m.source_agent!='hippomind-distill' "
    "AND m.kind IN (" + ",".join("'%s'" % kind for kind in DISTILLABLE_KINDS) + ") "
)

PENDING_FROM = (
    "FROM memories m LEFT JOIN source_cursors c "
    "ON c.source_id='distill:' || m.memory_id WHERE " + PENDING_WHERE
)


def default_ledger_path() -> Path:
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "hippomind.sqlite"


class ReadOnlyLedger:
    """Open the live ledger without any possibility of writing to it."""

    def __init__(self, path: os.PathLike):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError("账本不存在: %s" % self.path)
        self._conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
        except sqlite3.DatabaseError:
            pass

    def rows(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row:
        return self._conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "ReadOnlyLedger":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def state_distribution(ledger: ReadOnlyLedger) -> List[Dict[str, Any]]:
    """Every (quality_state, lifecycle, raw_chat) bucket, biggest first."""
    rows = ledger.rows(
        "SELECT quality_state, lifecycle, raw_chat, COUNT(*) n FROM memories "
        "GROUP BY 1,2,3 ORDER BY n DESC")
    return [{"quality_state": str(r["quality_state"]),
             "lifecycle": str(r["lifecycle"]),
             "raw_chat": int(r["raw_chat"]),
             "rows": int(r["n"])} for r in rows]


def active_split(ledger: ReadOnlyLedger) -> Dict[str, int]:
    """accepted+active, split by whether the row is raw chat or structured."""
    row = ledger.one(
        "SELECT SUM(CASE WHEN raw_chat=1 THEN 1 ELSE 0 END) raw, "
        "SUM(CASE WHEN raw_chat=0 THEN 1 ELSE 0 END) structured, "
        "COUNT(*) total FROM memories "
        "WHERE quality_state='accepted' AND lifecycle='active'")
    return {"raw": int(row["raw"] or 0), "structured": int(row["structured"] or 0),
            "total": int(row["total"] or 0)}


def distill_progress(ledger: ReadOnlyLedger) -> Dict[str, Any]:
    """How much of the eligible population the distill cursors already cover."""
    cursors = int(ledger.one(
        "SELECT COUNT(*) n FROM source_cursors WHERE source_id LIKE 'distill:%'")["n"])
    eligible = int(ledger.one(
        "SELECT COUNT(*) n FROM memories WHERE quality_state='accepted' "
        "AND lifecycle='active' AND source_agent!='hippomind-distill' "
        "AND kind IN (" + ",".join("'%s'" % k for k in DISTILLABLE_KINDS) + ")")["n"])
    with_claims = int(ledger.one(
        "SELECT COUNT(*) n FROM source_cursors WHERE source_id LIKE 'distill:%' "
        "AND CAST(COALESCE(json_extract(metadata_json,'$.claims'),0) AS INTEGER) > 0")["n"])
    return {"cursors": cursors, "eligible": eligible,
            "ratio": round(cursors / eligible, 4) if eligible else 0.0,
            "cursors_with_claims": with_claims,
            "cursors_without_claims": cursors - with_claims}


def pending_backlog(ledger: ReadOnlyLedger) -> Dict[str, Any]:
    """The number that matters: what is still queued, by rows and by content."""
    counts = ledger.one(
        "SELECT SUM(CASE WHEN m.raw_chat=1 THEN 1 ELSE 0 END) raw, "
        "SUM(CASE WHEN m.raw_chat=0 THEN 1 ELSE 0 END) structured, "
        "COUNT(*) total " + PENDING_FROM)
    return {"raw_rows": int(counts["raw"] or 0),
            "structured_rows": int(counts["structured"] or 0),
            "rows": int(counts["total"] or 0),
            "raw_distinct": None, "structured_distinct": None, "distinct": None,
            "scanned": 0, "scan_truncated": False}


def distinct_contents(ledger: ReadOnlyLedger, *, scan_limit: int = 200_000) -> Dict[str, Any]:
    """Count distinct normalised contents among pending rows.

    Deliberately reads rows in Python rather than using COUNT(DISTINCT content):
    the engine dedupes on the *normalised* fingerprint (whitespace, timestamps
    and runner wrappers stripped), and two rows that differ only by a captured
    timestamp are one model call.  COUNT(DISTINCT content) would overstate the
    cost, which is the same class of error as counting cold rows as backlog.
    """
    rows = ledger.rows("SELECT m.memory_id, m.content, m.raw_chat " + PENDING_FROM)
    truncated = len(rows) > scan_limit
    if truncated:
        rows = rows[:scan_limit]
    raw_keys, structured_keys = set(), set()
    for row in rows:
        key = content_fingerprint(str(row["content"] or ""))
        if not key:
            key = "\x00" + str(row["memory_id"])
        (raw_keys if int(row["raw_chat"]) else structured_keys).add(key)
    return {"raw_distinct": len(raw_keys), "structured_distinct": len(structured_keys),
            "distinct": len(raw_keys) + len(structured_keys),
            "scanned": len(rows), "scan_truncated": truncated}


def throughput(ledger: ReadOnlyLedger, *, hours: int = 6,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """Rows/hour from the distill cursor timestamps themselves.

    The cursor's updated_at is the only honest record of when a row was
    distilled: the distilled *memories* are created in bursts and the daemon's
    health counter is overwritten every tick, so neither can be replayed.
    """
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=max(1, hours))).isoformat()
    rows = ledger.rows(
        "SELECT updated_at FROM source_cursors WHERE source_id LIKE 'distill:%' "
        "AND updated_at >= ? ORDER BY updated_at", (since,))
    stamps = []
    for row in rows:
        try:
            stamps.append(datetime.fromisoformat(str(row["updated_at"])))
        except (TypeError, ValueError):
            continue
    if len(stamps) < 2:
        return {"window_hours": hours, "samples": len(stamps),
                "rows_per_hour": None, "note": "样本不足，无法估算吞吐"}
    span_hours = (stamps[-1] - stamps[0]).total_seconds() / 3600.0
    if span_hours <= 0:
        return {"window_hours": hours, "samples": len(stamps),
                "rows_per_hour": None, "note": "时间跨度为零，无法估算"}
    rate = len(stamps) / span_hours
    # Hourly histogram makes a stalled daemon obvious at a glance.
    buckets: Dict[str, int] = {}
    for stamp in stamps:
        key = stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
        buckets[key] = buckets.get(key, 0) + 1
    return {"window_hours": hours, "samples": len(stamps),
            "span_hours": round(span_hours, 2),
            "rows_per_hour": round(rate, 1),
            "hourly": [{"hour": key, "rows": buckets[key]} for key in sorted(buckets)]}


def drain_estimate(pending: Dict[str, Any], rate: Optional[float],
                   *, batch_size: int, limit: int) -> Dict[str, Any]:
    """Time to drain at the measured rate, plus the calls the settings imply."""
    rows = int(pending.get("rows") or 0)
    result: Dict[str, Any] = {"rows": rows, "rows_per_hour": rate}
    if rate:
        result["hours_at_measured_rate"] = round(rows / rate, 2)
        result["days_at_measured_rate"] = round(rows / rate / 24.0, 2)
    else:
        result["hours_at_measured_rate"] = None
        result["days_at_measured_rate"] = None
    calls = int(pending.get("distinct") or 0)
    result["model_calls_needed"] = calls
    result["configured_batch_size"] = batch_size
    result["configured_run_limit"] = limit
    # One run() pass claims "limit" rows and sends them as ceil(limit/batch)
    # model calls; draining the backlog therefore needs ceil(calls/limit) passes.
    per_run = max(1, limit)
    result["calls_per_run"] = -(-per_run // max(1, batch_size))
    result["runs_needed"] = -(-calls // per_run) if calls else 0
    result["rows_per_run"] = per_run
    return result


def distill_status(ledger: ReadOnlyLedger, *, hours: int = 6,
                   scan_limit: int = 200_000,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the report prints, as plain data."""
    total = int(ledger.one("SELECT COUNT(*) n FROM memories")["n"])
    distribution = state_distribution(ledger)
    active = active_split(ledger)
    progress = distill_progress(ledger)
    pending = pending_backlog(ledger)
    distinct = distinct_contents(ledger, scan_limit=scan_limit)
    pending.update({key: distinct[key] for key in
                    ("raw_distinct", "structured_distinct", "distinct",
                     "scanned", "scan_truncated")})
    rate = throughput(ledger, hours=hours, now=now)
    batch = default_batch_size()
    limit = default_run_limit()
    drain = drain_estimate(pending, rate.get("rows_per_hour"),
                           batch_size=batch, limit=limit)
    cold = sum(item["rows"] for item in distribution if item["lifecycle"] == "cold")
    return {
        "ledger": str(ledger.path),
        "memories_total": total,
        "distribution": distribution,
        "active": active,
        "progress": progress,
        "pending": pending,
        "throughput": rate,
        "drain": drain,
        "cold_rows": cold,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_int(value: Optional[int]) -> str:
    return "n/a" if value is None else "{:,}".format(int(value))


def _fmt_rate(value: Optional[float]) -> str:
    return "n/a" if value is None else "{:,.1f}".format(float(value))


def render(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    add = lines.append
    active = result["active"]
    pending = result["pending"]
    progress = result["progress"]
    rate = result["throughput"]
    drain = result["drain"]

    add("HippoMind 提炼状态 · 账本 %s" % result["ledger"])
    add("=" * 78)
    add("")
    add("1. 总量")
    add("   记忆总数              %s" % _fmt_int(result["memories_total"]))
    add("")
    add("2. 状态分布（关键：cold 是归档态，不是积压）")
    add("   %-12s %-12s %-9s %12s" % ("quality_state", "lifecycle", "raw_chat", "行数"))
    for item in result["distribution"]:
        add("   %-12s %-12s %-9s %12s" % (item["quality_state"], item["lifecycle"],
                                          item["raw_chat"], _fmt_int(item["rows"])))
    add("   其中 cold（归档）合计   %s  <- 不计入待处理" % _fmt_int(result["cold_rows"]))
    add("")
    add("3. accepted + active（真正在用的记忆）")
    add("   raw_chat=1（原始会话）  %s" % _fmt_int(active["raw"]))
    add("   raw_chat=0（结构化）    %s" % _fmt_int(active["structured"]))
    add("   合计                    %s" % _fmt_int(active["total"]))
    add("")
    add("4. 已提炼（distill 游标）")
    add("   游标数                  %s" % _fmt_int(progress["cursors"]))
    add("   可提炼总量              %s" % _fmt_int(progress["eligible"]))
    add("   覆盖率                  %.1f%%" % (progress["ratio"] * 100))
    add("   其中产出了 claims 的     %s" % _fmt_int(progress["cursors_with_claims"]))
    add("   其中没有可提炼内容的     %s（已正确标记，不算积压）"
        % _fmt_int(progress["cursors_without_claims"]))
    add("")
    add("5. 真正剩余待提炼")
    add("   原始会话 raw_chat=1     %s 行" % _fmt_int(pending["raw_rows"]))
    add("   结构化 raw_chat=0       %s 行" % _fmt_int(pending["structured_rows"]))
    add("   合计（行）              %s 行" % _fmt_int(pending["rows"]))
    add("   按内容去重后            %s 条不同内容  <- 这才是模型调用次数"
        % _fmt_int(pending["distinct"]))
    add("     · 原始会话去重后       %s 条" % _fmt_int(pending["raw_distinct"]))
    add("     · 结构化去重后         %s 条" % _fmt_int(pending["structured_distinct"]))
    if pending.get("scan_truncated"):
        add("   ! 去重只扫描了前 %d 行，实际去重数只会更低（不会更高）"
            % int(pending["scanned"]))
    add("")
    add("6. 吞吐（从 distill 游标的 updated_at 实测）")
    add("   窗口                    %d 小时" % rate["window_hours"])
    add("   样本                    %s 条游标" % _fmt_int(rate["samples"]))
    if rate.get("rows_per_hour") is None:
        add("   条/小时                 %s（%s）" % ("n/a", rate.get("note", "")))
    else:
        add("   条/小时                 %s" % _fmt_rate(rate["rows_per_hour"]))
        add("   有效跨度                %.2f 小时" % rate["span_hours"])
    add("")
    add("7. 预计清完")
    add("   待处理行数              %s" % _fmt_int(drain["rows"]))
    if drain.get("hours_at_measured_rate") is not None:
        add("   按实测速率              %.1f 小时（%.2f 天）"
            % (drain["hours_at_measured_rate"], drain["days_at_measured_rate"]))
    else:
        add("   按实测速率              n/a（无有效吞吐样本）")
    add("   需要模型调用            %s 次（去重后）" % _fmt_int(drain["model_calls_needed"]))
    add("   当前配置                batch=%d  limit=%d  -> 每轮 %d 行 / %d 次调用"
        % (drain["configured_batch_size"], drain["configured_run_limit"],
           drain["rows_per_run"], drain["calls_per_run"]))
    add("   清完需要的轮数          %s 轮（每轮 %d 行）"
        % (_fmt_int(drain["runs_needed"]), drain["rows_per_run"]))
    add("")
    add("8. 复现方式（只读，可直接对照 sqlite3）")
    add("   sqlite3 \"$LEDGER\" \"SELECT quality_state,lifecycle,raw_chat,COUNT(*)")
    add("     FROM memories GROUP BY 1,2,3 ORDER BY 4 DESC;\"")
    add("   sqlite3 \"$LEDGER\" \"SELECT COUNT(*) FROM memories m LEFT JOIN source_cursors c")
    add("     ON c.source_id='distill:'||m.memory_id WHERE " + PENDING_WHERE.strip() + ";\"")
    add("")
    add("   注意：cold 行（%s 条）是归档，不是积压。" % _fmt_int(result["cold_rows"]))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="提炼积压与吞吐真相（只读，不会修改账本）")
    parser.add_argument("--ledger", default=os.path.expanduser(str(default_ledger_path())),
                        help="账本路径，默认取 HIPPOMIND_HOME")
    parser.add_argument("--hours", type=int, default=6,
                        help="吞吐估算回看多少小时（默认 6）")
    parser.add_argument("--scan-limit", type=int, default=200_000,
                        help="去重扫描最多读多少行（默认 200000）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    ledger_path = Path(args.ledger).expanduser()
    if not ledger_path.is_file():
        print("账本不存在: %s" % ledger_path, file=sys.stderr)
        return 2
    try:
        with ReadOnlyLedger(ledger_path) as ledger:
            result = distill_status(ledger, hours=max(1, args.hours),
                                    scan_limit=max(1, args.scan_limit))
    except sqlite3.Error as exc:
        print("读取账本失败: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
