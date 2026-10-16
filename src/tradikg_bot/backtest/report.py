"""Textbericht für die Kommandozeile."""

from __future__ import annotations

from typing import Any

from tradikg_bot.backtest.engine import BacktestResult


def _pct(v: Any) -> str:
    return "n/a" if v is None else f"{v * 100:,.2f} %"


def _num(v: Any, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:,.{digits}f}"


def format_report(result: BacktestResult, currency: str = "EUR") -> str:
    m = result.metrics
    bench = m["daily_volume_benchmark"]
    lines = [
        f"Backtest: {result.symbol} | Strategie: {result.strategy_name} {result.strategy_params}",
        f"Zeitraum: {result.equity.index[0]} – {result.equity.index[-1]} ({m['bars']} Kerzen)",
        "",
        f"Startkapital:          {_num(m['initial_capital'])} {currency}",
        f"Endkapital:            {_num(m['final_equity'])} {currency}",
        f"Nettogewinn:           {_num(m['net_profit'])} {currency} ({_pct(m['total_return'])})",
        f"Buy & Hold (Referenz): {_pct(m['buy_and_hold_return'])}",
        f"CAGR:                  {_pct(m['cagr'])}",
        f"Max. Drawdown:         {_pct(m['max_drawdown'])}",
        f"Sharpe / Sortino:      {_num(m['sharpe'])} / {_num(m['sortino'])}",
        f"Marktexposition:       {_pct(m['exposure'])}",
        "",
        f"Trades:                {m['num_trades']}",
        f"Trefferquote:          {_pct(m['win_rate'])}",
        f"Profit-Faktor:         {_num(m['profit_factor'])}",
        f"Erwartungswert/Trade:  {_num(m['expectancy'], 4)} {currency}",
        f"Ø R-Multiple:          {_num(m['avg_r_multiple'])}",
        f"Exit-Gründe:           {m['exit_reasons']}",
        f"Verworfene Einstiege:  {m['skipped_entries']}",
        f"Tagesverlust-Stopps:   {m['daily_loss_halts']}",
        "",
        f"Bruttoergebnis:        {_num(m['gross_pnl'], 4)} {currency}",
        f"Kommissionen:          {_num(m['total_commission'], 4)} {currency}",
        f"Spread + Slippage:     {_num(m['total_slippage_and_spread'], 4)} {currency}",
        f"Kosten/|Brutto|:       {_pct(m['costs_to_gross_ratio'])}",
        f"Handelsvolumen:        {_num(m['traded_volume'])} {currency}",
        "",
        "Analyse-Benchmark (kein Handelsziel): Nettoergebnis / Tagesvolumen "
        f">= {_pct(bench['benchmark'])}",
        f"  Handelstage: {bench['trading_days']}, Ø {_pct(bench['mean'])}, "
        f"Median {_pct(bench['median'])}, erreicht an {bench['days_meeting_benchmark']} Tag(en)",
    ]
    return "\n".join(lines)
