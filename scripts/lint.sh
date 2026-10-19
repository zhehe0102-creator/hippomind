#!/usr/bin/env bash
# Static analysis gate — an incremental ratchet, never a big-bang reformat.
#
# Two rule sets (both configured in pyproject.toml, so this script never
# duplicates the rule list):
#
#   gate    `ruff check .` — the explicit, high-signal set from
#           [tool.ruff.lint].select: syntax errors, undefined names, shadowed
#           imports, import placement, whitespace.  These are the findings that
#           actually break code, so they must not grow.
#   legacy  `ruff check --select E,F,W .` — the wide default set, kept for
#           visibility.  It carries pre-existing debt that is not worth a
#           risky mass edit; pinning its count means the debt cannot grow.
#
# Both rule sets are pinned in scripts/lint-baseline.txt as `path rule count`
# lines.  The gate fails when any (path, rule) pair exceeds its baseline — new
# files therefore start from zero, which is what "new code is clean" means.
# Decreases are reported but do not fail: fixing debt must never require
# touching the baseline in the same change.
#
# Usage:
#   scripts/lint.sh                    # check against the baseline (CI + local)
#   scripts/lint.sh --update-baseline  # re-pin after intentionally fixing debt
#
# ruff is optional locally: when it is absent the script skips with exit 0 so a
# fresh clone can still run tests.  CI installs a pinned version, so CI always
# enforces the gate.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

BASELINE="scripts/lint-baseline.txt"
UPDATE=0
if [ "${1:-}" = "--update-baseline" ]; then
  UPDATE=1
elif [ "$#" -gt 0 ]; then
  echo "lint: unknown argument: $1" >&2
  echo "usage: scripts/lint.sh [--update-baseline]" >&2
  exit 2
fi

if ! command -v ruff >/dev/null 2>&1; then
  echo "lint: SKIP — ruff is not installed."
  echo "lint: install it with: python3 -m pip install ruff"
  exit 0
fi

echo "lint: ruff $(ruff --version | awk '{print $2}')"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hippomind-lint.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

# ruff exits 1 when it finds anything; that is data here, not a script failure.
collect() {
  local label="$1" out="$TMP_DIR/$1.json"
  shift
  ruff check . --output-format=json "$@" >"$out" 2>"$TMP_DIR/$label.err" || true
  if [ -s "$TMP_DIR/$label.err" ]; then
    cat "$TMP_DIR/$label.err" >&2
  fi
  python3 - "$out" <<'PY'
import collections
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    try:
        findings = json.load(handle)
    except ValueError:
        findings = []
counts = collections.Counter()
for item in findings:
    path = item.get("filename") or ""
    try:
        path = os.path.relpath(path)
    except ValueError:
        pass
    counts[(path.replace(os.sep, "/"), str(item.get("code", "")))] += 1
for (path, code), count in sorted(counts.items()):
    print("%s %s %d" % (path, code, count))
PY
}

# Read one `[section]` of the baseline as `path rule count` lines.
read_baseline() {
  local section="$1"
  [ -f "$BASELINE" ] || return 0
  awk -v want="[$section]" '
    /^\[/ { inside = ($0 == want); next }
    inside && NF == 3 { print }
  ' "$BASELINE"
}

# compare <label> <current-file> <baseline-file>
compare() {
  local label="$1" current="$2" baseline_file="$3"
  python3 - "$label" "$current" "$baseline_file" <<'PY'
import sys

label, current_path, baseline_path = sys.argv[1:4]
current = {}
for line in open(current_path, encoding="utf-8"):
    path, code, count = line.split()
    current[(path, code)] = int(count)
baseline = {}
try:
    handle = open(baseline_path, encoding="utf-8")
except OSError:
    handle = None
if handle:
    with handle:
        for line in handle:
            path, code, count = line.split()
            baseline[(path, code)] = int(count)

regressions = {key: (baseline.get(key, 0), value) for key, value in current.items()
               if value > baseline.get(key, 0)}
fixed = {key: (baseline[key], current.get(key, 0)) for key in baseline
         if current.get(key, 0) < baseline[key]}

print("%s: %d finding(s), baseline %d"
      % (label, sum(current.values()), sum(baseline.values())))
for (path, code), (was, now) in sorted(regressions.items()):
    print("  NEW   %s %s: %d -> %d" % (path, code, was, now))
for (path, code), (was, now) in sorted(fixed.items()):
    print("  fixed %s %s: %d -> %d (baseline can be lowered)" % (path, code, was, now))
sys.exit(1 if regressions else 0)
PY
}

if [ "$UPDATE" = "1" ]; then
  {
    echo "# Static-analysis baseline for scripts/lint.sh."
    echo "# Format: <path> <ruff-rule> <count>, grouped by rule set."
    echo "# Regenerate with: scripts/lint.sh --update-baseline"
    echo "#"
    echo "# The gate must ratchet down to zero; legacy debt is pinned so it"
    echo "# cannot grow. Never add entries by hand to silence a new finding."
    echo
    echo "[gate]"
    collect gate
    echo
    echo "[legacy]"
    collect legacy --select E,F,W
  } >"$BASELINE"
  echo "lint: baseline updated -> $BASELINE"
  exit 0
fi

collect gate >"$TMP_DIR/current.gate"
collect legacy --select E,F,W >"$TMP_DIR/current.legacy"
read_baseline gate >"$TMP_DIR/baseline.gate"
read_baseline legacy >"$TMP_DIR/baseline.legacy"

status=0
compare gate "$TMP_DIR/current.gate" "$TMP_DIR/baseline.gate" || status=1
compare legacy "$TMP_DIR/current.legacy" "$TMP_DIR/baseline.legacy" || status=1

if [ "$status" -ne 0 ]; then
  echo
  echo "lint: FAILED — new findings above the baseline."
  echo "lint: fix them, or for genuinely intentional debt run:"
  echo "lint:     scripts/lint.sh --update-baseline"
  echo "lint: and explain the new entries in the commit message."
  exit 1
fi

echo "lint: OK"
exit 0
