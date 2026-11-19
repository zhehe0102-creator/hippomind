#!/usr/bin/env bash
# Backup/restore drill: prove scripts/backup.sh + scripts/restore.sh round-trip
# on a disposable synthetic ledger, including the tamper-refusal path.
#
# Safety: everything happens under a fresh mktemp dir; HOME and HIPPOMIND_HOME
# are redirected there, and the live ledger path is never a real one.
#
# Usage: scripts/backup_restore_drill.sh [--json]
# Env:   PYTHON=/path/to/python3   (default: python3)
#        KEEP_LOGS=1               keep the temp workdir on success too
# Exit:  0 pass, 1 drill failure, 2 usage error.
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd) || exit 2
PYTHON=${PYTHON:-python3}
JSON=0
for arg in "$@"; do
  case "$arg" in
    --json) JSON=1 ;;
    -h|--help) sed -n "2,18p" "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "backup_restore_drill: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "backup_restore_drill: python not found: $PYTHON" >&2
  exit 2
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/hippomind-drill.XXXXXX") || exit 1
KEEP=0
cleanup() {
  if [ "$KEEP" -eq 0 ]; then rm -rf "$WORK"; else echo "backup_restore_drill: workdir kept: $WORK" >&2; fi
}
trap cleanup EXIT

export HOME="$WORK/home"
export HIPPOMIND_HOME="$WORK/home/hippomind"
export PYTHONPATH="$ROOT"
mkdir -p "$HOME" "$WORK/data" "$WORK/backups"
LEDGER="$WORK/data/hippomind.sqlite"

log() { if [ "$JSON" -eq 0 ]; then printf "%s\n" "$*"; else printf "%s\n" "$*" >&2; fi; }

fail() {
  log "DRILL FAIL: $*"
  printf '{"ok": false, "failed_step": "%s", "workdir": "%s"}\n' "$1" "$WORK"
  KEEP=1
  exit 1
}

# ---------------------------------------------------------------- synthetic ledger
log "[1/6] create synthetic ledger"
CREATE_JSON=$("$PYTHON" - "$LEDGER" <<'PY'
import json, sys
from hippomind.ledger import SQLiteLedger

ledger = SQLiteLedger(sys.argv[1])
try:
    for index in range(12):
        ledger.ingest({
            "kind": "fact",
            "scope": "shared",
            "source_agent": "drill",
            "content": "drill fact %d: rotation window is %02d" % (index, index),
            "evidence": ["drill:%d" % index],
            "claims": [{"subject": "drill.subject", "predicate": "config",
                        "slot_key": "slot-%d" % index, "value": str(index),
                        "confidence": 0.9}],
            "timestamp": "2026-01-01T00:00:%02d+00:00" % index,
        })
    stats = ledger.stats()
finally:
    ledger.close()
print(json.dumps({"memories": stats.get("memories", 0),
                  "events": stats.get("events", 0),
                  "claims": stats.get("claims", 0)}, separators=(",", ":")))
PY
) || fail "create_ledger"
ORIGINAL=$CREATE_JSON
log "  ledger: $ORIGINAL"

# ---------------------------------------------------------------------- backup
log "[2/6] backup"
BACKUP_OUT=$(PYTHON="$PYTHON" bash "$ROOT/scripts/backup.sh" --db "$LEDGER" --out "$WORK/backups" --keep 2 2>&1) || {
  log "$BACKUP_OUT"
  fail "backup"
}
SNAPSHOT=$(ls -1t "$WORK"/backups/*.sqlite 2>/dev/null | head -1)
[ -n "$SNAPSHOT" ] && [ -f "$SNAPSHOT" ] || fail "backup_snapshot_missing"
SUMS="${SNAPSHOT%.sqlite}.sha256"
[ -f "$SUMS" ] || fail "backup_checksum_missing"
( cd "$WORK/backups" && (shasum -a 256 -c ./*.sha256 >/dev/null 2>&1 || sha256sum -c ./*.sha256 >/dev/null 2>&1) ) || fail "backup_checksum_verify"
log "  snapshot: $(basename "$SNAPSHOT")"

# ------------------------------------------------------------- mutate live ledger
log "[3/6] mutate live ledger (restore must revert it)"
("$PYTHON" - "$LEDGER" <<'PY'
import sys
from hippomind.ledger import SQLiteLedger

ledger = SQLiteLedger(sys.argv[1])
try:
    ledger.ingest({
        "kind": "fact",
        "scope": "shared",
        "source_agent": "drill-mutation",
        "content": "mutation row that must disappear after restore",
        "evidence": ["drill:mutation"],
        "timestamp": "2026-01-02T00:00:00+00:00",
    })
finally:
    ledger.close()
PY
) || fail "mutate_ledger"
MUTATED=$("$PYTHON" - "$LEDGER" <<'PY'
import json, sys
from hippomind.ledger import SQLiteLedger

ledger = SQLiteLedger(sys.argv[1])
try:
    stats = ledger.stats()
finally:
    ledger.close()
print(json.dumps({"memories": stats.get("memories", 0),
                  "events": stats.get("events", 0),
                  "claims": stats.get("claims", 0)}, separators=(",", ":")))
PY
) || fail "count_mutated"
log "  mutated: $MUTATED"

# --------------------------------------------------------------------- restore
log "[4/6] restore snapshot"
RESTORE_OUT=$(PYTHON="$PYTHON" bash "$ROOT/scripts/restore.sh" --db "$LEDGER" --force "$SNAPSHOT" 2>&1) || {
  log "$RESTORE_OUT"
  fail "restore"
}
RESTORED=$("$PYTHON" - "$LEDGER" <<'PY'
import json, sys
from hippomind.ledger import SQLiteLedger

ledger = SQLiteLedger(sys.argv[1])
try:
    stats = ledger.stats()
    with ledger._reading() as conn:
        rows = conn.execute("PRAGMA integrity_check").fetchone()[0]
        schema_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
finally:
    ledger.close()
print(json.dumps({"memories": stats.get("memories", 0),
                  "events": stats.get("events", 0),
                  "claims": stats.get("claims", 0),
                  "integrity": str(rows),
                  "schema_version": int(schema_row[0]) if schema_row else 0},
                 separators=(",", ":")))
PY
) || fail "count_restored"
log "  restored: $RESTORED"
printf "%s\n%s\n" "$ORIGINAL" "$RESTORED" | "$PYTHON" -c '
import json, sys
rows = [json.loads(line) for line in sys.stdin if line.strip()]
keys = ("memories", "events", "claims")
sys.exit(0 if all(rows[0].get(k) == rows[1].get(k) for k in keys) else 1)
' || fail "counts_after_restore"
log "  counts match the pre-mutation snapshot"

# -------------------------------------------------------- checkup after restore
log "[5/6] checkup after restore"
CHECKUP_JSON=$("$PYTHON" -m hippomind checkup --ledger "$LEDGER" --format json 2>&1) || {
  log "$CHECKUP_JSON"
  fail "checkup"
}
printf '%s' "$CHECKUP_JSON" | "$PYTHON" -c "import json,sys; json.load(sys.stdin)" || fail "checkup_json"

# ----------------------------------------------------- tamper refusal (negative)
log "[6/6] tampered snapshot must be refused and live ledger untouched"
cp "$SNAPSHOT" "$WORK/tampered.sqlite"
ORIGINAL_SHA=$( (shasum -a 256 "$SNAPSHOT" 2>/dev/null || sha256sum "$SNAPSHOT") | awk '{print $1}' )
printf '%s  tampered.sqlite\n' "$ORIGINAL_SHA" > "$WORK/tampered.sha256"
"$PYTHON" - "$WORK/tampered.sqlite" <<'PY'
import sys
path = sys.argv[1]
with open(path, "r+b") as handle:
    handle.seek(0, 2)
    handle.write(b"X")
PY
LEDGER_SHA_BEFORE=$( (shasum -a 256 "$LEDGER" 2>/dev/null || sha256sum "$LEDGER") | awk '{print $1}' )
if PYTHON="$PYTHON" bash "$ROOT/scripts/restore.sh" --db "$LEDGER" --force "$WORK/tampered.sqlite" >"$WORK/tamper.log" 2>&1; then
  log "  restore unexpectedly accepted a tampered snapshot"
  fail "tamper_refusal"
fi
grep -q "CHECKSUM MISMATCH" "$WORK/tamper.log" || { log "$(cat "$WORK/tamper.log")"; fail "tamper_reason"; }
LEDGER_SHA_AFTER=$( (shasum -a 256 "$LEDGER" 2>/dev/null || sha256sum "$LEDGER") | awk '{print $1}' )
[ "$LEDGER_SHA_BEFORE" = "$LEDGER_SHA_AFTER" ] || fail "tamper_touched_ledger"
log "  tampered snapshot refused; live ledger unchanged"

if [ "$JSON" -eq 1 ]; then
  printf '{"ok": true, "ledger": "%s", "snapshot": "%s", "counts": %s, "checks": ["create_ledger", "backup", "checksum", "mutate", "restore", "counts", "checkup", "tamper_refusal", "tamper_ledger_untouched"]}\n' \
    "$LEDGER" "$(basename "$SNAPSHOT")" "$RESTORED"
else
  log "DRILL PASS: backup -> mutate -> restore -> checkup -> tamper-refusal"
fi
[ "${KEEP_LOGS:-0}" = "1" ] && KEEP=1
exit 0
