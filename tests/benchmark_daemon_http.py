#!/usr/bin/env python3
"""daemon HTTP 并发基准：混合读写压测（/ingest //recall //context-pack）。

验证 daemon 路径端到端在并发下的吞吐与延迟分布；与 SQLite 直连压力
（memory_hub_stress.py）互补——这里包含 HTTP 解析、鉴权与请求槽排队。
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from hippomind.daemon import (MemoryHubHTTPServer, MemoryHubService,
                              RuntimePaths)


def main() -> int:
    tmp = tempfile.TemporaryDirectory(prefix="hippomind-http-bench-")
    paths = RuntimePaths.for_ledger(Path(tmp.name) / "hub.sqlite",
                                    Path(tmp.name) / "rt")
    service = MemoryHubService(paths, watch=False)
    server = MemoryHubHTTPServer(("127.0.0.1", 0), service)
    service._server = server
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    token = service.token
    latencies: list = []
    errors: list = []
    lock = threading.Lock()
    stop_at = time.perf_counter() + 8.0
    counters = {"ingest": 0, "recall": 0, "pack": 0}

    def worker(worker_id: int) -> None:
        op_index = 0
        while time.perf_counter() < stop_at:
            op_index += 1
            kind = ("ingest", "recall", "pack")[op_index % 3]
            started = time.perf_counter()
            try:
                if kind == "ingest":
                    body = {"kind": "progress", "scope": "shared",
                            "source_agent": f"bench-{worker_id}",
                            "content": f"基准写入 {worker_id}-{op_index} 部署进展记录",
                            "evidence": [f"bench:{worker_id}:{op_index}"],
                            "task_id": f"task-{worker_id % 4}"}
                    _post(port, token, "/ingest", body)
                elif kind == "recall":
                    _post(port, token, "/recall",
                          {"query": f"部署进展 基准 {worker_id % 4}", "limit": 8})
                else:
                    _post(port, token, "/context-pack",
                          {"query": f"部署进展 基准 {worker_id % 4}"})
                with lock:
                    latencies.append(time.perf_counter() - started)
                    counters[kind] += 1
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{type(exc).__name__}: {str(exc)[:80]}")

    def _post(port_: int, token_: str, path: str, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port_}{path}", data=raw, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-HippoMind-Token", token_)
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()

    workers = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(6)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    total = len(latencies)
    if total:
        ordered = sorted(latencies)
        p50 = ordered[int(total * 0.50)] * 1000
        p95 = ordered[min(total - 1, int(total * 0.95))] * 1000
        elapsed = 8.0
        print(json.dumps({
            "total_requests": total,
            "rps": round(total / elapsed, 1),
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "max_ms": round(ordered[-1] * 1000, 1),
            "errors": len(errors),
            "ops": counters,
        }, ensure_ascii=False))
    else:
        print("no successful requests")
        return 1

    server.shutdown()
    server.server_close()
    tmp.cleanup()
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
