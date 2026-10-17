"""Market Scanner: findet handelbare Kraken-Spot-Paare, die zum Kapital passen.

Es werden nur Instrumente ausgewählt, die über den Broker tatsächlich handelbar
sind (Status ``online``, EUR-Quote, keine Fiat-/Stablecoin-Basis). Gefiltert wird nach:

* kleinste zulässige Order (``ordermin`` × Ask bzw. ``costmin``) inkl. Gebühr ≤ Kapital
* Spread ≤ ``max_spread_bps``
* 24h-Handelsvolumen in EUR ≥ ``min_quote_volume_24h``

Jede Ablehnung wird mit Grund protokolliert. Der Scanner trifft keine
Handelsentscheidung; er liefert nur das Universum für die Signalsuche.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tradikg_bot.costs import KRAKEN_SPOT_TAKER
from tradikg_bot.live.models import PairRules
from tradikg_bot.market.kraken_data import KrakenMarketData, Quote


@dataclass(frozen=True)
class ScanConfig:
    capital_eur: float = 100.0
    max_spread_bps: float = 30.0
    min_quote_volume_24h: float = 250_000.0
    assumed_taker_fee: float = KRAKEN_SPOT_TAKER
    max_candidates: int | None = None  # Begrenzung der Datenabrufe, KEINE Trade-Grenze


@dataclass(frozen=True)
class ScanCandidate:
    rules: PairRules
    quote: Quote
    min_order_eur: float
    score: float


@dataclass
class ScanResult:
    candidates: list[ScanCandidate] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)


def scan_markets(market: KrakenMarketData, cfg: ScanConfig) -> ScanResult:
    result = ScanResult()
    pairs = market.tradable_pairs("ZEUR")
    quotes = market.ticker([p.altname for p in pairs]) if pairs else {}
    for rules in pairs:
        quote = quotes.get(rules.pair) or quotes.get(rules.altname)
        name = rules.altname
        if quote is None:
            result.rejected[name] = "kein Ticker"
            continue
        min_order = max(rules.ordermin * quote.ask, rules.costmin) * (1 + cfg.assumed_taker_fee)
        if min_order > cfg.capital_eur:
            result.rejected[name] = f"Mindestorder {min_order:.2f} EUR > Kapital"
            continue
        if quote.spread_bps > cfg.max_spread_bps:
            result.rejected[name] = f"Spread {quote.spread_bps:.1f} bps"
            continue
        if quote.quote_volume_24h < cfg.min_quote_volume_24h:
            result.rejected[name] = f"24h-Volumen {quote.quote_volume_24h:,.0f} EUR zu gering"
            continue
        # Liquidere Märkte mit engem Spread zuerst
        score = quote.quote_volume_24h / (1.0 + quote.spread_bps)
        result.candidates.append(ScanCandidate(rules, quote, min_order, score))
    result.candidates.sort(key=lambda c: c.score, reverse=True)
    if cfg.max_candidates is not None:
        for c in result.candidates[cfg.max_candidates :]:
            result.rejected[c.rules.altname] = "außerhalb der Datenabruf-Begrenzung"
        result.candidates = result.candidates[: cfg.max_candidates]
    return result
