"""Persistentes Ausführungsjournal (SQLite, Write-Ahead).

Jede Absicht wird **vor** dem Senden an die Börse gespeichert. Nach einem
Absturz oder Neustart kann so jede gesendete oder vielleicht gesendete Order über
ihre ``client_ref`` wiedergefunden werden. Außerdem verwaltet das Journal:

* eine streng monotone Nonce (auch über Neustarts hinweg),
* den Sperrzustand (``SAFE_HALT``), der nur manuell aufgehoben werden kann,
* ein Audit-Log aller Entscheidungen.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_ref        INTEGER NOT NULL UNIQUE,
    venue             TEXT NOT NULL,
    pair              TEXT NOT NULL,
    state             TEXT NOT NULL,
    requested_volume  REAL NOT NULL,
    entry_limit_price REAL NOT NULL,
    stop_loss_price   REAL NOT NULL,
    take_profit_price REAL NOT NULL,
    entry_txid        TEXT,
    filled_volume     REAL NOT NULL DEFAULT 0,
    entry_avg_price   REAL NOT NULL DEFAULT 0,
    entry_fee         REAL NOT NULL DEFAULT 0,
    stop_loss_txid    TEXT,
    take_profit_txid  TEXT,
    exit_reason       TEXT,
    exit_volume       REAL NOT NULL DEFAULT 0,
    exit_avg_price    REAL NOT NULL DEFAULT 0,
    exit_fee          REAL NOT NULL DEFAULT 0,
    realized_pnl      REAL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    closed_at         TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL,
    data    TEXT
);
"""


class TradeState(StrEnum):
    SUBMITTING = "submitting"  # in Journal geschrieben, Senden läuft / Ergebnis unbekannt
    ENTRY_ACCEPTED = "entry_accepted"  # Börse hat Einstieg bestätigt, Schutz noch unbestätigt
    PROTECTED = "protected"  # SL und TP börsenseitig bestätigt
    CLOSED = "closed"  # Position vollständig geschlossen
    NO_FILL = "no_fill"  # Einstieg ohne Ausführung beendet
    REJECTED = "rejected"  # Börse hat eindeutig abgelehnt
    UNRESOLVED = "unresolved"  # Zustand unklar -> SAFE_HALT, manuelle Prüfung

    @property
    def is_active(self) -> bool:
        return self in {
            TradeState.SUBMITTING,
            TradeState.ENTRY_ACCEPTED,
            TradeState.PROTECTED,
            TradeState.UNRESOLVED,
        }


@dataclass
class TradeRecord:
    id: int
    client_ref: int
    venue: str
    pair: str
    state: TradeState
    requested_volume: float
    entry_limit_price: float
    stop_loss_price: float
    take_profit_price: float
    entry_txid: str | None
    filled_volume: float
    entry_avg_price: float
    entry_fee: float
    stop_loss_txid: str | None
    take_profit_txid: str | None
    exit_reason: str | None
    exit_volume: float
    exit_avg_price: float
    exit_fee: float
    realized_pnl: float | None
    created_at: str
    updated_at: str
    closed_at: str | None


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class ExecutionJournal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- Schlüssel/Wert ----------------------------------------------------------
    def _get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def _set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- Nonce ----------------------------------------------------------------------
    def next_nonce(self) -> int:
        """Streng monoton steigende Nonce (Mikrosekunden), persistent über Neustarts."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            last = int(self._get("nonce") or 0)
            nonce = max(last + 1, time.time_ns() // 1000)
            self._set("nonce", str(nonce))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return nonce

    # -- Sperrzustand ---------------------------------------------------------------
    @property
    def halt_reason(self) -> str | None:
        return self._get("halt_reason") or None

    def set_halt(self, reason: str) -> None:
        if not self.halt_reason:
            self._set("halt_reason", reason)
            self._set("halt_since", _now())
        self.log("CRITICAL", f"SAFE_HALT: {reason}")

    def clear_halt(self, operator_note: str) -> None:
        if not operator_note.strip():
            raise ValueError("Für das Aufheben der Sperre ist eine Begründung erforderlich")
        previous = self.halt_reason
        self._set("halt_reason", "")
        self.log(
            "WARNING", "SAFE_HALT manuell aufgehoben", {"previous": previous, "note": operator_note}
        )

    # -- Trades ---------------------------------------------------------------------
    def new_client_ref(self) -> int:
        """Eindeutige positive 31-Bit-Referenz (Kraken ``userref`` ist int32)."""
        while True:
            ref = secrets.randbelow(2**31 - 1) + 1
            exists = self._conn.execute(
                "SELECT 1 FROM trades WHERE client_ref = ?", (ref,)
            ).fetchone()
            if not exists:
                return ref

    def create_trade(
        self,
        *,
        client_ref: int,
        venue: str,
        pair: str,
        requested_volume: float,
        entry_limit_price: float,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> TradeRecord:
        now = _now()
        cur = self._conn.execute(
            """INSERT INTO trades(client_ref, venue, pair, state, requested_volume,
                 entry_limit_price, stop_loss_price, take_profit_price, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                client_ref,
                venue,
                pair,
                TradeState.SUBMITTING.value,
                requested_volume,
                entry_limit_price,
                stop_loss_price,
                take_profit_price,
                now,
                now,
            ),
        )
        return self.get_trade(int(cur.lastrowid))

    def update_trade(self, trade_id: int, **fields: Any) -> TradeRecord:
        if not fields:
            return self.get_trade(trade_id)
        fields["updated_at"] = _now()
        if isinstance(fields.get("state"), TradeState):
            fields["state"] = fields["state"].value
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE trades SET {cols} WHERE id = ?", (*fields.values(), trade_id))
        return self.get_trade(trade_id)

    def get_trade(self, trade_id: int) -> TradeRecord:
        cur = self._conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(trade_id)
        return self._row(cur, row)

    def trades(self, active_only: bool = False) -> list[TradeRecord]:
        cur = self._conn.execute("SELECT * FROM trades ORDER BY id")
        out = [self._row(cur, r) for r in cur.fetchall()]
        return [t for t in out if t.state.is_active] if active_only else out

    @staticmethod
    def _row(cur: sqlite3.Cursor, row: tuple) -> TradeRecord:
        data = {d[0]: v for d, v in zip(cur.description, row, strict=True)}
        data["state"] = TradeState(data["state"])
        return TradeRecord(**data)

    # -- PnL ------------------------------------------------------------------------
    def realized_pnl_total(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM trades WHERE realized_pnl IS NOT NULL"
        ).fetchone()
        return float(row[0])

    def realized_pnl_on(self, day: dt.date) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM trades "
            "WHERE realized_pnl IS NOT NULL AND substr(closed_at, 1, 10) = ?",
            (day.isoformat(),),
        ).fetchone()
        return float(row[0])

    # -- Audit ----------------------------------------------------------------------
    def log(self, level: str, message: str, data: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            "INSERT INTO events(ts, level, message, data) VALUES (?, ?, ?, ?)",
            (_now(), level, message, json.dumps(data, default=str) if data else None),
        )

    def events(self, limit: int = 50) -> list[tuple[str, str, str, str | None]]:
        return self._conn.execute(
            "SELECT ts, level, message, data FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
