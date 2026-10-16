from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradikg_bot.backtest import check_no_lookahead
from tradikg_bot.costs import CRYPTO_SPOT_DEFAULT
from tradikg_bot.data import generate_synthetic_ohlcv
from tradikg_bot.domain import Instrument
from tradikg_bot.features import FEATURE_COLUMNS, build_features, detect_regime
from tradikg_bot.research.engine import (
    ResearchConfig,
    ResearchEngine,
    StrategyResearch,
    TradeStats,
    Verdict,
    trade_stats,
)
from tradikg_bot.research.report import render_report
from tradikg_bot.research.store import ResearchStore
from tradikg_bot.strategies.families import strategy_registry

from edge_data import momentum_ohlcv

CFG = ResearchConfig(random_runs=10, wf_folds=3)
REGISTRY = strategy_registry()


@pytest.fixture(scope="module")
def random_walk():
    return generate_synthetic_ohlcv(1200, freq="1h", seed=21, periods_per_year=8760, annual_vol=0.7)


def engine():
    return ResearchEngine(Instrument.default_for("SYN", "crypto"), CRYPTO_SPOT_DEFAULT, CFG)


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_strategy_family_is_causal(name, random_walk):
    spec = REGISTRY[name]
    for params in spec.grid:
        assert check_no_lookahead(spec.build(params), random_walk, n_checkpoints=15).ok


def test_at_least_six_distinct_families():
    assert len({s.family for s in REGISTRY.values()}) >= 6


def test_features_and_regime_are_causal(random_walk):
    full_f, full_r = build_features(random_walk), detect_regime(random_walk)
    cut = random_walk.iloc[:700]
    pd.testing.assert_frame_equal(full_f.iloc[:700], build_features(cut))
    pd.testing.assert_series_equal(full_r.iloc[:700], detect_regime(cut))
    assert list(full_f.columns) == FEATURE_COLUMNS
    assert set(full_r.unique()) <= {"trend_up", "trend_down", "range", "high_volatility", "unknown"}


def test_random_walk_yields_no_confirmed_edge(random_walk):
    eng = engine()
    for name in ("sma_crossover", "momentum_breakout", "mean_reversion"):
        r = eng.research(REGISTRY[name], random_walk, "SYN", "synthetic")
        assert r.verdict != Verdict.CONFIRMED
        assert r.reasons


def test_splits_never_use_test_data_for_selection(random_walk):
    r = engine().research(REGISTRY["sma_crossover"], random_walk, "SYN", "synthetic")
    t_test = random_walk.index[int(len(random_walk) * 0.8)]
    assert all(t.entry_time >= t_test for t in r.test.trades)
    if r.validation:
        assert all(t.entry_time < t_test for t in r.validation.trades)
    if r.train:
        assert all(
            t.entry_time < random_walk.index[int(len(random_walk) * 0.6)] for t in r.train.trades
        )


def test_fee_sensitivity_is_monotone(random_walk):
    r = engine().research(REGISTRY["volume_breakout"], random_walk, "SYN", "synthetic")
    x0, x1, x2 = (r.fee_sensitivity[k] for k in ("x0", "x1", "x2"))
    if x0.n and x2.n and x0.n == x2.n:
        assert x0.net_profit >= x1.net_profit >= x2.net_profit


def test_pipeline_finds_positive_expectancy_where_edge_exists():
    bars = momentum_ohlcv(n=2500)
    r = engine().research(REGISTRY["sma_crossover"], bars, "EDGE", "synthetic-edge")
    assert r.test.stats.n > 0 and r.test.stats.expectancy_r > 0
    assert r.walk_forward.expectancy_r > 0


def _fake_result(**test) -> StrategyResearch:
    stats = TradeStats(**test)
    r = StrategyResearch(
        "x",
        "trend",
        "SYN",
        "t",
        True,
        {},
        "",
        None,
        None,
        None,
        TradeStats(n=50, expectancy_eur=0.1, expectancy_r=0.1),
    )
    from tradikg_bot.research.engine import SegmentResult

    r.test = SegmentResult({}, stats, [])
    r.fee_sensitivity = {"x2": TradeStats(n=stats.n, expectancy_eur=0.05)}
    r.random_baseline = {"p_value": 0.01}
    return r


def test_verdict_rules():
    eng = engine()
    good = _fake_result(n=40, expectancy_eur=0.2, expectancy_r=0.3, profit_factor=1.5)
    eng._judge(good)
    assert good.verdict == Verdict.CONFIRMED
    few = _fake_result(n=10, expectancy_eur=0.2, expectancy_r=0.3, profit_factor=1.5)
    eng._judge(few)
    assert few.verdict == Verdict.HYPOTHESIS and any("Test-Trades" in x for x in few.reasons)
    bad = _fake_result(n=40, expectancy_eur=-0.01, expectancy_r=-0.1, profit_factor=0.9)
    eng._judge(bad)
    assert bad.verdict == Verdict.REJECTED
    lucky = _fake_result(n=40, expectancy_eur=0.2, expectancy_r=0.3, profit_factor=1.5)
    lucky.random_baseline = {"p_value": 0.4}
    eng._judge(lucky)
    assert lucky.verdict == Verdict.HYPOTHESIS


def test_trade_stats_empty_and_basic():
    assert trade_stats([], 100.0).n == 0


def test_store_and_report_roundtrip(tmp_path, random_walk):
    eng = engine()
    results = [
        eng.research(REGISTRY[n], random_walk, "SYN", "synthetic")
        for n in ("sma_crossover", "volume_breakout")
    ]
    md = render_report(results, CFG, "synthetic", "1h")
    assert "synthetische Daten" in md and "keine Garantie" in md
    store = ResearchStore(tmp_path / "r.sqlite")
    run_id = store.save(results, md, "synthetic", "1h", {"random_runs": 10})
    stats = store.oos_stats(run_id)
    pooled = [s for s in stats if s.symbol == "*" and s.regime == "*"]
    assert {s.strategy for s in pooled} == {"sma_crossover", "volume_breakout"}
    for res in results:
        unique = {(t.entry_time, t.exit_time) for t in res.test.trades + res.walk_forward_trades}
        row = next(
            s for s in stats if s.strategy == res.strategy and s.symbol == "SYN" and s.regime == "*"
        )
        assert row.n == len(unique)  # nur OOS-Trades, Duplikate einmal
    assert store.report(run_id) == md
    assert not store.results(run_id).empty
    assert np.isfinite(len(store.runs()))
