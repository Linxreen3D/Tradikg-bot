"""Drawdown-Sperre der Trendfolge: Höchststand, manuelle Lösung, Paper-Replay."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pandas as pd
import pytest

from tradikg_bot.risk.limits import (
    DRAWDOWN_HALT_NAME,
    TREND_DRAWDOWN_FRACTION,
    DrawdownGuard,
    format_drawdown_banner,
    validate_trend_drawdown,
)
from tradikg_bot.trading.allocation import TrendAllocator, TrendConfig, TrendState, run_trend


def at(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso)


def test_drawdown_defaults_and_tighten_only():
    guard = DrawdownGuard()
    assert guard.limit == pytest.approx(0.45)
    assert TREND_DRAWDOWN_FRACTION == 0.45
    assert guard.peak_equity == 0.0 and not guard.halted
    validate_trend_drawdown(0.45)
    validate_trend_drawdown(0.30)
    with pytest.raises(ValueError, match="0.45"):
        validate_trend_drawdown(0.50)
    with pytest.raises(ValueError, match="0.45"):
        validate_trend_drawdown(0.0)
    TrendConfig(drawdown_fraction=0.45)
    with pytest.raises(ValueError, match="0.45"):
        TrendConfig(drawdown_fraction=0.46)
    assert TrendConfig().invest_fraction == 1.0
    assert TrendConfig().drawdown_enabled is False
    assert TrendConfig().stop_frac == pytest.approx(0.20)


def test_trigger_exactly_at_the_floor_and_not_above_it():
    guard = DrawdownGuard(limit=0.45)
    now = at("2019-01-01T00:00:00+00:00")
    assert not guard.observe(140.0, now)
    assert guard.peak_equity == pytest.approx(140.0)
    assert guard.floor == pytest.approx(77.0)
    assert not guard.observe(77.01, now)
    assert not guard.halted
    assert guard.observe(77.0, now)
    assert guard.halted
    assert guard.halted_at == now.isoformat()
    # Schon gesperrt: kein zweites Auslösen, Erholung hebt die Sperre nicht auf.
    assert not guard.observe(200.0, now)
    assert guard.halted and guard.peak_equity == pytest.approx(140.0)


def test_peak_tracks_new_highs_until_the_halt():
    guard = DrawdownGuard(limit=0.45)
    guard.observe(100.0, at("2018-01-01T00:00:00+00:00"))
    guard.observe(80.0, at("2018-06-01T00:00:00+00:00"))
    assert not guard.halted and guard.peak_equity == pytest.approx(100.0)
    guard.observe(160.0, at("2018-12-01T00:00:00+00:00"))
    assert guard.peak_equity == pytest.approx(160.0)
    assert guard.floor == pytest.approx(88.0)
    assert not guard.observe(88.01)
    assert guard.observe(88.0)
    assert guard.halted and guard.peak_equity == pytest.approx(160.0)


def test_restart_keeps_peak_and_halt_until_manual_reset_rebases():
    guard = DrawdownGuard(limit=0.45)
    guard.observe(100.0, at("2019-06-01T00:00:00+00:00"))
    assert guard.observe(50.0, at("2019-12-01T00:00:00+00:00"))
    restored = DrawdownGuard.restore(guard.snapshot(), limit=0.45)
    assert restored.halted
    assert restored.peak_equity == pytest.approx(100.0)
    assert restored.floor == pytest.approx(55.0)
    assert not restored.observe(90.0, at("2020-01-01T00:00:00+00:00"))
    assert restored.halted and restored.peak_equity == pytest.approx(100.0)
    # Enger konfiguriert: die Schwelle kommt aus der Konfiguration, der Höchststand aus der Datei.
    tighter = DrawdownGuard.restore(guard.snapshot(), limit=0.30)
    assert tighter.limit == pytest.approx(0.30) and tighter.peak_equity == pytest.approx(100.0)
    restored.reset()
    assert not restored.halted and restored.rebase
    assert not restored.observe(80.0, at("2020-01-02T00:00:00+00:00"))
    assert restored.peak_equity == pytest.approx(80.0) and not restored.halted
    # Ohne die neue Basis würde die noch niedrige Equity sofort wieder sperren.
    assert not restored.observe(70.0)
    assert not restored.halted


def test_banner_for_snapshot_dashboard():
    assert format_drawdown_banner(None) == ""
    assert format_drawdown_banner({"enabled": False}) == "Drawdown-Sperre: aus"
    quiet = format_drawdown_banner(
        {
            "enabled": True,
            "halted": False,
            "limit": 0.45,
            "peak_equity": 20.0,
            "equity": 19.0,
            "drawdown": -0.05,
        }
    )
    assert "5.0%" in quiet and "20.00" in quiet and "Sperre aus" in quiet
    active = format_drawdown_banner(
        {
            "enabled": True,
            "halted": True,
            "limit": 0.45,
            "peak_equity": 140.0,
            "equity": 77.0,
            "drawdown": 77 / 140 - 1,
        }
    )
    assert active.startswith("DRAWDOWN-SPERRE aktiv")
    assert "45%" in active and "drawdown-reset" in active and "DRAWDOWN_HALT" in active
    assert "Kraken-Stops" in active


def _ohlc(index: pd.DatetimeIndex, closes: list[float], *, lows=None, opens=None):
    low = lows or closes
    open_ = opens or closes
    return pd.DataFrame(
        {
            "open": open_,
            "high": [max(o, c, c * 1.01) for o, c in zip(open_, closes, strict=True)],
            "low": low,
            "close": closes,
            "volume": 1.0,
        },
        index=index,
    )


def _frames(stamp_closes: dict[str, list[float]], lows=None, opens=None):
    n = len(next(iter(stamp_closes.values())))
    index = pd.date_range("2018-01-01", periods=n, freq="D", tz="UTC")
    lows = lows or {}
    opens = opens or {}
    return {
        sym: _ohlc(index, closes, lows=lows.get(sym), opens=opens.get(sym))
        for sym, closes in stamp_closes.items()
    }


def _cfg(**overrides) -> TrendConfig:
    cfg = TrendConfig(
        sma=3,
        stop_frac=0.2,
        maker_fee=0.004,
        taker_fee=0.008,
        spread_bps=0.0,
        stop_slippage_bps=0.0,
        invest_fraction=1.0,
        drawdown_enabled=True,
        drawdown_fraction=0.45,
    )
    return replace(cfg, **overrides)


def _run(data, cfg=None):
    index = data["AAA"].index
    return run_trend(data, index[0], None, 20.0, lambda _s, _p: 0.0, cfg or _cfg())


def _gap_frames():
    """Beide Coins steigen, reißen per Kurslücke den 20-%-Stop, wollen danach wieder kaufen."""
    aaa = [10, 10, 10, 12, 12, 14, 16, 1, 1, 20]
    bbb = [10, 10, 10, 12, 12, 14, 16, 1, 1, 20]
    gap_low = [10, 10, 10, 11, 11, 13, 15, 0.5, 1, 19]
    gap_open = [10, 10, 10, 12, 12, 14, 16, 1, 1, 20]
    return _frames(
        {"AAA": aaa, "BBB": bbb},
        lows={"AAA": gap_low, "BBB": gap_low},
        opens={"AAA": gap_open, "BBB": gap_open},
    )


def _summarize(log, trades, alloc) -> str:
    lines = [
        "PAPER-REPLAY Trendfolge, 20 €, invest_fraction 1.0, Drawdown-Sperre 45 % vom Höchststand"
    ]
    guard = alloc.drawdown
    peak = guard.peak_equity if guard is not None else float("nan")
    halted = "aktiv" if guard is not None and guard.halted else "aus"
    lines.append(f"  Höchststand {peak:.2f} €, Sperre {halted}")
    for item in log:
        ts = pd.Timestamp(item["ts"])
        lines.append(f"  {ts:%Y-%m-%d}  {item['symbol']:9}  {item['status']:8}  {item['reason']}")
    if trades:
        lines.append("Geschlossene Trades:")
        for trade in trades:
            lines.append(
                f"  {trade['asset']} {trade['exit_reason']} netto {trade['net_pnl_eur']:+.2f} €"
            )
    else:
        lines.append("Geschlossene Trades: keine")
    return "\n".join(lines)


def test_paper_replay_blocks_new_buys_after_45_percent_and_stops_still_exit():
    equity, trades, log, alloc = _run(_gap_frames())
    summary = _summarize(log, trades, alloc)
    print(summary)
    assert summary.startswith("PAPER-REPLAY")
    assert alloc.drawdown is not None and alloc.drawdown.halted
    assert alloc.drawdown.peak_equity > 20  # Höchststand zieht mit dem Anstieg mit
    assert alloc.cfg.invest_fraction == 1.0
    halts = [item for item in log if item["status"] == "halt"]
    assert len(halts) == 1 and "Drawdown-Sperre" in halts[0]["reason"]
    rejected = [item for item in log if item["status"] == "rejected"]
    assert rejected and all("Drawdown-Sperre" in item["reason"] for item in rejected)
    assert not any(item["status"] == "entry" and pd.Timestamp(item["ts"]).day >= 10 for item in log)
    # Die Kurslücke verkauft über den bestehenden Notfall-Stop, nicht über die Sperre.
    assert trades and {t["exit_reason"] for t in trades} == {"notfall_stop"}
    assert equity.iloc[-1] < alloc.drawdown.peak_equity * (1 - 0.45)


def _held_then(aaa_last: float, aaa_low: float, *, sma: int = 20):
    """Langer Schnitt, damit ein Rücksetzer über dem Schnitt und über dem 20-%-Stop bleiben kann."""
    warm = [10.0] * sma
    # Letzte Kerze führt einen am Vortag geplanten BBB-Kauf aus, ohne den Kurs weiter zu bewegen.
    aaa = warm + [12.0, 12.0, aaa_last, aaa_last]
    bbb = warm + [10.0, 10.0, 14.0, 14.0]
    return _frames(
        {"AAA": aaa, "BBB": bbb},
        lows={"AAA": warm + [11.0, 11.0, aaa_low, aaa_low], "BBB": bbb},
    ), sma


def test_paper_mild_dip_does_not_trigger_and_still_buys():
    # Ein Coin fällt knapp 8 % und bleibt über SMA und über dem Stop. Portfolio weit über −45 %.
    data, sma = _held_then(11.0, 10.7)
    equity, trades, log, alloc = _run(data, _cfg(sma=sma))
    assert alloc.drawdown is not None and not alloc.drawdown.halted
    assert not trades
    assert any(item["symbol"] == "BBB" and item["status"] == "entry" for item in log)
    assert "BBB" in alloc.state.positions
    assert equity.iloc[-1] > alloc.drawdown.peak_equity * (1 - 0.45)


def test_paper_halt_does_not_sell_when_the_stop_is_not_hit():
    """Engere 5-%-Schwelle sperrt bei −12 % Kurs. Der 20-%-Stop und der Schnitt tun das nicht."""
    data, sma = _held_then(10.56, 10.4)
    _, trades, log, alloc = _run(data, _cfg(sma=sma, drawdown_fraction=0.05))
    assert not trades
    assert "AAA" in alloc.state.positions
    assert "AAA" not in alloc.state.pending  # kein Signal-Verkauf, kein Zwangsverkauf
    assert alloc.state.positions["AAA"].stop == pytest.approx(12 * 0.8, rel=0.02)
    assert alloc.drawdown is not None and alloc.drawdown.halted
    assert any(item["symbol"] == "BBB" and item["status"] == "rejected" for item in log)
    assert not any(item["status"] == "entry" and item["symbol"] == "BBB" for item in log)


def test_paper_flag_off_still_buys_after_the_same_crash():
    data = _gap_frames()
    _, _, blocked, halted = _run(data)
    _, _, open_log, alloc = _run(data, _cfg(drawdown_enabled=False))
    assert alloc.drawdown is None and alloc.state.drawdown is None
    assert any(item["status"] == "halt" for item in blocked)
    assert not any("Drawdown-Sperre" in item["reason"] for item in open_log)
    entries = [item for item in open_log if item["status"] == "entry"]
    assert any(pd.Timestamp(item["ts"]) == data["AAA"].index[-1] for item in entries)
    assert halted.drawdown is not None and halted.drawdown.halted
    print("FLAG AUS: Einstieg am letzten Tag trotz des Einbruchs")


def test_paper_persists_across_restart_and_manual_reset_allows_buys(tmp_path):
    data = _gap_frames()
    index = data["AAA"].index
    cfg = _cfg()
    alloc = TrendAllocator(["AAA", "BBB"], 20.0, lambda _s, _p: 0.0, cfg)
    alloc.drawdown_file = tmp_path / DRAWDOWN_HALT_NAME
    closes_full = {sym: df["close"] for sym, df in data.items()}

    def step_at(engine, ts):
        bars = {sym: df.loc[ts] for sym, df in data.items()}
        closes = {sym: series.loc[:ts] for sym, series in closes_full.items()}
        return engine.step(ts, bars, closes)

    for ts in index[:8]:  # bis zur Kurslücke, Sperre ist gesetzt
        step_at(alloc, ts)
    assert alloc.drawdown is not None and alloc.drawdown.halted
    assert alloc.drawdown_file.exists()
    raw = alloc.state.to_json()
    assert raw["drawdown"]["halted"] is True
    assert raw["drawdown"]["peak_equity"] == pytest.approx(alloc.drawdown.peak_equity)

    resumed = TrendAllocator(
        ["AAA", "BBB"], 20.0, lambda _s, _p: 0.0, cfg, TrendState.from_json(raw)
    )
    resumed.drawdown_file = alloc.drawdown_file
    _, log = step_at(resumed, index[8])  # noch unter SMA, Sperre bleibt
    assert resumed.drawdown is not None and resumed.drawdown.halted
    _, log = step_at(resumed, index[9])  # über SMA, Kauf wäre fällig
    assert any("Drawdown-Sperre" in item["reason"] for item in log)
    assert "AAA" not in resumed.state.pending and "BBB" not in resumed.state.pending

    resumed.reset_drawdown()
    assert not resumed.drawdown_file.exists()
    assert resumed.state.drawdown is not None and resumed.state.drawdown["rebase"] is True
    # Nächster Schritt setzt den Höchststand neu. Dafür eine weitere Kerze über SMA.
    extra = index[-1] + pd.Timedelta(days=1)
    extra_bars = {}
    extra_closes = {}
    for sym, df in data.items():
        row = df.iloc[-1].copy()
        extra_bars[sym] = row
        extra_closes[sym] = pd.concat([df["close"], pd.Series([float(row.close)], index=[extra])])
    resumed.step(extra, extra_bars, extra_closes)
    assert resumed.drawdown is not None and not resumed.drawdown.halted
    assert resumed.drawdown.peak_equity == pytest.approx(
        resumed.equity({s: float(c.iloc[-1]) for s, c in extra_closes.items()})
    )
    assert resumed.state.pending  # Käufe wieder erlaubt


def test_deleting_the_flag_file_does_not_reset(tmp_path):
    data = _gap_frames()
    index = data["AAA"].index
    cfg = _cfg()
    alloc = TrendAllocator(["AAA", "BBB"], 20.0, lambda _s, _p: 0.0, cfg)
    alloc.drawdown_file = tmp_path / DRAWDOWN_HALT_NAME
    closes_full = {sym: df["close"] for sym, df in data.items()}
    for ts in index[:8]:
        bars = {sym: df.loc[ts] for sym, df in data.items()}
        closes = {sym: series.loc[:ts] for sym, series in closes_full.items()}
        alloc.step(ts, bars, closes)
    assert alloc.drawdown_file.exists() and alloc.drawdown is not None and alloc.drawdown.halted
    alloc.drawdown_file.unlink()
    ts = index[9]
    bars = {sym: df.loc[ts] for sym, df in data.items()}
    closes = {sym: series.loc[:ts] for sym, series in closes_full.items()}
    _, log = alloc.step(ts, bars, closes)
    assert alloc.drawdown.halted
    assert any(item["status"] == "rejected" and "Drawdown" in item["reason"] for item in log)
    assert not any(item["status"] == "entry" for item in log)
    alloc.reset_drawdown()
    assert alloc.state.drawdown is not None and alloc.state.drawdown["rebase"] is True
