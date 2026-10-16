"""Lesende Abfragen für das Dashboard (keine Schreibzugriffe, keine Schlüssel).

Alle Datenbanken werden schreibgeschützt geöffnet (``mode=ro``). Fehlt eine
Datenbank (frische Installation), liefern die Funktionen leere Ergebnisse.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tradikg_bot.platform_config import PlatformConfig
from tradikg_bot.service.ops import read_status


@contextmanager
def _ro(path: Path):
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def _df(path: Path, sql: str, params: tuple = ()) -> pd.DataFrame:
    with _ro(path) as conn:
        if conn is None:
            return pd.DataFrame()
        try:
            return pd.read_sql_query(sql, conn, params=params)
        except (sqlite3.OperationalError, pd.errors.DatabaseError):
            return pd.DataFrame()


def paper_db(cfg: PlatformConfig) -> Path:
    return cfg.paper_dir / "paper.sqlite"


# -- Übersicht ------------------------------------------------------------------------------------
def balances(cfg: PlatformConfig) -> dict[str, float]:
    df = _df(
        paper_db(cfg),
        "SELECT COALESCE(SUM(to_trading),0) t, COALESCE(SUM(to_payout),0) p, "
        "COALESCE(SUM(to_longterm),0) l, COALESCE(SUM(to_tax),0) x, "
        "COALESCE(SUM(net_pnl),0) n FROM ledger_entries",
    )
    last = _df(
        paper_db(cfg), "SELECT unrecovered_after FROM ledger_entries ORDER BY id DESC LIMIT 1"
    )
    initial = cfg.capital_eur
    row = df.iloc[0] if not df.empty else pd.Series({"t": 0, "p": 0, "l": 0, "x": 0, "n": 0})
    out = {
        "initial_capital": initial,
        "trading_balance": initial + float(row["t"]),
        "payout_reserve": float(row["p"]),
        "longterm_portfolio": float(row["l"]),
        "tax_reserve": float(row["x"]),
        "total_net_pnl": float(row["n"]),
        "unrecovered_losses": float(last.iloc[0, 0]) if not last.empty else 0.0,
    }
    out["total_capital"] = (
        out["trading_balance"]
        + out["payout_reserve"]
        + out["longterm_portfolio"]
        + out["tax_reserve"]
    )
    out["net_return"] = out["total_net_pnl"] / initial if initial else 0.0
    return out


def closed_trades(cfg: PlatformConfig) -> pd.DataFrame:
    trades = _df(paper_db(cfg), "SELECT * FROM trades WHERE state = 'closed' ORDER BY id")
    analysis = _df(paper_db(cfg), "SELECT trade_id, payload FROM trade_analysis")
    if trades.empty:
        return trades
    if not analysis.empty:
        parsed = pd.DataFrame([json.loads(p) for p in analysis["payload"]])
        keep = [
            "trade_id",
            "strategy",
            "regime",
            "p_win",
            "mfe",
            "mae",
            "fees_eur",
            "holding_seconds",
            "assessment",
            "findings",
            "entry_ts",
            "exit_ts",
        ]
        parsed = parsed[[c for c in keep if c in parsed.columns]]
        trades = trades.merge(parsed, left_on="id", right_on="trade_id", how="left")
    trades["closed_at"] = pd.to_datetime(trades["closed_at"], utc=True, format="ISO8601")
    return trades


def pnl_windows(cfg: PlatformConfig, now: pd.Timestamp | None = None) -> dict[str, float]:
    trades = closed_trades(cfg)
    now = now or pd.Timestamp.now(tz="UTC")
    if trades.empty:
        return {"today": 0.0, "week": 0.0, "total": 0.0}
    pnl = trades.set_index("closed_at")["realized_pnl"]
    return {
        "today": float(pnl[pnl.index.date == now.date()].sum()),
        "week": float(pnl[pnl.index >= now - pd.Timedelta(days=7)].sum()),
        "total": float(pnl.sum()),
    }


def open_positions(cfg: PlatformConfig) -> pd.DataFrame:
    return _df(
        paper_db(cfg),
        "SELECT id, pair, state, filled_volume, entry_avg_price, entry_fee, "
        "stop_loss_price, take_profit_price, created_at FROM trades "
        "WHERE state IN ('submitting','entry_accepted','protected','unresolved')",
    )


def equity_curve(cfg: PlatformConfig) -> pd.DataFrame:
    df = _df(paper_db(cfg), "SELECT * FROM equity ORDER BY ts")
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    df["drawdown"] = df["equity"] / df["equity"].cummax() - 1
    return df.set_index("ts")


# -- Analytics --------------------------------------------------------------------
def _stats(pnl: pd.Series) -> dict[str, Any]:
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    return {
        "trades": int(len(pnl)),
        "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,
        "expectancy": float(pnl.mean()) if len(pnl) else None,
        "net": float(pnl.sum()),
    }


def performance(cfg: PlatformConfig) -> dict[str, Any]:
    trades = closed_trades(cfg)
    if trades.empty:
        return {
            "overall": _stats(pd.Series(dtype=float)),
            "by_asset": pd.DataFrame(),
            "by_strategy": pd.DataFrame(),
            "by_regime": pd.DataFrame(),
            "by_weekday": pd.DataFrame(),
            "costs": {},
        }

    def grouped(col: str) -> pd.DataFrame:
        if col not in trades:
            return pd.DataFrame()
        rows = [{col: k, **_stats(g["realized_pnl"])} for k, g in trades.groupby(col)]
        return pd.DataFrame(rows)

    trades["weekday"] = trades["closed_at"].dt.day_name()
    fees = float(trades["entry_fee"].sum() + trades["exit_fee"].sum())
    gross = float((trades["realized_pnl"] + trades["entry_fee"] + trades["exit_fee"]).sum())
    return {
        "overall": _stats(trades["realized_pnl"]),
        "by_asset": grouped("pair"),
        "by_strategy": grouped("strategy"),
        "by_regime": grouped("regime"),
        "by_weekday": grouped("weekday"),
        "costs": {
            "fees_eur": fees,
            "gross_before_fees_eur": gross,
            "fees_share_of_gross": fees / abs(gross) if gross else None,
        },
    }


def recent_signals(cfg: PlatformConfig, limit: int = 200) -> pd.DataFrame:
    return _df(paper_db(cfg), "SELECT * FROM signals_log ORDER BY id DESC LIMIT ?", (limit,))


def daily_reports(cfg: PlatformConfig) -> pd.DataFrame:
    return _df(paper_db(cfg), "SELECT day, markdown FROM daily_reports ORDER BY day DESC")


# -- AI Research --------------------------------------------------------------------
def research_results(cfg: PlatformConfig) -> pd.DataFrame:
    df = _df(
        cfg.research_db,
        "SELECT r.run_id, r.strategy, r.family, r.symbol, r.verdict, r.test_json, "
        "r.wf_json, r.random_json FROM research_results r WHERE r.run_id = "
        "(SELECT MAX(id) FROM research_runs)",
    )
    if df.empty:
        return df
    test = pd.DataFrame([json.loads(x) if x and x != "null" else {} for x in df["test_json"]])
    wf = pd.DataFrame([json.loads(x) if x else {} for x in df["wf_json"]])
    rnd = pd.DataFrame([json.loads(x) if x else {} for x in df["random_json"]])
    out = df[["run_id", "strategy", "family", "symbol", "verdict"]].copy()
    out["test_trades"] = test.get("n")
    out["test_expectancy_r"] = test.get("expectancy_r")
    out["test_profit_factor"] = test.get("profit_factor")
    out["wf_expectancy_r"] = wf.get("expectancy_r")
    out["random_p_value"] = rnd.get("p_value")
    order = {"bestätigt": 0, "hypothese": 1, "verworfen": 2}
    out["_rank"] = out["verdict"].map(order).fillna(3)
    out = out.sort_values(
        ["_rank", "test_expectancy_r"], ascending=[True, False], na_position="last"
    )
    return out.drop(columns="_rank")


def research_runs(cfg: PlatformConfig) -> pd.DataFrame:
    return _df(
        cfg.research_db,
        "SELECT id, created_at, data_source, timeframe, symbols "
        "FROM research_runs ORDER BY id DESC",
    )


def models(cfg: PlatformConfig) -> pd.DataFrame:
    return _df(
        cfg.models_db,
        "SELECT version, kind, status, created_at, train_start, train_end, "
        "test_start, test_end, metrics_json FROM models ORDER BY created_at DESC",
    )


def model_events(cfg: PlatformConfig) -> pd.DataFrame:
    return _df(
        cfg.models_db,
        "SELECT ts, version, event, detail FROM model_events ORDER BY id DESC LIMIT 50",
    )


def calibration(cfg: PlatformConfig) -> pd.DataFrame:
    """Prognose vs. tatsächliches Ergebnis (nur Prognosen mit bekanntem Ausgang)."""
    df = _df(cfg.models_db, "SELECT p, outcome FROM predictions WHERE outcome IS NOT NULL")
    if df.empty:
        return df
    df["bin"] = pd.cut(df["p"], bins=np.linspace(0, 1, 11), include_lowest=True)
    out = (
        df.groupby("bin", observed=True)
        .agg(n=("p", "size"), predicted=("p", "mean"), observed=("outcome", "mean"))
        .reset_index()
    )
    out["bin"] = out["bin"].astype(str)
    return out


# -- System Health --------------------------------------------------------------------
def health(cfg: PlatformConfig) -> dict[str, Any]:
    status = read_status(cfg.status_file) or {}
    h = _df(paper_db(cfg), "SELECT key, value, updated_at FROM health")
    halt = _df(paper_db(cfg), "SELECT value FROM kv WHERE key = 'halt_reason'")
    errors = _df(
        paper_db(cfg),
        "SELECT ts, level, message FROM events WHERE level IN "
        "('ERROR','CRITICAL','WARNING') ORDER BY id DESC LIMIT 50",
    )
    info: dict[str, Any] = {
        "status": status,
        "health": {r.key: (r.value, r.updated_at) for r in h.itertuples()} if not h.empty else {},
        "safe_halt": (halt.iloc[0, 0] or None) if not halt.empty else None,
        "kill_switch": cfg.kill_switch.exists(),
        "errors": errors,
        "lock_pid": (cfg.lock_file.read_text().strip() or None) if cfg.lock_file.exists() else None,
    }
    try:
        import psutil

        info["cpu_percent"] = psutil.cpu_percent(interval=None)
        info["ram_percent"] = psutil.virtual_memory().percent
        procs = []
        for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if "tradikg" in cmd and "dashboard" not in cmd:
                procs.append(
                    {
                        "pid": p.info["pid"],
                        "cmd": cmd[:120],
                        "started": pd.Timestamp(p.info["create_time"], unit="s", tz="UTC"),
                    }
                )
        info["processes"] = pd.DataFrame(procs)
    except Exception:
        info["cpu_percent"] = info["ram_percent"] = None
        info["processes"] = pd.DataFrame()
    return info


def settings(cfg: PlatformConfig) -> dict[str, Any]:
    """Einstellungen ohne Geheimnisse; der Live-Modus ist im Dashboard nicht schaltbar."""
    return {
        "mode": cfg.mode,
        "live_mode": "deaktiviert (nicht über das Dashboard schaltbar)",
        "timeframe": cfg.timeframe,
        "capital_eur": cfg.capital_eur,
        "symbols": list(cfg.symbols),
        "use_scanner": cfg.use_scanner,
        "strategies": list(cfg.strategies) or "alle",
        "tax_rate": cfg.tax_rate,
        "log_level": cfg.log_level,
        "home": str(cfg.home),
        "local_llm_enabled": cfg.local_llm_enabled,
    }


# -- Historical Replay -----------------------------------------------------------------------
def replay_runs(cfg: PlatformConfig) -> pd.DataFrame:
    return _df(
        cfg.replay_db,
        "SELECT id, created_at, data_source, synthetic, timeframe, symbols, holdout_evaluated, "
        "holdout_count FROM replay_runs ORDER BY id DESC",
    )


def replay_run(cfg: PlatformConfig, run_id: int) -> dict[str, Any] | None:
    df = _df(
        cfg.replay_db,
        "SELECT id, created_at, data_source, synthetic, timeframe, symbols, plan_json, "
        "research_json, holdout_evaluated, holdout_count, report_md FROM replay_runs WHERE id = ?",
        (run_id,),
    )
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    row["symbols"] = json.loads(row["symbols"])
    row["plan"] = json.loads(row.pop("plan_json"))
    row["research"] = json.loads(row.pop("research_json"))
    row["synthetic"] = bool(row["synthetic"])
    return row


def replay_scenarios(cfg: PlatformConfig, run_id: int) -> pd.DataFrame:
    df = _df(cfg.replay_db, "SELECT * FROM replay_scenarios WHERE run_id = ?", (run_id,))
    for col in ("start", "end"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    return df


def replay_equity(cfg: PlatformConfig, run_id: int, scenario_ids: list[str]) -> pd.DataFrame:
    if not scenario_ids:
        return pd.DataFrame()
    marks = ", ".join("?" * len(scenario_ids))
    df = _df(
        cfg.replay_db,
        f"SELECT * FROM replay_equity WHERE run_id = ? AND scenario_id IN ({marks}) ORDER BY ts",
        (run_id, *scenario_ids),
    )
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df


def replay_trades(cfg: PlatformConfig, run_id: int, scenario_ids: list[str]) -> pd.DataFrame:
    if not scenario_ids:
        return pd.DataFrame()
    marks = ", ".join("?" * len(scenario_ids))
    df = _df(
        cfg.replay_db,
        f"SELECT scenario_id, payload FROM replay_trades WHERE run_id = ? AND scenario_id IN "
        f"({marks}) ORDER BY scenario_id, trade_id",
        (run_id, *scenario_ids),
    )
    if df.empty:
        return df
    pairs = zip(df["scenario_id"], df["payload"], strict=True)
    rows = [{"scenario_id": s, **json.loads(p)} for s, p in pairs]
    return pd.DataFrame(rows)


def replay_strategy_stats(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty or "strategy" not in trades.columns:
        return pd.DataFrame()
    g = trades.groupby("strategy")
    out = pd.DataFrame(
        {
            "trades": g.size(),
            "trefferquote": g["net_pnl_eur"].apply(lambda s: float((s > 0).mean())),
            "netto_eur": g["net_pnl_eur"].sum(),
            "gebuehren_eur": g["fees_eur"].sum(),
            "sl_tp_gleiche_kerze": g["intrabar_ambiguous"].sum()
            if "intrabar_ambiguous" in trades.columns
            else 0,
        }
    )
    return out.reset_index().sort_values("trades", ascending=False)
