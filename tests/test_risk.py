import datetime as dt

import pytest

from tradikg_bot.config import RiskConfig
from tradikg_bot.costs import CRYPTO_SPOT_DEFAULT, STOCK_NEOBROKER_DEFAULT, FeeModel
from tradikg_bot.domain import AssetClass, Instrument
from tradikg_bot.risk import DailyLossGuard, SkipReason, size_position, worst_case_loss

CRYPTO = Instrument("BTC-EUR", AssetClass.CRYPTO, qty_step=1e-6, min_notional=5.0)
STOCK = Instrument("XYZ", AssetClass.STOCK, qty_step=1.0, min_notional=1.0)


def size(**overrides):
    kwargs = dict(
        equity=100.0,
        cash=100.0,
        ref_price=100.0,
        stop_distance=5.0,
        take_profit_distance=10.0,
        risk=RiskConfig(),
        instrument=CRYPTO,
        fees=CRYPTO_SPOT_DEFAULT,
    )
    kwargs.update(overrides)
    return size_position(**kwargs)


def test_worst_case_loss_includes_all_costs():
    fees = FeeModel(commission_rate=0.001, spread_bps=10, slippage_bps=5)
    loss = worst_case_loss(1.0, 100.0, 5.0, fees)
    assert loss > 5.0  # reiner Kursverlust wäre 5.0
    assert worst_case_loss(1.0, 100.0, 5.0, FeeModel()) == pytest.approx(5.0)


def test_size_respects_risk_budget_after_costs():
    d = size()
    assert d.accepted
    assert d.worst_case_loss <= 1.0 + 1e-9  # 1 % von 100 €
    assert d.worst_case_loss > 0.95  # Budget wird weitgehend genutzt


def test_size_respects_capital_cap():
    d = size(stop_distance=0.5, take_profit_distance=3.0)  # winziger Stop -> großer Hebelwunsch
    assert d.accepted
    assert d.qty * d.expected_entry_price <= 50.0 + 1e-9  # max. 50 % des Kapitals


def test_size_respects_remaining_daily_budget():
    d = size(remaining_daily_loss_budget=0.3)
    assert d.accepted and d.worst_case_loss <= 0.3 + 1e-9
    assert size(remaining_daily_loss_budget=0.0).skip_reason is SkipReason.NO_RISK_BUDGET


def test_invalid_levels_rejected():
    assert size(stop_distance=0).skip_reason is SkipReason.INVALID_LEVELS
    assert size(take_profit_distance=-1).skip_reason is SkipReason.INVALID_LEVELS
    assert size(stop_distance=150).skip_reason is SkipReason.INVALID_LEVELS


def test_whole_shares_below_one_rejected():
    d = size(
        instrument=STOCK,
        fees=FeeModel(),
        ref_price=300.0,
        stop_distance=10.0,
        take_profit_distance=30.0,
    )
    assert d.skip_reason is SkipReason.BELOW_MIN_SIZE


def test_cost_filter_blocks_trades_eaten_by_fixed_fees():
    # 1 Aktie à 10 €, Stop 0.5 €, TP 1 € brutto: 2 × 1 € Gebühr frisst den Gewinn.
    d = size(
        instrument=STOCK,
        fees=STOCK_NEOBROKER_DEFAULT,
        ref_price=10.0,
        stop_distance=0.5,
        take_profit_distance=1.0,
        risk=RiskConfig(risk_per_trade=0.1),
    )
    assert d.skip_reason is SkipReason.COSTS_TOO_HIGH
    assert d.net_reward_at_take_profit < d.worst_case_loss


def test_reward_risk_threshold():
    strict = RiskConfig(min_reward_risk_after_costs=3.0)
    assert size(risk=strict).skip_reason is SkipReason.COSTS_TOO_HIGH
    assert size(risk=RiskConfig(min_reward_risk_after_costs=1.5)).accepted


def test_daily_loss_guard():
    g = DailyLossGuard(limit=0.10)
    g.start_day(dt.date(2024, 1, 1), 100.0)
    assert g.remaining_budget(95.0) == pytest.approx(5.0)
    assert not g.check(90.01)
    assert g.check(90.0)
    assert g.halted and g.remaining_budget(95.0) == 0.0
    assert not g.check(80.0)  # nur einmal melden
    g.start_day(dt.date(2024, 1, 2), 80.0)
    assert not g.halted and g.floor == pytest.approx(72.0)
    assert g.halted_days == [dt.date(2024, 1, 1)]
