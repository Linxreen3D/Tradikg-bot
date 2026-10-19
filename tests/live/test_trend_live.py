"""Echtgeld-Trendfolge gegen eine simulierte Kraken-Börse (keine Netzwerkzugriffe)."""

from __future__ import annotations

import datetime as dt
import json
import sys

import pandas as pd
import pytest

from tradikg_bot.live.models import (
    Balance,
    ExchangeOrder,
    OrderBook,
    OrderRejectedError,
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
        self.reject_if = None  # params -> True heißt: Order ablehnen, nichts anlegen
        self.reject_stops = 0
        self.reject_message = "EOrder:Insufficient funds"
        self.events: list[tuple] = []

    def pair_rules(self, sym):
        return RULES[sym]

    def order_book(self, sym, depth=1):
        p = self.price[sym]
        return OrderBook(sym, [(p * 0.999, 1.0)], [(p * 1.001, 1.0)])

    def _asset_of(self, order: ExchangeOrder) -> str:
        if order.side == OrderSide.BUY:
            return "ZEUR"
        return "XXBT" if "XBT" in order.pair else "XETH"

    def _reserved(self, asset: str) -> float:
        """Wie Kraken ``hold_trade``: Kauf bindet Euro, Verkauf (Limit, Market, Stop) die Basis."""
        total = 0.0
        for order in self.orders.values():
            if order.status != OrderStatus.OPEN or self._asset_of(order) != asset:
                continue
            if order.side == OrderSide.BUY:
                total += order.volume * (order.price or 0)
            else:
                total += order.volume - order.filled
        return total

    def balances(self):
        return {a: Balance(a, v, self._reserved(a)) for a, v in self.bal.items()}

    def has_withdrawal_permission(self):
        return self.withdraw

    def has_margin_positions(self):
        return False

    def add_order(self, params, validate=False):
        if validate:
            self.validated.append(params)
            return None
        if self.reject_if and self.reject_if(params):
            raise OrderRejectedError("EOrder:Orders do not match")
        if params.get("ordertype") == "stop-loss" and self.reject_stops > 0:
            self.reject_stops -= 1
            self.events.append(("reject", params.get("price")))
            raise OrderRejectedError(self.reject_message)
        if params["type"] == "sell":
            base = "XXBT" if "XBT" in params["pair"] else "XETH"
            need = float(params["volume"])
            if need > self.bal[base] - self._reserved(base) + 1e-9:
                self.events.append(("reject", params.get("price")))
                raise OrderRejectedError("EOrder:Insufficient funds")
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
        self.events.append(("add", params["ordertype"], params.get("price"), txid))
        if self.unknown_next:
            self.unknown_next = False
            raise OutcomeUnknownError("Timeout nach dem Senden")
        return txid

    def query_orders(self, txids):
        return {t: self.orders[t] for t in txids if t in self.orders}

    def orders_by_client_ref(self, ref):
        return [o for t, o in self.orders.items() if self.refs[t] == ref]

    def cancel_order(self, txid):
        self.events.append(("cancel", txid))
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


def test_exit_signal_keeps_stop_then_market_sells_remainder(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r.run_once()
    buy = next(o for o in fake.open() if o.pair == "XBTEUR")
    fake.fill(buy.txid)
    r.run_once()
    m.day(90)  # unter SMA → Stop bleibt, kein ruhendes Verkaufs-Limit
    r.run_once()
    (stop,) = [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert not [o for o in fake.open("limit") if o.pair == "XBTEUR" and o.side == OrderSide.SELL]
    assert r.state.symbols["XBTEUR"].sell_day
    m.day(85)  # nächster Tagesschluss: Stop lösen, Market-Verkauf der Restmenge
    r.run_once()
    assert fake.orders[stop.txid].status == OrderStatus.CANCELED
    (mkt,) = [o for o in fake.open("market") if o.pair == "XBTEUR"]
    assert mkt.volume == pytest.approx(buy.volume)
    fake.fill(mkt.txid, volume=mkt.volume / 2, close=False)
    m.day(80)
    r.run_once()
    (rest,) = [o for o in fake.open("market") if o.pair == "XBTEUR"]
    assert rest.volume == pytest.approx(buy.volume / 2)  # nie mehr als die eigene Restmenge


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
    with pytest.raises(LiveSafetyError, match="20"):
        LiveTrendConfig(budget_eur=20.01)


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
    assert "bot_cash_eur" not in raw
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


def test_slice_equity_trips_at_five_percent_of_budget_not_of_the_account(tmp_path):
    """Konto 100 €, Budget 20 €: 5 % sind etwa 1 € der Scheibe, nicht 5 € vom Konto."""
    logs: list[str] = []
    clock = {"t": dt.datetime(2026, 10, 19, 10, 0, tzinfo=dt.UTC)}
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 8)
    run = runner(tmp_path, fake, market, cfg=_loss_cfg(), now=lambda: clock["t"], log=logs.append)
    run.run_once()
    assert run.guard is not None
    assert run.guard.day_start_equity == pytest.approx(20.0, abs=0.05)
    assert run.state.bot_cash_eur == pytest.approx(20.0)
    for order in list(fake.open("limit")):
        if order.pair == "XBTEUR":
            fake.fill(order.txid)
    run.run_once()
    assert run.guard.day_start_equity == pytest.approx(20.0, abs=0.05)
    assert run.state.bot_cash_eur == pytest.approx(10.0, abs=0.2)
    units = run.state.symbols["XBTEUR"].units
    assert units > 0
    # Kurs so weit runter, dass die Scheibe die 5 % reißt, das 100-€-Konto aber nicht.
    bot_cash = run.state.bot_cash_eur
    start = run.guard.day_start_equity
    target = start * (1 - 0.05) - 0.05  # knapp unter dem Boden von 19 €
    # Bid = Preis * 0.999, also Preis ≈ (Ziel-Kasse) / Menge / 0.999
    fake.price["XBTEUR"] = max(1.0, (target - bot_cash) / units / 0.999)
    run.run_once()
    equity = run._account_equity()
    account = fake.bal["ZEUR"] + units * fake.price["XBTEUR"] * 0.999
    assert equity is not None and equity <= start * (1 - 0.05)
    assert account > 100 * (1 - 0.05)  # altes Vollkonto wäre noch nicht bei 5 %
    assert run.guard.halted
    assert run.guard.day_start_equity == pytest.approx(start)

    cash_before = run.state.bot_cash_eur
    units_before = run.state.symbols["XBTEUR"].units
    fake.bal["ZEUR"] += 80  # Einzahlung
    fake.bal["XXBT"] += 0.02  # manueller Zukauf, nicht die Bot-Menge
    fake.bal["XETH"] += 0.5
    fake.bal["ZEUR"] -= 30  # manuell woanders ausgegeben, Scheibe bleibt gedeckt
    run.run_once()
    assert run.guard.day_start_equity == pytest.approx(start)
    assert run.state.bot_cash_eur == pytest.approx(cash_before)
    assert run.state.symbols["XBTEUR"].units == pytest.approx(units_before)
    assert run.state.symbols["XBTEUR"].units < fake.bal["XXBT"]

    again = runner(tmp_path, fake, market, cfg=_loss_cfg(), now=lambda: clock["t"], log=logs.append)
    assert again.state.bot_cash_eur == pytest.approx(cash_before)
    assert again.guard is not None
    assert again.guard.day_start_equity == pytest.approx(start)
    assert again.guard.halted


def test_missing_bid_blocks_buys_and_does_not_use_entry_price(tmp_path):
    logs: list[str] = []
    clock = {"t": dt.datetime(2026, 10, 19, 10, 0, tzinfo=dt.UTC)}
    fake, market = FakeKraken(), Market()
    market.day(110, 8)
    run = runner(tmp_path, fake, market, cfg=_loss_cfg(), now=lambda: clock["t"], log=logs.append)
    run.run_once()
    for order in list(fake.open("limit")):
        if order.pair == "XBTEUR":
            fake.fill(order.txid)
    run.run_once()
    assert run.guard is not None and not run.guard.halted
    start = run.guard.day_start_equity
    real_book = fake.order_book

    def no_btc_bid(sym, depth=1):
        book = real_book(sym, depth)
        if sym == "XBTEUR":
            return OrderBook(sym, [], book.asks)
        return book

    fake.order_book = no_btc_bid
    fake.price["XBTEUR"] = 5.0  # echter Bid würde die Grenze reißen
    market.day(110, 14)  # ETH will kaufen
    run.run_once()
    assert not run.guard.halted  # ohne Bid keine Scheinbewertung zum Einstand oder zu null
    assert run.guard.day_start_equity == pytest.approx(start)
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR"]
    assert [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert any("kein Bid" in line and "Einstandspreis" in line for line in logs)

    fake.order_book = real_book
    run.run_once()  # selber Tag, diesmal mit Bid: die Grenze gilt
    assert run.guard.halted
    assert run.guard.day_start_equity == pytest.approx(start)


def test_balance_fetch_error_still_sells_and_keeps_the_stop_path(tmp_path):
    logs: list[str] = []
    clock = {"t": dt.datetime(2026, 10, 19, 10, 0, tzinfo=dt.UTC)}
    fake, market = FakeKraken(), Market()
    market.day(110, 8)
    run = runner(tmp_path, fake, market, cfg=_loss_cfg(), now=lambda: clock["t"], log=logs.append)
    run.run_once()
    for order in list(fake.open("limit")):
        if order.pair == "XBTEUR":
            fake.fill(order.txid)
    run.run_once()
    assert [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]

    def boom():
        raise TimeoutError("Kraken balance")

    fake.balances = boom
    market.day(80, 14)  # BTC unter SMA → Verkauf, ETH über SMA → wäre ein Kauf
    run.run_once()  # darf nicht abbrechen
    assert [o for o in fake.orders.values() if o.pair == "XBTEUR" and o.side == OrderSide.SELL]
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR" and o.side == OrderSide.BUY]
    assert any("Guthaben nicht lesbar" in line for line in logs)
    assert any("ETHEUR: keine neuen Käufe" in line for line in logs)


def _protection(fake: FakeKraken, r: LiveTrendRunner, sym: str) -> tuple[bool, str]:
    st = r.state.symbols[sym]
    stops = [o for o in fake.open("stop-loss") if o.pair == sym]
    sells = [
        o
        for o in fake.open()
        if o.pair == sym and o.side == OrderSide.SELL and o.ordertype != "stop-loss"
    ]
    protected = st.units <= 1e-12 or bool(stops) or bool(sells)
    stop_txt = ",".join(f"{o.price:.2f}×{o.volume:.8f}" for o in stops) or "–"
    sell_txt = ",".join(f"{o.ordertype}:{o.volume:.8f}" for o in sells) or "–"
    line = (
        f"{sym} close={float(r.closes(sym).iloc[-1]):.2f} units={st.units:.8f} "
        f"stop={stop_txt} sell={sell_txt} protected={'yes' if protected else 'NO'}"
    )
    return protected, line


def test_rejected_market_exit_restores_the_stop(tmp_path):
    logs: list[str] = []
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r.log = logs.append
    r.run_once()
    for order in list(fake.open("limit")):
        fake.fill(order.txid)
    r.run_once()
    m.day(90)  # Signal: Stop bleibt
    r.run_once()
    assert len(fake.open("stop-loss")) == 2
    fake.reject_if = lambda params: params.get("ordertype") == "market"
    m.day(85)
    r.run_once()
    assert not fake.open("market")
    assert {o.pair for o in fake.open("stop-loss")} == {"XBTEUR", "ETHEUR"}
    assert any("ALARM" in line and "Market-Ausstieg" in line for line in logs)
    assert r.state.symbols["XBTEUR"].stop_txid
    assert r.state.symbols["XBTEUR"].sell_day  # Ausstieg bleibt vorgemerkt


def test_market_exit_keeps_the_stop_when_cancel_does_not_remove_it(tmp_path):
    logs: list[str] = []
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    r.log = logs.append
    r.run_once()
    for order in list(fake.open("limit")):
        fake.fill(order.txid)
    r.run_once()
    m.day(90)
    r.run_once()
    stops = {o.pair: o.txid for o in fake.open("stop-loss")}
    fake.cancel_order = lambda txid: None  # Börse lässt den Stop liegen
    m.day(85)
    r.run_once()
    assert not fake.open("market")
    assert {o.pair: o.txid for o in fake.open("stop-loss")} == stops
    assert r.state.symbols["XBTEUR"].stop_txid == stops["XBTEUR"]
    assert any("ließ sich nicht lösen" in line for line in logs)


def test_resting_limit_sell_is_replaced_by_a_stop_on_the_next_pass(tmp_path):
    """Alter Zustand: Post-only liegt, Stop ist weg. Nächster Durchgang macht das rückgängig."""
    fake, m = _bought(tmp_path)
    r = runner(tmp_path, fake, m)
    st = r.state.symbols["XBTEUR"]
    stop = next(o for o in fake.open("stop-loss") if o.pair == "XBTEUR")
    fake.cancel_order(stop.txid)
    limit = fake.add_order(
        {
            "pair": "XBTEUR",
            "userref": 1,
            "type": "sell",
            "ordertype": "limit",
            "oflags": "post",
            "price": "100.0",
            "volume": f"{stop.volume:.8f}",
        }
    )
    st.stop_txid, st.stop_price, st.stop_units = None, 0.0, 0.0
    st.sell_txid, st.sell_day = limit, st.last_candle
    r.run_once()  # gleicher Tag: _decide kehrt sofort zurück
    assert fake.orders[limit].status == OrderStatus.CANCELED
    (restored,) = [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert restored.volume == pytest.approx(stop.volume)
    assert r.state.symbols["XBTEUR"].stop_txid == restored.txid
    eth = next(o for o in fake.open("stop-loss") if o.pair == "ETHEUR")
    assert eth.txid  # der andere Stop wurde nicht angetastet


def test_paper_replay_exit_never_ends_a_cycle_without_protection(tmp_path, capsys):
    """Mehrtagelauf gegen die Scheinbörse. Am Ende jedes Durchgangs liegt ein Stop oder
    ein Market-Verkauf, solange noch eine Menge offen ist."""
    logs: list[str] = []
    fake, m = FakeKraken(), Market()
    r = runner(tmp_path, fake, m)
    r.log = logs.append
    transcript: list[str] = []

    def step(title: str) -> None:
        r.run_once()
        lines = [title]
        for sym in r.cfg.symbols:
            ok, line = _protection(fake, r, sym)
            assert ok, line
            lines.append("  " + line)
        transcript.append("\n".join(lines))

    m.day(110)
    r.run_once()  # Kauf-Limits, noch keine Position
    for order in list(fake.open("limit")):
        fake.fill(order.txid)
    step("nach Kauf und Stop")
    m.day(130)
    step("höherer Schluss, Stop nachgezogen")
    m.day(120)  # nicht mehr über SMA, aber über dem Stop (20 % unter 130)
    step("Verkaufssignal – Stop bleibt, kein Post-only")
    assert all(st.sell_day for st in r.state.symbols.values())
    assert not [o for o in fake.open("limit") if o.side == OrderSide.SELL]

    # Alter Fehlerzustand mitten am Tag: Limit ohne Stop. Der nächste Durchgang heilt ihn.
    st = r.state.symbols["XBTEUR"]
    live_stop = next(o for o in fake.open("stop-loss") if o.pair == "XBTEUR")
    fake.cancel_order(live_stop.txid)
    naked = fake.add_order(
        {
            "pair": "XBTEUR",
            "userref": 99,
            "type": "sell",
            "ordertype": "limit",
            "oflags": "post",
            "price": "120.0",
            "volume": f"{live_stop.volume:.8f}",
        }
    )
    st.stop_txid, st.stop_price, st.stop_units = None, 0.0, 0.0
    st.sell_txid = naked
    step("geheilt: Limit ohne Stop wieder mit Stop ersetzt")
    assert fake.orders[naked].status == OrderStatus.CANCELED

    fake.reject_if = lambda params: params.get("ordertype") == "market"
    m.day(115)
    step("Market-Ausstieg abgelehnt – Stop im selben Durchgang zurück")
    assert not fake.open("market")
    assert any("ALARM" in line and "Market-Ausstieg" in line for line in logs)
    fake.reject_if = None
    m.day(112)
    step("nächster Tag: Market-Verkauf, Stop erst unmittelbar davor gelöst")
    assert {o.pair for o in fake.open("market")} == {"XBTEUR", "ETHEUR"}
    assert not fake.open("stop-loss")
    for order in list(fake.open("market")):
        fake.fill(order.txid)
    step("Market ausgeführt – flat, kein Stop nötig")
    assert all(st.units == 0 for st in r.state.symbols.values())

    print("PAPER REPLAY (Scheinbörse, kein Netz)")
    print("\n\n".join(transcript))
    captured = capsys.readouterr().out
    assert "protected=yes" in captured
    assert "protected=NO" not in captured
    print(captured, file=sys.stderr, end="")


def test_new_stop_is_placed_before_the_unlocked_old_one(tmp_path):
    """Der alte Auftrag bindet nichts (Volumen 0). Der neue Stop muss vorher stehen."""
    fake, m = _bought(tmp_path)
    r = runner(tmp_path, fake, m)
    for sym in ("XBTEUR", "ETHEUR"):
        stop = next(o for o in fake.open("stop-loss") if o.pair == sym)
        stop.volume = 0.0
        r.state.symbols[sym].stop_units = 0.0
    fake.events.clear()
    m.day(150)
    r.run_once()
    assert [e[0] for e in fake.events[:2]] == ["add", "cancel"]
    assert len(fake.open("stop-loss")) == 2
    assert next(o.price for o in fake.open("stop-loss") if o.pair == "XBTEUR") == pytest.approx(
        120.0
    )


def test_rejected_replacement_restores_the_old_stop(tmp_path):
    logs: list[str] = []
    fake, m = _bought(tmp_path)
    old = {o.pair: o.price for o in fake.open("stop-loss")}
    r = runner(tmp_path, fake, m)
    r.log = logs.append
    fake.reject_stops = 2  # beide Versuche des neuen BTC-Stops, dann Wiederherstellung
    fake.events.clear()
    m.day(150)
    r.run_once()
    assert [e[0] for e in fake.events[:4]] == ["cancel", "reject", "reject", "add"]
    (btc,) = [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert btc.price == pytest.approx(old["XBTEUR"])
    assert r.state.symbols["XBTEUR"].stop_txid == btc.txid
    eth = next(o for o in fake.open("stop-loss") if o.pair == "ETHEUR")
    assert eth.price == pytest.approx(12.0)  # 15 × 0,8, der Zähler war schon leer
    assert any("wiederhergestellt" in line for line in logs)


def test_failed_restore_leaves_no_pretend_stop_and_alarms(tmp_path):
    logs: list[str] = []
    fake, m = _bought(tmp_path)
    r = runner(tmp_path, fake, m)
    r.log = logs.append
    fake.reject_stops = 20  # neuer Stop und Wiederherstellung, beide Paare
    m.day(150)
    r.run_once()
    assert not fake.open("stop-loss")
    assert r.state.symbols["XBTEUR"].stop_txid is None
    assert r.state.symbols["ETHEUR"].stop_txid is None
    assert sum("POSITION OHNE STOP" in line for line in logs) == 2


def test_invalid_new_stop_does_not_cancel_the_live_one(tmp_path):
    logs: list[str] = []
    fake, m = _bought(tmp_path)
    r = runner(tmp_path, fake, m)
    r.log = logs.append
    for sym in ("XBTEUR", "ETHEUR"):
        stop = next(o for o in fake.open("stop-loss") if o.pair == sym)
        stop.volume = 0.0  # frei genug, um den neuen Stop zuerst zu versuchen
        r.state.symbols[sym].stop_units = 0.0
    ids = {o.pair: o.txid for o in fake.open("stop-loss")}
    fake.reject_stops = 1
    fake.reject_message = "EOrder:Invalid price"
    fake.events.clear()
    m.day(150)
    r.run_once()
    assert ("cancel", ids["XBTEUR"]) not in fake.events
    assert fake.orders[ids["XBTEUR"]].status == OrderStatus.OPEN
    assert r.state.symbols["XBTEUR"].stop_txid == ids["XBTEUR"]
    assert any("bleibt aktiv" in line for line in logs)


def test_unknown_outcome_keeps_the_stop_the_exchange_already_accepted(tmp_path):
    fake, m = _bought(tmp_path)
    r = runner(tmp_path, fake, m)
    fake.unknown_next = True  # Order entsteht, die Antwort geht verloren
    m.day(150)
    r.run_once()
    btc = [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert len(btc) == 1
    assert btc[0].price == pytest.approx(120.0)
    assert r.state.symbols["XBTEUR"].stop_txid == btc[0].txid


def test_cancel_that_leaves_the_stop_open_does_not_send_another(tmp_path):
    logs: list[str] = []
    fake, m = _bought(tmp_path)
    r = runner(tmp_path, fake, m)
    r.log = logs.append
    ids = {o.txid for o in fake.open("stop-loss")}

    def refuse(txid):
        return None

    fake.cancel_order = refuse
    fake.events.clear()
    m.day(150)
    r.run_once()
    assert {o.txid for o in fake.open("stop-loss")} == ids
    assert not [e for e in fake.events if e[0] in {"add", "reject"}]
    assert any("ließ sich nicht lösen" in line for line in logs)


def test_paper_replay_trailing_stop_survives_a_rejected_replace(tmp_path, capsys):
    logs: list[str] = []
    fake, m = FakeKraken(), Market()
    r = runner(tmp_path, fake, m)
    r.log = logs.append
    transcript: list[str] = []

    def step(title: str) -> None:
        r.run_once()
        lines = [title]
        for sym in r.cfg.symbols:
            st = r.state.symbols[sym]
            stops = [o for o in fake.open("stop-loss") if o.pair == sym]
            protected = st.units <= 1e-12 or bool(stops)
            stop_txt = ",".join(f"{o.price:.2f}" for o in stops) or "–"
            line = (
                f"{sym} close={float(r.closes(sym).iloc[-1]):.2f} units={st.units:.8f} "
                f"stop={stop_txt} protected={'yes' if protected else 'NO'}"
            )
            assert protected, line
            lines.append("  " + line)
        transcript.append("\n".join(lines))

    m.day(110)
    r.run_once()
    for order in list(fake.open("limit")):
        fake.fill(order.txid)
    step("nach Kauf und erstem Stop")
    m.day(130)
    step("höherer Schluss, Stop nachgezogen")
    before = {o.pair: o.price for o in fake.open("stop-loss")}
    fake.reject_stops = 2
    m.day(160)
    step("neuer BTC-Stop abgelehnt – alter Preis wiederhergestellt")
    btc = next(o for o in fake.open("stop-loss") if o.pair == "XBTEUR")
    assert btc.price == pytest.approx(before["XBTEUR"])
    assert any("wiederhergestellt" in line for line in logs)
    fake.reject_stops = 0
    m.day(180)
    step("nächster Tag zieht beide Stops nach")
    assert next(o.price for o in fake.open("stop-loss") if o.pair == "XBTEUR") == pytest.approx(
        144.0
    )
    print("PAPER REPLAY (Scheinbörse, kein Netz)")
    print("\n\n".join(transcript))
    captured = capsys.readouterr().out
    assert "protected=yes" in captured and "protected=NO" not in captured
    print(captured, file=sys.stderr, end="")


def test_lost_state_file_never_buys_twice_while_bot_orders_are_open(tmp_path):
    fake, m = _bought(tmp_path)
    fake.bal["ZEUR"] += 20.0  # Nutzer zahlt nach
    (tmp_path / "state.json").unlink()  # Zustand verloren (Festplatte, versehentlich gelöscht)
    with pytest.raises(LiveSafetyError, match="Zustand"):
        runner(tmp_path, fake, m).run_once()
    assert not [o for o in fake.open() if o.side == OrderSide.BUY]


def _dd_cfg(**overrides):
    params = dict(
        budget_eur=20.0,
        invest_fraction=1.0,
        sma=3,
        stop_frac=0.20,
        drawdown_enabled=True,
        drawdown_fraction=0.45,
    )
    params.update(overrides)
    return LiveTrendConfig(**params)


def _btc_with_drawdown(tmp_path, *, enabled: bool = True):
    """BTC gekauft und mit Kraken-Stop, ETH noch unter dem Schnitt. Drawdown-Sperre je nach Flag."""
    logs: list[str] = []
    clock = {"t": dt.datetime(2026, 10, 19, 10, 0, tzinfo=dt.UTC)}
    cfg = _dd_cfg(drawdown_enabled=enabled)
    fake, market = FakeKraken(), Market()
    market.day(110, 8)
    run = runner(tmp_path, fake, market, cfg=cfg, now=lambda: clock["t"], log=logs.append)
    run.snapshot_path = tmp_path / "snapshot.json"
    run.run_once()
    for order in list(fake.open("limit")):
        if order.pair == "XBTEUR":
            fake.fill(order.txid)
    run.run_once()
    assert run.state.symbols["XBTEUR"].units > 0
    assert [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    return fake, market, run, clock, logs


def _price_for_drawdown(fake, run, remaining: float) -> float:
    """Setzt den BTC-Kurs so, dass die Equity etwa ``remaining`` mal dem Höchststand ist."""
    assert run.drawdown is not None
    target = run.drawdown.peak_equity * remaining
    lo, hi = 0.5, max(fake.price["XBTEUR"], 1.0)
    for _ in range(40):
        mid = (lo + hi) / 2
        fake.price["XBTEUR"] = mid
        if run._account_equity() > target:
            hi = mid
        else:
            lo = mid
    fake.price["XBTEUR"] = lo
    return lo


def test_drawdown_config_is_off_by_default_and_cannot_loosen_past_45_percent():
    cfg = LiveTrendConfig()
    assert cfg.drawdown_enabled is False
    assert cfg.drawdown_fraction == pytest.approx(0.45)
    assert cfg.invest_fraction == 1.0
    assert cfg.stop_frac == pytest.approx(0.20)
    assert cfg.budget_eur == 20.0
    LiveTrendConfig(drawdown_fraction=0.45)
    LiveTrendConfig(drawdown_fraction=0.30)
    with pytest.raises(LiveSafetyError, match="0.45"):
        LiveTrendConfig(drawdown_fraction=0.46)
    with pytest.raises(LiveSafetyError, match="20"):
        LiveTrendConfig(budget_eur=20.01, drawdown_enabled=True)


def test_drawdown_hit_blocks_buys_but_stop_and_sell_still_work(tmp_path):
    fake, market, run, _clock, logs = _btc_with_drawdown(tmp_path)
    assert run.drawdown is not None and not run.drawdown.halted
    assert run.cfg.invest_fraction == 1.0
    stop = next(o for o in fake.open("stop-loss") if o.pair == "XBTEUR")
    _price_for_drawdown(fake, run, 0.50)  # 50 % unter dem Höchststand, Stop wird nicht ausgeführt
    run.run_once()
    assert run.drawdown.halted
    assert stop.status == OrderStatus.OPEN
    assert (tmp_path / "DRAWDOWN_HALT").exists()
    assert not (tmp_path / "KILL").exists()
    assert any(line.startswith("[DRAWDOWN]") and "Kraken-Stops" in line for line in logs)
    snap = json.loads((tmp_path / "snapshot.json").read_text())
    assert snap["drawdown"]["enabled"] is True and snap["drawdown"]["halted"] is True

    market.day(110, 14)  # ETH will kaufen, BTC bleibt über dem Schnitt
    run.run_once()
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR"]
    assert [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert any("ETHEUR: keine neuen Käufe" in line for line in logs)
    run.buy_now()
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR"]

    market.day(80, 14)  # BTC unter SMA → Verkauf, ETH-Kauf bleibt gesperrt
    run.run_once()
    assert [o for o in fake.orders.values() if o.pair == "XBTEUR" and o.side == OrderSide.SELL]
    assert not [o for o in fake.open() if o.pair == "ETHEUR" and o.side == OrderSide.BUY]


def test_drawdown_not_hit_still_buys(tmp_path):
    fake, market, run, _clock, logs = _btc_with_drawdown(tmp_path)
    _price_for_drawdown(fake, run, 0.70)  # 30 % unter dem Höchststand
    run.run_once()
    assert run.drawdown is not None and not run.drawdown.halted
    assert not (tmp_path / "DRAWDOWN_HALT").exists()
    assert not any("Keine neuen Käufe, bis die Sperre" in line for line in logs)
    market.day(110, 14)
    run.run_once()
    assert [o for o in fake.open("limit") if o.pair == "ETHEUR" and o.side == OrderSide.BUY]


def test_drawdown_peak_tracks_and_persists_across_restart(tmp_path):
    fake, market, run, clock, logs = _btc_with_drawdown(tmp_path)
    assert run.drawdown is not None
    before = run.drawdown.peak_equity
    fake.price["XBTEUR"] = 300.0
    run.run_once()
    assert run.drawdown.peak_equity > before + 5
    assert not run.drawdown.halted
    peak = run.drawdown.peak_equity

    again = runner(tmp_path, fake, market, cfg=_dd_cfg(), now=lambda: clock["t"], log=logs.append)
    assert again.drawdown is not None
    assert again.drawdown.peak_equity == pytest.approx(peak)
    assert not again.drawdown.halted
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["drawdown"]["peak_equity"] == pytest.approx(peak)

    # 50 % unter dem höheren Höchststand: ohne den Anstieg wäre das noch keine 45-%-Sperre.
    _price_for_drawdown(fake, again, 0.50)
    equity = again._account_equity()
    assert equity > before * 0.55
    again.run_once()
    assert again.drawdown.halted
    assert again.drawdown.peak_equity == pytest.approx(peak)


def test_drawdown_flag_off_does_not_change_buys_or_state(tmp_path):
    fake, market, run, _clock, logs = _btc_with_drawdown(tmp_path, enabled=False)
    assert run.drawdown is None
    _price_for_drawdown_without_guard(fake, run)
    run.run_once()
    assert not any(line.startswith("[DRAWDOWN]") for line in logs)
    assert not (tmp_path / "DRAWDOWN_HALT").exists()
    raw = json.loads((tmp_path / "state.json").read_text())
    assert "drawdown" not in raw
    snap = json.loads((tmp_path / "snapshot.json").read_text())
    assert snap["drawdown"] == {"enabled": False, "limit": 0.45}
    market.day(110, 14)
    run.run_once()
    assert [o for o in fake.open("limit") if o.pair == "ETHEUR"]


def _price_for_drawdown_without_guard(fake, run):
    """Kurs, der bei eingeschalteter Sperre die 45 % reißen würde (halbes Budget in BTC)."""
    fake.price["XBTEUR"] = 5.0
    units = run.state.symbols["XBTEUR"].units
    assert units * 5.0 < 20 * 0.45


def test_drawdown_manual_reset_by_deleting_the_file_and_by_cli(tmp_path, monkeypatch, capsys):
    from tradikg_bot.cli import main

    fake, market, run, clock, logs = _btc_with_drawdown(tmp_path)
    _price_for_drawdown(fake, run, 0.50)
    run.run_once()
    assert run.drawdown is not None and run.drawdown.halted
    assert not (tmp_path / "KILL").exists()  # die Sperre legt keinen Kill-Switch an
    (tmp_path / "DRAWDOWN_HALT").unlink()  # Hinweisdatei, kein Schalter
    market.day(110, 14)
    run.run_once()
    assert run.drawdown.halted
    assert not any("DRAWDOWN_HALT fehlt" in line for line in logs)
    assert not any("drawdown-reset gelöst" in line for line in logs)
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR"]
    from tradikg_bot.live.trend_live import release_drawdown_halt

    release_drawdown_halt(tmp_path / "state.json")
    market.day(112, 16)
    run.run_once()
    assert not run.drawdown.halted
    assert any("drawdown-reset gelöst" in line for line in logs)
    assert [o for o in fake.open("limit") if o.pair == "ETHEUR"]
    assert not (tmp_path / "KILL").exists()

    # Kalter Start: CLI setzt die Lösung in die Zustandsdatei, der Kill-Switch bleibt.
    home = tmp_path / "home"
    live = home / "live"
    live.mkdir(parents=True)
    state_path = live / "trend_state.json"
    state_path.write_text(
        json.dumps(
            {
                "symbols": {},
                "drawdown": {
                    "limit": 0.45,
                    "peak_equity": 40.0,
                    "halted": True,
                    "rebase": False,
                    "peak_at": "2019-01-01T00:00:00+00:00",
                    "halted_at": "2019-06-01T00:00:00+00:00",
                },
            }
        )
    )
    (live / "DRAWDOWN_HALT").write_text("sperre\n")
    (live / "KILL_SWITCH").write_text("an")
    monkeypatch.setenv("TRADIKG_HOME", str(home))
    assert main(["live", "status"]) == 0
    assert "Drawdown-Sperre: AKTIV" in capsys.readouterr().out
    assert main(["live", "drawdown-reset"]) == 0
    assert "nichts verkauft" in capsys.readouterr().out
    assert not (live / "DRAWDOWN_HALT").exists()
    assert (live / "KILL_SWITCH").read_text() == "an"
    raw = json.loads(state_path.read_text())
    assert raw["drawdown"]["halted"] is False
    assert raw["drawdown"]["rebase"] is True
    assert raw["drawdown"]["reset_id"] == 1
    assert raw["drawdown"]["peak_equity"] == pytest.approx(40.0)
    assert "symbols" in raw


def test_buy_now_trip_on_first_coin_blocks_the_second(tmp_path):
    """Halt auf BTC darf ETH nicht freigeben, auch wenn die Datei noch halted:false hat."""
    fake, market, run, _clock, logs = _btc_with_drawdown(tmp_path)
    assert run.drawdown is not None
    peak = run.drawdown.peak_equity
    on_disk = json.loads((tmp_path / "state.json").read_text())["drawdown"]
    assert on_disk["halted"] is False
    market.day(110, 14)  # ETH über dem Schnitt, würde ohne Sperre sofort kaufen
    _price_for_drawdown(fake, run, 0.50)
    run.buy_now()
    assert run.drawdown.halted
    assert run.drawdown.peak_equity == pytest.approx(peak)
    assert not any("drawdown-reset gelöst" in line for line in logs)
    assert any("ETHEUR: keine neuen Käufe" in line for line in logs)
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR"]
    saved = json.loads((tmp_path / "state.json").read_text())["drawdown"]
    assert saved["halted"] is True
    assert saved["peak_equity"] == pytest.approx(peak)


def test_dry_run_keeps_drawdown_halt_across_cycles(tmp_path):
    fake, market, run, clock, logs = _btc_with_drawdown(tmp_path)
    assert run.drawdown is not None
    peak = run.drawdown.peak_equity
    market.day(110, 14)
    dry = runner(
        tmp_path,
        fake,
        market,
        dry_run=True,
        cfg=_dd_cfg(),
        now=lambda: clock["t"],
        log=logs.append,
    )
    assert dry.drawdown is not None and not dry.drawdown.halted
    _price_for_drawdown(fake, dry, 0.50)
    before = len(fake.validated)
    dry.run_once()
    assert dry.drawdown.halted
    assert dry.drawdown.peak_equity == pytest.approx(peak)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["drawdown"]["halted"] is True
    assert "XBTEUR" in raw["symbols"]
    dry.run_once()
    assert dry.drawdown.halted
    assert dry.drawdown.peak_equity == pytest.approx(peak)
    eth_buys = [
        p for p in fake.validated[before:] if p.get("pair") == "ETHEUR" and p.get("type") == "buy"
    ]
    assert not eth_buys
    again = runner(
        tmp_path,
        fake,
        market,
        dry_run=True,
        cfg=_dd_cfg(),
        now=lambda: clock["t"],
        log=logs.append,
    )
    assert again.drawdown is not None and again.drawdown.halted
    assert again.drawdown.peak_equity == pytest.approx(peak)


def test_drawdown_reset_marker_releases_exactly_once(tmp_path):
    from tradikg_bot.live.trend_live import release_drawdown_halt

    fake, market, run, _clock, logs = _btc_with_drawdown(tmp_path)
    assert run.drawdown is not None
    peak = run.drawdown.peak_equity
    _price_for_drawdown(fake, run, 0.50)
    run.run_once()
    assert run.drawdown.halted
    assert run.drawdown.peak_equity == pytest.approx(peak)

    release_drawdown_halt(tmp_path / "state.json")
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["drawdown"]["reset_id"] == 1
    assert raw["drawdown"]["halted"] is False
    run.run_once()
    assert not run.drawdown.halted
    assert run.drawdown.reset_id == 1
    rebased = run.drawdown.peak_equity
    assert rebased < peak * 0.6
    assert sum("drawdown-reset gelöst" in line for line in logs) == 1

    # Dieselbe Marke setzt den Höchststand nicht noch einmal, auch nach einem neuen Riss.
    fake.price["XBTEUR"] = 100.0
    run.run_once()
    assert not run.drawdown.halted
    climbed = run.drawdown.peak_equity
    assert climbed > rebased + 5
    _price_for_drawdown(fake, run, 0.50)
    run.run_once()
    assert run.drawdown.halted
    held = run.drawdown.peak_equity
    assert held == pytest.approx(climbed)
    disk = json.loads((tmp_path / "state.json").read_text())
    disk["drawdown"]["halted"] = False
    disk["drawdown"]["rebase"] = True
    (tmp_path / "state.json").write_text(json.dumps(disk))
    market.day(110, 14)
    run.run_once()
    assert run.drawdown.halted
    assert run.drawdown.peak_equity == pytest.approx(held)
    assert run.drawdown.reset_id == 1
    assert sum("drawdown-reset gelöst" in line for line in logs) == 1
    assert not [o for o in fake.orders.values() if o.pair == "ETHEUR"]

    release_drawdown_halt(tmp_path / "state.json")
    assert json.loads((tmp_path / "state.json").read_text())["drawdown"]["reset_id"] == 2
    run.run_once()
    assert not run.drawdown.halted
    assert run.drawdown.reset_id == 2
    assert sum("drawdown-reset gelöst" in line for line in logs) == 2


def test_drawdown_uses_bot_slice_not_the_whole_account(tmp_path):
    """Konto 100 €, Budget 20 €: −45 % der Scheibe sperrt, −45 % vom Konto wäre unerreichbar."""
    logs: list[str] = []
    clock = {"t": dt.datetime(2026, 10, 19, 10, 0, tzinfo=dt.UTC)}
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 8)
    run = runner(tmp_path, fake, market, cfg=_dd_cfg(), now=lambda: clock["t"], log=logs.append)
    run.run_once()
    for order in list(fake.open("limit")):
        if order.pair == "XBTEUR":
            fake.fill(order.txid)
    run.run_once()
    assert run.drawdown is not None
    assert run.drawdown.peak_equity == pytest.approx(20.0, abs=0.5)
    assert run.state.bot_cash_eur == pytest.approx(10.0, abs=0.3)
    _price_for_drawdown(fake, run, 0.50)
    units = run.state.symbols["XBTEUR"].units
    account = fake.bal["ZEUR"] + units * fake.price["XBTEUR"] * 0.999
    run.run_once()
    assert run.drawdown.halted
    assert run.drawdown.peak_equity == pytest.approx(20.0, abs=0.5)
    assert account > 55  # 45 % vom 100-€-Konto wären erst bei 55 €
    peak = run.drawdown.peak_equity
    cash = run.state.bot_cash_eur
    fake.bal["ZEUR"] += 80
    fake.bal["XXBT"] += 0.02
    fake.bal["XETH"] += 0.4
    run.run_once()
    assert run.drawdown.halted
    assert run.drawdown.peak_equity == pytest.approx(peak)
    assert run.state.bot_cash_eur == pytest.approx(cash)
    assert run.state.symbols["XBTEUR"].units < fake.bal["XXBT"]


def test_drawdown_does_not_replace_withdrawal_abort(tmp_path):
    fake, market = FakeKraken(), Market()
    market.day(110, 14)
    fake.withdraw = True
    with pytest.raises(LiveSafetyError, match="Auszahlungsrechte"):
        runner(tmp_path, fake, market, cfg=_dd_cfg()).run_once()
    assert not (tmp_path / "DRAWDOWN_HALT").exists()


def test_dust_remainder_stays_tracked_and_bot_stop_below_minimum_is_cancelled(tmp_path):
    """Unter 0,50 € und unter dem Börsenminimum: Menge bleibt, Bot-Stop wird gelöst, keiner neu."""
    fake, m = _bought(tmp_path)
    r = runner(tmp_path, fake, m)
    st = r.state.symbols["XBTEUR"]
    bot_stop = next(o for o in fake.open("stop-loss") if o.pair == "XBTEUR")
    dust = 0.00001  # unter Mindestmenge 0,00005 und unter 0,50 €
    st.units, st.entry_price, st.peak = dust, 100.0, 100.0
    fake.bal["XXBT"] = bot_stop.volume + 0.01  # Platz für den fremden Stop, Menge bleibt Staub
    manual = fake.add_order(
        {
            "pair": "XBTEUR",
            "userref": 1,
            "type": "sell",
            "ordertype": "stop-loss",
            "price": "50.0",
            "volume": "0.01",
        }
    )
    r.run_once()  # selber Tag: keine neue Tagesentscheidung
    assert st.units == pytest.approx(dust)
    assert st.entry_price == pytest.approx(100.0)
    assert st.peak == pytest.approx(100.0)
    assert fake.orders[bot_stop.txid].status == OrderStatus.CANCELED
    assert st.stop_txid is None
    assert fake.orders[manual].status == OrderStatus.OPEN  # ohne Bot-userref
    assert not [
        o
        for o in fake.open("stop-loss")
        if o.pair == "XBTEUR" and o.client_ref and o.client_ref % 10 == 3
    ]


def test_forgotten_bot_stop_on_empty_state_is_cancelled(tmp_path):
    """Alter Zustand hat die Menge gelöscht, der Bot-Stop liegt noch bei Kraken."""
    fake, m = _bought(tmp_path)
    r = runner(tmp_path, fake, m)
    st = r.state.symbols["XBTEUR"]
    orphan = next(o for o in fake.open("stop-loss") if o.pair == "XBTEUR")
    st.units, st.entry_price, st.peak = 0.0, 0.0, 0.0
    st.stop_txid, st.stop_price, st.stop_units = None, 0.0, 0.0
    r.run_once()
    assert fake.orders[orphan.txid].status == OrderStatus.CANCELED
    assert not [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]


def test_remainder_under_dust_but_above_exchange_minimum_keeps_a_stop(tmp_path):
    fake, m = _bought(tmp_path)
    r = runner(tmp_path, fake, m)
    cheap = PairRules("XXBTZEUR", "XBTEUR", "XXBT", "ZEUR", 8, 1, 0.1, 1e-8, 0.01)
    fake.pair_rules = lambda sym: cheap if sym == "XBTEUR" else RULES[sym]
    st = r.state.symbols["XBTEUR"]
    st.units, st.entry_price, st.peak = 0.004, 100.0, 100.0  # 0,40 € < 0,50 € Staubgrenze
    fake.bal["XXBT"] = 0.004
    r.run_once()
    assert st.units == pytest.approx(0.004)
    assert st.entry_price == pytest.approx(100.0)
    (stop,) = [o for o in fake.open("stop-loss") if o.pair == "XBTEUR"]
    assert stop.volume == pytest.approx(0.004)
    assert st.stop_txid == stop.txid


def test_buy_now_userref_does_not_collide_with_daily_fallback(tmp_path):
    fake, m = FakeKraken(), Market()
    m.day(110)
    r = runner(tmp_path, fake, m)
    day = str(m("XBTEUR").index[-1].date())
    assert r._ref(day, "XBTEUR", "buy_now") != r._ref(day, "XBTEUR", "buy_fallback")
    order = {"type": "buy", "ordertype": "limit", "price": "100.0", "volume": "0.05"}
    first = r._submit("XBTEUR", "buy_fallback", day, dict(order))
    again = r._submit("XBTEUR", "buy_fallback", day, dict(order))
    assert again == first  # echter Doppelgänger wird übernommen
    assert len(fake.orders) == 1
    now = r._submit("XBTEUR", "buy_now", day, dict(order))
    assert now != first
    assert fake.refs[first] == r._ref(day, "XBTEUR", "buy_fallback")
    assert fake.refs[now] == r._ref(day, "XBTEUR", "buy_now")

    live, market = FakeKraken(), Market()
    market.day(110)
    bot = runner(tmp_path / "live2", live, market)
    (tmp_path / "live2").mkdir()
    bot.run_once()
    bot.buy_now()
    today = bot._now().date().isoformat()
    placed = [o for o in live.orders.values() if "XBT" in o.pair and live.refs[o.txid] % 10 == 6]
    assert placed
    assert all(o.client_ref == bot._ref(today, "XBTEUR", "buy_now") for o in placed)
    before = len(live.orders)
    fallback = bot._submit("XBTEUR", "buy_fallback", today, dict(order))
    assert len(live.orders) == before + 1
    assert live.refs[fallback] == bot._ref(today, "XBTEUR", "buy_fallback")
    assert bot._submit("XBTEUR", "buy_now", today, dict(order)) == placed[0].txid
