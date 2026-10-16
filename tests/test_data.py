import sys
import types

import numpy as np
import pandas as pd
import pytest

from tradikg_bot.data import (
    DataValidationError,
    chronological_split,
    drop_incomplete_bars,
    fetch_crypto_ohlcv,
    fetch_stock_ohlcv,
    generate_synthetic_ohlcv,
    load_csv,
    normalize_ohlcv,
    resample_ohlcv,
    timeframe_to_timedelta,
    validate_ohlcv,
)


def raw_frame():
    return pd.DataFrame(
        {
            "Date": [
                "2024-01-03",
                "2024-01-01",
                "2024-01-02",
                "2024-01-02",
                "2024-01-05",
                "2024-01-06",
            ],
            "Open": [10, 10, 11, 11.5, 12, 12],
            "High": [11, 11, 12, 12.5, 11, 13],  # 2024-01-05: High < Open -> ungültig
            "Low": [9, 9, 10, 11, 10, 11],
            "Close": [10.5, 10.5, 11.5, 12, 10.5, 12.5],
            "Volume": [1, 1, 1, 1, 1, 1],
        }
    )


def test_validate_sorts_dedupes_and_removes_invalid():
    clean, report = validate_ohlcv(raw_frame(), expected_freq="1D")
    assert report.was_unsorted
    assert report.duplicates_removed == 1
    assert report.invalid_rows_removed == 1
    assert clean.index.is_monotonic_increasing
    assert str(clean.index.tz) == "UTC"
    assert list(clean.columns) == ["open", "high", "low", "close", "volume"]
    # letzter Duplikat-Eintrag gewinnt
    assert clean.loc["2024-01-02", "close"].item() == 12
    # Lücke 03.01. -> 06.01. (05.01. entfernt, 04.01. fehlt)
    assert report.gap_count == 1


def test_validate_rejects_missing_columns():
    with pytest.raises(DataValidationError):
        validate_ohlcv(pd.DataFrame({"date": ["2024-01-01"], "close": [1]}))


def test_validate_rejects_all_invalid():
    df = raw_frame().iloc[[4]]
    with pytest.raises(DataValidationError):
        validate_ohlcv(df)


def test_normalize_epoch_ms_and_timezone_conversion():
    df = pd.DataFrame(
        {
            "timestamp": [1704067200000],
            "open": [1],
            "high": [1],
            "low": [1],
            "close": [1],
            "volume": [0],
        }
    )
    out = normalize_ohlcv(df, timestamp_column="timestamp")
    assert out.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    berlin = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [0]},
        index=pd.DatetimeIndex([pd.Timestamp("2024-01-01 01:00", tz="Europe/Berlin")]),
    )
    assert normalize_ohlcv(berlin).index[0] == pd.Timestamp("2024-01-01 00:00", tz="UTC")


def test_resample_aggregates_without_future_data():
    idx = pd.date_range("2024-01-01", periods=48, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": np.arange(48.0) + 1,
            "high": np.arange(48.0) + 2,
            "low": np.arange(48.0),
            "close": np.arange(48.0) + 1.5,
            "volume": 1.0,
        },
        index=idx,
    )
    daily = resample_ohlcv(df, "1D")
    assert len(daily) == 2
    first = daily.iloc[0]
    assert first["open"] == 1 and first["close"] == 24.5
    assert first["high"] == 25 and first["low"] == 0 and first["volume"] == 24
    assert daily.index[0] == pd.Timestamp("2024-01-01", tz="UTC")


def test_resample_drops_incomplete_last_bucket():
    idx = pd.date_range("2024-01-01", periods=30, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx
    )
    assert len(resample_ohlcv(df, "1D")) == 1
    assert len(resample_ohlcv(df, "1D", drop_incomplete=False)) == 2


def test_chronological_split_keeps_order():
    df = generate_synthetic_ohlcv(100)
    train, test = chronological_split(df, 0.7)
    assert len(train) == 70 and len(test) == 30
    assert train.index[-1] < test.index[0]


def test_synthetic_data_is_valid_and_reproducible():
    a = generate_synthetic_ohlcv(500, seed=1)
    b = generate_synthetic_ohlcv(500, seed=1)
    pd.testing.assert_frame_equal(a, b)
    clean, report = validate_ohlcv(a)
    assert report.invalid_rows_removed == 0 and len(clean) == 500


def test_csv_roundtrip(tmp_path):
    df = generate_synthetic_ohlcv(20)
    path = tmp_path / "x.csv"
    df.to_csv(path)
    loaded = load_csv(path)
    pd.testing.assert_frame_equal(loaded, df, check_freq=False)


@pytest.mark.parametrize(
    ("tf", "expected"),
    [
        ("15m", pd.Timedelta(minutes=15)),
        ("4h", pd.Timedelta(hours=4)),
        ("1d", pd.Timedelta(days=1)),
        ("1w", pd.Timedelta(weeks=1)),
    ],
)
def test_timeframe_parsing(tf, expected):
    assert timeframe_to_timedelta(tf) == expected


def test_timeframe_parsing_rejects_garbage():
    with pytest.raises(ValueError):
        timeframe_to_timedelta("1 day")


def test_drop_incomplete_bars():
    df = generate_synthetic_ohlcv(5, start="2024-01-01")
    now = pd.Timestamp("2024-01-05 12:00", tz="UTC")  # Kerze vom 05.01. läuft noch
    assert drop_incomplete_bars(df, "1d", now=now).index[-1] == pd.Timestamp("2024-01-04", tz="UTC")


def test_fetch_crypto_paginates_and_drops_running_candle(monkeypatch):
    day = 86_400_000
    start = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    today = now_ms - now_ms % day
    candles = [[t, 1, 2, 0.5, 1.5, 10] for t in range(start, today + 1, day)]

    class FakeExchange:
        def __init__(self, cfg):
            self.calls = 0

        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            self.calls += 1
            return [c for c in candles if c[0] >= since][:limit]

    monkeypatch.setitem(sys.modules, "ccxt", types.SimpleNamespace(binance=FakeExchange))
    df = fetch_crypto_ohlcv("BTC/EUR", "1d", since="2024-01-01", limit_per_call=100)
    assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert df.index[-1] < pd.Timestamp(today, unit="ms", tz="UTC")  # laufende Kerze entfernt
    assert len(df) == len(candles) - 1
    assert df.index.is_unique


def test_fetch_stock_uses_adjusted_history(monkeypatch):
    calls = {}
    idx = pd.date_range("2024-01-01", periods=3, freq="1D", tz="America/New_York")
    hist = pd.DataFrame(
        {
            "Open": [1.0, 2, 3],
            "High": [1.5, 2.5, 3.5],
            "Low": [0.5, 1.5, 2.5],
            "Close": [1.2, 2.2, 3.2],
            "Volume": [100, 100, 100],
        },
        index=idx,
    )

    class FakeTicker:
        def __init__(self, ticker):
            calls["ticker"] = ticker

        def history(self, **kwargs):
            calls.update(kwargs)
            return hist

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))
    df = fetch_stock_ohlcv("AAPL", "1d", start="2024-01-01")
    assert calls["ticker"] == "AAPL" and calls["auto_adjust"] is True
    assert str(df.index.tz) == "UTC" and len(df) == 3
