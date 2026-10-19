"""Übergabe Mac → Linux gegen eine simulierte Börse (kein Netz, keine echten Schlüssel)."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest
from test_trend_live import FakeKraken, Market, runner

from tradikg_bot.live.cutover import find_other_instance, read_state_file, state_from_json
from tradikg_bot.live.models import ExchangeOrder, OrderSide, OrderStatus
from tradikg_bot.live.trend_live import KIND, LiveSafetyError, LiveTrendRunner


def _stop(fake: FakeKraken, runner_: LiveTrendRunner, sym: str, volume: float, price: float) -> str:
    day = str(runner_.closes(sym).index[-1].date())
    ref = runner_._ref(day, sym, "stop")
    txid = f"STOP-{sym}"
    fake.orders[txid] = ExchangeOrder(
        txid,
        ref,
        sym,
        OrderSide.SELL,
        "stop-loss",
        OrderStatus.OPEN,
        volume,
        price=price,
        opened_at=time.time() - 86_400,
    )
    fake.refs[txid] = ref
    base = "XXBT" if sym == "XBTEUR" else "XETH"
    fake.bal[base] = max(fake.bal[base], volume)
    return txid


def test_import_rejects_a_broken_file_and_does_not_trust_a_larger_claim(tmp_path):
    with pytest.raises(LiveSafetyError, match="JSON"):
        read_state_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(LiveSafetyError, match="JSON"):
        read_state_file(bad)
    with pytest.raises(LiveSafetyError, match="negativ"):
        state_from_json({"symbols": {"XBTEUR": {"units": -1, "entry_price": 1}}})

    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110)
    bot = runner(tmp_path, fake, market)
    txid = _stop(fake, bot, "XBTEUR", volume=0.05, price=80.0)
    _stop(fake, bot, "ETHEUR", volume=0.5, price=8.0)
    claimed = {
        "symbols": {
            "XBTEUR": {
                "units": 1.0,
                "entry_price": 100.0,
                "peak": 100.0,
                "stop_txid": txid,
                "stop_price": 80.0,
                "stop_units": 1.0,
            }
        }
    }
    path = tmp_path / "from-mac.json"
    path.write_text(json.dumps(claimed))
    bot.state = read_state_file(path)
    bot.state_path.write_text(path.read_text())
    notes: list[str] = []
    bot.log = notes.append
    bot.run_once()
    st = bot.state.symbols["XBTEUR"]
    assert st.units == pytest.approx(0.05)
    assert st.stop_txid == txid
    assert st.units < 1.0
    assert not [o for o in fake.orders.values() if o.side == OrderSide.BUY]
    assert len(fake.open("stop-loss")) == 2
    assert any("verworfen" in line or "Datei hatte" in line for line in notes)


def test_reconstruct_from_userref_ignores_untagged_balance(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110)
    bot = runner(tmp_path, fake, market)
    txid = _stop(fake, bot, "XBTEUR", volume=0.05, price=80.0)
    _stop(fake, bot, "ETHEUR", volume=0.5, price=8.0)
    fake.bal["XXBT"] = 1.0  # Konto hält viel mehr, als der Bot-Stop deckt
    assert not (tmp_path / "state.json").exists()
    bot.run_once()
    st = bot.state.symbols["XBTEUR"]
    assert st.units == pytest.approx(0.05)
    assert st.stop_txid == txid
    assert st.units * st.entry_price <= 20.0
    assert not [o for o in fake.orders.values() if o.side == OrderSide.BUY]
    assert txid in {o.txid for o in fake.open("stop-loss")}
    assert len(fake.open("stop-loss")) == 2


def test_existing_stop_is_adopted_and_not_duplicated(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110)
    bot = runner(tmp_path, fake, market)
    btc = _stop(fake, bot, "XBTEUR", volume=0.05, price=80.0)
    eth = _stop(fake, bot, "ETHEUR", volume=0.5, price=8.0)
    before = len(fake.orders)
    bot.run_once()
    assert len(fake.orders) == before
    assert {o.txid for o in fake.open("stop-loss")} == {btc, eth}
    assert not any(o.side == OrderSide.BUY for o in fake.orders.values())


def test_no_second_buy_while_a_bot_stop_is_open(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110)
    bot = runner(tmp_path, fake, market)
    _stop(fake, bot, "XBTEUR", volume=0.05, price=80.0)
    _stop(fake, bot, "ETHEUR", volume=0.5, price=8.0)
    market.day(120)  # neuer Tag: ohne übernommene Menge würde die Regel kaufen
    bot.run_once()
    assert not [o for o in fake.orders.values() if o.side == OrderSide.BUY]
    assert bot.state.symbols["XBTEUR"].units == pytest.approx(0.05)


def test_over_slice_bot_position_refuses_before_any_order(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110)
    bot = runner(tmp_path, fake, market)
    _stop(fake, bot, "XBTEUR", volume=0.5, price=80.0)  # 0,5 * 100 € geschätzt
    before = len(fake.orders)
    with pytest.raises(LiveSafetyError, match="Scheibe"):
        bot.run_once()
    assert len(fake.orders) == before


def test_recent_bot_order_refuses_live_start(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110)
    bot = runner(tmp_path, fake, market)
    txid = _stop(fake, bot, "XBTEUR", volume=0.05, price=80.0)
    fake.orders[txid].opened_at = time.time()
    before = len(fake.orders)
    with pytest.raises(LiveSafetyError, match="zweite Instanz"):
        bot.run_once()
    assert len(fake.orders) == before
    probe = runner(tmp_path, fake, market, dry_run=True)
    lines: list[str] = []
    probe.log = lines.append
    probe.run_once()
    assert any("ZWEITE INSTANZ" in line for line in lines)
    assert not [o for o in fake.orders.values() if o.side == OrderSide.BUY]


def test_live_pidfile_refuses_second_instance(tmp_path):
    fake, market = FakeKraken(eur=20.0), Market()
    market.day(110)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        (tmp_path / "live.pid").write_text(str(proc.pid))
        bot = runner(tmp_path, fake, market)
        with pytest.raises(LiveSafetyError, match="PID-Datei"):
            bot.run_once()
        assert not fake.orders
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_foreign_lock_is_a_second_instance(tmp_path):
    code = (
        "import sys, time\n"
        "from tradikg_bot.service.ops import InstanceLock\n"
        "lock = InstanceLock(sys.argv[1])\n"
        "lock.acquire()\n"
        "time.sleep(60)\n"
    )
    lock = tmp_path / "live.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(lock)], stderr=subprocess.PIPE, text=True
    )
    try:
        held = False
        for _ in range(100):
            if lock.exists() and lock.read_text().strip().isdigit():
                held = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert held, proc.stderr.read() if proc.stderr else "Sperre nicht gehalten"
        reasons = find_other_instance(tmp_path, [], ("XBTEUR", "ETHEUR"))
        assert any("Sperre" in reason for reason in reasons)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_import_command_writes_state_without_credentials(tmp_path, monkeypatch, capsys):
    import tradikg_bot.live.kraken.credentials as creds_mod
    from tradikg_bot.cli import main

    src = tmp_path / "mac.json"
    src.write_text(
        json.dumps({"symbols": {"XBTEUR": {"units": 0.01, "entry_price": 100.0, "peak": 100.0}}})
    )
    monkeypatch.setenv("TRADIKG_HOME", str(tmp_path / "home"))

    def spy(*_a, **_k):
        raise AssertionError("Zugangsdaten gelesen")

    monkeypatch.setattr(creds_mod, "load_credentials", spy)
    assert main(["live", "import", str(src)]) == 0
    dest = tmp_path / "home" / "live" / "trend_state.json"
    assert json.loads(dest.read_text())["symbols"]["XBTEUR"]["units"] == 0.01
    assert "live check" in capsys.readouterr().out
    assert main(["live", "import", str(src)]) == 2


def test_old_stop_is_not_a_second_instance(tmp_path):
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110)
    bot = runner(tmp_path, fake, market)
    _stop(fake, bot, "XBTEUR", volume=0.05, price=80.0)
    bot.run_once()  # opened_at liegt einen Tag zurück
    assert bot.state.symbols["XBTEUR"].stop_txid
    assert KIND["stop"] == 3
