"""Trainingspipeline: einfache Baseline zuerst, ML nur wenn belastbar besser.

1. Datensatz bauen (Triple-Barrier-Labels, kausale Features).
2. Chronologische Aufteilung Train/Validation/Test mit Embargo.
3. Basisraten-Modell und logistische Regression auf Train (+Validation) trainieren.
4. Beide auf denselben Testdaten bewerten (Brier, Log-Loss, Kalibrierung).
5. Gibt es noch keinen Champion, wird die **einfache** Baseline Champion.
6. Die logistische Regression wird Challenger und nur über ``champion.decide``
   befördert. Liefert ML keinen nachweisbaren Vorteil, bleibt die Baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from tradikg_bot.ml.champion import ChampionDecision, decide
from tradikg_bot.ml.dataset import chronological_splits
from tradikg_bot.ml.models import MODEL_FEATURES, BaseRateModel, LogisticModel, evaluate
from tradikg_bot.ml.registry import ModelRegistry


@dataclass
class TrainingResult:
    baseline_version: str | None
    challenger_version: str | None
    decision: ChampionDecision | None
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def train_and_evaluate(
    dataset: pd.DataFrame,
    registry: ModelRegistry,
    *,
    live_mode: bool = False,
    embargo: pd.Timedelta | None = None,
    min_samples: int = 200,
) -> TrainingResult:
    result = TrainingResult(None, None, None)
    if len(dataset) < 50:
        result.notes.append(f"zu wenige Signale für ein Training ({len(dataset)})")
        return result
    train, val, test = chronological_splits(dataset, embargo=embargo)
    fit_df = pd.concat([train, val])
    if test.empty or fit_df.empty:
        result.notes.append("Aufteilung ergab leere Bereiche")
        return result
    ranges = {
        "train_start": str(fit_df["signal_ts"].min()),
        "train_end": str(fit_df["signal_ts"].max()),
        "test_start": str(test["signal_ts"].min()),
        "test_end": str(test["signal_ts"].max()),
    }
    y = test["label"].to_numpy(float)

    baseline = BaseRateModel().fit(fit_df)
    p_base = baseline.predict(test)
    m_base = evaluate(p_base, y)
    result.metrics["baseline"] = m_base
    champion = registry.champion()
    if champion is None:
        result.baseline_version = registry.register(
            baseline, m_base, ranges, ["strategy", "reward_risk"], "einfache Baseline"
        )
        registry.set_champion(result.baseline_version, "erstes Modell: einfache Baseline")
        champion = registry.get(result.baseline_version)
    p_champion = champion.load().predict(test)

    try:
        challenger = LogisticModel().fit(fit_df)
    except ImportError:
        result.notes.append(
            "scikit-learn nicht installiert – nur Baseline (pip install -e '.[ml]')"
        )
        return result
    p_chal = challenger.predict(test)
    result.metrics["challenger"] = evaluate(p_chal, y)
    result.challenger_version = registry.register(
        challenger, result.metrics["challenger"], ranges, MODEL_FEATURES, "logistische Regression"
    )
    result.decision = decide(y, p_champion, p_chal, live_mode=live_mode, min_samples=min_samples)
    if result.decision.promote:
        registry.set_champion(result.challenger_version, "; ".join(result.decision.reasons))
    elif not result.decision.requires_manual_approval or not any(
        "belastbar besser" in r for r in result.decision.reasons
    ):
        registry.reject(result.challenger_version, "; ".join(result.decision.reasons))
    return result
