#!/usr/bin/env python3
"""Repeatable short-duration stress check for the SQLite/WAL Memory Hub.

This is deliberately independent of real chat archives. It exercises batched
multi-agent writes, Context Pack reads, maintenance and a reopen cycle. Use a
longer duration in CI or a scheduled job when a 24-hour soak is desired.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Keep the standalone verification command runnable from the repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hippomind.context_pack import ContextPackBuilder, ContextPackOptions
from hippomind.ledger import SQLiteLedger


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="HippoMind Memory Hub short stress check")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--writers", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args(argv)
    seconds = max(0.5, min(86_400.0, args.seconds))
    writers = max(1, min(32, args.writers))
    batch_size = max(1, min(256, args.batch_size))
    with tempfile.TemporaryDirectory(prefix="hippomind-stress-") as root:
        path = os.path.join(root, "hub.sqlite")
        ledger = SQLiteLedger(path)
        writer_ledgers = [SQLiteLedger(path) for _ in range(writers)]
        started = time.perf_counter()
        rounds = 0

        def write(worker: int, round_number: int) -> int:
            rows = []
            for index in range(batch_size):
                event_id = f"stress-{worker}-{round_number}-{index}"
                rows.append({
                    "kind": "progress", "content": f"writer {worker} progress {round_number}-{index}",
                    "source_agent": f"agent-{worker}", "session_id": f"session-{worker}",
                    "task_id": f"task-{worker % 3}", "project": "stress", "event_id": event_id,
                    "timestamp": f"2026-08-16T00:{round_number % 60:02d}:{index % 60:02d}Z",
                    "scope": "shared", "evidence": ["stress:" + event_id],
                    "context": {"progress": [f"round {round_number}"],
                                "next_steps": ["verify concurrent WAL writes"]},
                })
            return sum(result.inserted for result in writer_ledgers[worker].ingest_many(rows))

        inserted = 0
        with ThreadPoolExecutor(max_workers=writers) as pool:
            while time.perf_counter() - started < seconds:
                inserted += sum(pool.map(lambda worker: write(worker, rounds), range(writers)))
                rounds += 1
        write_elapsed = time.perf_counter() - started
        read_started = time.perf_counter()
        builder = ContextPackBuilder(ledger)
        reads = 0
        for _ in range(100):
            pack = builder.build("progress", ContextPackOptions(max_items=8, max_chars=5000))
            if len(pack["text"]) > 5000:
                raise RuntimeError("Context Pack exceeded its budget")
            reads += 1
        maintenance = ledger.maintain(active_limit=50_000)
        reopened = SQLiteLedger(path)
        elapsed = time.perf_counter() - started
        read_elapsed = time.perf_counter() - read_started
        stats = reopened.stats()
        print({"seconds": round(elapsed, 3), "write_seconds": round(write_elapsed, 3),
               "read_seconds": round(read_elapsed, 3), "rounds": rounds, "inserted": inserted,
               "events": stats["events"], "reads": reads, "journal": stats["journal_mode"],
               "maintenance": maintenance, "writes_per_second": round(inserted / write_elapsed, 1)})
        if stats["events"] != inserted or stats["journal_mode"] != "WAL":
            raise RuntimeError("stress verification failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
