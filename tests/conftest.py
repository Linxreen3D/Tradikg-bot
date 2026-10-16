from __future__ import annotations

import pandas as pd
import pytest

from tradikg_bot.costs import FeeModel
from tradikg_bot.data import generate_synthetic_ohlcv
from tradikg_bot.domain import AssetClass, Instrument
from tradikg_bot.strategies.base import Strategy, empty_signals

ZERO_FEES = FeeModel()


class ScriptedStrategy(Strategy):
    """Teststrategie mit fest vorgegebenen Signalpositionen (kausal per Konstruktion)."""

    name = "scripted"

    def __init__(
        self,
        entries: list[int],
        exits: list[int] | None = None,
        stop_distance: float = 5.0,
        take_profit_distance: float = 10.0,
    ) -> None:
        self.entries = set(entries)
        self.exits = set(exits or [])
        self.sd = stop_distance
        self.tpd = take_profit_distance

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        sig = empty_signals(bars.index)
        n = len(bars)
        for i in self.entries:
            if i < n:
                sig.iloc[i, sig.columns.get_loc("entry")] = True
        for i in self.exits:
            if i < n:
                sig.iloc[i, sig.columns.get_loc("exit")] = True
        sig["stop_distance"] = self.sd
        sig["take_profit_distance"] = self.tpd
        return sig


def make_bars(
    rows: list[tuple[float, float, float, float]], freq: str = "1D", start: str = "2024-01-01"
) -> pd.DataFrame:
    """rows: (open, high, low, close)"""
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC", name="timestamp")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1000.0
    return df


@pytest.fixture
def crypto_instrument() -> Instrument:
    return Instrument("TEST-EUR", AssetClass.CRYPTO, qty_step=1e-6, min_notional=0.0)


@pytest.fixture
def synthetic_bars() -> pd.DataFrame:
    return generate_synthetic_ohlcv(n_bars=600, seed=7, annual_vol=0.8)
