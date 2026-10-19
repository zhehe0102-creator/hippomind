#!/bin/zsh
# Double-click starts/reuses the local Memory Hub. Add --gui for the on-demand
# Chinese workbench visualizer, or --network for the full-screen layered
# memory network; the GUI never remains a background dependency.
set -u

PROJECT_DIR="${0:A:h}"
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
fi
OPEN_GUI=0
OPEN_NETWORK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gui) OPEN_GUI=1; shift ;;
    --network) OPEN_NETWORK=1; shift ;;
    *) break ;;
  esac
done

cd "$PROJECT_DIR" || exit 1
"$PYTHON_BIN" -m hippomind.cli start "$@" || exit $?

# Refresh managed Agent integration on each one-click launch. The installer is
# idempotent, preserves user rules, and leaves a rule-based fallback whenever
# a native client hook or the DSH Cordis plugin cannot be verified.
if ! "$PYTHON_BIN" -m hippomind install-agents --agent all; then
  echo "Agent 显式记忆接入刷新失败；Memory Hub 已启动，可稍后运行 install-agents 查看详情。"
fi

# Low-frequency provider discovery: report newly installed runtimes. Unknown
# agents are report-only and are never modified automatically.
"$PYTHON_BIN" -m hippomind discover-agents >/dev/null 2>&1 || true

if [[ "$OPEN_GUI" -eq 1 || "$OPEN_NETWORK" -eq 1 ]]; then
  MPL_CACHE="${TMPDIR:-/tmp}/hippomind-matplotlib"
  mkdir -p "$MPL_CACHE"
  export MPLCONFIGDIR="$MPL_CACHE"
  if ! "$PYTHON_BIN" -c "import matplotlib" >/dev/null 2>&1; then
    echo "无法导入 matplotlib。Memory Hub 已启动；安装 requirements.txt 后可使用 --gui/--network。"
    exit 1
  fi
  if [[ "$OPEN_NETWORK" -eq 1 ]]; then
    exec "$PYTHON_BIN" -m hippomind.main --mode network --one-click
  fi
  exec "$PYTHON_BIN" -m hippomind.main --one-click
fi

echo "HippoMind Memory Hub 已运行。使用 '$0 --gui' 打开工作台，'$0 --network' 打开全屏记忆网络。"
