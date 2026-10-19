#!/usr/bin/env bash
# Hält den Echtgeld-Trendbot. Vor jedem Start, und noch einmal nach einem
# Absturz, läuft `tradikg live restart-check` (validate-only, nichts gespeichert).
# Exit 0: sauberes Ende, kein Neustart. Exit 2: Schutz, kein Neustart.
# Keine Geheimnisse ausgeben oder loggen.
set -euo pipefail

trap 'exit 0' TERM INT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${TRADIKG_REPO:-}" ]; then
  REPO_DIR="$TRADIKG_REPO"
elif [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
  REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  printf '%s\n' "Neustart verweigert: Repo nicht gefunden. Kein Neustart." >&2
  exit 2
fi

BIN="${TRADIKG_BIN:-$REPO_DIR/.venv/bin/tradikg}"
DELAY="${TRADIKG_RESTART_DELAY_S:-30}"
case "$DELAY" in
  ''|*[!0-9]*) DELAY=30 ;;
esac

if [ ! -x "$BIN" ]; then
  printf '%s\n' "Neustart verweigert: tradikg nicht ausführbar. Kein Neustart." >&2
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

log() {
  printf '%s UTC  %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*"
}

run_check() {
  log "Abgleich vor dem Start: validate-only, Handelsfreigabe aus, nichts wird gespeichert."
  set +e
  "$BIN" live restart-check "${COMMON[@]}"
  status=$?
  set -e
  if [ "$status" -ne 0 ]; then
    log "Neustart verweigert: Abgleich nicht bestanden (Status ${status}). Kein Neustart."
    exit 2
  fi
}

crashed=0
while true; do
  if [ "$crashed" -eq 1 ]; then
    sleep "$DELAY"
  fi
  run_check
  set +e
  "$BIN" live run "${COMMON[@]}" --require-dry-run --i-understand-this-uses-real-money
  status=$?
  set -e
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
