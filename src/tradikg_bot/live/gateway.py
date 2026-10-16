"""Schnittstelle zwischen Ausführungslogik und Börse (live oder paper)."""

from __future__ import annotations

from typing import Protocol

from tradikg_bot.live.models import (
    Balance,
    BracketRequest,
    ExchangeOrder,
    FeeRates,
    GatewayCapabilities,
    OrderBook,
    PairRules,
)


class ExchangeGateway(Protocol):
    name: str
    is_live: bool

    @property
    def capabilities(self) -> GatewayCapabilities: ...

    def pair_rules(self, pair: str) -> PairRules: ...

    def order_book(self, pair: str, depth: int = 25) -> OrderBook: ...

    def fee_rates(self, pair: str) -> FeeRates: ...

    def balances(self) -> dict[str, Balance]: ...

    def open_orders(self) -> list[ExchangeOrder]: ...

    def orders_by_client_ref(self, client_ref: int) -> list[ExchangeOrder]:
        """Alle (offenen und geschlossenen) Orders mit dieser Client-Referenz inkl. Schutzorders."""
        ...

    def submit_bracket(self, request: BracketRequest) -> str:
        """Sendet Einstieg + SL + TP atomar. Gibt die Txid des Einstiegs zurück.

        Raises:
            OrderRejectedError: eindeutig abgelehnt, keine Order entstanden.
            OutcomeUnknownError: Ergebnis unklar – Abgleich über ``client_ref`` nötig.
            BracketNotSupportedError: Börse bietet keinen API-Bracket.
        """
        ...

    def cancel_order(self, txid: str) -> None: ...

    def has_withdrawal_permission(self) -> bool: ...

    def has_margin_positions(self) -> bool: ...
