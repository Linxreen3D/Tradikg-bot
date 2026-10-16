"""Minimaler, abhängigkeitsfreier Kraken-Spot-REST-Client.

Sicherheitsmerkmale:
* **Endpunkt-Allowlist:** Nur die unten aufgeführten privaten Endpunkte sind
  aufrufbar. Auszahlungen, Einzahlungen, Wallet-Transfers, Margin-Funktionen
  und Earn/Staking sind nicht erreichbar.
* **Parameter-Sperren für AddOrder:** ``leverage``, ``reduce_only``,
  ``settle-position`` und ``margin`` werden vor dem Senden abgelehnt.
* **Fehlerklassen:** Eindeutige Ablehnungen (``KrakenRejectedError``) werden
  von unklaren Ergebnissen (``KrakenUnknownOutcomeError``: Timeout, Abbruch
  nach dem Senden, 5xx, ``EService:*``) unterschieden. Bei unklarem Ergebnis
  darf eine Order **nicht** blind wiederholt werden.
* Secrets erscheinen nie in ``repr``, Logs oder Fehlermeldungen.

Signatur (offizielle Kraken-Spezifikation):
``API-Sign = base64(HMAC-SHA512(base64decode(secret), uri_path + SHA256(nonce + postdata)))``
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from tradikg_bot.live.kraken.credentials import KrakenCredentials

API_URL = "https://api.kraken.com"
API_VERSION = "0"

PUBLIC_ENDPOINTS = frozenset({"Time", "SystemStatus", "AssetPairs", "Ticker", "Depth", "OHLC"})

# Lesende private Endpunkte
PRIVATE_READ_ENDPOINTS = frozenset(
    {
        "Balance",
        "BalanceEx",
        "OpenOrders",
        "ClosedOrders",
        "QueryOrders",
        "OpenPositions",  # nur um zu prüfen, dass KEINE Margin-Positionen existieren
        "TradeVolume",  # tatsächliche Gebühren des Kontos
        "WithdrawMethods",  # nur als Berechtigungsprobe (erwartet: Permission denied)
    }
)
# Schreibende private Endpunkte (nur Orders, keine Geldbewegungen)
PRIVATE_TRADE_ENDPOINTS = frozenset({"AddOrder", "CancelOrder"})
PRIVATE_ENDPOINTS = PRIVATE_READ_ENDPOINTS | PRIVATE_TRADE_ENDPOINTS

FORBIDDEN_ORDER_PARAMS = frozenset({"leverage", "reduce_only", "margin"})
ALLOWED_ORDER_TYPES = frozenset(
    {"limit", "market", "stop-loss", "take-profit", "stop-loss-limit", "take-profit-limit"}
)

# Fehler, bei denen Kraken die Anfrage sicher NICHT verarbeitet hat.
_REJECT_PREFIXES = (
    "EOrder:",
    "EGeneral:Invalid arguments",
    "EGeneral:Permission denied",
    "EAPI:Invalid key",
    "EAPI:Invalid signature",
    "EAPI:Invalid nonce",
    "EAPI:Rate limit exceeded",
    "EAPI:Feature disabled",
    "EQuery:",
    "EFunding:",
)


class KrakenError(RuntimeError):
    pass


class KrakenRejectedError(KrakenError):
    """Anfrage wurde eindeutig abgelehnt (keine Wirkung)."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class KrakenUnknownOutcomeError(KrakenError):
    """Ergebnis unbekannt – die Anfrage könnte verarbeitet worden sein."""


class KrakenNotSentError(KrakenError):
    """Verbindung kam nicht zustande – die Anfrage hat Kraken sicher nicht erreicht."""


class ForbiddenOperationError(KrakenError):
    """Lokale Sicherheitssperre (Endpunkt/Parameter nicht erlaubt)."""


def sign(uri_path: str, data: dict[str, Any], secret_b64: str) -> str:
    postdata = urllib.parse.urlencode(data)
    message = (str(data["nonce"]) + postdata).encode()
    sha256 = hashlib.sha256(message).digest()
    mac = hmac.new(base64.b64decode(secret_b64), uri_path.encode() + sha256, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> HttpResponse: ...


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> HttpResponse:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    sent = False
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            sent = True
            return HttpResponse(resp.status, resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read() or b"")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)) and not sent:
            raise KrakenNotSentError(f"Verbindung fehlgeschlagen: {reason}") from exc
        raise KrakenUnknownOutcomeError(f"Netzwerkfehler: {reason}") from exc
    except (TimeoutError, ConnectionResetError) as exc:
        raise KrakenUnknownOutcomeError(f"Timeout/Abbruch: {exc}") from exc


class KrakenClient:
    def __init__(
        self,
        credentials: KrakenCredentials | None = None,
        nonce_source: Callable[[], int] | None = None,
        transport: Transport = urllib_transport,
        base_url: str = API_URL,
        timeout: float = 10.0,
        allow_trading: bool = False,
    ) -> None:
        self._credentials = credentials
        self._nonce_source = nonce_source
        self._transport = transport
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.allow_trading = allow_trading

    def __repr__(self) -> str:  # niemals Secrets ausgeben
        has_key = self._credentials is not None
        return f"KrakenClient(base_url={self.base_url!r}, credentials={'***' if has_key else None})"

    # -- öffentlich ------------------------------------------------------------
    def public(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if endpoint not in PUBLIC_ENDPOINTS:
            raise ForbiddenOperationError(f"Öffentlicher Endpunkt nicht erlaubt: {endpoint}")
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.base_url}/{API_VERSION}/public/{endpoint}{query}"
        resp = self._transport("GET", url, {"User-Agent": "tradikg-bot"}, None, self.timeout)
        return self._parse(resp)

    # -- privat ----------------------------------------------------------------
    def private(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if endpoint not in PRIVATE_ENDPOINTS:
            raise ForbiddenOperationError(f"Privater Endpunkt gesperrt: {endpoint}")
        validate_only = endpoint == "AddOrder" and str((params or {}).get("validate")) == "true"
        if endpoint in PRIVATE_TRADE_ENDPOINTS and not self.allow_trading and not validate_only:
            raise ForbiddenOperationError(
                f"{endpoint} ist gesperrt: Client wurde ohne Handelsfreigabe erzeugt."
            )
        if endpoint == "AddOrder":
            self._check_order_params(params or {})
        if self._credentials is None or self._nonce_source is None:
            raise ForbiddenOperationError("Private Endpunkte benötigen Zugangsdaten und Nonce")
        data: dict[str, Any] = {"nonce": self._nonce_source(), **(params or {})}
        uri_path = f"/{API_VERSION}/private/{endpoint}"
        headers = {
            "API-Key": self._credentials.api_key,
            "API-Sign": sign(uri_path, data, self._credentials.api_secret),
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "tradikg-bot",
        }
        body = urllib.parse.urlencode(data).encode()
        resp = self._transport("POST", self.base_url + uri_path, headers, body, self.timeout)
        return self._parse(resp)

    @staticmethod
    def _check_order_params(params: dict[str, Any]) -> None:
        bad = FORBIDDEN_ORDER_PARAMS & set(params)
        if bad:
            raise ForbiddenOperationError(f"Verbotene Order-Parameter: {sorted(bad)}")
        if params.get("ordertype") not in ALLOWED_ORDER_TYPES:
            raise ForbiddenOperationError(f"Ordertyp nicht erlaubt: {params.get('ordertype')}")
        close_type = params.get("close[ordertype]")
        if close_type is not None and close_type not in ALLOWED_ORDER_TYPES:
            raise ForbiddenOperationError(f"Close-Ordertyp nicht erlaubt: {close_type}")

    @staticmethod
    def _parse(resp: HttpResponse) -> Any:
        if resp.status >= 500:
            raise KrakenUnknownOutcomeError(f"HTTP {resp.status}")
        try:
            payload = json.loads(resp.body.decode() or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            if resp.status >= 400:
                raise KrakenRejectedError([f"HTTP {resp.status}"]) from exc
            raise KrakenUnknownOutcomeError("Antwort nicht lesbar") from exc
        errors = [str(e) for e in payload.get("error", [])]
        if errors:
            if all(e.startswith(_REJECT_PREFIXES) for e in errors):
                raise KrakenRejectedError(errors)
            raise KrakenUnknownOutcomeError("; ".join(errors))
        if resp.status >= 400:
            raise KrakenRejectedError([f"HTTP {resp.status}"])
        if "result" not in payload:
            raise KrakenUnknownOutcomeError("Antwort ohne 'result'")
        return payload["result"]
