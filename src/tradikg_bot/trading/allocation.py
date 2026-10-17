"""Trendfolge-Allokation (200-Tage-Regel) – Alternative zum Bracket-Kreislauf.

Regeln (vorab festgelegt, nicht auf Daten optimiert):

* **Einstieg:** Tagesschluss über dem SMA 200 → am Folgetag Kauf per Maker-Limit zum letzten
  Schlusskurs (0,40 %). Wird das Limit nicht erreicht (Kurs läuft weg), Kauf als Taker zum
  Schluss des Folgetags (0,80 % + halber Spread).
* **Ausstieg per Signal:** Schluss unter dem SMA 200 → Verkauf genauso (Maker, sonst Taker).
* **Notfall-Stop:** 20 % unter dem höchsten Schluss seit Einstieg (nachgezogen). Er ist als
  *ein* serverseitiger Stop-Auftrag gedacht – das kann Kraken Spot (anders als SL+TP-OCO).
  Ausführung als Taker mit Stop-Slippage. Danach erst wieder Einstieg nach neuem Kreuzen
  von unten (Schluss zuvor unter SMA).
* **Aufteilung:** Kapital gleichmäßig auf die Symbole; Mindestorder (Kraken) wird beachtet,
  zu kleine Positionen werden nicht eröffnet (Grund wird protokolliert).

Kausal: Die Entscheidung an Kerze t nutzt nur Schlusskurse bis t; ausgeführt wird ab t+1.
Kein Hebel, nur long, nur virtuelles Geld.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from tradikg_bot.costs import KRAKEN_SPOT_MAKER, KRAKEN_SPOT_TAKER

STRATEGY = "trend_sma200"


@dataclass(frozen=True)
class TrendConfig:
    sma: int = 200
    stop_frac: float = 0.20
    maker_fee: float = KRAKEN_SPOT_MAKER
    taker_fee: float = KRAKEN_SPOT_TAKER
    spread_bps: float = 10.0
    stop_slippage_bps: float = 50.0
    invest_fraction: float = 1.0  # Risikoregler: Anteil des Kapitals, der höchstens investiert wird
    cash_yield_pa: float = (
        0.0  # Zins auf nicht investiertes Geld – nur setzen, wenn er real gezahlt wird
    )


@dataclass
class Position:
    units: float
    entry_price: float
    entry_ts: str
    entry_fee: float
    peak: float
    stop: float


@dataclass
class TrendState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)  # side, limit, eur
    blocked: dict[str, bool] = field(default_factory=dict)  # nach Notfall-Stop
    trade_seq: int = 0

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["positions"] = {s: asdict(p) for s, p in self.positions.items()}
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TrendState:
        st = cls(**{k: v for k, v in d.items() if k != "positions"})
        st.positions = {s: Position(**p) for s, p in d.get("positions", {}).items()}
        return st


class TrendAllocator:
    """Zustand + ein Schritt je abgeschlossener Kerze. ``min_order(symbol, price)`` liefert die
    kleinste zulässige Order in EUR (Kraken AssetPairs)."""

    def __init__(
        self,
        symbols: list[str],
        capital: float,
        min_order,
        cfg: TrendConfig | None = None,
        state: TrendState | None = None,
    ) -> None:
        self.symbols, self.capital, self.min_order = list(symbols), capital, min_order
        self.cfg = cfg or TrendConfig()
        self.state = state or TrendState(cash=capital)

    # -- Bewertung -----------------------------------------------------------------------------
    def equity(self, closes: dict[str, float]) -> float:
        held = sum(p.units * closes.get(s, p.entry_price) for s, p in self.state.positions.items())
        return self.state.cash + held

    # -- ein Schritt ---------------------------------------------------------------------------
    def step(
        self,
        ts: pd.Timestamp,
        bars: dict[str, Any],
        closes: dict[str, pd.Series],
        block_entries: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``bars``: OHLC je Symbol für Kerze ``ts``; ``closes``: Schlusskurse bis inkl. ``ts``.
        ``block_entries``: Grund, keine neuen Einstiege zu planen (Ausstiege laufen weiter).

        Rückgabe: (geschlossene Trades, Signal-Protokoll).
        """
        st, cfg, half = self.state, self.cfg, self.cfg.spread_bps / 2 * 1e-4
        st.cash *= 1 + cfg.cash_yield_pa / 365  # tägliche Verzinsung des Cash-Anteils (1d)
        closed: list[dict[str, Any]] = []
        log: list[dict[str, Any]] = []
        for sym in self.symbols:
            bar = bars.get(sym)
            if bar is None:
                continue
            # 1. Notfall-Stop für Positionen vom Vortag (intraday vor allen Schluss-Aufträgen;
            #    Kurslücke unter den Stop → Eröffnungskurs)
            pos = st.positions.get(sym)
            if pos and bar.low <= pos.stop:
                px = min(bar.open, pos.stop) * (1 - cfg.stop_slippage_bps * 1e-4)
                closed.append(self._close(sym, px, cfg.taker_fee, ts, "notfall_stop"))
                st.blocked[sym] = True
                st.pending.pop(sym, None)
            # 2. Offene Aufträge vom Vortag (Maker-Limit, sonst Taker zum Schluss)
            order = st.pending.pop(sym, None)
            if order and order["side"] == "buy":
                maker = bar.low <= order["limit"]
                px = order["limit"] if maker else bar.close * (1 + half)
                fee_rate = cfg.maker_fee if maker else cfg.taker_fee
                units = min(order["eur"], st.cash) / (px * (1 + fee_rate))
                fee = units * px * fee_rate
                st.cash -= units * px + fee
                peak = max(px, bar.close)
                st.positions[sym] = Position(
                    units, px, ts.isoformat(), fee, peak, peak * (1 - cfg.stop_frac)
                )
            elif order and order["side"] == "sell" and sym in st.positions:
                maker = bar.high >= order["limit"]
                px = order["limit"] if maker else bar.close * (1 - half)
                fee_rate = cfg.maker_fee if maker else cfg.taker_fee
                closed.append(self._close(sym, px, fee_rate, ts, "signal"))
            # Stop nachziehen (höchster Schluss seit Einstieg)
            pos = st.positions.get(sym)
            if pos:
                pos.peak = max(pos.peak, bar.close)
                pos.stop = max(pos.stop, pos.peak * (1 - cfg.stop_frac))
            # 3. Entscheidung zum Schluss (nur bekannte Kerzen bis ts)
            hist = closes[sym]
            if len(hist) < cfg.sma:
                continue
            sma = float(hist.iloc[-cfg.sma :].mean())
            above = bar.close > sma
            if not above:
                st.blocked[sym] = False  # nach Stop erst neues Kreuzen von unten
            if sym in st.positions and not above:
                st.pending[sym] = {"side": "sell", "limit": bar.close}
                log.append(self._log(ts, sym, sma, bar.close, "exit", "Schluss unter SMA 200"))
            elif sym not in st.positions and above:
                if st.blocked.get(sym) or block_entries:
                    why = block_entries or "Sperre nach Notfall-Stop"
                    log.append(self._log(ts, sym, sma, bar.close, "rejected", why))
                    continue
                slot = self.equity(_last(closes)) * cfg.invest_fraction / len(self.symbols)
                eur = min(slot, st.cash)
                need = self.min_order(sym, bar.close) * (1 + cfg.taker_fee)
                if eur < need:
                    log.append(
                        self._log(
                            ts,
                            sym,
                            sma,
                            bar.close,
                            "rejected",
                            f"Anteil {eur:.2f} € unter Mindestorder {need:.2f} €",
                        )
                    )
                    continue
                st.pending[sym] = {"side": "buy", "limit": bar.close, "eur": eur}
                log.append(self._log(ts, sym, sma, bar.close, "entry", "Schluss über SMA 200"))
        return closed, log

    def _close(self, sym: str, px: float, fee_rate: float, ts: pd.Timestamp, reason: str):
        st = self.state
        pos = st.positions.pop(sym)
        fee = pos.units * px * fee_rate
        st.cash += pos.units * px - fee
        st.trade_seq += 1
        gross = pos.units * (px - pos.entry_price)
        net = gross - fee - pos.entry_fee
        return {
            "trade_id": st.trade_seq,
            "asset": sym,
            "strategy": STRATEGY,
            "entry_ts": pos.entry_ts,
            "exit_ts": ts.isoformat(),
            "entry_price": pos.entry_price,
            "exit_price": px,
            "volume": pos.units,
            "fees_eur": fee + pos.entry_fee,
            "gross_pnl_eur": gross,
            "net_pnl_eur": net,
            "exit_reason": reason,
            "outcome_win": net > 0,
            "holding_seconds": (ts - pd.Timestamp(pos.entry_ts)).total_seconds(),
        }

    @staticmethod
    def _log(ts, sym, sma, close, status, reason) -> dict[str, Any]:
        return {
            "ts": ts,
            "symbol": sym,
            "sma": sma,
            "close": close,
            "status": status,
            "reason": reason,
        }


def _last(closes: dict[str, pd.Series]) -> dict[str, float]:
    return {s: float(c.iloc[-1]) for s, c in closes.items() if len(c)}


def run_trend(
    data: dict[str, pd.DataFrame],
    start: pd.Timestamp,
    end: pd.Timestamp | None,
    capital: float,
    min_order,
    cfg: TrendConfig | None = None,
) -> tuple[pd.Series, list[dict[str, Any]], list[dict[str, Any]], TrendAllocator]:
    """Schnelldurchlauf ab ``start``: Kerze für Kerze, nur Vergangenheit sichtbar."""
    alloc = TrendAllocator(list(data), capital, min_order, cfg)
    index = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in data.values()])))
    index = index[(index >= start) & ((index <= end) if end is not None else True)]
    closes_full = {s: d["close"] for s, d in data.items()}
    equity, trades, log = {}, [], []
    for ts in index:
        bars = {s: d.loc[ts] for s, d in data.items() if ts in d.index}
        closes = {s: c.iloc[: c.index.searchsorted(ts, "right")] for s, c in closes_full.items()}
        done, lg = alloc.step(ts, bars, closes)
        trades += done
        log += lg
        equity[ts] = alloc.equity(_last(closes))
    return pd.Series(equity, dtype=float), trades, log, alloc


class TrendPaperEngine:
    """Paper-Dauerbetrieb der Allokation mit derselben Schnittstelle und denselben Tabellen
    (equity, trade_analysis, signals_log, ledger_entries) wie der Bracket-Kreislauf – das
    Dashboard zeigt beides gleich an. Zustand als JSON im Paper-Store (neustartfest)."""

    def __init__(
        self,
        paths,
        rules: dict[str, Any],
        capital: float,
        timeframe: str,
        tax_rate: float = 0.0,
        cfg: TrendConfig | None = None,
    ):
        from tradikg_bot.trading.engine import KillSwitch, PaperEngineConfig
        from tradikg_bot.trading.ledger import CapitalLedger
        from tradikg_bot.trading.store import PaperStore

        self.cfg = PaperEngineConfig(timeframe=timeframe, capital_eur=capital)
        self.store = PaperStore(paths.db)
        self.ledger = CapitalLedger(paths.db, capital, tax_rate=tax_rate)
        self.kill = KillSwitch(paths.kill_switch)
        self.journal = _NoHalt()
        self.rules = rules
        raw = self.store.get_json("trend_state")
        self.alloc = TrendAllocator(
            list(rules),
            capital,
            self._min_order,
            cfg or TrendConfig(),
            TrendState.from_json(raw) if raw else None,
        )
        self.min_history = self.alloc.cfg.sma  # Vorlauf beim Erststart
        n = self.alloc.cfg.sma + 10
        self.closes = {s: self.store.bars(s, limit=n)["close"] for s in rules}

    def _min_order(self, sym: str, px: float) -> float:
        r = self.rules[sym]
        return max(r.ordermin * px, r.costmin)

    def entries_blocked(self) -> str | None:
        return "Kill Switch aktiv" if self.kill.engaged else None

    def _append(self, sym: str, ts: pd.Timestamp, bar: Any) -> None:
        self.store.save_bar(sym, ts, bar.open, bar.high, bar.low, bar.close, bar.volume)
        c = self.closes.get(sym, pd.Series(dtype=float))
        if c.empty or c.index[-1] < ts:
            self.closes[sym] = pd.concat([c, pd.Series([bar.close], index=[ts])]).iloc[-500:]

    def seed_history(self, data: dict[str, pd.DataFrame]) -> int:
        from tradikg_bot.trading.engine import Bar

        stamps = sorted(set().union(*[set(df.index) for df in data.values()]))
        for ts in stamps:
            for sym, df in data.items():
                if ts in df.index and sym in self.rules:
                    r = df.loc[ts]
                    self._append(
                        sym,
                        ts,
                        Bar(*(float(r[c]) for c in ("open", "high", "low", "close", "volume"))),
                    )
            self.store.mark_step(ts, ts + self.cfg.step)
        return len(stamps)

    def step(self, ts: pd.Timestamp, bars: dict[str, Any], allow_entries: bool = True):
        from tradikg_bot.trading.engine import StepReport

        report = StepReport(ts=ts)
        if self.store.step_done(ts):
            report.skipped = True
            return report
        bars = {s: b for s, b in bars.items() if s in self.rules}
        for sym, bar in bars.items():
            self._append(sym, ts, bar)
        block = self.entries_blocked() or (None if allow_entries else "Daten nicht aktuell")
        closed, log = self.alloc.step(ts, bars, self.closes, block_entries=block)
        when = (ts + self.cfg.step).to_pydatetime()
        for t in closed:
            self.ledger.book(
                f"trend:{t['trade_id']}", t["net_pnl_eur"], when, note=t["exit_reason"]
            )
            self.store.save_analysis(t["trade_id"], t["exit_ts"], t)
            report.closed.append(t["trade_id"])
        for x in log:
            self.store.log_signal(
                ts,
                x["symbol"],
                STRATEGY,
                "trend" if x["close"] > x["sma"] else "unter_sma",
                1.0,
                f"SMA {self.alloc.cfg.sma} = {x['sma']:.2f}",
                0.0,
                0.0,
                "accepted" if x["status"] in ("entry", "exit") else x["status"],
                x["reason"],
            )
            if x["status"] == "entry":
                report.new_intents.append(x["symbol"])
        last = {s: float(c.iloc[-1]) for s, c in self.closes.items() if len(c)}
        held = sum(
            p.units * last.get(s, p.entry_price) for s, p in self.alloc.state.positions.items()
        )
        b = self.ledger.balances()
        self.store.save_equity(
            ts + self.cfg.step,
            {
                "equity": self.alloc.equity(last),
                "cash": self.alloc.state.cash,
                "positions_value": held,
                "trading_balance": b.trading_balance,
                "payout_reserve": b.payout_reserve,
                "longterm_portfolio": b.longterm_portfolio,
                "tax_reserve": b.tax_reserve,
                "unrecovered": b.unrecovered_losses,
                "realized_today": 0.0,
                "unrealized": 0.0,
                "open_positions": len(self.alloc.state.positions),
                "status": "trend",
            },
        )
        self.store.set_json("trend_state", self.alloc.state.to_json())
        self.store.mark_step(ts, ts + self.cfg.step)
        return report


class _NoHalt:
    """Der Allokationsmodus nutzt keinen Bracket-Executor; SAFE_HALT entfällt."""

    halt_reason = None
