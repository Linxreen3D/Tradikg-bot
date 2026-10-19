#!/usr/bin/env bash
# ECHTGELD: Wartende Kauf-Limits des Trend-Bots sofort marktnah kaufen und Stop bei Kraken setzen.
# Kauft nur, wenn die Regel ohnehin kaufen würde (über 200-Tage-Linie, keine Sperre, kein Kill).
#   scripts/live_buy_now.sh            # Budget 20 €, wie start_live.sh
#   scripts/live_buy_now.sh 20 1.0
set -euo pipefail
. "$(dirname "$0")/_common.sh"

BUDGET="${1:-20}"
FRACTION="${2:-1.0}"
BIN="$(tradikg_bin)"
"$(dirname "$0")/stop_live.sh"
"$BIN" live buy-now --budget "$BUDGET" --invest-fraction "$FRACTION" \
  --i-understand-this-uses-real-money || warn "Sofortkauf nicht ausgeführt – siehe Ausgabe oben"
"$(dirname "$0")/start_live.sh" "$BUDGET" "$FRACTION"
"$BIN" live status
