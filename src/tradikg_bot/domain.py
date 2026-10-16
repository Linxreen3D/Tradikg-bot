"""Gemeinsame Domänentypen (Anlageklassen, Instrumente, Exit-Gründe)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    STOCK = "stock"


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    SIGNAL = "signal"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    END_OF_DATA = "end_of_data"


@dataclass(frozen=True)
class Instrument:
    """Handelbares Instrument inkl. Handelsrestriktionen.

    ``qty_step`` ist die kleinste handelbare Mengeneinheit (z. B. 1 Aktie oder
    0.00001 BTC). ``min_notional`` ist der kleinste zulässige Ordernennwert in
    Kontowährung.
    """

    symbol: str
    asset_class: AssetClass
    qty_step: float
    min_notional: float = 0.0
    currency: str = "EUR"

    def __post_init__(self) -> None:
        if self.qty_step <= 0:
            raise ValueError("qty_step muss > 0 sein")
        if self.min_notional < 0:
            raise ValueError("min_notional darf nicht negativ sein")

    def round_qty(self, qty: float) -> float:
        """Rundet eine Menge auf das handelbare Raster ab (nie auf)."""
        if qty <= 0:
            return 0.0
        steps = math.floor(qty / self.qty_step + 1e-9)
        return round(steps * self.qty_step, 12)

    @classmethod
    def default_for(cls, symbol: str, asset_class: AssetClass | str) -> Instrument:
        """Konservative Standardrestriktionen je Anlageklasse."""
        asset_class = AssetClass(asset_class)
        if asset_class is AssetClass.CRYPTO:
            return cls(symbol, asset_class, qty_step=1e-6, min_notional=5.0)
        # Aktien: ganze Stücke (Bruchstücke sind bei vielen Brokern nur im Sparplan möglich).
        return cls(symbol, asset_class, qty_step=1.0, min_notional=1.0)
