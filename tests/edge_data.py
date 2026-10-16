"""Synthetische Testdaten MIT bekanntem eingebautem Vorteil (nur für Tests der Pipeline)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def momentum_ohlcv(
    n: int = 3000,
    phi: float = 0.3,
    sigma: float = 0.006,
    seed: int = 11,
    freq: str = "1h",
    start: str = "2024-01-01",
) -> pd.DataFrame:
    """AR(1)-Renditen mit positiver Autokorrelation: Trend-/Momentum-Strategien im Vorteil."""
    rng = np.random.default_rng(seed)
    eps = rng.normal(0, sigma, n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = phi * r[i - 1] + eps[i]
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.concatenate([[100.0], close[:-1]])
    wick = np.abs(rng.normal(0, sigma * 0.3, (2, n)))
    high = np.maximum(open_, close) * (1 + wick[0])
    low = np.minimum(open_, close) * (1 - wick[1])
    vol = rng.lognormal(10, 0.3, n) * (1 + 20 * np.abs(r))
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC", name="timestamp").as_unit("ns")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx
    )
