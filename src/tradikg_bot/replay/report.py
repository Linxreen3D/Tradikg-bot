"""Markdown-Bericht des Historical Replay: Was wäre aus dem Startkapital geworden?

Der Bericht zeigt immer alle Zeiträume und die vollständige Verteilung – nicht nur
den besten Backtest. Die Belastbarkeits-Checkliste ist streng: Ein Ergebnis gilt
nur als belastbar, wenn **alle** Punkte erfüllt sind. Auch dann ist es keine
Renditezusage.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from tradikg_bot.costs import KRAKEN_SPOT_MAKER, KRAKEN_SPOT_TAKER
from tradikg_bot.replay.walkforward import (
    ReplayResult,
    ScenarioResult,
    distribution,
    weekly_stats,
)

PERIOD_LABEL = {
    "walk_forward": "Walk-Forward (Entwicklungszeitraum)",
    "holdout": "Holdout (unangetastet)",
    "window": "Rollierendes Fenster",
}


def eur(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "–"
    s = f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def pct(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "–"
    return f"{x * 100:+.{digits}f} %".replace(".", ",")


def _weeks(s: ScenarioResult | None) -> dict | None:
    return weekly_stats(s.equity["equity"], s.capital) if s and not s.equity.empty else None


def _share(w: dict | None) -> str:
    return f"{w['positive_share']:.0%}" if w and w["weeks"] else "–"


def _one(result: ReplayResult, **kw: Any) -> ScenarioResult | None:
    found = result.select(**kw)
    return found[0] if found else None


def checklist(result: ReplayResult, holdout_count: int, profile: str = "eur100") -> list[tuple]:
    """(Prüfpunkt, erfüllt?, Detail). ``None`` = nicht prüfbar (zählt als nicht erfüllt)."""
    hold = _one(result, period="holdout", variant="bot", profile=profile)
    hold_bh = _one(result, period="holdout", variant="buy_hold", profile=profile)
    wf = _one(result, period="walk_forward", variant="bot", profile=profile)
    windows = result.select(period="window", variant="bot", profile=profile)
    window_bh = {
        s.start: s for s in result.select(period="window", variant="buy_hold", profile=profile)
    }
    dist = distribution([w.final_equity for w in windows], windows[0].capital) if windows else {}
    trades = (hold.n_trades if hold else 0) + (wf.n_trades if wf else 0)

    def variant_ok(key: str) -> tuple[bool | None, str]:
        rows = [s for s in result.select(variant=key, profile=profile) if s.period != "window"]
        if not rows:
            return None, "nicht gerechnet"
        ok = all(s.net_profit > 0 for s in rows)
        return ok, ", ".join(f"{PERIOD_LABEL[s.period]}: {eur(s.final_equity)}" for s in rows)

    def beats(key: str) -> tuple[bool | None, str]:
        pairs = []
        for period in ("walk_forward", "holdout"):
            bot = _one(result, period=period, variant="bot", profile=profile)
            base = _one(result, period=period, variant=key, profile=profile)
            if bot and base:
                pairs.append((bot.final_equity > base.final_equity, period, bot, base))
        if not pairs:
            return None, "nicht gerechnet"
        return all(p[0] for p in pairs), ", ".join(
            f"{PERIOD_LABEL[p[1]]}: {eur(p[2].final_equity)} vs. {eur(p[3].final_equity)}"
            for p in pairs
        )

    beat_windows = sum(
        1
        for w in windows
        if w.start in window_bh and w.final_equity > window_bh[w.start].final_equity
    )
    rows: list[tuple] = [
        (
            "Echte Marktdaten (nicht synthetisch)",
            not result.synthetic,
            result.data_source,
        ),
        (
            "Holdout erstmals ausgewertet (unangetastet)",
            None if not result.config.evaluate_holdout else holdout_count <= 1,
            "nicht ausgewertet"
            if not result.config.evaluate_holdout
            else f"Auswertung Nr. {holdout_count} für diese Daten",
        ),
        (
            "Holdout nach allen Kosten positiv",
            None if hold is None else hold.net_profit > 0,
            "–" if hold is None else f"{eur(hold.capital)} → {eur(hold.final_equity)}",
        ),
        (
            "Holdout besser als Buy & Hold",
            None if hold is None or hold_bh is None else hold.final_equity > hold_bh.final_equity,
            "–"
            if hold is None or hold_bh is None
            else f"{eur(hold.final_equity)} vs. {eur(hold_bh.final_equity)}",
        ),
        (
            "Walk-Forward nach allen Kosten positiv",
            None if wf is None else wf.net_profit > 0,
            "–" if wf is None else f"{eur(wf.capital)} → {eur(wf.final_equity)}",
        ),
        (
            "Median der rollierenden Fenster über Startkapital",
            None if not dist else dist["median"] > windows[0].capital,
            "keine Fenster" if not dist else f"Median {eur(dist['median'])}",
        ),
        (
            "Höchstens 1/3 der Fenster mit Verlust",
            None if not dist else dist["share_loss"] <= 1 / 3,
            "keine Fenster" if not dist else f"{dist['share_loss']:.0%} der Fenster mit Verlust",
        ),
        (
            "Mehrheit der Fenster besser als Buy & Hold",
            None if not windows else beat_windows > len(windows) / 2,
            f"{beat_windows} von {len(windows)} Fenstern",
        ),
        ("Mindestens 30 Trades (Walk-Forward + Holdout)", trades >= 30, f"{trades} Trades"),
        ("Positiv bei doppelten Gebühren", *variant_ok("fees_x2")),
        ("Positiv bei schlechterer Ausführung", *variant_ok("worse_execution")),
        ("Positiv bei ungewöhnlicher Volatilität", *variant_ok("volatility_stress")),
        ("Besser als Zufallseinstiege", *beats("random")),
        ("Besser als Strategiesignale ohne EV-Filter", *beats("no_filter")),
    ]
    return rows


def render_replay_report(result: ReplayResult, holdout_count: int = 0) -> str:
    cfg, plan = result.config, result.plan
    idx = plan.index
    lines = ["# Historical Replay – Walk-Forward-Bericht", ""]
    if result.synthetic:
        lines += [
            "> **Synthetische Daten.** Dieser Lauf prüft nur die Pipeline. Die Zahlen sagen "
            "nichts über echte Märkte aus.",
            "",
        ]
    lines += [
        f"- Datenquelle: `{result.data_source}` · Zeitraster `{cfg.timeframe}` · "
        f"Assets: {', '.join(result.symbols)}",
        f"- Zeitraum: {idx[0].date()} – {idx[-1].date()} ({len(idx)} Kerzen)",
        "- Ausführung: dieselbe Paper-Engine wie im Dauerbetrieb (Signale, Netto-EV-Filter, "
        f"Risiko 1 % je Trade, Mindestordergrößen, Gebühren {KRAKEN_SPOT_TAKER:.2%} Taker/"
        f"{KRAKEN_SPOT_MAKER:.2%} Maker (Kraken-Stufe 1), Spread 10 bps, "
        "Slippage, SL/TP je Kerze, Ledger 33/33/34 nach Verlustaufholung). **Keine echten "
        "Orders.**",
        f"- Handelsregeln (Mindestordergrößen, Tick, Dezimalen): {result.rules_note or '–'}. "
        "Heutige Mindestgrößen werden auf historische Preise angewendet.",
        "- SL und TP in derselben Kerze: konservativ „Stop zuerst“; „TP zuerst“ nur als "
        "gekennzeichnete Obergrenze. Keine erfundene Tick-Reihenfolge.",
        "",
        "## Zeitliche Trennung",
        "",
        "Für jeden Abschnitt wird die Research (darin Train 60 % / Validation 20 % / Test 20 %) "
        "nur auf den Kerzen **vor** dem Abschnitt ausgeführt. Bewertet wird nur danach.",
        "",
        "| Abschnitt | Research-/Trainingsdaten | Bewertung | Kerzen |",
        "|---|---|---|---|",
    ]
    for row in plan.describe():
        lines.append(
            f"| {row['abschnitt']} | {row['research_daten']} | {row['bewertung']} | "
            f"{row['kerzen']} |"
        )
    if not cfg.evaluate_holdout:
        lines += ["", "_Holdout in diesem Lauf bewusst **nicht** ausgewertet._"]
    elif holdout_count > 1:
        lines += [
            "",
            f"> **Warnung:** Der Holdout dieser Daten wurde bereits {holdout_count}-mal "
            "ausgewertet. Wurde danach etwas am Bot geändert, ist er nicht mehr unangetastet.",
        ]

    # -- Was wäre aus 100 € geworden? ---------------------------------------------------------
    lines += ["", "## Was wäre aus 100 € geworden?", ""]
    lines += [
        "| Zeitraum | Von – bis | Bot | Buy & Hold | Cash | Trades | Max. Drawdown Bot | "
        "Wochen im Plus (Bot / B&H) | Schlechteste Woche (Bot / B&H) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in [
        *result.select(period="walk_forward", variant="bot", profile="eur100"),
        *result.select(period="holdout", variant="bot", profile="eur100"),
        *result.select(period="window", variant="bot", profile="eur100"),
    ]:
        bh = next(
            (
                b
                for b in result.select(period=s.period, variant="buy_hold", profile="eur100")
                if b.start == s.start
            ),
            None,
        )
        w = _weeks(s)
        wb = _weeks(bh)
        lines.append(
            f"| {PERIOD_LABEL[s.period]} | {s.start.date()} – {s.end.date()} | "
            f"**{eur(s.final_equity)}** ({pct(s.return_pct)}) | "
            f"{eur(bh.final_equity) if bh else '–'} | {eur(s.capital)} | {s.n_trades} | "
            f"{pct(s.max_drawdown)} | "
            f"{_share(w)} von {w['weeks'] if w else 0} / {_share(wb)} | "
            f"{pct(w['worst_week']) if w else '–'} / {pct(wb['worst_week']) if wb else '–'} |"
        )

    # -- Verteilung -----------------------------------------------------------------------------
    lines += ["", "## Verteilung über alle Zeitfenster (inkl. negativer Ergebnisse)", ""]
    profiles = {p.key: p for p in cfg.profiles}
    lines += [
        "| Kapital | Fenster | Min | 10 % | Median | 90 % | Max | Fenster mit Verlust | "
        "Median Buy & Hold |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key in cfg.window_profiles:
        bot = [s.final_equity for s in result.select(period="window", variant="bot", profile=key)]
        bh = [
            s.final_equity for s in result.select(period="window", variant="buy_hold", profile=key)
        ]
        if not bot:
            continue
        d, db = distribution(bot, profiles[key].capital), distribution(bh, profiles[key].capital)
        lines.append(
            f"| {profiles[key].label} | {int(d['n'])} | {eur(d['min'])} | {eur(d['p10'])} | "
            f"{eur(d['median'])} | {eur(d['p90'])} | {eur(d['max'])} | {d['share_loss']:.0%} | "
            f"{eur(db.get('median'))} |"
        )
    if not plan.windows:
        lines.append("| – | 0 | Zeitraum zu kurz für rollierende Fenster | | | | | | |")

    # -- Kapitalszenarien und Ledger ------------------------------------------------------------
    lines += ["", "## Kapitalszenarien und Gewinnverteilung (33/33/34)", ""]
    lines += [
        "| Zeitraum | Kapital | Endwert | Netto | Max. DD | Trades | Trefferquote | Gebühren | "
        "Trading | Auszahlungsreserve | Langfrist | offene Verluste | Buy & Hold | Cash |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for period in ("walk_forward", "holdout"):
        for s in result.select(period=period, variant="bot"):
            bh = _one(result, period=period, variant="buy_hold", profile=s.profile)
            led = s.ledger
            lines.append(
                f"| {PERIOD_LABEL[period]} | {s.profile_label} | {eur(s.final_equity)} | "
                f"{eur(s.net_profit)} | {pct(s.max_drawdown)} | {s.n_trades} | "
                f"{'–' if s.win_rate is None else f'{s.win_rate:.0%}'} | {eur(s.fees_eur)} | "
                f"{eur(led.get('trading_balance'))} | {eur(led.get('payout_reserve'))} | "
                f"{eur(led.get('longterm_portfolio'))} | {eur(led.get('unrecovered_losses'))} | "
                f"{eur(bh.final_equity) if bh else '–'}{f' ({bh.note})' if bh and bh.note else ''}"
                f" | {eur(s.capital)} |"
            )
    lines += [
        "",
        "Die Aufteilung betrifft nur realisierte Gewinne geschlossener Trades: Zuerst werden "
        "Verluste aufgeholt, erst danach 33 % Trading, 33 % Auszahlungsreserve, 34 % Langfrist. "
        "Offene Positionen am Ende sind zum letzten Schlusskurs abzüglich Verkaufsgebühr "
        "bewertet.",
    ]

    # -- Robustheit und Baselines ---------------------------------------------------------------
    lines += ["", "## Robustheit und Vergleich mit einfachen Baselines (100 €)", ""]
    lines += [
        "| Variante | Walk-Forward | Holdout | Trades (WF/Holdout) |",
        "|---|---|---|---|",
    ]
    keys = ["bot", *[v for v in cfg.variants if v != "bot"], "buy_hold", "buy_hold_first", "cash"]
    for key in keys:
        wf = _one(result, period="walk_forward", variant=key, profile=cfg.variant_profile)
        ho = _one(result, period="holdout", variant=key, profile=cfg.variant_profile)
        ref = wf or ho
        if ref is None:
            continue
        label = ref.label + (" ⚠️ Obergrenze" if ref.upper_bound else "")
        lines.append(
            f"| {label} | {eur(wf.final_equity) if wf else '–'} | "
            f"{eur(ho.final_equity) if ho else '–'} | "
            f"{wf.n_trades if wf else '–'}/{ho.n_trades if ho else '–'} |"
        )
    amb = sum(s.ambiguous_exits for s in result.select(variant="bot") if s.period != "window")
    lines += [
        "",
        f"SL und TP lagen bei {amb} Bot-Exits (alle Kapitalszenarien) in derselben Kerze. Diese "
        "sind als „Reihenfolge unbekannt“ markiert und konservativ als Stop gewertet. Der "
        "Abstand zur Zeile „Obergrenze“ zeigt, wie stark das Ergebnis von dieser Unsicherheit "
        "abhängt.",
    ]

    # -- Strategieanalyse --------------------------------------------------------------------------
    lines += ["", "## Strategieanalyse (Bot, 100 €, Walk-Forward + Holdout)", ""]
    per: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in result.select(variant="bot", profile="eur100"):
        if s.period != "window":
            for t in s.trades:
                per[str(t.get("strategy"))].append(t)
    if per:
        lines += [
            "| Strategie | Trades | Trefferquote | Netto | Ø je Trade | Gebühren "
            "| SL+TP in einer Kerze |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, trades in sorted(per.items(), key=lambda kv: -len(kv[1])):
            net = sum(t.get("net_pnl_eur", 0.0) for t in trades)
            lines.append(
                f"| {name} | {len(trades)} | "
                f"{sum(1 for t in trades if t.get('net_pnl_eur', 0) > 0) / len(trades):.0%} | "
                f"{eur(net)} | {eur(net / len(trades), 3)} | "
                f"{eur(sum(t.get('fees_eur', 0.0) for t in trades))} | "
                f"{sum(1 for t in trades if t.get('intrabar_ambiguous'))} |"
            )
    else:
        lines.append(
            "Keine Trades. Ohne belastbare Out-of-Sample-Belege handelt der Bot nicht – das ist "
            "ein gültiges Ergebnis."
        )

    # -- Warum (nicht) gehandelt wurde -----------------------------------------------------------
    lines += ["", "## Häufigste Ablehnungsgründe", ""]
    for key in ("eur10", "eur10_pilot", "eur100"):
        s = _one(result, period="walk_forward", variant="bot", profile=key)
        if s and s.rejections:
            lines.append(f"**{s.profile_label}:**")
            lines += [f"- {reason} ({n}×)" for reason, n in list(s.rejections.items())[:5]]
            lines.append("")

    # -- Research je Entscheidungszeitpunkt ----------------------------------------------------
    lines += ["## Research je Entscheidungszeitpunkt", ""]
    lines += ["| Entscheidung ab | Urteile | bestätigt |", "|---|---|---|"]
    for r in result.research:
        verdicts = ", ".join(f"{k}: {v}" for k, v in sorted(r["verdicts"].items())) or "–"
        lines.append(
            f"| {r['cutoff_ts'][:10]} | {verdicts} | {', '.join(r['confirmed']) or 'keine'} |"
        )

    # -- Belastbarkeit ---------------------------------------------------------------------------
    rows = checklist(result, holdout_count)
    passed = all(ok is True for _, ok, _ in rows)
    lines += ["", "## Wie belastbar ist das Ergebnis?", ""]
    lines += ["| Prüfpunkt | Ergebnis | Detail |", "|---|---|---|"]
    for name, ok, detail in rows:
        mark = "✅" if ok is True else ("❌" if ok is False else "–")
        lines.append(f"| {name} | {mark} | {detail} |")
    lines += [
        "",
        "**Gesamturteil: "
        + (
            "belastbar für diese Daten und Zeiträume"
            if passed
            else "nicht belastbar (mindestens ein Prüfpunkt nicht erfüllt)"
        )
        + ".**",
        "",
        "Historische Ergebnisse sind keine Vorhersage. Auch ein belastbares Ergebnis ist keine "
        "Renditezusage und keine Freigabe für Echtgeld.",
    ]
    return "\n".join(lines) + "\n"
