#!/usr/bin/env bash
# Startet den Paper-Dauerbetrieb (über launchd, falls eingerichtet; sonst im Hintergrund).
# Eine zweite Instanz verhindert die Instanzsperre im Programm selbst.
set -euo pipefail
. "$(dirname "$0")/_common.sh"

if launchd_loaded "$PAPER_LABEL"; then
  launchctl kickstart -k "gui/$(id -u)/$PAPER_LABEL"
  info "Paper-Dienst über launchd (neu) gestartet"
  exit 0
fi
BIN="$(tradikg_bin)"
mkdir -p "$TRADIKG_HOME/run" "$TRADIKG_HOME/logs"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  info "Läuft bereits (PID $(cat "$PID_FILE"))"
  exit 0
fi
nohup "$BIN" paper run >>"$TRADIKG_HOME/logs/paper.out.log" 2>&1 &
echo $! > "$PID_FILE"
info "Paper-Betrieb gestartet (PID $!). Logs: $TRADIKG_HOME/logs/"
