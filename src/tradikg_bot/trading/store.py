"""Persistenter Zustand des autonomen Paper-Kreislaufs (eigene SQLite-Datei).

Getrennt vom Live-Journal: Paper und Live teilen Code, aber nie Zustände.
Alles, was für einen Neustart nötig ist, liegt hier: verarbeitete Kerzen
(Idempotenz), Kursverlauf, ausstehende Absichten, Trade-Metadaten, Cooldowns,
Equity-Verlauf, Nachanalysen, Tagesberichte und der Zustand der Paper-Börse.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed_steps (ts TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL, ts TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
    volume REAL, PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS intents (
    key TEXT PRIMARY KEY, symbol TEXT NOT NULL, strategy TEXT NOT NULL, signal_ts TEXT NOT NULL,
    status TEXT NOT NULL, payload TEXT NOT NULL, result TEXT, updated_ts TEXT
);
CREATE TABLE IF NOT EXISTS signals_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL,
    strategy TEXT NOT NULL, regime TEXT, p_win REAL, p_source TEXT, expected_r REAL,
    score REAL, status TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS trade_meta (
    trade_id INTEGER PRIMARY KEY, intent_key TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cooldowns (
    symbol TEXT PRIMARY KEY, until_ts TEXT, consecutive_losses INTEGER NOT NULL DEFAULT 0,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY, equity REAL, cash REAL, positions_value REAL, trading_balance REAL,
    payout_reserve REAL, longterm_portfolio REAL, tax_reserve REAL, unrecovered REAL,
    realized_today REAL, unrealized REAL, open_positions INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS trade_analysis (
    trade_id INTEGER PRIMARY KEY, closed_ts TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_reports (day TEXT PRIMARY KEY, markdown TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS health (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""


class PaperStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.executescript(_SCHEMA)
        if self.get("schema_version") is None:
            self.set("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.conn.close()

    # -- kv ---------------------------------------------------------------------------------
    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM engine_kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO engine_kv(key, value) VALUES (?, ?)", (key, value)
        )

    def get_json(self, key: str) -> Any:
        raw = self.get(key)
        return None if raw is None else json.loads(raw)

    def set_json(self, key: str, value: Any) -> None:
        self.set(key, json.dumps(value, default=str))

    # -- Schritte & Kerzen -------------------------------------------------------------------
    def step_done(self, ts: pd.Timestamp) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM processed_steps WHERE ts = ?", (ts.isoformat(),)
            ).fetchone()
            is not None
        )

    def mark_step(self, ts: pd.Timestamp, now: pd.Timestamp) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_steps VALUES (?, ?)", (ts.isoformat(), now.isoformat())
        )

    def last_step(self) -> pd.Timestamp | None:
        row = self.conn.execute("SELECT MAX(ts) FROM processed_steps").fetchone()
        return pd.Timestamp(row[0]) if row and row[0] else None

    def save_bar(
        self, symbol: str, ts: pd.Timestamp, o: float, h: float, lo: float, c: float, v: float
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO bars VALUES (?,?,?,?,?,?,?)",
            (symbol, ts.isoformat(), o, h, lo, c, v),
        )

    def bars(self, symbol: str, limit: int | None = None) -> pd.DataFrame:
        q = "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = ? ORDER BY ts"
        frame = pd.read_sql_query(q, self.conn, params=(symbol,))
        frame.index = pd.DatetimeIndex(
            pd.to_datetime(frame.pop("ts"), utc=True), name="timestamp"
        ).as_unit("ns")
        return frame.iloc[-limit:] if limit else frame

    # -- Absichten, Signale, Metadaten --------------------------------------------------------
    def add_intent(
        self, key: str, symbol: str, strategy: str, signal_ts: pd.Timestamp, payload: dict[str, Any]
    ) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO intents(key, symbol, strategy, signal_ts, status, payload) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (key, symbol, strategy, signal_ts.isoformat(), json.dumps(payload, default=str)),
        )
        return cur.rowcount == 1

    def pending_intents(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT key, payload FROM intents WHERE status = 'pending' ORDER BY rowid"
        ).fetchall()
        return [(k, json.loads(p)) for k, p in rows]

    def resolve_intent(self, key: str, status: str, result: str, ts: pd.Timestamp) -> None:
        self.conn.execute(
            "UPDATE intents SET status = ?, result = ?, updated_ts = ? WHERE key = ?",
            (status, result, ts.isoformat(), key),
        )

    def log_signal(
        self,
        ts: pd.Timestamp,
        symbol: str,
        strategy: str,
        regime: str,
        p_win: float,
        p_source: str,
        expected_r: float,
        score: float,
        status: str,
        reason: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO signals_log(ts, symbol, strategy, regime, p_win, p_source, expected_r, "
            "score, status, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ts.isoformat(),
                symbol,
                strategy,
                regime,
                p_win,
                p_source,
                expected_r,
                score,
                status,
                reason,
            ),
        )

    def save_trade_meta(self, trade_id: int, intent_key: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO trade_meta VALUES (?, ?, ?)",
            (trade_id, intent_key, json.dumps(payload, default=str)),
        )

    def trade_meta(self, trade_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload FROM trade_meta WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    # -- Cooldowns ------------------------------------------------------------------------------
    def cooldown(self, symbol: str) -> tuple[pd.Timestamp | None, int, str | None]:
        row = self.conn.execute(
            "SELECT until_ts, consecutive_losses, reason FROM cooldowns WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            return None, 0, None
        return (pd.Timestamp(row[0]) if row[0] else None), int(row[1]), row[2]

    def set_cooldown(
        self, symbol: str, until: pd.Timestamp | None, losses: int, reason: str | None
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cooldowns VALUES (?, ?, ?, ?)",
            (symbol, until.isoformat() if until is not None else None, losses, reason),
        )

    # -- Equity, Analysen, Berichte, Health --------------------------------------------------------
    def save_equity(self, ts: pd.Timestamp, row: dict[str, Any]) -> None:
        cols = [
            "equity",
            "cash",
            "positions_value",
            "trading_balance",
            "payout_reserve",
            "longterm_portfolio",
            "tax_reserve",
            "unrecovered",
            "realized_today",
            "unrealized",
            "open_positions",
            "status",
        ]
        self.conn.execute(
            f"INSERT OR REPLACE INTO equity VALUES (?, {', '.join('?' * len(cols))})",
            (ts.isoformat(), *[row[c] for c in cols]),
        )

    def save_analysis(self, trade_id: int, closed_ts: str, payload: dict[str, Any]) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO trade_analysis VALUES (?, ?, ?)",
            (trade_id, closed_ts, json.dumps(payload, default=str)),
        )
        return cur.rowcount == 1

    def analyses(self, day: str | None = None) -> list[dict[str, Any]]:
        if day:
            rows = self.conn.execute(
                "SELECT payload FROM trade_analysis WHERE substr(closed_ts, 1, 10) = ? "
                "ORDER BY trade_id",
                (day,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT payload FROM trade_analysis ORDER BY trade_id"
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_daily_report(self, day: str, markdown: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_reports VALUES (?, ?, ?)",
            (day, markdown, json.dumps(payload, default=str)),
        )

    def set_health(self, key: str, value: str, now: pd.Timestamp) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO health VALUES (?, ?, ?)", (key, value, now.isoformat())
        )

    def health(self) -> dict[str, tuple[str, str]]:
        return {k: (v, t) for k, v, t in self.conn.execute("SELECT * FROM health").fetchall()}
