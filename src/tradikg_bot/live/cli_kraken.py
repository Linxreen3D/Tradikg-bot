"""CLI-Unterbefehle ``tradikg kraken …``.

* ``market-check``  – öffentlich: Mindestvolumen (Basis), Mindestwert (EUR), Präzision,
                      Spread, Liquidität, Slippage je Paar (kein API-Key nötig)
* ``preflight``     – privat, nur lesend: Gebühren (TradeVolume), Berechtigungen,
                      Bestände, offene Orders, Exposure, optional ``validate=true``-Order
* ``paper-trade``   – Ablauf gegen die Paper-Börse (optional mit Live-Kursen)
* ``status`` / ``reset-halt`` – Journal und SAFE_HALT verwalten
* ``live-trade``    – Echtgeld; mehrfach gesperrt (siehe ``live/gate.py``). Derzeit
                      immer blockiert, weil die Kraken-Spot-API keinen SL/TP-Bracket bietet.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from tradikg_bot.costs import KRAKEN_SPOT_TAKER
from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.executor import LiveExecutor, PlanRejectedError, TradingHaltedError
from tradikg_bot.live.gate import LiveApproval, LiveTradingBlockedError, check_live_gate
from tradikg_bot.live.journal import ExecutionJournal
from tradikg_bot.live.kraken.client import KrakenClient
from tradikg_bot.live.kraken.credentials import CredentialsError, load_credentials
from tradikg_bot.live.kraken.gateway import KRAKEN_SPOT_CAPABILITIES, KrakenSpotGateway
from tradikg_bot.live.models import BracketNotSupportedError, OrderBook, PairRules
from tradikg_bot.live.paper import PaperExchange
from tradikg_bot.live.precheck import current_exposure

LIVE_JOURNAL = Path("data/live_journal.sqlite")
PAPER_JOURNAL = Path("data/paper_journal.sqlite")


def assess_market(rules: PairRules, book: OrderBook, cap_eur: float, taker: float) -> list[str]:
    min_value = max(rules.ordermin * book.best_ask, rules.costmin)
    min_with_fee = min_value * (1 + taker)
    depth_eur = sum(p * v for p, v in book.asks[:10])
    return [
        f"Paar {rules.altname} ({rules.pair}), Status {rules.status}, Quote {rules.quote}",
        f"  Mindestvolumen: {rules.ordermin} {rules.base} ≈ {rules.ordermin * book.best_ask:.2f} "
        f"EUR | Mindestwert (costmin): {rules.costmin} EUR",
        f"  Kleinste zulässige Order inkl. {taker * 100:.2f} % Gebühr: {min_with_fee:.2f} EUR "
        f"-> {'passt' if min_with_fee <= cap_eur else 'PASST NICHT'} in {cap_eur:.2f} EUR",
        f"  Präzision: Preis {rules.pair_decimals} Dezimalen (Tick {rules.tick_size}), "
        f"Menge {rules.lot_decimals} Dezimalen",
        f"  Bid {book.best_bid} / Ask {book.best_ask} | Spread {book.spread_bps:.1f} bps | "
        f"Ask-Tiefe (10 Stufen) ≈ {depth_eur:,.0f} EUR",
    ]


def cmd_market_check(args: argparse.Namespace) -> int:
    cfg = LiveRiskConfig()
    gw = KrakenSpotGateway(KrakenClient())
    for pair in args.pair:
        try:
            rules, book = gw.pair_rules(pair), gw.order_book(pair, 25)
        except Exception as exc:
            print(f"{pair}: Marktdaten nicht abrufbar: {exc}")
            continue
        for line in assess_market(rules, book, cfg.pilot_capital_eur, args.assumed_taker):
            print(line)
    print(
        "\nHinweis: Gebühren hier nur als Annahme. Die tatsächlichen Gebühren liefert "
        "'kraken preflight' (TradeVolume)."
    )
    return 0


def _live_gateway(allow_trading: bool, journal: ExecutionJournal) -> KrakenSpotGateway:
    client = KrakenClient(
        load_credentials(), nonce_source=journal.next_nonce, allow_trading=allow_trading
    )
    return KrakenSpotGateway(client)


def cmd_preflight(args: argparse.Namespace) -> int:
    cfg = LiveRiskConfig()
    journal = ExecutionJournal(args.journal)
    try:
        gw = _live_gateway(allow_trading=False, journal=journal)
    except CredentialsError as exc:
        print(f"ERGEBNIS: NOT LIVE READY\n  - Zugangsdaten: {exc}")
        return 2
    blockers: list[str] = []

    def step(label: str, fn):
        try:
            value = fn()
            print(f"[OK]   {label}: {value}")
            return value
        except Exception as exc:
            print(f"[FEHLER] {label}: {exc}")
            blockers.append(f"{label}: {exc}")
            return None

    withdraw = step("Auszahlungsberechtigung des Keys", gw.has_withdrawal_permission)
    if withdraw:
        blockers.append("API-Key hat Auszahlungsberechtigung")
    if step("Margin-Positionen", gw.has_margin_positions):
        blockers.append("Margin-Positionen vorhanden")
    fees = step(f"Tatsächliche Gebühren {args.pair} (TradeVolume)", lambda: gw.fee_rates(args.pair))
    step("Guthaben (BalanceEx)", lambda: {k: (v.total, v.held) for k, v in gw.balances().items()})
    step("Offene Orders", lambda: [o.txid for o in gw.open_orders()])
    step("Exposure (offene Orders + Bestände)", lambda: current_exposure(gw, cfg))
    rules = step("Handelsregeln", lambda: gw.pair_rules(args.pair))
    book = step("Orderbuch", lambda: gw.order_book(args.pair, 25))
    if rules and book and fees:
        for line in assess_market(rules, book, cfg.pilot_capital_eur, fees.taker):
            print(line)
        if args.validate_order:
            volume = rules.round_volume_down(
                max(rules.ordermin, rules.costmin / book.best_ask) * 1.0001
                + 10**-rules.lot_decimals
            )
            step(
                "Kraken-Validierung einer Kauforder (validate=true, KEINE Ausführung)",
                lambda: gw.validate_entry_order(
                    args.pair, volume, rules.round_price_down(book.best_bid * 0.9)
                ),
            )
    print(
        f"\nSL/TP-Bracket über API: {'ja' if KRAKEN_SPOT_CAPABILITIES.native_bracket else 'NEIN'}"
        f" – {KRAKEN_SPOT_CAPABILITIES.evidence}"
    )
    if not KRAKEN_SPOT_CAPABILITIES.native_bracket:
        blockers.append("Kein börsenseitiger SL/TP-Bracket über die Kraken-Spot-API")
    if journal.halt_reason:
        blockers.append(f"SAFE_HALT aktiv: {journal.halt_reason}")
    print("\nERGEBNIS: " + ("LIVE READY" if not blockers else "NOT LIVE READY"))
    for b in blockers:
        print(f"  - {b}")
    return 0 if not blockers else 2


def cmd_paper_trade(args: argparse.Namespace) -> int:
    paper = PaperExchange(native_bracket=args.simulate_bracket, balances_={"ZEUR": args.paper_eur})
    if args.live_prices:
        gw = KrakenSpotGateway(KrakenClient())
        rules = gw.pair_rules(args.pair)
        paper.rules[rules.altname] = rules
        live_book = gw.order_book(args.pair, 25)
        paper.books[rules.altname] = live_book
    else:
        paper.set_book(args.pair, args.bid, args.ask)
    journal = ExecutionJournal(args.journal)
    ex = LiveExecutor(paper, journal, LiveRiskConfig(confirm_timeout_s=2, poll_interval_s=0.1))
    plan = ex.plan(args.pair, args.stop_loss, args.take_profit, args.notional)
    print(plan.summary())
    try:
        trade = ex.open_position(args.pair, args.stop_loss, args.take_profit, args.notional)
    except BracketNotSupportedError as exc:
        print(f"\nBLOCKIERT (wie bei Kraken live): {exc}")
        return 2
    except (PlanRejectedError, TradingHaltedError) as exc:
        print(f"\nBLOCKIERT: {exc}")
        return 2
    print(
        f"\nTrade {trade.id}: Status {trade.state}, Volumen {trade.filled_volume} "
        f"@ {trade.entry_avg_price:.2f}, SL {trade.stop_loss_txid}, TP {trade.take_profit_txid}"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    journal = ExecutionJournal(args.journal)
    print(f"SAFE_HALT: {journal.halt_reason or 'nein'}")
    for t in journal.trades():
        print(f"Trade {t.id} {t.pair} {t.state} Vol {t.filled_volume} PnL {t.realized_pnl}")
    for ts, level, msg, _ in journal.events(args.events):
        print(f"{ts} {level:8} {msg}")
    return 0


def cmd_reset_halt(args: argparse.Namespace) -> int:
    journal = ExecutionJournal(args.journal)
    print(f"Bisherige Sperre: {journal.halt_reason or 'keine'}")
    journal.clear_halt(args.note)
    print("Sperre aufgehoben. Vor dem nächsten Trade erfolgt ein vollständiger Abgleich.")
    return 0


def cmd_live_trade(args: argparse.Namespace) -> int:
    cfg = LiveRiskConfig()
    journal = ExecutionJournal(args.journal)
    approval = LiveApproval.from_env(cli_flag=args.i_understand_this_uses_real_money)
    try:
        gw = _live_gateway(allow_trading=True, journal=journal)
        check_live_gate(
            gw, approval, args.pair, cfg.pilot_capital_eur, today=dt.datetime.now(dt.UTC).date()
        )
        ex = LiveExecutor(gw, journal, cfg, approval)
        trade = ex.open_position(args.pair, args.stop_loss, args.take_profit, args.notional)
    except (
        LiveTradingBlockedError,
        BracketNotSupportedError,
        PlanRejectedError,
        TradingHaltedError,
        CredentialsError,
    ) as exc:
        print(f"KEINE ORDER GESENDET.\n{exc}")
        return 2
    print(f"Trade {trade.id}: {trade.state}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    kraken = sub.add_parser("kraken", help="Kraken-Spot-Ausführung (Pilot)")
    ks = kraken.add_subparsers(dest="kraken_command", required=True)

    def journal_arg(p: argparse.ArgumentParser, default: Path) -> None:
        p.add_argument("--journal", default=str(default), help="Pfad zum Ausführungsjournal")

    p = ks.add_parser("market-check", help="Öffentliche Marktregeln und Liquidität prüfen")
    p.add_argument("--pair", action="append", default=None)
    p.add_argument("--assumed-taker", type=float, default=KRAKEN_SPOT_TAKER)
    p.set_defaults(func=lambda a: cmd_market_check(_pairs_default(a)))

    p = ks.add_parser("preflight", help="Konto/Key prüfen (nur lesend) -> LIVE READY?")
    p.add_argument("--pair", default="XBTEUR")
    p.add_argument(
        "--validate-order",
        action="store_true",
        help="Kauforder nur validieren lassen (validate=true, keine Ausführung)",
    )
    journal_arg(p, LIVE_JOURNAL)
    p.set_defaults(func=cmd_preflight)

    def trade_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pair", default="XBTEUR")
        p.add_argument("--stop-loss", type=float, required=True)
        p.add_argument("--take-profit", type=float, required=True)
        p.add_argument("--notional", type=float, default=10.0, help="max. EUR für den Einstieg")

    p = ks.add_parser("paper-trade", help="Ablauf gegen die Paper-Börse testen")
    trade_args(p)
    p.add_argument("--bid", type=float, default=60000.0)
    p.add_argument("--ask", type=float, default=60010.0)
    p.add_argument("--paper-eur", type=float, default=10.0)
    p.add_argument("--live-prices", action="store_true", help="Regeln/Orderbuch von Kraken laden")
    p.add_argument(
        "--simulate-bracket",
        action="store_true",
        help="TESTATTRAPPE: hypothetischen API-Bracket simulieren",
    )
    journal_arg(p, PAPER_JOURNAL)
    p.set_defaults(func=cmd_paper_trade)

    p = ks.add_parser("status", help="Journal, Sperre und Ereignisse anzeigen")
    p.add_argument("--events", type=int, default=20)
    journal_arg(p, LIVE_JOURNAL)
    p.set_defaults(func=cmd_status)

    p = ks.add_parser("reset-halt", help="SAFE_HALT nach manueller Prüfung aufheben")
    p.add_argument("--note", required=True, help="Begründung / Prüfergebnis")
    journal_arg(p, LIVE_JOURNAL)
    p.set_defaults(func=cmd_reset_halt)

    p = ks.add_parser("live-trade", help="ECHTGELD – mehrfach gesperrt, derzeit immer blockiert")
    trade_args(p)
    p.add_argument("--i-understand-this-uses-real-money", action="store_true")
    journal_arg(p, LIVE_JOURNAL)
    p.set_defaults(func=cmd_live_trade)


def _pairs_default(args: argparse.Namespace) -> argparse.Namespace:
    if not args.pair:
        args.pair = list(LiveRiskConfig().allowed_pairs)
    return args
