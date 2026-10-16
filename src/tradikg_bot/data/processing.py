"""Verarbeitung historischer Kursdaten (Resampling, Train/Test-Aufteilung)."""

from __future__ import annotations

import pandas as pd

from tradikg_bot.data.validation import OHLCV_COLUMNS


def resample_ohlcv(df: pd.DataFrame, rule: str, drop_incomplete: bool = True) -> pd.DataFrame:
    """Aggregiert Kerzen auf ein gröberes Intervall.

    Die neue Kerze wird mit ihrer Öffnungszeit beschriftet (``label='left'``) und
    enthält nur Daten aus ``[t, t + rule)``. Mit ``drop_incomplete=True`` wird die
    letzte Kerze verworfen, falls ihr Intervall durch die Quelldaten noch nicht
    vollständig abgedeckt ist – sonst würde eine unfertige Kerze wie eine
    abgeschlossene behandelt (Look-Ahead-/Repainting-Gefahr im Live-Betrieb).
    """
    if df.empty:
        return df.copy()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df[OHLCV_COLUMNS].resample(rule, label="left", closed="left").agg(agg)
    out = out.dropna(subset=["open", "high", "low", "close"])
    if drop_incomplete and len(df) > 1 and not out.empty:
        src_step = df.index.to_series().diff().median()
        last_bucket_end = out.index[-1] + pd.tseries.frequencies.to_offset(rule)
        if df.index[-1] + src_step < last_bucket_end:
            out = out.iloc[:-1]
    out.index.name = "timestamp"
    return out


def chronological_split(
    df: pd.DataFrame, train_fraction: float = 0.7
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Zeitlich geordnete Aufteilung (kein Shuffle) für Out-of-Sample-Tests."""
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction muss in (0, 1) liegen")
    cut = int(len(df) * train_fraction)
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()
