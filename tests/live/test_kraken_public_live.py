"""Optionale Integrationstests gegen die ECHTE öffentliche Kraken-API (nur lesend).

Aktivierung: ``KRAKEN_PUBLIC_INTEGRATION=1 pytest tests/live/test_kraken_public_live.py``
Es werden ausschließlich öffentliche Endpunkte genutzt, ohne API-Key und ohne Orders.
"""

from __future__ import annotations

import os

import pytest

from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.kraken.client import KrakenClient
from tradikg_bot.live.kraken.gateway import KrakenSpotGateway

pytestmark = pytest.mark.skipif(
    os.environ.get("KRAKEN_PUBLIC_INTEGRATION") != "1",
    reason="Opt-in: KRAKEN_PUBLIC_INTEGRATION=1 (benötigt Internetzugang zu api.kraken.com)",
)


@pytest.mark.parametrize("pair", LiveRiskConfig().allowed_pairs)
def test_public_rules_and_book(pair):
    gw = KrakenSpotGateway(KrakenClient())
    rules = gw.pair_rules(pair)
    book = gw.order_book(pair, 10)
    assert rules.quote in ("ZEUR", "EUR") and rules.ordermin > 0 and rules.costmin > 0
    assert book.best_bid < book.best_ask
    print(
        f"{pair}: ordermin {rules.ordermin} {rules.base} ≈ "
        f"{rules.ordermin * book.best_ask:.2f} EUR, costmin {rules.costmin} EUR, "
        f"tick {rules.tick_size}, spread {book.spread_bps:.1f} bps"
    )
