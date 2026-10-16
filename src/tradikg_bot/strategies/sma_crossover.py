"""Regelbasierte Baseline: SMA-Crossover mit Trendfilter und ATR-basiertem SL/TP.

Long-only (Spot-Krypto bzw. Aktien ohne Leerverkauf).

* Einstieg: schneller SMA kreuzt langsamen SMA von unten nach oben und der
  Schlusskurs liegt über dem Trend-SMA (optional).
* Ausstieg per Signal: schneller SMA kreuzt langsamen SMA nach unten.
* Stop-Loss: ``atr_stop_mult * ATR`` unter dem Einstieg.
* Take-Profit: ``atr_take_profit_mult * ATR`` über dem Einstieg.

Diese Strategie dient als Referenz ("Baseline"), an der spätere ML-Modelle
gemessen werden – sie ist nicht auf Profitabilität optimiert.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from tradikg_bot.indicators import atr, sma
from tradikg_bot.strategies.base import Strategy, empty_signals


@dataclass(frozen=True)
class SmaCrossoverParams:
    fast: int = 20
    slow: int = 50
    trend: int | None = 100
    atr_window: int = 14
    atr_stop_mult: float = 2.0
    atr_take_profit_mult: float = 3.0

    def __post_init__(self) -> None:
        if not 1 <= self.fast < self.slow:
            raise ValueError("Es muss gelten: 1 <= fast < slow")
        if self.trend is not None and self.trend < 1:
            raise ValueError("trend muss >= 1 oder None sein")
        if self.atr_stop_mult <= 0 or self.atr_take_profit_mult <= 0:
            raise ValueError("ATR-Multiplikatoren müssen > 0 sein")


class SmaCrossoverStrategy(Strategy):
    name = "sma_crossover"

    def __init__(self, params: SmaCrossoverParams | None = None) -> None:
        self.p = params or SmaCrossoverParams()

    @property
    def warmup_bars(self) -> int:
        return max(self.p.slow, self.p.trend or 0, self.p.atr_window) + 1

    def params(self) -> dict[str, Any]:
        return asdict(self.p)

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        close = bars["close"]
        fast = sma(close, self.p.fast)
        slow = sma(close, self.p.slow)
        above = fast > slow
        # shift(1) blickt in die Vergangenheit -> kausal.
        prev_above = above.shift(1, fill_value=False)
        valid = fast.notna() & slow.notna() & slow.shift(1).notna()
        cross_up = valid & above & ~prev_above
        cross_down = valid & ~above & prev_above

        trend_ok = pd.Series(True, index=bars.index)
        if self.p.trend is not None:
            trend_ma = sma(close, self.p.trend)
            trend_ok = trend_ma.notna() & (close > trend_ma)

        vol = atr(bars, self.p.atr_window)
        signals = empty_signals(bars.index)
        signals["entry"] = (cross_up & trend_ok & vol.notna() & (vol > 0)).astype(bool)
        signals["exit"] = cross_down.astype(bool)
        signals["stop_distance"] = vol * self.p.atr_stop_mult
        signals["take_profit_distance"] = vol * self.p.atr_take_profit_mult
        return signals
