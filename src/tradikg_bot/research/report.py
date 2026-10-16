"""Nachvollziehbare Research-Berichte (Markdown)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from tradikg_bot.research.engine import ResearchConfig, StrategyResearch, TradeStats, pooled_stats


def _f(v: Any, pct: bool = False, digits: int = 2) -> str:
    if v is None:
        return "–"
    return f"{v * 100:.{digits}f} %" if pct else f"{v:.{digits}f}"


def render_report(
    results: list[StrategyResearch],
    cfg: ResearchConfig,
    data_source: str,
    timeframe: str | None = None,
) -> str:
    now = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Research-Bericht",
        "",
        f"Erstellt: {now} · Datenquelle: **{data_source}** · Zeitintervall: {timeframe or '–'}",
        "",
        "> Ein Ergebnis gilt nur für die untersuchten Daten und den untersuchten Zeitraum.",
        "> „bestätigt“ heißt: auf ungesehenen Testdaten nach Kosten positiv und besser als",
        "> Zufallseinstiege – **keine Garantie für zukünftige Gewinne**.",
    ]
    if "synthetic" in data_source:
        lines += [
            "",
            "> **Achtung: synthetische Daten.** Das Ergebnis prüft nur die Pipeline "
            "und sagt nichts über echte Märkte aus.",
        ]
    lines += [
        "",
        f"Aufteilung: Train {cfg.train_frac:.0%} / Validation {cfg.val_frac:.0%} / Test "
        f"{1 - cfg.train_frac - cfg.val_frac:.0%}, Walk-Forward {cfg.wf_folds} Blöcke, "
        f"{cfg.random_runs} Zufallsläufe, Kostenfaktoren {list(cfg.fee_multipliers)}.",
        "",
        "## Übersicht (Testzeitraum, Out-of-Sample)",
        "",
        "| Strategie | Asset | Urteil | Trades | Treffer | PF | Ø R | Netto | Max DD | "
        "Ø R ×2 Kosten | Zufall p | WF Ø R | Buy&Hold |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda x: (x.strategy, x.symbol)):
        s = r.test.stats if r.test else TradeStats()
        x2 = r.fee_sensitivity.get("x2", TradeStats())
        lines.append(
            f"| {r.strategy} | {r.symbol} | {r.verdict} | {s.n} | {_f(s.win_rate, True, 0)} | "
            f"{_f(s.profit_factor)} | {_f(s.expectancy_r)} | {_f(s.net_return, True)} | "
            f"{_f(s.max_drawdown, True)} | {_f(x2.expectancy_r)} | "
            f"{_f(r.random_baseline.get('p_value'))} | {_f(r.walk_forward.expectancy_r)} | "
            f"{_f(r.buy_and_hold_test, True)} |"
        )
    lines += [
        "",
        "## Gepoolt über alle Assets (Test + Walk-Forward-OOS)",
        "",
        "| Strategie | Trades | Treffer | PF | Ø R | t-Wert (R) |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted({r.strategy for r in results}):
        p = pooled_stats(results, name, cfg.initial_capital)
        lines.append(
            f"| {name} | {p.n} | {_f(p.win_rate, True, 0)} | {_f(p.profit_factor)} | "
            f"{_f(p.expectancy_r)} | {_f(p.t_stat_r)} |"
        )
    lines += ["", "## Details", ""]
    for r in sorted(results, key=lambda x: (x.strategy, x.symbol)):
        lines += [
            f"### {r.strategy} · {r.symbol} → {r.verdict}",
            "",
            f"- Familie: {r.family}; Parameterwahl: {r.selection_note}; "
            f"Parameter: `{r.chosen_params}`",
            f"- Begründung: {'; '.join(r.reasons)}",
        ]
        if r.regimes:
            reg = ", ".join(
                f"{k}: n={v.n}, Ø R={_f(v.expectancy_r)}" for k, v in sorted(r.regimes.items())
            )
            lines.append(f"- Marktphasen (Test): {reg}")
        fees = ", ".join(f"{k}: Ø R={_f(v.expectancy_r)}" for k, v in r.fee_sensitivity.items())
        lines += [f"- Kosten-Sensitivität: {fees}", ""]
    return "\n".join(lines)
