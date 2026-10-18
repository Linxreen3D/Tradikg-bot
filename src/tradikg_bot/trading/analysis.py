"""Automatische Nachanalyse jedes abgeschlossenen Trades und Tagesberichte.

Leitsätze:
* Ein verlorener Trade ist nicht automatisch ein fehlerhafter Trade, ein
  gewonnener nicht automatisch ein guter.
* Bewertet wird der **Prozess**: War das Signal statistisch begründet, passte die
  Strategie zur Marktphase, war die Größe im Rahmen, haben Kosten den Vorteil
  aufgezehrt, lagen Daten- oder Ausführungsprobleme vor?
* Verbesserungen sind **Hypothesen**. Sie werden nie automatisch aktiviert,
  sondern müssen in der Research-Engine getestet werden.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from tradikg_bot.live.journal import TradeRecord


def analyze_trade(
    trade: TradeRecord,
    meta: dict[str, Any],
    bars: pd.DataFrame,
    oos_regime_expectancy: float | None,
) -> dict[str, Any]:
    entry_ts = pd.Timestamp(meta["entry_ts"])
    exit_ts = pd.Timestamp(trade.closed_at) if trade.closed_at else entry_ts
    window = bars[(bars.index >= entry_ts) & (bars.index < exit_ts + pd.Timedelta(seconds=1))]
    entry = trade.entry_avg_price
    mfe = float(window["high"].max() / entry - 1) if not window.empty and entry else 0.0
    mae = float(window["low"].min() / entry - 1) if not window.empty and entry else 0.0
    gross = trade.exit_volume * trade.exit_avg_price - trade.filled_volume * entry
    fees = trade.entry_fee + trade.exit_fee
    net = trade.realized_pnl or 0.0
    planned_loss = meta.get("loss_net_per_unit", 0.0) * trade.filled_volume
    expected_ev = meta.get("ev_per_unit", 0.0) * trade.filled_volume
    ref = meta.get("ref_price") or entry
    entry_slippage = (entry / ref - 1) if ref else 0.0

    findings: list[str] = []
    if gross > 0 and net <= 0:
        findings.append("Kosten haben den Bruttogewinn aufgezehrt")
    if oos_regime_expectancy is not None and oos_regime_expectancy <= 0:
        findings.append(f"Strategie in Marktphase '{meta.get('regime')}' historisch ohne Vorteil")
    if meta.get("p_source", "").startswith("prior"):
        findings.append("Signal ohne Out-of-Sample-Belege (sollte nicht vorkommen)")
    if planned_loss > 0 and net < -1.5 * planned_loss:
        findings.append("Verlust deutlich über geplantem Worst Case (Kurslücke/Slippage)")
    if entry_slippage > 0.003:
        findings.append(f"Einstiegs-Slippage {entry_slippage:.2%} ungewöhnlich hoch")
    if meta.get("data_flags"):
        findings.append(f"Datenhinweise beim Signal: {meta['data_flags']}")
    take_distance = meta.get("take_distance") or 0.0
    if (
        "stop_loss" in (trade.exit_reason or "")
        and take_distance
        and mfe * entry >= 0.8 * (take_distance)
    ):
        findings.append("Kurs lief vor dem Stop fast bis zum Ziel (TP evtl. zu weit)")

    process_ok = not any(
        ("Datenhinweise" in f or "ohne Out-of-Sample" in f or "Slippage" in f) for f in findings
    )
    outcome = "Gewinn" if net > 0 else "Verlust"
    if process_ok:
        assessment = f"{outcome} bei regelkonformem Prozess"
    else:
        assessment = f"{outcome} mit Prozess-/Daten-/Ausführungsauffälligkeit"
    return {
        "trade_id": trade.id,
        "asset": trade.pair,
        "strategy": meta.get("strategy"),
        "model_version": meta.get("model_version"),
        "p_source": meta.get("p_source"),
        "regime": meta.get("regime"),
        "signal_ts": meta.get("signal_ts"),
        "entry_ts": meta.get("entry_ts"),
        "exit_ts": trade.closed_at,
        "p_win": meta.get("p_win"),
        "expected_ev_eur": expected_ev,
        "entry_price": entry,
        "exit_price": trade.exit_avg_price,
        "stop_loss": trade.stop_loss_price,
        "take_profit": trade.take_profit_price,
        "volume": trade.filled_volume,
        "fees_eur": fees,
        "spread_bps_assumed": meta.get("spread_bps"),
        "entry_slippage": entry_slippage,
        "gross_pnl_eur": gross,
        "net_pnl_eur": net,
        "planned_worst_loss_eur": planned_loss,
        "holding_seconds": (exit_ts - entry_ts).total_seconds(),
        "mfe": mfe,
        "mae": mae,
        "exit_reason": trade.exit_reason,
        "outcome_win": net > 0,
        "process_ok": process_ok,
        "assessment": assessment,
        "findings": findings,
    }


def daily_report(day: str, analyses: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    lines = [f"# Tagesbericht {day}", ""]
    if not analyses:
        lines.append("Keine abgeschlossenen Trades. Keine Trades ist ein gültiges Ergebnis.")
        return "\n".join(lines), {"day": day, "trades": 0}
    df = pd.DataFrame(analyses)
    n = len(df)
    net = float(df["net_pnl_eur"].sum())
    fees = float(df["fees_eur"].sum())
    win_rate = float(df["outcome_win"].mean())
    predicted = float(df["p_win"].astype(float).mean())
    payload: dict[str, Any] = {
        "day": day,
        "trades": n,
        "net_pnl_eur": net,
        "fees_eur": fees,
        "win_rate": win_rate,
        "predicted_win_rate": predicted,
        "process_ok_share": float(df["process_ok"].mean()),
    }
    lines += [
        f"- Trades: {n}, Netto: {net:+.4f} EUR, Gebühren: {fees:.4f} EUR",
        f"- Trefferquote: {win_rate:.0%} (vorhergesagt Ø {predicted:.0%})",
        f"- Prozess regelkonform: {payload['process_ok_share']:.0%}",
        "",
        "## Nach Strategie",
        "",
    ]
    for strat, g in df.groupby("strategy"):
        lines.append(
            f"- {strat}: {len(g)} Trades, Netto {g['net_pnl_eur'].sum():+.4f} EUR, "
            f"Treffer {g['outcome_win'].mean():.0%}"
        )
    lines += ["", "## Nach Marktphase", ""]
    for reg, g in df.groupby("regime"):
        lines.append(f"- {reg}: {len(g)} Trades, Netto {g['net_pnl_eur'].sum():+.4f} EUR")

    hypotheses: list[str] = []
    findings = [f for fs in df["findings"] for f in fs]
    if any("Kosten" in f for f in findings):
        hypotheses.append(
            "Kosten zehren Vorteil auf: größere TP-Abstände oder Maker-Einstiege "
            "in der Research-Engine testen."
        )
    if any("Marktphase" in f for f in findings):
        hypotheses.append("Regime-Filter für betroffene Strategien testen.")
    if any("fast bis zum Ziel" in f for f in findings):
        hypotheses.append("Engeres Take-Profit bzw. Teilgewinnmitnahme testen.")
    if n >= 10 and abs(win_rate - predicted) > 0.2:
        hypotheses.append(
            f"Kalibrierung prüfen: vorhergesagt {predicted:.0%}, realisiert "
            f"{win_rate:.0%} (n={n}, kleine Stichprobe beachten)."
        )
    payload["hypotheses"] = hypotheses
    lines += ["", "## Verbesserungshypothesen (nur zum Testen, nicht aktiviert)", ""]
    lines += [f"- {h}" for h in hypotheses] or ["- keine"]
    return "\n".join(lines), payload


# -- Nachanalyse für Trendfolge, Grid und Echtgeld (Audit Punkt 5) ----------------------------
LOSS_CLASSES = (
    "Gewinn",
    "Gebührenproblem",
    "Ausführungsfehler",
    "Prognosefehler",
    "normaler statistischer Verlust",
)


def classify_trade(
    gross: float,
    net: float,
    slippage: float | None = None,
    ref_loss_p10: float | None = None,
    loss_pct: float | None = None,
) -> str:
    """Ordnet einen abgeschlossenen Trade genau einer Ursache zu (Reihenfolge = Vorrang).

    * Gebührenproblem: brutto im Plus, netto nicht.
    * Ausführungsfehler: Ausführung > 0,5 % ungünstiger als der Entscheidungskurs.
    * Prognosefehler: Verlust größer als die schlechtesten 10 % der historischen Verluste
      (``ref_loss_p10``, z. B. −0,15) – die Strategie verhält sich anders als getestet.
    * sonst: normaler statistischer Verlust (im Rahmen dessen, was die Tests erwarten lassen).
    """
    if net > 0:
        return "Gewinn"
    if gross > 0:
        return "Gebührenproblem"
    if slippage is not None and slippage < -0.005:
        return "Ausführungsfehler"
    if ref_loss_p10 is not None and loss_pct is not None and loss_pct < ref_loss_p10:
        return "Prognosefehler"
    return "normaler statistischer Verlust"


HYPOTHESES = {
    "Gebührenproblem": "Weniger, größere Bewegungen handeln oder konsequent Maker-Orders nutzen.",
    "Ausführungsfehler": "Ordertyp/Zeitpunkt prüfen (Maker-Limit, kein Warten bis zum Fallback).",
    "Prognosefehler": "Regel verhält sich anders als im Test: Regime/Datenbasis prüfen.",
}


def hypotheses(classes: list[str]) -> list[str]:
    """Verbesserungsideen aus gehäuften Ursachen – nie automatisch aktiv. Prüfung nur auf
    getrennten Daten: Kerzen nach dem Audit (18.10.2026) oder nicht genutzte Coins (z. B. SOL)."""
    n = len(classes)
    out = []
    for cls, idea in HYPOTHESES.items():
        k = classes.count(cls)
        if k and (k >= 2 or n < 5):
            out.append(f"{cls} ({k}/{n}): {idea} Nur auf getrennten Daten testen.")
    return out
