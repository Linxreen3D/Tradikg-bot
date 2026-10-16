"""Datenquellen für historische Kurse.

* CSV-Import (offline)
* Synthetische Daten (offline, reproduzierbar – für Tests/Demos, **keine** Marktdaten)
* Optionale kostenlose Online-Quellen ohne API-Schlüssel:
  - Krypto: öffentliche OHLCV-Endpunkte via ``ccxt`` (z. B. Binance, Kraken)
  - Aktien: Yahoo Finance via ``yfinance``

Die Online-Adapter werden nur bei Bedarf importiert (``pip install -e '.[data]'``).
Es werden ausschließlich öffentliche Marktdaten gelesen; es gibt keine Order-Funktionen.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import numpy as np
import pandas as pd

from tradikg_bot.data.validation import canonical_index, normalize_ohlcv

_TF_PATTERN = re.compile(r"^(\d+)([mhdw])$")
_TF_UNITS = {"m": "min", "h": "h", "d": "D", "w": "W"}


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    """'15m', '1h', '4h', '1d', '1w' -> Timedelta."""
    match = _TF_PATTERN.match(timeframe.strip().lower())
    if not match:
        raise ValueError(f"Unbekanntes Zeitintervall: {timeframe!r}")
    amount, unit = match.groups()
    if unit == "w":
        return pd.Timedelta(weeks=int(amount))
    return pd.Timedelta(f"{amount}{_TF_UNITS[unit]}")


def drop_incomplete_bars(
    df: pd.DataFrame, timeframe: str, now: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Entfernt Kerzen, deren Intervall zum Zeitpunkt ``now`` noch nicht beendet ist."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    step = timeframe_to_timedelta(timeframe)
    return df[df.index + step <= now]


# -- Offline-Quellen ----------------------------------------------------------
def load_csv(path: str | Path, timestamp_column: str | None = None) -> pd.DataFrame:
    raw = pd.read_csv(path)
    return normalize_ohlcv(raw, timestamp_column=timestamp_column)


def generate_synthetic_ohlcv(
    n_bars: int = 1000,
    start: str = "2022-01-01",
    freq: str = "1D",
    start_price: float = 100.0,
    annual_drift: float = 0.05,
    annual_vol: float = 0.6,
    seed: int | None = 42,
    periods_per_year: float = 365.0,
) -> pd.DataFrame:
    """Geometrische Brownsche Bewegung mit plausiblen Intrabar-Hochs/-Tiefs.

    Nur für Tests und Demos geeignet – enthält kein echtes Marktverhalten.
    """
    if n_bars < 2:
        raise ValueError("n_bars muss >= 2 sein")
    rng = np.random.default_rng(seed)
    dt = 1.0 / periods_per_year
    sigma = annual_vol * np.sqrt(dt)
    mu = (annual_drift - 0.5 * annual_vol**2) * dt
    log_ret = rng.normal(mu, sigma, n_bars)
    close = start_price * np.exp(np.cumsum(log_ret))
    open_ = np.empty(n_bars)
    open_[0] = start_price
    open_[1:] = close[:-1] * np.exp(rng.normal(0, sigma * 0.1, n_bars - 1))
    wick_up = np.abs(rng.normal(0, sigma * 0.5, n_bars))
    wick_dn = np.abs(rng.normal(0, sigma * 0.5, n_bars))
    high = np.maximum(open_, close) * np.exp(wick_up)
    low = np.minimum(open_, close) * np.exp(-wick_dn)
    volume = rng.lognormal(mean=10, sigma=0.5, size=n_bars)
    index = canonical_index(pd.date_range(start=start, periods=n_bars, freq=freq, tz="UTC"))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index
    )


# -- Optionale Online-Quellen (kostenlos, ohne API-Schlüssel) -------------------
def _optional_import(module: str):
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - abhängig von Installation
        raise ImportError(
            f"Das optionale Paket '{module}' fehlt. Installation: pip install -e '.[data]'"
        ) from exc


def fetch_crypto_ohlcv(
    symbol: str,
    timeframe: str = "1d",
    since: str | pd.Timestamp = "2022-01-01",
    until: str | pd.Timestamp | None = None,
    exchange_id: str = "binance",
    limit_per_call: int = 1000,
    max_calls: int = 500,
) -> pd.DataFrame:
    """Lädt öffentliche Krypto-Kerzen via ccxt (ohne Authentifizierung)."""
    ccxt = _optional_import("ccxt")
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    since_ms = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)
    until_ts = pd.Timestamp.now(tz="UTC") if until is None else pd.Timestamp(until, tz="UTC")
    until_ms = int(until_ts.timestamp() * 1000)
    rows: list[list[float]] = []
    cursor = since_ms
    for _ in range(max_calls):
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit_per_call)
        if not batch:
            break
        rows.extend(batch)
        last_ts = int(batch[-1][0])
        if last_ts <= cursor or last_ts >= until_ms:
            break
        cursor = last_ts + 1
    if not rows:
        raise ValueError(f"Keine Daten für {symbol} ({exchange_id}) erhalten")
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = normalize_ohlcv(frame, timestamp_column="timestamp")
    frame = frame[~frame.index.duplicated(keep="last")]
    frame = frame[frame.index < until_ts]
    return drop_incomplete_bars(frame, timeframe)


def fetch_stock_ohlcv(
    ticker: str,
    timeframe: str = "1d",
    start: str = "2022-01-01",
    end: str | None = None,
) -> pd.DataFrame:
    """Lädt Aktienkurse via yfinance (Yahoo Finance, inoffizielle kostenlose Quelle).

    ``auto_adjust=True`` liefert dividenden-/splitbereinigte Kurse.
    """
    yf = _optional_import("yfinance")
    interval = timeframe.lower()
    history = yf.Ticker(ticker).history(
        start=start, end=end, interval=interval, auto_adjust=True, actions=False
    )
    if history is None or history.empty:
        raise ValueError(f"Keine Daten für {ticker} erhalten")
    frame = normalize_ohlcv(history)
    return drop_incomplete_bars(frame, timeframe)
