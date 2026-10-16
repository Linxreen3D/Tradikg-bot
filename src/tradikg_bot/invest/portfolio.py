"""Langfristige Investment-Engine – Grundgerüst (keine echten Käufe).

Strikt getrennt vom Daytrading: eigener Kapitaltopf (die Ledger-Position
„Langfristportfolio“), eigene Strategielogik, eigene Datenmodelle. Diese Version
enthält nur Datenmodelle, Schnittstellen für spätere Datenquellen und einfache,
verlässlich berechenbare Analysen:

* Konzentrationsrisiko (größte Position, Herfindahl-Index)
* Diversifikation nach Assetklasse und Region
* Vergleich mit einem einfachen Benchmark-Portfolio (Rendite, Volatilität, Drawdown)
* Rebalancing-**Vorschläge** (keine Ausführung)

Fundamentaldaten werden nicht geschätzt oder erfunden: Ohne angebundene, verlässliche
Quelle bleiben die Felder leer (siehe ``FundamentalsProvider``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import numpy as np
import pandas as pd


class AssetType(StrEnum):
    ETF = "etf"
    STOCK = "stock"
    CRYPTO = "crypto"
    CASH = "cash"


@dataclass(frozen=True)
class InvestAsset:
    symbol: str
    name: str
    asset_type: AssetType
    region: str = "global"
    currency: str = "EUR"
    ter: float | None = None  # laufende Kosten (ETF), falls bekannt


@dataclass(frozen=True)
class Holding:
    asset: InvestAsset
    quantity: float
    price_eur: float

    @property
    def value_eur(self) -> float:
        return self.quantity * self.price_eur


@dataclass(frozen=True)
class Fundamentals:
    symbol: str
    pe_ratio: float | None = None
    dividend_yield: float | None = None
    market_cap_eur: float | None = None
    source: str = "nicht verfügbar"


class FundamentalsProvider(Protocol):
    def fundamentals(self, symbol: str) -> Fundamentals: ...


class PriceHistoryProvider(Protocol):
    def daily_closes(self, symbol: str, start: str, end: str | None = None) -> pd.Series: ...


class NoFundamentals:
    """Platzhalter: liefert ehrlich „nicht verfügbar“ statt geschätzter Kennzahlen."""

    def fundamentals(self, symbol: str) -> Fundamentals:
        return Fundamentals(symbol)


@dataclass
class LongTermPortfolio:
    """Buchposition des Langfristportfolios. ``cash_eur`` stammt aus dem Ledger."""

    cash_eur: float
    holdings: list[Holding] = field(default_factory=list)

    @property
    def invested_eur(self) -> float:
        return sum(h.value_eur for h in self.holdings)

    @property
    def total_eur(self) -> float:
        return self.cash_eur + self.invested_eur

    def weights(self) -> pd.Series:
        total = self.total_eur
        if total <= 0:
            return pd.Series(dtype=float)
        w = {h.asset.symbol: h.value_eur / total for h in self.holdings}
        if self.cash_eur > 0:
            w["CASH"] = self.cash_eur / total
        return pd.Series(w)


def concentration(portfolio: LongTermPortfolio) -> dict[str, float]:
    w = portfolio.weights()
    if w.empty:
        return {"max_weight": 0.0, "hhi": 0.0, "effective_positions": 0.0}
    hhi = float((w**2).sum())
    return {"max_weight": float(w.max()), "hhi": hhi, "effective_positions": 1.0 / hhi}


def exposure_by(portfolio: LongTermPortfolio, attribute: str) -> pd.Series:
    total = portfolio.total_eur
    if total <= 0:
        return pd.Series(dtype=float)
    rows: dict[str, float] = {}
    for h in portfolio.holdings:
        key = str(getattr(h.asset, attribute))
        rows[key] = rows.get(key, 0.0) + h.value_eur / total
    if portfolio.cash_eur > 0:
        rows["cash"] = rows.get("cash", 0.0) + portfolio.cash_eur / total
    return pd.Series(rows).sort_values(ascending=False)


def performance_stats(closes: pd.Series) -> dict[str, float]:
    """Kennzahlen einer Tageskursreihe: Gesamtrendite, annualisierte Vola, Max-Drawdown."""
    closes = closes.dropna()
    if len(closes) < 2:
        return {"total_return": 0.0, "volatility": 0.0, "max_drawdown": 0.0}
    rets = closes.pct_change().dropna()
    return {
        "total_return": float(closes.iloc[-1] / closes.iloc[0] - 1),
        "volatility": float(rets.std(ddof=1) * np.sqrt(252)),
        "max_drawdown": float((closes / closes.cummax() - 1).min()),
    }


def compare_to_benchmark(
    portfolio_values: pd.Series, benchmark_values: pd.Series
) -> dict[str, dict[str, float]]:
    joined = pd.concat([portfolio_values, benchmark_values], axis=1, join="inner").dropna()
    return {
        "portfolio": performance_stats(joined.iloc[:, 0]),
        "benchmark": performance_stats(joined.iloc[:, 1]),
    }


@dataclass(frozen=True)
class RebalanceProposal:
    symbol: str
    current_weight: float
    target_weight: float
    trade_eur: float  # >0 kaufen, <0 verkaufen – nur Vorschlag


def rebalance_proposals(
    portfolio: LongTermPortfolio, targets: dict[str, float], tolerance: float = 0.05
) -> list[RebalanceProposal]:
    """Vorschläge, wenn eine Gewichtung um mehr als ``tolerance`` vom Ziel abweicht.

    Es wird nichts ausgeführt. Echte Käufe sind eine eigene Roadmap-Phase mit eigener
    Freigabe.
    """
    if abs(sum(targets.values()) - 1.0) > 1e-6:
        raise ValueError("Zielgewichte müssen sich zu 1 addieren")
    w = portfolio.weights()
    total = portfolio.total_eur
    out = []
    for symbol, target in targets.items():
        current = float(w.get(symbol, 0.0))
        if abs(current - target) > tolerance:
            out.append(RebalanceProposal(symbol, current, target, (target - current) * total))
    return out
