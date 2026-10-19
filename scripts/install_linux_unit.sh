#!/usr/bin/env bash
# Installiert die systemd-Benutzerunit. Startet den Bot nicht und liest keine Geheimnisse.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONFIG_HOME/systemd/user"
ENV_FILE="$CONFIG_HOME/tradikg/live.env"
TEMPLATE="$REPO_DIR/deploy/linux/tradikg-live.service"
DEST="$UNIT_DIR/tradikg-live.service"

if [ ! -f "$TEMPLATE" ]; then
  printf '[FEHLER] Vorlage fehlt: %s\n' "$TEMPLATE" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR" "$(dirname "$ENV_FILE")"
if [ ! -e "$ENV_FILE" ]; then
  umask 077
  printf '%s\n' \
    '# Tradikg live – außerhalb des Repos. chmod 600. Nicht committen.' \
    '# Der Bot loggt diese Werte nicht.' \
    'KRAKEN_API_KEY=' \
    'KRAKEN_API_SECRET=' \
    >"$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

python3 -c 'import pathlib,sys; src,dest,repo=sys.argv[1:]; pathlib.Path(dest).write_text(pathlib.Path(src).read_text().replace("@REPO@", repo))' \
  "$TEMPLATE" "$DEST" "$REPO_DIR"

printf '[INFO] Unit: %s\n' "$DEST"
printf '[INFO] EnvironmentFile: %s (chmod 600, außerhalb des Repos)\n' "$ENV_FILE"
printf '[INFO] Linger, damit die Unit ohne Login läuft: sudo loginctl enable-linger "%s"\n' "${USER:-}"
printf '[INFO] Nicht gestartet. Zuerst docs/CUTOVER.md (dry-run), danach systemctl --user start.\n'
