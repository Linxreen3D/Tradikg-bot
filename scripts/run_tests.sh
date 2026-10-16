#!/usr/bin/env bash
# Lint + Tests (wie in der CI).
set -euo pipefail
. "$(dirname "$0")/_common.sh"

PY="$VENV_DIR/bin/python"
[ -x "$PY" ] || PY="python3"
cd "$REPO_DIR"
"$PY" -m ruff check src tests
"$PY" -m ruff format --check src tests
"$PY" -m pytest "$@"
