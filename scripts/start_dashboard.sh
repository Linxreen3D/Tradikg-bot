#!/usr/bin/env bash
# Startet das Dashboard ausschließlich lokal (127.0.0.1).
set -euo pipefail
. "$(dirname "$0")/_common.sh"

PORT="${TRADIKG_DASHBOARD_PORT:-8501}"
[ -x "$VENV_DIR/bin/streamlit" ] || fail "Streamlit fehlt – scripts/setup_mac.sh ausführen"
info "Dashboard: http://127.0.0.1:$PORT (nur lokal erreichbar)"
exec "$VENV_DIR/bin/streamlit" run "$REPO_DIR/src/tradikg_bot/dashboard/app.py" \
  --server.address 127.0.0.1 --server.port "$PORT" --server.headless true \
  --browser.gatherUsageStats false --client.toolbarMode minimal
