#!/usr/bin/env bash
# Installation auf macOS (Apple Silicon). Löscht niemals bestehende Handelsdaten.
#   scripts/setup_mac.sh                  # venv + Abhängigkeiten + Initialisierung
#   scripts/setup_mac.sh --install-launchd  # zusätzlich Autostart (Paper-Modus) einrichten
set -euo pipefail
. "$(dirname "$0")/_common.sh"

INSTALL_LAUNCHD=0
for arg in "$@"; do
  case "$arg" in
    --install-launchd) INSTALL_LAUNCHD=1 ;;
    *) fail "Unbekannte Option: $arg" ;;
  esac
done

is_macos || warn "Dieses Skript ist für macOS gedacht – fahre trotzdem fort."
[ "$(uname -m)" = "arm64" ] || warn "Keine Apple-Silicon-Architektur erkannt ($(uname -m))."

PY=""
for cand in python3.12 python3.11 python3.13; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || fail "Python 3.11–3.13 fehlt. Installation: brew install python@3.12"
info "Verwende $($PY --version)"

# Bestehende Daten vor einem Update sichern (nie löschen)
if [ -x "$VENV_DIR/bin/tradikg" ] && [ -d "$TRADIKG_HOME/data" ]; then
  info "Sichere bestehende Datenbanken vor dem Update ..."
  "$VENV_DIR/bin/tradikg" backup || warn "Backup fehlgeschlagen – Update wird abgebrochen"
fi

if [ ! -d "$VENV_DIR" ]; then
  info "Erzeuge virtuelle Umgebung: $VENV_DIR"
  "$PY" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
info "Installiere Abhängigkeiten (Kern, ML, Dashboard) ..."
"$VENV_DIR/bin/python" -m pip install -e "${REPO_DIR}[ml,dashboard,dev]"

mkdir -p "$TRADIKG_HOME"
chmod 700 "$TRADIKG_HOME"
"$VENV_DIR/bin/tradikg" init

if [ "$INSTALL_LAUNCHD" = "1" ]; then
  is_macos || fail "launchd gibt es nur unter macOS"
  mkdir -p "$LAUNCH_AGENTS" "$TRADIKG_HOME/logs"
  for label in "$PAPER_LABEL" "$DASH_LABEL"; do
    src="$REPO_DIR/deploy/launchd/$label.plist.template"
    dest="$LAUNCH_AGENTS/$label.plist"
    sed -e "s#__REPO_DIR__#$REPO_DIR#g" -e "s#__VENV_DIR__#$VENV_DIR#g" \
        -e "s#__TRADIKG_HOME__#$TRADIKG_HOME#g" "$src" > "$dest"
    plutil -lint "$dest" >/dev/null || fail "Ungültige plist: $dest"
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$dest"
    info "launchd-Dienst eingerichtet: $label"
  done
fi

info "Fertig. Nächste Schritte: siehe docs/MACOS_SETUP.md"
info "  Research:   $VENV_DIR/bin/tradikg research run --source kraken"
info "  Paper:      scripts/start_bot.sh"
info "  Dashboard:  scripts/start_dashboard.sh  (http://127.0.0.1:8501)"
