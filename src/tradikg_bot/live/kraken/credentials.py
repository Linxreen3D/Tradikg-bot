"""Laden der Kraken-API-Zugangsdaten – niemals aus dem Repository.

Quellen (in dieser Reihenfolge):
1. macOS-Schlüsselbund (auf dem Mac, oder wenn ein Lookup übergeben wurde),
   Dienst ``tradikg-kraken``, Konten ``api-key`` und ``api-secret``.
   Anlegen im Terminal (fragt die Werte ab, nichts landet in der Shell-Historie)::

       security add-generic-password -s tradikg-kraken -a api-key -w
       security add-generic-password -s tradikg-kraken -a api-secret -w

2. Umgebungsvariablen ``KRAKEN_API_KEY`` und ``KRAKEN_API_SECRET`` – Fallback, wenn der
   Schlüsselbund nichts enthält (auf Linux der normale Weg, z. B. systemd ``EnvironmentFile``).
3. JSON-Datei, deren Pfad in ``TRADIKG_KRAKEN_CREDENTIALS`` steht
   (``{"api_key": "...", "api_secret": "..."}``). Die Datei muss außerhalb jedes
   Git-Arbeitsverzeichnisses liegen und darf nur für den Besitzer lesbar sein (0600).

Ein unvollständiger Schlüsselbund (nur einer der beiden Werte) wird abgelehnt und nicht mit
Werten aus der Umgebung gemischt. Werte werden nicht geloggt.

Empfohlene Key-Berechtigungen: *Query Funds*, *Query Open Orders & Trades*,
*Query Closed Orders & Trades*, *Create & Modify Orders*, *Cancel/Close Orders*.
**Nicht** vergeben: *Withdraw Funds*, *Deposit Funds*, *Export Data*, Earn/Staking.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ENV_KEY = "KRAKEN_API_KEY"
ENV_SECRET = "KRAKEN_API_SECRET"
ENV_FILE = "TRADIKG_KRAKEN_CREDENTIALS"


class CredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class KrakenCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_secret:
            raise CredentialsError("API-Key und Secret dürfen nicht leer sein")
        try:
            base64.b64decode(self.api_secret, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CredentialsError("API-Secret ist kein gültiges Base64") from exc

    def __repr__(self) -> str:
        return "KrakenCredentials(api_key=***, api_secret=***)"


def _inside_git_worktree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in [path.parent, *path.parent.parents])


KEYCHAIN_SERVICE = "tradikg-kraken"


def _keychain_lookup(account: str) -> str | None:
    """Liest ein Geheimnis aus dem macOS-Schlüsselbund (None, wenn nicht vorhanden)."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _keychain_pair(keychain: Callable[[str], str | None]) -> tuple[str, str] | None:
    """Beide Konten oder nichts. Ein einzelner Treffer ist ein Fehler, kein Mix mit der Umgebung."""
    key = (keychain("api-key") or "").strip()
    secret = (keychain("api-secret") or "").strip()
    if not key and not secret:
        return None
    if not key or not secret:
        raise CredentialsError(
            "macOS-Schlüsselbund unvollständig (Dienst "
            f"'{KEYCHAIN_SERVICE}'): api-key und api-secret müssen beide vorhanden sein."
        )
    return key, secret


def load_credentials(
    env: dict[str, str] | None = None,
    keychain: Callable[[str], str | None] | None = None,
) -> KrakenCredentials:
    env = dict(os.environ) if env is None else env
    # Schlüsselbund zuerst (macOS oder injizierter Lookup). Linux ruft ihn nicht selbst auf.
    if keychain is None and sys.platform == "darwin":
        keychain = _keychain_lookup
    if keychain is not None:
        found = _keychain_pair(keychain)
        if found is not None:
            return KrakenCredentials(*found)
    if env.get(ENV_KEY) or env.get(ENV_SECRET):
        return KrakenCredentials(env.get(ENV_KEY, ""), env.get(ENV_SECRET, ""))
    file_path = env.get(ENV_FILE)
    if not file_path:
        raise CredentialsError(
            f"Keine Zugangsdaten: {ENV_KEY}/{ENV_SECRET}, {ENV_FILE} oder macOS-Schlüsselbund "
            f"(Dienst '{KEYCHAIN_SERVICE}')."
        )
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise CredentialsError(f"Zugangsdatei {path} ({ENV_FILE}) existiert nicht")
    if _inside_git_worktree(path):
        raise CredentialsError(
            f"{path} liegt in einem Git-Arbeitsverzeichnis – Zugangsdaten nur außerhalb speichern."
        )
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise CredentialsError(f"{path} ist für andere lesbar (Modus {oct(mode)}); chmod 600")
    raw = json.loads(path.read_text())
    return KrakenCredentials(raw.get("api_key", ""), raw.get("api_secret", ""))
