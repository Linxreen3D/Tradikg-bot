"""Validierung und Bereinigung historischer OHLCV-Daten.

Kanonisches Format im ganzen Projekt:
* ``pandas.DataFrame`` mit tz-aware UTC-``DatetimeIndex`` namens ``timestamp``
* Zeitstempel = **Öffnungszeit** der Kerze; die Kerze ist erst nach Ablauf ihres
  Intervalls abgeschlossen und darf erst dann für Entscheidungen genutzt werden.
* Spalten ``open, high, low, close, volume`` als ``float64``
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataValidationError(ValueError):
    pass


@dataclass
class ValidationReport:
    input_rows: int = 0
    output_rows: int = 0
    duplicates_removed: int = 0
    invalid_rows_removed: int = 0
    was_unsorted: bool = False
    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = field(default_factory=list)

    @property
    def gap_count(self) -> int:
        return len(self.gaps)

    def summary(self) -> str:
        return (
            f"Zeilen: {self.input_rows} -> {self.output_rows}, "
            f"Duplikate entfernt: {self.duplicates_removed}, "
            f"ungültige Zeilen entfernt: {self.invalid_rows_removed}, "
            f"Lücken: {self.gap_count}"
        )


def canonical_index(index: pd.Index) -> pd.DatetimeIndex:
    """UTC, Nanosekunden-Auflösung, Name ``timestamp`` – unabhängig von der pandas-Version."""
    if not isinstance(index, pd.DatetimeIndex):
        if pd.api.types.is_numeric_dtype(index):
            # Numerische Zeitstempel werden als Millisekunden seit Epoch interpretiert.
            index = pd.to_datetime(np.asarray(index, dtype="int64"), unit="ms", utc=True)
        else:
            index = pd.to_datetime(index, utc=True)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return pd.DatetimeIndex(index.as_unit("ns"), name="timestamp")


def normalize_ohlcv(df: pd.DataFrame, timestamp_column: str | None = None) -> pd.DataFrame:
    """Bringt Rohdaten in das kanonische Format (ohne inhaltliche Bereinigung)."""
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    if timestamp_column is not None:
        out = out.set_index(timestamp_column.lower())
    elif not isinstance(out.index, pd.DatetimeIndex):
        for candidate in ("timestamp", "date", "datetime", "time"):
            if candidate in out.columns:
                out = out.set_index(candidate)
                break
    missing = [c for c in OHLCV_COLUMNS if c not in out.columns]
    if missing:
        raise DataValidationError(f"Fehlende Spalten: {missing}")
    out.index = canonical_index(out.index)
    out = out[OHLCV_COLUMNS].apply(pd.to_numeric, errors="coerce").astype("float64")
    return out


def validate_ohlcv(
    df: pd.DataFrame,
    expected_freq: str | pd.Timedelta | None = None,
    timestamp_column: str | None = None,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Normalisiert, sortiert, entfernt Duplikate/ungültige Kerzen und meldet Lücken.

    Ungültige Kerzen werden *entfernt*, nicht repariert – eine Reparatur würde
    Informationen erfinden. Lücken werden nur gemeldet, nicht aufgefüllt.
    """
    report = ValidationReport(input_rows=len(df))
    out = normalize_ohlcv(df, timestamp_column=timestamp_column)

    if not out.index.is_monotonic_increasing:
        report.was_unsorted = True
        out = out.sort_index(kind="stable")

    dup_mask = out.index.duplicated(keep="last")
    report.duplicates_removed = int(dup_mask.sum())
    out = out[~dup_mask]

    prices = out[["open", "high", "low", "close"]]
    invalid = (
        prices.isna().any(axis=1)
        | (prices <= 0).any(axis=1)
        | out["volume"].isna()
        | (out["volume"] < 0)
        | (out["high"] < out[["open", "close", "low"]].max(axis=1))
        | (out["low"] > out[["open", "close", "high"]].min(axis=1))
    )
    report.invalid_rows_removed = int(invalid.sum())
    out = out[~invalid]

    if expected_freq is not None and len(out) > 1:
        step = pd.Timedelta(expected_freq)
        deltas = out.index.to_series().diff()
        gap_mask = deltas > step
        for end_ts in out.index[gap_mask.to_numpy()]:
            start_ts = out.index[out.index.get_loc(end_ts) - 1]
            report.gaps.append((start_ts, end_ts))

    if out.empty:
        raise DataValidationError("Nach der Bereinigung sind keine gültigen Kerzen übrig.")
    report.output_rows = len(out)
    return out, report
