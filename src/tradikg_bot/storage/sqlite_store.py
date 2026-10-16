"""Lokale Datenhaltung in SQLite (keine externe Datenbank nötig).

Tabellen:
* ``instruments``   – Stammdaten & Handelsrestriktionen
* ``bars``          – OHLCV-Kerzen (Zeitstempel als UTC-Millisekunden, Öffnungszeit)
* ``backtest_runs`` – Parameter, Kostenmodell und Kennzahlen jedes Backtests
* ``trades``        – jeder abgeschlossene (simulierte) Trade inkl. Kosten und Exit-Grund
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from tradikg_bot.data.validation import OHLCV_COLUMNS, canonical_index
from tradikg_bot.domain import AssetClass, Instrument

if TYPE_CHECKING:
    from tradikg_bot.backtest.engine import BacktestResult

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    symbol       TEXT PRIMARY KEY,
    asset_class  TEXT NOT NULL CHECK (asset_class IN ('crypto', 'stock')),
    qty_step     REAL NOT NULL CHECK (qty_step > 0),
    min_notional REAL NOT NULL DEFAULT 0,
    currency     TEXT NOT NULL DEFAULT 'EUR'
);
CREATE TABLE IF NOT EXISTS bars (
    symbol    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL    NOT NULL,
    source    TEXT    NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (symbol, timeframe, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS backtest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT,
    strategy        TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    fee_model_json  TEXT NOT NULL,
    risk_json       TEXT NOT NULL,
    metrics_json    TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    final_equity    REAL NOT NULL,
    data_start      TEXT,
    data_end        TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    symbol        TEXT NOT NULL,
    entry_time    TEXT NOT NULL,
    exit_time     TEXT NOT NULL,
    qty           REAL NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL NOT NULL,
    stop_loss     REAL NOT NULL,
    take_profit   REAL NOT NULL,
    gross_pnl     REAL NOT NULL,
    commission    REAL NOT NULL,
    slippage_cost REAL NOT NULL,
    net_pnl       REAL NOT NULL,
    planned_risk  REAL NOT NULL,
    r_multiple    REAL NOT NULL,
    exit_reason   TEXT NOT NULL,
    bars_held     INTEGER NOT NULL,
    mae           REAL NOT NULL,
    mfe           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_run ON trades(run_id);
"""


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if math.isnan(obj) else float(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    return str(obj)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, sort_keys=True)


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- Instrumente ---------------------------------------------------------
    def upsert_instrument(self, instrument: Instrument) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO instruments(symbol, asset_class, qty_step, min_notional, currency)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET asset_class=excluded.asset_class,
                     qty_step=excluded.qty_step, min_notional=excluded.min_notional,
                     currency=excluded.currency""",
                (
                    instrument.symbol,
                    str(instrument.asset_class),
                    instrument.qty_step,
                    instrument.min_notional,
                    instrument.currency,
                ),
            )

    def get_instrument(self, symbol: str) -> Instrument | None:
        row = self._conn.execute(
            "SELECT symbol, asset_class, qty_step, min_notional, currency "
            "FROM instruments WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        return Instrument(row[0], AssetClass(row[1]), row[2], row[3], row[4])

    # -- Kerzen ----------------------------------------------------------------
    def save_bars(
        self, symbol: str, timeframe: str, bars: pd.DataFrame, source: str = "unknown"
    ) -> int:
        """Speichert Kerzen idempotent (vorhandene Zeitstempel werden überschrieben)."""
        if bars.empty:
            return 0
        if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
            raise ValueError("Kerzen benötigen einen tz-aware DatetimeIndex (validate_ohlcv)")
        ts_ms = bars.index.tz_convert("UTC").as_unit("ms").asi8
        values = bars[OHLCV_COLUMNS].to_numpy(dtype=float)
        rows = [
            (symbol, timeframe, int(t), *map(float, v), source)
            for t, v in zip(ts_ms, values, strict=True)
        ]
        with self._tx() as conn:
            conn.executemany(
                """INSERT INTO bars(symbol, timeframe, ts, open, high, low, close, volume, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET open=excluded.open,
                     high=excluded.high, low=excluded.low, close=excluded.close,
                     volume=excluded.volume, source=excluded.source""",
                rows,
            )
        return len(rows)

    def load_bars(
        self,
        symbol: str,
        timeframe: str,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        query = (
            "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = ? AND timeframe = ?"
        )
        params: list[Any] = [symbol, timeframe]
        if start is not None:
            query += " AND ts >= ?"
            params.append(int(pd.Timestamp(start, tz="UTC").timestamp() * 1000))
        if end is not None:
            query += " AND ts < ?"
            params.append(int(pd.Timestamp(end, tz="UTC").timestamp() * 1000))
        query += " ORDER BY ts"
        rows = self._conn.execute(query, params).fetchall()
        frame = pd.DataFrame(rows, columns=["ts", *OHLCV_COLUMNS])
        frame.index = canonical_index(pd.Index(frame.pop("ts").astype("int64")))
        return frame.astype("float64")

    def list_series(self) -> pd.DataFrame:
        rows = self._conn.execute(
            """SELECT symbol, timeframe, COUNT(*), MIN(ts), MAX(ts)
               FROM bars GROUP BY symbol, timeframe ORDER BY symbol, timeframe"""
        ).fetchall()
        frame = pd.DataFrame(rows, columns=["symbol", "timeframe", "bars", "first", "last"])
        for col in ("first", "last"):
            frame[col] = pd.to_datetime(frame[col], unit="ms", utc=True)
        return frame

    # -- Backtest-Ergebnisse -------------------------------------------------------
    def save_backtest(self, result: BacktestResult, timeframe: str | None = None) -> int:
        eq_index = result.equity.index
        with self._tx() as conn:
            cur = conn.execute(
                """INSERT INTO backtest_runs(created_at, symbol, timeframe, strategy, params_json,
                     fee_model_json, risk_json, metrics_json, initial_capital, final_equity,
                     data_start, data_end)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pd.Timestamp.now(tz="UTC").isoformat(),
                    result.symbol,
                    timeframe,
                    result.strategy_name,
                    _dumps(result.strategy_params),
                    _dumps(asdict(result.fee_model)),
                    _dumps(asdict(result.risk)),
                    _dumps(result.metrics),
                    result.initial_capital,
                    result.final_equity,
                    eq_index[0].isoformat(),
                    eq_index[-1].isoformat(),
                ),
            )
            run_id = int(cur.lastrowid)
            conn.executemany(
                """INSERT INTO trades(run_id, symbol, entry_time, exit_time, qty, entry_price,
                     exit_price, stop_loss, take_profit, gross_pnl, commission, slippage_cost,
                     net_pnl, planned_risk, r_multiple, exit_reason, bars_held, mae, mfe)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        t.symbol,
                        t.entry_time.isoformat(),
                        t.exit_time.isoformat(),
                        t.qty,
                        t.entry_price,
                        t.exit_price,
                        t.stop_loss,
                        t.take_profit,
                        t.gross_pnl,
                        t.commission,
                        t.slippage_cost,
                        t.net_pnl,
                        t.planned_risk,
                        t.r_multiple,
                        str(t.exit_reason),
                        t.bars_held,
                        t.max_adverse_excursion,
                        t.max_favorable_excursion,
                    )
                    for t in result.trades
                ],
            )
        return run_id

    def load_runs(self) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT id, created_at, symbol, timeframe, strategy, initial_capital, final_equity "
            "FROM backtest_runs ORDER BY id",
            self._conn,
        )

    def load_run_metrics(self, run_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT metrics_json FROM backtest_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Backtest-Lauf {run_id} existiert nicht")
        return json.loads(row[0])

    def load_trades(self, run_id: int) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT * FROM trades WHERE run_id = ? ORDER BY id", self._conn, params=(run_id,)
        )
