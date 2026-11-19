#!/usr/bin/env bash
# Release gate: one command that proves a release is shippable, locally or in
# CI.  Every step prints PASS/FAIL; any failure makes the script exit non-zero.
#
# Steps (in order):
#   1. compileall            every shipped module compiles (syntax gate)
#   2. unittest discover     full regression suite (skipped by --quick)
#   3. scripts/lint.sh       ruff ratchet (script itself skips when ruff absent)
#   4. schema drill          frozen historical ledgers stay readable
#   5. node --check          DSH plugin parses
#   6. node plugin tests     DSH injection-boundary tests
#   7. acceptance audit      the 13 reproduced audit findings
#   8. quality gate          retrieval benchmark fixture vs committed baseline
#                            (recall@k / MRR / update-hit / abstention; small and
#                            fast, so it also runs in --quick mode)
#   9. backup/restore drill  synthetic ledger -> backup -> mutate -> restore ->
#                            checkup -> tampered-snapshot refusal, all in a temp HOME
#
# Usage:
#   scripts/release_check.sh            # full gate (local release check)
#   scripts/release_check.sh --quick    # gate without the full test suite
#
# Environment:
#   PYTHON=/path/to/python    interpreter to use (default: python3)
#   NODE=/path/to/node        node binary (default: node from PATH)
#
# Isolation: HIPPOMIND_HOME and HOME point at a temporary directory so the gate
# can never touch a real ledger or real Agent config, PYTHONDONTWRITEBYTECODE=1
# keeps the checkout clean, and the temporary tree is removed on success (it is
# kept on failure so the per-step logs remain inspectable).
#
# CI runs the same release-gate commands explicitly in
# .github/workflows/ci.yml; this script is the local equivalent plus the
# compile/lint/test floor.

set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd) || exit 2
cd "$ROOT" || exit 2

QUICK=0
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "release_check: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

PYTHON=${PYTHON:-python3}
NODE=${NODE:-node}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "release_check: python interpreter not found: $PYTHON" >&2
  exit 2
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "release_check: Python >= 3.9 required, got: $("$PYTHON" --version 2>&1)" >&2
  exit 2
fi
if ! command -v "$NODE" >/dev/null 2>&1; then
  echo "release_check: node not found: $NODE (required for the DSH plugin gate)" >&2
  exit 2
fi

# Overriding HOME below isolates Agent config and matplotlib caches, but it
# also moves Python's user site-packages.  Dev machines commonly have numpy /
# matplotlib installed with 'pip install --user' (CI installs them into the
# runner environment instead), so preserve the user base explicitly.
ORIGINAL_USER_BASE=${PYTHONUSERBASE:-}
if [ -z "$ORIGINAL_USER_BASE" ]; then
  ORIGINAL_USER_BASE=$("$PYTHON" -c 'import site; print(site.getuserbase())' 2>/dev/null || true)
fi

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/hippomind-release-check.XXXXXX") || {
  echo "release_check: mktemp failed" >&2
  exit 2
}
KEEP_WORK=0
cleanup() {
  if [ "$KEEP_WORK" -eq 0 ]; then
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

export HIPPOMIND_HOME="$WORK_DIR/home/hippomind-home"
export HOME="$WORK_DIR/home/user"
mkdir -p "$HIPPOMIND_HOME" "$HOME" "$WORK_DIR/logs"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT"
if [ -n "$ORIGINAL_USER_BASE" ]; then
  export PYTHONUSERBASE="$ORIGINAL_USER_BASE"
fi

STEP_NO=0
STEPS_RUN=0
STEPS_PASSED=0
STEPS_FAILED=0
STEPS_SKIPPED=0
FAILED_LABELS=()

run_step() {
  local label="$1"
  shift
  STEP_NO=$((STEP_NO + 1))
  STEPS_RUN=$((STEPS_RUN + 1))
  local log="$WORK_DIR/logs/$(printf '%02d' "$STEP_NO").log"
  local start=$SECONDS
  local rc=0
  printf '\n[%02d] %s\n' "$STEP_NO" "$label"
  # Tee keeps the console readable and the log inspectable; pipefail is set
  # above, so PIPESTATUS[0] is the step's real exit code, not tee's.
  "$@" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  local elapsed=$((SECONDS - start))
  if [ "$rc" -eq 0 ]; then
    STEPS_PASSED=$((STEPS_PASSED + 1))
    printf '[PASS] %s (%ss)\n' "$label" "$elapsed"
  else
    STEPS_FAILED=$((STEPS_FAILED + 1))
    FAILED_LABELS+=("$label (exit $rc, log: $log)")
    printf '[FAIL] %s (exit %s, %ss)\n' "$label" "$rc" "$elapsed"
  fi
  return 0
}

skip_step() {
  local label="$1"
  local reason="$2"
  STEP_NO=$((STEP_NO + 1))
  STEPS_SKIPPED=$((STEPS_SKIPPED + 1))
  printf '\n[%02d] %s\n[SKIP] %s (%s)\n' "$STEP_NO" "$label" "$label" "$reason"
}

step_compile() {
  # -x skips exFAT AppleDouble companions (._foo.py are resource-fork blobs,
  # not Python; they exist only on the external checkout volume, never in CI).
  "$PYTHON" -m compileall -q -x '(^|/)\._' hippomind integrations
}

step_unittest() {
  "$PYTHON" -m unittest discover -s tests
}

step_lint() {
  bash scripts/lint.sh
}

step_drill() {
  "$PYTHON" tests/drill_schema_migration.py --json
}

step_node_check() {
  "$NODE" --check integrations/dsh-plugin/lib/index.js
}

step_node_test() {
  "$NODE" tests/test_dsh_plugin.mjs
}

step_acceptance() {
  "$PYTHON" scripts/audit_acceptance.py --json
}

step_quality() {
  "$PYTHON" scripts/eval_benchmark.py --json
}

step_backup_restore() {
  PYTHON="$PYTHON" bash scripts/backup_restore_drill.sh --json
}

printf 'HippoMind release check\n'
printf '  root:   %s\n' "$ROOT"
printf '  python: %s\n' "$("$PYTHON" --version 2>&1)"
printf '  node:   %s\n' "$("$NODE" --version 2>&1)"
printf '  mode:   %s\n' "$([ "$QUICK" -eq 1 ] && echo 'quick (full suite skipped)' || echo 'full')"

run_step "compileall (hippomind + integrations)" step_compile
if [ "$QUICK" -eq 1 ]; then
  skip_step "unittest discover -s tests" "--quick"
else
  run_step "unittest discover -s tests" step_unittest
fi
run_step "lint ratchet (scripts/lint.sh)" step_lint
run_step "schema migration drill" step_drill
run_step "node syntax check (DSH plugin)" step_node_check
run_step "DSH plugin tests" step_node_test
run_step "acceptance audit (13 reproduced findings)" step_acceptance
run_step "retrieval quality gate (benchmark fixture)" step_quality
run_step "backup/restore drill (synthetic ledger)" step_backup_restore

printf '\n================================================================\n'
printf 'release check summary: %d run, %d passed, %d failed, %d skipped\n' \
  "$STEPS_RUN" "$STEPS_PASSED" "$STEPS_FAILED" "$STEPS_SKIPPED"
if [ "$STEPS_FAILED" -gt 0 ]; then
  printf 'FAILED steps:\n'
  for label in "${FAILED_LABELS[@]}"; do
    printf '  - %s\n' "$label"
  done
  KEEP_WORK=1
  printf 'step logs kept in: %s\n' "$WORK_DIR"
  printf 'RELEASE CHECK: FAIL\n'
  exit 1
fi
printf 'RELEASE CHECK: PASS\n'
exit 0
