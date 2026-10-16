"""Performance-, Risiko- und Kostenkennzahlen eines Backtests."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from tradikg_bot.backtest.engine import BacktestResult

SECONDS_PER_YEAR = 365.25 * 24 * 3600


def _periods_per_year(index: pd.DatetimeIndex) -> float:
    """Kerzen pro Jahr, geschätzt aus der tatsächlichen Datenabdeckung.

    Funktioniert für 24/7-Krypto und Börsenhandelstage gleichermaßen.
    """
    if len(index) < 2:
        return float("nan")
    span = (index[-1] - index[0]).total_seconds()
    if span <= 0:
        return float("nan")
    return (len(index) - 1) / (span / SECONDS_PER_YEAR)


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1).min())


def daily_volume_benchmark(
    equity: pd.Series, fills: pd.DataFrame, benchmark: float, initial_capital: float
) -> dict[str, Any]:
    """Nettoergebnis je Tag im Verhältnis zum gehandelten Volumen (Kauf+Verkauf).

    Reine Analysegröße – die Engine verwendet diesen Wert nie für Entscheidungen.
    """
    if fills.empty:
        return {
            "benchmark": benchmark,
            "trading_days": 0,
            "mean": None,
            "median": None,
            "days_meeting_benchmark": 0,
        }
    day_end_equity = equity.groupby(equity.index.date).last()
    prev = day_end_equity.shift(1)
    prev.iloc[0] = initial_capital
    daily_pnl = day_end_equity - prev
    volume = fills.groupby(pd.DatetimeIndex(fills["time"]).date)["notional"].sum()
    traded = volume[volume > 0]
    ratio = (daily_pnl.reindex(traded.index) / traded).dropna()
    return {
        "benchmark": benchmark,
        "trading_days": int(len(ratio)),
        "mean": float(ratio.mean()) if len(ratio) else None,
        "median": float(ratio.median()) if len(ratio) else None,
        "days_meeting_benchmark": int((ratio >= benchmark).sum()),
    }


def compute_metrics(
    result: BacktestResult, bars: pd.DataFrame, benchmark_daily_volume_return: float = 0.10
) -> dict[str, Any]:
    eq = result.equity["equity"]
    initial = result.initial_capital
    final = float(eq.iloc[-1])
    rets = eq.pct_change().dropna()
    ppy = _periods_per_year(eq.index)
    years = (eq.index[-1] - eq.index[0]).total_seconds() / SECONDS_PER_YEAR

    std = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    sharpe = float(rets.mean() / std * np.sqrt(ppy)) if std > 0 and np.isfinite(ppy) else None
    downside = rets[rets < 0]
    dstd = float(np.sqrt((downside**2).mean())) if len(downside) else 0.0
    sortino = float(rets.mean() / dstd * np.sqrt(ppy)) if dstd > 0 and np.isfinite(ppy) else None
    cagr = (final / initial) ** (1 / years) - 1 if years >= 0.25 and final > 0 else None

    trades = result.trades
    net = np.array([t.net_pnl for t in trades], dtype=float)
    gross = np.array([t.gross_pnl for t in trades], dtype=float)
    wins, losses = net[net > 0], net[net <= 0]
    commission = float(sum(t.commission for t in trades))
    slippage = float(sum(t.slippage_cost for t in trades))
    total_cost = commission + slippage
    gross_abs = float(np.abs(gross).sum())

    bh_return = float(bars["close"].iloc[-1] / bars["open"].iloc[0] - 1)
    mdd = max_drawdown(eq)
    total_return = final / initial - 1

    return {
        "initial_capital": initial,
        "final_equity": final,
        "net_profit": final - initial,
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": mdd,
        "calmar": (cagr / abs(mdd)) if cagr is not None and mdd < 0 else None,
        "sharpe": sharpe,
        "sortino": sortino,
        "volatility_annualized": std * np.sqrt(ppy) if np.isfinite(ppy) else None,
        "bars": len(eq),
        "periods_per_year": ppy,
        "exposure": float((result.equity["position_qty"] > 0).mean()),
        "num_trades": len(trades),
        "win_rate": float(len(wins) / len(trades)) if trades else None,
        "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(losses.mean()) if len(losses) else None,
        "profit_factor": (
            float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else None
        ),
        "expectancy": float(net.mean()) if trades else None,
        "avg_r_multiple": float(np.mean([t.r_multiple for t in trades])) if trades else None,
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])) if trades else None,
        "gross_pnl": float(gross.sum()),
        "total_commission": commission,
        "total_slippage_and_spread": slippage,
        "total_costs": total_cost,
        "costs_to_gross_ratio": total_cost / gross_abs if gross_abs > 0 else None,
        "traded_volume": float(result.fills["notional"].sum()) if not result.fills.empty else 0.0,
        "exit_reasons": dict(Counter(str(t.exit_reason) for t in trades)),
        "skipped_entries": dict(Counter(str(s.reason) for s in result.skipped_entries)),
        "daily_loss_halts": len(result.halted_days),
        "buy_and_hold_return": bh_return,
        "daily_volume_benchmark": daily_volume_benchmark(
            eq, result.fills, benchmark_daily_volume_return, initial
        ),
    }
