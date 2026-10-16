"""Trainingsdaten für die Signalqualität: Wird bei einem Strategie-Signal zuerst das
Take-Profit oder zuerst der Stop-Loss erreicht? (Triple-Barrier-Label)

* Merkmale stammen aus der **Signalkerze** (kausal, ``features.build_features``).
* Einstieg zur Eröffnung der Folgekerze; danach werden die Kerzen bis zum
  Horizont geprüft. Berühren SL und TP dieselbe Kerze, gilt konservativ SL.
* Wird innerhalb des Horizonts keine Schranke erreicht, wird das Signal verworfen
  (kein erfundenes Label).
* Jede Zeile trägt ihren Zeitstempel; Aufteilungen erfolgen nur chronologisch.
  Ein Label „sieht“ bis zu ``horizon`` Kerzen in die Zukunft – deshalb wird
  zwischen Trainings- und Testbereich ein Abstand (Embargo) von ``horizon``
  Kerzen gelassen.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradikg_bot.features import FEATURE_COLUMNS, build_features
from tradikg_bot.strategies.base import Strategy, validate_signals


def label_signals(bars: pd.DataFrame, signals: pd.DataFrame, horizon: int = 48) -> pd.DataFrame:
    o, h, lo = (bars[c].to_numpy(float) for c in ("open", "high", "low"))
    rows = []
    entries = np.flatnonzero(signals["entry"].to_numpy(bool))
    stop = signals["stop_distance"].to_numpy(float)
    take = signals["take_profit_distance"].to_numpy(float)
    n = len(bars)
    for i in entries:
        if i + 1 >= n or not (stop[i] > 0 and take[i] > 0):
            continue
        entry = o[i + 1]
        sl, tp = entry - stop[i], entry + take[i]
        label = None
        for j in range(i + 1, min(n, i + 1 + horizon)):
            if lo[j] <= sl:
                label = 0
                break
            if h[j] >= tp:
                label = 1
                break
        if label is None:
            continue
        rows.append((bars.index[i], label, take[i] / stop[i], j - i))
    return pd.DataFrame(rows, columns=["signal_ts", "label", "reward_risk", "bars_to_outcome"])


def build_dataset(
    data: dict[str, pd.DataFrame], strategies: dict[str, list[Strategy]], horizon: int = 48
) -> pd.DataFrame:
    frames = []
    for symbol, bars in data.items():
        feats = build_features(bars)
        for strat in strategies.get(symbol, []):
            sig = validate_signals(strat.generate_signals(bars), bars)
            labels = label_signals(bars, sig, horizon)
            if labels.empty:
                continue
            labels = labels.join(feats, on="signal_ts")
            labels["symbol"], labels["strategy"] = symbol, strat.name
            frames.append(labels)
    if not frames:
        return pd.DataFrame(
            columns=["signal_ts", "label", "reward_risk", *FEATURE_COLUMNS, "symbol", "strategy"]
        )
    out = pd.concat(frames, ignore_index=True).dropna(subset=FEATURE_COLUMNS)
    return out.sort_values("signal_ts", kind="stable").reset_index(drop=True)


def chronological_splits(
    df: pd.DataFrame, train: float = 0.6, val: float = 0.2, embargo: pd.Timedelta | None = None
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    times = df["signal_ts"]
    t_val = times.quantile(train)
    t_test = times.quantile(train + val)
    gap = embargo or pd.Timedelta(0)
    tr = df[times < t_val - gap]
    va = df[(times >= t_val) & (times < t_test - gap)]
    te = df[times >= t_test]
    return tr, va, te
