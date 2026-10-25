"""Linux-Cutover: Scheibe, Probelauf, Übergabe. Keine echten Orders, keine Secrets."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import pytest
from test_trend_live import RULES, FakeKraken, Market, runner

from tradikg_bot.cli import build_parser
from tradikg_bot.live.cli_live import _trend_config
from tradikg_bot.live.handover import (
    ACTIVITY_WINDOW_S,
    execute_dry_run,
    install_state,
    local_pid_reasons,
    marker_block_reason,
    reconstruct_state,
    validate_state_document,
)
from tradikg_bot.live.models import ExchangeOrder, OrderSide, OrderStatus
from tradikg_bot.live.trend_live import (
    KIND,
    MAX_BUDGET_EUR,
    LiveState,
    LiveTrendConfig,
    LiveTrendRunner,
    SymbolState,
)
from tradikg_bot.live.trend_live import (
    LiveSafetyError as Safety,
)
from tradikg_bot.service.ops import AlreadyRunningError, InstanceLock

ROOT = Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)
DAY = "2026-10-06"


def _cfg(**overrides) -> LiveTrendConfig:
    params = dict(
        budget_eur=20.0,
        invest_fraction=1.0,
        sma=3,
        daily_loss_enabled=True,
        daily_loss_fraction=0.05,
    )
    params.update(overrides)
    return LiveTrendConfig(**params)


def _ref(sym_index: int, kind: str, day: str = DAY) -> int:
    return int(pd.Timestamp(day).strftime("%y%m%d")) * 100 + (sym_index + 1) * 10 + KIND[kind]


def _paths(root: Path) -> dict[str, Path]:
    live = root / "live"
    live.mkdir(parents=True, exist_ok=True)
    return {
        "state": live / "trend_state.json",
        "kill": live / "KILL_SWITCH",
        "lock": live / "live.lock",
        "pid": live / "live.pid",
        "dry_run_ok": live / "dry_run_ok.json",
    }


def _stop_order(volume: float, opened_at: float | None) -> ExchangeOrder:
    rules = RULES["XBTEUR"]
    price = rules.round_price_down(110.0 * 0.8)
    return ExchangeOrder(
        "TSTOP",
        _ref(0, "stop"),
        "XBTEUR",
        OrderSide.SELL,
        "stop-loss",
        OrderStatus.OPEN,
        rules.round_volume_down(volume),
        price=price,
        opened_at=opened_at,
    )


def _run_dry(root: Path, fake: FakeKraken, market: Market, **kwargs) -> str:
    return execute_dry_run(
        gateway=fake,
        paths=_paths(root),
        cfg=_cfg(),
        closes=market,
        log=lambda _msg: None,
        now=lambda: NOW,
        base_of=lambda sym: RULES[sym].base,
        **kwargs,
    )


def test_hundred_eur_account_is_sized_from_the_twenty_eur_slice_only(tmp_path):
    assert MAX_BUDGET_EUR == 20.0
    with pytest.raises(Safety, match="20"):
        LiveTrendConfig(budget_eur=20.01)
    LiveTrendConfig(budget_eur=20.0)

    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 14)  # beide über SMA, Konto bleibt bei 100 €
    run = runner(tmp_path, fake, market, cfg=_cfg(daily_loss_enabled=False))
    run.run_once()
    buys = [order for order in fake.orders.values() if order.side == OrderSide.BUY]
    assert len(buys) == 2
    notionals = [order.volume * (order.price or 0.0) for order in buys]
    assert max(notionals) <= 10.0 + 1e-6
    assert 15.0 < sum(notionals) <= 20.0 + 1e-6
    assert sum(notionals) < 50  # nicht das 100-€-Konto und nicht die alte 50-€-Kappe


def test_import_accepts_a_slice_and_rejects_the_whole_account_or_secrets(tmp_path):
    cfg = _cfg()
    mac = tmp_path / "from-mac.json"
    LiveState(
        {
            "XBTEUR": SymbolState(
                units=0.01,
                entry_price=100.0,
                peak=110.0,
                stop_txid="T1",
                stop_price=80.0,
                stop_units=0.01,
            )
        }
    ).save(mac)
    installed = install_state(mac, tmp_path / "live" / "trend_state.json", cfg)
    assert installed.symbols["XBTEUR"].stop_txid == "T1"
    assert installed.symbols["XBTEUR"].units * installed.symbols["XBTEUR"].entry_price <= 20

    fat = {
        "symbols": {
            "XBTEUR": {"units": 1.0, "entry_price": 40.0},
            "ETHEUR": {"units": 1.0, "entry_price": 40.0},
        }
    }
    with pytest.raises(Safety, match="Budget"):
        validate_state_document(fat, cfg)

    leaked = {"symbols": {}, "api_secret": "SUPERSECRETVALUE"}
    with pytest.raises(Safety, match="Geheimnis") as raised:
        validate_state_document(leaked, cfg)
    assert "SUPERSECRETVALUE" not in str(raised.value)


def test_reconstruct_adopts_stop_inside_the_slice_and_ignores_extra_coins(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 8)  # ETH unter SMA, damit der Probelauf dort nichts kaufen will
    volume = 0.09  # 0.09 * 110 € = 9,90 € Einstand, innerhalb der Scheibe
    order = _stop_order(volume, opened_at=NOW.timestamp() - 2 * 86400)
    fake.orders[order.txid] = order
    fake.refs[order.txid] = order.client_ref
    fake.bal["XXBT"] = order.volume + 0.5  # manueller Bestand außerhalb der Scheibe
    fake.bal["ZEUR"] = 100.0
    before = set(fake.orders)

    text = _run_dry(tmp_path, fake, market, reconstruct=True)
    assert "OK" in text and "keine Order" in text
    assert set(fake.orders) == before and not fake.validated

    state = LiveState.load(_paths(tmp_path)["state"])
    st = state.symbols["XBTEUR"]
    assert st.units == pytest.approx(order.volume)
    assert st.units < fake.bal["XXBT"]
    assert st.stop_txid == "TSTOP"
    assert st.units * st.entry_price <= 20.0

    live = LiveTrendRunner(
        fake,
        _paths(tmp_path)["state"],
        _cfg(),
        market,
        _paths(tmp_path)["kill"],
        dry_run=False,
        log=lambda _msg: None,
        now=lambda: NOW,
    )
    live.run_once()
    stops = [o for o in fake.orders.values() if o.ordertype == "stop-loss"]
    assert [o.txid for o in stops] == ["TSTOP"]
    assert fake.orders["TSTOP"].status == OrderStatus.OPEN
    assert not [o for o in fake.orders.values() if o.side == OrderSide.BUY]


def test_open_buy_is_adopted_and_not_sent_again(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 14)
    buy = ExchangeOrder(
        "TBUY",
        _ref(0, "buy"),
        "XBTEUR",
        OrderSide.BUY,
        "limit",
        OrderStatus.OPEN,
        0.05,
        price=100.0,
        opened_at=NOW.timestamp() - 3 * 3600,
    )
    fake.orders[buy.txid] = buy
    fake.refs[buy.txid] = buy.client_ref
    _run_dry(tmp_path, fake, market, reconstruct=True)
    assert [order.txid for order in fake.orders.values() if order.side == OrderSide.BUY] == ["TBUY"]
    state = LiveState.load(_paths(tmp_path)["state"])
    assert state.symbols["XBTEUR"].buy_txid == "TBUY"
    assert state.symbols["XBTEUR"].units == 0


def test_dry_run_refuses_a_live_pid_a_fresh_order_and_withdrawal(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 8)
    proc = subprocess.Popen(["sleep", "30"])  # noqa: S603 - lokale PID, kein Shell
    try:
        _paths(tmp_path)["pid"].write_text(str(proc.pid))
        with pytest.raises(Safety, match="PID"):
            _run_dry(tmp_path, fake, market, reconstruct=True)
        assert not fake.validated
    finally:
        proc.kill()
        proc.wait(timeout=5)

    assert local_pid_reasons(_paths(tmp_path)["pid"]) == []
    _paths(tmp_path)["pid"].write_text(str(os.getpid()))
    assert local_pid_reasons(_paths(tmp_path)["pid"]) == []
    _paths(tmp_path)["pid"].unlink()

    fresh = _stop_order(0.09, opened_at=NOW.timestamp() - 30)
    fake.orders[fresh.txid] = fresh
    fake.bal["XXBT"] = fresh.volume
    with pytest.raises(Safety, match="opentm"):
        _run_dry(tmp_path, fake, market, reconstruct=True)
    assert not fake.validated
    assert ACTIVITY_WINDOW_S == 15 * 60

    fake.orders.clear()
    fake.withdraw = True
    with pytest.raises(Safety, match="Auszahlungsrechte"):
        _run_dry(tmp_path, fake, market, reconstruct=True)


def test_missing_opentm_does_not_look_like_a_running_bot(tmp_path):
    """Grenze: ohne Zeitstempel ist ein liegender Stop kein Laufsignal."""
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 8)
    order = _stop_order(0.09, opened_at=None)
    fake.orders[order.txid] = order
    fake.refs[order.txid] = order.client_ref
    fake.bal["XXBT"] = order.volume
    text = _run_dry(tmp_path, fake, market, reconstruct=True)
    assert "OK" in text and not fake.validated


def test_held_lock_refuses_start(tmp_path):
    path = tmp_path / "live.lock"
    # Die Sperre muss am Objekt hängen bleiben – sonst gibt GC den flock frei.
    code = (
        "import sys, time\n"
        "from tradikg_bot.service.ops import InstanceLock\n"
        "lock = InstanceLock(sys.argv[1])\n"
        "lock.acquire()\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(path)])  # noqa: S603
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                InstanceLock(path).acquire().release()
            except AlreadyRunningError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("Sperre wurde nicht gehalten")
        fake, market = FakeKraken(), Market()
        with pytest.raises(Safety, match="Sperrdatei"):
            execute_dry_run(
                gateway=fake,
                paths={
                    "state": tmp_path / "trend_state.json",
                    "kill": tmp_path / "KILL",
                    "lock": path,
                    "pid": tmp_path / "live.pid",
                    "dry_run_ok": tmp_path / "dry_run_ok.json",
                },
                cfg=_cfg(),
                closes=market,
                log=lambda _msg: None,
                now=lambda: NOW,
                base_of=lambda sym: RULES[sym].base,
                reconstruct=True,
            )
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_marker_tracks_config_and_commit_not_the_state_file(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 8)
    order = _stop_order(0.09, opened_at=NOW.timestamp() - 86400)
    fake.orders[order.txid] = order
    fake.refs[order.txid] = order.client_ref
    fake.bal["XXBT"] = order.volume
    _run_dry(tmp_path, fake, market, reconstruct=True)
    paths = _paths(tmp_path)
    stamped = json.loads(paths["dry_run_ok"].read_text())
    assert stamped["code_commit"] and "state_sha256" not in stamped
    assert (
        marker_block_reason(
            paths["dry_run_ok"],
            budget_eur=20.0,
            daily_loss_enabled=True,
            daily_loss_fraction=0.05,
        )
        is None
    )
    assert marker_block_reason(
        paths["dry_run_ok"],
        budget_eur=20.0,
        daily_loss_enabled=False,
        daily_loss_fraction=0.05,
    )
    assert "Drawdown" in (
        marker_block_reason(
            paths["dry_run_ok"],
            budget_eur=20.0,
            daily_loss_enabled=True,
            daily_loss_fraction=0.05,
            drawdown_enabled=True,
            drawdown_fraction=0.45,
        )
        or ""
    )
    assert "Code-Stand" in (
        marker_block_reason(
            paths["dry_run_ok"],
            budget_eur=20.0,
            daily_loss_enabled=True,
            daily_loss_fraction=0.05,
            code_commit="nicht-der-probelauf",
        )
        or ""
    )
    # Der Bot schreibt die Datei im Betrieb. Das allein macht den Stempel nicht ungültig.
    paths["state"].write_text(paths["state"].read_text() + "\n")
    assert (
        marker_block_reason(
            paths["dry_run_ok"],
            budget_eur=20.0,
            daily_loss_enabled=True,
            daily_loss_fraction=0.05,
        )
        is None
    )


def test_mismatch_does_not_stamp_and_places_nothing(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 8)
    paths = _paths(tmp_path)
    LiveState().save(paths["state"])  # leer, aber die Börse hat einen Stop
    order = _stop_order(0.09, opened_at=NOW.timestamp() - 86400)
    fake.orders[order.txid] = order
    fake.bal["XXBT"] = order.volume
    with pytest.raises(Safety, match="Abgleich"):
        _run_dry(tmp_path, fake, market)
    assert not paths["dry_run_ok"].exists()
    assert not fake.validated


def test_reconstruct_refuses_a_position_bigger_than_the_slice():
    order = _stop_order(2.0, opened_at=None)  # 2 * 110 € liegt weit über 20 €
    with pytest.raises(Safety, match="Scheibe"):
        reconstruct_state(
            [order],
            {"XXBT": type("B", (), {"total": 5.0})(), "ZEUR": type("B", (), {"total": 100.0})()},
            _cfg(),
            lambda _sym: "XXBT",
        )


def test_unit_file_enables_daily_loss_and_drawdown():
    text = (ROOT / "deploy" / "linux" / "tradikg-live.service").read_text()
    loop = (ROOT / "deploy" / "linux" / "run_live_loop.sh").read_text()
    wrapper = (ROOT / "scripts" / "tradikg_run_live_loop.sh").read_text()
    assert "Restart=on-failure" in text
    assert "RestartPreventExitStatus=2" in text
    assert "deploy/linux/run_live_loop.sh" in text
    assert "deploy/linux/run_live_loop.sh" in wrapper
    assert "Environment=TRADIKG_HOME=%h/.tradikg" in text
    assert "Environment=TRADIKG_REPO=@REPO@" in text
    assert "EnvironmentFile=%h/.config/tradikg/live.env" in text
    assert "enable-linger" in text
    assert "--budget 20" in loop
    assert "--daily-loss" in loop and "--daily-loss-fraction 0.05" in loop
    assert "--drawdown-kill-fraction 0.45" in loop
    assert "--require-dry-run" in loop
    assert "live restart-check" in loop
    assert "Exit 2" in loop
    assert "trend_state.json" in text and "KILL_SWITCH" in text
    assert "TRADIKG_DRAWDOWN_KILL_FRACTION" not in text
    assert "TRADIKG_DRAWDOWN_KILL_FRACTION" not in loop
    assert "noch nicht gemergt" not in text
    assert "api_secret" not in text.lower()
    assert "api_secret" not in loop.lower()


def test_install_script_sets_mode_600_and_does_not_print_or_start(tmp_path):
    env_file = tmp_path / "config" / "tradikg" / "live.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("KRAKEN_API_KEY=dummy-not-a-secret\nKRAKEN_API_SECRET=dummy-not-a-secret\n")
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install_linux_unit.sh")],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            **os.environ,
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "HOME": str(tmp_path),
        },
    )
    assert "dummy-not-a-secret" not in proc.stdout and "dummy-not-a-secret" not in proc.stderr
    assert "systemctl --user start" in proc.stdout
    assert "enable --now" not in proc.stdout
    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode == 0o600
    assert env_file.read_text().startswith("KRAKEN_API_KEY=dummy-not-a-secret")
    unit = (tmp_path / "config" / "systemd" / "user" / "tradikg-live.service").read_text()
    assert "@REPO@" not in unit
    assert "Restart=on-failure" in unit and "RestartPreventExitStatus=2" in unit
    assert "deploy/linux/run_live_loop.sh" in unit
    assert "TRADIKG_DRAWDOWN_KILL_FRACTION" not in unit
    loop = tmp_path / ".local" / "bin" / "tradikg_run_live_loop.sh"
    assert loop.is_file()
    assert stat.S_IMODE(loop.stat().st_mode) & 0o111
    loop_text = loop.read_text()
    assert "run_live_loop.sh" in loop_text
    assert "dummy-not-a-secret" not in loop_text
    versioned = (ROOT / "deploy" / "linux" / "run_live_loop.sh").read_text()
    assert "--daily-loss" in versioned and "--daily-loss-fraction 0.05" in versioned
    assert "--drawdown-kill-fraction 0.45" in versioned and "--require-dry-run" in versioned


def test_parser_exposes_the_linux_run_flags():
    args = build_parser().parse_args(
        [
            "live",
            "run",
            "--budget",
            "20",
            "--daily-loss",
            "--daily-loss-fraction",
            "0.05",
            "--drawdown-kill-fraction",
            "0.45",
            "--require-dry-run",
            "--i-understand-this-uses-real-money",
        ]
    )
    assert args.daily_loss and args.require_dry_run
    assert args.budget == 20.0 and args.daily_loss_fraction == 0.05
    assert args.drawdown_kill_fraction == 0.45
    check = build_parser().parse_args(
        [
            "live",
            "restart-check",
            "--budget",
            "20",
            "--daily-loss",
            "--drawdown-kill-fraction",
            "0.45",
        ]
    )
    assert check.budget == 20.0 and check.daily_loss and check.drawdown_kill_fraction == 0.45
    dry = build_parser().parse_args(
        ["live", "dry-run", "--budget", "20", "--daily-loss", "--import-state", "state.json"]
    )
    assert dry.import_state == "state.json" and dry.daily_loss and not dry.reconstruct
    assert dry.drawdown_kill_fraction is None


def test_drawdown_flag_is_off_unless_passed_and_cannot_loosen():
    def ns(fraction: float | None) -> argparse.Namespace:
        return argparse.Namespace(
            budget=20.0,
            invest_fraction=1.0,
            daily_loss=True,
            daily_loss_fraction=0.05,
            drawdown_kill_fraction=fraction,
        )

    off = _trend_config(ns(None))
    assert off.drawdown_enabled is False and off.drawdown_fraction == pytest.approx(0.45)
    on = _trend_config(ns(0.45))
    assert on.drawdown_enabled is True and on.drawdown_fraction == pytest.approx(0.45)
    tight = _trend_config(ns(0.30))
    assert tight.drawdown_enabled is True and tight.drawdown_fraction == pytest.approx(0.30)
    with pytest.raises(Safety, match="0.45"):
        _trend_config(ns(0.46))


def test_market_exit_keeps_slice_cash_and_guards_on_a_hundred_eur_account(tmp_path):
    """Kauf, Market-Ausstieg und Bewertung der Grenzen auf einem 100-€-Konto.

    Nach dem Verkauf ist die Bot-Kasse der Erlös der Scheibe. Der nächste Kauf
    nimmt nur dieses Geld, nicht den Rest des Kontos. Tagesverlust und Drawdown
    bewerten dieselbe Kasse.
    """
    logs: list[str] = []
    clock = {"t": dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)}
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 8)
    cfg = _cfg(
        symbols=("XBTEUR",),
        daily_loss_enabled=True,
        daily_loss_fraction=0.05,
        drawdown_enabled=True,
        drawdown_fraction=0.45,
    )
    run = runner(tmp_path, fake, market, cfg=cfg, now=lambda: clock["t"], log=logs.append)
    run.run_once()
    (buy,) = [o for o in fake.open("limit") if o.pair == "XBTEUR"]
    assert buy.volume * (buy.price or 0) <= 20.0 + 1e-6
    fake.fill(buy.txid)
    run.run_once()
    assert run.state.symbols["XBTEUR"].units > 0
    assert run.state.bot_cash_eur is not None and run.state.bot_cash_eur < 5
    assert run.guard is not None and run.drawdown is not None
    peak = run.drawdown.peak_equity
    assert peak == pytest.approx(20.0, abs=0.5)
    assert run.guard.day_start_equity == pytest.approx(20.0, abs=0.5)

    market.day(90, 8)  # unter SMA: Stop bleibt, Verkauf erst am nächsten Schluss
    run.run_once()
    assert run.state.symbols["XBTEUR"].sell_day
    assert run.state.symbols["XBTEUR"].stop_txid
    market.day(80, 8)
    run.run_once()
    (mkt,) = [o for o in fake.open("market") if o.pair == "XBTEUR"]
    fake.fill(mkt.txid, price=80.0)
    run.run_once()  # gleicher Kerzentag: Ausführung verbuchen, kein neuer Kauf

    cash = run.state.bot_cash_eur
    equity = run._account_equity()
    assert cash is not None and equity is not None
    assert run.state.symbols["XBTEUR"].units == 0
    assert equity == pytest.approx(cash)
    assert 11 < cash < 18
    assert fake.bal["ZEUR"] > 70
    assert equity < fake.bal["ZEUR"] - 50
    assert run.drawdown.peak_equity == pytest.approx(peak)
    assert not run.drawdown.halted
    assert run.guard.halted
    assert run.guard.day_start_equity == pytest.approx(20.0, abs=0.5)
    snap = run._drawdown_snapshot()
    assert snap["equity"] == pytest.approx(equity)
    assert snap["peak_equity"] == pytest.approx(peak)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert isinstance(raw.get("drawdown"), dict)
    reloaded = validate_state_document(raw, cfg)
    assert reloaded.bot_cash_eur == pytest.approx(cash)
    assert reloaded.drawdown == raw["drawdown"]

    # Nächster Wiener Tag: Tagesgrenze neu auf der Scheibe, Drawdown-Höchststand bleibt.
    clock["t"] = dt.datetime(2026, 10, 6, 22, 30, tzinfo=dt.UTC)
    market.day(200, 8)
    run.run_once()
    assert not run.guard.halted
    assert run.guard.day_start_equity == pytest.approx(cash)
    assert run.guard.day_start_equity < 30
    assert not run.drawdown.halted
    assert run.drawdown.peak_equity == pytest.approx(peak)
    (again,) = [o for o in fake.open("limit") if o.pair == "XBTEUR" and o.side == OrderSide.BUY]
    notional = again.volume * (again.price or 0)
    assert 1 < notional <= cash + 1e-6
    assert notional < 18
    assert fake.bal["ZEUR"] > notional + 50
