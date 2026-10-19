"""Echtgeld-Trendfolge auf Kraken Spot (200-Tage-Regel) – kleines, fest gedeckeltes Budget.

Gleiche Regel wie ``trading/allocation.py`` (Paper/Schnelldurchlauf), aber mit echten Orders:

* Einstieg nach Tagesschluss über SMA 200: Kauf-Limit **post-only** (Maker) zum Schlusskurs
  bzw. darunter. Nicht ausgeführt bis zum nächsten Tagesschluss → stornieren und als
  marktnahes IOC-Limit (Ask + 0,5 %) kaufen.
* Sofort nach dem Kauf ein **Notfall-Stop direkt bei Kraken** (``stop-loss``, Verkauf der
  gekauften Menge) 20 % unter dem höchsten Schluss; wird täglich nachgezogen. Er schützt auch,
  wenn der Mac schläft. Beim Nachziehen bleibt immer ein Stop offen: der neue wird zuerst
  gelegt, wenn die freie eigene Menge reicht, sonst wird der alte bei einem Fehlschlag
  wiederhergestellt.
* Ausstieg nach Tagesschluss unter SMA 200: der Notfall-Stop **bleibt**. Kraken bindet die
  Basismenge nur einmal (``hold_trade``); ein ruhendes Verkaufs-Limit würde den Stop verdrängen
  und bei einem Kurssturz nicht ausführen. Am nächsten Tagesschluss wird der Stop erst
  unmittelbar vor dem Market-Verkauf gelöst. Schlägt der Verkauf fehl, setzt derselbe Durchgang
  den Stop wieder. Ein noch offenes Limit aus einem älteren Lauf wird sofort durch den Stop ersetzt.
* Nach ausgelöstem Stop erst wieder Einstieg nach neuem Kreuzen von unten.

Sicherheit:
* Budget hart gedeckelt (``MAX_BUDGET_EUR``); je Coin höchstens Budget × Anteil / Anzahl Coins.
* Der Bot handelt **nur seine eigenen Mengen** (aus seinen Orders), nie andere Bestände.
* Jede Order trägt eine eindeutige ``userref``; bei unklarem Ergebnis wird nicht blind
  wiederholt, sondern bei Kraken nachgesehen.
* Abbruch, wenn der API-Key Auszahlungsrechte hat oder Margin-Positionen existieren.
* Kill-Switch-Datei: keine neuen Käufe, Stops bleiben aktiv.
* Optionale Tagesverlustgrenze (standardmäßig **aus**): Equity = die Scheibe des
  Bots, nicht das ganze Kraken-Konto. Das ist die mitgeführte Bot-Kasse (zu Beginn
  höchstens Budget abzüglich des Kaufpreises offener Positionen) plus die eigenen
  Mengen zum Bid. Einzahlungen und manuelle Trades ändern diese Kasse nicht.
  Fehlt ein Bid oder das Guthaben, gibt es in diesem Durchgang keine neuen Käufe;
  Verkäufe und Stops laufen weiter. Standard 5 % der Scheibe zum Beginn des Wiener
  Tages (00:00 Europe/Vienna), höchstens 10 %. Ist sie gerissen, keine neuen Käufe
  bis zum nächsten Wiener Tag. Es wird nichts zwangsverkauft.
* Optionale Drawdown-Sperre (standardmäßig **aus**): dieselbe Scheibe gegen ihren
  Höchststand. Standard 45 % darunter, nur enger. Sie sperrt neue Käufe, bis
  jemand ``tradikg live drawdown-reset`` ausführt. ``DRAWDOWN_HALT`` ist nur ein
  Hinweis; Löschen löst die Sperre nicht. Keine Zwangsverkäufe, Stops bleiben.
* ``dry_run``: Kraken prüft jede Order nur (``validate=true``), nichts wird ausgeführt.
  Orders und Kerzen werden nicht gespeichert. Eine ausgelöste Drawdown-Sperre schon,
  sofort, damit sie den nächsten Coin und den nächsten Durchgang sperrt.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from tradikg_bot.costs import KRAKEN_SPOT_TAKER
from tradikg_bot.live.models import OrderRejectedError, OutcomeUnknownError
from tradikg_bot.risk.limits import (
    DRAWDOWN_HALT_NAME,
    TREND_DAILY_LOSS_FRACTION,
    TREND_DAILY_LOSS_TIMEZONE,
    TREND_DRAWDOWN_FRACTION,
    DailyLossGuard,
    DrawdownGuard,
    validate_trend_daily_loss,
    validate_trend_drawdown,
)

MAX_BUDGET_EUR = 50.0
KIND = {"buy": 1, "buy_fallback": 2, "stop": 3, "sell": 4, "sell_fallback": 5}


def _balance_map(balances) -> dict[str, dict[str, float]]:
    if not balances:
        return {}
    return {
        asset: {"total": bal.total, "held": bal.held}
        for asset, bal in balances.items()
        if bal.total
    }


class LiveSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveTrendConfig:
    budget_eur: float = 20.0
    invest_fraction: float = 1.0
    symbols: tuple[str, ...] = ("XBTEUR", "ETHEUR")
    sma: int = 200
    stop_frac: float = 0.20
    dust_eur: float = 0.50  # Restmengen darunter gelten als „keine Position“
    # Tagesverlustgrenze. Im Echtbetrieb aus, bis sie bewusst eingeschaltet wird.
    # 5 % von ~20 € begrenzen einen schlechten Tag auf etwa 1 €; höchstens 10 %.
    daily_loss_enabled: bool = False
    daily_loss_fraction: float = TREND_DAILY_LOSS_FRACTION
    daily_loss_timezone: str = TREND_DAILY_LOSS_TIMEZONE
    # Drawdown vom Equity-Höchststand. Aus, bis sie bewusst eingeschaltet wird.
    # 45 % ist die weiteste Schwelle (−30 % hätte 2019 den Trend beendet).
    drawdown_enabled: bool = False
    drawdown_fraction: float = TREND_DRAWDOWN_FRACTION

    def __post_init__(self) -> None:
        if not 0 < self.budget_eur <= MAX_BUDGET_EUR:
            raise LiveSafetyError(f"Budget muss in (0, {MAX_BUDGET_EUR:.0f}] € liegen")
        if not 0 < self.invest_fraction <= 1:
            raise LiveSafetyError("invest_fraction muss in (0, 1] liegen")
        try:
            validate_trend_daily_loss(self.daily_loss_fraction, self.daily_loss_timezone)
            validate_trend_drawdown(self.drawdown_fraction)
        except ValueError as exc:
            raise LiveSafetyError(str(exc)) from exc

    @property
    def slot_eur(self) -> float:
        return self.budget_eur * self.invest_fraction / len(self.symbols)


@dataclass
class SymbolState:
    units: float = 0.0
    entry_price: float = 0.0
    peak: float = 0.0
    blocked: bool = False
    stop_txid: str | None = None
    stop_price: float = 0.0
    stop_units: float = 0.0
    buy_txid: str | None = None
    buy_day: str | None = None
    buy_eur: float = 0.0
    sell_txid: str | None = None
    sell_day: str | None = None
    last_candle: str | None = None
    realized_eur: float = 0.0


@dataclass
class LiveState:
    symbols: dict[str, SymbolState] = field(default_factory=dict)
    # Nur gesetzt, wenn die jeweilige Grenze an ist. Sonst fehlt der Schlüssel in der Datei.
    daily_loss: dict[str, Any] | None = None
    # EUR, die der Bot selbst noch hält. None, solange keine Grenze die Scheibe bewertet.
    # Einzahlungen und fremde Trades ändern den Wert nicht; nur eigene Käufe und Verkäufe.
    bot_cash_eur: float | None = None
    drawdown: dict[str, Any] | None = None

    @classmethod
    def load(cls, path: Path) -> LiveState:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        state = cls({s: SymbolState(**v) for s, v in raw.get("symbols", {}).items()})
        state.daily_loss = raw.get("daily_loss")
        state.drawdown = raw.get("drawdown")
        if raw.get("bot_cash_eur") is not None:
            state.bot_cash_eur = float(raw["bot_cash_eur"])
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"symbols": {s: asdict(v) for s, v in self.symbols.items()}}
        if self.daily_loss is not None:
            payload["daily_loss"] = self.daily_loss
        if self.bot_cash_eur is not None:
            payload["bot_cash_eur"] = self.bot_cash_eur
        if self.drawdown is not None:
            payload["drawdown"] = self.drawdown
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)  # atomar


def release_drawdown_halt(state_path: Path) -> None:
    """Löst die Drawdown-Sperre von Hand. Die Kill-Switch-Datei bleibt unberührt.

    Schreibt eine neue ``reset_id`` in die Zustandsdatei. Ein laufender Prozess löst
    nur, wenn diese Marke höher ist als die gemerkte. ``DRAWDOWN_HALT`` wird nur als
    Hinweis entfernt; die Datei allein zu löschen lässt die Sperre bestehen. Der
    nächste Durchgang setzt den Höchststand auf die dann gesehene Equity.
    """
    flag = state_path.parent / DRAWDOWN_HALT_NAME
    flag.unlink(missing_ok=True)
    if not state_path.exists():
        return
    state = LiveState.load(state_path)
    if state.drawdown is None:
        return
    try:
        seen = int(state.drawdown.get("reset_id") or 0)
    except (TypeError, ValueError):
        seen = 0
    # ``reset_id`` ist der einzige Schalter für einen laufenden Prozess. ``halted: false``
    # auf der Platte reicht nicht: der Guard läuft je Coin, bevor der neue Stand liegt.
    state.drawdown = {
        **state.drawdown,
        "halted": False,
        "halted_at": None,
        "rebase": True,
        "reset_id": seen + 1,
    }
    state.save(state_path)


class LiveTrendRunner:
    def __init__(
        self,
        gateway,
        state_path: Path,
        cfg: LiveTrendConfig,
        closes: Callable[[str], pd.Series],
        kill_switch: Path,
        dry_run: bool = True,
        log: Callable[[str], None] = print,
        snapshot_path: Path | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.gw, self.cfg, self.closes, self.kill = gateway, cfg, closes, kill_switch
        self.state_path, self.dry_run, self.log = state_path, dry_run, log
        self.snapshot_path = snapshot_path
        self.now = now or (lambda: dt.datetime.now(dt.UTC))
        self._fresh = not state_path.exists()
        self.state = LiveState.load(state_path)
        self._valuation_block = False
        self._last_submit_error: str | None = None
        self._naked_noted: set[str] = set()
        # Aus: kein Guard, keine Zustandsänderung. An: Referenz und Sperre überleben Neustarts.
        self.guard: DailyLossGuard | None = None
        if cfg.daily_loss_enabled:
            self.guard = DailyLossGuard.restore(
                self.state.daily_loss,
                limit=cfg.daily_loss_fraction,
                timezone=cfg.daily_loss_timezone,
            )
        # Hinweisdatei, nicht der Kill-Switch. Löschen hebt die Sperre nicht auf.
        self.drawdown_file = state_path.parent / DRAWDOWN_HALT_NAME
        self.drawdown: DrawdownGuard | None = None
        if cfg.drawdown_enabled:
            self.drawdown = DrawdownGuard.restore(self.state.drawdown, limit=cfg.drawdown_fraction)

    # -- Kraken-Aufrufe ------------------------------------------------------------------------
    def _ref(self, day: str, sym: str, kind: str) -> int:
        return (
            int(pd.Timestamp(day).strftime("%y%m%d")) * 100
            + (self.cfg.symbols.index(sym) + 1) * 10
            + KIND[kind]
        )

    def _submit(self, sym: str, kind: str, day: str, params: dict[str, Any]) -> str | None:
        self._last_submit_error = None
        rules = self.gw.pair_rules(sym)
        params = {"pair": rules.altname, "userref": self._ref(day, sym, kind), **params}
        what = f"{kind} {sym} {params.get('volume')} @ {params.get('price', 'Markt')}"
        if self.dry_run:
            self.gw.add_order(params, validate=True)
            self.log(f"[PROBE] Kraken hat geprüft (nicht ausgeführt): {what}")
            return None
        # Idempotenz: gibt es diese Order (gleiche userref) schon, z. B. nach einem Absturz
        # zwischen Senden und Speichern, wird sie übernommen statt doppelt gesendet.
        existing = [
            o
            for o in self.gw.orders_by_client_ref(params["userref"])
            if not o.status.is_final or o.filled > 0
        ]
        if existing:
            self.log(f"[VORHANDEN] {what} → {existing[0].txid} (nicht erneut gesendet)")
            return existing[0].txid
        try:
            txid = self.gw.add_order(params)
        except OutcomeUnknownError as exc:
            found = self.gw.orders_by_client_ref(params["userref"])
            found = [o for o in found if not o.status.is_final] or found
            if not found:
                self.log(
                    f"[UNKLAR] {what}: {exc} – nicht gefunden, kein Wiederholen in diesem Lauf"
                )
                return None
            txid = found[0].txid
        except OrderRejectedError as exc:
            self._last_submit_error = str(exc)
            self.log(f"[ABGELEHNT] {what}: {exc}")
            return None
        self.log(f"[ORDER] {what} → {txid}")
        return txid

    def _save(self) -> None:
        if not self.dry_run:
            self.state.save(self.state_path)

    def _cancel(self, txid: str | None, why: str) -> None:
        if not txid:
            return
        if self.dry_run:
            self.log(f"[PROBE] würde stornieren: {txid} ({why})")
            return
        try:
            self.gw.cancel_order(txid)
            self.log(f"[STORNO] {txid} ({why})")
        except OrderRejectedError as exc:  # z. B. schon ausgeführt – der nächste Abgleich zeigt es
            self.log(f"[STORNO] {txid} nicht möglich: {exc}")

    def _refuse_if_state_lost(self) -> None:
        """Ohne Zustandsdatei kennt der Bot seine Positionen nicht. Liegen bei Kraken noch offene
        Orders mit Bot-Kennung (z. B. Stops), würde er sonst ein zweites Mal kaufen."""
        if not self._fresh or self.dry_run:
            return
        n = len(self.cfg.symbols)
        mine = [
            o.txid
            for o in self.gw.open_orders()
            if o.client_ref
            and o.client_ref >= 10**7
            and o.client_ref % 10 in KIND.values()
            and 1 <= o.client_ref // 10 % 10 <= n
        ]
        if mine:
            raise LiveSafetyError(
                f"Zustandsdatei {self.state_path} fehlt, aber bei Kraken sind Bot-Orders offen "
                f"({', '.join(mine)}). Datei wiederherstellen oder diese Orders selbst prüfen."
            )
        self._fresh = False

    # -- Tagesverlustgrenze --------------------------------------------------------------------
    def _now(self) -> dt.datetime:
        now = self.now()
        if now.tzinfo is None:
            return now.replace(tzinfo=dt.UTC)
        return now

    def _mark(self, sym: str) -> float | None:
        """Bid der eigenen Menge. None heißt: keine Bewertung, nicht der Einstandspreis."""
        try:
            bid = self.gw.order_book(sym, 1).best_bid
        except Exception:
            return None
        if bid is None or float(bid) <= 0:
            return None
        return float(bid)

    def _equity_needed(self) -> bool:
        return self.guard is not None or getattr(self, "drawdown", None) is not None

    def _quote_asset(self) -> str:
        if not self.cfg.symbols:
            return "ZEUR"
        return self.gw.pair_rules(self.cfg.symbols[0]).quote

    def _deployed_cost(self) -> float:
        """Kaufpreis der eigenen Mengen. Offene Kauf-Orders stecken noch in der Bot-Kasse."""
        return sum(st.units * st.entry_price for st in self.state.symbols.values() if st.units > 0)

    def _ensure_bot_cash(self, balances) -> None:
        """Legt die Bot-Kasse einmal an: min(Kontostand, Budget abzüglich Kaufpreis)."""
        if not self._equity_needed() or self.state.bot_cash_eur is not None or balances is None:
            return
        quote = self._quote_asset()
        account = balances[quote].total if quote in balances else 0.0
        room = max(0.0, self.cfg.budget_eur - self._deployed_cost())
        self.state.bot_cash_eur = min(max(0.0, account), room)

    def _note_valuation_block(self, why: str) -> None:
        self._valuation_block = True
        if getattr(self, "_valuation_logged", False):
            return
        self._valuation_logged = True
        self.log(f"[BEWERTUNG] {why}. Keine neuen Käufe in diesem Durchgang.")

    def _read_balances(self):
        """None bei einem Lesefehler. Der Durchgang läuft weiter, neue Käufe nicht."""
        try:
            balances = self.gw.balances()
        except Exception as exc:
            self._note_valuation_block(f"Guthaben nicht lesbar ({exc})")
            return None
        self._ensure_bot_cash(balances)
        return balances

    def _account_equity(self) -> float | None:
        """Bot-Scheibe: eigene Kasse plus eigene Mengen zum Bid. None = keine Bewertung.

        Das übrige EUR auf dem Konto zählt nicht. Ohne Bid wird der Einstandspreis
        nicht eingesetzt.
        """
        if self.state.bot_cash_eur is None:
            self._note_valuation_block("Bot-Kasse unbekannt (Guthaben fehlt)")
            return None
        marked = 0.0
        for sym, st in self.state.symbols.items():
            if st.units <= 0:
                continue
            bid = self._mark(sym)
            if bid is None:
                self._note_valuation_block(
                    f"{sym}: kein Bid, Einstandspreis wird nicht als Kurs eingesetzt"
                )
                return None
            marked += st.units * bid
        return self.state.bot_cash_eur + marked

    def _poll_daily_loss(self) -> None:
        """Aktualisiert die Tagesreferenz. Ausgeschaltet: kein Aufruf, kein Effekt."""
        if self.guard is None:
            return
        equity = self._account_equity()
        if equity is None:
            return  # Start-Equity bleibt; Käufe sind für diesen Durchgang schon gesperrt
        previous_day = self.guard.current_day
        breached = self.guard.observe(self._now(), equity)
        self.state.daily_loss = self.guard.snapshot()
        tz = self.cfg.daily_loss_timezone
        if previous_day is not None and self.guard.current_day != previous_day:
            self.log(
                f"[TAGESLIMIT] Neuer Tag {self.guard.current_day} ({tz}): "
                f"Start-Equity {self.guard.day_start_equity:.2f} €, "
                f"Grenze {self.guard.floor:.2f} € ({self.cfg.daily_loss_fraction:.0%}). "
                "Neue Käufe sind wieder erlaubt, solange diese Grenze hält."
            )
        if breached:
            base = self.guard.day_start_equity
            pnl = equity - base
            pct = pnl / base if base else 0.0
            self.log(
                f"[TAGESLIMIT] Tagesverlust {pnl:.2f} € ({pct:.1%} von {base:.2f} € "
                f"Start-Equity) erreicht die Grenze von {self.cfg.daily_loss_fraction:.0%}. "
                f"Keine neuen Käufe bis 00:00 {tz}. "
                "Verkäufe und Kraken-Stops laufen weiter."
            )

    def _daily_loss_blocks(self, sym: str) -> bool:
        if self.guard is None or not self.guard.halted:
            return False
        self.log(
            f"[TAGESLIMIT] {sym}: keine neuen Käufe "
            f"(Tagesverlustgrenze {self.cfg.daily_loss_fraction:.0%} seit "
            f"{self.guard.day_start_equity:.2f} €, bis 00:00 {self.cfg.daily_loss_timezone})"
        )
        return True

    def _new_buys_blocked(self, sym: str) -> bool:
        """Tagesgrenze, Drawdown oder fehlende Bewertung. Verkäufe und Stops nicht."""
        if self._valuation_block:
            self.log(f"[BEWERTUNG] {sym}: keine neuen Käufe (keine Bewertung in diesem Durchgang)")
            return True
        return self._daily_loss_blocks(sym) or self._drawdown_blocks(sym)

    def _saved_drawdown(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        try:
            saved = json.loads(self.state_path.read_text()).get("drawdown")
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return saved if isinstance(saved, dict) else None

    def _apply_saved_drawdown_reset(self) -> None:
        """Nur eine neue ``reset_id`` löst. ``halted: false`` auf der Platte zählt nicht."""
        if self.drawdown is None:
            return
        saved = self._saved_drawdown()
        if saved is None:
            return
        try:
            saved_id = int(saved.get("reset_id") or 0)
        except (TypeError, ValueError):
            return
        if saved_id <= self.drawdown.reset_id:
            return
        self.drawdown.reset_id = saved_id
        self.drawdown.reset()
        self.log(
            "[DRAWDOWN] Sperre per tradikg live drawdown-reset gelöst. "
            "Der Höchststand wird auf die jetzige Equity gesetzt. "
            "Verkäufe und Kraken-Stops waren die ganze Zeit aktiv."
        )

    def _persist_drawdown_halt(self) -> None:
        """Sperre sofort auf die Platte, auch im Probelauf, vor dem nächsten Coin.

        Im Probelauf wird nur der Drawdown-Block gemerged. Orders und Kerzen bleiben
        ungespeichert. Eine neuere ``reset_id`` auf der Platte wird nicht überschrieben.
        """
        if self.drawdown is None:
            return
        saved = self._saved_drawdown()
        if saved is not None:
            try:
                if int(saved.get("reset_id") or 0) > self.drawdown.reset_id:
                    return
            except (TypeError, ValueError):
                pass
        self.state.drawdown = self.drawdown.snapshot()
        if not self.dry_run:
            self.state.save(self.state_path)
            return
        raw: dict[str, Any] = {}
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text())
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, json.JSONDecodeError):
                raw = {}
        raw["drawdown"] = self.state.drawdown
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        tmp.replace(self.state_path)

    def _poll_drawdown(self) -> None:
        """Höchststand nachziehen. Lösen nur per ``tradikg live drawdown-reset``."""
        if self.drawdown is None:
            return
        self._apply_saved_drawdown_reset()
        equity = self._account_equity()
        if equity is None:
            return  # Höchststand bleibt; Käufe sind für diesen Durchgang schon gesperrt
        breached = self.drawdown.observe(equity, self._now())
        self.state.drawdown = self.drawdown.snapshot()
        if breached:
            self._persist_drawdown_halt()
            if not self.dry_run:
                self._write_drawdown_file(equity)
            dd = abs(self.drawdown.drawdown_from(equity))
            self.log(
                f"[DRAWDOWN] Equity {equity:.2f} € ist {dd:.0%} unter dem Höchststand "
                f"{self.drawdown.peak_equity:.2f} € (Grenze −{self.cfg.drawdown_fraction:.0%}). "
                "Keine neuen Käufe, bis die Sperre von Hand gelöst wird "
                "(tradikg live drawdown-reset). "
                "Verkäufe und Kraken-Stops laufen weiter."
            )

    def _write_drawdown_file(self, equity: float) -> None:
        self.drawdown_file.parent.mkdir(parents=True, exist_ok=True)
        self.drawdown_file.write_text(
            "Drawdown-Sperre: keine neuen Käufe.\n"
            f"Höchststand {self.drawdown.peak_equity:.2f} €, zuletzt {equity:.2f} €, "
            f"Grenze −{self.cfg.drawdown_fraction:.0%}.\n"
            "Lösen nur mit: tradikg live drawdown-reset\n"
            "Diese Datei ist ein Hinweis. Löschen hebt die Sperre nicht auf.\n"
            "Das ist nicht die Kill-Switch-Datei.\n"
            "Stops bei Kraken bleiben liegen. Es wird nichts automatisch verkauft.\n"
        )

    def _drawdown_blocks(self, sym: str) -> bool:
        if self.drawdown is None or not self.drawdown.halted:
            return False
        equity = self._account_equity()
        if equity is None:
            self.log(f"[DRAWDOWN] {sym}: keine neuen Käufe (Sperre aktiv)")
            return True
        dd = abs(self.drawdown.drawdown_from(equity))
        self.log(
            f"[DRAWDOWN] {sym}: keine neuen Käufe "
            f"({dd:.0%} unter Höchststand {self.drawdown.peak_equity:.2f} €, "
            "nur manuell lösbar)"
        )
        return True

    def _drawdown_snapshot(self) -> dict[str, Any]:
        if self.drawdown is None:
            return {"enabled": False, "limit": self.cfg.drawdown_fraction}
        equity = self._account_equity()
        if equity is None:
            return {
                "enabled": True,
                "halted": self.drawdown.halted,
                "limit": self.cfg.drawdown_fraction,
                "peak_equity": self.drawdown.peak_equity,
                "equity": None,
                "drawdown": None,
                "rebase": self.drawdown.rebase,
            }
        return {
            "enabled": True,
            "halted": self.drawdown.halted,
            "limit": self.cfg.drawdown_fraction,
            "peak_equity": self.drawdown.peak_equity,
            "equity": equity,
            "drawdown": self.drawdown.drawdown_from(equity),
            "rebase": self.drawdown.rebase,
        }

    # -- ein Durchgang -------------------------------------------------------------------------
    def run_once(self) -> LiveState:
        if self.gw.has_withdrawal_permission():
            raise LiveSafetyError(
                "API-Key hat Auszahlungsrechte – bitte Key ohne Auszahlung anlegen"
            )
        self._refuse_if_state_lost()
        if self.gw.has_margin_positions():
            raise LiveSafetyError("Margin-Positionen vorhanden – der Bot handelt nur Spot")
        self._valuation_block = False
        self._valuation_logged = False
        balances = self._read_balances()
        self._eur_used = 0.0
        self._naked_noted = set()
        for sym in self.cfg.symbols:
            st = self.state.symbols.setdefault(sym, SymbolState())
            self._sync(sym, st, balances)
            self._poll_daily_loss()
            self._poll_drawdown()
            closes = self.closes(sym)
            if len(closes) >= self.cfg.sma:
                self._decide(sym, st, closes, balances)
            self._release_unprotected_limit_sell(sym, st)
            self._ensure_stop(sym, st)
            self._alarm_if_naked(sym, st)
            self._save()
        self._write_snapshot()
        return self.state

    def _snapshot_balances(self):
        """Kontostand fürs Dashboard. Ein Lesefehler bricht den Handel nicht ab."""
        try:
            return self.gw.balances()
        except Exception:
            return {}

    def _write_snapshot(self) -> None:
        """Echter Kontostand für das Dashboard (nur lesend; der Schlüssel bleibt im Bot-Prozess)."""
        if self.dry_run or self.snapshot_path is None:
            return
        prices: dict[str, float | None] = {}
        for sym in self.cfg.symbols:
            try:
                prices[sym] = self.gw.order_book(sym, 1).best_bid
            except Exception:  # Kurs fehlt – Snapshot trotzdem schreiben
                prices[sym] = None
        snap = {
            "updated": pd.Timestamp.now(tz="UTC").isoformat(),
            "budget_eur": self.cfg.budget_eur,
            "balances": _balance_map(self._snapshot_balances()),
            "prices": prices,
            "symbols": {s: asdict(v) for s, v in self.state.symbols.items()},
            "drawdown": self._drawdown_snapshot(),
        }
        tmp = self.snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=1))
        tmp.replace(self.snapshot_path)

    def buy_now(self) -> LiveState:
        """Vom Nutzer ausgelöst: wartendes Kauf-Limit sofort durch einen marktnahen Kauf ersetzen
        (IOC, Taker) – nur wenn die Regel ohnehin kaufen würde – danach sofort Stop setzen."""
        if self.gw.has_withdrawal_permission():
            raise LiveSafetyError(
                "API-Key hat Auszahlungsrechte – bitte Key ohne Auszahlung anlegen"
            )
        self._refuse_if_state_lost()
        self._valuation_block = False
        self._valuation_logged = False
        balances = self._read_balances()
        today = self._now().date().isoformat()
        for sym in self.cfg.symbols:
            st = self.state.symbols.setdefault(sym, SymbolState())
            self._sync(sym, st, balances)
            self._poll_daily_loss()
            self._poll_drawdown()
            closes = self.closes(sym)
            above = len(closes) >= self.cfg.sma and float(closes.iloc[-1]) > float(
                closes.iloc[-self.cfg.sma :].mean()
            )
            if st.units > 0 or not above or st.blocked or self.kill.exists():
                self.log(f"[JETZT] {sym}: kein Sofortkauf (gekauft, Abwärtstrend, Sperre, Kill)")
                continue
            if self._new_buys_blocked(sym):
                continue
            if st.buy_txid:
                self._cancel_and_settle(sym, st, st.buy_txid, "Sofortkauf statt Limit", buy=True)
                st.buy_txid = None
                balances = self._read_balances()  # stornierte Limits geben das Geld wieder frei
            if balances is None:
                continue
            rules, book = self.gw.pair_rules(sym), self.gw.order_book(sym, 1)
            eur_free = balances[rules.quote].available if rules.quote in balances else 0.0
            eur = min(self.cfg.slot_eur - st.units * st.entry_price, eur_free)
            price = rules.round_price_up(book.best_ask * 1.005)
            vol = rules.round_volume_down(eur / (price * (1 + KRAKEN_SPOT_TAKER)))
            if vol * price < max(rules.ordermin * price, rules.costmin):
                self.log(f"[JETZT] {sym}: {eur:.2f} € reichen nicht für die Mindestorder")
                continue
            st.buy_eur = eur
            st.buy_txid = self._submit(
                sym,
                "buy_fallback",
                today,
                {
                    "type": "buy",
                    "ordertype": "limit",
                    "timeinforce": "IOC",
                    "price": rules.format_price(price),
                    "volume": rules.format_volume(vol),
                },
            )
            self._save()
        balances = self._read_balances()
        for sym in self.cfg.symbols:  # Ausführung übernehmen und sofort Stop bei Kraken setzen
            st = self.state.symbols[sym]
            self._sync(sym, st, balances)
            self._ensure_stop(sym, st)
            self._save()
        self._write_snapshot()
        return self.state

    def _sync(self, sym: str, st: SymbolState, balances) -> None:
        """Ausführungen der eigenen Orders übernehmen (Kraken ist die Wahrheit)."""
        txids = [t for t in (st.buy_txid, st.sell_txid, st.stop_txid) if t]
        orders = self.gw.query_orders(txids)
        buy = orders.get(st.buy_txid or "")
        if buy and buy.status.is_final:
            self._apply_buy(sym, st, buy)
        for txid in (st.sell_txid, st.stop_txid):
            o = orders.get(txid or "")
            if o and o.status.is_final:
                self._apply_sell(sym, st, o)
        if balances is None:
            return  # Ausführungen sind übernommen; ohne Guthaben keine Bestände kürzen
        base = self.gw.pair_rules(sym).base
        held = balances.get(base).total if base in balances else 0.0
        # Teilverkäufe noch offener eigener Orders werden erst beim Abschluss verbucht
        held += sum(
            o.filled
            for t, o in orders.items()
            if t in (st.sell_txid, st.stop_txid) and not o.status.is_final
        )
        if st.units > held + 1e-12:  # z. B. von Hand verkauft – nie mehr verkaufen als da ist
            self.log(f"[ABGLEICH] {sym}: Bot-Menge {st.units} > Bestand {held} – angepasst")
            st.units = held
        if st.units * max(st.entry_price, 1e-9) < self.cfg.dust_eur and not st.buy_txid:
            st.units, st.entry_price, st.peak = 0.0, 0.0, 0.0

    def _apply_buy(self, sym: str, st: SymbolState, o) -> None:
        if o.filled > 0:
            spent = o.filled * o.avg_price + o.fee
            if self.state.bot_cash_eur is not None:
                self.state.bot_cash_eur = max(0.0, self.state.bot_cash_eur - spent)
            cost = st.units * st.entry_price + o.filled * o.avg_price
            st.units += o.filled
            st.entry_price = cost / st.units
            st.peak = max(st.peak, o.avg_price)
            self.log(f"[KAUF] {sym} {o.filled} zu {o.avg_price:.2f} (Gebühr {o.fee:.4f} €)")
        st.buy_txid, st.buy_day = None, None

    def _apply_sell(self, sym: str, st: SymbolState, o) -> None:
        is_stop = o.txid == st.stop_txid
        if o.filled > 0:
            pnl = o.filled * (o.avg_price - st.entry_price) - o.fee
            if self.state.bot_cash_eur is not None:
                proceeds = o.filled * o.avg_price - o.fee
                self.state.bot_cash_eur = max(0.0, self.state.bot_cash_eur + proceeds)
            st.realized_eur += pnl
            st.units = max(0.0, st.units - o.filled)
            why = "Notfall-Stop" if is_stop else "Signal"
            self.log(
                f"[VERKAUF] {sym} {o.filled} zu {o.avg_price:.2f} ({why}), Ergebnis ≈ {pnl:+.2f} €"
            )
            st.blocked = st.blocked or is_stop
        if is_stop:
            st.stop_txid, st.stop_price, st.stop_units = None, 0.0, 0.0
        else:
            st.sell_txid = None
        if st.units <= 0:
            st.sell_day = None

    def _cancel_and_settle(self, sym: str, st: SymbolState, txid: str, why: str, buy: bool) -> None:
        """Stornieren und eine eventuelle Teilausführung übernehmen, bevor nachgeordert wird."""
        self._cancel(txid, why)
        if self.dry_run:
            return
        o = self.gw.query_orders([txid]).get(txid)
        if o is not None:
            (self._apply_buy if buy else self._apply_sell)(sym, st, o)

    def _decide(self, sym: str, st: SymbolState, closes: pd.Series, balances) -> None:
        day = str(closes.index[-1].date())
        if st.last_candle == day:
            return
        close = float(closes.iloc[-1])
        sma = float(closes.iloc[-self.cfg.sma :].mean())
        above = close > sma
        rules = self.gw.pair_rules(sym)
        book = self.gw.order_book(sym, 1)
        self.log(
            f"[TAG {day}] {sym}: Schluss {close:.2f}, SMA {self.cfg.sma} {sma:.2f} → "
            f"{'über' if above else 'unter'}"
        )
        if (
            st.blocked
        ):  # Sperre nach Stop endet mit einem Schluss unter SMA – auch an verpassten Tagen
            sma_all = closes.rolling(self.cfg.sma).mean()
            ref = pd.Timestamp(st.last_candle, tz="UTC") if st.last_candle else closes.index[0]
            since = closes.index > ref
            st.blocked = not bool((closes[since] < sma_all[since]).any())
        # Vortags-Aufträge, die (noch) nicht ausgeführt sind → Taker-Fallback
        if st.buy_txid:
            self._cancel_and_settle(sym, st, st.buy_txid, "Kauf-Limit nicht ausgeführt", buy=True)
            st.buy_txid = None
            remaining = st.buy_eur - st.units * st.entry_price
            if (
                above
                and remaining >= self.cfg.dust_eur
                and not self.kill.exists()
                and not self._new_buys_blocked(sym)
            ):
                vol = rules.round_volume_down(
                    remaining / (book.best_ask * 1.005 * (1 + KRAKEN_SPOT_TAKER))
                )
                st.buy_txid = self._submit(
                    sym,
                    "buy_fallback",
                    day,
                    {
                        "type": "buy",
                        "ordertype": "limit",
                        "timeinforce": "IOC",
                        "price": rules.format_price(rules.round_price_up(book.best_ask * 1.005)),
                        "volume": rules.format_volume(vol),
                    },
                )
        if st.sell_txid:
            if self._cancel_if_gone(sym, st, st.sell_txid, "Verkauf nicht ausgeführt", buy=False):
                st.sell_txid = None
            else:
                self.log(
                    f"[ALARM] {sym}: Verkauf {st.sell_txid} liegt weiter – "
                    "kein zweiter Verkauf, solange die Menge gebunden ist"
                )
        if not st.sell_txid and st.sell_day and st.units > 0 and st.sell_day != day:
            self._market_exit(sym, st, day, rules)
        elif st.units > 0 and not above:
            # Kein Post-only: das Limit würde die vom Stop gebundene Menge brauchen und den
            # Kurssturz nicht auffangen. Der Stop bleibt, bis _market_exit ihn ablöst.
            if st.sell_day != day:
                st.sell_day = day
                self.log(
                    f"[AUSSTIEG] {sym}: Tagesschluss unter SMA – Notfall-Stop bleibt bis zum "
                    "Market-Verkauf am nächsten Tagesschluss"
                )
        elif st.units > 0:
            st.peak = max(st.peak, close)
        elif above and not st.blocked and not st.buy_txid:
            if self.kill.exists():
                self.log(f"[KILL] {sym}: keine neuen Käufe (Kill Switch)")
            elif self._new_buys_blocked(sym):
                pass  # bereits protokolliert; Verkäufe und Stops oben laufen weiter
            else:
                self._buy(sym, st, day, close, book, rules, balances)
        st.last_candle = day

    def _cancel_if_gone(self, sym: str, st: SymbolState, txid: str, why: str, *, buy: bool) -> bool:
        """Storniert ``txid``. True, wenn die Order danach nicht mehr offen ist – eine
        Teilausführung ist dann verbucht. False, wenn sie liegen bleibt; der Zustand
        zeigt dann weiter auf diese Order."""
        self._cancel(txid, why)
        order = self.gw.query_orders([txid]).get(txid)
        if order is not None and not order.status.is_final:
            return False
        if order is not None:
            (self._apply_buy if buy else self._apply_sell)(sym, st, order)
        return True

    def _market_exit(self, sym: str, st: SymbolState, day: str, rules) -> None:
        """Market-Verkauf. Der Stop wird erst hier gelöst, weil Kraken die Basismenge nur
        einmal bindet. Bleibt der Stop offen oder wird der Verkauf abgelehnt, ist die Position
        am Ende des Durchgangs wieder börsenseitig geschützt (``_ensure_stop``)."""
        if self.dry_run:
            if st.stop_txid:
                self._cancel(st.stop_txid, "Market-Ausstieg – Stop gibt die Menge frei")
            if st.units > 0 and (volume := rules.round_volume_down(st.units)) > 0:
                self._submit(
                    sym,
                    "sell_fallback",
                    day,
                    {
                        "type": "sell",
                        "ordertype": "market",
                        "volume": rules.format_volume(volume),
                    },
                )
            return
        if st.stop_txid:
            txid = st.stop_txid
            if not self._cancel_if_gone(
                sym, st, txid, "Market-Ausstieg – Stop gibt die Menge frei", buy=False
            ):
                self.log(
                    f"[ALARM] {sym}: Stop {txid} ließ sich nicht lösen – "
                    "kein Market-Verkauf, der Stop bleibt"
                )
                return
            if st.stop_txid == txid:  # Order weg, aber ohne Abschlussmeldung
                st.stop_txid, st.stop_price, st.stop_units = None, 0.0, 0.0
        if st.units <= 0:
            st.sell_day = None
            return
        volume = rules.round_volume_down(st.units)
        if volume <= 0:
            self.log(
                f"[ALARM] {sym}: Restmenge {st.units} ist nicht handelbar – Stop wird benötigt"
            )
            return
        st.sell_txid = self._submit(
            sym,
            "sell_fallback",
            day,
            {
                "type": "sell",
                "ordertype": "market",
                "volume": rules.format_volume(volume),
            },
        )
        if not st.sell_txid:
            self.log(
                f"[ALARM] {sym}: Market-Ausstieg abgelehnt oder unklar – "
                "Stop wird im selben Durchgang wieder gesetzt"
            )

    def _release_unprotected_limit_sell(self, sym: str, st: SymbolState) -> None:
        """Ein ruhendes Verkaufs-Limit ist kein Schutz: es bindet die Menge und fängt einen
        Kurssturz nicht auf. Market-Aufträge bleiben – die verkaufen in den Bid."""
        if not st.sell_txid or self.dry_run:
            return
        order = self.gw.query_orders([st.sell_txid]).get(st.sell_txid)
        if order is None or order.status.is_final or order.ordertype == "market":
            return
        self.log(
            f"[SCHUTZ] {sym}: Verkaufs-Limit {st.sell_txid} verdrängt den Stop – "
            "stornieren und Stop wieder setzen"
        )
        txid = st.sell_txid
        if not self._cancel_if_gone(sym, st, txid, "Limit-Verkauf ohne Stop", buy=False):
            self.log(
                f"[ALARM] {sym}: Verkaufs-Limit {txid} ließ sich nicht stornieren – "
                "Stop kann die gebundene Menge nicht decken"
            )
            return
        st.sell_txid = None

    def _alarm_if_naked(self, sym: str, st: SymbolState) -> None:
        if self.dry_run or st.units <= 0 or st.stop_txid or st.sell_txid:
            return
        if sym in self._naked_noted:
            return
        self.log(
            f"[ALARM] {sym}: POSITION OHNE STOP – {st.units} Einheiten, "
            "weder Notfall-Stop noch Verkaufsorder. Sofort prüfen."
        )

    def _buy(self, sym, st, day, close, book, rules, balances) -> None:
        eur_free = (
            balances[rules.quote].available if rules.quote in balances else 0.0
        ) - self._eur_used
        eur = min(self.cfg.slot_eur, eur_free)
        price = rules.round_price_down(min(close, book.best_bid))
        vol = rules.round_volume_down(eur / (price * (1 + KRAKEN_SPOT_TAKER)))
        need = max(rules.ordermin * price, rules.costmin)
        if vol * price < need:
            self.log(
                f"[SKIP] {sym}: {eur:.2f} € frei/zugeteilt reicht nicht für die "
                f"Mindestorder {need:.2f} €"
            )
            return
        st.buy_eur = eur
        self._eur_used += eur
        st.buy_txid = self._submit(
            sym,
            "buy",
            day,
            {
                "type": "buy",
                "ordertype": "limit",
                "oflags": "post",
                "price": rules.format_price(price),
                "volume": rules.format_volume(vol),
            },
        )
        st.buy_day = day

    def _ensure_stop(self, sym: str, st: SymbolState) -> None:
        """Notfall-Stop bei Kraken setzen bzw. nachziehen (nur für eigene Menge).

        Kraken bindet die Basismenge eines Verkaufs nur einmal (``hold_trade``). Ein zweiter
        Stop über die volle Position wird abgelehnt, solange der alte liegt. Deshalb: neuen
        Stop zuerst legen, wenn die freie *eigene* Menge reicht; sonst den alten lösen, den
        neuen versuchen und bei Fehlschlag den alten Preis wiederherstellen.

        Ein offener Verkauf (Market-Ausstieg) bindet die Menge selbst. Der Stop wird dann
        nicht angefasst. Fehlt der Verkauf, legt dieser Durchgang den Stop wieder.
        """
        if st.units <= 0 or st.sell_txid:
            return
        rules = self.gw.pair_rules(sym)
        target = rules.round_price_down(max(st.peak, st.entry_price) * (1 - self.cfg.stop_frac))
        volume = rules.round_volume_down(st.units)
        if volume <= 0:
            return
        if st.stop_txid and target <= st.stop_price * 1.001 and volume == st.stop_units:
            if self.dry_run or self._stop_is_open(st.stop_txid):
                return
            st.stop_txid = None  # Preis bleibt, falls der Ersatz scheitert
        day = st.last_candle or str(pd.Timestamp.now(tz="UTC").date())
        if self.dry_run:
            self._cancel(st.stop_txid, f"Stop nachziehen {st.stop_price:.2f} → {target:.2f}")
            self._submit_stop(sym, day, rules, target, volume)
            st.stop_txid, st.stop_price, st.stop_units = None, target, volume
            return
        self._move_stop(sym, st, day, rules, target, volume)

    def _submit_stop(self, sym: str, day: str, rules, price: float, volume: float) -> str | None:
        return self._submit(
            sym,
            "stop",
            day,
            {
                "type": "sell",
                "ordertype": "stop-loss",
                "price": rules.format_price(price),
                "volume": rules.format_volume(volume),
            },
        )

    def _stop_is_open(self, txid: str | None) -> bool:
        if not txid:
            return False
        order = self.gw.query_orders([txid]).get(txid)
        return order is not None and not order.status.is_final

    def _stop_matches(self, txid: str | None, price: float, volume: float) -> bool:
        if not txid:
            return False
        order = self.gw.query_orders([txid]).get(txid)
        if order is None or order.status.is_final or order.price is None:
            return False
        return (
            abs(order.price - price) <= max(abs(price) * 1e-6, 1e-8)
            and abs(order.volume - volume) <= 1e-8
        )

    def _rejected_for_funds(self) -> bool:
        return "insufficient" in (self._last_submit_error or "").lower()

    def _own_free(self, sym: str, st: SymbolState) -> float:
        """Eigene Münzen, die weder unser Stop noch eine fremde Order bindet.

        Nur diese Menge darf ein zweiter Stop belegen. Sonst würde er Bestände verkaufen,
        die nicht zu dieser Position gehören, oder an ``Insufficient funds`` scheitern.
        """
        locked = 0.0
        if st.stop_txid:
            order = self.gw.query_orders([st.stop_txid]).get(st.stop_txid)
            if order is not None and not order.status.is_final:
                locked = max(0.0, order.volume - order.filled)
        rules = self.gw.pair_rules(sym)
        bal = self.gw.balances().get(rules.base)
        exchange_free = bal.available if bal else 0.0
        return min(max(0.0, st.units - locked), max(0.0, exchange_free))

    def _release_stop(self, sym: str, st: SymbolState, txid: str, why: str) -> bool:
        """True, wenn der Stop nicht mehr offen ist. Eine Ausführung wird verbucht.
        Bleibt er offen, zeigt der Zustand weiter auf ihn."""
        self._cancel(txid, why)
        order = self.gw.query_orders([txid]).get(txid)
        if order is not None and not order.status.is_final:
            return False
        if order is not None:
            self._apply_sell(sym, st, order)
        elif st.stop_txid == txid:
            st.stop_txid, st.stop_price, st.stop_units = None, 0.0, 0.0
        return True

    def _move_stop(
        self, sym: str, st: SymbolState, day: str, rules, target: float, volume: float
    ) -> None:
        old_txid, old_price, old_units = st.stop_txid, float(st.stop_price), float(st.stop_units)
        if self._own_free(sym, st) + 1e-12 >= volume:
            txid = self._submit_stop(sym, day, rules, target, volume)
            if self._stop_matches(txid, target, volume):
                self._keep_new_stop(sym, st, txid, target, volume, old_txid)
                return
            idempotent_hit = bool(old_txid and txid == old_txid)
            keep_old = (
                self._stop_is_open(old_txid)
                and not idempotent_hit
                and not self._rejected_for_funds()
            )
            if keep_old:
                self.log(
                    f"[ALARM] {sym}: neuer Stop {target:.2f} abgelehnt – "
                    f"alter Stop {old_txid} bei {old_price:.2f} bleibt aktiv"
                )
                return
        if self._stop_is_open(old_txid):
            if not self._release_stop(
                sym, st, old_txid, f"Stop nachziehen {old_price:.2f} → {target:.2f}"
            ):
                self.log(
                    f"[ALARM] {sym}: alter Stop {old_txid} ließ sich nicht lösen – "
                    "er bleibt, der neue Preis wird nicht erzwungen"
                )
                return
            if st.units <= 0:
                return
            volume = rules.round_volume_down(st.units)
            target = rules.round_price_down(max(st.peak, st.entry_price) * (1 - self.cfg.stop_frac))
            if volume <= 0:
                self._alarm_naked_stop(sym, target, old_price)
                return
        self._place_or_restore(sym, st, day, rules, target, volume, old_price, old_units)

    def _keep_new_stop(
        self,
        sym: str,
        st: SymbolState,
        txid: str,
        target: float,
        volume: float,
        old_txid: str | None,
    ) -> None:
        old_stuck = (
            old_txid
            and old_txid != txid
            and self._stop_is_open(old_txid)
            and not self._release_stop(sym, st, old_txid, "neuer Stop steht, alten entfernen")
        )
        if old_stuck:
            self.log(
                f"[ALARM] {sym}: neuer Stop {txid} steht, alter Stop {old_txid} "
                "ließ sich nicht lösen"
            )
        if st.units <= 0:
            if self._stop_is_open(txid):
                self._cancel(txid, "Position bereits geschlossen")
            return
        st.stop_txid, st.stop_price, st.stop_units = txid, target, volume

    def _place_or_restore(
        self,
        sym: str,
        st: SymbolState,
        day: str,
        rules,
        target: float,
        volume: float,
        old_price: float,
        old_units: float,
    ) -> None:
        for attempt in (1, 2):
            txid = self._submit_stop(sym, day, rules, target, volume)
            if self._stop_matches(txid, target, volume):
                st.stop_txid, st.stop_price, st.stop_units = txid, target, volume
                return
            self.log(f"[ALARM] {sym}: Stop {target:.2f} Versuch {attempt} fehlgeschlagen")
        restore_vol = rules.round_volume_down(min(old_units, st.units)) if old_price > 0 else 0.0
        if old_price > 0 and restore_vol > 0:
            for attempt in (1, 2):
                txid = self._submit_stop(sym, day, rules, old_price, restore_vol)
                if self._stop_matches(txid, old_price, restore_vol):
                    st.stop_txid, st.stop_price, st.stop_units = txid, old_price, restore_vol
                    self.log(
                        f"[ALARM] {sym}: neuer Stop {target:.2f} gescheitert – alter Stop "
                        f"{old_price:.2f} für {restore_vol} wiederhergestellt ({txid})"
                    )
                    return
                if self._stop_matches(txid, target, volume):
                    st.stop_txid, st.stop_price, st.stop_units = txid, target, volume
                    return
                self.log(
                    f"[ALARM] {sym}: Wiederherstellen des Stops {old_price:.2f} "
                    f"Versuch {attempt} fehlgeschlagen"
                )
        st.stop_txid, st.stop_price, st.stop_units = None, 0.0, 0.0
        self._alarm_naked_stop(sym, target, old_price)

    def _alarm_naked_stop(self, sym: str, target: float, old_price: float) -> None:
        self._naked_noted.add(sym)
        self.log(
            f"[ALARM] {sym}: POSITION OHNE STOP. Neuer Stop {target:.2f} und "
            f"Wiederherstellung {old_price:.2f} sind gescheitert. Sofort in Kraken prüfen."
        )
