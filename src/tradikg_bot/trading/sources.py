"""Signalquellen für den Paper-Kreislauf.

* ``PrecomputedSignals`` (historisches Replay): Signale, Regime und Features werden
  einmal auf der vollen Historie berechnet und zeitpunktgenau abgefragt. Das ist
  nur zulässig, weil jede Strategie kausal ist (Look-Ahead-Tests); das Ergebnis
  ist identisch mit einer schrittweisen Berechnung, nur schneller.
* ``RollingSignals`` (laufender Paper-Betrieb): berechnet bei jeder neuen Kerze
  auf dem gespeicherten Kursverlauf.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from tradikg_bot.features import build_features, detect_regime
from tradikg_bot.strategies.base import Strategy, validate_signals


@dataclass(frozen=True)
class RawSignal:
    strategy: str
    entry: bool
    stop_distance: float
    take_distance: float
    regime: str
    features: pd.Series | None


class SignalSource(Protocol):
    def signals_at(
        self, symbol: str, ts: pd.Timestamp, history: pd.DataFrame
    ) -> list[RawSignal]: ...


def _signals_for(strategies: list[Strategy], bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {s.name: validate_signals(s.generate_signals(bars), bars) for s in strategies}


class PrecomputedSignals:
    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        strategies: dict[str, list[Strategy]],
        with_features: bool = True,
    ) -> None:
        self._sig = {sym: _signals_for(strategies[sym], bars) for sym, bars in data.items()}
        self._regime = {sym: detect_regime(bars) for sym, bars in data.items()}
        self._feat = (
            {sym: build_features(bars) for sym, bars in data.items()} if with_features else {}
        )

    def signals_at(self, symbol: str, ts: pd.Timestamp, history: pd.DataFrame) -> list[RawSignal]:
        out = []
        regime = str(self._regime[symbol].get(ts, "unknown"))
        feats = self._feat.get(symbol)
        frow = feats.loc[ts] if feats is not None and ts in feats.index else None
        for name, sig in self._sig[symbol].items():
            if ts not in sig.index:
                continue
            row = sig.loc[ts]
            out.append(
                RawSignal(
                    name,
                    bool(row["entry"]),
                    float(row["stop_distance"]),
                    float(row["take_profit_distance"]),
                    regime,
                    frow,
                )
            )
        return out


class RollingSignals:
    def __init__(self, strategies: dict[str, list[Strategy]], window: int = 600) -> None:
        self.strategies, self.window = strategies, window

    def signals_at(self, symbol: str, ts: pd.Timestamp, history: pd.DataFrame) -> list[RawSignal]:
        bars = history.iloc[-self.window :]
        if bars.empty or bars.index[-1] != ts:
            return []
        regime = str(detect_regime(bars).iloc[-1])
        frow = build_features(bars).iloc[-1]
        out = []
        for name, sig in _signals_for(self.strategies.get(symbol, []), bars).items():
            row = sig.iloc[-1]
            out.append(
                RawSignal(
                    name,
                    bool(row["entry"]),
                    float(row["stop_distance"]),
                    float(row["take_profit_distance"]),
                    regime,
                    frow,
                )
            )
        return out
