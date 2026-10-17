"""End-to-End- und Szenario-Tests des autonomen Paper-Kreislaufs."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradikg_bot.live.paper import PaperExchange, generic_rules
from tradikg_bot.trading.engine import (
    AutonomyNotAuthorizedError,
    Bar,
    PaperEngineConfig,
    PaperTradingEngine,
)
from tradikg_bot.trading.probability import ProbabilityEstimate
from tradikg_bot.trading.runner import PaperPaths, open_engine, replay
from tradikg_bot.trading.sources import RawSignal

from helpers import MECHANICS_FEES


class FixedProbability:
    def __init__(self, p: float = 0.6):
        self.p = p

    def estimate(self, strategy, symbol, regime, reward_risk, features=None):
        return ProbabilityEstimate(self.p, "fest (Test)", 100)


class ScriptedSource:
    """Einstiegssignal an vorgegebenen Zeitpunkten (oder immer)."""

    def __init__(self, at: set[pd.Timestamp] | None = None, stop=2.0, take=5.0, strategies=("s1",)):
        self.at, self.stop, self.take, self.strategies = at, stop, take, strategies

    def signals_at(self, symbol, ts, history):
        fire = self.at is None or ts in self.at
        return [
            RawSignal(s, fire, self.stop * (1 + i * 0.1), self.take, "range", None)
            for i, s in enumerate(self.strategies)
        ]


def bars_frame(closes, start="2026-01-01", vol=1e6) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    idx = pd.date_range(start, periods=len(closes), freq="1h", tz="UTC", name="timestamp")
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) * 1.001,
            "low": np.minimum(opens, closes) * 0.999,
            "close": closes,
            "volume": vol,
        },
        index=idx.as_unit("ns"),
    )


def flat(n=320, price=100.0):
    return bars_frame([price] * n)


CFG = PaperEngineConfig(capital_eur=100.0, min_history_bars=10)


def make_engine(tmp_path, symbols=("AAAEUR",), source=None, p=0.6, cfg=CFG, price=100.0):
    rules = {s: generic_rules(s, price) for s in symbols}
    return open_engine(
        PaperPaths(Path(tmp_path)),
        rules,
        FixedProbability(p),
        source or ScriptedSource(),
        cfg,
        fees=MECHANICS_FEES,
    )


def totals(engine):
    return engine.ledger.balances(), [t for t in engine.journal.trades()]


# -- Grundablauf ----------------------------------------------------------------------------
def test_full_cycle_opens_closes_books_and_analyzes(tmp_path):
    data = flat(40)
    entry_ts = data.index[15]
    up = data.copy()
    up.loc[up.index[17] :, ["close", "high"]] = [[106.0, 106.5]] * len(up.loc[up.index[17] :])
    up["open"] = up["close"].shift(1).fillna(100.0)
    up["low"] = np.minimum(up["open"], up["close"]) * 0.999
    engine = make_engine(tmp_path, source=ScriptedSource({entry_ts}))
    reports = replay(engine, {"AAAEUR": up})
    executed = [t for r in reports for t in r.executed]
    closed = [t for r in reports for t in r.closed]
    assert executed and closed == executed
    trade = engine.journal.get_trade(closed[0])
    assert trade.exit_reason == "take_profit" and trade.realized_pnl > 0
    b = engine.ledger.balances()
    assert b.total_net_pnl == pytest.approx(trade.realized_pnl)
    assert b.payout_reserve == pytest.approx(trade.realized_pnl * 0.33)
    analysis = engine.store.analyses()
    assert analysis[0]["trade_id"] == trade.id and analysis[0]["outcome_win"]
    for key in ("mfe", "mae", "fees_eur", "p_win", "regime", "strategy", "expected_ev_eur"):
        assert key in analysis[0]
    # Einstieg zur Eröffnung der Folgekerze, nicht zum Schluss der Signalkerze
    meta = engine.store.trade_meta(trade.id)
    assert pd.Timestamp(meta["entry_ts"]) == data.index[16]


def test_no_signals_means_no_trades(tmp_path):
    engine = make_engine(tmp_path, source=ScriptedSource(set()))
    reports = replay(engine, {"AAAEUR": flat(50)})
    assert not any(r.executed for r in reports)
    assert engine.ledger.balances().total_net_pnl == 0


def test_costs_exceeding_edge_block_trades(tmp_path):
    # p = 0.34 bei TP/SL = 2 -> ohne Vorteil; nach Kosten negativer EV
    engine = make_engine(tmp_path, p=0.34)
    replay(engine, {"AAAEUR": flat(40)})
    assert engine.journal.trades() == []
    reasons = engine.store.conn.execute(
        "SELECT reason FROM signals_log WHERE reason LIKE 'Netto-EV%'"
    ).fetchall()
    assert reasons


def test_no_fixed_daily_trade_cap_many_symbols_trade_same_day(tmp_path):
    symbols = [f"S{i:02d}EUR" for i in range(15)]
    ts = flat(30).index[15]
    engine = make_engine(
        tmp_path,
        symbols,
        source=ScriptedSource({ts}),
        cfg=PaperEngineConfig(capital_eur=10_000.0, min_history_bars=10),
    )
    engine.gw.balances_["ZEUR"] = 10_000.0
    # kleines Risiko je Trade, damit Kapital für alle 15 Signale reicht
    engine.executor.cfg = engine.risk.__class__.for_paper(
        10_000.0,
        allowed_pairs=tuple(symbols),
        risk_per_trade_fraction=0.001,
        max_loss_per_trade_eur=10.0,
    )
    reports = replay(engine, {s: flat(30) for s in symbols})
    executed = [t for r in reports for t in r.executed]
    assert len(executed) == 15  # 15 geeignete Signale -> 15 Trades, keine feste Grenze


def test_limited_capital_goes_to_best_ranked_signal_first(tmp_path):
    symbols = ["AAAEUR", "BBBEUR", "CCCEUR"]
    ts = flat(30).index[15]

    class Ranked(ScriptedSource):
        def signals_at(self, symbol, t, history):
            take = {"AAAEUR": 4.0, "BBBEUR": 8.0, "CCCEUR": 6.0}[symbol]
            return [RawSignal("s1", t == ts, 2.0, take, "range", None)]

    cfg = PaperEngineConfig(capital_eur=12.0, min_history_bars=10)
    engine = make_engine(tmp_path, symbols, source=Ranked(), cfg=cfg)
    engine.gw.balances_["ZEUR"] = 12.0
    engine.executor.cfg = engine.risk.__class__.for_paper(
        12.0, allowed_pairs=tuple(symbols), risk_per_trade_fraction=0.1, max_loss_per_trade_eur=1.2
    )
    reports = replay(engine, {s: flat(30) for s in symbols})
    executed = [t for r in reports for t in r.executed]
    assert len(executed) >= 1
    first = engine.journal.get_trade(executed[0])
    assert first.pair == "BBBEUR"  # bestes Chance/Risiko zuerst
    assert any(
        "Exposure" in v or "Guthaben" in v or "Mindest" in v
        for r in reports
        for v in r.rejected.values()
    )


def test_only_5_eur_available_trades_nothing(tmp_path):
    cfg = PaperEngineConfig(capital_eur=5.0, min_history_bars=10)
    engine = make_engine(tmp_path, cfg=cfg)
    engine.gw.balances_["ZEUR"] = 5.0
    reports = replay(engine, {"AAAEUR": flat(40)})
    assert engine.journal.trades() == []
    assert any("Mindestvolumen" in v for r in reports for v in r.rejected.values())


def test_20_consecutive_signals_no_duplicates_and_cooldown(tmp_path):
    engine = make_engine(tmp_path, source=ScriptedSource(None))  # Signal auf jeder Kerze
    data = bars_frame(100 * 0.97 ** np.arange(40))  # fallender Markt: jede Position stoppt aus
    replay(engine, {"AAAEUR": data})
    trades = engine.journal.trades()
    entry_times = [engine.store.trade_meta(t.id)["entry_ts"] for t in trades]
    assert len(entry_times) == len(set(entry_times))
    assert len(trades) < 20  # Cooldowns nach Verlustserien bremsen Re-Entry-Schleifen
    _, losses, reason = engine.store.cooldown("AAAEUR")
    assert losses >= 2 and "Verlust" in reason
    keys = engine.store.conn.execute("SELECT COUNT(*), COUNT(DISTINCT key) FROM intents").fetchone()
    assert keys[0] == keys[1]


def test_daily_loss_shutdown(tmp_path):
    # Einstieg, dann Kurslücke weit unter den Stop -> Verlust > 10 % der Balance
    cfg = PaperEngineConfig(capital_eur=100.0, min_history_bars=10)
    engine = make_engine(
        tmp_path,
        source=ScriptedSource({flat(1).index[0] + pd.Timedelta(hours=15)}, stop=1.0, take=5.0),
        cfg=cfg,
    )
    engine.risk = engine.executor.cfg = engine.risk.__class__.for_paper(
        100.0, allowed_pairs=("AAAEUR",), max_loss_per_trade_eur=10.0, risk_per_trade_fraction=0.1
    )
    frame = bars_frame([100.0] * 20 + [40.0] * 11, start="2026-01-01 00:00")
    frame.iloc[20, frame.columns.get_loc("open")] = 40.0  # echte Kurslücke unter den Stop
    frame.iloc[20, frame.columns.get_loc("high")] = 40.1
    reports = replay(engine, {"AAAEUR": frame})
    assert engine.journal.trades()[0].realized_pnl < -10.0
    halted = [r for r in reports if r.blocked_reason == "Tagesverlust-Abschaltschwelle erreicht"]
    assert halted
    day = halted[0].ts.date().isoformat()
    assert engine.store.get(f"day_halt:{day}")
    after = [
        t
        for r in reports
        if r.ts >= halted[0].ts and r.ts.date() == halted[0].ts.date()
        for t in r.executed
    ]
    assert after == []


def test_kill_switch_blocks_entries_but_exits_still_processed(tmp_path):
    data = flat(40)
    engine = make_engine(tmp_path, source=ScriptedSource({data.index[15], data.index[25]}))
    replay(engine, {"AAAEUR": data.iloc[:18]})
    assert len(engine.journal.trades(active_only=True)) == 1
    engine.kill.engage("Test")
    moved = data.iloc[18:].copy()
    moved.loc[moved.index[2] :, ["high"]] = 110.0
    reports = replay(engine, {"AAAEUR": moved})
    assert any(r.closed for r in reports)  # TP wird weiter verarbeitet
    assert not any(r.executed for r in reports)
    assert all(r.blocked_reason == "Kill Switch aktiv" for r in reports)


def test_restart_resumes_without_double_processing(tmp_path):
    data = bars_frame(100 + np.cumsum(np.random.default_rng(1).normal(0, 0.8, 120)))
    fires = set(data.index[15::7])
    engine = make_engine(tmp_path / "a", source=ScriptedSource(fires))
    replay(engine, {"AAAEUR": data})
    ref_balance, ref_trades = totals(engine)

    first = make_engine(tmp_path / "b", source=ScriptedSource(fires))
    replay(first, {"AAAEUR": data.iloc[:60]})
    first.store.close()
    second = make_engine(tmp_path / "b", source=ScriptedSource(fires))  # "Mac startet neu"
    reports = replay(second, {"AAAEUR": data})  # gleiche Kerzen erneut gemeldet
    assert all(r.skipped for r in reports[:60])
    bal, trades = totals(second)
    assert len(trades) == len(ref_trades)
    assert bal.total_net_pnl == pytest.approx(ref_balance.total_net_pnl)


def test_duplicate_bar_event_is_idempotent_and_thread_safe(tmp_path):
    engine = make_engine(tmp_path)
    ts = pd.Timestamp("2026-01-01 00:00", tz="UTC")
    bar = Bar(100, 100.1, 99.9, 100, 1e6)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(engine.step(ts, {"AAAEUR": bar})))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(not r.skipped for r in results) == 1


def test_gap_in_data_blocks_entries(tmp_path):
    data = flat(40)
    gapped = data.drop(data.index[14])  # fehlende Kerze vor dem Signal
    engine = make_engine(tmp_path, source=ScriptedSource({data.index[15]}))
    replay(engine, {"AAAEUR": gapped})
    assert engine.journal.trades() == []
    row = engine.store.conn.execute(
        "SELECT reason FROM signals_log WHERE reason LIKE 'Datenqualität%'"
    ).fetchone()
    assert "Datenlücke" in row[0]


def test_stale_feed_is_detected(tmp_path):
    engine = make_engine(tmp_path)
    replay(engine, {"AAAEUR": flat(20)})
    engine.now = engine.now + pd.Timedelta(hours=5)  # Feed steht seit Stunden
    assert "veraltete Daten" in engine._data_flags("AAAEUR", engine.now)


def test_timeout_after_accept_is_reconciled(tmp_path):
    data = flat(30)
    engine = make_engine(tmp_path, source=ScriptedSource({data.index[15]}))
    replay(engine, {"AAAEUR": data.iloc[:16]})
    engine.gw.fail_next = "timeout_after_accept"
    reports = replay(engine, {"AAAEUR": data.iloc[16:]})
    assert sum(len(r.executed) for r in reports) == 1
    assert not engine.executor.halted


def test_unknown_order_outcome_halts_safely(tmp_path):
    data = flat(30)
    engine = make_engine(tmp_path, source=ScriptedSource({data.index[15], data.index[20]}))
    replay(engine, {"AAAEUR": data.iloc[:16]})
    engine.gw.fail_next = "timeout_before_accept"
    reports = replay(engine, {"AAAEUR": data.iloc[16:]})
    assert engine.executor.halted
    assert any((r.blocked_reason or "").startswith("SAFE_HALT") for r in reports)


def test_rejected_stop_loss_order_halts(tmp_path):
    data = flat(30)
    engine = make_engine(tmp_path, source=ScriptedSource({data.index[15]}))
    engine.gw.reject_protection = True
    replay(engine, {"AAAEUR": data})
    assert engine.executor.halted and "Schutz" in engine.journal.halt_reason


def test_live_gateway_is_refused(tmp_path):
    from tradikg_bot.live.journal import ExecutionJournal
    from tradikg_bot.trading.ledger import CapitalLedger
    from tradikg_bot.trading.store import PaperStore

    gw = PaperExchange(native_bracket=True)
    gw.is_live = True
    with pytest.raises(AutonomyNotAuthorizedError):
        PaperTradingEngine(
            gw,
            PaperStore(tmp_path / "x.sqlite"),
            ExecutionJournal(tmp_path / "x.sqlite"),
            CapitalLedger(tmp_path / "x.sqlite", 100.0),
            FixedProbability(),
            ScriptedSource(),
            CFG,
        )


def test_corrupt_exchange_state_is_not_silently_reset(tmp_path):
    engine = make_engine(tmp_path)
    replay(engine, {"AAAEUR": flat(12)})
    engine.store.set("paper_exchange", '{"version": 99}')
    engine.store.close()
    with pytest.raises(RuntimeError, match="beschädigt"):
        make_engine(tmp_path)


def test_daily_report_written_on_day_change(tmp_path):
    data = flat(60, 100.0)
    engine = make_engine(tmp_path, source=ScriptedSource({data.index[15]}))
    up = data.copy()
    up.loc[up.index[20] :, "high"] = 110.0
    replay(engine, {"AAAEUR": up})
    row = engine.store.conn.execute("SELECT day, markdown FROM daily_reports").fetchone()
    assert row and "Tagesbericht" in row[1]


def test_same_bar_stop_and_target_is_flagged_and_conservative(tmp_path):
    data = flat(30)
    wide = data.copy()
    wide.loc[wide.index[17], ["high", "low"]] = [120.0, 80.0]  # SL und TP in einer Kerze
    engine = make_engine(tmp_path, source=ScriptedSource({data.index[15]}))
    replay(engine, {"AAAEUR": wide})
    trade = engine.journal.trades()[0]
    assert trade.exit_reason == "stop_loss"  # konservativ
    analysis = engine.store.analyses()[0]
    assert analysis["intrabar_ambiguous"] is True
    assert any("Reihenfolge unbekannt" in f for f in analysis["findings"])


def test_take_profit_first_is_only_an_explicit_upper_bound(tmp_path):
    data = flat(30)
    wide = data.copy()
    wide.loc[wide.index[17], ["high", "low"]] = [120.0, 80.0]
    engine = make_engine(tmp_path, source=ScriptedSource({data.index[15]}))
    engine.gw.ambiguous_policy = "take_profit_first"
    replay(engine, {"AAAEUR": wide})
    assert engine.journal.trades()[0].exit_reason == "take_profit"
    assert engine.store.analyses()[0]["intrabar_ambiguous"] is True
