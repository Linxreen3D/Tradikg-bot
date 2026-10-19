"""Kraken-Pro-Spot-Gateway.

Wichtigste Feststellung (Recherche Stand 16.10.2026, siehe ``docs/KRAKEN_LIVE_PILOT.md``):
Die offizielle Spot-API (REST ``AddOrder``/``AddOrderBatch``, WebSocket v2
``add_order``) bietet als verknüpfte Absicherung nur **Conditional Close**, also
eine One-Triggers-Other-Order mit genau **einem** Folgeauftrag. Ein
Stop-Loss/Take-Profit-Bracket mit OCO-Verknüpfung ist laut Kraken nur in der
Kraken-Pro-Oberfläche dokumentiert, ohne API-Parameter. Deshalb meldet dieses
Gateway ``native_bracket=False`` und ``submit_bracket`` verweigert jede Ausführung.
Eine lokal simulierte Absicherung (Kursüberwachung durch den Bot) gibt es bewusst nicht.
"""

from __future__ import annotations

from typing import Any

from tradikg_bot.live.kraken.client import (
    KrakenClient,
    KrakenNotSentError,
    KrakenRejectedError,
    KrakenUnknownOutcomeError,
)
from tradikg_bot.live.models import (
    Balance,
    BracketNotSupportedError,
    BracketRequest,
    ExchangeOrder,
    FeeRates,
    GatewayCapabilities,
    OrderBook,
    OrderRejectedError,
    OrderSide,
    OrderStatus,
    OutcomeUnknownError,
    PairRules,
)

KRAKEN_SPOT_CAPABILITIES = GatewayCapabilities(
    native_bracket=False,
    evidence=(
        "Kraken-Spot-API: Conditional Close = OTO mit genau einem Folgeauftrag; "
        "laut Kraken-Support 'cannot be used to set both a profit target and stop loss'. "
        "TP/SL mit OCO ist nur für die Kraken-Pro-Oberfläche dokumentiert, "
        "nicht als API-Parameter."
    ),
    sources=(
        "https://docs.kraken.com/api/docs/rest-api/add-order",
        "https://docs.kraken.com/api/docs/websocket-v2/add_order",
        "https://support.kraken.com/articles/360038640052-conditional-close",
        "https://blog.kraken.com/product/pro/take-profit-stop-loss-spot-and-margin-trading",
    ),
)

_STATUS = {
    "pending": OrderStatus.PENDING,
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.CLOSED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_pair_rules(key: str, raw: dict[str, Any]) -> PairRules:
    return PairRules(
        pair=key,
        altname=raw["altname"],
        base=raw["base"],
        quote=raw["quote"],
        lot_decimals=int(raw["lot_decimals"]),
        pair_decimals=int(raw["pair_decimals"]),
        tick_size=_f(raw.get("tick_size"), 10 ** -int(raw["pair_decimals"])),
        ordermin=_f(raw.get("ordermin")),
        costmin=_f(raw.get("costmin")),
        status=str(raw.get("status", "online")),
        margin_available=bool(raw.get("leverage_buy") or raw.get("leverage_sell")),
    )


def parse_order(txid: str, raw: dict[str, Any]) -> ExchangeOrder:
    descr = raw.get("descr", {})
    price = _f(descr.get("price"), 0.0) or None
    return ExchangeOrder(
        txid=txid,
        client_ref=int(raw["userref"]) if raw.get("userref") not in (None, 0, "0") else None,
        pair=str(descr.get("pair", "")),
        side=OrderSide(descr.get("type", "buy")),
        ordertype=str(descr.get("ordertype", "")),
        status=_STATUS.get(str(raw.get("status")), OrderStatus.PENDING),
        volume=_f(raw.get("vol")),
        filled=_f(raw.get("vol_exec")),
        avg_price=_f(raw.get("price")),
        fee=_f(raw.get("fee")),
        price=price,
        opened_at=_f(raw["opentm"]) if raw.get("opentm") not in (None, "", 0, "0") else None,
    )


class KrakenSpotGateway:
    name = "kraken-spot"
    is_live = True

    def __init__(self, client: KrakenClient) -> None:
        self.client = client
        self._rules: dict[str, PairRules] = {}

    @property
    def capabilities(self) -> GatewayCapabilities:
        return KRAKEN_SPOT_CAPABILITIES

    # -- öffentliche Marktdaten ------------------------------------------------------
    def pair_rules(self, pair: str) -> PairRules:
        if pair not in self._rules:
            result = self.client.public("AssetPairs", {"pair": pair})
            key, raw = next(iter(result.items()))
            rules = parse_pair_rules(key, raw)
            self._rules[pair] = self._rules[rules.altname] = rules
        return self._rules[pair]

    def order_book(self, pair: str, depth: int = 25) -> OrderBook:
        result = self.client.public("Depth", {"pair": pair, "count": depth})
        raw = next(iter(result.values()))
        bids = [(_f(p), _f(v)) for p, v, *_ in raw.get("bids", [])]
        asks = [(_f(p), _f(v)) for p, v, *_ in raw.get("asks", [])]
        return OrderBook(pair, bids, asks)

    # -- private, lesend ---------------------------------------------------------------
    def fee_rates(self, pair: str) -> FeeRates:
        """Tatsächliche Kontogebühren via TradeVolume.

        AssetPairs liefert seit 08.09.2026 keine Gebühren mehr.
        """
        rules = self.pair_rules(pair)
        result = self.client.private("TradeVolume", {"pair": rules.altname})
        fees = result.get("fees") or {}
        fees_maker = result.get("fees_maker") or {}
        taker_raw = fees.get(rules.pair) or fees.get(rules.altname)
        if not taker_raw:
            raise RuntimeError(f"TradeVolume lieferte keine Gebühren für {pair}")
        maker_raw = fees_maker.get(rules.pair) or fees_maker.get(rules.altname) or taker_raw
        return FeeRates(
            taker=_f(taker_raw.get("fee")) / 100,
            maker=_f(maker_raw.get("fee")) / 100,
            source=f"Kraken TradeVolume (30-Tage-Volumen {result.get('volume', '?')})",
        )

    def balances(self) -> dict[str, Balance]:
        result = self.client.private("BalanceEx")
        return {
            asset: Balance(asset, _f(v.get("balance")), _f(v.get("hold_trade")))
            for asset, v in result.items()
        }

    def open_orders(self) -> list[ExchangeOrder]:
        result = self.client.private("OpenOrders")
        return [parse_order(txid, raw) for txid, raw in result.get("open", {}).items()]

    def orders_by_client_ref(self, client_ref: int) -> list[ExchangeOrder]:
        opened = self.client.private("OpenOrders", {"userref": client_ref}).get("open", {})
        closed = self.client.private("ClosedOrders", {"userref": client_ref}).get("closed", {})
        return [parse_order(t, r) for t, r in {**opened, **closed}.items()]

    def has_withdrawal_permission(self) -> bool:
        """Erwartet 'EGeneral:Permission denied'. Jede andere Antwort gilt als 'vorhanden'."""
        try:
            self.client.private("WithdrawMethods")
        except KrakenRejectedError as exc:
            if any("Permission denied" in e for e in exc.errors):
                return False
            raise
        return True

    def has_margin_positions(self) -> bool:
        return bool(self.client.private("OpenPositions"))

    def validate_entry_order(self, pair: str, volume: float, limit_price: float) -> dict[str, Any]:
        """Lässt Kraken eine Kauforder **nur validieren** (``validate=true``): keine Ausführung.

        Prüft serverseitig Paar, Mindestgrößen, Präzision und Berechtigungen.
        """
        rules = self.pair_rules(pair)
        return self.client.private(
            "AddOrder",
            {
                "pair": rules.altname,
                "type": "buy",
                "ordertype": "limit",
                "price": rules.format_price(limit_price),
                "volume": rules.format_volume(volume),
                "timeinforce": "IOC",
                "validate": "true",
            },
        )

    # -- private, schreibend -----------------------------------------------------------
    def submit_bracket(self, request: BracketRequest) -> str:
        raise BracketNotSupportedError(self.capabilities.evidence)

    def add_order(self, params: dict[str, Any], validate: bool = False) -> str | None:
        """Sendet eine einzelne Order (Spot, ohne Hebel). ``validate`` = Kraken prüft nur.

        Rückgabe: txid (bzw. ``None`` bei reiner Validierung). Unklare Ergebnisse werden als
        ``OutcomeUnknownError`` gemeldet – der Aufrufer prüft dann per ``userref`` nach.
        """
        data = {**params, **({"validate": "true"} if validate else {})}
        try:
            result = self.client.private("AddOrder", data)
        except KrakenRejectedError as exc:
            raise OrderRejectedError(str(exc)) from exc
        except (KrakenUnknownOutcomeError, KrakenNotSentError) as exc:
            raise OutcomeUnknownError(str(exc)) from exc
        txids = result.get("txid") or []
        return None if validate else txids[0]

    def query_orders(self, txids: list[str]) -> dict[str, ExchangeOrder]:
        if not txids:
            return {}
        result = self.client.private("QueryOrders", {"txid": ",".join(txids)})
        return {t: parse_order(t, raw) for t, raw in result.items()}

    def cancel_order(self, txid: str) -> None:
        try:
            self.client.private("CancelOrder", {"txid": txid})
        except KrakenRejectedError as exc:
            raise OrderRejectedError(str(exc)) from exc
        except (KrakenUnknownOutcomeError, KrakenNotSentError) as exc:
            raise OutcomeUnknownError(str(exc)) from exc
