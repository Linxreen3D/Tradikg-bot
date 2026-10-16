"""Kausale Merkmale für ML-Modelle und Signalbewertung.

Alle Merkmale zum Zeitpunkt ``t`` nutzen nur Daten bis einschließlich ``t``
(geprüft durch ``tests/test_research.py``). Sie sind skalenfrei (Renditen,
Verhältnisse, Ränge), damit sie über verschiedene Assets vergleichbar sind.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradikg_bot.features.regime import Regime, detect_regime
from tradikg_bot.indicators import (
    adx,
    atr,
    bollinger,
    donchian,
    ema,
    relative_volume,
    rolling_percentile,
    rsi,
    zscore,
)

FEATURE_COLUMNS = [
    "ret_1",
    "ret_5",
    "ret_20",
    "atr_pct",
    "vol_rank",
    "adx",
    "rsi",
    "bb_pos",
    "zscore_20",
    "rel_volume",
    "ema_ratio",
    "donchian_pos",
    "regime_trend_up",
    "regime_trend_down",
    "regime_high_vol",
]


def build_features(bars: pd.DataFrame) -> pd.DataFrame:
    c = bars["close"]
    a = atr(bars, 14)
    bb = bollinger(c, 20, 2.0)
    ch = donchian(bars, 20)
    regime = detect_regime(bars)
    span = (ch["high"] - ch["low"]).replace(0, np.nan)
    feats = pd.DataFrame(
        {
            "ret_1": c.pct_change(1),
            "ret_5": c.pct_change(5),
            "ret_20": c.pct_change(20),
            "atr_pct": a / c,
            "vol_rank": rolling_percentile(a / c, 200),
            "adx": adx(bars, 14) / 100,
            "rsi": rsi(c, 14) / 100,
            "bb_pos": (c - bb["lower"]) / (bb["upper"] - bb["lower"]).replace(0, np.nan),
            "zscore_20": zscore(c, 20),
            "rel_volume": np.log(relative_volume(bars["volume"], 20).clip(lower=1e-6)),
            "ema_ratio": ema(c, 20) / ema(c, 50) - 1,
            "donchian_pos": (c - ch["low"]) / span,
            "regime_trend_up": (regime == Regime.TREND_UP.value).astype(float),
            "regime_trend_down": (regime == Regime.TREND_DOWN.value).astype(float),
            "regime_high_vol": (regime == Regime.HIGH_VOLATILITY.value).astype(float),
        },
        index=bars.index,
    )
    feats[regime == Regime.UNKNOWN.value] = np.nan
    return feats[FEATURE_COLUMNS]
