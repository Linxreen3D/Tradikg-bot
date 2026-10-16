"""Champion/Challenger: Modellwechsel nur bei belastbarer Verbesserung auf unabhängigen Daten.

Kriterien (alle müssen erfüllt sein):
1. Beide Modelle wurden auf **denselben**, ungesehenen Testdaten bewertet.
2. Mindestens ``min_samples`` Testbeobachtungen.
3. Der Brier-Score des Challengers ist um mindestens ``min_improvement`` besser.
4. Die obere Grenze des 95-%-Bootstrap-Intervalls der Differenz
   ``Brier(Challenger) − Brier(Champion)`` liegt unter 0.
5. Der Challenger ist nicht schlechter kalibriert (ECE ≤ Champion-ECE + 0.02).

Im **Live-Modus** wird nie automatisch gewechselt: Die Entscheidung wird nur
empfohlen und braucht eine manuelle Freigabe.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from tradikg_bot.ml.models import expected_calibration_error


@dataclass
class ChampionDecision:
    promote: bool
    requires_manual_approval: bool
    reasons: list[str] = field(default_factory=list)
    brier_champion: float | None = None
    brier_challenger: float | None = None
    ci_upper: float | None = None


def decide(
    y: np.ndarray,
    p_champion: np.ndarray,
    p_challenger: np.ndarray,
    *,
    live_mode: bool = False,
    min_samples: int = 200,
    min_improvement: float = 0.002,
    bootstrap: int = 1000,
    seed: int = 7,
) -> ChampionDecision:
    y = np.asarray(y, float)
    d = ChampionDecision(False, live_mode)
    if len(y) < min_samples:
        d.reasons.append(f"zu wenige Testbeobachtungen ({len(y)} < {min_samples})")
        return d
    loss_c = (p_champion - y) ** 2
    loss_n = (p_challenger - y) ** 2
    d.brier_champion, d.brier_challenger = float(loss_c.mean()), float(loss_n.mean())
    diff = loss_n - loss_c
    rng = np.random.default_rng(seed)
    boots = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(bootstrap)]
    d.ci_upper = float(np.percentile(boots, 97.5))
    ok = True
    if d.brier_champion - d.brier_challenger < min_improvement:
        ok = False
        d.reasons.append(
            f"Brier-Verbesserung {d.brier_champion - d.brier_challenger:.4f} < {min_improvement}"
        )
    if d.ci_upper >= 0:
        ok = False
        d.reasons.append("Verbesserung statistisch nicht belastbar (95-%-KI schließt 0 ein)")
    ece_c = expected_calibration_error(p_champion, y)
    ece_n = expected_calibration_error(p_challenger, y)
    if ece_n > ece_c + 0.02:
        ok = False
        d.reasons.append(f"schlechter kalibriert (ECE {ece_n:.3f} vs. {ece_c:.3f})")
    if ok:
        d.reasons.append("Challenger belastbar besser")
        d.promote = not live_mode
        if live_mode:
            d.reasons.append("Live-Modus: Wechsel nur nach manueller Freigabe")
    return d
