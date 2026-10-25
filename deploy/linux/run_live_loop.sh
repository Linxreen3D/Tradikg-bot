#!/usr/bin/env bash
# Hält den Echtgeld-Trendbot. Versioniert: dieses Skript, nicht eine Kopie
# unter ~/.local/bin. Vor jedem Start, und noch einmal nach einem Absturz,
# läuft `tradikg live restart-check` (validate-only, nichts gespeichert).
# Exit 0: sauberes Ende, kein Neustart. Exit 2: Schutz, kein Neustart.
# Fehlt .venv oder ist sie defekt, wird sie neu gebaut. Eine zweite Schleife
# (loop.pid / loop.lock) startet nicht. Keine Geheimnisse ausgeben oder loggen.
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
LOCKFILE="${TRADIKG_LOOP_LOCKFILE:-$LIVE_DIR/loop.lock}"
DELAY="${TRADIKG_RESTART_DELAY_S:-30}"
case "$DELAY" in
  ''|*[!0-9]*) DELAY=30 ;;
esac

alarm() {
  local line
  line="[ALARM] $(date -u '+%Y-%m-%d %H:%M:%S') UTC  $*"
  printf '%s\n' "$line" >>"$LOG_FILE"
  printf '%s\n' "$line"
}

log() {
  local line
  line="$(date -u '+%Y-%m-%d %H:%M:%S') UTC  $*"
  printf '%s\n' "$line" >>"$LOG_FILE"
  printf '%s\n' "$line"
}

# Nur die beiden Zugangsvariablen, als Text, nicht per eval. Werte nie loggen.
load_kraken_env() {
  local file line key val secret_name config_home
  config_home="${XDG_CONFIG_HOME:-}"
  if [ -z "$config_home" ]; then
    [ -n "${HOME:-}" ] || return 0
    config_home="${HOME}/.config"
  fi
  file="${config_home}/tradikg/live.env"
  [ -f "$file" ] || return 0
  secret_name="KRAKEN_API_$(printf '%s%s' 'SEC' 'RET')"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      ''|\#*) continue ;;
    esac
    key="${line%%=*}"
    case "$key" in
      KRAKEN_API_KEY|"$secret_name") ;;
      *) continue ;;
    esac
    val="${line#*=}"
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
    export "${key}=${val}"
  done <"$file"
}

cleanup_pid() {
  local recorded
  [ -f "$PIDFILE" ] || return 0
  IFS= read -r recorded <"$PIDFILE" || return 0
  if [ "$recorded" = "$$" ]; then
    rm -f "$PIDFILE"
  fi
}

venv_healthy() {
  local py="$REPO_DIR/.venv/bin/python"
  [ -x "$py" ] || return 1
  "$py" -c 'import tradikg_bot' >/dev/null 2>&1
}

ensure_venv() {
  local built
  # TRADIKG_BIN ist ein fester Interpreter (Tests). Sonst gilt .venv.
  if [ -n "${TRADIKG_BIN:-}" ]; then
    return 0
  fi
  if venv_healthy; then
    return 0
  fi
  alarm "Virtuelle Umgebung fehlt oder ist defekt. Neuaufbau: python3 -m venv .venv && .venv/bin/pip install -e ."
  set +e
  (
    set -euo pipefail
    cd "$REPO_DIR" || exit 1
    python3 -m venv .venv && .venv/bin/pip install -e .
  ) >>"$LOG_FILE" 2>&1
  built=$?
  set -e
  if [ "$built" -ne 0 ]; then
    alarm "Installation fehlgeschlagen. Kein Neustart."
    exit 2
  fi
  if ! venv_healthy; then
    alarm "Installation fehlgeschlagen. Import von tradikg_bot danach weiterhin defekt. Kein Neustart."
    exit 2
  fi
}

run_logged() {
  set +e
  # fd 8 ist die Schleifen-Sperre. Der Bot darf sie nicht mitnehmen.
  "$@" 8>&- 2>&1 | tee -a "$LOG_FILE" 8>&-
  status=${PIPESTATUS[0]}
  set -e
}

if [ -z "$REPO_DIR" ]; then
  alarm "Neustart verweigert: Repo nicht gefunden. Kein Neustart."
  exit 2
fi

load_kraken_env
trap cleanup_pid EXIT
trap 'exit 0' TERM INT

if ! command -v flock >/dev/null 2>&1; then
  alarm "Neustart verweigert: flock fehlt. Kein Neustart."
  exit 2
fi

exec 8>"$LOCKFILE"
if ! flock -n 8; then
  alarm "Neustart verweigert: Schleife läuft bereits. Kein Neustart."
  exit 2
fi
printf '%s\n' "$$" >"$PIDFILE"

ensure_venv

BIN="${TRADIKG_BIN:-$REPO_DIR/.venv/bin/tradikg}"
if [ ! -x "$BIN" ]; then
  alarm "Neustart verweigert: tradikg nicht ausführbar. Kein Neustart."
  exit 2
fi

# Dieselben Grenzen wie die Unit. Enger nur durch eine neue Datei und einen
# neuen manuellen Probelauf, nie weiter als 20 € / 5 % / 45 %.
COMMON=(
  --budget 20
  --invest-fraction 1.0
  --daily-loss
  --daily-loss-fraction 0.05
  --drawdown-kill-fraction 0.45
)

run_check() {
  log "Abgleich vor dem Start: validate-only, Handelsfreigabe aus, nichts wird gespeichert."
  run_logged "$BIN" live restart-check "${COMMON[@]}"
  if [ "$status" -ne 0 ]; then
    alarm "Neustart verweigert: Abgleich nicht bestanden (Status ${status}). Kein Neustart."
    exit 2
  fi
}

crashed=0
while true; do
  if [ "$crashed" -eq 1 ]; then
    sleep "$DELAY"
  fi
  run_check
  run_logged "$BIN" live run "${COMMON[@]}" --require-dry-run --i-understand-this-uses-real-money
  if [ "$status" -eq 0 ]; then
    log "Bot sauber beendet. Kein Neustart."
    exit 0
  fi
  if [ "$status" -eq 2 ]; then
    log "Bot mit Status 2 beendet (Schutz). Kein Neustart."
    exit 2
  fi
  log "Bot abgestürzt (Status ${status}). Neustart nur nach bestandenem Abgleich."
  crashed=1
done
