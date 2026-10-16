import pandas as pd
import pytest

from tradikg_bot.backtest.metrics import daily_volume_benchmark, max_drawdown


def test_max_drawdown():
    eq = pd.Series([100.0, 120, 90, 130, 117])
    assert max_drawdown(eq) == pytest.approx(90 / 120 - 1)
    assert max_drawdown(pd.Series([1.0, 2, 3])) == 0.0


def test_daily_volume_benchmark_is_analysis_only():
    idx = pd.date_range("2024-01-01", periods=4, freq="12h", tz="UTC")
    equity = pd.Series([100.0, 102.0, 102.0, 101.0], index=idx)
    fills = pd.DataFrame(
        {"time": [idx[0], idx[1], idx[2], idx[3]], "notional": [50.0, 50.0, 40.0, 40.0]}
    )
    res = daily_volume_benchmark(equity, fills, benchmark=0.10, initial_capital=100.0)
    # Tag 1: +2 € auf 100 € Volumen = 2 %; Tag 2: -1 € auf 80 € = -1.25 %
    assert res["trading_days"] == 2
    assert res["mean"] == pytest.approx((0.02 - 0.0125) / 2)
    assert res["days_meeting_benchmark"] == 0


def test_daily_volume_benchmark_without_trades():
    idx = pd.date_range("2024-01-01", periods=2, freq="1D", tz="UTC")
    empty = pd.DataFrame(columns=["time", "notional"])
    res = daily_volume_benchmark(pd.Series([100.0, 100.0], index=idx), empty, 0.1, 100.0)
    assert res["trading_days"] == 0 and res["mean"] is None
