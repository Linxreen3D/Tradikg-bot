"""Signalbewertung und Ranking.

Für jedes Einstiegssignal wird ein **Netto-Erwartungswert** berechnet:

    EV = p_fill · ( p_win · Gewinn_netto(TP) − (1 − p_win) · Verlust_netto(SL) )

* ``Gewinn_netto``: TP-Abstand abzüglich Einstiegs-Spread/Slippage und Gebühren
  (Einstieg Taker, Ausstieg Maker-Limit).
* ``Verlust_netto``: SL-Abstand plus angenommene Stop-Slippage und Gebühren
  (Einstieg und Ausstieg Taker).
* ``p_fill``: geschätzte Ausführungswahrscheinlichkeit aus der Liquidität
  (Ordervolumen relativ zum Kerzenvolumen).

Nur Signale mit ``EV > 0`` (nach Kosten) und ausreichender Datenqualität sind
Kandidaten. Rangfolge: erwarteter Gewinn je riskiertem Euro (``EV / Verlust``),
reduziert bei hoher Korrelation zu offenen Positionen. Es gibt **keine** feste
Höchstzahl von Trades; begrenzt wird ausschließlich über Kapital und Risiko.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from tradikg_bot.live.models import FeeRates

BPS = 1e-4


@dataclass
class SignalCandidate:
    symbol: str
    strategy: str
    signal_ts: pd.Timestamp
    ref_price: float
    stop_distance: float
    take_distance: float
    regime: str
    p_win: float = 0.0
    p_source: str = ""
    model_version: str | None = None
    p_fill: float = 1.0
    win_net_per_unit: float = 0.0
    loss_net_per_unit: float = 0.0
    ev_per_unit: float = 0.0
    correlation_penalty: float = 0.0
    rejected: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def reward_risk(self) -> float:
        return self.take_distance / self.stop_distance if self.stop_distance > 0 else 0.0

    @property
    def expected_r(self) -> float:
        """Erwarteter Netto-Gewinn je riskiertem Euro (nach Kosten)."""
        if self.loss_net_per_unit <= 0:
            return 0.0
        return self.ev_per_unit / self.loss_net_per_unit

    @property
    def score(self) -> float:
        return self.expected_r * (1.0 - self.correlation_penalty)

    @property
    def idempotency_key(self) -> str:
        return f"{self.symbol}|{self.strategy}|{self.signal_ts.isoformat()}"


def evaluate_costs(
    c: SignalCandidate,
    fees: FeeRates,
    spread_bps: float,
    entry_slippage_bps: float,
    stop_slippage_bps: float,
) -> None:
    """Füllt Netto-Gewinn/-Verlust je Einheit und EV (ohne p_fill-Anpassung der Kosten)."""
    entry = c.ref_price * (1 + (spread_bps / 2 + entry_slippage_bps) * BPS)
    take = entry + c.take_distance
    stop = entry - c.stop_distance
    stop_fill = stop * (1 - stop_slippage_bps * BPS)
    c.win_net_per_unit = take * (1 - fees.maker) - entry * (1 + fees.taker)
    c.loss_net_per_unit = entry * (1 + fees.taker) - stop_fill * (1 - fees.taker)
    ev = c.p_win * c.win_net_per_unit - (1 - c.p_win) * c.loss_net_per_unit
    c.ev_per_unit = c.p_fill * ev


def fill_probability(
    order_notional: float, bar_quote_volume: float, max_participation: float = 0.01
) -> float:
    """1, solange die Order höchstens ``max_participation`` des Kerzenvolumens ausmacht."""
    if bar_quote_volume <= 0:
        return 0.0
    share = order_notional / bar_quote_volume
    return 1.0 if share <= max_participation else max(0.0, max_participation / share)


def correlation_penalty(
    candidate_returns: pd.Series, open_returns: dict[str, pd.Series], threshold: float = 0.7
) -> float:
    """Höchste Korrelation (≥ Schwelle) zu einer offenen Position, sonst 0."""
    worst = 0.0
    for other in open_returns.values():
        joined = pd.concat([candidate_returns, other], axis=1).dropna()
        if len(joined) < 30:
            continue
        rho = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
        if rho >= threshold:
            worst = max(worst, rho)
    return worst


def rank(candidates: list[SignalCandidate]) -> list[SignalCandidate]:
    eligible = [c for c in candidates if c.rejected is None and c.ev_per_unit > 0]
    return sorted(eligible, key=lambda c: (c.score, c.p_fill), reverse=True)
