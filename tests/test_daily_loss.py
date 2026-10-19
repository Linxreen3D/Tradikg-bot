"""Tagesverlustgrenze der Trendfolge: Guard, Wiener Mitternacht, Paper-Replay."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pandas as pd
import pytest

from tradikg_bot.risk.limits import (
    TREND_DAILY_LOSS_FRACTION,
    TREND_DAILY_LOSS_TIMEZONE,
    DailyLossGuard,
    validate_trend_daily_loss,
)
from tradikg_bot.trading.allocation import TrendAllocator, TrendConfig, TrendState, run_trend


def at(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso)


def test_guard_defaults_stay_on_the_backtest_contract():
    """Ohne Argumente bleibt der Backtest-Guard bei 10 % und UTC."""
    guard = DailyLossGuard()
    assert guard.limit == pytest.approx(0.10)
    assert guard.timezone == "UTC"
    assert TREND_DAILY_LOSS_FRACTION == 0.05
    assert TREND_DAILY_LOSS_TIMEZONE == "Europe/Vienna"


def test_limit_not_hit_then_hit_exactly_at_the_floor():
    guard = DailyLossGuard(limit=0.05, timezone="Europe/Vienna")
    noon = at("2026-01-15T12:00:00+00:00")
    assert not guard.observe(noon, 100.0)
    assert guard.floor == pytest.approx(95.0)
    later = at("2026-01-15T18:00:00+00:00")
    assert not guard.observe(later, 95.01)
    assert not guard.halted
    assert guard.observe(later, 95.0)  # genau 5 % ist gerissen
    assert guard.halted
    assert guard.remaining_budget(99.0) == 0.0
    assert not guard.observe(later, 90.0)  # nur einmal melden
    assert guard.halted_days == [dt.date(2026, 1, 15)]


def test_vienna_midnight_resets_including_dst_transitions():
    """00:00 Europe/Vienna, nicht 00:00 UTC. Sommer, Winter und beide Umstellungen."""
    guard = DailyLossGuard(limit=0.05, timezone="Europe/Vienna")
    # Winter (CET, UTC+1): Mitternacht Wien = 23:00 UTC am Vortag.
    assert not guard.observe(at("2026-01-15T12:00:00+00:00"), 100.0)
    assert guard.observe(at("2026-01-15T18:00:00+00:00"), 90.0)
    assert guard.halted and guard.current_day == dt.date(2026, 1, 15)
    assert not guard.observe(at("2026-01-15T22:59:00+00:00"), 90.0)
    assert guard.halted  # 23:59 Wien, noch der alte Tag
    assert not guard.observe(at("2026-01-15T23:00:00+00:00"), 90.0)
    assert not guard.halted and guard.current_day == dt.date(2026, 1, 16)
    assert guard.day_start_equity == pytest.approx(90.0)

    # Sommer (CEST, UTC+2): Mitternacht Wien = 22:00 UTC.
    guard = DailyLossGuard(limit=0.05, timezone="Europe/Vienna")
    guard.observe(at("2026-07-15T12:00:00+00:00"), 100.0)
    guard.observe(at("2026-07-15T18:00:00+00:00"), 90.0)
    assert guard.halted and guard.trading_day(at("2026-07-15T21:59:00+00:00")) == dt.date(
        2026, 7, 15
    )
    assert guard.trading_day(at("2026-07-15T22:00:00+00:00")) == dt.date(2026, 7, 16)
    assert not guard.observe(at("2026-07-15T22:00:00+00:00"), 90.0)
    assert not guard.halted and guard.current_day == dt.date(2026, 7, 16)

    # Frühjahr 2026-03-29: 02:00 CET fällt aus, 03:00 CEST. Mitternacht bleibt eindeutig.
    guard = DailyLossGuard(limit=0.05, timezone="Europe/Vienna")
    guard.observe(at("2026-03-28T12:00:00+00:00"), 100.0)
    assert guard.observe(at("2026-03-28T18:00:00+00:00"), 90.0)
    assert guard.trading_day(at("2026-03-28T22:59:00+00:00")) == dt.date(2026, 3, 28)
    assert guard.trading_day(at("2026-03-28T23:00:00+00:00")) == dt.date(2026, 3, 29)
    assert not guard.observe(at("2026-03-28T23:00:00+00:00"), 90.0)
    assert not guard.halted and guard.current_day == dt.date(2026, 3, 29)
    # In die Lücke hinein (01:30 UTC = 03:30 CEST): immer noch der 29.
    assert guard.trading_day(at("2026-03-29T01:30:00+00:00")) == dt.date(2026, 3, 29)
    guard.observe(at("2026-03-29T10:00:00+00:00"), 80.0)  # neuer Tagesverlust, Sperre
    assert guard.halted and guard.current_day == dt.date(2026, 3, 29)
    # Nächste Mitternacht ist 22:00 UTC, weil Wien jetzt UTC+2 ist.
    assert guard.trading_day(at("2026-03-29T21:59:00+00:00")) == dt.date(2026, 3, 29)
    assert guard.halted
    assert guard.trading_day(at("2026-03-29T22:00:00+00:00")) == dt.date(2026, 3, 30)
    assert not guard.observe(at("2026-03-29T22:00:00+00:00"), 80.0)
    assert not guard.halted and guard.current_day == dt.date(2026, 3, 30)

    # Herbst 2026-10-25: 03:00 CEST wird zu 02:00 CET. Die doppelte Stunde bleibt derselbe Tag.
    guard = DailyLossGuard(limit=0.05, timezone="Europe/Vienna")
    guard.observe(at("2026-10-24T16:00:00+00:00"), 100.0)  # 18:00 CEST, 24.10.
    assert guard.observe(at("2026-10-24T20:00:00+00:00"), 90.0)
    assert guard.current_day == dt.date(2026, 10, 24)
    # 25.10. 00:00 CEST = 24.10. 22:00 UTC
    assert guard.trading_day(at("2026-10-24T21:59:00+00:00")) == dt.date(2026, 10, 24)
    assert guard.halted
    assert not guard.observe(at("2026-10-24T22:00:00+00:00"), 90.0)
    assert not guard.halted and guard.current_day == dt.date(2026, 10, 25)
    guard.observe(at("2026-10-25T00:30:00+00:00"), 80.0)  # 02:30 CEST, erste Stunde
    assert guard.halted and guard.current_day == dt.date(2026, 10, 25)
    # 02:30 CET, die wiederholte Stunde: kein neuer Tag, Sperre bleibt.
    assert guard.trading_day(at("2026-10-25T01:30:00+00:00")) == dt.date(2026, 10, 25)
    assert not guard.observe(at("2026-10-25T01:30:00+00:00"), 80.0)
    assert guard.halted and guard.current_day == dt.date(2026, 10, 25)
    # 26.10. 00:00 CET = 25.10. 23:00 UTC
    assert guard.trading_day(at("2026-10-25T22:59:00+00:00")) == dt.date(2026, 10, 25)
    assert guard.trading_day(at("2026-10-25T23:00:00+00:00")) == dt.date(2026, 10, 26)
    assert not guard.observe(at("2026-10-25T23:00:00+00:00"), 80.0)
    assert not guard.halted and guard.current_day == dt.date(2026, 10, 26)


def test_restart_keeps_halt_until_the_saved_vienna_day_ends():
    guard = DailyLossGuard(limit=0.05, timezone="Europe/Vienna")
    guard.observe(at("2026-06-01T10:00:00+00:00"), 20.0)
    assert guard.observe(at("2026-06-01T15:00:00+00:00"), 18.0)
    restored = DailyLossGuard.restore(guard.snapshot(), limit=0.05, timezone="Europe/Vienna")
    assert restored.halted
    assert restored.day_start_equity == pytest.approx(20.0)
    assert restored.current_day == dt.date(2026, 6, 1)
    # Erholung am selben Tag hebt die Sperre nicht auf.
    assert not restored.observe(at("2026-06-01T20:00:00+00:00"), 19.5)
    assert restored.halted
    # Konfiguration gewinnt, falls die Schwelle enger gestellt wurde.
    tighter = DailyLossGuard.restore(guard.snapshot(), limit=0.03, timezone="Europe/Vienna")
    assert tighter.limit == pytest.approx(0.03)
    assert tighter.day_start_equity == pytest.approx(20.0)
    # Nächster Wiener Tag (CEST: 22:00 UTC) gibt Käufe wieder frei.
    assert not restored.observe(at("2026-06-01T22:00:00+00:00"), 18.0)
    assert not restored.halted
    assert restored.current_day == dt.date(2026, 6, 2)
    assert restored.day_start_equity == pytest.approx(18.0)


def test_timezone_is_configurable_and_reject_loose_or_unknown():
    vienna = DailyLossGuard(limit=0.05, timezone="Europe/Vienna")
    utc = DailyLossGuard(limit=0.05, timezone="UTC")
    instant = at("2026-01-15T23:30:00+00:00")
    vienna.observe(instant, 100.0)
    utc.observe(instant, 100.0)
    assert vienna.current_day == dt.date(2026, 1, 16)  # 00:30 Wien
    assert utc.current_day == dt.date(2026, 1, 15)
    validate_trend_daily_loss(0.10, "UTC")
    validate_trend_daily_loss(0.01, "Europe/Vienna")
    with pytest.raises(ValueError, match="0.10"):
        validate_trend_daily_loss(0.11, "Europe/Vienna")
    with pytest.raises(ValueError, match="Zeitzone"):
        validate_trend_daily_loss(0.05, "Europa/Wien")


def _ohlc(index: pd.DatetimeIndex, closes: list[float], *, lows: list[float] | None = None):
    low = lows or [min(c, c) for c in closes]
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [max(c, c * 1.01) for c in closes],
            "low": low,
            "close": closes,
            "volume": 1.0,
        },
        index=index,
    )
    return df


def _replay_frames():
    """Zwei Coins, Stundenkerzen. AAA wird gekauft und verliert, BBB will danach kaufen."""
    # Januar: Wien = UTC+1. 23:30 UTC ist bereits der nächste Wiener Tag.
    stamps = [
        "2026-01-15 10:00",
        "2026-01-15 11:00",
        "2026-01-15 12:00",
        "2026-01-15 13:00",  # AAA über SMA → Kauf geplant
        "2026-01-15 14:00",  # Kauf ausgeführt
        "2026-01-15 15:00",  # AAA-Stop, BBB will kaufen
        "2026-01-15 16:00",  # noch derselbe Wiener Tag
        "2026-01-15 23:30",  # 00:30 Wien am 16.01. → neue Käufe wieder frei
        "2026-01-16 01:30",  # geplanter BBB-Kauf wird ausgeführt
    ]
    index = pd.DatetimeIndex([pd.Timestamp(s, tz="UTC") for s in stamps])
    aaa_close = [10, 10, 10, 12, 12, 9, 9, 9, 9]
    aaa_low = [10, 10, 10, 10, 11.5, 8, 9, 9, 9]
    bbb_close = [10, 10, 10, 10, 10, 14, 14, 15, 15.2]
    data = {
        "AAA": _ohlc(index, aaa_close, lows=aaa_low),
        "BBB": _ohlc(index, bbb_close),
    }
    return data


def _trend_cfg(**overrides) -> TrendConfig:
    cfg = TrendConfig(
        sma=3,
        stop_frac=0.2,
        maker_fee=0.004,
        taker_fee=0.008,
        spread_bps=0.0,
        stop_slippage_bps=0.0,
        daily_loss_enabled=True,
        daily_loss_fraction=0.05,
        daily_loss_timezone="Europe/Vienna",
    )
    return replace(cfg, **overrides)


def _summarize(log, trades) -> str:
    lines = ["PAPER-REPLAY Trendfolge, 20 €, Tagesverlustgrenze 5 %, Reset 00:00 Europe/Vienna"]
    for item in log:
        ts = pd.Timestamp(item["ts"])
        vienna = ts.tz_convert("Europe/Vienna")
        lines.append(
            f"  {vienna:%Y-%m-%d %H:%M} Wien  {item['symbol']:3}  "
            f"{item['status']:8}  {item['reason']}"
        )
    if trades:
        lines.append("Geschlossene Trades:")
        for trade in trades:
            lines.append(
                f"  {trade['asset']} {trade['exit_reason']} netto {trade['net_pnl_eur']:+.2f} €"
            )
    else:
        lines.append("Geschlossene Trades: keine")
    return "\n".join(lines)


def test_paper_replay_blocks_buys_after_the_limit_and_allows_them_next_vienna_day():
    data = _replay_frames()
    index = data["AAA"].index
    equity, trades, log, alloc = run_trend(
        data, index[0], None, 20.0, lambda _s, _p: 0.0, _trend_cfg()
    )
    summary = _summarize(log, trades)
    print(summary)
    assert summary.startswith("PAPER-REPLAY")

    bbb = [item for item in log if item["symbol"] == "BBB"]
    assert bbb, summary
    blocked = [item for item in bbb if item["status"] == "rejected"]
    assert len(blocked) >= 2  # 15:00 und 16:00 UTC, noch derselbe Wiener Tag
    assert all("Tagesverlustgrenze" in item["reason"] for item in blocked)
    assert blocked[0]["reason"].startswith("Tagesverlustgrenze 5%")
    entries = [item for item in bbb if item["status"] == "entry"]
    assert len(entries) == 1
    entry_local = pd.Timestamp(entries[0]["ts"]).tz_convert("Europe/Vienna")
    assert entry_local.date() == dt.date(2026, 1, 16)
    assert "BBB" in alloc.state.positions  # Kauf nach Mitternacht wird ausgeführt
    assert "AAA" not in alloc.state.positions
    assert alloc.guard is not None and not alloc.guard.halted
    assert alloc.guard.current_day == dt.date(2026, 1, 16)
    # AAA wird vom Notfall-Stop verkauft, nicht von der Tagesgrenze.
    assert trades and trades[0]["asset"] == "AAA" and trades[0]["exit_reason"] == "notfall_stop"
    assert "BBB" not in {t["asset"] for t in trades}
    # Stop hat realisiert. Die Sperre kommt vom Verlust, nicht von einem Zwangsverkauf.
    assert equity.iloc[-1] > 0
    loss = trades[0]["net_pnl_eur"]
    assert loss < -0.05 * 20  # mehr als 5 % des Startkapitals


def test_paper_replay_flag_off_still_buys_on_the_loss_day():
    data = _replay_frames()
    index = data["AAA"].index
    _, _, blocked_log, _ = run_trend(data, index[0], None, 20.0, lambda _s, _p: 0.0, _trend_cfg())
    _, _, open_log, alloc = run_trend(
        data, index[0], None, 20.0, lambda _s, _p: 0.0, _trend_cfg(daily_loss_enabled=False)
    )
    assert alloc.guard is None and alloc.state.daily_loss is None
    bbb_entries = [
        item for item in open_log if item["symbol"] == "BBB" and item["status"] == "entry"
    ]
    assert len(bbb_entries) == 1
    assert pd.Timestamp(bbb_entries[0]["ts"]) == index[5]  # 15:00 UTC, Verlusttag
    assert not any("Tagesverlustgrenze" in item["reason"] for item in open_log)
    assert any("Tagesverlustgrenze" in item["reason"] for item in blocked_log)
    print("FLAG AUS: BBB-Kauf am Verlusttag", pd.Timestamp(bbb_entries[0]["ts"]))


def test_paper_replay_persists_halt_across_restart():
    data = _replay_frames()
    index = data["AAA"].index
    cfg = _trend_cfg()
    alloc = TrendAllocator(["AAA", "BBB"], 20.0, lambda _s, _p: 0.0, cfg)
    closes_full = {sym: df["close"] for sym, df in data.items()}
    for ts in index[:6]:  # bis einschließlich 15:00 UTC, Sperre ist gesetzt
        bars = {sym: df.loc[ts] for sym, df in data.items()}
        closes = {sym: series.loc[:ts] for sym, series in closes_full.items()}
        alloc.step(ts, bars, closes)
    assert alloc.guard is not None and alloc.guard.halted
    raw = alloc.state.to_json()
    assert raw["daily_loss"]["halted"] is True
    assert raw["daily_loss"]["day_start_equity"] == pytest.approx(20.0, abs=0.05)

    resumed = TrendAllocator(
        ["AAA", "BBB"], 20.0, lambda _s, _p: 0.0, cfg, TrendState.from_json(raw)
    )
    assert resumed.guard is not None and resumed.guard.halted
    ts = index[6]  # 16:00 UTC, noch 15.01. Wien
    bars = {sym: df.loc[ts] for sym, df in data.items()}
    closes = {sym: series.loc[:ts] for sym, series in closes_full.items()}
    _, log = resumed.step(ts, bars, closes)
    assert any(item["symbol"] == "BBB" and "Tagesverlustgrenze" in item["reason"] for item in log)
    assert "BBB" not in resumed.state.pending


def test_paper_service_keeps_daily_loss_off_unless_configured(tmp_path):
    from tradikg_bot.platform_config import PlatformConfig

    assert PlatformConfig().daily_loss_enabled is False
    with pytest.raises(ValueError, match="0.10"):
        PlatformConfig(daily_loss_fraction=0.2)
    with pytest.raises(ValueError, match="Zeitzone"):
        PlatformConfig(daily_loss_timezone="Not/AZone")
