#!/usr/bin/env bash
# Zeigt Prozess-, Dienst- und Kontostatus des Paper-Betriebs.
set -euo pipefail
. "$(dirname "$0")/_common.sh"

if launchd_loaded "$PAPER_LABEL"; then info "launchd: $PAPER_LABEL geladen"; fi
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  info "Prozess läuft (PID $(cat "$PID_FILE"))"
elif [ -f "$TRADIKG_HOME/run/paper.lock" ] && [ -s "$TRADIKG_HOME/run/paper.lock" ]; then
  info "Instanzsperre gehalten von PID $(cat "$TRADIKG_HOME/run/paper.lock")"
else
  info "Kein laufender Paper-Prozess gefunden"
fi
"$(tradikg_bin)" paper status
if [ -f "$TRADIKG_HOME/run/status.json" ]; then
  info "Letzter Status ($TRADIKG_HOME/run/status.json):"
  cat "$TRADIKG_HOME/run/status.json"
fi
