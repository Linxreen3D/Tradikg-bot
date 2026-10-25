#!/usr/bin/env bash
# Hängt eine stille, idempotente Zeile an ~/.bashrc und ~/.profile.
# Die Zeile ruft ensure_running.sh auf. Dieses Skript startet den Bot nicht.
# Keine Geheimnisse ausgeben oder loggen.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENSURE="$REPO_DIR/deploy/linux/ensure_running.sh"
MARKER="tradikg-live-ensure"
LOG_FILE="${TRADIKG_LOOP_LOG:-${TRADIKG_HOME:-${HOME:-}/.tradikg}/live/loop.log}"

alarm() {
  local line
  mkdir -p "$(dirname "$LOG_FILE")"
  line="[ALARM] $(date -u '+%Y-%m-%d %H:%M:%S') UTC  $*"
  printf '%s\n' "$line" >>"$LOG_FILE"
  printf '%s\n' "$line" >&2
}

if [ ! -x "$ENSURE" ]; then
  alarm "Installation fehlgeschlagen: ensure_running.sh fehlt oder ist nicht ausführbar."
  exit 1
fi

ENSURE_Q="$(printf '%q' "$ENSURE")"
LINE="[ -n \"\${TRADIKG_ENSURE_DONE:-}\" ] || { TRADIKG_ENSURE_DONE=1; [ -x ${ENSURE_Q} ] && ${ENSURE_Q} >/dev/null 2>&1 || true; } # ${MARKER}"

install_into() {
  local file tmp
  file="$1"
  mkdir -p "$(dirname "$file")" || return 1
  if [ -f "$file" ] && grep -qF "$MARKER" "$file"; then
    if grep -qxF "$LINE" "$file"; then
      return 0
    fi
    tmp="${file}.tradikg-tmp"
    grep -vF "$MARKER" "$file" >"$tmp" || true
    printf '%s\n' "$LINE" >>"$tmp" || return 1
    mv "$tmp" "$file" || return 1
    return 0
  fi
  if [ -s "$file" ]; then
    printf '\n%s\n' "$LINE" >>"$file" || return 1
    return 0
  fi
  touch "$file" || return 1
  printf '%s\n' "$LINE" >>"$file" || return 1
}

HOME_DIR="${HOME:-}"
if [ -z "$HOME_DIR" ]; then
  alarm "Installation fehlgeschlagen: HOME ist nicht gesetzt."
  exit 1
fi

if ! install_into "${HOME_DIR}/.bashrc"; then
  alarm "Installation fehlgeschlagen: Hook konnte nicht nach ${HOME_DIR}/.bashrc geschrieben werden."
  exit 1
fi
if ! install_into "${HOME_DIR}/.profile"; then
  alarm "Installation fehlgeschlagen: Hook konnte nicht nach ${HOME_DIR}/.profile geschrieben werden."
  exit 1
fi

printf '[INFO] Login-Hook steht in %s und %s (eine Zeile, still, kein zweites Mal).\n' \
  "${HOME_DIR}/.bashrc" "${HOME_DIR}/.profile"
printf '[INFO] Bot nicht gestartet. Die nächste Login-Shell ruft ensure_running.sh auf.\n'
