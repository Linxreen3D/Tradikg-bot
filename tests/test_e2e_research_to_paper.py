"""Vertikaler Durchstich: Daten -> Research (OOS) -> Signal -> Kosten/Risiko -> virtueller
Trade -> Abschluss -> Ledger -> Nachanalyse -> SQLite (Dashboard-Abfragen)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tradikg_bot.costs import KRAKEN_SPOT_ASSUMED
from tradikg_bot.data import generate_synthetic_ohlcv
from tradikg_bot.domain import Instrument
from tradikg_bot.live.paper import generic_rules
from tradikg_bot.research.engine import ResearchConfig, ResearchEngine, research_many
from tradikg_bot.research.report import render_report
from tradikg_bot.research.store import ResearchStore
from tradikg_bot.strategies.families import strategy_registry
from tradikg_bot.trading.engine import PaperEngineConfig
from tradikg_bot.trading.probability import EmpiricalProbability
from tradikg_bot.trading.runner import PaperPaths, open_engine, replay, strategies_for
from tradikg_bot.trading.sources import PrecomputedSignals

from edge_data import momentum_ohlcv

NAMES = ("sma_crossover", "momentum_breakout", "volume_breakout")


def run_chain(tmp_path: Path, data: dict, source_name: str):
    registry = {k: strategy_registry()[k] for k in NAMES}
    hist = {k: v.iloc[:2000] for k, v in data.items()}
    live = {k: v.iloc[1700:] for k, v in data.items()}
    research = ResearchEngine(
        Instrument.default_for("X", "crypto"),
        KRAKEN_SPOT_ASSUMED,
        ResearchConfig(random_runs=10, wf_folds=3),
    )
    results = research_many(research, registry, hist, source_name)
    store = ResearchStore(tmp_path / "research.sqlite")
    run_id = store.save(
        results, render_report(results, research.cfg, source_name), source_name, "1h", {}
    )
    stats = store.oos_stats(run_id)
    rules = {k: generic_rules(k, float(v["close"].iloc[0])) for k, v in data.items()}
    source = PrecomputedSignals(live, strategies_for(list(data), stats, list(NAMES)))
    engine = open_engine(
        PaperPaths(tmp_path / "paper"),
        rules,
        EmpiricalProbability(stats),
        source,
        PaperEngineConfig(capital_eur=100.0),
    )
    return engine, replay(engine, live)


def test_chain_trades_when_out_of_sample_edge_exists(tmp_path):
    data = {
        "AAAEUR": momentum_ohlcv(2600, seed=11, sigma=0.02, phi=0.35),
        "BBBEUR": momentum_ohlcv(2600, seed=12, sigma=0.02, phi=0.35),
    }
    engine, reports = run_chain(tmp_path, data, "synthetic-edge")
    closed = [t for r in reports for t in r.closed]
    assert closed, "bei nachweisbarem OOS-Vorteil sollte gehandelt werden"
    b = engine.ledger.balances()
    realized = sum(engine.journal.get_trade(t).realized_pnl for t in closed)
    assert b.total_net_pnl == pytest.approx(realized)
    assert b.total_capital == pytest.approx(b.initial_capital + b.total_net_pnl)
    assert len(engine.store.analyses()) == len(closed)
    assert all(a["p_source"].startswith("oos") for a in engine.store.analyses())


def test_chain_does_not_trade_random_walk(tmp_path):
    data = {
        "AAAEUR": generate_synthetic_ohlcv(
            2600, freq="1h", seed=4, periods_per_year=8760, annual_vol=0.8, annual_drift=0.0
        )
    }
    engine, reports = run_chain(tmp_path, data, "synthetic")
    executed = [t for r in reports for t in r.executed]
    # Ohne nachweisbaren Vorteil darf (fast) nichts gehandelt werden; jede Entscheidung ist belegt
    assert len(executed) <= 2
    rows = engine.store.conn.execute(
        "SELECT COUNT(*) FROM signals_log WHERE status='rejected'"
    ).fetchone()
    assert rows[0] > 0
