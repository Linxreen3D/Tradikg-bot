"""Echtgeld-Trendfolge auf Kraken Spot (200-Tage-Regel) – kleines, fest gedeckeltes Budget.

Gleiche Regel wie ``trading/allocation.py`` (Paper/Schnelldurchlauf), aber mit echten Orders:

* Einstieg nach Tagesschluss über SMA 200: Kauf-Limit **post-only** (Maker) zum Schlusskurs
  bzw. darunter. Nicht ausgeführt bis zum nächsten Tagesschluss → stornieren und als
  marktnahes IOC-Limit (Ask + 0,5 %) kaufen.
* Sofort nach dem Kauf ein **Notfall-Stop direkt bei Kraken** (``stop-loss``, Verkauf der
  gekauften Menge) 20 % unter dem höchsten Schluss; wird täglich nachgezogen. Er schützt auch,
  wenn der Mac schläft.
* Ausstieg nach Tagesschluss unter SMA 200: Stop stornieren, Verkauf-Limit post-only; nicht
  ausgeführt bis zum nächsten Tagesschluss → Market-Verkauf.
* Nach ausgelöstem Stop erst wieder Einstieg nach neuem Kreuzen von unten.

Sicherheit:
* Budget hart gedeckelt (``MAX_BUDGET_EUR``); je Coin höchstens Budget × Anteil / Anzahl Coins.
* Der Bot handelt **nur seine eigenen Mengen** (aus seinen Orders), nie andere Bestände.
* Jede Order trägt eine eindeutige ``userref``; bei unklarem Ergebnis wird nicht blind
  wiederholt, sondern bei Kraken nachgesehen.
* Abbruch, wenn der API-Key Auszahlungsrechte hat oder Margin-Positionen existieren.
* Kill-Switch-Datei: keine neuen Käufe, Stops bleiben aktiv.
* ``dry_run``: Kraken prüft jede Order nur (``validate=true``), nichts wird ausgeführt oder
  gespeichert.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from tradikg_bot.costs import KRAKEN_SPOT_TAKER
from tradikg_bot.live.models import OrderRejectedError, OutcomeUnknownError

MAX_BUDGET_EUR = 50.0
KIND = {"buy": 1, "buy_fallback": 2, "stop": 3, "sell": 4, "sell_fallback": 5}


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

    def __post_init__(self) -> None:
        if not 0 < self.budget_eur <= MAX_BUDGET_EUR:
            raise LiveSafetyError(f"Budget muss in (0, {MAX_BUDGET_EUR:.0f}] € liegen")
        if not 0 < self.invest_fraction <= 1:
            raise LiveSafetyError("invest_fraction muss in (0, 1] liegen")

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

    @classmethod
    def load(cls, path: Path) -> LiveState:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        return cls({s: SymbolState(**v) for s, v in raw.get("symbols", {}).items()})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"symbols": {s: asdict(v) for s, v in self.symbols.items()}}, indent=2)
        )
        tmp.replace(path)  # atomar


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
    ) -> None:
        self.gw, self.cfg, self.closes, self.kill = gateway, cfg, closes, kill_switch
        self.state_path, self.dry_run, self.log = state_path, dry_run, log
        self.state = LiveState.load(state_path)

    # -- Kraken-Aufrufe ------------------------------------------------------------------------
    def _ref(self, day: str, sym: str, kind: str) -> int:
        return (
            int(pd.Timestamp(day).strftime("%y%m%d")) * 100
            + (self.cfg.symbols.index(sym) + 1) * 10
            + KIND[kind]
        )

    def _submit(self, sym: str, kind: str, day: str, params: dict[str, Any]) -> str | None:
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

    # -- ein Durchgang -------------------------------------------------------------------------
    def run_once(self) -> LiveState:
        if self.gw.has_withdrawal_permission():
            raise LiveSafetyError(
                "API-Key hat Auszahlungsrechte – bitte Key ohne Auszahlung anlegen"
            )
        if self.gw.has_margin_positions():
            raise LiveSafetyError("Margin-Positionen vorhanden – der Bot handelt nur Spot")
        balances = self.gw.balances()
        self._eur_used = 0.0
        for sym in self.cfg.symbols:
            st = self.state.symbols.setdefault(sym, SymbolState())
            self._sync(sym, st, balances)
            closes = self.closes(sym)
            if len(closes) >= self.cfg.sma:
                self._decide(sym, st, closes, balances)
            self._ensure_stop(sym, st)
            self._save()
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
            if above and remaining >= self.cfg.dust_eur and not self.kill.exists():
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
            self._cancel_and_settle(
                sym, st, st.sell_txid, "Verkauf-Limit nicht ausgeführt", buy=False
            )
            st.sell_txid = None
        if st.sell_day and st.units > 0 and st.sell_day != day:
            st.sell_txid = self._submit(
                sym,
                "sell_fallback",
                day,
                {
                    "type": "sell",
                    "ordertype": "market",
                    "volume": rules.format_volume(rules.round_volume_down(st.units)),
                },
            )
            st.sell_day = None
        elif st.units > 0 and not above:
            self._cancel(st.stop_txid, "Ausstieg per Signal")
            st.stop_txid, st.stop_price, st.stop_units = None, 0.0, 0.0
            price = rules.round_price_up(max(close, book.best_ask))
            st.sell_txid = self._submit(
                sym,
                "sell",
                day,
                {
                    "type": "sell",
                    "ordertype": "limit",
                    "oflags": "post",
                    "price": rules.format_price(price),
                    "volume": rules.format_volume(rules.round_volume_down(st.units)),
                },
            )
            st.sell_day = day
        elif st.units > 0:
            st.peak = max(st.peak, close)
        elif above and not st.blocked and not st.buy_txid:
            if self.kill.exists():
                self.log(f"[KILL] {sym}: keine neuen Käufe (Kill Switch)")
            else:
                self._buy(sym, st, day, close, book, rules, balances)
        st.last_candle = day

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
        """Notfall-Stop bei Kraken setzen bzw. nachziehen (nur für eigene Menge)."""
        if st.units <= 0 or st.sell_txid:
            return
        rules = self.gw.pair_rules(sym)
        target = rules.round_price_down(max(st.peak, st.entry_price) * (1 - self.cfg.stop_frac))
        volume = rules.round_volume_down(st.units)
        if st.stop_txid and target <= st.stop_price * 1.001 and volume == st.stop_units:
            return
        self._cancel(st.stop_txid, f"Stop nachziehen {st.stop_price:.2f} → {target:.2f}")
        day = st.last_candle or str(pd.Timestamp.now(tz="UTC").date())
        txid = self._submit(
            sym,
            "stop",
            day,
            {
                "type": "sell",
                "ordertype": "stop-loss",
                "price": rules.format_price(target),
                "volume": rules.format_volume(volume),
            },
        )
        if txid or self.dry_run:
            st.stop_txid, st.stop_price, st.stop_units = txid, target, volume
