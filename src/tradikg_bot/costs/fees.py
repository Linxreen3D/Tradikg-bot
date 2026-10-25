"""Handelskostenmodell: Kommission, Spread und Slippage.

Konvention: Kurse aus OHLCV-Daten werden als Mittelkurs (Mid) interpretiert.
Ein Kauf zahlt ``mid * (1 + halber Spread + Slippage)``, ein Verkauf erhält
``mid * (1 - halber Spread - Slippage)``. Limit-Orders (Take-Profit) werden zum
Limitkurs ohne Slippage ausgeführt; die Kommission fällt immer an.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tradikg_bot.domain import AssetClass

BPS = 1e-4


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Fill:
    side: OrderSide
    qty: float
    reference_price: float
    price: float
    commission: float

    @property
    def notional(self) -> float:
        return self.qty * self.price

    @property
    def slippage_cost(self) -> float:
        """Kosten aus Spread+Slippage ggü. Referenzkurs (immer >= 0 bei Marktorders)."""
        return abs(self.price - self.reference_price) * self.qty

    @property
    def total_cost(self) -> float:
        return self.commission + self.slippage_cost

    @property
    def cash_flow(self) -> float:
        """Veränderung des Barbestands (Kauf negativ, Verkauf positiv)."""
        if self.side is OrderSide.BUY:
            return -(self.notional + self.commission)
        return self.notional - self.commission


@dataclass(frozen=True)
class FeeModel:
    """Parametrisierbares Kostenmodell.

    commission_rate: prozentuale Kommission auf den Ordernennwert (0.001 = 0.1 %)
    min_commission:  Mindestkommission pro Order (Kontowährung)
    fixed_commission: fixe Gebühr pro Order (z. B. 1 € Fremdkostenpauschale)
    spread_bps:      gesamter Geld-Brief-Spread in Basispunkten
    slippage_bps:    zusätzliche Ausführungsabweichung je Marktorder in Basispunkten
    """

    commission_rate: float = 0.0
    min_commission: float = 0.0
    fixed_commission: float = 0.0
    spread_bps: float = 0.0
    slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "commission_rate",
            "min_commission",
            "fixed_commission",
            "spread_bps",
            "slippage_bps",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} darf nicht negativ sein")

    # -- Einzelbausteine -------------------------------------------------
    @property
    def market_impact(self) -> float:
        """Relativer Preisaufschlag einer Marktorder (halber Spread + Slippage)."""
        return (self.spread_bps / 2 + self.slippage_bps) * BPS

    def commission(self, notional: float) -> float:
        if notional <= 0:
            return 0.0
        return max(self.min_commission, notional * self.commission_rate) + self.fixed_commission

    def execution_price(self, side: OrderSide, mid_price: float) -> float:
        if mid_price <= 0:
            raise ValueError("Preis muss > 0 sein")
        if side is OrderSide.BUY:
            return mid_price * (1 + self.market_impact)
        return mid_price * (1 - self.market_impact)

    # -- Ausführung ------------------------------------------------------
    def market_fill(self, side: OrderSide, qty: float, mid_price: float) -> Fill:
        price = self.execution_price(side, mid_price)
        return Fill(side, qty, mid_price, price, self.commission(qty * price))

    def limit_fill(self, side: OrderSide, qty: float, limit_price: float) -> Fill:
        return Fill(side, qty, limit_price, limit_price, self.commission(qty * limit_price))

    # -- Entscheidungshilfen -----------------------------------------------
    def round_trip_cost(self, qty: float, mid_price: float) -> float:
        """Erwartete Gesamtkosten (Kauf+Verkauf als Marktorders) bei unverändertem Kurs."""
        buy = self.market_fill(OrderSide.BUY, qty, mid_price)
        sell = self.market_fill(OrderSide.SELL, qty, mid_price)
        return buy.total_cost + sell.total_cost

    def break_even_move(self, qty: float, mid_price: float) -> float:
        """Relative Kursbewegung, die nötig ist, um die Round-Trip-Kosten zu decken."""
        notional = qty * mid_price
        if notional <= 0:
            return float("inf")
        return self.round_trip_cost(qty, mid_price) / notional


# Konservative Standardannahmen. Diese Werte sind Platzhalter und müssen je
# genutzter Börse/Broker überprüft werden (siehe README, Abschnitt Kosten).
CRYPTO_SPOT_DEFAULT = FeeModel(
    commission_rate=0.001,  # 0.1 % Taker-Gebühr (typischer Spot-Standardtarif)
    spread_bps=10.0,
    slippage_bps=5.0,
)
# Kraken-Pro-Spot-Krypto, Stufe 1 (30-Tage-Volumen < 2.500 USD) laut öffentlicher
# Gebührenseite kraken.com/features/fee-schedule, abgerufen 17.10.2026. Nicht kontospezifisch
# verifiziert. Einzige Quelle für Research, Paper, Replay, Scanner und Live-Plausibilisierung.
KRAKEN_SPOT_TAKER = 0.008  # 0,80 %
KRAKEN_SPOT_MAKER = 0.004  # 0,40 %
KRAKEN_SPOT_ASSUMED = FeeModel(
    commission_rate=KRAKEN_SPOT_TAKER,
    spread_bps=10.0,
    slippage_bps=5.0,
)
STOCK_NEOBROKER_DEFAULT = FeeModel(
    fixed_commission=1.0,  # 1 € Fremdkostenpauschale je Order (typischer Neobroker)
    spread_bps=10.0,
    slippage_bps=5.0,
)


def default_fee_model(asset_class: AssetClass | str) -> FeeModel:
    asset_class = AssetClass(asset_class)
    if asset_class is AssetClass.CRYPTO:
        return CRYPTO_SPOT_DEFAULT
    return STOCK_NEOBROKER_DEFAULT
