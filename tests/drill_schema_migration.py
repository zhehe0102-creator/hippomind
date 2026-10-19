#!/usr/bin/env python3
"""schema 迁移演练：在真实库的副本上验证「打开即迁移」路径（前景轮四）。

流程：
1. 复制生产库三件套到临时目录（原库只读，绝不触碰）
2. 用 SQLiteLedger 打开副本（若 schema 落后则自动迁移）
3. 校验：integrity ok、行数与原库一致、FTS 与应索引集合一致、检索可用
4. 输出耗时报告；退出码非 0 表示迁移后校验失败

用法：
    python3 tests/drill_schema_migration.py            # 用默认生产库
    python3 tests/drill_schema_migration.py <库路径>   # 指定任意库
"""
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        src = Path(argv[0]).expanduser()
    else:
        from hippomind.daemon import default_ledger_path
        src = Path(default_ledger_path())
    if not src.exists():
        print(json.dumps({"error": f"库不存在: {src}"}, ensure_ascii=False))
        return 1

    tmp = tempfile.TemporaryDirectory(prefix="hippomind-migration-drill-")
    root = Path(tmp.name)
    dst = root / src.name
    t0 = time.perf_counter()
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(src) + suffix)
        if f.exists():
            shutil.copy2(f, str(dst) + suffix)
    copy_s = time.perf_counter() - t0

    # 原库基线计数（只读连接）
    raw_src = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    base_mem = raw_src.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    base_evt = raw_src.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
    try:
        base_schema = int(raw_src.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0])
    except sqlite3.OperationalError:
        base_schema = 0  # v1 前无 schema_meta 表
    raw_src.close()

    from hippomind.ledger import SQLiteLedger
    t1 = time.perf_counter()
    led = SQLiteLedger(dst)  # 打开即按需迁移
    open_s = time.perf_counter() - t1

    t2 = time.perf_counter()
    stats = led.stats()
    o = led.health_overview()
    check_s = time.perf_counter() - t2

    t3 = time.perf_counter()
    hits = led.search("记忆", scope="shared", limit=5)
    search_ms = (time.perf_counter() - t3) * 1000

    report = {
        "source": str(src),
        "copied_mb": round(dst.stat().st_size / 1e6, 1),
        "copy_seconds": round(copy_s, 2),
        "open_and_migrate_seconds": round(open_s, 2),
        "verify_seconds": round(check_s, 2),
        "schema_before": base_schema,
        "schema_after": int(stats.get("schema_version", -1)),
        "memories_before": base_mem,
        "memories_after": int(o["memories"]),
        "events_before": base_evt,
        "events_after": int(stats.get("events", 0)),
        "fts": o["fts"]["note"],
        "fts_drift": bool(o["fts"]["drift"]),
        "probe_search_hits": len(hits),
        "probe_search_ms": round(search_ms, 1),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))

    ok = (report["memories_before"] == report["memories_after"]
          and report["events_before"] == report["events_after"]
          and not report["fts_drift"])
    tmp.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
