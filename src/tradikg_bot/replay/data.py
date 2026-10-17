"""Historische Daten für das Market Replay.

Kostenlose Quellen und ihre Grenzen:

- Kraken-REST ``OHLC``: offiziell, ohne Schlüssel, aber höchstens 720 Kerzen je
  Intervall (1d ≈ 2 Jahre, 4h ≈ 120 Tage, 1h ≈ 30 Tage) – ``tradikg data fetch-kraken``.
- Kraken „Downloadable historical OHLCVT“: offiziell, vollständige Historie (1m–1d),
  manueller ZIP-Download – ``tradikg data import-kraken``.
- ccxt (z. B. Binance): lange Historie über Paginierung, aber **andere Börse** –
  Preise weichen ab; die Quelle wird im Bericht gekennzeichnet.

Das Kraken-CSV-Format hat **keine Kopfzeile**: ``timestamp, open, high, low, close,
volume, trades`` (Unix-Sekunden). Intervalle ohne Handel fehlen (keine Nullfüllung) –
der Replay behandelt solche Lücken als Datenlücke (keine neuen Einstiege).

``synthetic_market_phases`` erzeugt **künstliche** Testdaten mit Aufwärts-, Abwärts-,
Seitwärts- und Crash-Phasen. Sie prüfen nur die Pipeline und sind im Bericht immer als
synthetisch markiert.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from tradikg_bot.data.validation import validate_ohlcv
from tradikg_bot.features.regime import detect_regime

KRAKEN_CSV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "trades"]
INTERVAL_MINUTES = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "12h": 720,
    "1d": 1440,
}


def read_kraken_ohlcvt(source: str | Path | io.IOBase) -> pd.DataFrame:
    """Liest eine Kraken-OHLCVT-CSV (ohne Kopfzeile) in das kanonische OHLCV-Format."""
    raw = pd.read_csv(source, header=None, names=KRAKEN_CSV_COLUMNS)
    raw["timestamp"] = raw["timestamp"].astype("int64") * 1000  # Sekunden -> ms
    bars, report = validate_ohlcv(raw.drop(columns=["trades"]), timestamp_column="timestamp")
    bars.attrs["validation"] = report.summary()  # Bereinigung beim Import sichtbar machen
    return bars


def import_kraken_ohlcvt(
    path: str | Path, pairs: list[str], timeframe: str
) -> dict[str, pd.DataFrame]:
    """Importiert ``<PAIR>_<Minuten>.csv`` aus einem Ordner oder ZIP-Archiv."""
    minutes = INTERVAL_MINUTES[timeframe]
    path = Path(path)
    wanted = {f"{p}_{minutes}.csv".lower(): p for p in pairs}
    out: dict[str, pd.DataFrame] = {}
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                pair = wanted.get(Path(name).name.lower())
                if pair:
                    with zf.open(name) as fh:
                        out[pair] = read_kraken_ohlcvt(io.TextIOWrapper(fh, "utf-8"))
    else:
        for file in path.glob("*.csv"):
            pair = wanted.get(file.name.lower())
            if pair:
                out[pair] = read_kraken_ohlcvt(file)
    return out


@dataclass(frozen=True)
class Coverage:
    symbol: str
    bars: int
    start: pd.Timestamp
    end: pd.Timestamp
    missing_bars: int
    regime_share: dict[str, float]
    total_return: float


def coverage(data: dict[str, pd.DataFrame], timeframe: str) -> list[Coverage]:
    """Abdeckung je Symbol inkl. Anteil der Marktphasen (prüft, ob verschiedene Phasen
    enthalten sind)."""
    step = pd.Timedelta(minutes=INTERVAL_MINUTES[timeframe])
    out = []
    for sym, bars in data.items():
        if bars.empty:
            continue
        expected = int((bars.index[-1] - bars.index[0]) / step) + 1
        share = detect_regime(bars).value_counts(normalize=True).round(3).to_dict()
        out.append(
            Coverage(
                sym,
                len(bars),
                bars.index[0],
                bars.index[-1],
                max(0, expected - len(bars)),
                share,
                float(bars["close"].iloc[-1] / bars["open"].iloc[0] - 1),
            )
        )
    return out


def quality(
    bars: pd.DataFrame, timeframe: str, now: pd.Timestamp | None = None, outlier_z: float = 8.0
) -> dict:
    """Datenqualität einer gespeicherten Reihe (Cache ist bereits beim Import bereinigt).

    Lücken = Intervalle ohne Kerze (bei Kraken: kein Handel). Ausreißer = Log-Renditen mit
    robustem z-Wert (Median/MAD) über ``outlier_z`` – nur gemeldet, nie entfernt.
    """
    step = pd.Timedelta(minutes=INTERVAL_MINUTES[timeframe])
    now = now or pd.Timestamp.now(tz="UTC")
    idx = bars.index
    deltas = idx.to_series().diff().dropna()
    gaps = deltas[deltas > step]
    expected = int((idx[-1] - idx[0]) / step) + 1
    prices = bars[["open", "high", "low", "close"]]
    invalid = (
        (prices <= 0).any(axis=1)
        | (bars["high"] < prices.max(axis=1))
        | (bars["low"] > prices.min(axis=1))
        | (bars["volume"] < 0)
    )
    r = np.log(bars["close"]).diff().dropna()
    mad = float((r - r.median()).abs().median()) * 1.4826
    z = (r - r.median()).abs() / mad if mad > 0 else r * 0
    outliers = z[z > outlier_z].sort_values(ascending=False)
    return {
        "timeframe": timeframe,
        "bars": len(bars),
        "start": idx[0].isoformat(),
        "end": idx[-1].isoformat(),
        "timezone_utc": str(idx.tz) == "UTC",
        "duplicates": int(idx.duplicated().sum()),
        "unsorted": not idx.is_monotonic_increasing,
        "invalid_rows": int(invalid.sum()),
        "missing_bars": max(0, expected - len(bars)),
        "missing_share": round(max(0, expected - len(bars)) / expected, 4),
        "gap_count": len(gaps),
        "largest_gap": str(gaps.max()) if len(gaps) else "0",
        "zero_volume_bars": int((bars["volume"] == 0).sum()),
        "last_bar_incomplete": bool(idx[-1] + step > now),
        "outliers": [
            {"ts": t.isoformat(), "log_return": round(float(r[t]), 5), "z": round(float(v), 1)}
            for t, v in outliers.head(10).items()
        ],
        "outlier_count": len(outliers),
    }


def synthetic_market_phases(
    n: int = 730,
    seed: int = 1,
    start: str = "2024-01-01",
    freq: str = "1D",
    base_price: float = 100.0,
    correlation_seed: int | None = None,
) -> pd.DataFrame:
    """Künstliche Daten mit Phasen: Aufwärts, Seitwärts, Abwärts, Crash, Erholung.

    Nur für Pipeline-Tests – **keine Marktdaten**.
    """
    rng = np.random.default_rng(seed)
    common = (
        np.random.default_rng(correlation_seed).normal(0, 1, n)
        if correlation_seed is not None
        else np.zeros(n)
    )
    phases = [
        (0.25, 0.004, 0.025),
        (0.2, 0.0, 0.015),
        (0.2, -0.004, 0.03),
        (0.05, -0.02, 0.06),
        (0.3, 0.003, 0.03),
    ]
    drift, vol = np.empty(n), np.empty(n)
    i = 0
    for share, mu, sigma in phases:
        k = n - i if share == phases[-1][0] else int(n * share)
        drift[i : i + k], vol[i : i + k] = mu, sigma
        i += k
    shocks = (
        0.6 * common + 0.8 * rng.normal(0, 1, n)
        if correlation_seed is not None
        else rng.normal(0, 1, n)
    )
    r = np.zeros(n)
    for t in range(1, n):  # leichte Autokorrelation (Trendphasen)
        r[t] = drift[t] + 0.15 * (r[t - 1] - drift[t - 1]) + vol[t] * shocks[t]
    close = base_price * np.exp(np.cumsum(r))
    open_ = np.concatenate([[base_price], close[:-1]]) * np.exp(rng.normal(0, vol * 0.05))
    wick = np.abs(rng.normal(0, 1, (2, n))) * vol * 0.4
    high = np.maximum(open_, close) * (1 + wick[0])
    low = np.minimum(open_, close) * (1 - wick[1])
    volume = rng.lognormal(9, 0.4, n) * (1 + 10 * np.abs(r))
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC", name="timestamp").as_unit("ns")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )


def sanitize_pair(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())
