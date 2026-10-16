"""Strategie-Schnittstelle.

Vertrag (wird von der Backtesting-Engine und ``check_no_lookahead`` erzwungen):

* ``generate_signals(bars)`` erhält abgeschlossene Kerzen und liefert einen
  DataFrame mit demselben Index und den Spalten aus ``SIGNAL_COLUMNS``.
* Zeile ``t`` darf nur Informationen aus ``bars.loc[:t]`` verwenden.
* Die Engine führt ein Signal aus Zeile ``t`` frühestens zum **Eröffnungskurs der
  Kerze t+1** aus.
* Jedes Einstiegssignal muss positive Abstände für Stop-Loss und Take-Profit
  (in Kurseinheiten) enthalten; sonst wird es von der Engine verworfen.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

SIGNAL_COLUMNS = ["entry", "exit", "stop_distance", "take_profit_distance"]


class Strategy(ABC):
    name: str = "strategy"

    @property
    def warmup_bars(self) -> int:
        """Mindestanzahl Kerzen, bevor sinnvolle Signale entstehen."""
        return 0

    @abstractmethod
    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    def params(self) -> dict[str, Any]:
        return {}


def empty_signals(index: pd.Index) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "entry": False,
            "exit": False,
            "stop_distance": float("nan"),
            "take_profit_distance": float("nan"),
        },
        index=index,
    )


def validate_signals(signals: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in SIGNAL_COLUMNS if c not in signals.columns]
    if missing:
        raise ValueError(f"Signal-DataFrame fehlen Spalten: {missing}")
    if not signals.index.equals(bars.index):
        raise ValueError("Signal-Index muss exakt dem Kerzen-Index entsprechen")
    out = signals[SIGNAL_COLUMNS].copy()
    out["entry"] = out["entry"].fillna(False).astype(bool)
    out["exit"] = out["exit"].fillna(False).astype(bool)
    out["stop_distance"] = out["stop_distance"].astype("float64")
    out["take_profit_distance"] = out["take_profit_distance"].astype("float64")
    return out
