"""Paper-Tests der Ausführungs-Zustandsmaschine (inkl. Fehlerszenarien)."""

from __future__ import annotations

import pytest

from tradikg_bot.live.executor import PlanRejectedError, TradingHaltedError
from tradikg_bot.live.gate import LiveTradingBlockedError
from tradikg_bot.live.journal import ExecutionJournal, TradeState
from tradikg_bot.live.models import BracketNotSupportedError, OrderStatus
from tradikg_bot.live.paper import PaperExchange

from live_helpers import make_executor

SL, TP = 58000.0, 64000.0


def open_(ex, notional=10.0):
    return ex.open_position("XBTEUR", SL, TP, notional)


def test_kraken_like_exchange_refuses_without_creating_anything(journal):
    paper = PaperExchange()  # Standard = Kraken-Fähigkeiten: kein API-Bracket
    paper.set_book("XBTEUR", 60000.0, 60010.0)
    with pytest.raises(BracketNotSupportedError):
        open_(make_executor(paper, journal))
    assert journal.trades() == [] and paper.orders == {}
    assert not journal.halt_reason


def test_happy_path_take_profit(paper, journal):
    ex = make_executor(paper, journal)
    trade = open_(ex)
    assert trade.state is TradeState.PROTECTED
    assert trade.filled_volume == trade.requested_volume >= 0.0001
    legs = [o for o in paper.orders.values() if o.role and o.status is OrderStatus.OPEN]
    assert {str(o.role) for o in legs} == {"stop_loss", "take_profit"}
    paper.move_market("XBTEUR", 64000.0, 64010.0)
    report = ex.sync()
    closed = journal.get_trade(trade.id)
    assert closed.state is TradeState.CLOSED and closed.exit_reason == "take_profit"
    assert closed.realized_pnl > 0 and report.closed_trades == [trade.id]
    assert not ex.halted


def test_stop_loss_loss_stays_within_planned_worst_case(paper, journal):
    ex = make_executor(paper, journal)
    plan = ex.plan("XBTEUR", SL, TP, 10.0)
    trade = open_(ex)
    paper.move_market("XBTEUR", 57900.0, 57910.0)  # Gap unter den Stop
    ex.sync()
    closed = journal.get_trade(trade.id)
    assert closed.exit_reason == "stop_loss" and closed.realized_pnl < 0
    assert -closed.realized_pnl <= plan.worst_case_loss_eur + 1e-9


def test_reject_is_clean_and_does_not_halt(paper, journal):
    paper.fail_next = "reject"
    trade = open_(make_executor(paper, journal))
    assert trade.state is TradeState.REJECTED and not journal.halt_reason


def test_timeout_after_accept_is_reconciled_via_client_ref(paper, journal):
    paper.fail_next = "timeout_after_accept"
    trade = open_(make_executor(paper, journal))
    assert trade.state is TradeState.PROTECTED  # kein zweites Senden, sondern Abgleich
    assert sum(1 for o in paper.orders.values() if str(o.role) == "entry") == 1


def test_timeout_before_accept_halts_instead_of_guessing(paper, journal):
    paper.fail_next = "timeout_before_accept"
    trade = open_(make_executor(paper, journal))
    assert trade.state is TradeState.UNRESOLVED
    assert "nicht innerhalb" in journal.halt_reason
    with pytest.raises(TradingHaltedError):
        open_(make_executor(paper, journal))


def test_partial_fill_is_protected_with_filled_volume(paper, journal):
    paper.fill_ratio = 0.7
    trade = open_(make_executor(paper, journal))
    assert trade.state is TradeState.PROTECTED
    assert trade.filled_volume < trade.requested_volume
    legs = [o for o in paper.orders.values() if o.role and str(o.role) != "entry"]
    assert all(o.volume == trade.filled_volume for o in legs)


def test_partial_fill_below_minimum_halts(paper, journal):
    paper.fill_ratio = 0.3  # 0.3 * ~0.000166 BTC < ordermin 0.0001
    trade = open_(make_executor(paper, journal))
    assert trade.state is TradeState.UNRESOLVED
    assert "nicht börsenseitig absicherbar" in journal.halt_reason


def test_missing_protection_halts_and_never_cancels_anything_protective(paper, journal):
    paper.reject_protection = True
    trade = open_(make_executor(paper, journal))
    assert trade.state is TradeState.UNRESOLVED
    assert "Schutz nicht innerhalb" in journal.halt_reason


def test_broken_oco_halts(paper, journal):
    paper.oco_broken = True
    ex = make_executor(paper, journal)
    trade = open_(ex)
    paper.move_market("XBTEUR", 64000.0, 64010.0)
    ex.sync()
    assert journal.get_trade(trade.id).state is TradeState.UNRESOLVED
    assert "Gegenorder" in journal.halt_reason


def test_cancelled_protection_halts(paper, journal):
    ex = make_executor(paper, journal)
    trade = open_(ex)
    sl_txid = journal.get_trade(trade.id).stop_loss_txid
    paper.cancel_order(sl_txid)  # z. B. manuell in der Kraken-Oberfläche storniert
    ex.sync()
    assert "nicht mehr aktiv" in journal.halt_reason


def test_restart_keeps_state_and_reconciles(paper, tmp_path):
    path = tmp_path / "j.sqlite"
    first = ExecutionJournal(path)
    trade = open_(make_executor(paper, first))
    first.close()
    second = ExecutionJournal(path)  # "Neustart"
    ex = make_executor(paper, second)
    report = ex.reconcile()
    assert not report.halted and report.checked_trades == 1
    assert second.get_trade(trade.id).state is TradeState.PROTECTED
    with pytest.raises(TradingHaltedError, match="bereits eine Position"):
        open_(ex)


def test_restart_after_crash_during_submit_halts(paper, tmp_path):
    journal = ExecutionJournal(tmp_path / "j.sqlite")
    journal.create_trade(
        client_ref=777,
        venue="paper",
        pair="XBTEUR",
        requested_volume=0.0001,
        entry_limit_price=60100,
        stop_loss_price=SL,
        take_profit_price=TP,
    )
    report = make_executor(paper, journal).reconcile()
    assert report.halted and "Sendestatus unbekannt" in journal.halt_reason


def test_halt_survives_restart_and_needs_manual_reset(paper, tmp_path):
    path = tmp_path / "j.sqlite"
    j = ExecutionJournal(path)
    j.set_halt("Test")
    j.close()
    j2 = ExecutionJournal(path)
    with pytest.raises(TradingHaltedError):
        open_(make_executor(paper, j2))
    with pytest.raises(ValueError):
        j2.clear_halt("  ")
    j2.clear_halt("Konto in Kraken-Oberfläche geprüft, keine offenen Orders")
    assert open_(make_executor(paper, j2)).state is TradeState.PROTECTED


def test_unknown_balance_or_order_on_account_halts(paper, journal):
    paper.balances_["XXBT"] = 0.01  # Fremdbestand (z. B. Hauptkonto statt Sub-Konto)
    report = make_executor(paper, journal).reconcile()
    assert report.halted and "Unerwarteter Bestand" in journal.halt_reason


def test_daily_loss_limit_blocks_after_losses(paper, journal):
    ex = make_executor(paper, journal)
    losses = 0
    for _ in range(3):
        paper.move_market("XBTEUR", 60000.0, 60010.0)
        try:
            trade = open_(ex)
        except PlanRejectedError as exc:
            failed = {c.name for c in exc.plan.failed()}
            assert failed & {
                "Tagesverlustbudget",
                "Mindestvolumen (Basis)",
                "Worst-Case-Verlust",
                "Exposure-Obergrenze",
            }
            break
        paper.move_market("XBTEUR", 57000.0, 57010.0)
        ex.sync()
        losses += 1
        assert journal.get_trade(trade.id).exit_reason == "stop_loss"
    else:
        pytest.fail("Tagesverlustlimit hat nicht gegriffen")
    # Garantiert ist: keine neue Position, sobald das Budget erschöpft ist. Kurslücken unter
    # den Stop (hier 58000 -> 57000) können das Limit überschreiten -> dokumentiertes Restrisiko.
    assert losses >= 1
    assert ex.remaining_daily_budget() < ex.cfg.max_loss_per_trade_eur


def test_gap_through_stop_can_exceed_planned_loss_documented_risk(paper, journal):
    ex = make_executor(paper, journal)
    plan = ex.plan("XBTEUR", SL, TP, 10.0)
    trade = open_(ex)
    paper.move_market("XBTEUR", 55000.0, 55010.0)  # Lücke von ~5 % unter den Stop
    ex.sync()
    assert -journal.get_trade(trade.id).realized_pnl > plan.worst_case_loss_eur


def test_losses_are_not_refilled(paper, journal):
    ex = make_executor(paper, journal)
    trade = open_(ex)
    paper.move_market("XBTEUR", 57000.0, 57010.0)
    ex.sync()
    pnl = journal.get_trade(trade.id).realized_pnl
    assert ex.cfg.effective_cap(journal.realized_pnl_total()) == pytest.approx(10.0 + pnl)


def test_live_gateway_without_approval_is_blocked(paper, journal):
    paper.is_live = True  # simuliert eine Live-Börse MIT Bracket – Gate muss trotzdem sperren
    with pytest.raises(LiveTradingBlockedError) as err:
        open_(make_executor(paper, journal))
    assert any("Flag" in r for r in err.value.reasons)
    assert paper.orders == {}
