from __future__ import annotations

import datetime as dt

import pytest

from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.gate import (
    LiveApproval,
    LiveTradingBlockedError,
    approval_phrase,
    check_live_gate,
)
from tradikg_bot.live.journal import ExecutionJournal
from tradikg_bot.live.kraken.gateway import KRAKEN_SPOT_CAPABILITIES, parse_order, parse_pair_rules
from tradikg_bot.live.models import ExchangeOrder, OrderSide, OrderStatus
from tradikg_bot.live.paper import PAPER_RULES, PaperExchange
from tradikg_bot.live.precheck import ExposureUnknownError, current_exposure, plan_trade

CFG = LiveRiskConfig()


def plan(paper, pair="XBTEUR", sl=58000.0, tp=64000.0, notional=10.0, pnl=0.0, budget=1.0, cfg=CFG):
    return plan_trade(
        paper,
        cfg,
        pair=pair,
        stop_loss_price=sl,
        take_profit_price=tp,
        max_notional_eur=notional,
        cumulative_realized_pnl=pnl,
        remaining_daily_budget_eur=budget,
    )


def failed(p):
    return {c.name for c in p.failed()}


@pytest.fixture
def paper():
    ex = PaperExchange()
    ex.set_book("XBTEUR", 60000.0, 60010.0)
    ex.set_book("ETHEUR", 3000.0, 3000.5)
    return ex


# -- Konfiguration -----------------------------------------------------------------------
def test_cap_cannot_be_raised_above_10_eur():
    with pytest.raises(ValueError):
        LiveRiskConfig(pilot_capital_eur=10.01)
    with pytest.raises(ValueError):
        LiveRiskConfig(daily_loss_fraction=0.2)
    assert CFG.daily_loss_limit_eur == pytest.approx(1.0)


def test_no_refill_after_losses_and_no_increase_after_profits():
    assert CFG.effective_cap(-3.0) == pytest.approx(7.0)
    assert CFG.effective_cap(+5.0) == pytest.approx(10.0)
    assert CFG.effective_cap(-12.0) == 0.0


# -- Vorab-Prüfungen ------------------------------------------------------------------------
def test_valid_plan_passes_all_checks(paper):
    p = plan(paper)
    assert p.ok, p.summary()
    assert p.volume >= PAPER_RULES["XBTEUR"].ordermin
    assert p.notional_eur + p.entry_fee_eur <= 10.0 + 1e-9
    assert p.worst_case_loss_eur <= CFG.max_loss_per_trade_eur


def test_eth_minimum_volume_exceeds_10_eur_cap(paper):
    p = plan(paper, pair="ETHEUR", sl=2900.0, tp=3200.0)
    assert "Mindestvolumen (Basis)" in failed(p)  # 0.01 ETH ≈ 30 EUR > 10 EUR


def test_tight_risk_budget_shrinks_size_below_minimum(paper):
    p = plan(paper, budget=0.2)
    assert "Mindestvolumen (Basis)" in failed(p)


def test_daily_budget_exhausted(paper):
    assert "Tagesverlustbudget" in failed(plan(paper, budget=0.0))


def test_wide_spread_rejected(paper):
    paper.set_book("XBTEUR", 60000.0, 60400.0)  # ~66 bps
    assert "Spread" in failed(plan(paper))


def test_thin_book_rejected(paper):
    tick = PAPER_RULES["XBTEUR"].tick_size
    paper.set_book("XBTEUR", 60000.0, 60000.0 + tick, depth=0.00001)
    assert failed(plan(paper)) & {"Liquidität", "Slippage"}


def test_prices_are_rounded_to_tick(paper):
    p = plan(paper, sl=58000.04, tp=64000.06)
    assert p.stop_loss_price == 58000.1  # aufgerundet (enger)
    assert p.take_profit_price == 64000.0  # abgerundet (erreichbarer)
    assert p.ok


def test_stop_above_market_rejected(paper):
    assert "Preisstruktur" in failed(plan(paper, sl=60500.0))


def test_costs_kill_tiny_take_profit(paper):
    p = plan(paper, sl=59500.0, tp=60200.0)
    assert "Chance/Risiko nach Kosten" in failed(p)


def test_existing_holdings_count_towards_cap(paper):
    paper.balances_["XXBT"] = 0.0001  # ≈ 6 EUR bereits investiert
    p = plan(paper)
    assert p.notional_eur <= 10.0 - 0.0001 * 60000.0 + 1e-6
    assert "Mindestvolumen (Basis)" in failed(p)  # Rest reicht nicht mehr für ordermin


def test_open_buy_orders_count_towards_cap(paper):
    paper.orders["X"] = ExchangeOrder(
        "X", 1, "XBTEUR", OrderSide.BUY, "limit", OrderStatus.OPEN, 0.0001, 0.0, price=59000.0
    )
    exposure, details = current_exposure(paper, CFG)
    assert exposure == pytest.approx(5.9) and details


def test_open_order_on_foreign_pair_blocks(paper):
    paper.rules["XBTUSD"] = PAPER_RULES["XBTEUR"].__class__(
        "XXBTZUSD", "XBTUSD", "XXBT", "ZUSD", 8, 1, 0.1, 0.0001, 0.5
    )
    paper.orders["Y"] = ExchangeOrder(
        "Y", 1, "XBTUSD", OrderSide.BUY, "limit", OrderStatus.OPEN, 0.0001, 0.0, price=59000.0
    )
    with pytest.raises(ExposureUnknownError):
        current_exposure(paper, CFG)
    assert "Exposure bestimmbar" in failed(plan(paper))


def test_pair_not_allowed():
    assert "Paar erlaubt" in failed(plan(PaperExchange(), pair="DOGEEUR"))


def test_market_data_failure_blocks(paper):
    paper.books.clear()
    assert "Marktdaten & Gebühren" in failed(plan(paper))


# -- Gate ---------------------------------------------------------------------------------------
TODAY = dt.date(2026, 10, 17)


def live_paper(**kw):
    p = PaperExchange(native_bracket=True, **kw)
    p.is_live = True
    return p


def test_gate_requires_flag_and_date_bound_phrase():
    phrase = approval_phrase(TODAY, 10.0, "XBTEUR")
    assert phrase == "ECHTGELD-FREIGABE EINZELTRADE 2026-10-17 MAX 10.00 EUR XBTEUR"
    check_live_gate(live_paper(), LiveApproval(True, phrase), "XBTEUR", 10.0, today=TODAY)
    for approval in (
        None,
        LiveApproval(False, phrase),
        LiveApproval(True, None),
        LiveApproval(True, phrase.replace("XBTEUR", "ETHEUR")),
    ):
        with pytest.raises(LiveTradingBlockedError):
            check_live_gate(live_paper(), approval, "XBTEUR", 10.0, today=TODAY)
    # Freigabe von gestern gilt heute nicht mehr
    with pytest.raises(LiveTradingBlockedError):
        check_live_gate(
            live_paper(),
            LiveApproval(True, phrase),
            "XBTEUR",
            10.0,
            today=TODAY + dt.timedelta(days=1),
        )


@pytest.mark.parametrize("kw", [{"withdrawal_permission": True}, {"margin_positions": True}])
def test_gate_blocks_withdrawal_permission_and_margin(kw):
    phrase = approval_phrase(TODAY, 10.0, "XBTEUR")
    with pytest.raises(LiveTradingBlockedError):
        check_live_gate(live_paper(**kw), LiveApproval(True, phrase), "XBTEUR", 10.0, today=TODAY)


def test_paper_gateway_is_not_gated():
    check_live_gate(PaperExchange(), None, "XBTEUR", 10.0)


def test_kraken_capabilities_are_documented():
    assert KRAKEN_SPOT_CAPABILITIES.native_bracket is False
    assert any("conditional-close" in s for s in KRAKEN_SPOT_CAPABILITIES.sources)


# -- Journal --------------------------------------------------------------------------------
def test_nonce_monotonic_across_restart(tmp_path):
    path = tmp_path / "j.sqlite"
    j = ExecutionJournal(path)
    a = [j.next_nonce() for _ in range(5)]
    j.close()
    b = ExecutionJournal(path).next_nonce()
    assert a == sorted(set(a)) and b > a[-1]


def test_client_refs_are_unique_positive_int32(journal):
    refs = {journal.new_client_ref() for _ in range(200)}
    assert len(refs) == 200 and all(0 < r < 2**31 for r in refs)


# -- Kraken-Parser -------------------------------------------------------------------------------
def test_parse_kraken_payloads():
    rules = parse_pair_rules(
        "XXBTZEUR",
        {
            "altname": "XBTEUR",
            "base": "XXBT",
            "quote": "ZEUR",
            "lot_decimals": 8,
            "pair_decimals": 1,
            "tick_size": "0.1",
            "ordermin": "0.00005",
            "costmin": "0.5",
            "status": "online",
            "leverage_buy": [],
            "leverage_sell": [],
        },
    )
    assert rules.round_volume_down(0.000123456789) == 0.00012345
    assert not rules.margin_available
    order = parse_order(
        "OXYZ",
        {
            "userref": 55,
            "status": "open",
            "vol": "0.0002",
            "vol_exec": "0.0001",
            "price": "60000",
            "fee": "0.024",
            "descr": {
                "pair": "XBTEUR",
                "type": "sell",
                "ordertype": "stop-loss",
                "price": "58000.0",
            },
        },
    )
    assert order.client_ref == 55 and order.side is OrderSide.SELL
    assert order.filled == 0.0001 and order.price == 58000.0
