#!/usr/bin/env bash
# Lint + Tests (wie in der CI).
set -euo pipefail
. "$(dirname "$0")/_common.sh"

PY="$VENV_DIR/bin/python"
[ -x "$PY" ] || PY="python3"
# Werkzeuge aus der venv (z. B. shellcheck-py) für Tests auffindbar machen
export PATH="$VENV_DIR/bin:$PATH"
cd "$REPO_DIR"
"$PY" -m ruff check src tests
"$PY" -m ruff format --check src tests
"$PY" -m pytest "$@"
