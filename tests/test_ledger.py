from __future__ import annotations

import datetime as dt
import threading

import pytest

from tradikg_bot.trading.ledger import CapitalLedger

T0 = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def ledger(tmp_path):
    lg = CapitalLedger(tmp_path / "l.sqlite", initial_capital=100.0)
    yield lg
    lg.close()


def test_profit_is_split_33_33_34_without_losses(ledger):
    e = ledger.book("t1", 10.0, T0)
    assert (e.to_trading, e.to_payout, e.to_longterm) == pytest.approx((3.3, 3.3, 3.4))
    b = ledger.balances()
    assert b.trading_balance == pytest.approx(103.3)
    assert b.payout_reserve == pytest.approx(3.3) and b.longterm_portfolio == pytest.approx(3.4)


def test_losses_are_recovered_before_any_distribution(ledger):
    ledger.book("t1", -5.0, T0)
    assert ledger.balances().trading_balance == pytest.approx(95.0)
    assert ledger.balances().unrecovered_losses == pytest.approx(5.0)
    e = ledger.book("t2", 3.0, T0)  # deckt nur einen Teil des Verlusts
    assert e.recovered == pytest.approx(3.0) and e.to_payout == 0 and e.to_longterm == 0
    e = ledger.book("t3", 4.0, T0)  # 2 EUR Aufholung, 2 EUR verteilbar
    assert e.recovered == pytest.approx(2.0)
    assert e.to_payout == pytest.approx(0.66) and e.to_longterm == pytest.approx(0.68)
    b = ledger.balances()
    assert b.unrecovered_losses == 0
    assert b.trading_balance == pytest.approx(100.0 + 2 * 0.33)
    assert b.high_water_mark == pytest.approx(b.trading_balance)


def test_loss_series_and_buckets_are_never_touched_by_losses(ledger):
    ledger.book("w", 10.0, T0)
    for i in range(5):
        ledger.book(f"l{i}", -2.0, T0)
    b = ledger.balances()
    assert b.payout_reserve == pytest.approx(3.3) and b.longterm_portfolio == pytest.approx(3.4)
    assert b.trading_balance == pytest.approx(103.3 - 10.0)
    assert b.unrecovered_losses == pytest.approx(10.0)


def test_duplicate_report_books_only_once(ledger):
    ledger.book("t1", 10.0, T0)
    ledger.book("t1", 10.0, T0)
    ledger.book("t1", 99.0, T0)  # gleiche Trade-ID, andere Meldung -> ignoriert
    assert len(ledger.entries()) == 1
    assert ledger.balances().total_net_pnl == pytest.approx(10.0)


def test_concurrent_bookings_of_same_trade_are_idempotent(tmp_path):
    path = tmp_path / "c.sqlite"
    CapitalLedger(path, 100.0).close()

    def worker():
        lg = CapitalLedger(path, 100.0)
        lg.book("same", 5.0, T0)
        lg.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lg = CapitalLedger(path, 100.0)
    assert len(lg.entries()) == 1


def test_fee_correction_is_its_own_entry(ledger):
    ledger.book("t1", 10.0, T0)
    ledger.adjust("t1#fee-fix", -0.5, T0, "Gebührenkorrektur")
    b = ledger.balances()
    assert b.total_net_pnl == pytest.approx(9.5)
    assert b.unrecovered_losses == pytest.approx(0.5)  # Korrektur wird wie Verlust aufgeholt


def test_invariant_sum_of_buckets_equals_initial_plus_pnl(ledger):
    import random

    rnd = random.Random(3)
    for i in range(200):
        ledger.book(f"t{i}", rnd.uniform(-3, 3), T0)
    b = ledger.balances()
    assert b.total_capital == pytest.approx(b.initial_capital + b.total_net_pnl)


def test_tax_reserve_only_from_distributable_profit(tmp_path):
    lg = CapitalLedger(tmp_path / "t.sqlite", 100.0, tax_rate=0.25)
    lg.book("l", -4.0, T0)
    e = lg.book("w", 8.0, T0)  # 4 Aufholung, 4 verteilbar -> 1 Steuer, 3 Aufteilung
    assert e.to_tax == pytest.approx(1.0)
    assert e.to_payout == pytest.approx(0.99)


def test_capital_limit_never_raised_by_reserves(tmp_path):
    lg = CapitalLedger(tmp_path / "p.sqlite", 10.0)
    lg.book("w", 30.0, T0)
    assert lg.balances().trading_balance > 10.0
    assert lg.capital_limit(cap_eur=10.0) == 10.0
    lg.book("l", -15.0, T0)
    assert lg.capital_limit(cap_eur=10.0) == pytest.approx(lg.balances().trading_balance)


def test_day_start_balance_and_restart(tmp_path):
    path = tmp_path / "r.sqlite"
    lg = CapitalLedger(path, 100.0)
    lg.book("a", -10.0, T0 - dt.timedelta(days=1))
    lg.book("b", -5.0, T0)
    lg.close()
    lg2 = CapitalLedger(path, 100.0)  # Neustart
    assert lg2.day_start_trading_balance(T0.date()) == pytest.approx(90.0)
    assert lg2.balances().trading_balance == pytest.approx(85.0)
    with pytest.raises(ValueError):
        CapitalLedger(path, 50.0)  # anderes Startkapital -> Fehler statt stiller Änderung
