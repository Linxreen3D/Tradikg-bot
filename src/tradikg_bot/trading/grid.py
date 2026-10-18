"""Grid-Daytrading (Netzhandel) – häufige kleine Trades mit Maker-Limits.

Je Symbol ein Netz unter einem Referenzkurs: Kauf-Limits bei ref·(1−g·k), k = 1…n. Wird eine
Stufe gekauft, liegt sofort ein Verkauf-Limit beim Kaufpreis·(1+g). Notbremse: fällt eine
Position um ``brake`` unter ihren Kaufpreis, wird sie als Taker verkauft und das Netz am
aktuellen Kurs neu aufgespannt. Neue Käufe nur, wenn der Vortages-Schluss über dem SMA 200 lag
(``regime``), sonst wartet das Netz. Ist nichts gekauft und steigt der Kurs eine Stufe über
ref, wird das Netz nachgezogen.

Einstellungen (g = 3 %, n = 5, Bremse 10 %, Trendfilter) wurden auf 2019–2022 gewählt; auf
2023–2026 (ungesehen) ergab das etwa −1 % (BTC) bzw. −12 % (ETH) – kein belegter Vorteil.
Nur Paper/Schnelldurchlauf, kein Echtgeld.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from tradikg_bot.costs import KRAKEN_SPOT_MAKER, KRAKEN_SPOT_TAKER

STRATEGY = "grid"


@dataclass(frozen=True)
class GridConfig:
    spacing: float = 0.03
    levels: int = 5
    brake: float = 0.10
    sma: int = 200  # Tages-SMA des Regimefilters (auch Vorlauf im Paper-Betrieb)
    maker_fee: float = KRAKEN_SPOT_MAKER
    taker_fee: float = KRAKEN_SPOT_TAKER
    brake_slippage: float = 0.005


@dataclass
class Lot:
    price: float
    units: float
    ts: str
    fee: float


@dataclass
class GridState:
    cash: float
    refs: dict[str, float] = field(default_factory=dict)
    lots: dict[str, dict[str, Lot]] = field(default_factory=dict)  # Symbol -> Stufe -> Lot
    trade_seq: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "cash": self.cash,
            "refs": self.refs,
            "trade_seq": self.trade_seq,
            "lots": {s: {k: asdict(v) for k, v in d.items()} for s, d in self.lots.items()},
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> GridState:
        lots = {s: {k: Lot(**v) for k, v in m.items()} for s, m in d.get("lots", {}).items()}
        return cls(d["cash"], dict(d.get("refs", {})), lots, int(d.get("trade_seq", 0)))


class GridAllocator:
    def __init__(
        self,
        symbols: list[str],
        capital: float,
        min_order: Callable[[str, float], float],
        regime: Callable[[str, pd.Timestamp], bool],
        cfg: GridConfig | None = None,
        state: GridState | None = None,
    ) -> None:
        self.symbols, self.min_order, self.regime = list(symbols), min_order, regime
        self.cfg = cfg or GridConfig()
        self.slot = capital / len(self.symbols) / self.cfg.levels
        self.state = state or GridState(cash=capital)

    def open_count(self) -> int:
        return sum(len(d) for d in self.state.lots.values())

    def equity(self, closes: dict[str, float]) -> float:
        held = sum(
            lot.units * closes.get(s, lot.price)
            for s, d in self.state.lots.items()
            for lot in d.values()
        )
        return self.state.cash + held

    def step(self, ts, bars, closes=None, block_entries: str | None = None):
        st, cfg = self.state, self.cfg
        closed, log = [], []
        for sym in self.symbols:
            bar = bars.get(sym)
            if bar is None:
                continue
            lots = st.lots.setdefault(sym, {})
            ref = st.refs.setdefault(sym, bar.close)
            braked = False
            sold_now: set[str] = set()  # in dieser Kerze verkauft → erst ab nächster Kerze neu
            for k, lot in list(lots.items()):
                if bar.low <= lot.price * (1 - cfg.brake):  # Notbremse (Taker)
                    px = min(bar.open, lot.price * (1 - cfg.brake)) * (1 - cfg.brake_slippage)
                    closed.append(self._close(sym, k, lot, px, cfg.taker_fee, ts, "notbremse"))
                    braked = True
                elif bar.high >= lot.price * (1 + cfg.spacing):  # Ziel eine Stufe höher (Maker)
                    px = max(bar.open, lot.price * (1 + cfg.spacing))
                    closed.append(self._close(sym, k, lot, px, cfg.maker_fee, ts, "ziel"))
                    sold_now.add(k)
            if braked:
                st.refs[sym] = bar.close
                log.append(
                    self._log(ts, sym, bar.close, "rejected", "Notbremse – Netz neu am Kurs")
                )
                continue
            why = block_entries or (
                None if self.regime(sym, ts) else "Abwärtstrend (Tagesschluss unter SMA 200)"
            )
            for k in range(1, cfg.levels + 1):
                lvl = ref * (1 - cfg.spacing * k)
                key = str(k)
                if key in lots or key in sold_now or bar.low > lvl:
                    continue
                if why:
                    log.append(self._log(ts, sym, bar.close, "rejected", why))
                    break
                px = min(bar.open, lvl)
                if st.cash < self.slot * 0.999 or self.slot < self.min_order(sym, px) * (
                    1 + cfg.maker_fee
                ):
                    log.append(self._log(ts, sym, bar.close, "rejected", "Kapital/Mindestorder"))
                    break
                units = self.slot / (px * (1 + cfg.maker_fee))
                fee = units * px * cfg.maker_fee
                st.cash -= units * px + fee
                lots[key] = Lot(px, units, ts.isoformat(), fee)
                log.append(self._log(ts, sym, bar.close, "entry", f"Kauf Stufe {k} zu {px:.2f}"))
            if not lots and bar.close > ref * (1 + cfg.spacing):
                st.refs[sym] = bar.close  # Netz nach oben nachziehen
        return closed, log

    def _close(self, sym, key, lot: Lot, px, fee_rate, ts, reason) -> dict[str, Any]:
        st = self.state
        del st.lots[sym][key]
        fee = lot.units * px * fee_rate
        st.cash += lot.units * px - fee
        st.trade_seq += 1
        net = lot.units * (px - lot.price) - fee - lot.fee
        return {
            "trade_id": st.trade_seq,
            "asset": sym,
            "strategy": STRATEGY,
            "entry_ts": lot.ts,
            "exit_ts": ts.isoformat(),
            "entry_price": lot.price,
            "exit_price": px,
            "volume": lot.units,
            "fees_eur": fee + lot.fee,
            "gross_pnl_eur": lot.units * (px - lot.price),
            "net_pnl_eur": net,
            "exit_reason": reason,
            "outcome_win": net > 0,
            "holding_seconds": (ts - pd.Timestamp(lot.ts)).total_seconds(),
        }

    def _log(self, ts, sym, close, status, reason) -> dict[str, Any]:
        ref = self.state.refs.get(sym, close)
        return {
            "ts": ts,
            "symbol": sym,
            "sma": ref,
            "close": close,
            "status": status,
            "reason": reason,
            "strategy": STRATEGY,
            "ref": f"Netz-Referenz {ref:.2f}",
        }


def daily_regime(
    daily: dict[str, pd.Series], sma: int = 200
) -> Callable[[str, pd.Timestamp], bool]:
    """Regime aus Tagesschlüssen: True, wenn der letzte **abgeschlossene** Tag vor ``ts`` über
    seinem SMA lag (kausal)."""
    ok = {s: (c > c.rolling(sma).mean()) for s, c in daily.items()}

    def regime(sym: str, ts: pd.Timestamp) -> bool:
        s = ok.get(sym)
        if s is None:
            return False
        past = s[s.index + pd.Timedelta(days=1) <= ts]  # Tageskerze muss geschlossen sein
        return bool(past.iloc[-1]) if len(past) else False

    return regime


def run_grid(
    data: dict[str, pd.DataFrame],
    daily: dict[str, pd.Series],
    start: pd.Timestamp,
    end: pd.Timestamp | None,
    capital: float,
    min_order,
    cfg: GridConfig | None = None,
):
    """Schnelldurchlauf: Kerze für Kerze (z. B. 1h), Regime nur aus vergangenen Tagen."""
    cfg = cfg or GridConfig()
    alloc = GridAllocator(list(data), capital, min_order, daily_regime(daily, cfg.sma), cfg)
    index = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in data.values()])))
    index = index[(index >= start) & ((index <= end) if end is not None else True)]
    equity, trades, log = {}, [], []
    last: dict[str, float] = {}
    for ts in index:
        bars = {s: d.loc[ts] for s, d in data.items() if ts in d.index}
        done, lg = alloc.step(ts, bars)
        trades += done
        log += lg
        last.update({s: float(b["close"]) for s, b in bars.items()})
        equity[ts] = alloc.equity(last)
    return pd.Series(equity, dtype=float), trades, log, alloc
