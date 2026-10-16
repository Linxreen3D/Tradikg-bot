"""Empirische Prüfung auf Look-Ahead-Bias.

Idee: Ein kausales Signal in Zeile ``t`` muss identisch sein, egal ob die Strategie
die vollständige Historie oder nur ``bars.iloc[:t+1]`` sieht. Weicht ein Wert ab,
hat die Strategie Informationen aus der Zukunft genutzt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tradikg_bot.strategies.base import SIGNAL_COLUMNS, Strategy, validate_signals


class LookAheadBiasError(AssertionError):
    pass


@dataclass
class LookAheadReport:
    checked_positions: list[int] = field(default_factory=list)
    violations: list[tuple[pd.Timestamp, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _equal(a: object, b: object) -> bool:
    if isinstance(a, (bool, np.bool_)) or isinstance(b, (bool, np.bool_)):
        return bool(a) == bool(b)
    fa, fb = float(a), float(b)
    if np.isnan(fa) and np.isnan(fb):
        return True
    return bool(np.isclose(fa, fb, rtol=1e-9, atol=1e-12))


def check_no_lookahead(
    strategy: Strategy,
    bars: pd.DataFrame,
    n_checkpoints: int = 25,
    raise_on_violation: bool = True,
) -> LookAheadReport:
    full = validate_signals(strategy.generate_signals(bars), bars)
    n = len(bars)
    start = min(max(strategy.warmup_bars, 1), n - 1)
    positions = np.unique(np.linspace(start, n - 1, num=min(n_checkpoints, n - start), dtype=int))
    report = LookAheadReport(checked_positions=[int(p) for p in positions])
    for pos in positions:
        truncated = bars.iloc[: pos + 1]
        partial = validate_signals(strategy.generate_signals(truncated), truncated)
        ts = bars.index[pos]
        for col in SIGNAL_COLUMNS:
            if not _equal(partial[col].iloc[-1], full[col].iloc[pos]):
                report.violations.append((ts, col))
    if raise_on_violation and not report.ok:
        first_ts, first_col = report.violations[0]
        raise LookAheadBiasError(
            f"Strategie '{strategy.name}' nutzt Zukunftsdaten: {len(report.violations)} "
            f"Abweichung(en), erste bei {first_ts} in Spalte '{first_col}'."
        )
    return report
