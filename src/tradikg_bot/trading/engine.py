"""Autonomer Paper-Handelskreislauf (ohne Claude/OpenAI, ohne Echtgeld).

Ein Schritt (``step``) verarbeitet alle Kerzen, die zum selben Zeitpunkt
geschlossen wurden, in fester Reihenfolge:

1. **Ausführung** ausstehender Absichten aus dem vorigen Schritt zum
   Eröffnungskurs dieser Kerze (in Rangfolge, jeweils mit vollständiger
   Vorab-Prüfung des Executors – Kapital wird so zuerst für die besten Signale
   verwendet).
2. **SL/TP** offener Positionen anhand von Hoch/Tief der Kerze (konservativ).
3. **Abgleich** mit der (Paper-)Börse, Netto-Abrechnung geschlossener Trades
   im Ledger (33/33/34 nach Verlustaufholung), Nachanalyse, Cooldowns.
4. **Bewertung** zum Schlusskurs, Tagesverlust-Abschaltschwelle (realisiert +
   unrealisiert ≥ 10 % der Trading-Balance zu Tagesbeginn).
5. **Signalsuche**, Netto-EV-Bewertung, Ranking -> neue Absichten für den nächsten
   Schritt.

Sicherheit und Robustheit:
* Läuft **nur** gegen eine Paper-Börse. Eine Live-Börse wird abgelehnt
  (``AutonomyNotAuthorizedError``) – ein autonomer Echtgeldmodus existiert nicht.
* Idempotent: Jeder Zeitschritt wird genau einmal verarbeitet; Absichten haben
  eindeutige Schlüssel; Ledger-Buchungen sind eindeutig je Trade.
* Neustartfähig: Börsenzustand, Absichten, Cooldowns und Kursverlauf liegen in
  SQLite; beim Start gleicht der Executor alles mit der Börse ab.
* Keine feste Trade-Anzahl. Wiederholte Signale werden durch „eine Position je
  Paar“, eindeutige Signal-Schlüssel und dynamische Cooldowns gebremst.
* Kill Switch (Datei), SAFE_HALT (Executor), Tagesabschaltung und Datenprüfung
  (Lücken, Aktualität, Mindesthistorie) sperren neue Einstiege; offene
  Positionen bleiben durch SL/TP geschützt und werden weiter verarbeitet.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.executor import LiveExecutor, PlanRejectedError, TradingHaltedError
from tradikg_bot.live.journal import ExecutionJournal, TradeState
from tradikg_bot.live.paper import PaperExchange
from tradikg_bot.market.kraken_data import is_stale, timeframe_minutes
from tradikg_bot.trading.analysis import analyze_trade, daily_report
from tradikg_bot.trading.ledger import CapitalLedger
from tradikg_bot.trading.probability import ProbabilityModel
from tradikg_bot.trading.signals import (
    SignalCandidate,
    correlation_penalty,
    evaluate_costs,
    fill_probability,
    rank,
)
from tradikg_bot.trading.sources import SignalSource
from tradikg_bot.trading.store import PaperStore


class AutonomyNotAuthorizedError(RuntimeError):
    """Autonomer Handel ist nur im Paper-Modus erlaubt."""


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class PaperEngineConfig:
    timeframe: str = "1h"
    capital_eur: float = 100.0
    assumed_spread_bps: float = 10.0
    expected_entry_slippage_bps: float = 5.0  # Erwartung (das IOC-Limitband ist breiter)
    base_cooldown_bars: int = 1
    max_cooldown_bars: int = 48
    error_cooldown_bars: int = 6
    correlation_threshold: float = 0.7
    max_participation: float = 0.01
    min_history_bars: int = 250
    min_expected_r: float = 0.02
    tax_rate: float = 0.0

    @property
    def step(self) -> pd.Timedelta:
        return pd.Timedelta(minutes=timeframe_minutes(self.timeframe))


class _SimulatedClock:
    """Monotone Uhr für Bestätigungsfristen im Paper-Betrieb: ``sleep`` rückt sie vor.

    Jeder Warteschritt zählt mindestens 0,1 s, damit Fristen auch bei
    ``poll_interval_s = 0`` sicher ablaufen (keine Endlosschleife).
    """

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 0.1)


class KillSwitch:
    """Not-Aus: Existiert die Datei, werden keine neuen Positionen eröffnet."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None

    @property
    def engaged(self) -> bool:
        return bool(self.path and self.path.exists())

    def engage(self, reason: str) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(reason)

    def release(self) -> None:
        if self.path and self.path.exists():
            self.path.unlink()


@dataclass
class StepReport:
    ts: pd.Timestamp
    skipped: bool = False
    executed: list[int] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    closed: list[int] = field(default_factory=list)
    new_intents: list[str] = field(default_factory=list)
    candidates_seen: int = 0
    blocked_reason: str | None = None


class PaperTradingEngine:
    def __init__(
        self,
        gateway: PaperExchange,
        store: PaperStore,
        journal: ExecutionJournal,
        ledger: CapitalLedger,
        probability: ProbabilityModel,
        signal_source: SignalSource,
        config: PaperEngineConfig,
        risk: LiveRiskConfig | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        if getattr(gateway, "is_live", True):
            raise AutonomyNotAuthorizedError(
                "Der autonome Kreislauf läuft ausschließlich gegen eine Paper-Börse. "
                "Ein autonomer Echtgeldmodus ist nicht freigegeben und nicht implementiert."
            )
        if not gateway.capabilities.native_bracket:
            raise ValueError("Paper-Simulation benötigt eine Börse mit (simuliertem) Bracket")
        self.gw, self.store, self.journal, self.ledger = gateway, store, journal, ledger
        self.probability, self.source, self.cfg = probability, signal_source, config
        self.kill = kill_switch or KillSwitch(None)
        self.risk = risk or LiveRiskConfig.for_paper(
            config.capital_eur, allowed_pairs=tuple(gateway.rules)
        )
        self.now: pd.Timestamp = pd.Timestamp("1970-01-01", tz="UTC")
        self._catchup = False
        self._lock = threading.Lock()
        self._history: dict[str, pd.DataFrame] = {}
        self._clock = _SimulatedClock()
        self.executor = LiveExecutor(
            gateway,
            journal,
            self.risk,
            clock=self._clock,
            sleep=self._clock.sleep,
            now=lambda: self.now.to_pydatetime(),
            capital_limit=lambda: ledger.capital_limit(self.cfg.capital_eur),
            day_start_balance=ledger.day_start_trading_balance,
        )

    # -- Hilfen ------------------------------------------------------------------------------
    def history(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._history:
            self._history[symbol] = self.store.bars(symbol, limit=2000)
        return self._history[symbol]

    def _append(self, symbol: str, ts: pd.Timestamp, bar: Bar) -> None:
        self.store.save_bar(symbol, ts, bar.open, bar.high, bar.low, bar.close, bar.volume)
        hist = self.history(symbol)
        if hist.empty or hist.index[-1] < ts:
            row = pd.DataFrame(
                [[bar.open, bar.high, bar.low, bar.close, bar.volume]],
                columns=["open", "high", "low", "close", "volume"],
                index=pd.DatetimeIndex([ts], name="timestamp").as_unit("ns"),
            )
            self._history[symbol] = pd.concat([hist, row]).iloc[-2000:]

    def _set_book(self, symbol: str, price: float, volume: float) -> None:
        half = self.cfg.assumed_spread_bps / 2 * 1e-4
        depth = max(volume / 25.0, 1e-9)
        self.gw.set_book(symbol, price * (1 - half), price * (1 + half), depth=depth)

    def _day_halt_key(self) -> str:
        return f"day_halt:{self.now.date().isoformat()}"

    def entries_blocked(self) -> str | None:
        if self._catchup:
            return "Nachholen verpasster Kerzen – keine neuen Einstiege auf veralteten Daten"
        if self.kill.engaged:
            return "Kill Switch aktiv"
        if self.executor.halted:
            return f"SAFE_HALT: {self.journal.halt_reason}"
        if self.store.get(self._day_halt_key()):
            return "Tagesverlust-Abschaltschwelle erreicht"
        return None

    def _cooldown_active(self, symbol: str, ts: pd.Timestamp) -> str | None:
        until, _, reason = self.store.cooldown(symbol)
        if until is not None and ts < until:
            return f"Cooldown bis {until.isoformat()} ({reason})"
        return None

    def _active_symbols(self) -> set[str]:
        return {t.pair for t in self.journal.trades(active_only=True)}

    # -- Schritt -----------------------------------------------------------------------------
    def seed_history(self, data: dict[str, pd.DataFrame]) -> int:
        """Lädt Vorlaufdaten ohne zu handeln (z. B. beim ersten Start) und markiert die
        Zeitschritte als verarbeitet. Gibt die Anzahl der Schritte zurück."""
        with self._lock:
            stamps = sorted(set().union(*[set(df.index) for df in data.values()]))
            for ts in stamps:
                ts = pd.Timestamp(ts)
                if self.store.step_done(ts):
                    continue
                for sym, df in data.items():
                    if ts in df.index:
                        row = df.loc[ts]
                        self._append(
                            sym,
                            ts,
                            Bar(
                                float(row["open"]),
                                float(row["high"]),
                                float(row["low"]),
                                float(row["close"]),
                                float(row["volume"]),
                            ),
                        )
                self.store.mark_step(ts, ts + self.cfg.step)
            return len(stamps)

    def step(
        self, ts: pd.Timestamp, bars: dict[str, Bar], allow_entries: bool = True
    ) -> StepReport:
        """``allow_entries=False``: verpasste Kerzen nachholen – Exits ja, neue Einstiege nein."""
        with self._lock:
            self._catchup = not allow_entries
            ts = (
                pd.Timestamp(ts).tz_convert("UTC")
                if pd.Timestamp(ts).tzinfo
                else pd.Timestamp(ts, tz="UTC")
            )
            report = StepReport(ts)
            if self.store.step_done(ts):
                report.skipped = True
                return report
            previous_day = self.now.date() if self.now.year > 1970 else None
            self.now = ts + self.cfg.step
            for sym, bar in bars.items():
                self._append(sym, ts, bar)
            self._execute_pending(ts, bars, report)
            for sym, bar in bars.items():
                self.gw.process_bar(sym, bar.open, bar.high, bar.low, bar.close)
            self.executor.sync()
            self._book_closed(report)
            self._mark_to_market(bars)
            if previous_day is not None and previous_day != self.now.date():
                self.write_daily_report(previous_day.isoformat())
            report.blocked_reason = self.entries_blocked()
            if report.blocked_reason is None:
                self._find_signals(ts, bars, report)
            self.store.set_json("paper_exchange", self.gw.to_state())
            self.store.mark_step(ts, self.now)
            self.store.set_health("last_step", ts.isoformat(), self.now)
            return report

    def _execute_pending(self, ts: pd.Timestamp, bars: dict[str, Bar], report: StepReport) -> None:
        pending = sorted(self.store.pending_intents(), key=lambda kv: -kv[1].get("score", 0))
        for key, p in pending:
            sym = p["symbol"]
            reason = self.entries_blocked() or self._cooldown_active(sym, ts)
            if sym not in bars:
                reason = reason or "keine aktuelle Kerze (Daten fehlen)"
            if reason:
                self.store.resolve_intent(key, "expired", reason, ts)
                report.rejected[key] = reason
                continue
            bar = bars[sym]
            self._set_book(sym, bar.open, bar.volume)
            sl, tp = bar.open - p["stop_distance"], bar.open + p["take_distance"]
            try:
                trade = self.executor.open_position(
                    sym, sl, tp, self.ledger.capital_limit(self.cfg.capital_eur)
                )
            except PlanRejectedError as exc:
                why = "; ".join(f"{c.name}: {c.detail}" for c in exc.plan.failed())
                self.store.resolve_intent(key, "rejected", why, ts)
                report.rejected[key] = why
                continue
            except TradingHaltedError as exc:
                self.store.resolve_intent(key, "rejected", str(exc), ts)
                report.rejected[key] = str(exc)
                continue
            except Exception as exc:  # technischer Fehler -> Cooldown, kein Retry
                until = ts + self.cfg.error_cooldown_bars * self.cfg.step
                _, losses, _ = self.store.cooldown(sym)
                self.store.set_cooldown(sym, until, losses, f"technischer Fehler: {exc}")
                self.store.resolve_intent(key, "error", str(exc), ts)
                self.journal.log("ERROR", f"Ausführung {key} fehlgeschlagen: {exc}")
                report.rejected[key] = f"Fehler: {exc}"
                continue
            if trade.state is TradeState.PROTECTED:
                self.store.save_trade_meta(trade.id, key, {**p, "entry_ts": ts.isoformat()})
                report.executed.append(trade.id)
                self.store.resolve_intent(key, "executed", f"trade {trade.id}", ts)
            else:
                self.store.resolve_intent(key, str(trade.state), f"trade {trade.id}", ts)
                report.rejected[key] = str(trade.state)

    def _book_closed(self, report: StepReport) -> None:
        for t in self.journal.trades():
            if t.state is not TradeState.CLOSED or t.realized_pnl is None:
                continue
            key = f"trade:{t.id}"
            if self.ledger.has(key):
                continue
            closed = (
                dt.datetime.fromisoformat(t.closed_at) if t.closed_at else self.now.to_pydatetime()
            )
            self.ledger.book(key, t.realized_pnl, closed, note=t.exit_reason)
            meta = self.store.trade_meta(t.id) or {}
            oos = self._oos_regime_expectancy(meta.get("strategy"), t.pair, meta.get("regime"))
            analysis = analyze_trade(t, meta, self.history(t.pair), oos)
            try:
                legs = self.gw.orders_by_client_ref(t.client_ref)
            except Exception:
                legs = []
            analysis["intrabar_ambiguous"] = any(o.ambiguous for o in legs)
            if analysis["intrabar_ambiguous"]:
                analysis["findings"].append(
                    "SL und TP in derselben Kerze – Reihenfolge unbekannt, "
                    f"gewertet nach Regel '{self.gw.ambiguous_policy}'"
                )
            self.store.save_analysis(t.id, t.closed_at or closed.isoformat(), analysis)
            self._update_cooldown(t.pair, t.realized_pnl)
            report.closed.append(t.id)

    def _oos_regime_expectancy(
        self, strategy: str | None, symbol: str, regime: str | None
    ) -> float | None:
        lookup = getattr(self.probability, "_by_key", None)
        if not lookup or strategy is None:
            return None
        for key in ((strategy, symbol, regime), (strategy, "*", regime)):
            s = lookup.get(key)
            if s is not None and s.n >= 5:
                return s.expectancy_r
        return None

    def _update_cooldown(self, symbol: str, pnl: float) -> None:
        _, losses, _ = self.store.cooldown(symbol)
        losses = losses + 1 if pnl <= 0 else 0
        bars = min(self.cfg.max_cooldown_bars, self.cfg.base_cooldown_bars * 2**losses)
        reason = f"{losses} Verlust(e) in Folge" if losses else "nach Trade-Abschluss"
        self.store.set_cooldown(symbol, self.now + (bars - 1) * self.cfg.step, losses, reason)

    def _mark_to_market(self, bars: dict[str, Bar]) -> None:
        balances = self.gw.balances()
        closes = {
            s: (
                bars[s].close
                if s in bars
                else (self.history(s)["close"].iloc[-1] if not self.history(s).empty else 0.0)
            )
            for s in self.gw.rules
        }
        positions_value = 0.0
        for sym, rules in self.gw.rules.items():
            bal = balances.get(rules.base)
            if bal and bal.total > 0:
                positions_value += bal.total * closes.get(sym, 0.0)
        unrealized = 0.0
        fee = self.gw.fees.taker
        for t in self.journal.trades(active_only=True):
            px = closes.get(t.pair, t.entry_avg_price)
            unrealized += t.filled_volume * px * (1 - fee) - (
                t.filled_volume * t.entry_avg_price + t.entry_fee
            )
        realized_today = self.journal.realized_pnl_on(self.now.date())
        limit = self.executor.daily_loss_limit_eur()
        status = "ok"
        if realized_today + unrealized <= -limit and not self.store.get(self._day_halt_key()):
            self.store.set(
                self._day_halt_key(),
                f"realisiert {realized_today:.4f} + unrealisiert {unrealized:.4f}",
            )
            self.journal.log(
                "WARNING",
                "Tagesverlust-Abschaltschwelle erreicht",
                {"limit": limit, "realized": realized_today, "unrealized": unrealized},
            )
        if self.store.get(self._day_halt_key()):
            status = "tagesstopp"
        if self.entries_blocked() and status == "ok":
            status = "gesperrt"
        b = self.ledger.balances()
        cash = balances.get("ZEUR").total if "ZEUR" in balances else 0.0
        self.store.save_equity(
            self.now,
            {
                "equity": cash + positions_value,
                "cash": cash,
                "positions_value": positions_value,
                "trading_balance": b.trading_balance,
                "payout_reserve": b.payout_reserve,
                "longterm_portfolio": b.longterm_portfolio,
                "tax_reserve": b.tax_reserve,
                "unrecovered": b.unrecovered_losses,
                "realized_today": realized_today,
                "unrealized": unrealized,
                "open_positions": len(self._active_symbols()),
                "status": status,
            },
        )

    def _data_flags(self, symbol: str, ts: pd.Timestamp) -> list[str]:
        hist = self.history(symbol)
        flags = []
        if len(hist) < self.cfg.min_history_bars:
            flags.append(f"zu wenig Historie ({len(hist)} < {self.cfg.min_history_bars})")
        elif (hist.index[-1] - hist.index[-2]) != self.cfg.step:
            flags.append("Datenlücke vor der aktuellen Kerze")
        if hist.iloc[-5:][["open", "high", "low", "close"]].isna().any().any():
            flags.append("fehlende Kurswerte")
        if is_stale(hist.index[-1] if not hist.empty else None, self.cfg.timeframe, self.now):
            flags.append("veraltete Daten")
        return flags

    def _find_signals(self, ts: pd.Timestamp, bars: dict[str, Bar], report: StepReport) -> None:
        active = self._active_symbols()
        capital = self.ledger.capital_limit(self.cfg.capital_eur)
        returns = {s: self.history(s)["close"].pct_change().iloc[-100:] for s in active}
        candidates: list[SignalCandidate] = []
        for sym, bar in bars.items():
            raws = [r for r in self.source.signals_at(sym, ts, self.history(sym)) if r.entry]
            if not raws:
                continue
            flags = self._data_flags(sym, ts)
            block = (
                (f"Datenqualität: {', '.join(flags)}" if flags else None)
                or ("Position bereits offen" if sym in active else None)
                or self._cooldown_active(sym, self.now)
            )
            for raw in raws:
                report.candidates_seen += 1
                cand = SignalCandidate(
                    sym,
                    raw.strategy,
                    ts,
                    bar.close,
                    raw.stop_distance,
                    raw.take_distance,
                    raw.regime,
                )
                if block or not (
                    math.isfinite(raw.stop_distance)
                    and raw.stop_distance > 0
                    and math.isfinite(raw.take_distance)
                    and raw.take_distance > 0
                ):
                    cand.rejected = block or "ungültige SL/TP-Abstände"
                else:
                    est = self.probability.estimate(
                        raw.strategy, sym, raw.regime, cand.reward_risk, raw.features
                    )
                    cand.p_win, cand.p_source, cand.model_version = (
                        est.p_win,
                        est.source,
                        est.model_version,
                    )
                    cand.p_fill = fill_probability(
                        capital, bar.close * bar.volume, self.cfg.max_participation
                    )
                    evaluate_costs(
                        cand,
                        self.gw.fees,
                        self.cfg.assumed_spread_bps,
                        self.cfg.expected_entry_slippage_bps,
                        self.risk.assumed_stop_slippage_bps,
                    )
                    cand.correlation_penalty = correlation_penalty(
                        self.history(sym)["close"].pct_change().iloc[-100:],
                        returns,
                        self.cfg.correlation_threshold,
                    )
                    if cand.expected_r < self.cfg.min_expected_r:
                        cand.rejected = (
                            f"Netto-EV {cand.expected_r:+.3f} R nach Kosten "
                            f"< {self.cfg.min_expected_r} ({cand.p_source})"
                        )
                candidates.append(cand)
        ranked = rank(candidates)
        best_per_symbol: dict[str, SignalCandidate] = {}
        for c in ranked:
            best_per_symbol.setdefault(c.symbol, c)
        for c in candidates:
            chosen = best_per_symbol.get(c.symbol) is c
            status = "intent" if chosen else "rejected"
            reason = c.rejected or (None if chosen else "schwächeres Signal desselben Assets")
            self.store.log_signal(
                ts,
                c.symbol,
                c.strategy,
                c.regime,
                c.p_win,
                c.p_source,
                c.expected_r,
                c.score,
                status,
                reason,
            )
        for c in best_per_symbol.values():
            payload: dict[str, Any] = {
                "symbol": c.symbol,
                "strategy": c.strategy,
                "signal_ts": ts.isoformat(),
                "stop_distance": c.stop_distance,
                "take_distance": c.take_distance,
                "regime": c.regime,
                "p_win": c.p_win,
                "p_source": c.p_source,
                "model_version": c.model_version,
                "score": c.score,
                "expected_r": c.expected_r,
                "ev_per_unit": c.ev_per_unit,
                "loss_net_per_unit": c.loss_net_per_unit,
                "win_net_per_unit": c.win_net_per_unit,
                "ref_price": c.ref_price,
                "spread_bps": self.cfg.assumed_spread_bps,
            }
            if self.store.add_intent(c.idempotency_key, c.symbol, c.strategy, ts, payload):
                report.new_intents.append(c.idempotency_key)

    # -- Berichte --------------------------------------------------------------------------------
    def write_daily_report(self, day: str) -> str:
        md, payload = daily_report(day, self.store.analyses(day))
        self.store.save_daily_report(day, md, payload)
        return md
