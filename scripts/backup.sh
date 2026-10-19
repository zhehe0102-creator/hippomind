#!/usr/bin/env bash
# Consistent, checksummed, rotating backup of the HippoMind ledger.
#
# Why not `cp`: the ledger runs in WAL mode, so recent commits live in the
# `-wal` sidecar.  Copying the main file alone silently loses them, and copying
# all three files is not atomic against a live daemon.  Instead this uses
# SQLite's online backup API through `hippomind backup`, which takes a
# transactionally consistent snapshot of a *running* database.
#
# exFAT notes (the ledger may live on a removable volume):
#   * no atomic rename and no hard links are assumed; the snapshot is written
#     directly to its final name and the checksum is written next to it;
#   * the `.sha256` file is plain text in `sha256sum` format so
#     `cd <backup-dir> && shasum -a 256 -c *.sha256` verifies it on macOS or
#     Linux without this script;
#   * rotation uses `rm`, never `mv`.
#
# Usage:
#   scripts/backup.sh                       # default ledger, default backup dir
#   scripts/backup.sh --db /path/ledger.sqlite
#   scripts/backup.sh --out /path/backups --keep 14
#
# Exit codes: 0 success, 1 backup failed, 2 usage error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Keep in sync with hippomind.ledger.default_hub_home (macOS branch).
DEFAULT_DB="$HOME/Library/Application Support/HippoMind/hippomind.sqlite"
DB="$DEFAULT_DB"
OUT_DIR=""
KEEP=7

usage() {
  cat <<'USAGE'
usage: scripts/backup.sh [--db PATH] [--out DIR] [--keep N]

  --db PATH    ledger to snapshot (default: ~/Library/Application Support/HippoMind/hippomind.sqlite)
  --out DIR    directory for snapshots (default: <repo>/backups)
  --keep N     keep the N newest snapshots (default: 7, 0 disables rotation)
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --db)   DB="${2:?--db needs a path}"; shift 2 ;;
    --out)  OUT_DIR="${2:?--out needs a path}"; shift 2 ;;
    --keep) KEEP="${2:?--keep needs a number}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "backup: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$KEEP" in
  ''|*[!0-9]*) echo "backup: --keep must be a non-negative integer: $KEEP" >&2; exit 2 ;;
esac

[ -f "$DB" ] || { echo "backup: ledger not found: $DB" >&2; exit 1; }
OUT_DIR="${OUT_DIR:-$REPO_ROOT/backups}"
mkdir -p "$OUT_DIR"

# Refuse to write a snapshot over the live ledger or into its sidecars.
DB_DIR="$(cd "$(dirname "$DB")" && pwd)"
DB_REAL="$DB_DIR/$(basename "$DB")"
OUT_REAL="$(cd "$OUT_DIR" && pwd)"
if [ "$OUT_REAL" = "$DB_DIR" ]; then
  echo "backup: refusing to write snapshots into the live ledger directory" >&2
  echo "backup: use --out <dir> outside $DB_DIR" >&2
  exit 1
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BASE="hippomind-$STAMP"
SNAPSHOT="$OUT_REAL/$BASE.sqlite"
SUMS="$OUT_REAL/$BASE.sha256"

if [ -e "$SNAPSHOT" ]; then
  echo "backup: snapshot already exists: $SNAPSHOT" >&2
  exit 1
fi

# `hippomind backup` takes the consistent snapshot (SQLite backup API) and
# prints {"backup", "bytes", "sha256"} as JSON.
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "backup: $PYTHON not found (set PYTHON=/path/to/python3)" >&2
  exit 1
fi

echo "backup: source   $DB"
echo "backup: snapshot $SNAPSHOT"

# Run from the repo root so `python3 -m hippomind` resolves without install.
BACKUP_JSON="$(cd "$REPO_ROOT" && "$PYTHON" -m hippomind backup \
  --ledger "$DB" "$SNAPSHOT")" || {
  rc=$?
  echo "backup: snapshot failed (exit $rc)" >&2
  # A half-written snapshot is useless and would confuse rotation.
  rm -f "$SNAPSHOT" "$SUMS"
  exit 1
}

# Prefer the digest the CLI computed while streaming; recompute only if absent.
EXPECTED="$(printf '%s' "$BACKUP_JSON" | "$PYTHON" -c \
  'import json,sys; print(json.load(sys.stdin).get("sha256",""))' 2>/dev/null || true)"

actual_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    "$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  fi
}

ACTUAL="$(actual_sha256 "$SNAPSHOT")"
if [ -z "$ACTUAL" ]; then
  echo "backup: could not hash the snapshot" >&2
  rm -f "$SNAPSHOT"
  exit 1
fi
if [ -n "$EXPECTED" ] && [ "$EXPECTED" != "$ACTUAL" ]; then
  echo "backup: checksum mismatch between CLI and file" >&2
  echo "backup:   cli=$EXPECTED" >&2
  echo "backup:   file=$ACTUAL" >&2
  rm -f "$SNAPSHOT" "$SUMS"
  exit 1
fi

# `sha256sum -c` format: "<digest>  <filename>".  The bare filename keeps the
# file portable when the backup directory is copied elsewhere.
printf '%s  %s\n' "$ACTUAL" "$(basename "$SNAPSHOT")" >"$SUMS"

BYTES="$(wc -c <"$SNAPSHOT" | tr -d ' ')"
echo "backup: bytes    $BYTES"
echo "backup: sha256   $ACTUAL"
echo "backup: checksum $SUMS"

# Rotation: keep the KEEP newest snapshots by name (UTC timestamps sort
# lexicographically).  Each snapshot's `.sha256` is removed with it.
if [ "$KEEP" -gt 0 ]; then
  # shellcheck disable=SC2012  # names are generated, no spaces or newlines
  ALL="$(ls -1 "$OUT_REAL"/hippomind-*.sqlite 2>/dev/null | sort -r || true)"
  COUNT=0
  REMOVED=0
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    COUNT=$((COUNT + 1))
    if [ "$COUNT" -gt "$KEEP" ]; then
      # Sidecars can appear next to a snapshot that something opened read-write
      # (for example an older restore path); remove them with their snapshot.
      rm -f "$file" "${file%.sqlite}.sha256" "$file-shm" "$file-wal"
      REMOVED=$((REMOVED + 1))
    fi
  done <<EOF
$ALL
EOF
  if [ "$REMOVED" -gt 0 ]; then
    echo "backup: rotated $REMOVED old snapshot(s), keeping $KEEP"
  fi
fi

echo "backup: OK"
