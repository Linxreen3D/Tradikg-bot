"""Positionsgrößen und Kosten-/Chance-Risiko-Prüfung vor jedem Einstieg.

Die Größe wird so gewählt, dass der *Worst-Case-Verlust bis zum Stop-Loss inkl.
aller Kosten* (Kommissionen, Spread, Slippage) folgende Grenzen nicht übersteigt:

* ``risk_per_trade`` × Equity
* das verbleibende Tagesverlustbudget (10 %-Abschaltschwelle)

Zusätzlich gelten Kapital- und Konzentrationsgrenzen sowie die Handelsrestriktionen
des Instruments. Nach der Größenbestimmung wird geprüft, ob der Nettogewinn am
Take-Profit (nach Kosten) im Verhältnis zum Nettorisiko ausreichend ist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tradikg_bot.config import RiskConfig
from tradikg_bot.costs.fees import FeeModel, OrderSide
from tradikg_bot.domain import Instrument


class SkipReason(StrEnum):
    INVALID_LEVELS = "invalid_stop_or_take_profit"
    NO_RISK_BUDGET = "no_risk_budget"
    BELOW_MIN_SIZE = "below_min_size"
    COSTS_TOO_HIGH = "reward_after_costs_too_low"
    DAILY_LOSS_HALT = "daily_loss_halt"


@dataclass(frozen=True)
class SizingDecision:
    qty: float
    skip_reason: SkipReason | None = None
    expected_entry_price: float = 0.0
    worst_case_loss: float = 0.0
    net_reward_at_take_profit: float = 0.0
    round_trip_cost: float = 0.0

    @property
    def accepted(self) -> bool:
        return self.skip_reason is None and self.qty > 0

    @property
    def reward_risk_after_costs(self) -> float:
        if self.worst_case_loss <= 0:
            return 0.0
        return self.net_reward_at_take_profit / self.worst_case_loss


def worst_case_loss(qty: float, ref_price: float, stop_distance: float, fees: FeeModel) -> float:
    """Verlust, wenn zum Referenzkurs gekauft und exakt am Stop (Marktorder) verkauft wird."""
    if qty <= 0:
        return 0.0
    entry = fees.market_fill(OrderSide.BUY, qty, ref_price)
    stop_price = entry.price - stop_distance
    if stop_price <= 0:
        # Totalverlust als Obergrenze
        return entry.notional + entry.commission
    exit_ = fees.market_fill(OrderSide.SELL, qty, stop_price)
    return -(entry.cash_flow + exit_.cash_flow)


def net_reward_at_take_profit(
    qty: float, ref_price: float, take_profit_distance: float, fees: FeeModel
) -> float:
    """Nettogewinn, wenn der Take-Profit (Limit-Order) erreicht wird."""
    if qty <= 0:
        return 0.0
    entry = fees.market_fill(OrderSide.BUY, qty, ref_price)
    exit_ = fees.limit_fill(OrderSide.SELL, qty, entry.price + take_profit_distance)
    return entry.cash_flow + exit_.cash_flow


def size_position(
    *,
    equity: float,
    cash: float,
    ref_price: float,
    stop_distance: float,
    take_profit_distance: float,
    risk: RiskConfig,
    instrument: Instrument,
    fees: FeeModel,
    remaining_daily_loss_budget: float | None = None,
) -> SizingDecision:
    """Bestimmt die Ordergröße ausschließlich mit vor der Ausführung bekannten Daten."""
    if not (
        ref_price > 0
        and stop_distance > 0
        and take_profit_distance > 0
        and stop_distance < ref_price
    ):
        return SizingDecision(0.0, SkipReason.INVALID_LEVELS)

    risk_budget = equity * risk.risk_per_trade
    if remaining_daily_loss_budget is not None:
        risk_budget = min(risk_budget, remaining_daily_loss_budget)
    if risk_budget <= 0 or equity <= 0 or cash <= 0:
        return SizingDecision(0.0, SkipReason.NO_RISK_BUDGET)

    entry_price = fees.execution_price(OrderSide.BUY, ref_price)
    capital_cap = min(cash, equity * risk.max_position_fraction)

    def affordable(q: float) -> bool:
        return q * entry_price + fees.commission(q * entry_price) <= capital_cap + 1e-12

    def within_risk(q: float) -> bool:
        return worst_case_loss(q, ref_price, stop_distance, fees) <= risk_budget + 1e-12

    # Obergrenze ohne Kosten, danach Bisektion (beide Bedingungen sind monoton in q).
    hi = min(risk_budget / stop_distance, capital_cap / entry_price)
    lo = 0.0
    if affordable(hi) and within_risk(hi):
        lo = hi
    else:
        for _ in range(100):
            mid = (lo + hi) / 2
            if affordable(mid) and within_risk(mid):
                lo = mid
            else:
                hi = mid
    qty = instrument.round_qty(lo)
    if qty <= 0 or qty * entry_price < instrument.min_notional:
        return SizingDecision(0.0, SkipReason.BELOW_MIN_SIZE, expected_entry_price=entry_price)

    loss = worst_case_loss(qty, ref_price, stop_distance, fees)
    reward = net_reward_at_take_profit(qty, ref_price, take_profit_distance, fees)
    rt_cost = fees.round_trip_cost(qty, ref_price)
    decision_kwargs = dict(
        expected_entry_price=entry_price,
        worst_case_loss=loss,
        net_reward_at_take_profit=reward,
        round_trip_cost=rt_cost,
    )
    if reward <= 0 or reward < risk.min_reward_risk_after_costs * loss:
        return SizingDecision(0.0, SkipReason.COSTS_TOO_HIGH, **decision_kwargs)
    return SizingDecision(qty, None, **decision_kwargs)
