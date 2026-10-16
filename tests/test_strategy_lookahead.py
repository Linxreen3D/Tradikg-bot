import numpy as np
import pandas as pd
import pytest

from tradikg_bot.backtest import BacktestEngine, LookAheadBiasError, check_no_lookahead
from tradikg_bot.costs import CRYPTO_SPOT_DEFAULT
from tradikg_bot.indicators import atr, ema, rsi, sma
from tradikg_bot.strategies import SmaCrossoverParams, SmaCrossoverStrategy
from tradikg_bot.strategies.base import Strategy, empty_signals

from conftest import make_bars


class PeekingStrategy(Strategy):
    """Absichtlich fehlerhaft: kauft, wenn der *morgige* Schlusskurs steigt."""

    name = "peeking"

    def generate_signals(self, bars):
        sig = empty_signals(bars.index)
        sig["entry"] = (bars["close"].shift(-1) > bars["close"]).fillna(False)
        sig["stop_distance"] = 1.0
        sig["take_profit_distance"] = 2.0
        return sig


class CenteredMaStrategy(Strategy):
    """Subtiler Fehler: zentrierter gleitender Durchschnitt nutzt Zukunftswerte."""

    name = "centered"

    def generate_signals(self, bars):
        ma = bars["close"].rolling(5, center=True).mean()
        sig = empty_signals(bars.index)
        sig["entry"] = (bars["close"] > ma).fillna(False)
        sig["stop_distance"] = 1.0
        sig["take_profit_distance"] = 2.0
        return sig


def test_indicators_match_manual_calculation():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert sma(s, 3).isna().sum() == 2
    expected, alpha = 1.0, 2 / 3
    for x in [2.0, 3, 4, 5]:
        expected += alpha * (x - expected)
    assert ema(s, 2).iloc[-1] == pytest.approx(expected)
    assert np.isnan(ema(s, 2).iloc[0])
    bars = make_bars([(10, 12, 9, 11), (11, 13, 10, 12), (12, 12, 8, 9)])
    tr = [3.0, 3.0, 4.0]
    assert atr(bars, 1).tolist() == tr
    up = pd.Series(np.arange(30.0))
    assert rsi(up, 14).iloc[-1] == pytest.approx(100.0)


@pytest.mark.parametrize("fn", [lambda s: sma(s, 10), lambda s: ema(s, 10), lambda s: rsi(s, 14)])
def test_indicators_are_causal(fn, synthetic_bars):
    close = synthetic_bars["close"]
    full = fn(close)
    truncated = fn(close.iloc[:300])
    pd.testing.assert_series_equal(full.iloc[:300], truncated)


def test_sma_crossover_signals():
    # fallend, dann steigend -> genau ein Aufwärtskreuz
    closes = list(np.linspace(100, 80, 30)) + list(np.linspace(80, 120, 30))
    bars = make_bars([(c, c + 1, c - 1, c) for c in closes])
    strat = SmaCrossoverStrategy(SmaCrossoverParams(fast=3, slow=8, trend=None, atr_window=5))
    sig = strat.generate_signals(bars)
    assert sig["entry"].sum() == 1
    entry_pos = int(np.flatnonzero(sig["entry"].to_numpy())[0])
    assert entry_pos > 30
    assert sig["stop_distance"].iloc[entry_pos] > 0
    assert sig["take_profit_distance"].iloc[entry_pos] > sig["stop_distance"].iloc[entry_pos]
    # Kein Signal während der Aufwärmphase
    assert not sig["entry"].iloc[: strat.warmup_bars - 1].any()


def test_invalid_params_rejected():
    with pytest.raises(ValueError):
        SmaCrossoverParams(fast=50, slow=20)


def test_baseline_strategy_passes_lookahead_check(synthetic_bars):
    report = check_no_lookahead(SmaCrossoverStrategy(), synthetic_bars, n_checkpoints=40)
    assert report.ok and len(report.checked_positions) > 10


@pytest.mark.parametrize("strategy", [PeekingStrategy(), CenteredMaStrategy()])
def test_lookahead_check_detects_cheating(strategy, synthetic_bars):
    with pytest.raises(LookAheadBiasError):
        check_no_lookahead(strategy, synthetic_bars)
    report = check_no_lookahead(strategy, synthetic_bars, raise_on_violation=False)
    assert not report.ok


def test_engine_refuses_cheating_strategy(crypto_instrument, synthetic_bars):
    engine = BacktestEngine(crypto_instrument, CRYPTO_SPOT_DEFAULT)
    with pytest.raises(LookAheadBiasError):
        engine.run(synthetic_bars, PeekingStrategy())
