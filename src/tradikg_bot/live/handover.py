"""Übergabe des Echtgeld-Bots vom Mac auf diesen Rechner. Legt keine Orders.

Die Mac-Zustandsdatei (``~/.tradikg/live/trend_state.json``) wird geprüft und
übernommen, oder der Zustand wird nur aus offenen Bot-Orders (``userref``)
rekonstruiert – und nur in Höhe der Budget-Scheibe. Nicht gekennzeichnete
Bestände auf dem gemeinsamen Kraken-Konto bleiben außen vor.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tradikg_bot.live.models import ExchangeOrder
from tradikg_bot.live.trend_live import (
    KIND,
    LiveSafetyError,
    LiveState,
    LiveTrendConfig,
    LiveTrendRunner,
    SymbolState,
)
from tradikg_bot.service.ops import AlreadyRunningError, InstanceLock

# Frische Bot-Orders in diesem Fenster gelten als „jemand handelt gerade“.
# Ältere Stops sind der Normalfall nach dem Stoppen des Macs und kein Beweis.
ACTIVITY_WINDOW_S = 15 * 60

EXCHANGE_HEURISTIC_LIMIT = (
    "Grenze der Börsen-Heuristik: Kraken sagt nicht, welcher API-Key eine Order "
    "gelegt hat (Mac-Key und Linux-Key sind am Orderbuch nicht zu unterscheiden). "
    "Ein stiller Bot zwischen zwei 5-Minuten-Takten ist unsichtbar. "
    f"Nur Bot-Orders mit opentm in den letzten {ACTIVITY_WINDOW_S // 60} Minuten "
    "blockieren den Start. Liegende Stops ohne frisches opentm gelten nicht als "
    "laufende Instanz. Die PID- und Sperrdatei gelten nur auf diesem Rechner, "
    "nicht für den Mac."
)

_TOP_LEVEL = {"symbols", "daily_loss", "bot_cash_eur", "drawdown"}
_SECRET_HINTS = ("api_key", "apikey", "api_secret", "secret", "password", "token", "private_key")
_NONNEG = ("units", "entry_price", "peak", "stop_price", "stop_units", "buy_eur")
_BUY_KINDS = frozenset({"buy", "buy_fallback"})
_SELL_KINDS = frozenset({"sell", "sell_fallback"})


def bot_leg(client_ref: int | None, symbols: tuple[str, ...]) -> tuple[str, str] | None:
    """``(Symbol, Art)`` aus der Bot-``userref``, sonst None.

    Gleiches Schema wie ``LiveTrendRunner._ref``: ``jjmmtt * 100 + Slot * 10 + Art``.
    """
    if client_ref is None or client_ref < 10**7:
        return None
    kind_n = client_ref % 10
    slot = (client_ref // 10) % 10
    kind = next((name for name, number in KIND.items() if number == kind_n), None)
    if kind is None or not 1 <= slot <= len(symbols):
        return None
    return symbols[slot - 1], kind


def ref_day(client_ref: int) -> str | None:
    """Handelstag aus der userref (``20jj-mm-tt``)."""
    yymmdd = client_ref // 100
    yy, month, day = yymmdd // 10000, (yymmdd // 100) % 100, yymmdd % 100
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    try:
        return dt.date(2000 + yy, month, day).isoformat()
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def local_pid_reasons(pid_path: Path) -> list[str]:
    """Laufende andere PID in ``live.pid``. Die eigene PID zählt nicht."""
    if not pid_path.exists():
        return []
    text = pid_path.read_text().strip()
    if not text:
        return []
    if not text.isdigit():
        return [f"PID-Datei {pid_path.name} enthält keine PID"]
    pid = int(text)
    if pid == os.getpid() or not _pid_alive(pid):
        return []
    return [f"PID-Datei {pid_path.name} zeigt laufenden Prozess {pid}"]


def exchange_activity_reasons(
    orders: list[ExchangeOrder],
    symbols: tuple[str, ...],
    now: dt.datetime,
    window_s: int = ACTIVITY_WINDOW_S,
) -> list[str]:
    """Frische Bot-Orders. Ohne ``opened_at`` gibt es keinen Börsen-Hinweis."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    stamp = now.timestamp()
    fresh: list[str] = []
    for order in orders:
        if order.status.is_final or bot_leg(order.client_ref, symbols) is None:
            continue
        if order.opened_at is None:
            continue
        age = stamp - order.opened_at
        if 0 <= age <= window_s:
            fresh.append(order.txid)
    if not fresh:
        return []
    return [
        "Börse: Bot-Order mit frischem opentm "
        f"({', '.join(fresh)}). Eine andere Instanz könnte gerade handeln."
    ]


def peer_reasons(
    pid_path: Path,
    orders: list[ExchangeOrder],
    symbols: tuple[str, ...],
    now: dt.datetime,
) -> list[str]:
    return local_pid_reasons(pid_path) + exchange_activity_reasons(orders, symbols, now)


def _finite_number(name: str, value: Any, *, allow_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveSafetyError(f"{name} ist keine Zahl")
    number = float(value)
    if not math.isfinite(number):
        raise LiveSafetyError(f"{name} ist nicht endlich")
    if not allow_negative and number < 0:
        raise LiveSafetyError(f"{name} darf nicht negativ sein")
    return number


def _reject_secret_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SECRET_HINTS):
                raise LiveSafetyError(
                    "Zustandsdatei enthält ein Feld, das wie ein Geheimnis aussieht"
                )
            _reject_secret_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            _reject_secret_keys(item)


def validate_state_document(raw: Any, cfg: LiveTrendConfig) -> LiveState:
    """Prüft eine kopierte Zustandsdatei. Wirft, ohne Werte aus Geheimnisfeldern zu nennen."""
    if not isinstance(raw, dict):
        raise LiveSafetyError("Zustandsdatei ist kein JSON-Objekt")
    _reject_secret_keys(raw)
    extra = set(raw) - _TOP_LEVEL
    if extra:
        raise LiveSafetyError("Zustandsdatei enthält unbekannte Felder")
    symbols = raw.get("symbols", {})
    if not isinstance(symbols, dict):
        raise LiveSafetyError("symbols fehlt oder hat den falschen Typ")
    allowed = set(SymbolState.__dataclass_fields__)
    built: dict[str, SymbolState] = {}
    for sym, row in symbols.items():
        if sym not in cfg.symbols:
            raise LiveSafetyError(f"Unbekanntes Symbol in der Zustandsdatei: {sym}")
        if not isinstance(row, dict) or set(row) - allowed:
            raise LiveSafetyError(f"{sym}: unerwartete Felder in der Zustandsdatei")
        clean: dict[str, Any] = {}
        for name, value in row.items():
            if name in _NONNEG and value is not None:
                clean[name] = _finite_number(f"{sym}.{name}", value)
            elif name == "realized_eur" and value is not None:
                clean[name] = _finite_number(f"{sym}.{name}", value, allow_negative=True)
            elif name == "blocked":
                if not isinstance(value, bool):
                    raise LiveSafetyError(f"{sym}.blocked muss wahr oder falsch sein")
                clean[name] = value
            else:
                clean[name] = value
        try:
            built[sym] = SymbolState(**clean)
        except TypeError as exc:
            raise LiveSafetyError(f"{sym}: Zustand lässt sich nicht lesen") from exc
    if raw.get("daily_loss") is not None and not isinstance(raw["daily_loss"], dict):
        raise LiveSafetyError("daily_loss ist kein Objekt")
    if raw.get("drawdown") is not None and not isinstance(raw["drawdown"], dict):
        raise LiveSafetyError("drawdown ist kein Objekt")
    state = LiveState(built, raw.get("daily_loss"))
    state.drawdown = raw.get("drawdown")
    if raw.get("bot_cash_eur") is not None:
        cash = _finite_number("bot_cash_eur", raw["bot_cash_eur"])
        if cash > cfg.budget_eur + 1e-9:
            raise LiveSafetyError("bot_cash_eur liegt über dem Budget der Scheibe")
        state.bot_cash_eur = cash
    notional = sum(st.units * st.entry_price for st in state.symbols.values() if st.units > 0)
    if notional > cfg.budget_eur + 1e-6:
        raise LiveSafetyError(
            f"Einstand {notional:.2f} € übersteigt das Budget {cfg.budget_eur:.2f} €"
        )
    return state


def validate_state_file(path: Path, cfg: LiveTrendConfig) -> LiveState:
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise LiveSafetyError("Zustandsdatei ist kein gültiges JSON") from None
    return validate_state_document(raw, cfg)


def install_state(src: Path, dest: Path, cfg: LiveTrendConfig) -> LiveState:
    """Prüft ``src`` und schreibt die kanonische Zustandsdatei nach ``dest``."""
    state = validate_state_file(src, cfg)
    state.save(dest)
    return state


def reconstruct_state(
    orders: list[ExchangeOrder],
    balances: dict[str, Any],
    cfg: LiveTrendConfig,
    base_of: Callable[[str], str],
) -> LiveState:
    """Zustand nur aus Bot-Orders. Mengen über der Scheibe werden abgelehnt.

    Der Kontostand liefert keine Positionsgröße. Er begrenzt nur: ein Stop, der
    mehr Basiswährung will, als das Konto hält, wird nicht übernommen. Münzen
    ohne Bot-``userref`` bleiben unangetastet.
    """
    grouped: dict[str, list[tuple[str, ExchangeOrder]]] = {sym: [] for sym in cfg.symbols}
    for order in orders:
        if order.status.is_final:
            continue
        leg = bot_leg(order.client_ref, cfg.symbols)
        if leg is None:
            continue
        grouped[leg[0]].append((leg[1], order))

    state = LiveState()
    for sym, legs in grouped.items():
        stops = [order for kind, order in legs if kind == "stop"]
        buys = [order for kind, order in legs if kind in _BUY_KINDS]
        sells = [order for kind, order in legs if kind in _SELL_KINDS]
        if len(stops) > 1 or len(buys) > 1 or len(sells) > 1:
            raise LiveSafetyError(
                f"{sym}: mehrere offene Bot-Orders derselben Art – keine automatische Übernahme"
            )
        st = SymbolState()
        days: list[str] = []
        if stops:
            stop = stops[0]
            if stop.price is None or stop.price <= 0 or stop.volume <= 0:
                raise LiveSafetyError(f"{sym}: Stop ohne Preis oder Menge")
            implied = stop.price / (1.0 - cfg.stop_frac)
            st.units = stop.volume
            st.entry_price = implied
            st.peak = implied
            st.stop_txid = stop.txid
            st.stop_price = float(stop.price)
            st.stop_units = stop.volume
            base = base_of(sym)
            held = balances[base].total if base in balances else 0.0
            if held + 1e-12 < st.units:
                raise LiveSafetyError(
                    f"{sym}: Bot-Stop ({st.units}) ist größer als der Bestand ({held})"
                )
        for order in (*stops, *buys, *sells):
            if order.client_ref is not None:
                day = ref_day(order.client_ref)
                if day:
                    days.append(day)
        if buys:
            buy = buys[0]
            st.buy_txid = buy.txid
            st.buy_eur = buy.volume * (buy.price or 0.0)
            st.buy_day = ref_day(buy.client_ref or 0)
        if sells:
            sell = sells[0]
            st.sell_txid = sell.txid
            st.sell_day = ref_day(sell.client_ref or 0)
        if days:
            st.last_candle = max(days)
        if st.units or st.stop_txid or st.buy_txid or st.sell_txid:
            state.symbols[sym] = st

    notional = sum(st.units * st.entry_price for st in state.symbols.values() if st.units > 0)
    if notional > cfg.budget_eur + 1e-6:
        raise LiveSafetyError(
            "Rekonstruierte Positionen übersteigen die 20-€-Scheibe "
            f"({notional:.2f} € > {cfg.budget_eur:.2f} €). "
            "Zustandsdatei vom Mac importieren – dort steht der echte Einstand. "
            "Nicht getaggte Bestände werden nicht übernommen."
        )
    return state


def reconcile(
    state: LiveState, orders: list[ExchangeOrder], cfg: LiveTrendConfig
) -> tuple[str, bool]:
    """Vergleicht Datei und offene Bot-Orders. Rückgabe: Text, ob es zusammenpasst."""
    found: list[tuple[str, str, ExchangeOrder]] = []
    for order in orders:
        if order.status.is_final:
            continue
        leg = bot_leg(order.client_ref, cfg.symbols)
        if leg is not None:
            found.append((leg[0], leg[1], order))
    lines: list[str] = []
    ok = True
    for sym in cfg.symbols:
        st = state.symbols.get(sym, SymbolState())
        stops = [order for s, kind, order in found if s == sym and kind == "stop"]
        buys = [order for s, kind, order in found if s == sym and kind in _BUY_KINDS]
        if len(stops) > 1:
            ok = False
            lines.append(f"[ABGLEICH] {sym}: mehrere Stops an der Börse | ABWEICHUNG")
            continue
        ex_stop = stops[0].txid if stops else None
        ex_vol = stops[0].volume if stops else 0.0
        stop_match = (st.stop_txid or None) == ex_stop
        vol_match = abs(st.units - ex_vol) <= 1e-8 if stops else True
        buy_ids = {order.txid for order in buys}
        state_buy = {st.buy_txid} if st.buy_txid else set()
        buy_match = buy_ids == state_buy
        row_ok = stop_match and vol_match and buy_match
        ok = ok and row_ok
        lines.append(
            f"[ABGLEICH] {sym}: Datei Menge {st.units:.8f} Stop {st.stop_txid or '–'} "
            f"Kauf {st.buy_txid or '–'} | Börse Stop {ex_stop or '–'} "
            f"Menge {ex_vol:.8f} Kauf {','.join(sorted(buy_ids)) or '–'} | "
            f"{'OK' if row_ok else 'ABWEICHUNG'}"
        )
    if not found and not any(
        st.units or st.stop_txid or st.buy_txid for st in state.symbols.values()
    ):
        lines.append(
            "[ABGLEICH] Keine Bot-Orders und keine Bot-Position. "
            "Guthaben ohne userref gehört nicht zur Scheibe. "
            "Ein späterer Live-Start kauft höchstens die 20-€-Scheibe."
        )
    lines.append(EXCHANGE_HEURISTIC_LIMIT)
    return "\n".join(lines), ok


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_dry_run_marker(
    path: Path,
    *,
    budget_eur: float,
    state_path: Path,
    daily_loss_enabled: bool,
    daily_loss_fraction: float,
    drawdown_enabled: bool = False,
    drawdown_fraction: float = 0.45,
) -> None:
    payload = {
        "ok": True,
        "budget_eur": budget_eur,
        "daily_loss_enabled": daily_loss_enabled,
        "daily_loss_fraction": daily_loss_fraction,
        "drawdown_enabled": drawdown_enabled,
        "drawdown_fraction": drawdown_fraction,
        "state_sha256": _sha256(state_path),
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def marker_block_reason(
    path: Path,
    *,
    budget_eur: float,
    state_path: Path,
    daily_loss_enabled: bool,
    daily_loss_fraction: float,
    drawdown_enabled: bool = False,
    drawdown_fraction: float = 0.45,
) -> str | None:
    """None, wenn ein passender Probelauf vorliegt."""
    if not path.exists():
        return "Kein Probelauf-Stempel. Zuerst `tradikg live dry-run` ausführen."
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return "Probelauf-Stempel ist beschädigt. Probelauf wiederholen."
    if not data.get("ok"):
        return "Probelauf war nicht in Ordnung."
    if float(data.get("budget_eur", -1)) != float(budget_eur):
        return "Probelauf hatte ein anderes Budget."
    if bool(data.get("daily_loss_enabled")) != bool(daily_loss_enabled):
        return "Probelauf hatte eine andere Tagesverlustgrenze."
    recorded = float(data.get("daily_loss_fraction", -1))
    if abs(recorded - float(daily_loss_fraction)) > 1e-12:
        return "Probelauf hatte eine andere Tagesverlust-Schwelle."
    if bool(data.get("drawdown_enabled")) != bool(drawdown_enabled):
        return "Probelauf hatte eine andere Drawdown-Sperre."
    recorded_dd = float(data.get("drawdown_fraction", -1))
    if abs(recorded_dd - float(drawdown_fraction)) > 1e-12:
        return "Probelauf hatte eine andere Drawdown-Schwelle."
    if data.get("state_sha256") != _sha256(state_path):
        return "Zustand wurde seit dem Probelauf geändert. Probelauf wiederholen."
    return None


class _ValidateOnly:
    """Lässt im Probelauf nur ``validate=true`` durch. Jede echte Order wird abgebrochen."""

    def __init__(self, gateway: Any) -> None:
        self._gw = gateway

    def __getattr__(self, name: str) -> Any:
        return getattr(self._gw, name)

    def add_order(self, params: dict[str, Any], validate: bool = False) -> str | None:
        if not validate:
            raise LiveSafetyError("Probelauf hat versucht, eine Order zu legen")
        return self._gw.add_order(params, validate=True)

    def cancel_order(self, txid: str) -> None:
        raise LiveSafetyError("Probelauf hat versucht, eine Order zu stornieren")


def _open_orders(gateway: Any) -> list[ExchangeOrder]:
    try:
        return list(gateway.open_orders())
    except LiveSafetyError:
        raise
    except Exception as exc:
        raise LiveSafetyError("Offene Orders nicht lesbar – Start verweigert") from exc


def execute_dry_run(
    *,
    gateway: Any,
    paths: dict[str, Path],
    cfg: LiveTrendConfig,
    closes: Callable,
    log: Callable[[str], None],
    now: Callable[[], dt.datetime],
    import_from: Path | None = None,
    reconstruct: bool = False,
    base_of: Callable[[str], str],
) -> str:
    """Probelauf: Zustand übernehmen, abgleichen, Orders nur validieren.

    Schreibt bei einem sauberen Abgleich den Stempel ``dry_run_ok.json``.
    """
    if import_from is not None and reconstruct:
        raise LiveSafetyError("Entweder Zustand importieren oder rekonstruieren, nicht beides")
    if gateway.has_withdrawal_permission():
        raise LiveSafetyError("API-Key hat Auszahlungsrechte – bitte Key ohne Auszahlung anlegen")
    orders = _open_orders(gateway)
    reasons = peer_reasons(paths["pid"], orders, cfg.symbols, now())
    if reasons:
        raise LiveSafetyError(
            "Start verweigert, eine andere Instanz könnte laufen:\n- "
            + "\n- ".join(reasons)
            + "\n"
            + EXCHANGE_HEURISTIC_LIMIT
        )
    try:
        lock = InstanceLock(paths["lock"]).acquire()
    except AlreadyRunningError as exc:
        raise LiveSafetyError(
            "Start verweigert, die lokale Sperrdatei ist gehalten "
            "(eine Instanz auf diesem Rechner läuft bereits).\n" + EXCHANGE_HEURISTIC_LIMIT
        ) from exc
    try:
        return _dry_run_locked(
            gateway,
            paths,
            cfg,
            closes,
            log,
            now,
            import_from=import_from,
            reconstruct=reconstruct,
            base_of=base_of,
        )
    finally:
        lock.release()


def _dry_run_locked(
    gateway: Any,
    paths: dict[str, Path],
    cfg: LiveTrendConfig,
    closes: Callable,
    log: Callable[[str], None],
    now: Callable[[], dt.datetime],
    *,
    import_from: Path | None,
    reconstruct: bool,
    base_of: Callable[[str], str],
) -> str:
    orders = _open_orders(gateway)
    if import_from is not None:
        install_state(import_from, paths["state"], cfg)
        log(f"[IMPORT] Zustand geprüft und nach {paths['state'].name} übernommen")
    elif reconstruct:
        if paths["state"].exists():
            raise LiveSafetyError(
                "Zustandsdatei existiert schon. Nicht mit --reconstruct überschreiben."
            )
        built = reconstruct_state(orders, gateway.balances(), cfg, base_of)
        built.save(paths["state"])
        log(
            "[REKONSTRUKTION] Zustand nur aus Bot-Orders geschrieben (Scheibe, keine Fremdbestände)"
        )
    elif not paths["state"].exists():
        raise LiveSafetyError(
            "Keine Zustandsdatei. Vom Mac kopieren und --import-state angeben, "
            "oder --reconstruct, wenn nur Bot-Orders an der Börse liegen."
        )
    else:
        validate_state_file(paths["state"], cfg)

    state = LiveState.load(paths["state"])
    report, ok = reconcile(state, orders, cfg)
    for line in report.splitlines():
        log(line)
    if not ok:
        raise LiveSafetyError("Abgleich weicht ab – nicht live gehen.\n" + report)
    probes: list[str] = []

    def both(msg: str) -> None:
        probes.append(msg)
        log(msg)

    runner = LiveTrendRunner(
        _ValidateOnly(gateway),
        paths["state"],
        cfg,
        closes,
        paths["kill"],
        dry_run=True,
        log=both,
        now=now,
    )
    runner.run_once()
    write_dry_run_marker(
        paths["dry_run_ok"],
        budget_eur=cfg.budget_eur,
        state_path=paths["state"],
        daily_loss_enabled=cfg.daily_loss_enabled,
        daily_loss_fraction=cfg.daily_loss_fraction,
        drawdown_enabled=cfg.drawdown_enabled,
        drawdown_fraction=cfg.drawdown_fraction,
    )
    tail = "Probelauf fertig. Es wurde keine Order gelegt."
    if not probes:
        tail += " Kein neuer Tagesentscheid (letzter Bot-Tag ist der aktuelle Kerzentag)."
    log(tail)
    return report + "\n" + tail
