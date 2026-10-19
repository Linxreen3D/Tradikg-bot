from __future__ import annotations

import pandas as pd
import pytest

from tradikg_bot.trading.grid import GridAllocator, GridConfig, GridState, daily_regime, run_grid

CFG = GridConfig(
    spacing=0.1, levels=2, brake=0.2, sma=2, maker_fee=0.004, taker_fee=0.008, brake_slippage=0.0
)


def frame(rows):
    idx = pd.date_range("2026-01-10", periods=len(rows), freq="h", tz="UTC")
    df = pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])
    df["volume"] = 1.0
    return df


UP = pd.Series(
    [1.0, 2.0, 3.0, 4.0] * 3, index=pd.date_range("2025-12-30", periods=12, freq="D", tz="UTC")
)


def run(df, daily=UP, capital=100.0):
    return run_grid({"AAA": df}, {"AAA": daily}, df.index[0], None, capital, lambda s, p: 0.0, CFG)


def test_buys_levels_and_sells_one_step_higher_as_maker():
    df = frame([(100, 100, 100, 100), (100, 100, 89, 90), (90, 100, 90, 99)])
    _, trades, log, alloc = run(df)
    (t,) = trades
    assert (
        t["entry_price"] == 90
        and t["exit_price"] == pytest.approx(99)
        and t["exit_reason"] == "ziel"
    )
    units = 50 / (90 * 1.004)
    assert t["net_pnl_eur"] == pytest.approx(units * 9 - units * 99 * 0.004 - units * 90 * 0.004)
    assert alloc.open_count() == 0


def test_brake_sells_as_taker_and_resets_grid():
    df = frame([(100, 100, 100, 100), (100, 100, 89, 90), (90, 90, 60, 65)])
    _, trades, log, alloc = run(df)
    reasons = [t["exit_reason"] for t in trades]
    assert "notbremse" in reasons and alloc.state.refs["AAA"] == 65
    brake = next(t for t in trades if t["exit_reason"] == "notbremse")
    assert brake["exit_price"] == pytest.approx(72)  # 90 · (1 − 20 %)


def test_no_new_buys_in_downtrend():
    down = pd.Series([4.0, 3.0, 2.0, 1.0] * 3, index=UP.index)
    df = frame([(100, 100, 100, 100), (100, 100, 89, 90)])
    _, trades, log, alloc = run(df, daily=down)
    assert alloc.open_count() == 0 and "Abwärtstrend" in log[-1]["reason"]


def test_regime_uses_only_closed_days():
    daily = pd.Series(
        [1.0, 1.0, 5.0], index=pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
    )
    reg = daily_regime({"A": daily}, sma=2)
    assert not reg("A", pd.Timestamp("2026-01-03 12:00", tz="UTC"))  # Tag 3 noch offen
    assert reg("A", pd.Timestamp("2026-01-04 00:00", tz="UTC"))


def test_state_json_roundtrip_and_equity():
    df = frame([(100, 100, 100, 100), (100, 100, 89, 90)])
    _, _, _, alloc = run(df)
    clone = GridState.from_json(alloc.state.to_json())
    other = GridAllocator(["AAA"], 100.0, lambda s, p: 0.0, lambda s, t: True, CFG, clone)
    assert other.equity({"AAA": 95.0}) == pytest.approx(alloc.equity({"AAA": 95.0}))
