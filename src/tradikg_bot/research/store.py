"""Persistenz der Research-Ergebnisse und Out-of-Sample-Statistiken (SQLite).

``strategy_stats`` enthält ausschließlich Trades aus Test- und Walk-Forward-OOS-
Bereichen, nie aus dem Training. Daraus schätzt die Signalbewertung später
Trefferwahrscheinlichkeiten (mit Schrumpfung auf eine konservative Vorannahme).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tradikg_bot.research.engine import StrategyResearch, TradeStats

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    data_source TEXT NOT NULL,
    timeframe TEXT,
    symbols TEXT NOT NULL,
    config_json TEXT NOT NULL,
    report_md TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_results (
    run_id INTEGER NOT NULL REFERENCES research_runs(id),
    strategy TEXT NOT NULL, family TEXT NOT NULL, symbol TEXT NOT NULL,
    verdict TEXT NOT NULL, params_json TEXT, test_json TEXT, wf_json TEXT,
    fees_json TEXT, random_json TEXT, regimes_json TEXT, reasons_json TEXT,
    buy_hold REAL
);
CREATE TABLE IF NOT EXISTS strategy_stats (
    run_id INTEGER NOT NULL REFERENCES research_runs(id),
    strategy TEXT NOT NULL, symbol TEXT NOT NULL, regime TEXT NOT NULL,
    n INTEGER NOT NULL, wins INTEGER NOT NULL,
    avg_win_r REAL, avg_loss_r REAL, expectancy_r REAL,
    verdict TEXT NOT NULL, params_json TEXT
);
"""


@dataclass(frozen=True)
class OosStats:
    strategy: str
    symbol: str
    regime: str
    n: int
    wins: int
    avg_win_r: float | None
    avg_loss_r: float | None
    expectancy_r: float | None
    verdict: str
    params: dict[str, Any]


def _js(obj: Any) -> str:
    def default(o):
        if isinstance(o, TradeStats):
            return o.as_dict()
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        return str(o)

    return json.dumps(obj, default=default, sort_keys=True)


class ResearchStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def save(
        self,
        results: list[StrategyResearch],
        report_md: str,
        data_source: str,
        timeframe: str | None,
        config: dict[str, Any],
    ) -> int:
        symbols = sorted({r.symbol for r in results})
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO research_runs(created_at, data_source, timeframe, symbols, "
                "config_json, report_md) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    pd.Timestamp.now(tz="UTC").isoformat(),
                    data_source,
                    timeframe,
                    ",".join(symbols),
                    _js(config),
                    report_md,
                ),
            )
            run_id = int(cur.lastrowid)
            for r in results:
                self._conn.execute(
                    "INSERT INTO research_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        r.strategy,
                        r.family,
                        r.symbol,
                        r.verdict,
                        _js(r.chosen_params),
                        _js(r.test.stats if r.test else None),
                        _js(r.walk_forward),
                        _js(r.fee_sensitivity),
                        _js(r.random_baseline),
                        _js(r.regimes),
                        _js(r.reasons),
                        r.buy_and_hold_test,
                    ),
                )
            for row in _oos_rows(results):
                self._conn.execute(
                    "INSERT INTO strategy_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)", (run_id, *row)
                )
        return run_id

    def latest_run_id(self) -> int | None:
        row = self._conn.execute("SELECT MAX(id) FROM research_runs").fetchone()
        return row[0]

    def oos_stats(self, run_id: int | None = None) -> list[OosStats]:
        run_id = run_id or self.latest_run_id()
        if run_id is None:
            return []
        rows = self._conn.execute(
            "SELECT strategy, symbol, regime, n, wins, avg_win_r, avg_loss_r, expectancy_r, "
            "verdict, params_json FROM strategy_stats WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        return [OosStats(*r[:9], json.loads(r[9] or "{}") or {}) for r in rows]

    def runs(self) -> pd.DataFrame:
        return pd.read_sql_query(
            "SELECT id, created_at, data_source, timeframe, symbols FROM research_runs "
            "ORDER BY id DESC",
            self._conn,
        )

    def results(self, run_id: int | None = None) -> pd.DataFrame:
        run_id = run_id or self.latest_run_id()
        return pd.read_sql_query(
            "SELECT strategy, family, symbol, verdict, test_json, wf_json, random_json, "
            "fees_json, regimes_json, reasons_json, params_json, buy_hold "
            "FROM research_results WHERE run_id = ?",
            self._conn,
            params=(run_id,),
        )

    def report(self, run_id: int | None = None) -> str | None:
        run_id = run_id or self.latest_run_id()
        row = self._conn.execute(
            "SELECT report_md FROM research_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row[0] if row else None


def _group_rows(trades, regime_of, strategy, symbol, verdict, params):
    by_regime: dict[str, list] = {"*": list(trades)}
    for t in trades:
        by_regime.setdefault(regime_of.get(t.entry_time.isoformat(), "unknown"), []).append(t)
    for regime, ts in by_regime.items():
        r = np.array([t.r_multiple for t in ts]) if ts else np.array([])
        wins, losses = r[r > 0], r[r <= 0]
        yield (
            strategy,
            symbol,
            regime,
            len(ts),
            int((r > 0).sum()),
            float(wins.mean()) if len(wins) else None,
            float(losses.mean()) if len(losses) else None,
            float(r.mean()) if len(r) else None,
            verdict,
            _js(params),
        )


def _oos_rows(results: list[StrategyResearch]):
    pooled: dict[str, tuple[list, dict, list]] = {}
    for res in results:
        if not res.test:
            continue
        trades = res.test.trades + res.walk_forward_trades
        # Doppelte Trades (Test und WF überlappen zeitlich) nur einmal zählen
        unique = {(t.entry_time, t.exit_time): t for t in trades}
        trades = sorted(unique.values(), key=lambda t: t.entry_time)
        yield from _group_rows(
            trades, res.regime_of_trade, res.strategy, res.symbol, res.verdict, res.chosen_params
        )
        bucket = pooled.setdefault(res.strategy, ([], {}, []))
        bucket[0].extend(trades)
        bucket[1].update(res.regime_of_trade)
        bucket[2].append(res.verdict)
    for strategy, (trades, regimes, verdicts) in pooled.items():
        verdict = (
            "bestätigt"
            if verdicts and all(v == "bestätigt" for v in verdicts)
            else (
                "verworfen" if verdicts and all(v == "verworfen" for v in verdicts) else "hypothese"
            )
        )
        yield from _group_rows(trades, regimes, strategy, "*", verdict, {})
