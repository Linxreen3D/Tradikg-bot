from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from tradikg_bot.backtest import BacktestEngine
from tradikg_bot.config import RiskConfig
from tradikg_bot.costs import CRYPTO_SPOT_DEFAULT, FeeModel
from tradikg_bot.domain import ExitReason
from tradikg_bot.risk import SkipReason
from tradikg_bot.strategies import SmaCrossoverStrategy

from conftest import ZERO_FEES, ScriptedStrategy, make_bars

FLAT = (100.0, 101.0, 99.0, 100.0)
HELD = (102.0, 103.0, 101.0, 102.0)


def run(rows, instrument, strategy, fees=ZERO_FEES, risk=None, freq="1D", capital=100.0):
    bars = make_bars(rows, freq=freq)
    engine = BacktestEngine(instrument, fees, risk or RiskConfig(), initial_capital=capital)
    return bars, engine.run(bars, strategy)


def test_entry_is_executed_at_next_open_not_signal_bar(crypto_instrument):
    bars, res = run([FLAT, FLAT, HELD, HELD], crypto_instrument, ScriptedStrategy([1]))
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_time == bars.index[2]
    assert t.entry_price == pytest.approx(102.0)  # Open der Folgekerze, nicht Close der Signalkerze
    # Risiko 1 % von 100 € = 1 €; Stop-Abstand 5 -> 0.2 Einheiten
    assert t.qty == pytest.approx(0.2)
    assert t.stop_loss == pytest.approx(97.0)
    assert t.take_profit == pytest.approx(112.0)
    assert t.exit_reason is ExitReason.END_OF_DATA


def test_entry_price_includes_spread_and_slippage(crypto_instrument):
    fees = FeeModel(spread_bps=20, slippage_bps=10)  # Aufschlag 20 bps
    _, res = run([FLAT, FLAT, HELD, HELD], crypto_instrument, ScriptedStrategy([1]), fees=fees)
    assert res.trades[0].entry_price == pytest.approx(102.0 * 1.002)


def test_stop_loss_exit(crypto_instrument):
    rows = [FLAT, FLAT, HELD, (102.0, 103.0, 96.0, 98.0), HELD]
    _, res = run(rows, crypto_instrument, ScriptedStrategy([1]))
    t = res.trades[0]
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.exit_price == pytest.approx(97.0)
    assert t.net_pnl == pytest.approx(-1.0)
    assert t.r_multiple == pytest.approx(-1.0)
    assert res.final_equity == pytest.approx(99.0)


def test_stop_loss_fill_suffers_slippage(crypto_instrument):
    fees = FeeModel(slippage_bps=10)
    rows = [FLAT, FLAT, HELD, (102.0, 103.0, 96.0, 98.0), HELD]
    _, res = run(rows, crypto_instrument, ScriptedStrategy([1]), fees=fees)
    t = res.trades[0]
    entry = 102.0 * 1.001
    assert t.exit_price == pytest.approx((entry - 5.0) * 0.999)


def test_take_profit_exit_as_limit(crypto_instrument):
    fees = FeeModel(slippage_bps=10)
    rows = [FLAT, FLAT, HELD, (102.0, 120.0, 101.0, 110.0), HELD]
    _, res = run(rows, crypto_instrument, ScriptedStrategy([1]), fees=fees)
    t = res.trades[0]
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert t.exit_price == pytest.approx(102.0 * 1.001 + 10.0)  # Limit: keine Slippage


def test_both_levels_in_same_bar_assumes_stop_loss(crypto_instrument):
    rows = [FLAT, FLAT, HELD, (102.0, 120.0, 90.0, 110.0), HELD]
    _, res = run(rows, crypto_instrument, ScriptedStrategy([1]))
    assert res.trades[0].exit_reason is ExitReason.STOP_LOSS


def test_gap_below_stop_fills_at_open(crypto_instrument):
    rows = [FLAT, FLAT, HELD, (90.0, 91.0, 85.0, 88.0), HELD]
    _, res = run(rows, crypto_instrument, ScriptedStrategy([1]))
    t = res.trades[0]
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.exit_price == pytest.approx(90.0)
    assert t.net_pnl == pytest.approx(0.2 * (90.0 - 102.0))


def test_gap_above_take_profit_fills_at_open(crypto_instrument):
    rows = [FLAT, FLAT, HELD, (120.0, 121.0, 119.0, 120.0), HELD]
    _, res = run(rows, crypto_instrument, ScriptedStrategy([1]))
    assert res.trades[0].exit_reason is ExitReason.TAKE_PROFIT
    assert res.trades[0].exit_price == pytest.approx(120.0)


def test_signal_exit_at_next_open(crypto_instrument):
    rows = [FLAT, FLAT, HELD, HELD, (104.0, 105.0, 103.0, 104.0), HELD]
    bars, res = run(rows, crypto_instrument, ScriptedStrategy([1], exits=[3]))
    t = res.trades[0]
    assert t.exit_reason is ExitReason.SIGNAL
    assert t.exit_time == bars.index[4]
    assert t.exit_price == pytest.approx(104.0)


def test_signal_on_last_bar_is_never_executed(crypto_instrument):
    _, res = run([FLAT, FLAT, FLAT], crypto_instrument, ScriptedStrategy([2]))
    assert res.trades == []
    assert res.final_equity == pytest.approx(100.0)


def test_entry_without_stop_or_take_profit_is_rejected(crypto_instrument):
    strat = ScriptedStrategy([1], stop_distance=float("nan"))
    _, res = run([FLAT, FLAT, HELD, HELD], crypto_instrument, strat)
    assert res.trades == []
    assert [s.reason for s in res.skipped_entries] == [SkipReason.INVALID_LEVELS]


def test_every_trade_has_stop_loss_and_take_profit(crypto_instrument, synthetic_bars):
    engine = BacktestEngine(crypto_instrument, CRYPTO_SPOT_DEFAULT)
    res = engine.run(synthetic_bars, SmaCrossoverStrategy())
    assert res.trades, "Synthetische Daten sollten Trades erzeugen"
    for t in res.trades:
        assert 0 < t.stop_loss < t.entry_price < t.take_profit


def test_accounting_is_consistent_with_costs(crypto_instrument, synthetic_bars):
    engine = BacktestEngine(crypto_instrument, CRYPTO_SPOT_DEFAULT)
    res = engine.run(synthetic_bars, SmaCrossoverStrategy())
    net = sum(t.net_pnl for t in res.trades)
    assert res.final_equity == pytest.approx(100.0 + net)
    for t in res.trades:
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.commission - t.slippage_cost)
        assert t.commission > 0 and t.slippage_cost > 0
    assert res.metrics["total_costs"] > 0
    assert res.fills["commission"].sum() == pytest.approx(res.metrics["total_commission"])


def test_costs_reduce_result_for_identical_trades(crypto_instrument):
    rows = [FLAT, FLAT, HELD, (102.0, 120.0, 101.0, 110.0), HELD]
    fees = FeeModel(commission_rate=0.001, spread_bps=10, slippage_bps=5)
    _, free = run(rows, crypto_instrument, ScriptedStrategy([1]))
    _, costly = run(rows, crypto_instrument, ScriptedStrategy([1]), fees=fees)
    assert costly.final_equity < free.final_equity


def test_risk_per_trade_is_respected_including_costs(crypto_instrument, synthetic_bars):
    risk = RiskConfig(risk_per_trade=0.01)
    engine = BacktestEngine(crypto_instrument, CRYPTO_SPOT_DEFAULT, risk)
    res = engine.run(synthetic_bars, SmaCrossoverStrategy())
    equity_before = 100.0
    for t in res.trades:
        # geplantes Risiko <= 1 % der Equity vor Einstieg (kleine Toleranz für Gap-Kauf)
        assert t.planned_risk <= 0.011 * equity_before + 1e-9
        equity_before += t.net_pnl


def test_daily_loss_limit_liquidates_and_blocks_entries(crypto_instrument):
    risk = RiskConfig(risk_per_trade=0.1, max_position_fraction=1.0, min_reward_risk_after_costs=0)
    rows = [
        (100, 100, 100, 100),  # 01.01. 00h Einstiegssignal
        (100, 400, 100, 400),  # 01.01. 12h Einstieg @100, Schluss 400 -> Equity 160
        (400, 400, 300, 300),  # 02.01. 00h Equity 140 < 144 (=160*0.9) -> Sperre
        (300, 300, 300, 300),  # 02.01. 12h Glattstellung @Open; Signal wird blockiert
        (300, 300, 300, 300),  # 03.01. 00h neuer Tag, Signal erlaubt
        (300, 310, 295, 305),  # 03.01. 12h neuer Einstieg
    ]
    strat = ScriptedStrategy([0, 3, 4], stop_distance=50.0, take_profit_distance=1000.0)
    bars, res = run(rows, crypto_instrument, strat, risk=risk, freq="12h")
    first = res.trades[0]
    assert first.exit_reason is ExitReason.DAILY_LOSS_LIMIT
    assert first.exit_time == bars.index[3]
    assert res.halted_days == [dt.date(2024, 1, 2)]
    assert SkipReason.DAILY_LOSS_HALT in [s.reason for s in res.skipped_entries]
    assert res.trades[1].entry_time == bars.index[5]


def test_engine_has_no_lookahead_when_future_changes(crypto_instrument, synthetic_bars):
    engine = BacktestEngine(crypto_instrument, CRYPTO_SPOT_DEFAULT)
    base = engine.run(synthetic_bars, SmaCrossoverStrategy())
    k = 400
    perturbed = synthetic_bars.copy()
    rng = np.random.default_rng(1)
    factor = np.exp(rng.normal(0, 0.2, len(perturbed) - k))
    perturbed.iloc[k:, :4] = perturbed.iloc[k:, :4].to_numpy() * factor[:, None]
    other = engine.run(perturbed, SmaCrossoverStrategy())
    pd.testing.assert_series_equal(base.equity["equity"].iloc[:k], other.equity["equity"].iloc[:k])
    closed_before = [t for t in base.trades if t.exit_time < synthetic_bars.index[k]]
    assert closed_before == [t for t in other.trades if t.exit_time < synthetic_bars.index[k]]


def test_engine_rejects_unsorted_data(crypto_instrument):
    bars = make_bars([FLAT, FLAT, FLAT]).iloc[::-1]
    engine = BacktestEngine(crypto_instrument, ZERO_FEES)
    with pytest.raises(ValueError):
        engine.run(bars, ScriptedStrategy([]))


def test_metrics_present_and_finite(crypto_instrument, synthetic_bars):
    res = BacktestEngine(crypto_instrument, CRYPTO_SPOT_DEFAULT).run(
        synthetic_bars, SmaCrossoverStrategy()
    )
    m = res.metrics
    for key in ("total_return", "max_drawdown", "exposure", "total_costs", "traded_volume"):
        assert math.isfinite(m[key])
    assert -1 <= m["max_drawdown"] <= 0
    assert m["num_trades"] == len(res.trades)
    assert m["daily_volume_benchmark"]["benchmark"] == 0.10
