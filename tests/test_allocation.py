from __future__ import annotations

import pandas as pd
import pytest

from tradikg_bot.trading.allocation import TrendAllocator, TrendConfig, TrendState, run_trend

CFG = TrendConfig(
    sma=3, stop_frac=0.2, maker_fee=0.004, taker_fee=0.008, spread_bps=0.0, stop_slippage_bps=0.0
)


def frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D", tz="UTC")
    df = pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])
    df["volume"] = 1.0
    return df


def no_min(_sym: str, _px: float) -> float:
    return 0.0


def run(df: pd.DataFrame, capital: float = 100.0, min_order=no_min):
    return run_trend({"AAA": df}, df.index[0], None, capital, min_order, CFG)


def test_buys_maker_after_close_above_sma_and_sells_on_signal():
    df = frame(
        [
            (10, 10, 10, 10),
            (10, 10, 10, 10),
            (10, 10, 10, 10),
            (10, 12, 10, 12),  # Schluss 12 > SMA → Kauf-Limit 12
            (12, 13, 11.5, 13),  # Tief 11.5 ≤ 12 → Maker-Kauf zu 12
            (13, 13, 11, 11),  # Schluss 11 < SMA 12 → Verkauf-Limit 11 (Stop 10,4 nicht erreicht)
            (11, 11.5, 10.6, 11),  # Hoch 11,5 ≥ 11 → Maker-Verkauf zu 11
        ]
    )
    eq, trades, log, alloc = run(df)
    (t,) = trades
    assert t["entry_price"] == 12 and t["exit_price"] == 11 and t["exit_reason"] == "signal"
    units = 100 / (12 * 1.004)
    assert t["fees_eur"] == pytest.approx(units * 12 * 0.004 + units * 11 * 0.004)
    assert alloc.state.cash == pytest.approx(units * 11 * 0.996)
    assert [x["status"] for x in log] == ["entry", "exit"]


def test_gap_up_falls_back_to_taker_at_close():
    df = frame([(10, 10, 10, 10)] * 3 + [(10, 12, 10, 12), (14, 15, 13, 14)])
    _, _, _, alloc = run(df)
    pos = alloc.state.positions["AAA"]
    assert pos.entry_price == 14 and pos.entry_fee == pytest.approx(pos.units * 14 * 0.008)


def test_emergency_stop_then_blocked_until_fresh_cross():
    df = frame(
        [
            (10, 10, 10, 10),
            (10, 10, 10, 10),
            (10, 10, 10, 10),
            (10, 12, 10, 12),
            (12, 20, 12, 20),  # Kauf zu 12, Hoch-Schluss 20 → Stop 16
            (20, 20, 15, 18),  # Tief 15 ≤ 16 → Notfall-Stop zu 16
            (18, 19, 18, 19),  # über SMA, aber gesperrt
        ]
    )
    eq, trades, log, alloc = run(df)
    (t,) = trades
    assert t["exit_reason"] == "notfall_stop" and t["exit_price"] == 16
    assert log[-1]["reason"] == "Sperre nach Notfall-Stop" and not alloc.state.pending


def test_taker_fill_at_close_is_not_stopped_by_earlier_low():
    df = frame([(10, 10, 10, 10)] * 3 + [(10, 12, 10, 12), (13, 14, 8, 14)])
    _, trades, _, alloc = run(df)  # Tief 8 vor dem Kauf zum Schluss 14 – kein Stop
    assert not trades and "AAA" in alloc.state.positions


def test_minimum_order_blocks_too_small_slots():
    df = frame([(10, 10, 10, 10)] * 3 + [(10, 12, 10, 12), (12, 13, 11, 13)])
    _, trades, log, alloc = run(df, capital=10.0, min_order=lambda s, px: 20.0)
    assert not alloc.state.positions and "unter Mindestorder" in log[-1]["reason"]


def test_decisions_never_depend_on_future_bars():
    base = [(10, 10, 10, 10)] * 3 + [(10, 12, 10, 12), (12, 13, 11.5, 13), (13, 14, 12, 14)]
    a = run(frame(base + [(14, 15, 13, 15)] * 3))
    b = run(frame(base + [(14, 14, 2, 2)] * 3))
    cut = frame(base).index[-1]
    assert a[0][:cut].equals(b[0][:cut])


def test_state_survives_json_roundtrip():
    df = frame([(10, 10, 10, 10)] * 3 + [(10, 12, 10, 12), (12, 13, 11.5, 13)])
    _, _, _, alloc = run(df)
    clone = TrendState.from_json(alloc.state.to_json())
    assert clone == alloc.state
    assert TrendAllocator(["AAA"], 100.0, no_min, CFG, clone).equity(
        {"AAA": 13.0}
    ) == pytest.approx(alloc.equity({"AAA": 13.0}))


def test_invest_fraction_halves_position_and_cash_yield_accrues():
    from dataclasses import replace

    df = frame([(10, 10, 10, 10)] * 3 + [(10, 12, 10, 12), (12, 13, 11.5, 13)])
    _, _, _, full = run(df)
    half = run_trend(
        {"AAA": df}, df.index[0], None, 100.0, no_min, replace(CFG, invest_fraction=0.5)
    )[3]
    assert half.state.positions["AAA"].units == pytest.approx(full.state.positions["AAA"].units / 2)
    flat = frame([(10, 10, 10, 10)] * 365)  # nie über SMA → nur Cash
    rich = run_trend(
        {"AAA": flat}, flat.index[0], None, 100.0, no_min, replace(CFG, cash_yield_pa=0.02)
    )
    assert rich[0].iloc[-1] == pytest.approx(100 * (1 + 0.02 / 365) ** 365)
    assert run(flat)[0].iloc[-1] == 100.0  # ohne Zins bleibt Cash exakt gleich


def test_platform_config_validates_trend_risk_settings():
    from tradikg_bot.platform_config import PlatformConfig

    with pytest.raises(ValueError):
        PlatformConfig(invest_fraction=0)
    with pytest.raises(ValueError):
        PlatformConfig(cash_yield_pa=0.5)
    assert PlatformConfig(invest_fraction=0.5).invest_fraction == 0.5
