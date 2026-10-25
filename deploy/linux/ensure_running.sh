#!/usr/bin/env bash
# Startet deploy/linux/run_live_loop.sh, wenn sie nicht läuft. Idempotent und
# still: Login-Shells (ohne systemd, ohne cron) können das bei jedem Start
# aufrufen. Eine lebende PID in loop.pid beendet den Aufruf, ohne etwas
# auszugeben und ohne eine zweite Schleife. Keine Geheimnisse loggen.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${TRADIKG_REPO:-}" ]; then
  REPO_DIR="$TRADIKG_REPO"
elif [ -f "$SCRIPT_DIR/../../pyproject.toml" ]; then
  REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
else
  REPO_DIR=""
fi

TRADIKG_HOME="${TRADIKG_HOME:-${HOME:-}/.tradikg}"
LIVE_DIR="$TRADIKG_HOME/live"
umask 077
mkdir -p "$LIVE_DIR"
LIVE_DIR="$(cd "$LIVE_DIR" && pwd)"
LOG_FILE="${TRADIKG_LOOP_LOG:-$LIVE_DIR/loop.log}"
PIDFILE="${TRADIKG_LOOP_PIDFILE:-$LIVE_DIR/loop.pid}"
START_LOCK="${TRADIKG_LOOP_START_LOCK:-$LIVE_DIR/loop.start.lock}"
LOOP="${TRADIKG_LOOP_SCRIPT:-$SCRIPT_DIR/run_live_loop.sh}"

alarm() {
  local line
  line="[ALARM] $(date -u '+%Y-%m-%d %H:%M:%S') UTC  $*"
  printf '%s\n' "$line" >>"$LOG_FILE"
  printf '%s\n' "$line" >&2
}

loop_running() {
  local pid
  [ -f "$PIDFILE" ] || return 1
  IFS= read -r pid <"$PIDFILE" || return 1
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  if [ "$pid" -le 1 ]; then
    return 1
  fi
  kill -0 "$pid" 2>/dev/null
}

if [ -z "$REPO_DIR" ]; then
  alarm "Neustart verweigert: Repo nicht gefunden. Kein Neustart."
  exit 2
fi

if [ ! -x "$LOOP" ]; then
  alarm "Installation fehlgeschlagen: Schleife nicht ausführbar ($LOOP)."
  exit 1
fi

if ! command -v nohup >/dev/null 2>&1 || ! command -v setsid >/dev/null 2>&1; then
  alarm "Installation fehlgeschlagen: nohup oder setsid fehlt."
  exit 1
fi

if ! command -v flock >/dev/null 2>&1; then
  alarm "Installation fehlgeschlagen: flock fehlt."
  exit 1
fi

# Serialisiert zwei Login-Shells, die gleichzeitig starten wollen.
exec 9>"$START_LOCK"
if ! flock -w 20 9; then
  exit 0
fi

if loop_running; then
  exit 0
fi

export TRADIKG_REPO="$REPO_DIR"
export TRADIKG_HOME
# Die Schleife schreibt ins Log. Hier nichts ausgeben. setsid löst sie vom Terminal.
# 9>&- : die Kind-Schleife darf die Start-Sperre nicht erben, sonst blockiert
# der nächste Aufruf, bis die Schleife endet.
nohup setsid "$LOOP" >/dev/null 2>&1 </dev/null 9>&- &

i=0
while [ "$i" -lt 50 ]; do
  if loop_running; then
    exit 0
  fi
  sleep 0.1
  i=$((i + 1))
done
exit 0
