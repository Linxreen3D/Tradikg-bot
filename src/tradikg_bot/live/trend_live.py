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
* Budget hart gedeckelt (``MAX_BUDGET_EUR`` bleibt 50 und wird nicht angehoben). Der Trend-Bot
  handelt zusätzlich nur seine eigene Scheibe ``LIVE_SLICE_EUR`` (20 €), auch wenn das Konto
  mehr Guthaben hat. Käufe nehmen ``min(Rest der Scheibe, frei verfügbares Guthaben)`` –
  nie ``Balance.total``.
* Der Bot handelt **nur seine eigenen Mengen** (aus seinen Orders), nie andere Bestände.
* Jede Order trägt eine eindeutige ``userref``; bei unklarem Ergebnis wird nicht blind
  wiederholt, sondern bei Kraken nachgesehen.
* Abbruch, wenn der API-Key Auszahlungsrechte hat oder Margin-Positionen existieren.
* Kill-Switch-Datei: keine neuen Käufe, Stops bleiben aktiv.
* ``dry_run``: Kraken prüft jede Order nur (``validate=true``), nichts wird ausgeführt oder
  gespeichert.
* Übernahme vom Mac: kopierte Zustandsdatei wird geprüft und an Bot-Orders (``userref``)
  gemessen. Offene Bot-Stops werden übernommen, nicht ein zweites Mal gelegt. Eine mögliche
  zweite Instanz blockiert den Echtstart (``docs/CUTOVER.md``).
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
# Eigene Scheibe des Trend-Bots. Das Konto darf mehr enthalten; darüber wird nicht disponiert.
# MAX_BUDGET_EUR bleibt die unveränderte Obergrenze und wird hier nicht angehoben.
LIVE_SLICE_EUR = 20.0
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
        if self.budget_eur > LIVE_SLICE_EUR:
            raise LiveSafetyError(
                f"Der Trend-Bot handelt nur seine eigene Scheibe von {LIVE_SLICE_EUR:.0f} €"
            )
        if not 0 < self.invest_fraction <= 1:
            raise LiveSafetyError("invest_fraction muss in (0, 1] liegen")

    @property
    def own_slice_eur(self) -> float:
        """EUR, die dieser Bot einsetzen darf – nie mehr als die 20-€-Scheibe."""
        return min(self.budget_eur * self.invest_fraction, LIVE_SLICE_EUR)

    @property
    def slot_eur(self) -> float:
        return self.own_slice_eur / len(self.symbols)


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
        from tradikg_bot.live.cutover import read_state_file

        return read_state_file(path)

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
        snapshot_path: Path | None = None,
    ) -> None:
        self.gw, self.cfg, self.closes, self.kill = gateway, cfg, closes, kill_switch
        self.state_path, self.dry_run, self.log = state_path, dry_run, log
        self.snapshot_path = snapshot_path
        self.state = LiveState.load(state_path)
        self._eur_used = 0.0
        self._alone_checked = False

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

    def _tagged(self, sym: str) -> list[tuple[int, Any]]:
        from tradikg_bot.live.cutover import parse_userref

        idx = self.cfg.symbols.index(sym)
        rules = self.gw.pair_rules(sym)
        names = {sym, rules.altname, rules.pair}
        found = []
        for order in self.gw.open_orders():
            parsed = parse_userref(order.client_ref, len(self.cfg.symbols))
            if parsed is None or order.status.is_final:
                continue
            _day, slot, kind = parsed
            if slot == idx and order.pair in names:
                found.append((kind, order))
        return found

    def _reconcile(self) -> None:
        """Datei gegen Bot-Orders halten. Menge nur aus der Bot-Kennung, innerhalb der Scheibe."""
        from tradikg_bot.live.models import OrderSide

        notes: list[str] = []
        balances = self.gw.balances()
        notional = 0.0
        for sym in self.cfg.symbols:
            st = self.state.symbols.setdefault(sym, SymbolState())
            tagged = self._tagged(sym)
            stops = [
                o
                for kind, o in tagged
                if kind == KIND["stop"] and o.side == OrderSide.SELL and o.ordertype == "stop-loss"
            ]
            buys = [
                o
                for kind, o in tagged
                if kind in (KIND["buy"], KIND["buy_fallback"]) and o.side == OrderSide.BUY
            ]
            sells = [
                o
                for kind, o in tagged
                if kind in (KIND["sell"], KIND["sell_fallback"])
                and o.side == OrderSide.SELL
                and o.ordertype != "stop-loss"
            ]
            if len(stops) > 1:
                ids = ", ".join(o.txid for o in stops)
                raise LiveSafetyError(
                    f"{sym}: {len(stops)} Bot-Stops offen ({ids}). Nichts gesendet."
                )
            if len(buys) > 1:
                raise LiveSafetyError(f"{sym}: mehrere offene Bot-Käufe. Nichts gesendet.")
            if stops:
                self._adopt_stop(sym, st, stops[0], notes)
            elif (st.units > 0 or st.stop_txid) and not sells:
                self._drop_unverified(sym, st, notes)
            if buys:
                buy = buys[0]
                if st.buy_txid != buy.txid:
                    notes.append(f"{sym}: offenen Kauf {buy.txid} übernommen")
                st.buy_txid = buy.txid
                st.buy_eur = max(st.buy_eur, buy.volume * (buy.price or 0.0))
                if not st.last_candle:
                    from tradikg_bot.live.cutover import parse_userref

                    parsed = parse_userref(buy.client_ref, len(self.cfg.symbols))
                    if parsed:
                        st.last_candle = parsed[0]
            if sells and st.sell_txid != sells[0].txid:
                st.sell_txid = sells[0].txid
                notes.append(f"{sym}: offenen Verkauf {sells[0].txid} übernommen")
            base = self.gw.pair_rules(sym).base
            held = balances[base].total if base in balances else 0.0
            # Teilverkauf einer noch offenen Order ist schon vom Bestand weg, in ``units``
            # aber erst beim Abschluss. Sonst zieht der Fallback dieselbe Menge noch einmal ab.
            in_open_sell = sum(o.filled for o in (*stops, *sells))
            explainable = held + in_open_sell
            if st.units > explainable + 1e-8:
                notes.append(f"{sym}: Menge {st.units:.8f} > Bestand {explainable:.8f} – gekürzt")
                st.units = max(0.0, explainable)
            notional += max(0.0, st.units) * max(st.entry_price, 0.0)
            if st.buy_txid:
                notional += max(0.0, st.buy_eur)
            self.log(
                f"[ÜBERNAHME] {sym}: Menge {st.units:.8f} Einstieg {st.entry_price:.2f} "
                f"Stop {st.stop_txid or '–'} Kauf {st.buy_txid or '–'}"
            )
        if notional > self.cfg.own_slice_eur + 0.05:
            raise LiveSafetyError(
                f"Bot-Positionen {notional:.2f} € liegen über der Scheibe von "
                f"{self.cfg.own_slice_eur:.0f} €. Nichts gesendet."
            )
        for line in notes:
            self.log(f"[ÜBERNAHME] {line}")
        if not notes:
            self.log("[ÜBERNAHME] keine Korrektur")

    def _adopt_stop(self, sym: str, st: SymbolState, stop, notes: list[str]) -> None:
        from tradikg_bot.live.cutover import parse_userref

        vol = float(stop.volume)
        price = float(stop.price or 0.0)
        tol = max(1e-8, vol * 1e-6)
        same = st.stop_txid == stop.txid and abs(st.units - vol) <= tol
        if not same:
            notes.append(
                f"{sym}: Stop {stop.txid} userref {stop.client_ref} gilt, "
                f"Menge {vol:.8f} (Datei hatte {st.units:.8f})"
            )
            if not (st.entry_price > 0 and abs(st.units - vol) <= tol):
                if st.entry_price > 0:
                    notes.append(f"{sym}: Einstieg aus der Datei verworfen")
                implied = price / (1 - self.cfg.stop_frac) if price else 0.0
                st.entry_price = implied
                st.peak = max(st.peak, implied)
        st.stop_txid = stop.txid
        st.stop_price = price
        st.stop_units = vol
        st.units = vol
        if price and self.cfg.stop_frac < 1:
            st.peak = max(st.peak, st.entry_price, price / (1 - self.cfg.stop_frac))
        if not st.last_candle:
            parsed = parse_userref(stop.client_ref, len(self.cfg.symbols))
            if parsed:
                st.last_candle = parsed[0]

    def _drop_unverified(self, sym: str, st: SymbolState, notes: list[str]) -> None:
        """Menge aus der Datei nur behalten, wenn die Stop-Id eine Bot-Order ist."""
        from tradikg_bot.live.cutover import parse_userref

        if st.stop_txid:
            order = self.gw.query_orders([st.stop_txid]).get(st.stop_txid)
            if order is not None and parse_userref(order.client_ref, len(self.cfg.symbols)):
                if order.status.is_final:
                    notes.append(f"{sym}: Stop {st.stop_txid} ist geschlossen, Abgleich bucht ihn")
                return
        if st.units > 0:
            notes.append(f"{sym}: Menge {st.units:.8f} ohne Bot-Order – nicht gezählt")
        st.units, st.entry_price, st.peak = 0.0, 0.0, 0.0
        st.stop_txid, st.stop_price, st.stop_units = None, 0.0, 0.0

    def _guard_other_instance(self) -> bool:
        """False: Probelauf endet ohne neue Orders. Echtstart wirft ``LiveSafetyError``."""
        import time

        from tradikg_bot.live.cutover import QUIET_S, find_other_instance, write_heartbeat

        if self._alone_checked:
            if not self.dry_run:
                write_heartbeat(self.state_path.parent / "heartbeat.json")
            return True
        closed: list[Any] = []
        recent = getattr(self.gw, "recent_closed_orders", None)
        if recent is not None:
            closed = list(recent(time.time() - QUIET_S))
        reasons = find_other_instance(
            self.state_path.parent,
            [*self.gw.open_orders(), *closed],
            self.cfg.symbols,
        )
        if reasons:
            text = "Mögliche zweite Instanz: " + "; ".join(reasons)
            if self.dry_run:
                self.log(f"[ZWEITE INSTANZ] {text}. Probelauf sendet nichts.")
                return False
            raise LiveSafetyError(text + ". Nichts gesendet.")
        self._alone_checked = True
        if not self.dry_run:
            write_heartbeat(self.state_path.parent / "heartbeat.json")
        return True

    # -- ein Durchgang -------------------------------------------------------------------------
    def run_once(self) -> LiveState:
        if self.gw.has_withdrawal_permission():
            raise LiveSafetyError(
                "API-Key hat Auszahlungsrechte – bitte Key ohne Auszahlung anlegen"
            )
        if self.gw.has_margin_positions():
            raise LiveSafetyError("Margin-Positionen vorhanden – der Bot handelt nur Spot")
        self._reconcile()
        if not self._guard_other_instance():
            return self.state
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
        self._write_snapshot()
        return self.state

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
            "balances": {
                a: {"total": b.total, "held": b.held}
                for a, b in self.gw.balances().items()
                if b.total
            },
            "prices": prices,
            "symbols": {s: asdict(v) for s, v in self.state.symbols.items()},
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
        self._reconcile()
        if not self._guard_other_instance():
            return self.state
        balances = self.gw.balances()
        self._eur_used = 0.0
        today = str(pd.Timestamp.now(tz="UTC").date())
        for sym in self.cfg.symbols:
            st = self.state.symbols.setdefault(sym, SymbolState())
            self._sync(sym, st, balances)
            closes = self.closes(sym)
            above = len(closes) >= self.cfg.sma and float(closes.iloc[-1]) > float(
                closes.iloc[-self.cfg.sma :].mean()
            )
            if st.units > 0 or not above or st.blocked or self.kill.exists():
                self.log(f"[JETZT] {sym}: kein Sofortkauf (gekauft, Abwärtstrend, Sperre, Kill)")
                continue
            if st.buy_txid:
                self._cancel_and_settle(sym, st, st.buy_txid, "Sofortkauf statt Limit", buy=True)
                st.buy_txid = None
                balances = self.gw.balances()  # stornierte Limits geben das Geld wieder frei
            rules, book = self.gw.pair_rules(sym), self.gw.order_book(sym, 1)
            balances = self.gw.balances()
            eur = self._eur_for_new_buy(st, balances, rules.quote)
            price = rules.round_price_up(book.best_ask * 1.005)
            vol = rules.round_volume_down(eur / (price * (1 + KRAKEN_SPOT_TAKER)))
            if vol * price < max(rules.ordermin * price, rules.costmin):
                self.log(f"[JETZT] {sym}: {eur:.2f} € reichen nicht für die Mindestorder")
                continue
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
            self._remember_buy(st, eur, st.buy_txid)
            self._save()
        balances = self.gw.balances()
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
            # Nach dem Storno neu lesen: das frei gewordene Geld steht nicht im alten Snapshot.
            balances = self.gw.balances()
            eur = min(max(0.0, remaining), self._eur_for_new_buy(st, balances, rules.quote))
            if above and eur >= self.cfg.dust_eur and not self.kill.exists():
                price = rules.round_price_up(book.best_ask * 1.005)
                vol = rules.round_volume_down(eur / (price * (1 + KRAKEN_SPOT_TAKER)))
                txid = self._submit(
                    sym,
                    "buy_fallback",
                    day,
                    {
                        "type": "buy",
                        "ordertype": "limit",
                        "timeinforce": "IOC",
                        "price": rules.format_price(price),
                        "volume": rules.format_volume(vol),
                    },
                )
                st.buy_txid = txid
                self._remember_buy(st, eur, txid)
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

    def _position_cost(self, st: SymbolState) -> float:
        return max(0.0, st.units) * max(st.entry_price, 0.0)

    def _deployed_eur(self) -> float:
        """Eigene Scheibe, die schon in Positionen oder offenen Käufen steckt.

        ``buy_eur`` zählt nur bei einer noch offenen Order. Nach dem Fill steht derselbe
        Betrag in ``units * entry_price``; beides zusammen wäre doppelt.
        """
        total = 0.0
        for st in self.state.symbols.values():
            total += self._position_cost(st)
            if st.buy_txid:
                total += max(0.0, st.buy_eur)
        return total

    def _eur_for_new_buy(self, st: SymbolState, balances, quote: str) -> float:
        """EUR für einen neuen Kauf: min(Rest im Coin-Slot, Rest der 20-€-Scheibe, verfügbar).

        ``balances[quote].available`` ist frei (total − in Orders gebunden). ``Balance.total``
        wird nicht verwendet – ein 100-€-Konto gibt dem Bot keine größere Order.
        """
        bal = balances.get(quote) if balances else None
        available = bal.available if bal is not None else 0.0
        # Probelauf reserviert bei der Börse nichts. Echte Orders stecken schon in ``available``.
        if self.dry_run:
            available -= self._eur_used
        symbol_left = self.cfg.slot_eur - self._position_cost(st)
        if st.buy_txid:
            symbol_left -= max(0.0, st.buy_eur)
        untracked = self._eur_used
        if not self.dry_run:
            for other in self.state.symbols.values():
                if other.buy_txid:
                    untracked -= max(0.0, other.buy_eur)
            untracked = max(0.0, untracked)
        slice_left = self.cfg.own_slice_eur - self._deployed_eur() - untracked
        return max(0.0, min(symbol_left, slice_left, available))

    def _remember_buy(self, st: SymbolState, eur: float, txid: str | None) -> None:
        """Reservierung nur zählen, wenn die Order existiert oder der Probelauf sie geprüft hat."""
        if txid or self.dry_run:
            st.buy_eur = eur
            self._eur_used += eur

    def _buy(self, sym, st, day, close, book, rules, _balances) -> None:
        open_buys = [
            o for kind, o in self._tagged(sym) if kind in (KIND["buy"], KIND["buy_fallback"])
        ]
        if open_buys:
            st.buy_txid = open_buys[0].txid
            self.log(f"[ÜBERNAHME] {sym}: Kauf {open_buys[0].txid} schon offen, kein zweiter")
            return
        # Frisch lesen: der Snapshot vom Durchgangsstart kennt Orders aus diesem Umlauf nicht.
        balances = self.gw.balances()
        eur = self._eur_for_new_buy(st, balances, rules.quote)
        price = rules.round_price_down(min(close, book.best_bid))
        vol = rules.round_volume_down(eur / (price * (1 + KRAKEN_SPOT_TAKER)))
        need = max(rules.ordermin * price, rules.costmin)
        if vol * price < need:
            self.log(
                f"[SKIP] {sym}: {eur:.2f} € frei/zugeteilt reicht nicht für die "
                f"Mindestorder {need:.2f} €"
            )
            return
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
        self._remember_buy(st, eur, st.buy_txid)
        if st.buy_txid or self.dry_run:
            st.buy_day = day

    def _ensure_stop(self, sym: str, st: SymbolState) -> None:
        """Notfall-Stop bei Kraken setzen bzw. nachziehen (nur für eigene Menge)."""
        if st.units <= 0 or st.sell_txid:
            return
        stops = [
            o
            for kind, o in self._tagged(sym)
            if kind == KIND["stop"] and o.ordertype == "stop-loss"
        ]
        if len(stops) > 1:
            ids = ", ".join(o.txid for o in stops)
            raise LiveSafetyError(
                f"{sym}: {len(stops)} Bot-Stops offen ({ids}). Kein weiterer Stop."
            )
        if len(stops) == 1 and stops[0].txid != st.stop_txid:
            order = stops[0]
            st.stop_txid = order.txid
            st.stop_price = float(order.price or 0.0)
            st.stop_units = float(order.volume)
            self.log(f"[ÜBERNAHME] {sym}: bestehenden Stop {order.txid} übernommen, kein zweiter")
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
