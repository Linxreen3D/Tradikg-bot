"""Persistenz der Replay-Läufe (SQLite) und Zähler für Holdout-Auswertungen.

Ein Holdout ist nur so lange unangetastet, wie niemand seine Ergebnisse gesehen und
daraufhin etwas geändert hat. Der Store zählt deshalb je Daten-Fingerabdruck
(Symbole, Zeitraster, Holdout-Kerzen), wie oft ein Holdout ausgewertet wurde.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tradikg_bot.replay.walkforward import ReplayPlan, ReplayResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    data_source TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    timeframe TEXT NOT NULL,
    symbols TEXT NOT NULL,
    config_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    research_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    holdout_evaluated INTEGER NOT NULL,
    holdout_count INTEGER NOT NULL,
    report_md TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replay_scenarios (
    run_id INTEGER NOT NULL REFERENCES replay_runs(id),
    scenario_id TEXT NOT NULL,
    period TEXT NOT NULL, variant TEXT NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL,
    profile TEXT NOT NULL, profile_label TEXT NOT NULL, capital REAL NOT NULL,
    start TEXT NOT NULL, end TEXT NOT NULL,
    final_equity REAL NOT NULL, net_profit REAL NOT NULL, return_pct REAL NOT NULL,
    max_drawdown REAL NOT NULL, trades INTEGER NOT NULL, win_rate REAL, fees_eur REAL NOT NULL,
    ambiguous_exits INTEGER NOT NULL, halted_days INTEGER NOT NULL,
    open_positions_end INTEGER NOT NULL, upper_bound INTEGER NOT NULL, note TEXT,
    ledger_json TEXT, rejections_json TEXT,
    PRIMARY KEY (run_id, scenario_id)
);
CREATE TABLE IF NOT EXISTS replay_equity (
    run_id INTEGER NOT NULL, scenario_id TEXT NOT NULL, ts TEXT NOT NULL,
    equity REAL, trading_balance REAL, payout_reserve REAL, longterm_portfolio REAL,
    unrecovered REAL
);
CREATE INDEX IF NOT EXISTS replay_equity_idx ON replay_equity (run_id, scenario_id);
CREATE TABLE IF NOT EXISTS replay_trades (
    run_id INTEGER NOT NULL, scenario_id TEXT NOT NULL, trade_id INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdout_usage (
    fingerprint TEXT PRIMARY KEY, count INTEGER NOT NULL, first_at TEXT NOT NULL,
    last_at TEXT NOT NULL
);
"""


def holdout_fingerprint(data: dict[str, pd.DataFrame], plan: ReplayPlan, timeframe: str) -> str:
    h = hashlib.sha256(timeframe.encode())
    start = plan.index[plan.holdout_start]
    for sym in sorted(data):
        part = data[sym][data[sym].index >= start]
        h.update(sym.encode())
        h.update(np.round(part["close"].to_numpy(dtype=float), 6).tobytes())
        h.update(str(part.index.asi8.tolist()[:1] + part.index.asi8.tolist()[-1:]).encode())
    return h.hexdigest()[:16]


def _num(x: Any) -> Any:
    if isinstance(x, (np.floating, np.integer)):
        return x.item()
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


class ReplayStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- Holdout-Zähler ----------------------------------------------------------------------
    def holdout_count(self, fingerprint: str) -> int:
        row = self.conn.execute(
            "SELECT count FROM holdout_usage WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return int(row[0]) if row else 0

    def register_holdout(self, fingerprint: str) -> int:
        now = pd.Timestamp.now(tz="UTC").isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO holdout_usage VALUES (?, 1, ?, ?) ON CONFLICT(fingerprint) "
                "DO UPDATE SET count = count + 1, last_at = excluded.last_at",
                (fingerprint, now, now),
            )
        return self.holdout_count(fingerprint)

    # -- Speichern ---------------------------------------------------------------------------
    def save(
        self, result: ReplayResult, report_md: str, fingerprint: str, holdout_count: int
    ) -> int:
        cfg = dataclasses.asdict(result.config)
        plan = {
            "start": result.plan.index[0].isoformat(),
            "end": result.plan.index[-1].isoformat(),
            "cutoffs": [result.plan.index[c].isoformat() for c in result.plan.cutoffs],
            "holdout_start": result.plan.index[result.plan.holdout_start].isoformat(),
            "windows": [
                [result.plan.index[s].isoformat(), result.plan.index[e - 1].isoformat()]
                for s, e in result.plan.windows
            ],
            "segments": result.plan.describe(),
        }
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO replay_runs (created_at, data_source, synthetic, timeframe, symbols, "
                "config_json, plan_json, research_json, fingerprint, holdout_evaluated, "
                "holdout_count, report_md) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pd.Timestamp.now(tz="UTC").isoformat(),
                    result.data_source,
                    int(result.synthetic),
                    result.config.timeframe,
                    json.dumps(result.symbols),
                    json.dumps(cfg, default=str),
                    json.dumps(plan, default=str),
                    json.dumps(result.research, default=str),
                    fingerprint,
                    int(result.config.evaluate_holdout),
                    holdout_count,
                    report_md,
                ),
            )
            run_id = int(cur.lastrowid)
            for s in result.scenarios:
                self.conn.execute(
                    "INSERT INTO replay_scenarios VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        s.scenario_id,
                        s.period,
                        s.variant,
                        s.label,
                        s.kind,
                        s.profile,
                        s.profile_label,
                        s.capital,
                        s.start.isoformat(),
                        s.end.isoformat(),
                        s.final_equity,
                        s.net_profit,
                        s.return_pct,
                        s.max_drawdown,
                        s.n_trades,
                        s.win_rate,
                        s.fees_eur,
                        s.ambiguous_exits,
                        s.halted_days,
                        s.open_positions_end,
                        int(s.upper_bound),
                        s.note,
                        json.dumps(s.ledger),
                        json.dumps(s.rejections),
                    ),
                )
                eq = s.equity
                self.conn.executemany(
                    "INSERT INTO replay_equity VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            run_id,
                            s.scenario_id,
                            ts.isoformat(),
                            *(
                                _num(row[c])
                                for c in (
                                    "equity",
                                    "trading_balance",
                                    "payout_reserve",
                                    "longterm_portfolio",
                                    "unrecovered",
                                )
                            ),
                        )
                        for ts, row in eq.iterrows()
                    ],
                )
                self.conn.executemany(
                    "INSERT INTO replay_trades VALUES (?, ?, ?, ?)",
                    [
                        (
                            run_id,
                            s.scenario_id,
                            int(t.get("trade_id", i)),
                            json.dumps(t, default=str),
                        )
                        for i, t in enumerate(s.trades)
                    ],
                )
        return run_id

    # -- Lesen -------------------------------------------------------------------------------
    def runs(self) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT id, created_at, data_source, synthetic, timeframe, symbols, "
            "holdout_evaluated, holdout_count FROM replay_runs ORDER BY id DESC",
            self.conn,
        )

    def latest_run_id(self) -> int | None:
        row = self.conn.execute("SELECT MAX(id) FROM replay_runs").fetchone()
        return row[0] if row and row[0] is not None else None

    def run(self, run_id: int | None = None) -> dict[str, Any] | None:
        run_id = run_id or self.latest_run_id()
        if run_id is None:
            return None
        row = self.conn.execute(
            "SELECT id, created_at, data_source, synthetic, timeframe, symbols, config_json, "
            "plan_json, research_json, fingerprint, holdout_evaluated, holdout_count, report_md "
            "FROM replay_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        keys = [
            "id",
            "created_at",
            "data_source",
            "synthetic",
            "timeframe",
            "symbols",
            "config",
            "plan",
            "research",
            "fingerprint",
            "holdout_evaluated",
            "holdout_count",
            "report_md",
        ]
        out = dict(zip(keys, row, strict=True))
        for k in ("symbols", "config", "plan", "research"):
            out[k] = json.loads(out[k])
        out["synthetic"] = bool(out["synthetic"])
        return out

    def report(self, run_id: int | None = None) -> str | None:
        run = self.run(run_id)
        return run["report_md"] if run else None

    def scenarios(self, run_id: int) -> pd.DataFrame:
        df = pd.read_sql_query(
            "SELECT * FROM replay_scenarios WHERE run_id = ? ORDER BY scenario_id",
            self.conn,
            params=(run_id,),
        )
        return df

    def equity(self, run_id: int, scenario_ids: list[str]) -> pd.DataFrame:
        if not scenario_ids:
            return pd.DataFrame()
        marks = ", ".join("?" * len(scenario_ids))
        df = pd.read_sql_query(
            f"SELECT * FROM replay_equity WHERE run_id = ? AND scenario_id IN ({marks}) "
            "ORDER BY ts",
            self.conn,
            params=(run_id, *scenario_ids),
        )
        df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
        return df

    def trades(self, run_id: int, scenario_id: str) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT payload FROM replay_trades WHERE run_id = ? AND scenario_id = ? "
            "ORDER BY trade_id",
            (run_id, scenario_id),
        ).fetchall()
        return pd.DataFrame([json.loads(r[0]) for r in rows])
