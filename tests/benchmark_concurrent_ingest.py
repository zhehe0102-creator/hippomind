"""并发 ingest 压测：8 线程经 HTTP 写入，测吞吐与错误率（迭代 83）。"""
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from hippomind.daemon import MemoryHubHTTPServer, MemoryHubService, RuntimePaths


def main() -> int:
    tmp = tempfile.TemporaryDirectory(prefix="hippomind-concurrent-ingest-")
    paths = RuntimePaths.for_ledger(Path(tmp.name) / "hub.sqlite",
                                    Path(tmp.name) / "rt")
    service = MemoryHubService(paths, watch=False)
    server = MemoryHubHTTPServer(("127.0.0.1", 0), service)
    service._server = server
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = service.token

    threads_n, per_thread, batch = 8, 1000, 128
    errors: list = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        sent = 0
        while sent < per_thread:
            n = min(batch, per_thread - sent)
            records = [{"content": f"并发压测 t{tid}-{sent + i}",
                        "kind": "fact", "source_agent": f"stress-{tid}",
                        "evidence": [f"stress:{tid}:{sent + i}"]}
                       for i in range(n)]
            body = json.dumps({"records": records}).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/ingest", data=body,
                headers={"Content-Type": "application/json",
                         "X-HippoMind-Token": token})
            try:
                with urllib.request.urlopen(request, timeout=30) as resp:
                    resp.read()
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"t{tid}@{sent}: {exc}")
            sent += n

    start = time.perf_counter()
    workers = [threading.Thread(target=worker, args=(i,))
               for i in range(threads_n)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    elapsed = time.perf_counter() - start

    total = threads_n * per_thread
    stats = service.stats().get("ledger", {})
    stored = int(stats.get("memories", 0))
    rps = total / elapsed if elapsed else 0.0
    print(json.dumps({
        "threads": threads_n, "records": total, "elapsed_s": round(elapsed, 2),
        "throughput_rps": round(rps, 1), "http_errors": len(errors),
        "stored_memories": stored,
        "durable": stored == total,
    }, ensure_ascii=False))
    ok = not errors and stored == total and rps > 500
    server.shutdown()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
