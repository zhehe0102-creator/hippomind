#!/usr/bin/env bash
# Check (and optionally create) a reproducible Python environment for HippoMind.
#
# The workspace lives on an external exFAT volume: Python venv symlinks do not
# work there, so this script uses --copies.  It never touches the production
# ledger; it only prepares an interpreter and dependencies.
#
# Usage:
#   scripts/bootstrap-env.sh            # check only (no writes)
#   scripts/bootstrap-env.sh --create   # create .venv and install requirements
set -euo pipefail
cd "$(dirname "$0")/.." || exit 2

PYTHON="$(printenv PYTHON || true)"
if [ -z "$PYTHON" ]; then PYTHON="python3"; fi
CREATE=0
if [ "$#" -ge 1 ] && [ "$1" = "--create" ]; then
  CREATE=1
elif [ "$#" -ge 1 ]; then
  echo "usage: scripts/bootstrap-env.sh [--create]" >&2
  exit 2
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "找不到 Python 解释器：$PYTHON" >&2
  exit 1
fi
"$PYTHON" - <<'PY'
import sys
print("python", sys.version.split()[0], sys.executable)
if sys.version_info < (3, 9):
    raise SystemExit("HippoMind 需要 Python 3.9 或更新版本")
try:
    import sqlite3
    print("sqlite", sqlite3.sqlite_version)
except Exception as exc:
    raise SystemExit("sqlite3 不可用: %s" % exc)
PY

if [ -x .venv/bin/python ]; then
  echo "已存在 .venv：$(.venv/bin/python --version 2>&1)"
  echo "start-hippomind.command 会优先使用 .venv/bin/python。"
else
  echo "尚未创建 .venv。"
  if [ "$CREATE" -eq 0 ]; then
    echo "创建并安装依赖："
    echo "  scripts/bootstrap-env.sh --create"
    echo "或继续使用系统 Python（当前 daemon 就是这样运行的）。"
  fi
fi

if [ "$CREATE" -eq 1 ]; then
  if [ ! -x .venv/bin/python ]; then
    echo "创建 .venv（--copies，适配 exFAT）..."
    "$PYTHON" -m venv --copies .venv
  fi
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  echo "环境已就绪：.venv/bin/python"
  echo "启动：./start-hippomind.command --web"
fi

if [ -x .venv/bin/python ]; then
  .venv/bin/python -m hippomind.cli version 2>/dev/null || true
else
  "$PYTHON" -m hippomind.cli version 2>/dev/null || true
fi
