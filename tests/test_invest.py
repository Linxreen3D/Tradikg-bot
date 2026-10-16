import pandas as pd
import pytest

from tradikg_bot.invest.portfolio import (
    AssetType,
    Holding,
    InvestAsset,
    LongTermPortfolio,
    NoFundamentals,
    compare_to_benchmark,
    concentration,
    exposure_by,
    rebalance_proposals,
)

WORLD = InvestAsset("IWDA", "MSCI World ETF", AssetType.ETF, "global", ter=0.002)
EM = InvestAsset("EIMI", "EM ETF", AssetType.ETF, "emerging")
BTC = InvestAsset("XBT", "Bitcoin", AssetType.CRYPTO, "global")


def portfolio():
    return LongTermPortfolio(
        cash_eur=10.0,
        holdings=[
            Holding(WORLD, 1.0, 60.0),
            Holding(EM, 1.0, 20.0),
            Holding(BTC, 0.0001, 100000.0),
        ],
    )


def test_concentration_and_exposure():
    p = portfolio()
    assert p.total_eur == pytest.approx(100.0)
    c = concentration(p)
    assert c["max_weight"] == pytest.approx(0.6)
    assert c["effective_positions"] == pytest.approx(1 / (0.36 + 0.04 + 0.01 + 0.01))
    by_type = exposure_by(p, "asset_type")
    assert by_type["etf"] == pytest.approx(0.8) and by_type["cash"] == pytest.approx(0.1)


def test_rebalance_only_proposes():
    props = rebalance_proposals(portfolio(), {"IWDA": 0.7, "EIMI": 0.2, "XBT": 0.0, "CASH": 0.1})
    assert [p.symbol for p in props] == ["IWDA", "XBT"]
    assert props[0].trade_eur == pytest.approx(10.0)
    with pytest.raises(ValueError):
        rebalance_proposals(portfolio(), {"IWDA": 0.5})


def test_benchmark_comparison():
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    a = pd.Series([100, 110, 99, 120, 121.0], index=idx)
    b = pd.Series([100, 101, 102, 103, 104.0], index=idx)
    res = compare_to_benchmark(a, b)
    assert res["portfolio"]["total_return"] == pytest.approx(0.21)
    assert res["portfolio"]["max_drawdown"] == pytest.approx(-0.1)
    assert res["benchmark"]["max_drawdown"] == 0


def test_fundamentals_are_never_invented():
    f = NoFundamentals().fundamentals("IWDA")
    assert f.pe_ratio is None and f.source == "nicht verfügbar"


def test_empty_portfolio():
    p = LongTermPortfolio(cash_eur=0.0)
    assert concentration(p)["hhi"] == 0.0 and exposure_by(p, "region").empty
