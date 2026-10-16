"""Regelbasierte Marktphasen-Erkennung (interpretierbar, kausal).

Reihenfolge der Regeln:
1. ``high_volatility`` – ATR in % des Kurses liegt im obersten 15-%-Rang der letzten 200 Kerzen
2. ``trend_up`` / ``trend_down`` – ADX ≥ 25 und Steigung des SMA(50) über 10 Kerzen > 0 bzw. < 0
3. ``range`` – sonst
4. ``unknown`` – Aufwärmphase (zu wenig Daten)
"""

from __future__ import annotations

from enum import StrEnum

import pandas as pd

from tradikg_bot.indicators import adx, atr, rolling_percentile, sma


class Regime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    HIGH_VOLATILITY = "high_volatility"
    UNKNOWN = "unknown"


REGIMES = [r.value for r in Regime]


def detect_regime(
    bars: pd.DataFrame, adx_trend: float = 25.0, vol_rank_high: float = 0.85
) -> pd.Series:
    close = bars["close"]
    strength = adx(bars, 14)
    slope = sma(close, 50).pct_change(10)
    vol_rank = rolling_percentile(atr(bars, 14) / close, 200)
    regime = pd.Series(Regime.RANGE.value, index=bars.index, dtype=object)
    regime[(strength >= adx_trend) & (slope > 0)] = Regime.TREND_UP.value
    regime[(strength >= adx_trend) & (slope < 0)] = Regime.TREND_DOWN.value
    regime[vol_rank >= vol_rank_high] = Regime.HIGH_VOLATILITY.value
    regime[strength.isna() | slope.isna() | vol_rank.isna()] = Regime.UNKNOWN.value
    return regime
