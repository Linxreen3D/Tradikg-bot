"""Harte Grenzen für den Echtgeld-Piloten (und dieselben Regeln für Paper-Konten).

Dieselbe Risikokonfiguration wird für Live und Paper verwendet. Nur eine
ausdrücklich als ``paper=True`` erzeugte Konfiguration darf die 10-€-Obergrenze
überschreiten; eine solche Konfiguration wird von jeder Live-Börse abgelehnt
(siehe ``LiveExecutor``).
"""

from __future__ import annotations

from dataclasses import dataclass

HARD_CAP_EUR = 10.0  # absolute Obergrenze – per Konfiguration nicht erhöhbar


@dataclass(frozen=True)
class LiveRiskConfig:
    pilot_capital_eur: float = HARD_CAP_EUR
    daily_loss_fraction: float = 0.10  # nur Bracket-Pilot; gilt nicht für live/trend_live.py
    max_loss_per_trade_eur: float = 0.50
    max_spread_bps: float = 30.0
    max_entry_slippage_bps: float = 30.0  # Abstand des marktfähigen IOC-Limits zum besten Ask
    assumed_stop_slippage_bps: float = 50.0  # Annahme: Stop-Market füllt so weit unter Trigger
    min_depth_multiple: float = 5.0  # Orderbuchtiefe im Limitband >= x * Ordervolumen
    min_reward_risk_after_costs: float = 1.0
    allowed_pairs: tuple[str, ...] = ("XBTEUR", "ETHEUR", "SOLEUR", "XRPEUR")
    quote_currency: str = "EUR"
    confirm_timeout_s: float = 30.0
    poll_interval_s: float = 1.0
    # Optional: Verlust je Trade als Anteil des einsetzbaren Kapitals (statt fester EUR)
    risk_per_trade_fraction: float | None = None
    paper: bool = False  # nur Paper-Konten dürfen > 10 EUR verwalten

    def __post_init__(self) -> None:
        if self.pilot_capital_eur <= 0 or (
            not self.paper and self.pilot_capital_eur > HARD_CAP_EUR
        ):
            raise ValueError(f"pilot_capital_eur muss in (0, {HARD_CAP_EUR}] liegen")
        if self.risk_per_trade_fraction is not None and not (
            0 < self.risk_per_trade_fraction <= self.daily_loss_fraction
        ):
            raise ValueError("risk_per_trade_fraction muss in (0, daily_loss_fraction] liegen")
        if not 0 < self.daily_loss_fraction <= 0.10:
            raise ValueError("daily_loss_fraction muss in (0, 0.10] liegen")
        if not 0 < self.max_loss_per_trade_eur <= self.daily_loss_limit_eur:
            raise ValueError("max_loss_per_trade_eur muss in (0, Tagesverlustlimit] liegen")
        for name in ("max_spread_bps", "max_entry_slippage_bps", "assumed_stop_slippage_bps"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} darf nicht negativ sein")
        if self.quote_currency != "EUR":
            raise ValueError("Der Pilot handelt ausschließlich EUR-Paare")

    @classmethod
    def for_paper(cls, capital_eur: float = 100.0, **overrides) -> LiveRiskConfig:
        """Paper-Konfiguration: gleiche Regeln, beliebiges virtuelles Kapital."""
        params = {
            "pilot_capital_eur": capital_eur,
            "max_loss_per_trade_eur": capital_eur * 0.01,
            "risk_per_trade_fraction": 0.01,
            "confirm_timeout_s": 1.0,
            "poll_interval_s": 0.0,
            "paper": True,
            **overrides,
        }
        return cls(**params)

    @property
    def daily_loss_limit_eur(self) -> float:
        """10 % des Pilot-Kapitals (fix, nicht des aktuellen Kontostands)."""
        return self.pilot_capital_eur * self.daily_loss_fraction

    def effective_cap(self, cumulative_realized_pnl: float) -> float:
        """Keine automatische Wiederauffüllung: Verluste senken die Obergrenze dauerhaft,
        Gewinne erhöhen sie nicht."""
        return max(
            0.0, min(self.pilot_capital_eur, self.pilot_capital_eur + cumulative_realized_pnl)
        )
