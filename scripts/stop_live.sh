#!/usr/bin/env bash
# Beendet den Echtgeld-Prozess. Stops bei Kraken bleiben bestehen und schützen weiter.
set -euo pipefail
. "$(dirname "$0")/_common.sh"

PID="$TRADIKG_HOME/live/live.pid"
if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then
  kill -TERM "$(cat "$PID")"
  info "Echtbetrieb beendet (PID $(cat "$PID")). Stops bei Kraken bleiben aktiv."
else
  info "Kein laufender Echtbetrieb gefunden"
fi
rm -f "$PID"
