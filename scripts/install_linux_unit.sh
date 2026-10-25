#!/usr/bin/env bash
# Installiert die systemd-Benutzerunit. Startet den Bot nicht und liest keine Geheimnisse.
# Die Unit ist die Option, wo systemd --user läuft. Ohne systemd: install_login_hook.sh.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONFIG_HOME/systemd/user"
ENV_FILE="$CONFIG_HOME/tradikg/live.env"
TEMPLATE="$REPO_DIR/deploy/linux/tradikg-live.service"
DEST="$UNIT_DIR/tradikg-live.service"
LOOP_SRC="$REPO_DIR/deploy/linux/run_live_loop.sh"
LOOP_DEST="${HOME}/.local/bin/tradikg_run_live_loop.sh"

if [ ! -f "$TEMPLATE" ]; then
  printf '[FEHLER] Vorlage fehlt: %s\n' "$TEMPLATE" >&2
  exit 1
fi

if [ ! -x "$LOOP_SRC" ]; then
  printf '[FEHLER] Schleife fehlt oder ist nicht ausführbar: %s\n' "$LOOP_SRC" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR" "$(dirname "$ENV_FILE")" "$(dirname "$LOOP_DEST")"
if [ ! -e "$ENV_FILE" ]; then
  (
    umask 077
    printf '%s\n' \
      '# Tradikg live – außerhalb des Repos. chmod 600. Nicht committen.' \
      '# Der Bot loggt diese Werte nicht.' \
      'KRAKEN_API_KEY=' \
      'KRAKEN_API_SECRET=' \
      >"$ENV_FILE"
  )
fi
chmod 600 "$ENV_FILE"

python3 -c 'import pathlib, sys; src, dest, repo = sys.argv[1:]; pathlib.Path(dest).write_text(pathlib.Path(src).read_text().replace("@REPO@", repo))' \
  "$TEMPLATE" "$DEST" "$REPO_DIR"

python3 -c 'import pathlib, shlex, sys
dest, repo, src = sys.argv[1:]
quoted_args = chr(34) + chr(36) + "@" + chr(34)
text = """#!/usr/bin/env bash
# Erzeugt von scripts/install_linux_unit.sh. Ruft die versionierte Schleife.
set -euo pipefail
export TRADIKG_REPO=%s
exec %s %s
""" % (shlex.quote(repo), shlex.quote(src), quoted_args)
path = pathlib.Path(dest)
path.write_text(text)
path.chmod(0o755)
' "$LOOP_DEST" "$REPO_DIR" "$LOOP_SRC"

printf '[INFO] Unit: %s\n' "$DEST"
printf '[INFO] Schleife: %s\n' "$LOOP_SRC"
printf '[INFO] Bisheriger Pfad ruft dieselbe Schleife: %s\n' "$LOOP_DEST"
printf '[INFO] EnvironmentFile: %s (chmod 600, außerhalb des Repos)\n' "$ENV_FILE"
printf '[INFO] Linger, damit die Unit ohne Login läuft: sudo loginctl enable-linger "%s"\n' "${USER:-}"
printf '[INFO] Ohne systemd und ohne cron: %s\n' "$REPO_DIR/deploy/linux/install_login_hook.sh"
printf '[INFO] Nicht gestartet. Zuerst docs/CUTOVER.md (dry-run), danach systemctl --user start.\n'
