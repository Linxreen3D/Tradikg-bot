"""Paper-Börse für Integrations- und Fehlerszenario-Tests.

Sie bildet die Kraken-Spot-Regeln nach: ``ordermin`` (Basis), ``costmin`` (EUR),
Tick-Raster, Mengen-Dezimalen, Taker/Maker-Gebühren, gebundene Guthaben und
IOC-Ausführung gegen das Orderbuch.

**Standard:** ``native_bracket=False``, also dieselbe Fähigkeit wie die echte
Kraken-Spot-API. ``submit_bracket`` lehnt dann ab. Nur in Tests lässt sich ein
hypothetischer API-Bracket aktivieren (``native_bracket=True``), um die
Zustandsmaschine der Ausführung (Teilausführung, Timeout, Neustart, defekter OCO)
zu prüfen. Das ist eine Testattrappe, keine Absicherung für den Echtbetrieb.

Fehlerinjektion über ``fail_next``:
``"reject"``, ``"timeout_before_accept"``, ``"timeout_after_accept"``.

Für den autonomen Paper-Kreislauf wird ``process_bar`` verwendet: SL/TP werden
anhand von Open/High/Low der Kerze ausgelöst (Kurslücke -> Ausführung zum
Eröffnungskurs; SL und TP in derselben Kerze -> konservativ Stop-Loss). Der
komplette Zustand lässt sich speichern und laden (``to_state``/``from_state``),
damit ein Neustart die simulierte Börse nicht verliert.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from tradikg_bot.live.models import (
    Balance,
    BracketNotSupportedError,
    BracketRequest,
    ExchangeOrder,
    FeeRates,
    GatewayCapabilities,
    LegRole,
    OrderBook,
    OrderRejectedError,
    OrderSide,
    OrderStatus,
    OutcomeUnknownError,
    PairRules,
)

PAPER_RULES = {
    "XBTEUR": PairRules("XXBTZEUR", "XBTEUR", "XXBT", "ZEUR", 8, 1, 0.1, 0.0001, 0.5),
    "ETHEUR": PairRules("XETHZEUR", "ETHEUR", "XETH", "ZEUR", 8, 2, 0.01, 0.01, 0.5),
    "SOLEUR": PairRules("SOLEUR", "SOLEUR", "SOL", "ZEUR", 8, 2, 0.01, 0.02, 0.5),
    "XRPEUR": PairRules("XXRPZEUR", "XRPEUR", "XXRP", "ZEUR", 8, 5, 0.00001, 2.0, 0.5),
}
"""Beispielwerte für Tests – im Live-Betrieb werden die Regeln immer von Kraken geladen."""


def generic_rules(altname: str, price: float, base: str | None = None) -> PairRules:
    """Plausible Handelsregeln für Instrumente ohne Börsenregeln (z. B. synthetische Daten)."""
    decimals = max(1, min(8, 6 - int(math.floor(math.log10(max(price, 1e-8))))))
    tick = 10.0**-decimals
    return PairRules(
        altname,
        altname,
        base or altname.removesuffix("EUR"),
        "ZEUR",
        8,
        decimals,
        tick,
        ordermin=round(5.0 / price, 8) or 1e-8,
        costmin=0.5,
    )


@dataclass
class PaperExchange:
    rules: dict[str, PairRules] = field(default_factory=lambda: dict(PAPER_RULES))
    books: dict[str, OrderBook] = field(default_factory=dict)
    fees: FeeRates = field(default_factory=lambda: FeeRates(0.004, 0.0025, "paper (Annahme)"))
    balances_: dict[str, float] = field(default_factory=lambda: {"ZEUR": 10.0})
    native_bracket: bool = False
    fill_ratio: float = 1.0  # Anteil des Einstiegs, der ausgeführt wird (Teilausführung)
    reject_protection: bool = False  # Schutzorders werden nicht angelegt
    oco_broken: bool = False  # nach SL/TP wird die Gegenorder NICHT storniert
    withdrawal_permission: bool = False
    margin_positions: bool = False
    stop_slippage_bps: float = 20.0
    # Liegen SL und TP in derselben Kerze, ist die Reihenfolge aus OHLC nicht bestimmbar.
    # "stop_first" (Standard, konservativ) oder "take_profit_first" (nur als markierte
    # Obergrenze für Sensitivitätsanalysen). Betroffene Exits werden als ``ambiguous``
    # gekennzeichnet; es wird keine Tick-Reihenfolge erfunden.
    ambiguous_policy: str = "stop_first"
    fail_next: str | None = None
    orders: dict[str, ExchangeOrder] = field(default_factory=dict)
    name: str = "paper"
    is_live: bool = False
    next_id: int = 1

    # -- Fähigkeiten ------------------------------------------------------------------
    @property
    def capabilities(self) -> GatewayCapabilities:
        if self.native_bracket:
            return GatewayCapabilities(True, "Testattrappe: hypothetischer API-Bracket")
        return GatewayCapabilities(False, "Paper bildet Kraken-Spot ab: kein API-Bracket (OCO)")

    # -- Marktdaten ----------------------------------------------------------------
    def _rules_for(self, pair: str) -> PairRules:
        for r in self.rules.values():
            if pair in (r.altname, r.pair):
                return r
        raise OrderRejectedError(f"EQuery:Unknown asset pair {pair}")

    def pair_rules(self, pair: str) -> PairRules:
        return self._rules_for(pair)

    def set_book(self, pair: str, bid: float, ask: float, depth: float = 10.0) -> None:
        tick = self._rules_for(pair).tick_size
        bids = [(round(bid - i * tick, 10), depth) for i in range(25)]
        asks = [(round(ask + i * tick, 10), depth) for i in range(25)]
        self.books[self._rules_for(pair).altname] = OrderBook(pair, bids, asks)

    def order_book(self, pair: str, depth: int = 25) -> OrderBook:
        book = self.books[self._rules_for(pair).altname]
        return OrderBook(book.pair, book.bids[:depth], book.asks[:depth])

    def fee_rates(self, pair: str) -> FeeRates:
        return self.fees

    # -- Konto --------------------------------------------------------------------------
    def balances(self) -> dict[str, Balance]:
        held: dict[str, float] = {}
        for o in self.orders.values():
            if o.status is OrderStatus.OPEN and o.side is OrderSide.SELL:
                base = self._rules_for(o.pair).base
                # SL und TP eines Brackets binden denselben Bestand nur einmal
                if o.role is LegRole.TAKE_PROFIT:
                    continue
                held[base] = held.get(base, 0.0) + o.volume - o.filled
        return {a: Balance(a, v, held.get(a, 0.0)) for a, v in self.balances_.items()}

    def open_orders(self) -> list[ExchangeOrder]:
        return [replace(o) for o in self.orders.values() if o.status is OrderStatus.OPEN]

    def orders_by_client_ref(self, client_ref: int) -> list[ExchangeOrder]:
        return [replace(o) for o in self.orders.values() if o.client_ref == client_ref]

    def has_withdrawal_permission(self) -> bool:
        return self.withdrawal_permission

    def has_margin_positions(self) -> bool:
        return self.margin_positions

    # -- Orders -------------------------------------------------------------------------
    def _new_id(self) -> str:
        txid = f"PAPER-{self.next_id:06d}"
        self.next_id += 1
        return txid

    def submit_bracket(self, request: BracketRequest) -> str:
        if not self.native_bracket:
            raise BracketNotSupportedError(self.capabilities.evidence)
        mode, self.fail_next = self.fail_next, None
        if mode == "reject":
            raise OrderRejectedError("EOrder:Insufficient funds")
        if mode == "timeout_before_accept":
            raise OutcomeUnknownError("Timeout (Order nicht angekommen)")
        rules = self._rules_for(request.pair)
        self._validate(rules, request)
        txid = self._execute_entry(rules, request)
        if mode == "timeout_after_accept":
            raise OutcomeUnknownError("Timeout (Order wurde trotzdem ausgeführt)")
        return txid

    def _validate(self, rules: PairRules, req: BracketRequest) -> None:
        if req.volume < rules.ordermin:
            raise OrderRejectedError("EOrder:Order minimum not met")
        for price in (req.entry_limit_price, req.stop_loss_price, req.take_profit_price):
            if req.volume * price < rules.costmin:
                raise OrderRejectedError("EOrder:Cost minimum not met")
            if not rules.is_on_tick(price):
                raise OrderRejectedError("EOrder:Invalid price")
        if rules.round_volume_down(req.volume) != req.volume:
            raise OrderRejectedError("EGeneral:Invalid arguments:volume")
        cost = req.volume * req.entry_limit_price * (1 + self.fees.taker)
        if self.balances_.get(rules.quote, 0.0) < cost:
            raise OrderRejectedError("EOrder:Insufficient funds")

    def _execute_entry(self, rules: PairRules, req: BracketRequest) -> str:
        book = self.books[rules.altname]
        target = rules.round_volume_down(req.volume * self.fill_ratio)
        filled, cost = 0.0, 0.0
        for price, qty in book.asks:
            if price > req.entry_limit_price or filled >= target:
                break
            take = min(qty, target - filled)
            filled += take
            cost += take * price
        filled = rules.round_volume_down(filled)
        avg = cost / filled if filled else 0.0
        fee = cost * self.fees.taker
        txid = self._new_id()
        self.orders[txid] = ExchangeOrder(
            txid,
            req.client_ref,
            rules.altname,
            OrderSide.BUY,
            "limit",
            OrderStatus.CLOSED if filled >= req.volume else OrderStatus.CANCELED,  # IOC
            req.volume,
            filled,
            avg,
            fee,
            req.entry_limit_price,
            None,
            LegRole.ENTRY,
        )
        if filled > 0:
            self.balances_[rules.quote] -= cost + fee
            self.balances_[rules.base] = self.balances_.get(rules.base, 0.0) + filled
            if not self.reject_protection:
                for role, otype, price in (
                    (LegRole.STOP_LOSS, "stop-loss", req.stop_loss_price),
                    (LegRole.TAKE_PROFIT, "take-profit-limit", req.take_profit_price),
                ):
                    leg = self._new_id()
                    self.orders[leg] = ExchangeOrder(
                        leg,
                        req.client_ref,
                        rules.altname,
                        OrderSide.SELL,
                        otype,
                        OrderStatus.OPEN,
                        filled,
                        0.0,
                        0.0,
                        0.0,
                        price,
                        txid,
                        role,
                    )
        return txid

    def cancel_order(self, txid: str) -> None:
        order = self.orders.get(txid)
        if order is None:
            raise OrderRejectedError("EOrder:Unknown order")
        if order.status is OrderStatus.OPEN:
            order.status = OrderStatus.CANCELED

    # -- Marktbewegung (löst SL/TP aus) ------------------------------------------------
    def move_market(self, pair: str, bid: float, ask: float) -> None:
        self.set_book(pair, bid, ask)
        rules = self._rules_for(pair)
        legs = [
            o
            for o in self.orders.values()
            if o.status is OrderStatus.OPEN and o.pair == rules.altname and o.role
        ]
        for leg in legs:
            if leg.status is not OrderStatus.OPEN:
                continue
            hit = (leg.role is LegRole.STOP_LOSS and bid <= leg.price) or (
                leg.role is LegRole.TAKE_PROFIT and bid >= leg.price
            )
            if not hit:
                continue
            if leg.role is LegRole.STOP_LOSS:
                px = min(bid, leg.price) * (1 - self.stop_slippage_bps * 1e-4)
                fee_rate = self.fees.taker
            else:
                px, fee_rate = leg.price, self.fees.maker
            leg.status, leg.filled, leg.avg_price = OrderStatus.CLOSED, leg.volume, px
            leg.fee = leg.volume * px * fee_rate
            self.balances_[rules.base] -= leg.volume
            self.balances_[rules.quote] += leg.volume * px - leg.fee
            if not self.oco_broken:
                for sib in legs:
                    if sib is not leg and sib.parent_txid == leg.parent_txid:
                        sib.status = OrderStatus.CANCELED

    # -- Kerzenbasierte Ausführung (autonomer Paper-Kreislauf) ----------------------------
    def process_bar(self, pair: str, open_: float, high: float, low: float, close: float) -> int:
        """Löst SL/TP offener Schutzorders anhand einer Kerze aus. Gibt Anzahl Exits zurück."""
        rules = self._rules_for(pair)
        legs = [
            o
            for o in self.orders.values()
            if o.status is OrderStatus.OPEN and o.pair == rules.altname and o.role
        ]
        parents = {o.parent_txid for o in legs}
        exits = 0
        for parent in parents:
            group = [o for o in legs if o.parent_txid == parent and o.status is OrderStatus.OPEN]
            sl = next((o for o in group if o.role is LegRole.STOP_LOSS), None)
            tp = next((o for o in group if o.role is LegRole.TAKE_PROFIT), None)
            fill: tuple[ExchangeOrder, float, float] | None = None
            slip = 1 - self.stop_slippage_bps * 1e-4
            ambiguous = (
                sl is not None
                and tp is not None
                and low <= sl.price
                and high >= tp.price
                and sl.price < open_ < tp.price
            )
            if sl is not None and open_ <= sl.price:
                fill = (sl, open_ * slip, self.fees.taker)  # Kurslücke unter den Stop
            elif tp is not None and open_ >= tp.price:
                fill = (tp, open_, self.fees.maker)  # Kurslücke über das Ziel
            elif ambiguous and self.ambiguous_policy == "take_profit_first":
                fill = (tp, tp.price, self.fees.maker)  # nur als Obergrenze
            elif sl is not None and low <= sl.price:
                fill = (sl, sl.price * slip, self.fees.taker)  # konservativ: Stop zuerst
            elif tp is not None and high >= tp.price:
                fill = (tp, tp.price, self.fees.maker)
            if fill is None:
                continue
            leg, px, fee_rate = fill
            leg.ambiguous = ambiguous
            leg.status, leg.filled, leg.avg_price = OrderStatus.CLOSED, leg.volume, px
            leg.fee = leg.volume * px * fee_rate
            self.balances_[rules.base] = self.balances_.get(rules.base, 0.0) - leg.volume
            self.balances_[rules.quote] += leg.volume * px - leg.fee
            exits += 1
            if not self.oco_broken:
                for sib in group:
                    if sib is not leg:
                        sib.status = OrderStatus.CANCELED
        self.set_book(pair, close * (1 - 1e-4), close * (1 + 1e-4))
        return exits

    # -- Persistenz -----------------------------------------------------------------------
    def to_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "rules": {k: asdict(v) for k, v in self.rules.items()},
            "fees": asdict(self.fees),
            "balances": dict(self.balances_),
            "native_bracket": self.native_bracket,
            "stop_slippage_bps": self.stop_slippage_bps,
            "ambiguous_policy": self.ambiguous_policy,
            "next_id": self.next_id,
            "orders": [
                {
                    **asdict(o),
                    "side": str(o.side),
                    "status": str(o.status),
                    "role": str(o.role) if o.role else None,
                }
                for o in self.orders.values()
            ],
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> PaperExchange:
        if state.get("version") != 1:
            raise ValueError("Unbekannte oder beschädigte Paper-Börsen-Zustandsdatei")
        ex = cls(
            rules={k: PairRules(**v) for k, v in state["rules"].items()},
            fees=FeeRates(**state["fees"]),
            balances_=dict(state["balances"]),
            native_bracket=bool(state["native_bracket"]),
            stop_slippage_bps=float(state["stop_slippage_bps"]),
            ambiguous_policy=str(state.get("ambiguous_policy", "stop_first")),
            next_id=int(state["next_id"]),
        )
        for raw in state["orders"]:
            order = ExchangeOrder(
                **{
                    **raw,
                    "side": OrderSide(raw["side"]),
                    "status": OrderStatus(raw["status"]),
                    "role": LegRole(raw["role"]) if raw["role"] else None,
                }
            )
            ex.orders[order.txid] = order
        return ex
