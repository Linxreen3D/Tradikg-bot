"""Dashboard: Abfragen liefern die gespeicherten Ergebnisse korrekt; App läuft fehlerfrei."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from tradikg_bot.dashboard import queries as q
from tradikg_bot.platform_config import PlatformConfig
from tradikg_bot.trading.runner import PaperPaths, open_engine, replay

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
    assert "TOPSECRET" not in text and "deaktiviert" in q.settings(cfg)["live_mode"]


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
