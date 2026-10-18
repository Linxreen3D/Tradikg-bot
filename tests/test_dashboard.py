"""Dashboard: Abfragen liefern die gespeicherten Ergebnisse korrekt; App läuft fehlerfrei."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from tradikg_bot.dashboard import queries as q
from tradikg_bot.platform_config import PlatformConfig
from tradikg_bot.trading.runner import PaperPaths, open_engine, replay

from helpers import MECHANICS_FEES
from test_paper_engine import FixedProbability, ScriptedSource, bars_frame

APP = Path(__file__).resolve().parents[1] / "src" / "tradikg_bot" / "dashboard" / "app.py"


@pytest.fixture
def populated(tmp_path):
    cfg = PlatformConfig(home=tmp_path / "home", symbols=("AAAEUR",))
    cfg.ensure_dirs()
    from tradikg_bot.live.paper import generic_rules
    from tradikg_bot.trading.engine import PaperEngineConfig

    data = bars_frame([100.0] * 40)
    data.loc[data.index[20] :, "high"] = 110.0  # TP wird erreicht
    engine = open_engine(
        PaperPaths(cfg.paper_dir),
        {"AAAEUR": generic_rules("AAAEUR", 100.0)},
        FixedProbability(),
        ScriptedSource({data.index[15]}),
        PaperEngineConfig(capital_eur=cfg.capital_eur, min_history_bars=10),
        fees=MECHANICS_FEES,
    )
    replay(engine, {"AAAEUR": data})
    return cfg, engine


def test_queries_match_engine_state(populated):
    cfg, engine = populated
    b = q.balances(cfg)
    lb = engine.ledger.balances()
    assert b["trading_balance"] == pytest.approx(lb.trading_balance)
    assert b["payout_reserve"] == pytest.approx(lb.payout_reserve)
    assert b["longterm_portfolio"] == pytest.approx(lb.longterm_portfolio)
    trades = q.closed_trades(cfg)
    assert len(trades) == 1 and trades.iloc[0]["realized_pnl"] == pytest.approx(lb.total_net_pnl)
    assert "strategy" in trades.columns and "assessment" in trades.columns
    perf = q.performance(cfg)
    assert perf["overall"]["trades"] == 1 and perf["overall"]["win_rate"] == 1.0
    curve = q.equity_curve(cfg)
    assert not curve.empty and (curve["drawdown"] <= 0).all()
    assert q.pnl_windows(cfg, now=pd.Timestamp("2026-01-02", tz="UTC"))["total"] == pytest.approx(
        lb.total_net_pnl
    )
    assert not q.recent_signals(cfg).empty


def test_queries_on_fresh_install_are_empty_not_failing(tmp_path):
    cfg = PlatformConfig(home=tmp_path / "fresh")
    assert q.balances(cfg)["trading_balance"] == cfg.capital_eur
    assert q.closed_trades(cfg).empty and q.equity_curve(cfg).empty
    assert q.research_results(cfg).empty and q.models(cfg).empty and q.calibration(cfg).empty
    assert q.health(cfg)["safe_halt"] is None


def test_settings_contain_no_secrets(populated, monkeypatch):
    monkeypatch.setenv("KRAKEN_API_SECRET", "TOPSECRET")
    cfg, _ = populated
    text = str(q.settings(cfg)) + str(q.health(cfg)["status"])
    assert "TOPSECRET" not in text
    assert q.settings(cfg)["echtgeld_bot (tradikg live)"] == "aus"  # ohne laufenden Prozess


def test_queries_are_read_only(populated):
    cfg, _ = populated
    before = q.paper_db(cfg).stat().st_mtime_ns
    q.balances(cfg), q.closed_trades(cfg), q.health(cfg)
    assert q.paper_db(cfg).stat().st_mtime_ns == before


def test_streamlit_app_renders(populated, monkeypatch):
    testing = pytest.importorskip("streamlit.testing.v1")
    cfg, engine = populated
    monkeypatch.setenv("TRADIKG_HOME", str(cfg.home))
    at = testing.AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    labels = {m.label: m.value for m in at.metric}
    expected = f"{engine.ledger.balances().trading_balance:,.2f} €"
    assert labels["Trading-Balance"] == expected
    assert any("System bereit" in s.value for s in at.success)


def test_historical_replay_tab_shows_what_100_eur_became(tmp_path, monkeypatch):
    testing = pytest.importorskip("streamlit.testing.v1")
    from tradikg_bot.replay.data import synthetic_market_phases
    from tradikg_bot.replay.report import render_replay_report
    from tradikg_bot.replay.store import ReplayStore, holdout_fingerprint
    from tradikg_bot.replay.walkforward import HistoricalReplay, ReplayConfig

    cfg = PlatformConfig(home=tmp_path / "home")
    cfg.ensure_dirs()
    data = {"AAAEUR": synthetic_market_phases(n=320, seed=2)}
    rcfg = ReplayConfig(
        research_random_runs=3,
        strategies=("mean_reversion",),
        min_history_bars=100,
        variants=("bot", "random"),
        folds=2,
    )
    replay = HistoricalReplay(data, tmp_path / "work", rcfg, "synthetisch", True)
    result = replay.run()
    store = ReplayStore(cfg.replay_db)
    fp = holdout_fingerprint(replay.data, replay.plan, "1d")
    run_id = store.save(result, render_replay_report(result, 1), fp, store.register_holdout(fp))
    store.close()
    assert not q.replay_runs(cfg).empty
    sc = q.replay_scenarios(cfg, run_id)
    assert set(sc["period"]) == {"walk_forward", "holdout", "window"}

    monkeypatch.setenv("TRADIKG_HOME", str(cfg.home))
    at = testing.AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    labels = {m.label: m.value for m in at.metric}
    bot = sc[(sc["scenario_id"] == "walk_forward-bot-eur100")].iloc[0]
    assert labels["Bot – Endwert"] == f"{bot['final_equity']:,.2f} €"
    assert "Schlechtestes Fenster" in labels and "Fenster mit Verlust" in labels
    assert any("Synthetische Daten" in w.value for w in at.warning)


def test_replay_queries_on_fresh_install_are_empty(tmp_path):
    cfg = PlatformConfig(home=tmp_path / "fresh")
    assert q.replay_runs(cfg).empty and q.replay_run(cfg, 1) is None
    assert q.replay_scenarios(cfg, 1).empty and q.replay_strategy_stats(pd.DataFrame()).empty


def test_cockpit_reads_live_state_and_order_limits(tmp_path):
    import json

    from tradikg_bot.dashboard import cockpit
    from tradikg_bot.platform_config import PlatformConfig

    cfg = PlatformConfig(home=tmp_path)
    live = tmp_path / "live"
    live.mkdir()
    (live / "trend_state.json").write_text(json.dumps({"symbols": {"XBTEUR": {"buy_txid": "T1"}}}))
    (live / "live.log").write_text(
        "2026-10-18 01:52:12 UTC  [ORDER] buy XBTEUR 0.00013006 @ 72460.7 → T1\n"
    )
    state, limits, log = cockpit._live(cfg)
    assert state["XBTEUR"]["buy_txid"] == "T1" and limits == {"T1": 72460.7} and len(log) == 1
    assert cockpit._alive(tmp_path / "fehlt.pid") is None


def test_cockpit_renders_without_errors(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("TRADIKG_HOME", str(tmp_path))
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "src/tradikg_bot/dashboard/app.py")
    )
    app.run(timeout=60)
    assert not app.exception
