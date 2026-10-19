#!/usr/bin/env python3
"""长跑浸泡模拟：压缩验证 daemon 后台循环不卡顿、不涨内存。

模拟数小时 watcher 行为：归档持续追加 → 每轮 sync_once + maintain +
周期性 TRUNCATE 检查点，测量每轮延迟趋势与进程 RSS 是否有界。
"""
from __future__ import annotations

import os
import resource
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from hippomind.ledger import SQLiteLedger
from hippomind.watcher import ArchiveWatcher


def rss_mb() -> float:
    # macOS 的 ru_maxrss 单位是字节，Linux 是 KiB
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024.0 * 1024.0) if sys.platform == "darwin" else raw / 1024.0


def current_rss_mb() -> float:
    # macOS: ps 兜底取当前 RSS（ru_maxrss 是峰值）
    try:
        out = os.popen(f"ps -o rss= -p {os.getpid()}").read().strip()
        return float(out) / 1024.0
    except Exception:
        return 0.0


def main() -> int:
    tmp = tempfile.TemporaryDirectory(prefix="hippomind-soak-")
    root = Path(tmp.name)
    index = root / "index"
    index.mkdir()
    db = index / "index.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE messages (
            agent TEXT, session_id TEXT, event_id TEXT, ts TEXT, role TEXT,
            content TEXT, model TEXT, cwd TEXT, title TEXT, source_file TEXT)""")

    ledger = SQLiteLedger(root / "hub.sqlite")
    watcher = ArchiveWatcher(ledger, root, poll_interval=5)

    cycles = 120
    rows_per_cycle = 40
    latencies = []
    checkpoint_every = 20
    for cycle in range(1, cycles + 1):
        with sqlite3.connect(db) as conn:
            for i in range(rows_per_cycle):
                conn.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("codex", f"s{cycle}", f"evt-{cycle}-{i}",
                     f"2026-08-21T00:{cycle % 60:02d}:{i % 60:02d}Z",
                     "assistant" if i % 2 else "user",
                     f"第 {cycle} 轮第 {i} 条消息：继续部署经验记忆系统验证" * 3,
                     "model", "/project", "soak", "chat.json"))
        started = time.perf_counter()
        summary = watcher.sync_once()
        if cycle % checkpoint_every == 0:
            wal = ledger.maintenance_checkpoint()
            maintain = ledger.maintain(archive_raw=(cycle % 40 == 0))
        else:
            maintain = ledger.maintain(archive_raw=False)
            wal = None
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        if cycle % 30 == 0:
            stats = ledger.stats()
            print(f"cycle {cycle:4d}  latency={elapsed*1000:7.1f}ms  "
                  f"events={stats['events']:6d}  raw={stats['raw_chat']:6d}  "
                  f"peakRSS={rss_mb():6.1f}MB  curRSS={current_rss_mb():6.1f}MB  "
                  f"wal_busy={int(wal['wal_busy']) if wal else '-'}")

    first10 = statistics.mean(latencies[:10])
    last10 = statistics.mean(latencies[-10:])
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    print("-" * 72)
    print(f"cycles={cycles} rows_total={cycles * rows_per_cycle}")
    print(f"latency mean={statistics.mean(latencies)*1000:.1f}ms "
          f"first10={first10*1000:.1f}ms last10={last10*1000:.1f}ms p95={p95*1000:.1f}ms")
    print(f"peakRSS={rss_mb():.1f}MB finalRSS={current_rss_mb():.1f}MB")
    growth_ok = last10 < first10 * 3 + 0.05
    print("latency_trend:", "STABLE" if growth_ok else "DEGRADED")
    tmp.cleanup()
    return 0 if growth_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
