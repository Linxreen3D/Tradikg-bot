#!/usr/bin/env bash
# Die Schleife liegt versioniert in deploy/linux/run_live_loop.sh.
# Dieser Pfad bleibt, damit bestehende Aufrufe aus dem Repo weiter gelten.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/../deploy/linux/run_live_loop.sh" "$@"
