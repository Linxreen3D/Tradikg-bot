"""Übergabe vom Mac-Bot an den Linux-Bot auf demselben Kraken-Konto.

Beide Bots haben eigene API-Keys. Kraken zeigt offene Orders und Trades des Kontos,
sagt aber nicht, welcher Key sie angelegt hat. Die Bot-Kennung ist die ``userref``
(Tag ``JJMMTT``, Symbol, Art). Positionen ohne diese Kennung gehören nicht zum Bot
und zählen nicht in die 20-€-Scheibe.

Eine zweite Instanz wird vor dem Echtstart abgelehnt. Die Prüfung hat Lücken;
sie stehen in ``docs/CUTOVER.md``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import socket
import time
from pathlib import Path
from typing import Any

from tradikg_bot.live.models import ExchangeOrder

# Länger als der 5-Minuten-Takt, kurz genug für eine bewusste Übergabe.
QUIET_S = 15 * 60
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TX = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def parse_userref(ref: int | None, n_symbols: int) -> tuple[str, int, int] | None:
    """``(Tag YYYY-MM-DD, Symbol-Index, Art)`` oder None, wenn es keine Bot-userref ist."""
    from tradikg_bot.live.trend_live import KIND

    if ref is None or ref < 10**7 or n_symbols < 1:
        return None
    kind = ref % 10
    slot = (ref // 10) % 10
    if kind not in KIND.values() or not 1 <= slot <= n_symbols:
        return None
    yymmdd = ref // 100
    yy, mm, dd = yymmdd // 10000, (yymmdd // 100) % 100, yymmdd % 100
    try:
        day = dt.date(2000 + yy, mm, dd).isoformat()
    except ValueError:
        return None
    return day, slot - 1, kind


def state_from_json(raw: Any):
    """Prüft die Form einer kopierten Zustandsdatei. Der Inhalt gilt damit noch nicht als wahr."""
    from tradikg_bot.live.trend_live import LiveSafetyError, LiveState, SymbolState

    if not isinstance(raw, dict) or not isinstance(raw.get("symbols", {}), dict):
        raise LiveSafetyError("Zustandsdatei muss ein JSON-Objekt mit 'symbols' sein")
    allowed = set(SymbolState.__dataclass_fields__)
    symbols: dict[str, SymbolState] = {}
    for sym, row in raw["symbols"].items():
        if not isinstance(sym, str) or not sym or not isinstance(row, dict):
            raise LiveSafetyError("Jedes Symbol in der Zustandsdatei braucht ein Objekt")
        unknown = sorted(set(row) - allowed)
        if unknown:
            raise LiveSafetyError(f"{sym}: unbekannte Felder {unknown}")
        try:
            symbols[sym] = _symbol_state(sym, row, SymbolState, LiveSafetyError)
        except LiveSafetyError:
            raise
        except (TypeError, ValueError) as exc:
            raise LiveSafetyError(f"{sym}: Zustandsfelder sind ungültig") from exc
    return LiveState(symbols)


def read_state_file(path: Path):
    """Liest eine Zustandsdatei. Der Dateiinhalt erscheint nicht in der Fehlermeldung."""
    from tradikg_bot.live.trend_live import LiveSafetyError

    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LiveSafetyError("Zustandsdatei ist kein lesbares JSON") from None
    return state_from_json(raw)


def write_heartbeat(path: Path, *, now: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "t": time.time() if now is None else now,
        "pid": os.getpid(),
        "host": socket.gethostname(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def find_other_instance(
    live_dir: Path,
    orders: list[ExchangeOrder],
    symbols: tuple[str, ...],
    *,
    now: float | None = None,
    quiet_s: float = QUIET_S,
) -> list[str]:
    """Gründe, aus denen noch eine andere Instanz laufen könnte. Leer heißt: nichts gesehen.

    Lokal: flock auf ``live.lock`` und eine lebende PID in ``live.pid``.
    Heartbeat: frische Datei von einem anderen Rechner, oder von einer lebenden anderen PID.
    Börse: Bot-Order oder Bot-Trade mit Zeitstempel innerhalb von ``quiet_s``.
    """
    now = time.time() if now is None else now
    pid = os.getpid()
    host = socket.gethostname()
    reasons: list[str] = []
    lock = _foreign_lock(live_dir / "live.lock", pid)
    if lock:
        reasons.append(lock)
    foreign_pid = _foreign_pid(live_dir / "live.pid", pid)
    if foreign_pid:
        reasons.append(foreign_pid)
    beat = _foreign_heartbeat(live_dir / "heartbeat.json", now, quiet_s, host, pid)
    if beat:
        reasons.append(beat)
    for order in orders:
        if parse_userref(order.client_ref, len(symbols)) is None:
            continue
        stamp = order.closed_at if order.closed_at is not None else order.opened_at
        if stamp is None or now - stamp > quiet_s:
            continue
        age = max(0, int(now - stamp))
        reasons.append(f"Bot-Order {order.txid} (userref {order.client_ref}) ist {age} s alt")
    return reasons


def _foreign_lock(path: Path, pid: int) -> str | None:
    if not path.exists():
        return None
    owner = path.read_text().strip()
    if owner == str(pid):
        return None
    import fcntl

    fh = open(path, "a+")  # noqa: SIM115 - nur für die Probe offen
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return f"Lokale Sperre {path.name} gehört zu PID {owner or 'unbekannt'}"
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return None
    finally:
        fh.close()


def _foreign_pid(path: Path, pid: int) -> str | None:
    if not path.exists():
        return None
    text = path.read_text().strip()
    if not text.isdigit():
        return None
    other = int(text)
    if other == pid or not _alive(other):
        return None
    return f"PID-Datei {path.name} gehört zu laufendem Prozess {other}"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _foreign_heartbeat(path: Path, now: float, quiet_s: float, host: str, pid: int) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        ts = float(data["t"])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return "Heartbeat-Datei ist nicht lesbar"
    if now - ts > quiet_s:
        return None
    other_host = str(data.get("host", ""))
    other_pid = data.get("pid", -1)
    try:
        other_pid = int(other_pid)
    except (TypeError, ValueError):
        other_pid = -1
    if other_host == host and other_pid == pid:
        return None
    if other_host == host and not _alive(other_pid):
        return None
    if other_host == host:
        return f"Heartbeat einer anderen lokalen Instanz (PID {other_pid})"
    return f"Frischer Heartbeat von {other_host or 'unbekanntem Rechner'}"


def _symbol_state(sym: str, row: dict[str, Any], symbol_state, error):
    numbers = (
        "units",
        "entry_price",
        "peak",
        "stop_price",
        "stop_units",
        "buy_eur",
        "realized_eur",
    )
    clean: dict[str, Any] = {}
    for name in numbers:
        if name not in row:
            continue
        clean[name] = _number(sym, name, row[name], error, allow_negative=name == "realized_eur")
    if "blocked" in row:
        if not isinstance(row["blocked"], bool):
            raise error(f"{sym}: blocked muss true oder false sein")
        clean["blocked"] = row["blocked"]
    for name in ("stop_txid", "buy_txid", "sell_txid"):
        if name in row and row[name] is not None:
            if not isinstance(row[name], str) or not _TX.match(row[name]):
                raise error(f"{sym}: {name} ist keine Order-Id")
            clean[name] = row[name]
    for name in ("buy_day", "sell_day", "last_candle"):
        if name in row and row[name] is not None:
            if not isinstance(row[name], str) or not _DAY.match(row[name]):
                raise error(f"{sym}: {name} ist kein Tagesdatum")
            clean[name] = row[name]
    state = symbol_state(**clean)
    if state.units > 0 and state.entry_price <= 0:
        raise error(f"{sym}: Menge ohne Einstiegspreis")
    return state


def _number(sym: str, name: str, value: Any, error, *, allow_negative: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error(f"{sym}: {name} ist keine Zahl")
    number = float(value)
    if not math.isfinite(number):
        raise error(f"{sym}: {name} ist keine endliche Zahl")
    if number < 0 and not allow_negative:
        raise error(f"{sym}: {name} darf nicht negativ sein")
    return number
