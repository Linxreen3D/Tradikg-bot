"""Echtgeld-Trendfolge gegen eine simulierte Kraken-Börse (keine Netzwerkzugriffe)."""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from tradikg_bot.live.models import (
    Balance,
    ExchangeOrder,
    OrderBook,
    OrderSide,
    OrderStatus,
    OutcomeUnknownError,
    PairRules,
)
from tradikg_bot.live.trend_live import LiveSafetyError, LiveTrendConfig, LiveTrendRunner

RULES = {
    "XBTEUR": PairRules("XXBTZEUR", "XBTEUR", "XXBT", "ZEUR", 8, 1, 0.1, 0.00005, 0.5),
    "ETHEUR": PairRules("XETHZEUR", "ETHEUR", "XETH", "ZEUR", 8, 2, 0.01, 0.002, 0.5),
}


class FakeKraken:
    def __init__(self, eur: float = 20.0) -> None:
        self.bal = {"ZEUR": eur, "XXBT": 0.0, "XETH": 0.0}
        self.orders: dict[str, ExchangeOrder] = {}
        self.refs: dict[str, int] = {}
        self.params: dict[str, dict] = {}
        self.validated: list[dict] = []
        self.price = {"XBTEUR": 100.0, "ETHEUR": 10.0}
        self.withdraw = False
        self.unknown_next = False

    def pair_rules(self, sym):
        return RULES[sym]

    def order_book(self, sym, depth=1):
        p = self.price[sym]
        return OrderBook(sym, [(p * 0.999, 1.0)], [(p * 1.001, 1.0)])

    def balances(self):
        held = sum(
            o.volume * (o.price or 0)
            for o in self.orders.values()
            if o.status == OrderStatus.OPEN and o.side == OrderSide.BUY
        )
        return {a: Balance(a, v, held if a == "ZEUR" else 0.0) for a, v in self.bal.items()}

    def has_withdrawal_permission(self):
        return self.withdraw

    def has_margin_positions(self):
        return False

    def add_order(self, params, validate=False):
        if validate:
            self.validated.append(params)
            return None
        txid = f"T{len(self.orders) + 1}"
        self.orders[txid] = ExchangeOrder(
            txid,
            params["userref"],
            params["pair"],
            OrderSide(params["type"]),
            params["ordertype"],
            OrderStatus.OPEN,
            float(params["volume"]),
            price=float(params["price"]) if "price" in params else None,
        )
        self.refs[txid], self.params[txid] = params["userref"], params
        if params.get("timeinforce") == "IOC":  # sofort zum Ask ausgeführt
            sym = "XBTEUR" if "XBT" in params["pair"] else "ETHEUR"
            self.fill(txid, price=self.price[sym] * 1.001)
        if self.unknown_next:
            self.unknown_next = False
            raise OutcomeUnknownError("Timeout nach dem Senden")
        return txid

    def query_orders(self, txids):
        return {t: self.orders[t] for t in txids if t in self.orders}

    def orders_by_client_ref(self, ref):
        return [o for t, o in self.orders.items() if self.refs[t] == ref]

    def cancel_order(self, txid):
        o = self.orders[txid]
        if o.status == OrderStatus.OPEN:
            o.status = OrderStatus.CANCELED

    def fill(self, txid, volume=None, price=None, close=True):
        o = self.orders[txid]
        vol = o.volume if volume is None else volume
        px = price or o.price or self.price["XBTEUR" if "XBT" in o.pair else "ETHEUR"]
        o.filled, o.avg_price, o.fee = vol, px, vol * px * 0.004
        if close:
            o.status = OrderStatus.CLOSED
        base = "XXBT" if "XBT" in o.pair else "XETH"
        sign = 1 if o.side == OrderSide.BUY else -1
        self.bal[base] += sign * vol
        self.bal["ZEUR"] -= sign * vol * px + o.fee

    def open_orders(self):
        return self.open()

    def open(self, kind=None):
        return [
            o
            for o in self.orders.values()
            if o.status == OrderStatus.OPEN and (kind is None or o.ordertype == kind)
        ]


class Market:
    """Tagesschlüsse je Symbol; ``day(x)`` hängt einen neuen Schluss an."""

    def __init__(self, n=5, start=100.0):
        idx = pd.date_range("2026-10-01", periods=n, freq="D", tz="UTC")
        self.c = {"XBTEUR": pd.Series(start, index=idx), "ETHEUR": pd.Series(start / 10, index=idx)}

    def day(self, btc, eth=None):
        for sym, v in (("XBTEUR", btc), ("ETHEUR", eth if eth is not None else btc / 10)):
            s = self.c[sym]
            self.c[sym] = pd.concat([s, pd.Series([v], index=[s.index[-1] + pd.Timedelta(days=1)])])

    def __call__(self, sym):
        return self.c[sym]


CFG = LiveTrendConfig(budget_eur=20.0, invest_fraction=1.0, sma=3)


def runner(tmp_path, fake, market, dry_run=False, cfg=CFG, now=None, log=None):
    return LiveTrendRunner(
        fake,
        tmp_path / "state.json",
        cfg,
        market,
        tmp_path / "KILL",
        dry_run=dry_run,
        log=log or (lambda m: None),
        now=now,
    )


def test_dry_run_only_validates_and_saves_nothing(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)  # über SMA
    runner(tmp_path, fake, m, dry_run=True).run_once()
    assert not fake.orders and not (tmp_path / "state.json").exists()
    buys = [p for p in fake.validated if p["type"] == "buy"]
    assert len(buys) == 2 and all(p["oflags"] == "post" for p in buys)
    btc = next(p for p in buys if p["pair"] == "XBTEUR")
    assert float(btc["volume"]) * float(btc["price"]) <= 10.0  # höchstens 10 € je Coin


def test_buy_then_server_side_stop_and_trailing(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r.run_once()
    buy = next(o for o in fake.open() if o.pair == "XBTEUR")
    assert fake.refs[buy.txid] == 26100611  # Datum 06.10. · Symbol 1 · Kauf
    fake.fill(buy.txid)
    r.run_once()  # stündlicher Abgleich: Stop sofort bei Kraken
    (stop,) = [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert stop.price == pytest.approx(buy.price * 0.8, abs=0.1) and stop.volume == buy.volume
    m.day(150)
    r.run_once()  # neuer Höchststand → Stop nachziehen
    assert fake.orders[stop.txid].status == OrderStatus.CANCELED
    (stop2,) = [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert stop2.price == pytest.approx(120.0)


def test_exit_signal_maker_then_market_fallback_after_partial_fill(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r.run_once()
    buy = next(o for o in fake.open() if o.pair == "XBTEUR")
    fake.fill(buy.txid)
    r.run_once()
    m.day(90)  # unter SMA → Stop stornieren, Verkauf-Limit post-only
    r.run_once()
    assert not [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    (sell,) = [o for o in fake.open("limit") if o.pair == "XBTEUR"]
    assert fake.params[sell.txid]["oflags"] == "post" and sell.side == OrderSide.SELL
    fake.fill(sell.txid, volume=sell.volume / 2, close=False)  # nur halb ausgeführt
    m.day(85)
    r.run_once()
    (mkt,) = [o for o in fake.open("market") if o.pair == "XBTEUR"]
    assert mkt.volume == pytest.approx(buy.volume / 2)  # nie mehr als die eigene Restmenge


def test_stop_trigger_blocks_until_fresh_cross(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r.run_once()
    fake.fill(next(o for o in fake.open() if o.pair == "XBTEUR").txid)
    r.run_once()
    stop = next(o for o in fake.open("stop-loss") if o.pair == "XBTEUR")
    fake.fill(stop.txid, price=stop.price)  # Kraken löst den Stop aus
    m.day(120)
    r.run_once()
    st = r.state.symbols["XBTEUR"]
    assert st.units == 0 and st.blocked and not [o for o in fake.open() if o.pair == "XBTEUR"]
    m.day(50)
    m.day(200)
    r.run_once()  # erst unter, dann wieder über SMA → neuer Kauf
    assert [o for o in fake.open("limit") if o.pair == "XBTEUR"]


def test_safety_guards(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    fake.withdraw = True
    with pytest.raises(LiveSafetyError, match="Auszahlungsrechte"):
        runner(tmp_path, fake, m).run_once()
    with pytest.raises(LiveSafetyError):
        LiveTrendConfig(budget_eur=500)
    fake.withdraw = False
    (tmp_path / "KILL").touch()
    runner(tmp_path, fake, m).run_once()
    assert not fake.orders  # Kill Switch: keine neuen Käufe


def test_unknown_outcome_is_looked_up_not_resent(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    fake.unknown_next = True
    r = runner(tmp_path, fake, m)
    r.run_once()
    assert len([o for o in fake.orders.values() if o.pair == "XBTEUR"]) == 1
    assert r.state.symbols["XBTEUR"].buy_txid == "T1"


def test_never_sells_more_than_held_and_state_survives_restart(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r.run_once()
    buy = next(o for o in fake.open() if o.pair == "XBTEUR")
    fake.fill(buy.txid)
    fake.bal["XXBT"] = buy.volume / 4  # von Hand teilweise verkauft
    again = runner(tmp_path, fake, m)  # Neustart: Zustand aus Datei
    again.run_once()
    assert again.state.symbols["XBTEUR"].units == pytest.approx(buy.volume / 4)
    (stop,) = [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert stop.volume == pytest.approx(buy.volume / 4)


def test_cli_requires_confirmation_and_kill_switch_works(tmp_path, monkeypatch, capsys):
    from tradikg_bot.cli import main

    monkeypatch.setenv("TRADIKG_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRADIKG_KRAKEN_CREDENTIALS", str(tmp_path / "fehlt.json"))
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    assert main(["live", "run"]) == 2
    assert "--i-understand-this-uses-real-money" in capsys.readouterr().out
    assert main(["live", "check"]) == 2  # ohne Key: verständlicher Abbruch, keine Anfrage
    assert "Kein API-Key" in capsys.readouterr().out
    assert main(["live", "kill"]) == 0 and (tmp_path / "home/live/KILL_SWITCH").exists()
    assert main(["live", "status"]) == 0 and "AKTIV" in capsys.readouterr().out
    assert main(["live", "kill", "--release"]) == 0
    assert not (tmp_path / "home/live/KILL_SWITCH").exists()


def test_crash_between_send_and_save_does_not_duplicate_order(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r._save = lambda: None  # Absturz: Zustand wird nie gespeichert
    r.run_once()
    assert len(fake.orders) == 2
    with pytest.raises(LiveSafetyError, match="Zustand"):  # Neustart ohne Zustand: Halt
        runner(tmp_path, fake, m).run_once()
    assert len(fake.orders) == 2  # nichts doppelt gesendet
    del r._save  # Datei wiederhergestellt
    r._save()
    runner(tmp_path, fake, m).run_once()
    assert len(fake.orders) == 2  # gleiche userref: Orders übernommen statt neu gesendet


def test_buy_now_replaces_limit_buys_immediately_and_sets_stop(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r.snapshot_path = tmp_path / "snap.json"
    r.run_once()  # wartende Maker-Limits
    limits = [o for o in fake.open("limit")]
    assert len(limits) == 2
    r.buy_now()
    assert all(fake.orders[o.txid].status == OrderStatus.CANCELED for o in limits)
    for sym in ("XBTEUR", "ETHEUR"):
        st = r.state.symbols[sym]
        assert st.units > 0 and st.buy_txid is None
        (stop,) = [o for o in fake.open("stop-loss") if o.pair == sym]
        assert stop.volume == pytest.approx(st.units, rel=1e-6)
    import json

    snap = json.loads((tmp_path / "snap.json").read_text())
    assert snap["balances"]["ZEUR"]["total"] < 20 and snap["symbols"]["XBTEUR"]["units"] > 0
    before = len(fake.orders)
    r.buy_now()  # zweites Mal: schon gekauft → nichts Neues
    assert len(fake.orders) == before


def test_buy_now_respects_downtrend_and_kill_switch(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(80)  # unter SMA
    runner(tmp_path, fake, m).buy_now()
    assert not fake.orders
    m.day(200)
    (tmp_path / "KILL").touch()
    runner(tmp_path, fake, m).buy_now()
    assert not fake.orders


# -- Audit Punkt 8: Wiederanlauf nach Mac-Ausfall, Neustart, API-Timeout -----------------


def _bought(tmp_path):
    """Position BTC+ETH gekauft, Stops bei Kraken gesetzt."""
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r.run_once()
    for o in list(fake.open("limit")):
        fake.fill(o.txid)
    r.run_once()
    assert len(fake.open("stop-loss")) == 2
    return fake, m


def test_restart_keeps_existing_stops_untouched(tmp_path):
    fake, m = _bought(tmp_path)
    before = {o.txid: (o.price, o.volume) for o in fake.open("stop-loss")}
    n_orders = len(fake.orders)
    runner(tmp_path, fake, m).run_once()  # Neustart, gleicher Tag
    assert {o.txid: (o.price, o.volume) for o in fake.open("stop-loss")} == before
    assert len(fake.orders) == n_orders  # nichts Neues gesendet


def test_stop_filled_while_mac_was_off_is_booked_and_blocks(tmp_path):
    fake, m = _bought(tmp_path)
    stop = next(o for o in fake.open("stop-loss") if o.pair == "XBTEUR")
    fake.fill(stop.txid, price=85.0)
    m.day(111)  # Mac wieder an, Kurs wieder über SMA
    r = runner(tmp_path, fake, m)
    r.run_once()
    st = r.state.symbols["XBTEUR"]
    assert st.units == 0 and st.blocked and st.stop_txid is None
    assert not [o for o in fake.open() if o.pair == "XBTEUR"]  # kein Rückkauf


def test_api_timeout_mid_run_changes_nothing_and_next_run_recovers(tmp_path):
    fake, m = _bought(tmp_path)
    before = {o.txid for o in fake.open("stop-loss")}
    state_before = (tmp_path / "state.json").read_text()

    def flaky(sym):
        raise TimeoutError("Kraken antwortet nicht")

    m.day(130)  # neuer Tag: Stop müsste nachgezogen werden
    with pytest.raises(TimeoutError):
        runner(tmp_path, fake, flaky).run_once()
    assert {o.txid for o in fake.open("stop-loss")} == before
    assert (tmp_path / "state.json").read_text() == state_before
    r = runner(tmp_path, fake, m)
    r.run_once()  # nächster Durchgang
    stops = fake.open("stop-loss")
    assert len(stops) == 2
    assert r.state.symbols["XBTEUR"].stop_price == pytest.approx(104.0)  # 130 × 0,8


def test_timeout_while_cancelling_stop_leaves_a_stop_after_next_run(tmp_path):
    fake, m = _bought(tmp_path)
    real_cancel = fake.cancel_order

    def cancel_then_timeout(txid):
        real_cancel(txid)
        raise TimeoutError("Antwort verloren")

    fake.cancel_order = cancel_then_timeout
    m.day(130)
    with pytest.raises(TimeoutError):
        runner(tmp_path, fake, m).run_once()
    fake.cancel_order = real_cancel
    runner(tmp_path, fake, m).run_once()
    assert {o.pair for o in fake.open("stop-loss")} == {"XBTEUR", "ETHEUR"}


def _loss_cfg(**overrides):
    params = dict(
        budget_eur=20.0,
        invest_fraction=1.0,
        sma=3,
        daily_loss_enabled=True,
        daily_loss_fraction=0.05,
        daily_loss_timezone="Europe/Vienna",
    )
    params.update(overrides)
    return LiveTrendConfig(**params)


def _btc_position_then_drop(tmp_path, *, price: float, enabled: bool = True):
    """BTC gekauft und gestoppt, ETH noch unter dem Schnitt. Danach fällt der BTC-Kurs."""
    logs: list[str] = []
    # 19.10.2026 ist noch Sommerzeit: 10:00 UTC = 12:00 Wien.
    clock = {"t": dt.datetime(2026, 10, 19, 10, 0, tzinfo=dt.UTC)}
    cfg = _loss_cfg(daily_loss_enabled=enabled)
    fake, market = FakeKraken(), Market()
    market.day(110, 8)  # BTC über SMA, ETH darunter → nur BTC wird gekauft
    run = runner(tmp_path, fake, market, cfg=cfg, now=lambda: clock["t"], log=logs.append)
    run.run_once()
    for order in list(fake.open("limit")):
        if order.pair == "XBTEUR":
            fake.fill(order.txid)
    run.run_once()
    assert [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    fake.price["XBTEUR"] = price
    run.run_once()
    return fake, market, run, clock, logs


def test_daily_loss_config_is_off_by_default_and_cannot_loosen_past_10_percent():
    cfg = LiveTrendConfig()
    assert cfg.daily_loss_enabled is False
    assert cfg.daily_loss_fraction == pytest.approx(0.05)
    assert cfg.daily_loss_timezone == "Europe/Vienna"
    assert cfg.budget_eur == 20.0 and cfg.stop_frac == pytest.approx(0.20)
    LiveTrendConfig(daily_loss_fraction=0.10)  # Obergrenze, nicht darüber
    with pytest.raises(LiveSafetyError, match="0.10"):
        LiveTrendConfig(daily_loss_fraction=0.11)
    with pytest.raises(LiveSafetyError, match="50"):
        LiveTrendConfig(budget_eur=50.01)


def test_daily_loss_hit_blocks_buys_but_stop_and_sell_still_work(tmp_path):
    fake, market, run, clock, logs = _btc_position_then_drop(tmp_path, price=70.0)
    assert run.guard is not None and run.guard.halted
    assert run.guard.day_start_equity == pytest.approx(20.0, abs=0.05)
    stop = next(o for o in fake.open("stop-loss") if o.pair == "XBTEUR")
    assert stop.status == OrderStatus.OPEN  # die Grenze selbst storniert den Stop nicht
    assert any("Keine neuen Käufe" in line and "Kraken-Stops" in line for line in logs)

    market.day(110, 14)  # selber Wiener Tag: ETH will kaufen, BTC bleibt über dem Schnitt
    run.run_once()
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR"]
    assert [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]  # Stop bleibt bei Kraken
    assert any("ETHEUR: keine neuen Käufe" in line for line in logs)
    run.buy_now()  # Sofortkauf ist auch ein neuer Kauf
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR"]

    market.day(80, 14)  # BTC unter SMA → Verkauf, ETH-Kauf bleibt gesperrt
    run.run_once()
    assert [o for o in fake.orders.values() if o.pair == "XBTEUR" and o.side == OrderSide.SELL]
    assert not [o for o in fake.open() if o.pair == "ETHEUR" and o.side == OrderSide.BUY]


def test_daily_loss_not_hit_still_buys(tmp_path):
    fake, market, run, _clock, _logs = _btc_position_then_drop(tmp_path, price=108.0)
    assert run.guard is not None and not run.guard.halted
    market.day(110, 14)
    run.run_once()
    assert [o for o in fake.open("limit") if o.pair == "ETHEUR" and o.side == OrderSide.BUY]


def test_daily_loss_flag_off_does_not_change_buys_or_state(tmp_path):
    fake, market, run, _clock, logs = _btc_position_then_drop(tmp_path, price=70.0, enabled=False)
    assert run.guard is None
    assert not any(line.startswith("[TAGESLIMIT]") for line in logs)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert "daily_loss" not in raw
    market.day(110, 14)
    run.run_once()
    assert [o for o in fake.open("limit") if o.pair == "ETHEUR"]


def test_daily_loss_persists_across_restart_and_resets_at_vienna_midnight(tmp_path):
    fake, market, run, clock, logs = _btc_position_then_drop(tmp_path, price=70.0)
    assert run.guard is not None and run.guard.halted
    start_equity = run.guard.day_start_equity
    halted_day = run.guard.current_day

    again = runner(
        tmp_path,
        fake,
        market,
        cfg=_loss_cfg(),
        now=lambda: clock["t"],
        log=logs.append,
    )
    assert again.guard is not None
    assert again.guard.halted
    assert again.guard.current_day == halted_day
    assert again.guard.day_start_equity == pytest.approx(start_equity)
    market.day(112, 15)  # neuer Kerzentag, aber noch derselbe Wiener Tag
    again.run_once()
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR"]

    # 19.10.2026 ist CEST (UTC+2): 22:30 UTC = 20.10. 00:30 Wien.
    clock["t"] = dt.datetime(2026, 10, 19, 22, 30, tzinfo=dt.UTC)
    market.day(112, 16)
    again.run_once()
    assert again.guard.current_day == dt.date(2026, 10, 20)
    assert not again.guard.halted
    assert [o for o in fake.open("limit") if o.pair == "ETHEUR" and o.side == OrderSide.BUY]
    assert any("Neuer Tag 2026-10-20" in line for line in logs)


def test_daily_loss_does_not_replace_withdrawal_abort(tmp_path):
    fake, market = FakeKraken(), Market()
    market.day(110, 14)
    fake.withdraw = True
    with pytest.raises(LiveSafetyError, match="Auszahlungsrechte"):
        runner(tmp_path, fake, market, cfg=_loss_cfg()).run_once()


def test_lost_state_file_never_buys_twice_while_bot_orders_are_open(tmp_path):
    fake, m = _bought(tmp_path)
    fake.bal["ZEUR"] += 20.0  # Nutzer zahlt nach
    (tmp_path / "state.json").unlink()  # Zustand verloren (Festplatte, versehentlich gelöscht)
    with pytest.raises(LiveSafetyError, match="Zustand"):
        runner(tmp_path, fake, m).run_once()
    assert not [o for o in fake.open() if o.side == OrderSide.BUY]
