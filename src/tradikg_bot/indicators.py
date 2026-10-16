"""Kausale technische Indikatoren.

Jeder Wert zum Zeitpunkt ``t`` hängt ausschließlich von Daten bis einschließlich
``t`` ab (``rolling``/``ewm`` ohne ``center``, keine negativen ``shift``).
"""

from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    if window < 1:
        raise ValueError("window muss >= 1 sein")
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    if span < 1:
        raise ValueError("span muss >= 1 sein")
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1, skipna=True)


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average True Range nach Wilder (exponentielle Glättung mit alpha=1/window)."""
    if window < 1:
        raise ValueError("window muss >= 1 sein")
    return true_range(df).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def adx(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average Directional Index nach Wilder (Trendstärke, 0–100, richtungslos)."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(df).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / window, adjust=False, min_periods=window).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / window, adjust=False, min_periods=window).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    return dx.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def bollinger(series: pd.Series, window: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(series, window)
    std = series.rolling(window, min_periods=window).std(ddof=0)
    return pd.DataFrame(
        {"mid": mid, "upper": mid + k * std, "lower": mid - k * std, "width": (2 * k * std) / mid}
    )


def zscore(series: pd.Series, window: int = 20) -> pd.Series:
    mean = series.rolling(window, min_periods=window).mean()
    std = series.rolling(window, min_periods=window).std(ddof=0)
    return (series - mean) / std.replace(0, float("nan"))


def donchian(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Hoch/Tief der **vorherigen** ``window`` Kerzen (ohne aktuelle Kerze -> kausal)."""
    return pd.DataFrame(
        {
            "high": df["high"].shift(1).rolling(window, min_periods=window).max(),
            "low": df["low"].shift(1).rolling(window, min_periods=window).min(),
        }
    )


def roc(series: pd.Series, window: int = 10) -> pd.Series:
    return series / series.shift(window) - 1


def relative_volume(volume: pd.Series, window: int = 20) -> pd.Series:
    """Volumen im Verhältnis zum Mittel der **vorherigen** ``window`` Kerzen."""
    base = volume.shift(1).rolling(window, min_periods=window).mean()
    return volume / base.replace(0, float("nan"))


def rolling_percentile(series: pd.Series, window: int = 100) -> pd.Series:
    """Rang des aktuellen Werts innerhalb der letzten ``window`` Werte (0..1)."""
    return series.rolling(window, min_periods=window).rank(pct=True)


def confirmed_swings(df: pd.DataFrame, lag: int = 3) -> pd.DataFrame:
    """Swing-Hochs/-Tiefs, die erst ``lag`` Kerzen später bestätigt und sichtbar werden.

    Ein Pivot bei ``t`` ist ein Hoch, das höher ist als die ``lag`` Kerzen davor und
    danach. Es ist frühestens bei ``t + lag`` bekannt; deshalb werden die Werte um
    ``lag`` verschoben und vorwärts gefüllt (keine Zukunftsinformation).
    """
    width = 2 * lag + 1
    hi_center = df["high"].rolling(width, center=True).max()
    lo_center = df["low"].rolling(width, center=True).min()
    swing_high = df["high"].where(df["high"] == hi_center)
    swing_low = df["low"].where(df["low"] == lo_center)
    return pd.DataFrame(
        {
            "swing_high": swing_high.shift(lag).ffill(),
            "swing_low": swing_low.shift(lag).ffill(),
        }
    )
