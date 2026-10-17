#!/usr/bin/env bash
# Logs des Paper-Betriebs anzeigen.
#   scripts/logs.sh            # letzte 50 Zeilen aller Logs
#   scripts/logs.sh --errors   # nur Warnungen und Fehler (strukturiertes Log + stderr)
#   scripts/logs.sh -f         # laufend mitlesen
set -euo pipefail
. "$(dirname "$0")/_common.sh"

LOG_DIR="$TRADIKG_HOME/logs"
[ -d "$LOG_DIR" ] || fail "Kein Log-Verzeichnis: $LOG_DIR"
case "${1:-}" in
  --errors)
    grep -h -E '"level": "(WARNING|ERROR|CRITICAL)"' "$LOG_DIR"/tradikg.jsonl* 2>/dev/null | tail -n 50 || true
    for f in "$LOG_DIR"/*.err.log; do
      if [ -s "$f" ]; then info "$f"; tail -n 20 "$f"; fi
    done
    ;;
  -f) tail -n 20 -F "$LOG_DIR"/*.log "$LOG_DIR"/tradikg.jsonl ;;
  "") tail -n 50 "$LOG_DIR"/*.log "$LOG_DIR"/tradikg.jsonl ;;
  *) fail "Unbekannte Option: $1 (erlaubt: --errors, -f)" ;;
esac
