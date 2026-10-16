"""Schätzung der Trefferwahrscheinlichkeit (TP vor SL) für ein Signal.

Grundsatz: **Ohne Belege kein Vorteil.** Die Vorannahme ``p0 = SL / (SL + TP)`` ist
die Trefferquote, die bei zufälligem Kursverlauf (ohne Drift) ungefähr zu
erwarten ist. Damit ist der erwartete Netto-Wert eines Signals ohne Belege immer
negativ (Kosten), und es wird nicht gehandelt.

Belege kommen aus den Out-of-Sample-Statistiken der Research-Engine (Test +
Walk-Forward, nie Training). Die Schätzung schrumpft die beobachtete
Trefferquote in Richtung ``p0`` (Beta-Binomial, Gewicht ``prior_weight``).
Strategien mit dem Urteil „verworfen“ erhalten immer ``p0``.

Ein ML-Modell kann über ``ModelProbability`` vorgeschaltet werden, sofern es als
Champion registriert ist (siehe ``ml/``); sonst gilt die empirische Schätzung.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from tradikg_bot.research.store import OosStats


@dataclass(frozen=True)
class ProbabilityEstimate:
    p_win: float
    source: str
    n_evidence: int
    model_version: str | None = None


class ProbabilityModel(Protocol):
    def estimate(
        self,
        strategy: str,
        symbol: str,
        regime: str,
        reward_risk: float,
        features: pd.Series | None = None,
    ) -> ProbabilityEstimate: ...


def no_edge_probability(reward_risk: float) -> float:
    return 1.0 / (1.0 + max(reward_risk, 1e-9))


class EmpiricalProbability:
    def __init__(
        self, stats: list[OosStats], prior_weight: float = 20.0, min_evidence: int = 10
    ) -> None:
        self.prior_weight, self.min_evidence = prior_weight, min_evidence
        self._by_key = {(s.strategy, s.symbol, s.regime): s for s in stats}

    def _lookup(self, strategy: str, symbol: str, regime: str) -> OosStats | None:
        for key in (
            (strategy, symbol, regime),
            (strategy, symbol, "*"),
            (strategy, "*", regime),
            (strategy, "*", "*"),
        ):
            s = self._by_key.get(key)
            if s is not None and s.n >= self.min_evidence:
                return s
        return None

    def verdict(self, strategy: str) -> str | None:
        s = self._by_key.get((strategy, "*", "*"))
        return s.verdict if s else None

    def estimate(
        self,
        strategy: str,
        symbol: str,
        regime: str,
        reward_risk: float,
        features: pd.Series | None = None,
    ) -> ProbabilityEstimate:
        p0 = no_edge_probability(reward_risk)
        if self.verdict(strategy) == "verworfen":
            return ProbabilityEstimate(p0, "prior (Strategie verworfen)", 0)
        s = self._lookup(strategy, symbol, regime)
        if s is None:
            return ProbabilityEstimate(p0, "prior (keine OOS-Belege)", 0)
        p = (s.wins + self.prior_weight * p0) / (s.n + self.prior_weight)
        return ProbabilityEstimate(p, f"oos[{s.symbol}/{s.regime}]", s.n)


class ModelProbability:
    """Nutzt den registrierten Champion, falls er ein ML-Modell ist; sonst die Fallback-Schätzung.

    Jede verwendete Prognose wird im Register protokolliert, damit sie später mit dem
    tatsächlichen Ergebnis verglichen werden kann.
    """

    def __init__(self, registry, fallback: ProbabilityModel) -> None:
        self.registry, self.fallback = registry, fallback
        record = registry.champion()
        self.record = record if record is not None and record.kind != "base_rate" else None
        self.model = self.record.load() if self.record else None
        self._by_key = getattr(fallback, "_by_key", {})

    def estimate(
        self,
        strategy: str,
        symbol: str,
        regime: str,
        reward_risk: float,
        features: pd.Series | None = None,
    ) -> ProbabilityEstimate:
        base = self.fallback.estimate(strategy, symbol, regime, reward_risk, features)
        if self.model is None or features is None or features.isna().any():
            return base
        if base.source.startswith("prior (Strategie verworfen)"):
            return base
        row = pd.DataFrame(
            [{**features.to_dict(), "reward_risk": reward_risk, "strategy": strategy}]
        )
        p = float(self.model.predict(row)[0])
        ts = str(features.name) if features.name is not None else ""
        self.registry.record_prediction(self.record.version, symbol, strategy, ts, p)
        return ProbabilityEstimate(
            p, f"ml[{self.record.version}]", base.n_evidence, self.record.version
        )
