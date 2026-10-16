"""Integrationstests über echtes HTTP gegen einen lokalen Kraken-Emulator.

Der Emulator prüft die Signatur jeder privaten Anfrage (wie Kraken) und liefert
Antworten im Format der offiziellen Kraken-REST-API. So werden Transport,
Signatur, Nonce, Parsing, Fehlerklassen und Timeouts Ende-zu-Ende getestet,
ohne echtes Geld und ohne Netzwerkzugriff nach außen.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.gate import LiveApproval, LiveTradingBlockedError, check_live_gate
from tradikg_bot.live.kraken.client import (
    KrakenClient,
    KrakenRejectedError,
    KrakenUnknownOutcomeError,
    sign,
)
from tradikg_bot.live.kraken.credentials import KrakenCredentials
from tradikg_bot.live.kraken.gateway import KrakenSpotGateway
from tradikg_bot.live.models import BracketNotSupportedError, BracketRequest, OrderStatus
from tradikg_bot.live.precheck import current_exposure

from live_helpers import DOC_SECRET

ASSET_PAIRS = {
    "XXBTZEUR": {
        "altname": "XBTEUR",
        "wsname": "XBT/EUR",
        "base": "XXBT",
        "quote": "ZEUR",
        "pair_decimals": 1,
        "cost_decimals": 5,
        "lot_decimals": 8,
        "lot_multiplier": 1,
        "leverage_buy": [2, 3],
        "leverage_sell": [2, 3],
        "fees": [],
        "fees_maker": [],
        "ordermin": "0.00005",
        "costmin": "0.5",
        "tick_size": "0.1",
        "status": "online",
    }
}
DEPTH = {
    "XXBTZEUR": {
        "bids": [["60000.0", "0.5", 1], ["59990.0", "1.0", 1]],
        "asks": [["60010.0", "0.4", 1], ["60020.0", "2.0", 1]],
    }
}


class KrakenEmulator(BaseHTTPRequestHandler):
    seen: list[dict] = []
    withdraw_allowed = False
    slow_endpoints: set[str] = set()

    def log_message(self, *args):  # still
        pass

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        if path == "/0/public/AssetPairs":
            return self._reply({"error": [], "result": ASSET_PAIRS})
        if path == "/0/public/Depth":
            assert params["pair"] == "XBTEUR"
            return self._reply({"error": [], "result": DEPTH})
        return self._reply({"error": ["EGeneral:Unknown method"]})

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        raw = self.rfile.read(length).decode()
        data = dict(urllib.parse.parse_qsl(raw))
        expected = sign(self.path, data, DOC_SECRET)
        if self.headers.get("API-Sign") != expected:
            return self._reply({"error": ["EAPI:Invalid signature"]})
        endpoint = self.path.rsplit("/", 1)[-1]
        self.seen.append({"endpoint": endpoint, **data})
        if endpoint in self.slow_endpoints:
            time.sleep(1.0)
        handlers = {
            "BalanceEx": lambda: {
                "ZEUR": {"balance": "10.0000", "hold_trade": "0.0000"},
                "XXBT": {"balance": "0.0000000000", "hold_trade": "0"},
            },
            "OpenOrders": lambda: {"open": {}},
            "ClosedOrders": lambda: {
                "closed": {
                    "OABC-1": {
                        "userref": int(data.get("userref", 0)),
                        "status": "canceled",
                        "vol": "0.0001",
                        "vol_exec": "0.0000",
                        "price": "0",
                        "fee": "0",
                        "descr": {
                            "pair": "XBTEUR",
                            "type": "buy",
                            "ordertype": "limit",
                            "price": "60100.0",
                        },
                    }
                }
                if data.get("userref")
                else {}
            },
            "OpenPositions": lambda: {},
            "TradeVolume": lambda: {
                "currency": "ZUSD",
                "volume": "0.0000",
                "fees": {"XXBTZEUR": {"fee": "0.4000"}},
                "fees_maker": {"XXBTZEUR": {"fee": "0.2500"}},
            },
            "AddOrder": lambda: (
                {"descr": {"order": "buy 0.00010000 XBTEUR @ limit 54000.0"}}
                if data.get("validate") == "true"
                else None
            ),
        }
        if endpoint == "WithdrawMethods":
            if self.withdraw_allowed:
                return self._reply({"error": [], "result": [{"asset": "XXBT"}]})
            return self._reply({"error": ["EGeneral:Permission denied"]})
        result = handlers.get(endpoint, lambda: None)()
        if result is None:
            return self._reply({"error": ["EGeneral:Invalid arguments"]})
        return self._reply({"error": [], "result": result})


@pytest.fixture
def emulator(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    KrakenEmulator.seen = []
    KrakenEmulator.withdraw_allowed = False
    KrakenEmulator.slow_endpoints = set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), KrakenEmulator)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def gateway(base_url, timeout=5.0, allow_trading=False):
    nonces = iter(range(10**15, 10**15 + 1000))
    client = KrakenClient(
        KrakenCredentials("test-key", DOC_SECRET),
        lambda: next(nonces),
        base_url=base_url,
        timeout=timeout,
        allow_trading=allow_trading,
    )
    return KrakenSpotGateway(client)


def test_public_market_rules_and_book(emulator):
    gw = gateway(emulator)
    rules = gw.pair_rules("XBTEUR")
    assert (rules.ordermin, rules.costmin, rules.tick_size) == (0.00005, 0.5, 0.1)
    assert rules.margin_available  # Paar bietet Margin -> wir nutzen sie dennoch nie
    book = gw.order_book("XBTEUR")
    assert book.best_bid == 60000.0 and book.best_ask == 60010.0
    assert book.buy_vwap(1.0) == pytest.approx((0.4 * 60010 + 0.6 * 60020) / 1.0)


def test_private_reads_are_signed_and_parsed(emulator):
    gw = gateway(emulator)
    fees = gw.fee_rates("XBTEUR")
    assert (fees.taker, fees.maker) == (0.004, 0.0025)
    assert gw.balances()["ZEUR"].available == 10.0
    assert gw.open_orders() == []
    assert gw.has_margin_positions() is False
    assert gw.has_withdrawal_permission() is False
    exposure, _ = current_exposure(gw, LiveRiskConfig(allowed_pairs=("XBTEUR",)))
    assert exposure == 0.0
    nonces = [int(c["nonce"]) for c in KrakenEmulator.seen]
    assert nonces == sorted(nonces) and len(set(nonces)) == len(nonces)


def test_orders_by_client_ref_uses_userref(emulator):
    orders = gateway(emulator).orders_by_client_ref(4242)
    assert orders[0].client_ref == 4242 and orders[0].status is OrderStatus.CANCELED
    assert {"OpenOrders", "ClosedOrders"} <= {c["endpoint"] for c in KrakenEmulator.seen}


def test_validate_only_order_needs_no_trading_permission(emulator):
    result = gateway(emulator, allow_trading=False).validate_entry_order("XBTEUR", 0.0001, 54000.0)
    assert "descr" in result
    sent = KrakenEmulator.seen[-1]
    assert sent["validate"] == "true" and sent["timeinforce"] == "IOC"
    assert "leverage" not in sent


def test_bracket_is_refused_without_any_request(emulator):
    gw = gateway(emulator, allow_trading=True)
    with pytest.raises(BracketNotSupportedError):
        gw.submit_bracket(BracketRequest(1, "XBTEUR", 0.0001, 60100.0, 58000.0, 64000.0))
    assert all(c["endpoint"] != "AddOrder" for c in KrakenEmulator.seen)


def test_live_gate_blocks_kraken_even_with_valid_approval(emulator):
    import datetime as dt

    gw = gateway(emulator, allow_trading=True)
    today = dt.date(2026, 10, 17)
    approval = LiveApproval(True, "ECHTGELD-FREIGABE 2026-10-17 MAX 10.00 EUR XBTEUR")
    with pytest.raises(LiveTradingBlockedError) as err:
        check_live_gate(gw, approval, "XBTEUR", 10.0, today=today)
    assert len(err.value.reasons) == 1 and "Bracket" in err.value.reasons[0]


def test_withdrawal_permission_is_detected(emulator):
    KrakenEmulator.withdraw_allowed = True
    assert gateway(emulator).has_withdrawal_permission() is True


def test_timeout_is_unknown_outcome(emulator):
    KrakenEmulator.slow_endpoints = {"BalanceEx"}
    with pytest.raises(KrakenUnknownOutcomeError):
        gateway(emulator, timeout=0.2).balances()


def test_wrong_secret_is_rejected_by_server(emulator):
    other = "A" * 86 + "=="
    client = KrakenClient(KrakenCredentials("k", other), lambda: 1, base_url=emulator)
    with pytest.raises(KrakenRejectedError, match="Invalid signature"):
        client.private("BalanceEx")
