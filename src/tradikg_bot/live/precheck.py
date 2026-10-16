"""Vorab-Prüfungen vor jeder Order.

Geprüft werden Mindestvolumen (Basis), Mindestwert (EUR), Preis- und
Mengenpräzision, Spread, Liquidität (Orderbuchtiefe), Slippage, tatsächliche
Gebühren, Worst-Case-Verlust, Chance/Risiko nach Kosten, Exposure-Obergrenze
(inkl. offener Orders und Positionen) und das Tagesverlustbudget.
Jede Prüfung wird mit Begründung protokolliert. Schlägt eine fehl, gibt es keine Order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.gateway import ExchangeGateway
from tradikg_bot.live.models import FeeRates, OrderSide, PairRules

BPS = 1e-4


class ExposureUnknownError(RuntimeError):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class TradePlan:
    pair: str
    volume: float = 0.0
    entry_limit_price: float = 0.0
    expected_entry_price: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    notional_eur: float = 0.0
    entry_fee_eur: float = 0.0
    worst_case_loss_eur: float = 0.0
    net_reward_eur: float = 0.0
    fees: FeeRates | None = None
    rules: PairRules | None = None
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> str:
        lines = [
            f"Plan {self.pair}: Volumen {self.volume} @ Limit {self.entry_limit_price}, "
            f"SL {self.stop_loss_price}, TP {self.take_profit_price}"
        ]
        lines += [f"  [{'OK ' if c.ok else 'NEIN'}] {c.name}: {c.detail}" for c in self.checks]
        return "\n".join(lines)


def current_exposure(gateway: ExchangeGateway, cfg: LiveRiskConfig) -> tuple[float, list[str]]:
    """Echtgeld-Exposure in EUR aus dem **Börsenzustand** (nicht aus lokaler Erinnerung).

    = offene Kauforders (Restvolumen × Limit) + Bestände der Pilot-Basiswährungen × Bid.
    Offene Orders auf nicht erlaubten bzw. nicht in EUR notierten Paaren machen die
    Exposure unbestimmbar -> Fehler (fail-closed).
    """
    details: list[str] = []
    rules_by_alt = {p: gateway.pair_rules(p) for p in cfg.allowed_pairs}
    total = 0.0
    for order in gateway.open_orders():
        rules = rules_by_alt.get(order.pair) or next(
            (r for r in rules_by_alt.values() if r.pair == order.pair), None
        )
        if rules is None or rules.quote not in ("ZEUR", "EUR"):
            raise ExposureUnknownError(f"Offene Order {order.txid} auf fremdem Paar {order.pair}")
        if order.side is OrderSide.BUY:
            price = order.price or gateway.order_book(rules.altname, 1).best_ask
            value = (order.volume - order.filled) * price
            total += value
            details.append(f"offene Kauforder {order.txid}: {value:.2f} EUR")
    balances = gateway.balances()
    for rules in rules_by_alt.values():
        bal = balances.get(rules.base)
        if bal is None or bal.total <= 0:
            continue
        value = bal.total * gateway.order_book(rules.altname, 1).best_bid
        total += value
        details.append(f"Bestand {rules.base}: {bal.total} ≈ {value:.2f} EUR")
    return total, details


def plan_trade(
    gateway: ExchangeGateway,
    cfg: LiveRiskConfig,
    *,
    pair: str,
    stop_loss_price: float,
    take_profit_price: float,
    max_notional_eur: float,
    cumulative_realized_pnl: float,
    remaining_daily_budget_eur: float,
) -> TradePlan:
    plan = TradePlan(pair=pair)
    add = plan.checks.append

    if pair not in cfg.allowed_pairs:
        add(Check("Paar erlaubt", False, f"{pair} nicht in {cfg.allowed_pairs}"))
        return plan
    try:
        rules = gateway.pair_rules(pair)
        book = gateway.order_book(pair, 25)
        fees = gateway.fee_rates(pair)
    except Exception as exc:  # Marktdaten/Gebühren unklar -> keine Order
        add(Check("Marktdaten & Gebühren", False, f"nicht abrufbar: {exc}"))
        return plan
    plan.rules, plan.fees = rules, fees
    add(Check("Paar handelbar", rules.status == "online", f"Status '{rules.status}'"))
    add(Check("Quote EUR", rules.quote in ("ZEUR", "EUR"), f"Quote {rules.quote}"))
    add(
        Check(
            "Gebühren",
            True,
            f"Taker {fees.taker * 100:.3f} %, Maker {fees.maker * 100:.3f} % ({fees.source})",
        )
    )
    if not book.bids or not book.asks:
        add(Check("Orderbuch", False, "leer"))
        return plan
    add(
        Check(
            "Spread",
            book.spread_bps <= cfg.max_spread_bps,
            f"{book.spread_bps:.1f} bps (max {cfg.max_spread_bps})",
        )
    )

    # Preise auf Tick-Raster: SL aufrunden (enger), TP abrunden (erreichbarer), Limit aufrunden
    entry_limit = rules.round_price_up(book.best_ask * (1 + cfg.max_entry_slippage_bps * BPS))
    sl = rules.round_price_up(stop_loss_price)
    tp = rules.round_price_down(take_profit_price)
    plan.entry_limit_price, plan.stop_loss_price, plan.take_profit_price = entry_limit, sl, tp
    add(
        Check(
            "Preisstruktur",
            0 < sl < book.best_bid and tp > entry_limit,
            f"SL {sl} < Bid {book.best_bid}, TP {tp} > Limit {entry_limit}",
        )
    )
    if not (0 < sl < book.best_bid and tp > entry_limit):
        return plan

    # Größenbestimmung: Notional-, Exposure- und Risikogrenzen
    try:
        exposure, exp_details = current_exposure(gateway, cfg)
    except Exception as exc:
        add(Check("Exposure bestimmbar", False, str(exc)))
        return plan
    cap = cfg.effective_cap(cumulative_realized_pnl)
    headroom = cap - exposure
    add(
        Check(
            "Exposure-Obergrenze",
            headroom > 0,
            f"aktuell {exposure:.2f} EUR, Obergrenze {cap:.2f} EUR (ohne Wiederauffüllung); "
            + ("; ".join(exp_details) or "keine offenen Orders/Bestände"),
        )
    )
    risk_budget = min(cfg.max_loss_per_trade_eur, remaining_daily_budget_eur)
    add(
        Check(
            "Tagesverlustbudget",
            risk_budget > 0,
            f"verbleibend {remaining_daily_budget_eur:.2f} EUR "
            f"(Limit {cfg.daily_loss_limit_eur:.2f} EUR/Tag)",
        )
    )
    stop_fill = sl * (1 - cfg.assumed_stop_slippage_bps * BPS)
    unit_loss = entry_limit * (1 + fees.taker) - stop_fill * (1 - fees.taker)
    budget_eur = min(max_notional_eur, headroom)
    vol_by_budget = budget_eur / (entry_limit * (1 + fees.taker))
    vol_by_risk = risk_budget / unit_loss if unit_loss > 0 else 0.0
    volume = rules.round_volume_down(max(0.0, min(vol_by_budget, vol_by_risk)))
    plan.volume = volume
    min_order_eur = rules.ordermin * book.best_ask
    add(
        Check(
            "Mindestvolumen (Basis)",
            volume >= rules.ordermin,
            f"{rules.format_volume(volume)} {rules.base} vs. ordermin {rules.ordermin} "
            f"{rules.base} (≈ {min_order_eur:.2f} EUR); begrenzt durch Budget "
            f"{vol_by_budget:.8f} / Risiko {vol_by_risk:.8f}",
        )
    )
    if volume <= 0:
        return plan

    vwap = book.buy_vwap(volume)
    plan.expected_entry_price = vwap or entry_limit
    plan.notional_eur = volume * entry_limit
    plan.entry_fee_eur = plan.notional_eur * fees.taker
    add(
        Check(
            "Mindestwert Einstieg (EUR)",
            volume * entry_limit >= rules.costmin,
            f"{volume * entry_limit:.4f} EUR vs. costmin {rules.costmin}",
        )
    )
    add(
        Check(
            "Mindestwert Stop-Loss-Order",
            volume * sl >= rules.costmin,
            f"{volume * sl:.4f} EUR vs. costmin {rules.costmin}",
        )
    )
    add(
        Check(
            "Mindestwert Take-Profit-Order",
            volume * tp >= rules.costmin,
            f"{volume * tp:.4f} EUR vs. costmin {rules.costmin}",
        )
    )
    add(
        Check(
            "Preispräzision",
            all(rules.is_on_tick(p) for p in (entry_limit, sl, tp)),
            f"Tick {rules.tick_size}, {rules.pair_decimals} Nachkommastellen",
        )
    )

    depth = sum(v for p, v in book.asks if p <= entry_limit)
    add(
        Check(
            "Liquidität",
            vwap is not None and depth >= cfg.min_depth_multiple * volume,
            f"Tiefe bis Limit {depth:.8f} vs. {cfg.min_depth_multiple}× {volume}",
        )
    )
    slip_bps = ((vwap / book.best_ask) - 1) / BPS if vwap else float("inf")
    add(
        Check(
            "Slippage",
            slip_bps <= cfg.max_entry_slippage_bps,
            f"erwartet {slip_bps:.1f} bps (max {cfg.max_entry_slippage_bps})",
        )
    )

    worst = volume * unit_loss
    reward = volume * tp * (1 - fees.maker) - volume * plan.expected_entry_price * (1 + fees.taker)
    plan.worst_case_loss_eur, plan.net_reward_eur = worst, reward
    add(
        Check(
            "Worst-Case-Verlust",
            worst <= risk_budget + 1e-9,
            f"{worst:.4f} EUR inkl. Gebühren & {cfg.assumed_stop_slippage_bps} bps "
            f"Stop-Slippage (max {risk_budget:.2f})",
        )
    )
    add(
        Check(
            "Chance/Risiko nach Kosten",
            reward > 0 and reward >= cfg.min_reward_risk_after_costs * worst,
            f"netto +{reward:.4f} / -{worst:.4f} EUR",
        )
    )
    cost_total = plan.notional_eur + plan.entry_fee_eur
    add(
        Check(
            "Exposure nach Order",
            exposure + cost_total <= cap + 1e-9,
            f"{exposure:.2f} + {cost_total:.2f} <= {cap:.2f} EUR",
        )
    )
    try:
        quote_bal = gateway.balances().get(rules.quote)
        avail = quote_bal.available if quote_bal else 0.0
    except Exception as exc:
        add(Check("Guthaben", False, f"nicht abrufbar: {exc}"))
        return plan
    add(
        Check(
            "Guthaben",
            avail >= cost_total,
            f"verfügbar {avail:.2f} EUR, benötigt {cost_total:.2f} EUR",
        )
    )
    return plan
