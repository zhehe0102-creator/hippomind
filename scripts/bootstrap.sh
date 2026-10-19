#!/usr/bin/env sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
VENV_DIR="$PROJECT_DIR/.venv"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "Python 3.9+ is required." >&2; exit 1; }
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
"$VENV_DIR/bin/python" -m pip install -e "$PROJECT_DIR"
cd "$PROJECT_DIR"
"$VENV_DIR/bin/python" -m hippomind init
echo "Installed. Run: $VENV_DIR/bin/python -m hippomind start"
echo "Then install only detected integrations: $VENV_DIR/bin/python -m hippomind install-agents --auto"
