"""Historical Market Replay: Daten, zeitliche Trennung, Look-Ahead-Freiheit, Ergebnisse."""

from __future__ import annotations

import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from tradikg_bot.cli import main
from tradikg_bot.live.paper import generic_rules
from tradikg_bot.replay import walkforward as wf
from tradikg_bot.replay.data import (
    coverage,
    import_kraken_ohlcvt,
    read_kraken_ohlcvt,
    synthetic_market_phases,
)
from tradikg_bot.replay.report import checklist, render_replay_report
from tradikg_bot.replay.store import ReplayStore, holdout_fingerprint
from tradikg_bot.replay.walkforward import (
    VARIANTS,
    HistoricalReplay,
    ReplayConfig,
    distribution,
    make_plan,
    max_drawdown,
    stress_volatility,
)

KRAKEN_CSV = "1704067200,100,110,95,105,12.5,40\n1704153600,105,108,101,107,8.0,31\n"
FAST = dict(
    research_random_runs=5,
    strategies=("momentum_breakout", "mean_reversion"),
    min_history_bars=100,
    seed_bars=300,
)


def phase_data(n: int = 420, symbols=("AAAEUR", "BBBEUR")) -> dict[str, pd.DataFrame]:
    return {
        s: synthetic_market_phases(n=n, seed=i + 1, base_price=100.0 * (i + 1), correlation_seed=5)
        for i, s in enumerate(symbols)
    }


# -- Daten ----------------------------------------------------------------------------------
def test_kraken_ohlcvt_csv_has_no_header_and_seconds():
    bars = read_kraken_ohlcvt(io.StringIO(KRAKEN_CSV))
    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
    assert bars.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert len(bars) == 2 and bars["close"].iloc[-1] == 107


def test_import_kraken_zip_picks_requested_pair_and_interval(tmp_path):
    archive = tmp_path / "Kraken_OHLCVT.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("XBTEUR_1440.csv", KRAKEN_CSV)
        zf.writestr("XBTEUR_60.csv", KRAKEN_CSV)
        zf.writestr("ETHEUR_60.csv", KRAKEN_CSV)
    data = import_kraken_ohlcvt(archive, ["XBTEUR", "ETHEUR"], "1d")
    assert list(data) == ["XBTEUR"] and len(data["XBTEUR"]) == 2
    folder = tmp_path / "csv"
    folder.mkdir()
    (folder / "ETHEUR_60.csv").write_text(KRAKEN_CSV)
    assert list(import_kraken_ohlcvt(folder, ["ETHEUR"], "1h")) == ["ETHEUR"]


def test_coverage_reports_gaps_and_market_phases():
    bars = synthetic_market_phases(n=400, seed=3)
    gappy = bars.drop(bars.index[[50, 51, 52]])
    (c,) = coverage({"X": gappy}, "1d")
    assert c.bars == 397 and c.missing_bars == 3
    assert {"trend_up", "trend_down"} <= set(c.regime_share)


def test_synthetic_phases_are_valid_and_contain_crash():
    bars = synthetic_market_phases(n=730, seed=2)
    assert (bars["high"] >= bars[["open", "close"]].max(axis=1)).all()
    assert (bars["low"] <= bars[["open", "close"]].min(axis=1)).all()
    assert (bars["low"] > 0).all()
    assert max_drawdown(bars["close"], bars["close"].iloc[0]) < -0.2


# -- Zeitplan ----------------------------------------------------------------------------------
def test_plan_separates_research_trading_and_holdout():
    idx = pd.date_range("2024-01-01", periods=500, freq="1D", tz="UTC")
    plan = make_plan(idx, ReplayConfig(min_history_bars=100))
    assert plan.cutoffs[0] >= 100
    assert list(plan.cutoffs) == sorted(plan.cutoffs)
    assert plan.cutoffs[-1] < plan.holdout_start < plan.end
    assert all(e <= plan.holdout_start for _, e in plan.windows)
    for i in range(plan.cutoffs[0], plan.end):
        assert plan.research_cutoff(i) <= i
    with pytest.raises(ValueError):
        plan.research_cutoff(plan.cutoffs[0] - 1)
    with pytest.raises(ValueError, match="Zu wenig Daten"):
        make_plan(idx[:120], ReplayConfig(min_history_bars=100))


def test_research_only_ever_sees_data_before_its_cutoff(tmp_path, monkeypatch):
    seen: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    real = wf.research_many

    def spy(engine, specs, data, source):
        seen.append(max(df.index[-1] for df in data.values()))
        return real(engine, specs, data, source)

    monkeypatch.setattr(wf, "research_many", spy)
    replay = HistoricalReplay(phase_data(), tmp_path, ReplayConfig(**FAST), "synthetic", True)
    plan = replay.plan
    replay.run_segment(
        "wf",
        "walk_forward",
        plan.cutoffs[0],
        plan.end,
        replay.cfg.profile("eur100"),
        VARIANTS["bot"],
    )
    assert sorted(replay.research.by_cutoff) == list(plan.decision_points)
    for cutoff, last_seen in zip(plan.decision_points, seen, strict=True):
        assert last_seen < plan.index[cutoff]
    # Die Holdout-Research endet vor dem Holdout
    assert seen[-1] < plan.index[plan.holdout_start]


@pytest.mark.parametrize("variant", ["no_filter", "random"])
def test_changing_the_future_does_not_change_past_decisions(tmp_path, variant):
    """Look-Ahead-Test über die ganze Kette (Research, Signale, Engine, Ledger)."""
    data = phase_data()
    cfg = ReplayConfig(**FAST)
    a = HistoricalReplay(data, tmp_path / "a", cfg, "synthetic", True)
    plan = a.plan
    t_change = plan.cutoffs[2] + 5
    changed = {}
    rng = np.random.default_rng(0)
    for s, df in data.items():
        df = df.copy()
        future = df.index >= plan.index[t_change]
        shock = rng.uniform(0.6, 1.4, future.sum())
        df.loc[future, ["open", "high", "low", "close"]] = df.loc[
            future, ["open", "high", "low", "close"]
        ].mul(shock, axis=0)
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        changed[s] = df
    b = HistoricalReplay(changed, tmp_path / "b", cfg, "synthetic", True)
    prof = cfg.profile("eur100")
    ra = a.run_segment(
        "x", "walk_forward", plan.cutoffs[0], plan.holdout_start, prof, VARIANTS[variant]
    )
    rb = b.run_segment(
        "x", "walk_forward", plan.cutoffs[0], plan.holdout_start, prof, VARIANTS[variant]
    )
    cut = plan.index[t_change]
    before_a = ra.equity[ra.equity.index <= cut]
    before_b = rb.equity[rb.equity.index <= cut]
    pd.testing.assert_frame_equal(before_a, before_b)
    entries_a = [t for t in ra.trades if pd.Timestamp(t["exit_ts"]) < cut]
    entries_b = [t for t in rb.trades if pd.Timestamp(t["exit_ts"]) < cut]
    assert entries_a == entries_b
    assert ra.n_trades > 0  # der Test ist nur aussagekräftig, wenn gehandelt wurde
    assert not ra.equity.equals(rb.equity)  # und die Zukunft tatsächlich anders war


# -- Szenarien ----------------------------------------------------------------------------------
def test_ledger_split_and_equity_are_consistent(tmp_path):
    replay = HistoricalReplay(phase_data(), tmp_path, ReplayConfig(**FAST), "synthetic", True)
    plan = replay.plan
    s = replay.run_segment(
        "nf",
        "walk_forward",
        plan.cutoffs[0],
        plan.holdout_start,
        replay.cfg.profile("eur100"),
        VARIANTS["no_filter"],
    )
    led = s.ledger
    total = led["trading_balance"] + led["payout_reserve"] + led["longterm_portfolio"]
    assert total == pytest.approx(100 + led["realized_net_pnl"], abs=1e-6)
    assert sum(t["net_pnl_eur"] for t in s.trades) == pytest.approx(
        led["realized_net_pnl"], abs=1e-6
    )
    if led["payout_reserve"] > 0:  # 33/33/34: Langfrist erhält den Rundungsrest
        assert led["longterm_portfolio"] >= led["payout_reserve"]
    assert s.equity["equity"].iloc[0] > 0 and s.max_drawdown <= 0


def test_buy_and_hold_respects_minimum_order_size(tmp_path):
    data = phase_data(symbols=("AAAEUR", "BBBEUR", "CCCEUR"))
    rules = {s: generic_rules(s, float(df["close"].iloc[0])) for s, df in data.items()}
    replay = HistoricalReplay(data, tmp_path, ReplayConfig(**FAST), "synthetic", True, rules=rules)
    plan = replay.plan
    p10 = replay.cfg.profile("eur10")
    bh = replay.baseline("bh", "walk_forward", plan.cutoffs[0], plan.holdout_start, p10, "buy_hold")
    assert "nur 2 von 3 Assets" in bh.note
    cash = replay.baseline("c", "walk_forward", plan.cutoffs[0], plan.holdout_start, p10, "cash")
    assert cash.final_equity == 10.0 and cash.max_drawdown == 0.0
    # Kosten: sofortiger Kauf und Verkauf kostet Gebühren und Spread
    flat = {s: df.assign(open=100.0, high=100.0, low=100.0, close=100.0) for s, df in data.items()}
    r2 = HistoricalReplay(flat, tmp_path / "f", ReplayConfig(**FAST), "synthetic", True)
    b2 = r2.baseline(
        "b",
        "walk_forward",
        plan.cutoffs[0],
        plan.holdout_start,
        r2.cfg.profile("eur100"),
        "buy_hold",
    )
    expected = 100 * (1 - 0.0005) * (1 - 0.004) / ((1 + 0.0005) * (1 + 0.004))
    assert b2.final_equity == pytest.approx(expected, rel=1e-9)


def test_volatility_stress_changes_only_the_future():
    bars = synthetic_market_phases(n=300, seed=4)
    start = bars.index[200]
    stressed = stress_volatility(bars, start, gap_prob=0.2, seed=1)
    pd.testing.assert_frame_equal(stressed[stressed.index < start], bars[bars.index < start])
    after = stressed[stressed.index >= start]
    assert (after["high"] >= after[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (after["low"] <= after[["open", "close"]].min(axis=1) + 1e-9).all()
    assert (after["low"] > 0).all()
    orig = bars[bars.index >= start]
    assert (after["high"] / after["low"]).mean() > (orig["high"] / orig["low"]).mean()
    gaps = (after["open"] / after["close"].shift(1) - 1).abs()
    assert gaps.max() > (orig["open"] / orig["close"].shift(1) - 1).abs().max()


def test_distribution_keeps_negative_results():
    d = distribution([80.0, 90.0, 130.0], 100.0)
    assert d["min"] == 80.0 and d["max"] == 130.0
    assert d["share_loss"] == pytest.approx(2 / 3)


def test_full_run_report_store_and_holdout_counter(tmp_path):
    cfg = ReplayConfig(**FAST, variants=("bot", "fees_x2", "tp_first", "random"))
    replay = HistoricalReplay(phase_data(), tmp_path / "work", cfg, "synthetisch", True)
    result = replay.run()
    periods = {s.period for s in result.scenarios}
    assert periods == {"walk_forward", "holdout", "window"}
    hold = result.select(period="holdout", variant="bot", profile="eur100")[0]
    assert hold.start == replay.plan.index[replay.plan.holdout_start]
    assert result.select(variant="tp_first")[0].upper_bound
    store = ReplayStore(tmp_path / "replays.sqlite")
    fp = holdout_fingerprint(replay.data, replay.plan, "1d")
    assert store.register_holdout(fp) == 1
    report = render_replay_report(result, 1)
    for text in (
        "Was wäre aus 100 € geworden?",
        "Synthetische Daten",
        "Verteilung über alle Zeitfenster",
        "Holdout (unangetastet)",
        "Gesamturteil: nicht belastbar",
        "33 % Trading",
    ):
        assert text in report
    run_id = store.save(result, report, fp, 1)
    assert len(store.scenarios(run_id)) == len(result.scenarios)
    eq = store.equity(run_id, [hold.scenario_id])
    assert len(eq) == len(hold.equity)
    assert store.register_holdout(fp) == 2
    assert "bereits 2-mal" in render_replay_report(result, 2)
    rows = dict((name, ok) for name, ok, _ in checklist(result, 2))
    assert rows["Echte Marktdaten (nicht synthetisch)"] is False
    assert rows["Holdout erstmals ausgewertet (unangetastet)"] is False


# -- CLI ------------------------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADIKG_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_cli_data_import_coverage_and_replay(home, tmp_path, capsys):
    folder = tmp_path / "kraken"
    folder.mkdir()
    bars = synthetic_market_phases(n=400, seed=1)
    rows = np.column_stack(
        [
            bars.index.asi8 // 10**9,
            bars[["open", "high", "low", "close", "volume"]].to_numpy(),
            np.full(len(bars), 7),
        ]
    )
    pd.DataFrame(rows).to_csv(folder / "AAAEUR_1440.csv", header=False, index=False)
    assert main(["data", "import-kraken", str(folder), "--symbols", "AAAEUR"]) == 0
    assert main(["data", "coverage", "--symbols", "AAAEUR"]) == 0
    out = capsys.readouterr().out
    assert "400 Kerzen importiert" in out and "kraken-ohlcvt" in out
    args = [
        "replay",
        "run",
        "--symbols",
        "AAAEUR",
        "--quick",
        "--no-windows",
        "--research-random-runs",
        "3",
        "--strategies",
        "mean_reversion",
        "--min-history-bars",
        "100",
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Keine echten Orders" in out and "100 € im holdout" in out
    assert (home / "reports" / "replay-1.md").exists()
    assert main(args) == 0
    assert "bereits 1-mal ausgewertet" in capsys.readouterr().out
    assert main(["replay", "report"]) == 0
    assert "Holdout dieser Daten wurde bereits 2-mal" in capsys.readouterr().out


def test_quality_flags_gaps_invalid_outliers_and_open_last_bar():
    from tradikg_bot.replay.data import quality

    bars = synthetic_market_phases(n=300, seed=4)
    bars = bars.drop(bars.index[[100, 101]])
    bars.iloc[150, bars.columns.get_loc("close")] *= 5  # Ausreißer, zugleich high < close
    bars.iloc[200, bars.columns.get_loc("volume")] = 0
    q = quality(bars, "1d", now=bars.index[-1] + pd.Timedelta(hours=1))
    assert q["missing_bars"] == 2 and q["gap_count"] == 1 and q["largest_gap"].startswith("3 days")
    assert q["invalid_rows"] == 1 and q["zero_volume_bars"] == 1 and q["timezone_utc"]
    spike = {o["ts"][:10] for o in q["outliers"]}  # Sprung hoch und zurück = 2 Renditen
    assert {str(bars.index[150].date()), str(bars.index[151].date())} <= spike
    assert q["last_bar_incomplete"]
    assert not quality(bars, "1d", now=bars.index[-1] + pd.Timedelta(days=2))["last_bar_incomplete"]


def test_cli_data_quality_writes_report(home, tmp_path, capsys):
    folder = tmp_path / "kraken"
    folder.mkdir()
    (folder / "AAAEUR_1440.csv").write_text(KRAKEN_CSV + "1704153600,105,108,101,107,8.0,31\n")
    assert main(["data", "import-kraken", str(folder), "--symbols", "AAAEUR"]) == 0
    assert main(["data", "quality", "--symbols", "AAAEUR", "--timeframes", "1d,1h"]) == 0
    out = capsys.readouterr().out
    assert "Duplikate entfernt: 1" in out and "AAAEUR   1h   keine Daten" in out
    (md,) = (home / "reports").glob("data-quality-*.md")
    assert "| AAAEUR | 1d | kraken-ohlcvt | 2 |" in md.read_text()
