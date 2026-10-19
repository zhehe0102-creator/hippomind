"""ledger 打开耗时剖析：冷/热启动 × WAL 状态（迭代 87）。

冷启动=子进程 import+打开+首查；肥 WAL 用 wal_autocheckpoint=0 的独立连接
制造并在持有连接时快照三件套，保证被测进程见到真实的未合并 WAL。
"""
import json
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

COLD_SNIPPET = """
import sys, time
sys.path.insert(0, {root!r})
from hippomind.ledger import SQLiteLedger
t0 = time.perf_counter()
led = SQLiteLedger({path!r})
hits = led.search("压测记录", scope="shared", limit=10)
ms = (time.perf_counter() - t0) * 1000
print(f"{{ms:.2f}}|{{len(hits)}}")
"""


def build_library(root: Path, name: str, rows: int,
                  leave_wal: bool) -> tuple[Path, int]:
    """建库；leave_wal 时额外产出含肥 WAL 的快照目录。返回 (测量路径, wal_bytes)。"""
    from hippomind.ledger import SQLiteLedger
    path = root / f"{name}.sqlite"
    led = SQLiteLedger(path)
    batch = [{"content": f"压测记录 {i} 补充文本让行体积接近真实",
              "kind": "fact", "source_agent": "t",
              "evidence": [f"t:{i}"]} for i in range(rows)]
    for i in range(0, rows, 1000):
        led.ingest_many(batch[i:i + 1000])
    if not leave_wal:
        conn = led._connect(write=True)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        return path, 0

    # 制造肥 WAL：autocheckpoint=0 全表触碰 → 持有连接期间快照三件套
    holder = sqlite3.connect(path)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE memories SET touch_count = touch_count + 1")
    writer.commit()
    wal = Path(str(path) + "-wal")
    wal_size = wal.stat().st_size if wal.exists() else 0
    snap_dir = root / f"{name}-wal-snap"
    snap_dir.mkdir(exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src_file = Path(str(path) + suffix)
        if src_file.exists():
            shutil.copy2(src_file, str(snap_dir / path.name) + suffix)
    writer.close()
    holder.close()  # 此刻原库的 WAL 会被合并——无所谓，测的是快照
    return snap_dir / path.name, wal_size


def measure_cold(path: Path, repeats: int = 3) -> float:
    times = []
    for _ in range(repeats):
        out = subprocess.run(
            [sys.executable, "-c", COLD_SNIPPET.format(root=str(PROJECT_ROOT),
                                                       path=str(path))],
            capture_output=True, text=True)
        ms = float(out.stdout.strip().split("|")[0])
        times.append(ms)
    return min(times)


def measure_warm(path: Path, repeats: int = 20) -> float:
    from hippomind.ledger import SQLiteLedger
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        led = SQLiteLedger(path)
        led.search("压测记录", scope="shared", limit=10)
        if hasattr(led, "close"):
            led.close()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def main() -> int:
    tmp = tempfile.TemporaryDirectory(prefix="hippomind-open-profile-")
    root = Path(tmp.name)

    scenarios = [
        ("小库·已checkpoint", 1_000, False),
        ("大库·已checkpoint", 50_000, False),
        ("大库·肥WAL快照", 50_000, True),
    ]
    results = []
    for label, rows, leave_wal in scenarios:
        measure_path, wal_size = build_library(
            root, label.replace("·", "-"), rows, leave_wal)
        cold = measure_cold(measure_path)
        warm_source = measure_path if not leave_wal else \
            root / (label.replace("·", "-") + ".sqlite")
        warm = measure_warm(warm_source)
        entry = {"scenario": label, "rows": rows,
                 "db_mb": round(measure_path.stat().st_size / 1e6, 1),
                 "wal_mb": round(wal_size / 1e6, 1),
                 "cold_ms": round(cold, 1), "warm_ms": round(warm, 2)}
        results.append(entry)
        print(json.dumps(entry, ensure_ascii=False))

    print(json.dumps({"note": "cold=min(3次子进程)；warm=同进程中位(20次)；"
                              "肥WAL 场景每次都从快照恢复三件套"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
