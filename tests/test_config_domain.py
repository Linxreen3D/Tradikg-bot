from pathlib import Path

import pytest

from tradikg_bot.config import AppConfig, LiveTradingNotAllowedError, RiskConfig, TradingMode
from tradikg_bot.domain import AssetClass, Instrument

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["live", "LIVE", "real", "echtgeld", "production"])
def test_live_mode_is_blocked(mode):
    with pytest.raises(LiveTradingNotAllowedError):
        TradingMode.parse(mode)
    with pytest.raises(LiveTradingNotAllowedError):
        AppConfig.from_dict({"app": {"mode": mode}})


def test_only_backtest_and_paper_modes_exist():
    assert {m.value for m in TradingMode} == {"backtest", "paper"}


@pytest.mark.parametrize("key", ["api_key", "API_SECRET", "password"])
def test_secrets_in_config_are_rejected(key):
    with pytest.raises(LiveTradingNotAllowedError):
        AppConfig.from_dict({"exchange": {key: "x"}})


def test_default_toml_loads():
    cfg = AppConfig.from_toml(ROOT / "config" / "default.toml")
    assert cfg.initial_capital == 100.0
    assert cfg.mode is TradingMode.BACKTEST
    assert cfg.risk.daily_loss_limit == pytest.approx(0.10)
    assert cfg.daily_volume_return_benchmark == pytest.approx(0.10)


def test_unknown_risk_parameter_rejected():
    with pytest.raises(ValueError):
        AppConfig.from_dict({"risk": {"leverage": 10}})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"risk_per_trade": 0},
        {"risk_per_trade": 0.5},
        {"daily_loss_limit": 1.0},
        {"max_position_fraction": 1.5},
    ],
)
def test_invalid_risk_config(kwargs):
    with pytest.raises(ValueError):
        RiskConfig(**kwargs)


def test_instrument_rounds_down_to_step():
    inst = Instrument("X", AssetClass.STOCK, qty_step=1.0)
    assert inst.round_qty(2.999) == 2.0
    assert inst.round_qty(-1) == 0.0
    crypto = Instrument("BTC", AssetClass.CRYPTO, qty_step=0.001)
    assert crypto.round_qty(0.12345) == pytest.approx(0.123)
    assert crypto.round_qty(0.003) == pytest.approx(0.003)  # keine Float-Abrundungsfehler


def test_default_instruments():
    assert Instrument.default_for("AAPL", "stock").qty_step == 1.0
    assert Instrument.default_for("BTC/EUR", "crypto").qty_step < 1
