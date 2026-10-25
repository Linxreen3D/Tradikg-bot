#!/usr/bin/env bash
# Startet den ECHTGELD-Betrieb (Trendfolge, kleines Budget) im Hintergrund.
#   scripts/start_live.sh            # 20 € Budget, 100 % investiert
#   scripts/start_live.sh 20 1.0     # Budget, Anteil
set -euo pipefail
. "$(dirname "$0")/_common.sh"

BUDGET="${1:-20}"
FRACTION="${2:-1.0}"
BIN="$(tradikg_bin)"
LIVE_DIR="$TRADIKG_HOME/live"
PID="$LIVE_DIR/live.pid"
mkdir -p "$LIVE_DIR"
if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then
  info "Echtbetrieb läuft bereits (PID $(cat "$PID"))"
  exit 0
fi
nohup "$BIN" live run --budget "$BUDGET" --invest-fraction "$FRACTION" \
  --i-understand-this-uses-real-money >>"$LIVE_DIR/live.out.log" 2>&1 &
echo $! > "$PID"
info "ECHTBETRIEB gestartet (PID $!, Budget $BUDGET €). Status: $BIN live status"
