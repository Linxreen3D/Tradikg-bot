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
from pathlib import Path
from typing import Any

import pandas as pd

from tradikg_bot.costs import KRAKEN_SPOT_MAKER, KRAKEN_SPOT_TAKER
from tradikg_bot.risk.limits import (
    TREND_DAILY_LOSS_FRACTION,
    TREND_DAILY_LOSS_TIMEZONE,
    TREND_DRAWDOWN_FRACTION,
    DailyLossGuard,
    DrawdownGuard,
    validate_trend_daily_loss,
    validate_trend_drawdown,
)

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
    # Dieselbe Tagesverlustgrenze wie im Echtgeld-Bot. Standard aus, damit bestehende
    # Paper-Läufe und Replays sich nicht ändern. An: nur neue Käufe sperren, keine
    # Zwangsverkäufe, Notfall-Stops bleiben.
    daily_loss_enabled: bool = False
    daily_loss_fraction: float = TREND_DAILY_LOSS_FRACTION
    daily_loss_timezone: str = TREND_DAILY_LOSS_TIMEZONE
    # Drawdown vom Equity-Höchststand. Standard aus. 45 % ist die weiteste Schwelle.
    drawdown_enabled: bool = False
    drawdown_fraction: float = TREND_DRAWDOWN_FRACTION

    def __post_init__(self) -> None:
        validate_trend_daily_loss(self.daily_loss_fraction, self.daily_loss_timezone)
        validate_trend_drawdown(self.drawdown_fraction)


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
    daily_loss: dict[str, Any] | None = None  # Guard-Zustand, nur wenn die Grenze an ist
    drawdown: dict[str, Any] | None = None  # Höchststand und Sperre, nur wenn an

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
        self.guard: DailyLossGuard | None = None
        if self.cfg.daily_loss_enabled:
            self.guard = DailyLossGuard.restore(
                self.state.daily_loss,
                limit=self.cfg.daily_loss_fraction,
                timezone=self.cfg.daily_loss_timezone,
            )
        self.drawdown: DrawdownGuard | None = None
        self.drawdown_file: Path | None = None
        if self.cfg.drawdown_enabled:
            self.drawdown = DrawdownGuard.restore(
                self.state.drawdown, limit=self.cfg.drawdown_fraction
            )

    # -- Bewertung -----------------------------------------------------------------------------
    def open_count(self) -> int:
        return len(self.state.positions)

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
        # Referenz ist die Equity vor dieser Kerze (letzter Schluss). Die Kerze selbst
        # zählt danach als Tages-PnL, auch wenn sie die erste des Wiener Tages ist.
        if self.guard is not None:
            self._roll_daily_loss(ts, closes)
        if self.drawdown is not None:
            self._sync_drawdown(ts, closes, log)
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
                loss_block = self._loss_block_reason(closes) if self.guard is not None else None
                if self.drawdown is not None:
                    self._sync_drawdown(ts, closes, log)
                dd_block = self._drawdown_reason(closes) if self.drawdown is not None else None
                if st.blocked.get(sym) or block_entries or loss_block or dd_block:
                    why = dd_block or loss_block or block_entries or "Sperre nach Notfall-Stop"
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
        if self.guard is not None:
            # Auch ohne Kaufversuch festhalten (Neustart muss die Sperre wiederfinden).
            self._loss_block_reason(closes)
        if self.drawdown is not None:
            self._sync_drawdown(ts, closes, log)
        return closed, log

    def _marks(self, closes: dict[str, pd.Series], *, lag: int = 0) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym, series in closes.items():
            if len(series) > lag:
                out[sym] = float(series.iloc[-1 - lag])
            elif len(series):
                out[sym] = float(series.iloc[-1])
        return out

    def _roll_daily_loss(self, ts: pd.Timestamp, closes: dict[str, pd.Series]) -> None:
        assert self.guard is not None
        when = ts.to_pydatetime()
        day = self.guard.trading_day(when)
        if self.guard.current_day != day:
            self.guard.start_day(day, self.equity(self._marks(closes, lag=1)))
        self.state.daily_loss = self.guard.snapshot()

    def _sync_drawdown(self, ts: pd.Timestamp, closes: dict[str, pd.Series], log: list) -> None:
        assert self.drawdown is not None
        equity = self.equity(_last(closes))
        when = ts.to_pydatetime()
        if self.drawdown.observe(equity, when):
            self._write_drawdown_file(equity)
            log.append(
                self._log(ts, "PORTFOLIO", 0.0, equity, "halt", self._drawdown_reason(closes) or "")
            )
        self.state.drawdown = self.drawdown.snapshot()

    def _drawdown_reason(self, closes: dict[str, pd.Series]) -> str | None:
        if self.drawdown is None or not self.drawdown.halted:
            return None
        equity = self.equity(_last(closes))
        dd = abs(self.drawdown.drawdown_from(equity))
        return (
            f"Drawdown-Sperre {dd:.0%} unter Höchststand {self.drawdown.peak_equity:.2f} € "
            f"(Grenze −{self.cfg.drawdown_fraction:.0%}), nur manuell lösbar"
        )

    def _write_drawdown_file(self, equity: float) -> None:
        if self.drawdown is None or self.drawdown_file is None:
            return
        self.drawdown_file.parent.mkdir(parents=True, exist_ok=True)
        self.drawdown_file.write_text(
            "Drawdown-Sperre: keine neuen Käufe.\n"
            f"Höchststand {self.drawdown.peak_equity:.2f} €, zuletzt {equity:.2f} €.\n"
            "Lösen im Echtbetrieb nur mit: tradikg live drawdown-reset\n"
            "Diese Datei ist ein Hinweis. Löschen hebt die Sperre nicht auf.\n"
            "Es wird nichts automatisch verkauft.\n"
        )

    def reset_drawdown(self) -> None:
        """Manuelle Lösung im Paper-Lauf. Der nächste Schritt setzt den Höchststand neu."""
        if self.drawdown is None:
            return
        self.drawdown.reset()
        self.state.drawdown = self.drawdown.snapshot()
        if self.drawdown_file is not None:
            self.drawdown_file.unlink(missing_ok=True)

    def _loss_block_reason(self, closes: dict[str, pd.Series]) -> str | None:
        """Prüft die Equity zum aktuellen Schluss. None, wenn neue Käufe erlaubt sind."""
        assert self.guard is not None
        equity = self.equity(_last(closes))
        self.guard.check(equity)
        self.state.daily_loss = self.guard.snapshot()
        if not self.guard.halted:
            return None
        pnl = equity - self.guard.day_start_equity
        return (
            f"Tagesverlustgrenze {self.cfg.daily_loss_fraction:.0%} "
            f"({pnl:+.2f} € ggü. {self.guard.day_start_equity:.2f} € zum Tagesbeginn, "
            f"bis 00:00 {self.cfg.daily_loss_timezone})"
        )

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
        allocator_factory=None,
        state_key: str = "trend_state",
        status: str = "trend",
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
        self.state_key, self.status = state_key, status
        raw = self.store.get_json(state_key)
        if allocator_factory is not None:  # z. B. Grid-Daytrading mit derselben Infrastruktur
            self.alloc = allocator_factory(list(rules), capital, self._min_order, raw)
        else:
            self.alloc = TrendAllocator(
                list(rules),
                capital,
                self._min_order,
                cfg or TrendConfig(),
                TrendState.from_json(raw) if raw else None,
            )
        if getattr(self.alloc, "drawdown", None) is not None:
            from tradikg_bot.risk.limits import DRAWDOWN_HALT_NAME

            self.alloc.drawdown_file = paths.db.parent / DRAWDOWN_HALT_NAME
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
                f"{self.status}:{t['trade_id']}", t["net_pnl_eur"], when, note=t["exit_reason"]
            )
            self.store.save_analysis(t["trade_id"], t["exit_ts"], t)
            report.closed.append(t["trade_id"])
        for x in log:
            self.store.log_signal(
                ts,
                x["symbol"],
                x.get("strategy", STRATEGY),
                "trend" if x["close"] > x["sma"] else "unter_sma",
                1.0,
                x.get("ref", f"SMA {self.alloc.cfg.sma} = {x['sma']:.2f}"),
                0.0,
                0.0,
                "accepted" if x["status"] in ("entry", "exit") else x["status"],
                x["reason"],
            )
            if x["status"] == "entry":
                report.new_intents.append(x["symbol"])
        last = {s: float(c.iloc[-1]) for s, c in self.closes.items() if len(c)}
        equity = self.alloc.equity(last)
        held = equity - self.alloc.state.cash
        b = self.ledger.balances()
        self.store.save_equity(
            ts + self.cfg.step,
            {
                "equity": equity,
                "cash": self.alloc.state.cash,
                "positions_value": held,
                "trading_balance": b.trading_balance,
                "payout_reserve": b.payout_reserve,
                "longterm_portfolio": b.longterm_portfolio,
                "tax_reserve": b.tax_reserve,
                "unrecovered": b.unrecovered_losses,
                "realized_today": 0.0,
                "unrealized": 0.0,
                "open_positions": self.alloc.open_count(),
                "status": self.status,
            },
        )
        self.store.set_json(self.state_key, self.alloc.state.to_json())
        self.store.mark_step(ts, ts + self.cfg.step)
        return report


class _NoHalt:
    """Der Allokationsmodus nutzt keinen Bracket-Executor; SAFE_HALT entfällt."""

    halt_reason = None
