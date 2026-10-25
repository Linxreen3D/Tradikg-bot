import pytest

from tradikg_bot.costs import (
    CRYPTO_SPOT_DEFAULT,
    STOCK_NEOBROKER_DEFAULT,
    FeeModel,
    OrderSide,
    default_fee_model,
)
from tradikg_bot.domain import AssetClass


def test_percentage_commission():
    assert FeeModel(commission_rate=0.001).commission(1000) == pytest.approx(1.0)


def test_minimum_and_fixed_commission():
    fees = FeeModel(commission_rate=0.001, min_commission=2.0, fixed_commission=0.5)
    assert fees.commission(100) == pytest.approx(2.5)  # 0.10 < Minimum 2.00
    assert fees.commission(10_000) == pytest.approx(10.5)
    assert fees.commission(0) == 0.0


def test_execution_price_applies_half_spread_and_slippage():
    fees = FeeModel(spread_bps=10, slippage_bps=5)  # 5 + 5 = 10 bps
    assert fees.execution_price(OrderSide.BUY, 100) == pytest.approx(100.10)
    assert fees.execution_price(OrderSide.SELL, 100) == pytest.approx(99.90)


def test_fill_cash_flows_and_costs():
    fees = FeeModel(commission_rate=0.001, spread_bps=20)
    buy = fees.market_fill(OrderSide.BUY, 2, 50)
    assert buy.price == pytest.approx(50.05)
    assert buy.commission == pytest.approx(0.1001)
    assert buy.cash_flow == pytest.approx(-(100.1 + 0.1001))
    assert buy.slippage_cost == pytest.approx(0.1)
    sell = fees.limit_fill(OrderSide.SELL, 2, 60)
    assert sell.price == 60
    assert sell.slippage_cost == 0
    assert sell.cash_flow == pytest.approx(120 - 0.12)


def test_round_trip_and_break_even():
    fees = FeeModel(commission_rate=0.001, spread_bps=10, slippage_bps=5)
    rt = fees.round_trip_cost(1, 100)
    # Kauf: 0.10 Slippage + 0.1001 Kommission; Verkauf: 0.10 + 0.0999
    assert rt == pytest.approx(0.4)
    assert fees.break_even_move(1, 100) == pytest.approx(0.004)


def test_fixed_fee_dominates_small_stock_orders():
    # 1 € pro Order bei 20 € Positionsgröße -> >10 % Round-Trip-Kosten
    assert STOCK_NEOBROKER_DEFAULT.break_even_move(1, 20) > 0.10
    assert CRYPTO_SPOT_DEFAULT.break_even_move(1, 20) < 0.01


def test_negative_parameters_rejected():
    with pytest.raises(ValueError):
        FeeModel(commission_rate=-0.1)


def test_default_fee_model_per_asset_class():
    assert default_fee_model(AssetClass.CRYPTO) is CRYPTO_SPOT_DEFAULT
    assert default_fee_model("stock") is STOCK_NEOBROKER_DEFAULT


def test_all_kraken_fee_defaults_use_public_tier_1():
    """Kraken Pro Spot-Krypto Stufe 1 (abgerufen 17.10.2026): 0,80 % Taker / 0,40 % Maker."""
    from tradikg_bot.costs import KRAKEN_SPOT_ASSUMED, KRAKEN_SPOT_MAKER, KRAKEN_SPOT_TAKER
    from tradikg_bot.live.paper import PaperExchange
    from tradikg_bot.market.scanner import ScanConfig
    from tradikg_bot.trading.runner import DEFAULT_PAPER_FEES

    assert (KRAKEN_SPOT_TAKER, KRAKEN_SPOT_MAKER) == (0.008, 0.004)
    assert KRAKEN_SPOT_ASSUMED.commission_rate == KRAKEN_SPOT_TAKER
    assert (DEFAULT_PAPER_FEES.taker, DEFAULT_PAPER_FEES.maker) == (0.008, 0.004)
    assert (PaperExchange().fees.taker, PaperExchange().fees.maker) == (0.008, 0.004)
    assert ScanConfig().assumed_taker_fee == 0.008
