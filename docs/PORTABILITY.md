# Cross-Computer Setup

## What moves

Use `hippomind export portable.json` on the source computer and `hippomind import portable.json` on the destination. The export contains structured evidence events only. It excludes raw chats, tool or system events, local watcher cursors, daemon state, tokens and agent credentials. The destination creates its own token and installs only integrations it can detect.

## macOS and Linux

```sh
git clone <your-github-repository-url> hippocampus-agent
cd hippocampus-agent
./scripts/bootstrap.sh
.venv/bin/python -m hippomind start
.venv/bin/python -m hippomind install-agents --auto
```

On macOS, `./start-hippomind.command` also uses `.venv/bin/python` when present. On Linux and macOS, data defaults to `~/.local/state/hippomind` and `~/Library/Application Support/HippoMind` respectively.

## Windows

```powershell
git clone <your-github-repository-url> hippocampus-agent
cd hippocampus-agent
.\scripts\bootstrap.ps1
.\.venv\Scripts\python.exe -m hippomind start
.\.venv\Scripts\python.exe -m hippomind install-agents --auto
```

Windows data defaults to `%APPDATA%\HippoMind`. Python 3.9 or later is required on every platform.

## External disks and recovery

Set `HIPPOMIND_HOME` to a private, writable folder before starting the service when the ledger should live on an encrypted external disk. Run `hippomind backup <folder>` before transfer and `hippomind recovery-check` after restore. Do not copy a running SQLite database with Finder; stop the daemon first or use the built-in backup.

## Cross-machine migration via export/import (v1.0)

Two supported paths:

### 1. Whole-file backup (same OS family, daemon stopped)

```bash
hippomind stop                                   # or kill -TERM <pid>
# macOS default ledger: ~/Library/Application Support/HippoMind/memory.sqlite
# Linux default ledger: ~/.local/state/hippomind/memory.sqlite
hippomind backup --ledger <ledger-path> ~/mem-backup.sqlite   # output is positional
# -> {"backup": "...", "bytes": ..., "sha256": "..."}

# on the new machine (input is positional)
hippomind restore --ledger <new-ledger-path> \
    --sha256 <sha256-from-backup> ~/mem-backup.sqlite
```

The `--sha256` guard refuses to restore a corrupted or swapped file. On
Windows PowerShell the same commands work with backslash paths, e.g.
`hippomind backup --ledger $env:APPDATA\HippoMind\memory.sqlite $HOME\mem-backup.sqlite`.

### 2. JSONL export/import (cross-version, selective, idempotent)

```bash
hippomind export --ledger old.sqlite dump.jsonl                # output is positional
# add --include-raw-chat for raw evidence; --since <ISO> exports only newer rows;
# --encrypt --passphrase ... protects the file in transit (import auto-detects)
scp dump.jsonl newhost:
hippomind import --ledger new.sqlite dump.jsonl                # input is positional
# re-run is a no-op (content_hash dedupe); each import is stamped with an
# import_batch so `hippomind forget --batch <id>` can roll it back
```

Windows PowerShell: replace `scp` with your transfer method and use
`$env:USERPROFILE\dump.jsonl` style paths — flags are identical.

Measured on 10k rows: export ~0.1 s, import ~0.8 s, re-import fully
deduplicated (see docs/benchmarks/roundtrip-2026-08-21.md). Verify the
result with `hippomind checkup` — FTS sync and claim consistency are
rebuilt/validated as part of normal maintenance.

## LAN 只读镜像（第二台机器巡检/展示）

主库所在机器是唯一写入方；其他机器挂载**只读副本**：

```bash
# 主机：定时快照（登记在册，可被 --prune-backups 轮转）
hippomind backup ~/mirror/hippomind.sqlite

# 镜像机：rsync 拉取后以只读模式起本地 daemon
rsync -a mainhost:mirror/hippomind.sqlite* ~/mirror/
hippomind start --ledger ~/mirror/hippomind.sqlite --read-only
```

`--read-only` 下所有凭据（含管理员 token）降级为共享只读视图：
GET /health、/stats、/metrics、recall/context-pack 正常，
POST /ingest、/feedback、/sync、/admin/reload 一律 403——镜像机物理上
不可能误写主库。注意 rsync 必须包含 `-wal`/`-shm` 或先在主机 `stop`
（更稳妥：直接传 backup 产物，它天然是一致性快照）。
