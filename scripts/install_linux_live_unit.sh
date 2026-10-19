#!/usr/bin/env bash
# Schreibt die systemd-User-Unit für den Echtgeld-Trendbot.
# Startet den Bot nicht, ruft systemctl nicht auf und schreibt keine Zugangsdaten.
#   scripts/install_linux_live_unit.sh /pfad/ausserhalb/des/repos/kraken.env
# Die Datei muss schon existieren, Modus 0600, und darf nicht in einem Git-Arbeitsverzeichnis liegen.
set -euo pipefail
. "$(dirname "$0")/_common.sh"

if [ "$(uname -s)" != "Linux" ]; then
  fail "Dieses Skript ist nur für Linux."
fi
if [ "$#" -ne 1 ]; then
  fail "Aufruf: scripts/install_linux_live_unit.sh /pfad/zur/kraken.env (chmod 600, außerhalb des Repos). Startet nichts."
fi

ENV_FILE="$(realpath -- "$1")"
if [ ! -f "$ENV_FILE" ]; then
  fail "Datei fehlt: $ENV_FILE – selbst anlegen, chmod 600, nicht ins Repo legen."
fi

dir="$(dirname "$ENV_FILE")"
while [ -n "$dir" ] && [ "$dir" != "/" ]; do
  if [ -e "$dir/.git" ]; then
    fail "EnvironmentFile liegt in einem Git-Arbeitsverzeichnis ($dir)."
  fi
  dir="$(dirname "$dir")"
done

mode="$(stat -c '%a' "$ENV_FILE")"
if [ "$mode" != "600" ]; then
  fail "EnvironmentFile muss Modus 0600 haben (ist $mode)."
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
OUT="$UNIT_DIR/tradikg-live.service"
tmp="$(mktemp "$UNIT_DIR/tradikg-live.service.XXXXXX")"
sed \
  -e "s#__REPO_DIR__#${REPO_DIR}#g" \
  -e "s#__VENV_DIR__#${VENV_DIR}#g" \
  -e "s#__TRADIKG_HOME__#${TRADIKG_HOME}#g" \
  -e "s#__ENV_FILE__#${ENV_FILE}#g" \
  "$REPO_DIR/deploy/systemd/tradikg-live.service.template" >"$tmp"
mv "$tmp" "$OUT"
info "Unit geschrieben nach $OUT (nicht gestartet). Weiter in docs/LINUX_LIVE.md"
