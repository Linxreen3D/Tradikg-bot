#!/usr/bin/env bash
# Beendet den Paper-Dauerbetrieb geordnet (SIGTERM). Offene Paper-Positionen bleiben
# im Zustand gespeichert und werden beim nächsten Start abgeglichen.
set -euo pipefail
. "$(dirname "$0")/_common.sh"

if launchd_loaded "$PAPER_LABEL"; then
  launchctl bootout "gui/$(id -u)/$PAPER_LABEL" || true
  info "launchd-Dienst gestoppt (Autostart erst nach scripts/setup_mac.sh --install-launchd)"
fi
if [ -f "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    info "Prozess $pid beendet"
  fi
  rm -f "$PID_FILE"
fi
