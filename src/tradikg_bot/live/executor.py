"""Ausführungs-Zustandsmaschine mit fail-closed-Verhalten.

Zustände eines Trades (persistiert im Journal, siehe ``TradeState``)::

    SUBMITTING ──ack──▶ ENTRY_ACCEPTED ──SL+TP bestätigt──▶ PROTECTED ──SL|TP──▶ CLOSED
        │                    │                                  │
        │ abgelehnt          │ keine Ausführung                 │ Schutz fehlt / OCO defekt
        ▼                    ▼                                  ▼
     REJECTED             NO_FILL                    UNRESOLVED + SAFE_HALT

Grundsätze:
* **Write-Ahead:** Jede Absicht wird vor dem Senden im Journal gespeichert.
* **Keine blinden Wiederholungen:** Bei Timeout oder unklarer Antwort wird über
  ``client_ref`` abgeglichen, nie erneut gesendet.
* **SAFE_HALT:** Ist der Schutz nicht innerhalb der Frist bestätigt, bleibt der
  Zustand unklar, ist ein Restbestand nicht absicherbar, ist das OCO defekt oder
  tauchen unbekannte Orders oder Bestände auf, wird der Handel gesperrt. Die
  Sperre bleibt auch nach einem Neustart bestehen und lässt sich nur manuell
  aufheben. Schutzorders werden dabei **nie** storniert.
* **Neustart:** ``reconcile()`` gleicht beim Start und vor jedem Trade alle aktiven
  Journal-Einträge mit dem Börsenzustand ab.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.gate import LiveApproval, check_live_gate
from tradikg_bot.live.gateway import ExchangeGateway
from tradikg_bot.live.journal import ExecutionJournal, TradeRecord, TradeState
from tradikg_bot.live.models import (
    BracketNotSupportedError,
    BracketRequest,
    ExchangeOrder,
    LegRole,
    OrderRejectedError,
    OrderStatus,
    OutcomeUnknownError,
)
from tradikg_bot.live.precheck import TradePlan, plan_trade


class TradingHaltedError(RuntimeError):
    pass


class PlanRejectedError(RuntimeError):
    def __init__(self, plan: TradePlan):
        super().__init__("Vorab-Prüfung fehlgeschlagen:\n" + plan.summary())
        self.plan = plan


@dataclass
class ReconcileReport:
    checked_trades: int = 0
    closed_trades: list[int] = field(default_factory=list)
    halted: bool = False
    issues: list[str] = field(default_factory=list)


def _legs(orders: list[ExchangeOrder], role: LegRole) -> list[ExchangeOrder]:
    return [o for o in orders if o.role is role]


class LiveExecutor:
    def __init__(
        self,
        gateway: ExchangeGateway,
        journal: ExecutionJournal,
        config: LiveRiskConfig | None = None,
        approval: LiveApproval | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], dt.date] = lambda: dt.datetime.now(dt.UTC).date(),
    ) -> None:
        self.gw = gateway
        self.journal = journal
        self.cfg = config or LiveRiskConfig()
        self.approval = approval
        self._clock = clock
        self._sleep = sleep
        self._today = today

    # -- Status --------------------------------------------------------------------------
    @property
    def halted(self) -> bool:
        return bool(self.journal.halt_reason)

    def _halt(self, reason: str, trade: TradeRecord | None = None) -> None:
        if trade is not None and trade.state.is_active:
            self.journal.update_trade(trade.id, state=TradeState.UNRESOLVED)
        self.journal.set_halt(reason)

    def remaining_daily_budget(self) -> float:
        """Tageslimit minus heutiger realisierter Verlust minus Worst-Case offener Positionen."""
        realized_today = self.journal.realized_pnl_on(self._today())
        open_risk = 0.0
        for t in self.journal.trades(active_only=True):
            if t.filled_volume > 0:
                stop_fill = t.stop_loss_price * (1 - self.cfg.assumed_stop_slippage_bps * 1e-4)
                entry_value = t.filled_volume * t.entry_avg_price
                fee_rate = t.entry_fee / entry_value if entry_value > 0 else 0.01
                open_risk += max(
                    0.0, entry_value + t.entry_fee - t.filled_volume * stop_fill * (1 - fee_rate)
                )
        return self.cfg.daily_loss_limit_eur + min(0.0, realized_today) - open_risk

    # -- Abgleich (Start, vor jedem Trade, periodisch) ------------------------------------
    def reconcile(self) -> ReconcileReport:
        report = ReconcileReport()
        try:
            if self.gw.has_margin_positions():
                self._halt("Margin-Positionen auf dem Konto gefunden")
            active = [t for t in self.journal.trades(active_only=True) if t.venue == self.gw.name]
            for trade in active:
                report.checked_trades += 1
                self._reconcile_trade(trade, report)
            self._check_untracked(report)
        except Exception as exc:  # Börse nicht erreichbar o. Ä. -> unklarer Zustand
            report.issues.append(f"Abgleich fehlgeschlagen: {exc}")
            self._halt(f"Abgleich fehlgeschlagen: {exc}")
        report.halted = self.halted
        return report

    def _reconcile_trade(self, trade: TradeRecord, report: ReconcileReport) -> None:
        orders = self.gw.orders_by_client_ref(trade.client_ref)
        if trade.state is TradeState.UNRESOLVED:
            report.issues.append(f"Trade {trade.id} ist ungeklärt")
            self._halt(f"Trade {trade.id} ungeklärt – manuelle Prüfung erforderlich")
            return
        if trade.state is TradeState.SUBMITTING and not orders:
            # Order wurde evtl. gesendet, ist aber nicht auffindbar -> nicht raten.
            report.issues.append(f"Trade {trade.id}: Einstieg nicht auffindbar")
            self._halt(
                f"Trade {trade.id}: Sendestatus unbekannt (client_ref {trade.client_ref})", trade
            )
            return
        if trade.state in (TradeState.SUBMITTING, TradeState.ENTRY_ACCEPTED):
            done = self._evaluate_entry(trade, orders, final=False)
            if done is None:  # Einstieg/Schutz noch in Bearbeitung -> mit Frist abwarten
                done = self._await_protection(self.journal.get_trade(trade.id))
            if done.state in (TradeState.SUBMITTING, TradeState.ENTRY_ACCEPTED):
                self._halt(f"Trade {trade.id}: Schutzorders nach Neustart nicht bestätigt", done)
            return
        if trade.state is TradeState.PROTECTED:
            self._evaluate_protection(trade, orders, report)

    def _evaluate_entry(
        self, trade: TradeRecord, orders: list[ExchangeOrder], final: bool
    ) -> TradeRecord | None:
        """Prüft Einstieg und Schutz. ``final=True``: keine weitere Wartezeit möglich."""
        entries = _legs(orders, LegRole.ENTRY) or [o for o in orders if o.txid == trade.entry_txid]
        if not entries:
            return None if not final else trade
        entry = entries[0]
        trade = self.journal.update_trade(
            trade.id,
            state=TradeState.ENTRY_ACCEPTED,
            entry_txid=entry.txid,
            filled_volume=entry.filled,
            entry_avg_price=entry.avg_price,
            entry_fee=entry.fee,
        )
        if not entry.status.is_final:
            return None if not final else trade
        if entry.filled <= 0:
            return self.journal.update_trade(trade.id, state=TradeState.NO_FILL)
        rules = self.gw.pair_rules(trade.pair)
        if entry.filled < rules.ordermin:
            self._halt(
                f"Trade {trade.id}: Teilausführung {entry.filled} < ordermin {rules.ordermin} "
                "– Restbestand nicht börsenseitig absicherbar",
                trade,
            )
            return self.journal.get_trade(trade.id)
        sl = [o for o in _legs(orders, LegRole.STOP_LOSS) if o.status is OrderStatus.OPEN]
        tp = [o for o in _legs(orders, LegRole.TAKE_PROFIT) if o.status is OrderStatus.OPEN]
        covered = (
            abs(sum(o.volume for o in sl) - entry.filled) < 1e-12
            and abs(sum(o.volume for o in tp) - entry.filled) < 1e-12
        )
        if covered:
            return self.journal.update_trade(
                trade.id,
                state=TradeState.PROTECTED,
                stop_loss_txid=",".join(o.txid for o in sl),
                take_profit_txid=",".join(o.txid for o in tp),
            )
        return None if not final else trade

    def _evaluate_protection(
        self, trade: TradeRecord, orders: list[ExchangeOrder], report: ReconcileReport
    ) -> None:
        legs = _legs(orders, LegRole.STOP_LOSS) + _legs(orders, LegRole.TAKE_PROFIT)
        filled = [o for o in legs if o.filled > 0]
        open_legs = [o for o in legs if o.status is OrderStatus.OPEN]
        exited = sum(o.filled for o in filled)
        if not filled:
            sl_open = any(o.role is LegRole.STOP_LOSS for o in open_legs)
            tp_open = any(o.role is LegRole.TAKE_PROFIT for o in open_legs)
            if not (sl_open and tp_open):
                report.issues.append(f"Trade {trade.id}: Schutzorder fehlt")
                self._halt(f"Trade {trade.id}: Stop-Loss oder Take-Profit nicht mehr aktiv", trade)
            return
        if exited >= trade.filled_volume - 1e-12 and not open_legs:
            proceeds = sum(o.filled * o.avg_price for o in filled)
            exit_fee = sum(o.fee for o in filled)
            pnl = (
                proceeds - exit_fee - trade.filled_volume * trade.entry_avg_price - trade.entry_fee
            )
            reason = "+".join(sorted({str(o.role) for o in filled}))
            self.journal.update_trade(
                trade.id,
                state=TradeState.CLOSED,
                exit_reason=reason,
                exit_volume=exited,
                exit_avg_price=proceeds / exited,
                exit_fee=exit_fee,
                realized_pnl=pnl,
                closed_at=dt.datetime.now(dt.UTC).isoformat(),
            )
            self.journal.log("INFO", f"Trade {trade.id} geschlossen ({reason}), PnL {pnl:.4f} EUR")
            report.closed_trades.append(trade.id)
            if self.journal.realized_pnl_on(self._today()) <= -self.cfg.daily_loss_limit_eur:
                self.journal.log("WARNING", "Tagesverlustlimit erreicht – keine neuen Trades heute")
            return
        # Ein Bein ausgeführt, das andere noch offen -> OCO defekt / Überverkauf möglich
        report.issues.append(f"Trade {trade.id}: OCO-Gegenorder noch offen")
        self._halt(f"Trade {trade.id}: SL/TP ausgelöst, Gegenorder nicht storniert", trade)

    def _check_untracked(self, report: ReconcileReport) -> None:
        tracked_refs = {t.client_ref for t in self.journal.trades(active_only=True)}
        for order in self.gw.open_orders():
            if order.client_ref not in tracked_refs:
                report.issues.append(f"Unbekannte offene Order {order.txid}")
                self._halt(f"Unbekannte offene Order {order.txid} auf dem Konto")
        balances = self.gw.balances()
        for pair in self.cfg.allowed_pairs:
            rules = self.gw.pair_rules(pair)
            expected = sum(
                t.filled_volume
                for t in self.journal.trades(active_only=True)
                if self.gw.pair_rules(t.pair).base == rules.base
            )
            actual = balances.get(rules.base).total if rules.base in balances else 0.0
            if abs(actual - expected) > rules.ordermin * 0.01:
                report.issues.append(f"Bestand {rules.base}: {actual} statt {expected}")
                self._halt(
                    f"Unerwarteter Bestand {rules.base}: {actual} (erwartet {expected}). "
                    "Pilot benötigt ein Konto ohne Fremdbestände."
                )

    # -- Planung & Ausführung ---------------------------------------------------------------
    def plan(
        self,
        pair: str,
        stop_loss_price: float,
        take_profit_price: float,
        max_notional_eur: float,
    ) -> TradePlan:
        return plan_trade(
            self.gw,
            self.cfg,
            pair=pair,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            max_notional_eur=max_notional_eur,
            cumulative_realized_pnl=self.journal.realized_pnl_total(),
            remaining_daily_budget_eur=self.remaining_daily_budget(),
        )

    def open_position(
        self,
        pair: str,
        stop_loss_price: float,
        take_profit_price: float,
        max_notional_eur: float,
    ) -> TradeRecord:
        self.reconcile()
        if self.halted:
            raise TradingHaltedError(f"SAFE_HALT aktiv: {self.journal.halt_reason}")
        if self.journal.trades(active_only=True):
            raise TradingHaltedError("Es ist bereits eine Position aktiv (Pilot: max. eine)")
        check_live_gate(self.gw, self.approval, pair, self.cfg.pilot_capital_eur)
        caps = self.gw.capabilities
        if not caps.native_bracket:
            self.journal.log("WARNING", "Trade verweigert: kein API-Bracket", {"pair": pair})
            raise BracketNotSupportedError(caps.evidence)

        plan = self.plan(pair, stop_loss_price, take_profit_price, max_notional_eur)
        self.journal.log("INFO", "Vorab-Prüfung", {"ok": plan.ok, "summary": plan.summary()})
        if not plan.ok:
            raise PlanRejectedError(plan)

        ref = self.journal.new_client_ref()
        trade = self.journal.create_trade(
            client_ref=ref,
            venue=self.gw.name,
            pair=pair,
            requested_volume=plan.volume,
            entry_limit_price=plan.entry_limit_price,
            stop_loss_price=plan.stop_loss_price,
            take_profit_price=plan.take_profit_price,
        )
        request = BracketRequest(
            ref,
            pair,
            plan.volume,
            plan.entry_limit_price,
            plan.stop_loss_price,
            plan.take_profit_price,
        )
        try:
            txid = self.gw.submit_bracket(request)
            trade = self.journal.update_trade(
                trade.id, state=TradeState.ENTRY_ACCEPTED, entry_txid=txid
            )
        except OrderRejectedError as exc:
            self.journal.log("WARNING", f"Order abgelehnt: {exc}")
            return self.journal.update_trade(trade.id, state=TradeState.REJECTED)
        except OutcomeUnknownError as exc:
            self.journal.log("ERROR", f"Ergebnis unbekannt: {exc} – Abgleich über client_ref")
        except Exception as exc:
            self._halt(f"Unerwarteter Fehler beim Senden: {exc}", trade)
            raise
        return self._await_protection(trade)

    def _await_protection(self, trade: TradeRecord) -> TradeRecord:
        deadline = self._clock() + self.cfg.confirm_timeout_s
        last_error: Exception | None = None
        while True:
            try:
                orders = self.gw.orders_by_client_ref(trade.client_ref)
                done = self._evaluate_entry(trade, orders, final=False)
                if done is not None:
                    return done
                trade = self.journal.get_trade(trade.id)
            except Exception as exc:  # Abfrage gescheitert: weiter versuchen bis Frist
                last_error = exc
            if self._clock() >= deadline:
                break
            self._sleep(self.cfg.poll_interval_s)
        trade = self.journal.get_trade(trade.id)
        self._halt(
            f"Trade {trade.id}: Schutz nicht innerhalb {self.cfg.confirm_timeout_s}s bestätigt"
            + (f" (letzter Fehler: {last_error})" if last_error else ""),
            trade,
        )
        self._cancel_open_entry(trade)
        return self.journal.get_trade(trade.id)

    def _cancel_open_entry(self, trade: TradeRecord) -> None:
        """Storniert nur einen noch offenen *Einstieg* – niemals Schutzorders."""
        try:
            for o in self.gw.orders_by_client_ref(trade.client_ref):
                if o.role is LegRole.ENTRY and o.status is OrderStatus.OPEN:
                    self.gw.cancel_order(o.txid)
                    self.journal.log("WARNING", f"Offener Einstieg {o.txid} storniert")
        except Exception as exc:
            self.journal.log("ERROR", f"Stornierung des Einstiegs fehlgeschlagen: {exc}")

    def sync(self) -> ReconcileReport:
        """Periodischer Abgleich (z. B. jede Minute): Schließungen erkennen, Sperren setzen."""
        return self.reconcile()
