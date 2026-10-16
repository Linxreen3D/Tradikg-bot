#!/usr/bin/env bash
# Prüft Betriebssystem, Architektur, Python, Verzeichnisse und Installation.
# Exit-Code 0 = bereit, 1 = kritischer Fehler. Warnungen brechen nicht ab.
set -euo pipefail
. "$(dirname "$0")/_common.sh"

status=0
os="$(uname -s)"; arch="$(uname -m)"
if [ "$os" = "Darwin" ] && [ "$arch" = "arm64" ]; then
  info "macOS auf Apple Silicon ($arch)"
else
  warn "Zielplattform ist macOS arm64 – gefunden: $os $arch (Entwicklung/CI ok)"
fi
if is_macos; then
  info "macOS-Version: $(sw_vers -productVersion 2>/dev/null || echo unbekannt)"
fi

PY=""
for cand in python3.12 python3.11 python3.13 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    case "$ver" in
      3.11|3.12|3.13) PY="$cand"; info "Python $ver gefunden ($cand)"; break ;;
    esac
  fi
done
if [ -z "$PY" ]; then
  warn "Kein Python 3.11–3.13 gefunden. macOS: brew install python@3.12"
  status=1
fi

mkdir -p "$TRADIKG_HOME" 2>/dev/null || true
if [ -w "$TRADIKG_HOME" ]; then
  info "Datenverzeichnis beschreibbar: $TRADIKG_HOME"
else
  warn "Datenverzeichnis nicht beschreibbar: $TRADIKG_HOME"; status=1
fi
free_kb="$(df -Pk "$TRADIKG_HOME" | awk 'NR==2 {print $4}')"
if [ "${free_kb:-0}" -lt 1048576 ]; then warn "Weniger als 1 GB frei"; fi

if [ -x "$VENV_DIR/bin/tradikg" ]; then
  info "Installation gefunden: $VENV_DIR"
  "$VENV_DIR/bin/tradikg" doctor || status=1
else
  warn "Noch nicht installiert – scripts/setup_mac.sh ausführen"
fi

if is_macos; then
  for label in "$PAPER_LABEL" "$DASH_LABEL"; do
    if launchd_loaded "$label"; then info "launchd-Dienst aktiv: $label"
    else info "launchd-Dienst nicht geladen: $label"; fi
  done
  if pmset -g 2>/dev/null | grep -q " sleep  *0"; then
    info "Ruhezustand (System) ist deaktiviert"
  else
    warn "Mac kann in den Ruhezustand gehen – siehe docs/MACOS_SETUP.md (Energieeinstellungen)"
  fi
fi
exit "$status"
