#!/usr/bin/env bash
# Verify-then-restore a HippoMind ledger snapshot produced by scripts/backup.sh.
#
# Safety rules, in order:
#   1. the snapshot must exist and its `.sha256` sidecar must match *before*
#      anything is written — a corrupted or substituted file is refused;
#   2. the daemon must not be serving the target ledger (a restore overwrites it);
#   3. the current ledger is moved aside to a timestamped `.pre-restore` file
#      first, so a bad restore is recoverable without a backup;
#   4. the restore goes through `hippomind restore`, which re-validates schema,
#      required tables and integrity before copying.
#
# exFAT notes: no atomic rename or hard links are assumed.  The pre-restore
# safety copy is a plain copy; the final write is SQLite's own backup API.
#
# Usage:
#   scripts/restore.sh backups/hippomind-20261019T120000Z.sqlite
#   scripts/restore.sh --db /path/live.sqlite snapshot.sqlite
#   scripts/restore.sh --force snapshot.sqlite     # skip the daemon check
#
# Exit codes: 0 restored, 1 refused/failed, 2 usage error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEFAULT_DB="$HOME/Library/Application Support/HippoMind/hippomind.sqlite"
DB="$DEFAULT_DB"
FORCE=0
SNAPSHOT=""

usage() {
  cat <<'USAGE'
usage: scripts/restore.sh [--db PATH] [--force] SNAPSHOT

  SNAPSHOT     a .sqlite file written by scripts/backup.sh
  --db PATH    ledger to restore into (default: ~/Library/Application Support/HippoMind/hippomind.sqlite)
  --force      skip the running-daemon check (the caller guarantees it stopped)
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --db)    DB="${2:?--db needs a path}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "restore: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -n "$SNAPSHOT" ]; then
        echo "restore: only one snapshot may be given" >&2; exit 2
      fi
      SNAPSHOT="$1"; shift ;;
  esac
done

[ -n "$SNAPSHOT" ] || { usage >&2; exit 2; }
[ -f "$SNAPSHOT" ] || { echo "restore: snapshot not found: $SNAPSHOT" >&2; exit 1; }

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || {
  echo "restore: $PYTHON not found (set PYTHON=/path/to/python3)" >&2; exit 1; }

actual_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    "$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  fi
}

# ---------------------------------------------------------------- checksum ---
SUMS="${SNAPSHOT%.sqlite}.sha256"
EXPECTED=""
if [ -f "$SUMS" ]; then
  EXPECTED="$(awk 'NR==1 {print $1}' "$SUMS")"
else
  # Allow a snapshot that was moved away from its sidecar, but require the
  # operator to pass the digest explicitly rather than trusting a bare file.
  if [ -n "${HIPPOMIND_BACKUP_SHA256:-}" ]; then
    EXPECTED="$HIPPOMIND_BACKUP_SHA256"
    echo "restore: using checksum from HIPPOMIND_BACKUP_SHA256"
  else
    echo "restore: no checksum sidecar found: $SUMS" >&2
    echo "restore: refusing to restore an unverified snapshot." >&2
    echo "restore: expected one of:" >&2
    echo "restore:   * <snapshot>.sha256 next to the file (written by scripts/backup.sh)" >&2
    echo "restore:   * HIPPOMIND_BACKUP_SHA256=<digest> in the environment" >&2
    exit 1
  fi
fi

ACTUAL="$(actual_sha256 "$SNAPSHOT")"
if [ "$EXPECTED" != "$ACTUAL" ]; then
  echo "restore: CHECKSUM MISMATCH — refusing to restore." >&2
  echo "restore:   expected $EXPECTED" >&2
  echo "restore:   actual   $ACTUAL" >&2
  echo "restore: the snapshot is corrupt or was replaced." >&2
  exit 1
fi
echo "restore: checksum OK ($ACTUAL)"

# ------------------------------------------------------------ daemon check ---
# `hippomind restore` refuses on its own while a daemon serves this ledger; this
# early check only produces a friendlier message and avoids a wasted copy.
if [ "$FORCE" -eq 0 ] && [ -f "$DB" ]; then
  STATUS_JSON="$(cd "$REPO_ROOT" && "$PYTHON" -m hippomind status --ledger "$DB" 2>/dev/null || true)"
  RUNNING="$(printf '%s' "$STATUS_JSON" | "$PYTHON" -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    print("unknown")
else:
    print("yes" if payload.get("running") else "no")
' 2>/dev/null || echo unknown)"
  if [ "$RUNNING" = "yes" ]; then
    echo "restore: a daemon is serving $DB" >&2
    echo "restore: run 'hippomind stop' first, or pass --force if you know it is down." >&2
    exit 1
  fi
fi

# -------------------------------------------------- pre-restore safety copy ---
DB_DIR="$(dirname "$DB")"
mkdir -p "$DB_DIR"
PRE_RESTORE=""
if [ -f "$DB" ]; then
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  PRE_RESTORE="$DB_DIR/$(basename "$DB").pre-restore-$STAMP"
  echo "restore: saving current ledger -> $PRE_RESTORE"
  cp "$DB" "$PRE_RESTORE"
  # Sidecars belong to the old database; carrying them over would make the
  # restored file look newer than it is.  Preserve them alongside the copy.
  for suffix in -wal -shm; do
    if [ -f "$DB$suffix" ]; then
      cp "$DB$suffix" "$PRE_RESTORE$suffix"
    fi
  done
fi

# ------------------------------------------------------------------ restore ---
# Restore from a throwaway copy, never from the archived snapshot itself.
# `hippomind restore` opens its source through a normal (read-write) SQLite
# connection, which materialises `-shm`/`-wal` sidecars next to it; doing that
# in the backup directory would litter an archive that may be read-only or on
# removable media.  The copy is verified against the same digest, so nothing is
# trusted that the checksum above did not already cover.
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hippomind-restore.XXXXXX")"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT
RESTORE_SOURCE="$WORK_DIR/$(basename "$SNAPSHOT")"
cp "$SNAPSHOT" "$RESTORE_SOURCE"
if [ "$(actual_sha256 "$RESTORE_SOURCE")" != "$ACTUAL" ]; then
  echo "restore: copy of the snapshot did not verify — aborting." >&2
  exit 1
fi

echo "restore: snapshot $SNAPSHOT"
echo "restore: target   $DB"
if ! (cd "$REPO_ROOT" && "$PYTHON" -m hippomind restore \
        --ledger "$DB" --sha256 "$ACTUAL" "$RESTORE_SOURCE"); then
  echo "restore: FAILED — the target ledger was not replaced." >&2
  if [ -n "$PRE_RESTORE" ]; then
    echo "restore: current ledger is still at $DB (pre-restore copy: $PRE_RESTORE)" >&2
  fi
  exit 1
fi

# ------------------------------------------------------------- post-check ---
if (cd "$REPO_ROOT" && "$PYTHON" -m hippomind checkup --ledger "$DB" >/dev/null 2>&1); then
  echo "restore: checkup OK"
else
  echo "restore: WARNING — checkup failed on the restored ledger" >&2
  if [ -n "$PRE_RESTORE" ]; then
    echo "restore: previous ledger preserved at $PRE_RESTORE" >&2
  fi
  exit 1
fi

echo "restore: OK"
if [ -n "$PRE_RESTORE" ]; then
  echo "restore: previous ledger kept at $PRE_RESTORE"
fi
exit 0
