"""Datenquellen-Schnittstelle und Handelszeiten.

Jede Quelle beschreibt ehrlich, was sie liefert:

* ``realtime``: Sind die Daten zeitnah genug für Handelsentscheidungen?
* ``execution_venue``: Kann über diese Quelle auch gehandelt werden?

Aktien sind vorbereitet, aber **nicht** handelbar: Die kostenlose Yahoo-Quelle
(``yfinance``) ist inoffiziell und verzögert und wird deshalb nie als
Echtzeit-Handelsquelle behandelt. Ein Aktienbroker ist noch nicht angebunden.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from tradikg_bot.domain import AssetClass

BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class DataSourceInfo:
    name: str
    asset_class: AssetClass
    realtime: bool
    execution_venue: bool
    official: bool
    notes: str


KRAKEN_SPOT = DataSourceInfo(
    "kraken-spot",
    AssetClass.CRYPTO,
    realtime=True,
    execution_venue=True,
    official=True,
    notes="Öffentliche REST-API (OHLC max. 720 Kerzen je Abruf, Ticker, Orderbuch).",
)
YAHOO_STOCKS = DataSourceInfo(
    "yahoo-finance",
    AssetClass.STOCK,
    realtime=False,
    execution_venue=False,
    official=False,
    notes="Inoffizielle, verzögerte Daten (yfinance) – nur für Research, nie für Live-Signale.",
)
SOURCES = {s.name: s for s in (KRAKEN_SPOT, YAHOO_STOCKS)}


def usable_for_trading(source: DataSourceInfo) -> bool:
    return source.realtime and source.execution_venue and source.official


def is_market_open(asset_class: AssetClass, ts: dt.datetime) -> bool:
    """Krypto: 24/7. Aktien: Xetra-Kernhandelszeit Mo–Fr 09:00–17:30 Europe/Berlin.

    Feiertage sind **nicht** berücksichtigt (vereinfachte Annahme, siehe Doku).
    """
    if asset_class is AssetClass.CRYPTO:
        return True
    local = ts.astimezone(BERLIN)
    if local.weekday() >= 5:
        return False
    return dt.time(9, 0) <= local.time() < dt.time(17, 30)
