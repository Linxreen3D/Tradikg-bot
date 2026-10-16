#!/usr/bin/env bash
# Gemeinsame Hilfsfunktionen für alle Skripte (kompatibel mit macOS-bash 3.2).
# shellcheck disable=SC2034

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${TRADIKG_VENV:-$REPO_DIR/.venv}"
TRADIKG_HOME="${TRADIKG_HOME:-$HOME/.tradikg}"
export TRADIKG_HOME
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PAPER_LABEL="com.tradikg.paper"
DASH_LABEL="com.tradikg.dashboard"
PID_FILE="$TRADIKG_HOME/run/paper.pid"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
fail() { printf '[FEHLER] %s\n' "$*" >&2; exit 1; }

is_macos() { [ "$(uname -s)" = "Darwin" ]; }

tradikg_bin() {
  if [ -x "$VENV_DIR/bin/tradikg" ]; then
    printf '%s\n' "$VENV_DIR/bin/tradikg"
  else
    fail "Virtuelle Umgebung fehlt ($VENV_DIR). Zuerst scripts/setup_mac.sh ausführen."
  fi
}

launchd_loaded() {
  is_macos && launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1
}
