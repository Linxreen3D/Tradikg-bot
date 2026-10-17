"""Strategiefamilien für die Market-Edge-Research (alle long-only, alle kausal).

Jede Strategie ist zunächst eine **Hypothese**. Ob sie einen Vorteil nach Kosten
hat, entscheidet ausschließlich die Research-Engine auf ungesehenen Testdaten
(``research/``). Die Parameter sind bewusst klein gehalten (wenige Varianten je
Familie), um Data Snooping zu begrenzen.

Familien:
* trend      – EMA-Crossover mit ADX-Trendstärke und Volumenbestätigung
* momentum   – Donchian-Ausbruch mit positivem ROC und RSI-Fenster
* mean_rev   – Rückkehr zum Mittelwert nach Überdehnung (Bollinger/Z-Score)
* volatility – Ausbruch nach Volatilitätskompression (Bollinger-Breite)
* volume     – volumenbestätigter Ausbruch (relatives Volumen)
* structure  – Bruch des letzten bestätigten Swing-Hochs (Marktstruktur)
* regime     – Trendfolge mit Regimefilter (Donchian-Ausbruch nur über langem SMA), für
               1d/4h gedacht: wenige Trades, weite Ziele, damit Gebühren wenig ins Gewicht fallen

Orderbuch-Ungleichgewichte (Familie F) sind mit kostenlosen historischen Daten
nicht rückwirkend testbar und daher nicht als Strategie implementiert.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from tradikg_bot.indicators import (
    adx,
    atr,
    bollinger,
    confirmed_swings,
    donchian,
    ema,
    relative_volume,
    roc,
    rolling_percentile,
    rsi,
    sma,
    zscore,
)
from tradikg_bot.strategies.base import Strategy, empty_signals


def _signals(
    bars: pd.DataFrame, entry: pd.Series, exit_: pd.Series, stop: pd.Series, take: pd.Series
) -> pd.DataFrame:
    sig = empty_signals(bars.index)
    valid = stop.notna() & take.notna() & (stop > 0) & (take > 0)
    sig["entry"] = (entry.fillna(False) & valid).astype(bool)
    sig["exit"] = exit_.fillna(False).astype(bool)
    sig["stop_distance"] = stop
    sig["take_profit_distance"] = take
    return sig


class _ParamStrategy(Strategy):
    family = "generic"
    Params: type = object

    def __init__(self, params: Any = None) -> None:
        self.p = params or self.Params()

    def params(self) -> dict[str, Any]:
        return asdict(self.p)


# -- A. Trend Following -------------------------------------------------------------------
@dataclass(frozen=True)
class TrendParams:
    fast: int = 20
    slow: int = 50
    adx_min: float = 20.0
    rel_volume_min: float = 1.0
    stop_atr: float = 2.0
    take_atr: float = 3.0


class TrendAdxStrategy(_ParamStrategy):
    name, family, Params = "trend_adx", "trend", TrendParams

    @property
    def warmup_bars(self) -> int:
        return self.p.slow + 30

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        c = bars["close"]
        fast, slow = ema(c, self.p.fast), ema(c, self.p.slow)
        above = fast > slow
        prev = above.shift(1, fill_value=False)
        valid = slow.notna() & slow.shift(1).notna()
        strength = adx(bars, 14)
        vol_ok = relative_volume(bars["volume"], 20) >= self.p.rel_volume_min
        a = atr(bars, 14)
        entry = valid & above & ~prev & (strength >= self.p.adx_min) & vol_ok
        exit_ = valid & ~above & prev
        return _signals(bars, entry, exit_, a * self.p.stop_atr, a * self.p.take_atr)


# -- B. Momentum ----------------------------------------------------------------------------
@dataclass(frozen=True)
class MomentumParams:
    channel: int = 20
    roc_window: int = 10
    rsi_low: float = 55.0
    rsi_high: float = 80.0
    stop_atr: float = 1.5
    take_atr: float = 3.0


class MomentumBreakoutStrategy(_ParamStrategy):
    name, family, Params = "momentum_breakout", "momentum", MomentumParams

    @property
    def warmup_bars(self) -> int:
        return max(self.p.channel, self.p.roc_window, 14) + 30

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        c = bars["close"]
        ch = donchian(bars, self.p.channel)
        r = rsi(c, 14)
        a = atr(bars, 14)
        breakout = c > ch["high"]
        entry = (
            breakout
            & ~breakout.shift(1, fill_value=False)
            & (roc(c, self.p.roc_window) > 0)
            & r.between(self.p.rsi_low, self.p.rsi_high)
        )
        exit_ = c < ch["low"]
        return _signals(bars, entry, exit_, a * self.p.stop_atr, a * self.p.take_atr)


# -- C. Mean Reversion -------------------------------------------------------------------------
@dataclass(frozen=True)
class MeanReversionParams:
    window: int = 20
    z_entry: float = -2.0
    adx_max: float = 25.0  # nur in nicht trendenden Phasen
    stop_atr: float = 1.5
    min_take_atr: float = 1.0


class MeanReversionStrategy(_ParamStrategy):
    name, family, Params = "mean_reversion", "mean_reversion", MeanReversionParams

    @property
    def warmup_bars(self) -> int:
        return self.p.window + 30

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        c = bars["close"]
        z = zscore(c, self.p.window)
        bb = bollinger(c, self.p.window, 2.0)
        a = atr(bars, 14)
        stretched = (z <= self.p.z_entry) & (c < bb["lower"])
        entry = (
            stretched & ~stretched.shift(1, fill_value=False) & (adx(bars, 14) <= self.p.adx_max)
        )
        take = np.maximum(bb["mid"] - c, self.p.min_take_atr * a)
        exit_ = c >= bb["mid"]
        return _signals(bars, entry, exit_, a * self.p.stop_atr, pd.Series(take, index=c.index))


# -- D. Volatility Breakout --------------------------------------------------------------------
@dataclass(frozen=True)
class VolatilityParams:
    window: int = 20
    squeeze_pct: float = 0.2  # Bollinger-Breite im untersten Fünftel
    lookback: int = 100
    stop_atr: float = 1.5
    take_atr: float = 3.0


class VolatilitySqueezeStrategy(_ParamStrategy):
    name, family, Params = "volatility_squeeze", "volatility", VolatilityParams

    @property
    def warmup_bars(self) -> int:
        return self.p.lookback + self.p.window + 10

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        c = bars["close"]
        bb = bollinger(c, self.p.window, 2.0)
        width_rank = rolling_percentile(bb["width"], self.p.lookback)
        squeezed_recently = (width_rank <= self.p.squeeze_pct).rolling(5, min_periods=1).max() > 0
        a = atr(bars, 14)
        breakout = c > bb["upper"]
        entry = (
            breakout
            & ~breakout.shift(1, fill_value=False)
            & squeezed_recently.shift(1, fill_value=False)
        )
        exit_ = c < bb["mid"]
        return _signals(bars, entry, exit_, a * self.p.stop_atr, a * self.p.take_atr)


# -- F. Volume ----------------------------------------------------------------------
@dataclass(frozen=True)
class VolumeParams:
    rel_volume_min: float = 2.0
    close_location_min: float = 0.75
    stop_atr: float = 1.5
    take_atr: float = 2.5


class VolumeBreakoutStrategy(_ParamStrategy):
    name, family, Params = "volume_breakout", "volume", VolumeParams

    @property
    def warmup_bars(self) -> int:
        return 50

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        h, lo, c = bars["high"], bars["low"], bars["close"]
        rng = (h - lo).replace(0, np.nan)
        location = (c - lo) / rng
        a = atr(bars, 14)
        entry = (
            (relative_volume(bars["volume"], 20) >= self.p.rel_volume_min)
            & (c > h.shift(1))
            & (location >= self.p.close_location_min)
        )
        exit_ = c < sma(c, 10)
        return _signals(bars, entry, exit_, a * self.p.stop_atr, a * self.p.take_atr)


# -- E. Market Structure ------------------------------------------------------------
@dataclass(frozen=True)
class StructureParams:
    swing_lag: int = 3
    reward_risk: float = 2.0
    min_stop_atr: float = 1.0
    max_stop_atr: float = 4.0


class StructureBreakStrategy(_ParamStrategy):
    name, family, Params = "structure_break", "structure", StructureParams

    @property
    def warmup_bars(self) -> int:
        return 60

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        c = bars["close"]
        sw = confirmed_swings(bars, self.p.swing_lag)
        a = atr(bars, 14)
        higher_low = (
            sw["swing_low"]
            > sw["swing_low"].shift(1).where(sw["swing_low"] != sw["swing_low"].shift(1)).ffill()
        )
        broke = c > sw["swing_high"]
        entry = broke & ~broke.shift(1, fill_value=False) & higher_low
        stop = (c - sw["swing_low"]).clip(
            lower=self.p.min_stop_atr * a, upper=self.p.max_stop_atr * a
        )
        exit_ = c < sw["swing_low"]
        return _signals(bars, entry, exit_, stop, stop * self.p.reward_risk)


# -- G. Regime-Trendfolge --------------------------------------------------------------------
@dataclass(frozen=True)
class RegimeParams:
    """Parameter vorab aus der Trendfolge-Literatur festgelegt (Donchian 20/55, SMA 100/200,
    Stop 3 ATR, Ziel 6 ATR) – **nicht** auf den Kraken-Daten optimiert."""

    regime: int = 200
    channel: int = 55
    stop_atr: float = 3.0
    take_atr: float = 6.0


class RegimeBreakoutStrategy(_ParamStrategy):
    """Long nur im Aufwärtsregime (Schluss über SMA ``regime``) beim Ausbruch über das Hoch der
    letzten ``channel`` Kerzen. Ausstieg nur über SL/TP – wie im Paper- und Live-Pfad, der
    nur Brackets kennt; so rechnen Research und Paper-Engine identisch."""

    name, family, Params = "regime_breakout", "regime", RegimeParams

    @property
    def warmup_bars(self) -> int:
        return max(self.p.regime, self.p.channel) + 30

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        c = bars["close"]
        breakout = c > donchian(bars, self.p.channel)["high"]
        entry = breakout & ~breakout.shift(1, fill_value=False) & (c > sma(c, self.p.regime))
        a = atr(bars, 20)
        no_exit = pd.Series(False, index=bars.index)
        return _signals(bars, entry, no_exit, a * self.p.stop_atr, a * self.p.take_atr)


def _grid(cls, **variants: list[Any]) -> list[Any]:
    """Kartesisches Produkt weniger Parametervarianten."""
    import itertools

    keys = list(variants)
    return [
        cls(**dict(zip(keys, combo, strict=True)))
        for combo in itertools.product(*(variants[k] for k in keys))
    ]


@dataclass(frozen=True)
class StrategySpec:
    name: str
    family: str
    factory: type
    grid: list[Any]

    def build(self, params: Any = None) -> Strategy:
        return self.factory(params)


def strategy_registry() -> dict[str, StrategySpec]:
    from tradikg_bot.strategies.sma_crossover import SmaCrossoverParams, SmaCrossoverStrategy

    specs = [
        StrategySpec(
            "sma_crossover",
            "trend",
            SmaCrossoverStrategy,
            _grid(SmaCrossoverParams, fast=[10, 20], slow=[50, 100]),
        ),
        StrategySpec(
            "trend_adx",
            "trend",
            TrendAdxStrategy,
            _grid(TrendParams, fast=[10, 20], slow=[50], adx_min=[20.0, 25.0]),
        ),
        StrategySpec(
            "momentum_breakout",
            "momentum",
            MomentumBreakoutStrategy,
            _grid(MomentumParams, channel=[20, 55], take_atr=[2.0, 3.0]),
        ),
        StrategySpec(
            "mean_reversion",
            "mean_reversion",
            MeanReversionStrategy,
            _grid(MeanReversionParams, z_entry=[-2.0, -2.5], adx_max=[20.0, 25.0]),
        ),
        StrategySpec(
            "volatility_squeeze",
            "volatility",
            VolatilitySqueezeStrategy,
            _grid(VolatilityParams, squeeze_pct=[0.1, 0.2], take_atr=[2.0, 3.0]),
        ),
        StrategySpec(
            "volume_breakout",
            "volume",
            VolumeBreakoutStrategy,
            _grid(VolumeParams, rel_volume_min=[1.5, 2.0], take_atr=[2.0, 3.0]),
        ),
        StrategySpec(
            "structure_break",
            "structure",
            StructureBreakStrategy,
            _grid(StructureParams, swing_lag=[3, 5], reward_risk=[1.5, 2.0]),
        ),
        StrategySpec(
            "regime_breakout",
            "regime",
            RegimeBreakoutStrategy,
            _grid(RegimeParams, regime=[100, 200], channel=[20, 55]),
        ),
    ]
    return {s.name: s for s in specs}
