"""Market Data Engine für Kraken Spot (öffentliche Endpunkte, kein API-Key).

* OHLC-Kerzen für 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w (Kraken liefert max. 720 Kerzen
  pro Abruf). Die letzte von Kraken gelieferte Kerze ist laut Dokumentation noch
  **nicht abgeschlossen** und wird verworfen. Zusätzlich werden alle Kerzen
  entfernt, deren Intervall zum Abrufzeitpunkt noch läuft.
* Inkrementeller Cache in der bestehenden SQLite-Datenhaltung (Tabelle ``bars``).
* Ticker (Bid/Ask/Spread/24h-Volumen) und Orderbuch über das Kraken-Gateway.
* Rate-Limit (Token-Bucket) und Retry mit Backoff für alle Abrufe (nur lesend).
* Erkennung veralteter Daten (``is_stale``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import pandas as pd

from tradikg_bot.data.validation import validate_ohlcv
from tradikg_bot.live.kraken.client import KrakenNotSentError, KrakenUnknownOutcomeError
from tradikg_bot.live.kraken.gateway import parse_pair_rules
from tradikg_bot.live.models import PairRules
from tradikg_bot.market.resilience import TokenBucket, retry_call
from tradikg_bot.storage import SQLiteStore

KRAKEN_INTERVALS = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1w": 10080,
}
FIAT_AND_STABLE = {
    "ZUSD",
    "USD",
    "ZEUR",
    "EUR",
    "ZGBP",
    "GBP",
    "ZCAD",
    "ZJPY",
    "CHF",
    "ZAUD",
    "USDT",
    "USDC",
    "DAI",
    "PYUSD",
    "EURT",
    "TUSD",
    "USDG",
    "RLUSD",
    "EURQ",
    "EURR",
    "USDQ",
    "USDR",
}


class PublicApi(Protocol):
    def public(self, endpoint: str, params: dict[str, Any] | None = None) -> Any: ...


def timeframe_minutes(timeframe: str) -> int:
    try:
        return KRAKEN_INTERVALS[timeframe]
    except KeyError as exc:
        raise ValueError(
            f"Kraken unterstützt {timeframe!r} nicht: {sorted(KRAKEN_INTERVALS)}"
        ) from exc


def is_stale(
    last_bar_open: pd.Timestamp | None, timeframe: str, now: pd.Timestamp, grace: float = 0.5
) -> bool:
    """True, wenn die nächste abgeschlossene Kerze überfällig ist.

    Die letzte bekannte Kerze endet bei ``open + tf``; die nächste schließt bei
    ``open + 2*tf``. Mit ``grace`` (Anteil eines Intervalls) Spielraum für Latenz.
    """
    if last_bar_open is None:
        return True
    tf = pd.Timedelta(minutes=timeframe_minutes(timeframe))
    return now > last_bar_open + 2 * tf + grace * tf


@dataclass(frozen=True)
class Quote:
    pair: str
    bid: float
    ask: float
    last: float
    volume_24h: float  # Basiswährung
    vwap_24h: float
    fetched_at: pd.Timestamp

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 1e4 if mid > 0 else float("inf")

    @property
    def quote_volume_24h(self) -> float:
        return self.volume_24h * self.vwap_24h


class KrakenMarketData:
    def __init__(
        self,
        api: PublicApi,
        store: SQLiteStore | None = None,
        bucket: TokenBucket | None = None,
        now: Callable[[], pd.Timestamp] | None = None,
        sleep: Callable[[float], None] | None = None,
        attempts: int = 4,
    ) -> None:
        self.api = api
        self.store = store
        self.bucket = bucket or TokenBucket(rate=1.0, capacity=2.0)
        self._now = now or (lambda: pd.Timestamp.now(tz="UTC"))
        self._sleep = sleep
        self.attempts = attempts
        self.last_success: pd.Timestamp | None = None
        self.last_error: str | None = None

    # -- Basis --------------------------------------------------------------------------
    def _call(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        def once() -> Any:
            self.bucket.acquire()
            return self.api.public(endpoint, params)

        kwargs: dict[str, Any] = {}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        try:
            result = retry_call(
                once,
                retry_on=(KrakenUnknownOutcomeError, KrakenNotSentError),
                attempts=self.attempts,
                **kwargs,
            )
        except Exception as exc:
            self.last_error = f"{endpoint}: {exc}"
            raise
        self.last_success = self._now()
        self.last_error = None
        return result

    # -- Instrumente ---------------------------------------------------------------------
    def tradable_pairs(self, quote: str = "ZEUR") -> list[PairRules]:
        """Alle aktuell handelbaren Spot-Paare mit dieser Quote-Währung (Status 'online')."""
        result = self._call("AssetPairs")
        rules = []
        for key, raw in result.items():
            if ".d" in key or raw.get("quote") not in (quote, quote.lstrip("Z")):
                continue
            try:
                r = parse_pair_rules(key, raw)
            except (KeyError, ValueError):
                continue
            if r.status == "online" and r.base not in FIAT_AND_STABLE:
                rules.append(r)
        return rules

    # -- Kurse ----------------------------------------------------------------------------
    def ticker(self, pairs: list[str]) -> dict[str, Quote]:
        if not pairs:
            return {}
        result = self._call("Ticker", {"pair": ",".join(pairs)})
        now = self._now()
        quotes: dict[str, Quote] = {}
        for key, raw in result.items():
            quotes[key] = Quote(
                pair=key,
                bid=float(raw["b"][0]),
                ask=float(raw["a"][0]),
                last=float(raw["c"][0]),
                volume_24h=float(raw["v"][1]),
                vwap_24h=float(raw["p"][1]),
                fetched_at=now,
            )
        return quotes

    def fetch_ohlc(
        self, pair: str, timeframe: str, since: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        """Abgeschlossene, validierte Kerzen (UTC, Öffnungszeit als Index)."""
        params: dict[str, Any] = {"pair": pair, "interval": timeframe_minutes(timeframe)}
        if since is not None:
            params["since"] = int(since.timestamp())
        result = self._call("OHLC", params)
        rows = next(v for k, v in result.items() if k != "last")
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(
            [(int(r[0]) * 1000, r[1], r[2], r[3], r[4], r[6]) for r in rows[:-1]],  # letzte: offen
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        if frame.empty:
            return frame.set_index("timestamp")
        bars, _ = validate_ohlcv(frame, timestamp_column="timestamp")
        tf = pd.Timedelta(minutes=timeframe_minutes(timeframe))
        return bars[bars.index + tf <= self._now()]

    def update_cache(self, pair: str, timeframe: str) -> int:
        """Lädt neue abgeschlossene Kerzen in den SQLite-Cache. Gibt die Anzahl zurück."""
        if self.store is None:
            raise RuntimeError("Kein Cache konfiguriert")
        cached = self.store.load_bars(pair, timeframe)
        since = cached.index[-1] if not cached.empty else None
        fresh = self.fetch_ohlc(pair, timeframe, since=since)
        if since is not None:
            fresh = fresh[fresh.index > since]
        if fresh.empty:
            return 0
        return self.store.save_bars(pair, timeframe, fresh, source="kraken")

    def history(self, pair: str, timeframe: str, bars: int = 500) -> pd.DataFrame:
        if self.store is None:
            return self.fetch_ohlc(pair, timeframe).iloc[-bars:]
        return self.store.load_bars(pair, timeframe).iloc[-bars:]
