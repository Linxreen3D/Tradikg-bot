"""Börsenunabhängige Datentypen für die Live-/Paper-Ausführung."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"  # vollständig ausgeführt
    CANCELED = "canceled"
    EXPIRED = "expired"

    @property
    def is_final(self) -> bool:
        return self in {OrderStatus.CLOSED, OrderStatus.CANCELED, OrderStatus.EXPIRED}


class LegRole(StrEnum):
    ENTRY = "entry"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


@dataclass(frozen=True)
class PairRules:
    """Handelsregeln eines Spot-Paares, wie sie die Börse veröffentlicht."""

    pair: str  # API-Name, z. B. "XXBTZEUR"
    altname: str  # z. B. "XBTEUR"
    base: str
    quote: str
    lot_decimals: int
    pair_decimals: int
    tick_size: float
    ordermin: float  # Mindestvolumen in Basiswährung
    costmin: float  # Mindestwert in Quote-Währung
    status: str = "online"
    margin_available: bool = False

    def round_volume_down(self, volume: float) -> float:
        q = Decimal(1).scaleb(-self.lot_decimals)
        return float(Decimal(repr(volume)).quantize(q, rounding=ROUND_FLOOR))

    def _round_to_tick(self, price: float, rounding: str) -> float:
        tick = Decimal(repr(self.tick_size))
        steps = (Decimal(repr(price)) / tick).to_integral_value(rounding=rounding)
        q = Decimal(1).scaleb(-self.pair_decimals)
        return float((steps * tick).quantize(q))

    def round_price_down(self, price: float) -> float:
        return self._round_to_tick(price, ROUND_FLOOR)

    def round_price_up(self, price: float) -> float:
        return self._round_to_tick(price, ROUND_CEILING)

    def is_on_tick(self, price: float) -> bool:
        steps = price / self.tick_size
        return math.isclose(steps, round(steps), rel_tol=0, abs_tol=1e-6)

    def format_price(self, price: float) -> str:
        return f"{price:.{self.pair_decimals}f}"

    def format_volume(self, volume: float) -> str:
        return f"{volume:.{self.lot_decimals}f}"


@dataclass(frozen=True)
class OrderBook:
    pair: str
    bids: list[tuple[float, float]]  # (Preis, Volumen) absteigend
    asks: list[tuple[float, float]]  # (Preis, Volumen) aufsteigend

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.best_ask - self.best_bid) / self.mid * 1e4

    def buy_vwap(self, volume: float) -> float | None:
        """Durchschnittspreis für einen Kauf von ``volume`` über die Ask-Seite (None = zu dünn)."""
        return _vwap(self.asks, volume)

    def sell_vwap(self, volume: float) -> float | None:
        return _vwap(self.bids, volume)


def _vwap(levels: list[tuple[float, float]], volume: float) -> float | None:
    remaining, cost = volume, 0.0
    for price, qty in levels:
        take = min(remaining, qty)
        cost += take * price
        remaining -= take
        if remaining <= 1e-15:
            return cost / volume
    return None


@dataclass(frozen=True)
class FeeRates:
    taker: float  # als Anteil, z. B. 0.008 = 0.80 %
    maker: float
    source: str


@dataclass(frozen=True)
class Balance:
    asset: str
    total: float
    held: float = 0.0  # in offenen Orders gebunden

    @property
    def available(self) -> float:
        return self.total - self.held


@dataclass
class ExchangeOrder:
    txid: str
    client_ref: int | None
    pair: str
    side: OrderSide
    ordertype: str
    status: OrderStatus
    volume: float
    filled: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    price: float | None = None  # Limit- bzw. Triggerpreis
    parent_txid: str | None = None
    role: LegRole | None = None
    ambiguous: bool = False  # Paper: SL und TP in derselben Kerze, Reihenfolge unbekannt
    opened_at: float | None = None  # Unix-Sekunden (Kraken ``opentm``); None = unbekannt


@dataclass(frozen=True)
class BracketRequest:
    """Einstieg (Kauf) + börsenseitig verknüpfte SL-/TP-Verkaufsorders (OCO)."""

    client_ref: int
    pair: str
    volume: float
    entry_limit_price: float  # marktfähiges Limit (IOC) – begrenzt die Slippage
    stop_loss_price: float
    take_profit_price: float


@dataclass(frozen=True)
class GatewayCapabilities:
    native_bracket: bool
    """True nur, wenn die Börse Einstieg + SL + TP mit OCO-Verknüpfung über die API annimmt."""
    evidence: str = ""
    sources: tuple[str, ...] = field(default_factory=tuple)


class ExecutionError(RuntimeError):
    pass


class OrderRejectedError(ExecutionError):
    """Die Börse hat die Order eindeutig abgelehnt – es ist nichts entstanden."""


class OutcomeUnknownError(ExecutionError):
    """Ergebnis unbekannt (Timeout, Verbindungsabbruch nach dem Senden, Serverfehler)."""


class BracketNotSupportedError(ExecutionError):
    """Die Börse bietet über die API keinen vollständigen SL/TP-Bracket an."""
