# ruff: noqa: E501  (HTML/CSS-Vorlage)
"""Schnelldurchlauf: Bot an einem Zeitpunkt der Vergangenheit „einsetzen“ und bis heute vorspulen.

Ablauf (dieselbe Paper-Engine wie im Dauerbetrieb, keine echten Orders):

1. Am Startdatum läuft die Research **nur auf Kerzen davor** – der Bot weiß nichts über die
   Zukunft. Sie wird alle ``research_every`` Kerzen wiederholt (wie regelmäßiges Neu-Prüfen
   im echten Betrieb), jeweils wieder nur mit bis dahin bekannten Kerzen.
2. Danach Kerze für Kerze: Signale, Netto-EV-Filter, Positionsgröße, Gebühren, Spread,
   Slippage, SL/TP – genau wie im Paper-Betrieb.
3. Ergebnis: Kapitalverlauf (100 € und 10-€-Pilot), jede Entscheidung, Vergleich mit Buy & Hold
   und Cash, Wochenbilanz – als HTML-Seite und Markdown.
"""

from __future__ import annotations

import html
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tradikg_bot.replay.walkforward import (
    VARIANTS,
    CutoffResearch,
    HistoricalReplay,
    ReplayPlan,
    ScenarioResult,
    max_drawdown,
    weekly_stats,
)


@dataclass
class SimulationResult:
    start: pd.Timestamp
    end: pd.Timestamp
    timeframe: str
    data_source: str
    bots: list[ScenarioResult]
    buy_hold: ScenarioResult
    research: list[CutoffResearch] = field(default_factory=list)
    note: str = ""
    overview: pd.DataFrame | None = None  # je Startmonat ein 1-Jahres-Zeitraum


def _utc(x: str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(x)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def simulate(
    replay: HistoricalReplay,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp | None = None,
    research_every: int = 90,
    profiles: tuple[str, ...] = ("eur100", "eur10_pilot"),
) -> SimulationResult:
    idx = replay.plan.index
    s = int(idx.searchsorted(_utc(start)))
    e = len(idx) if end is None else int(idx.searchsorted(_utc(end), "right"))
    if s < replay.cfg.min_history_bars:
        raise ValueError(
            f"Zu früh: vor {idx[min(s, len(idx) - 1)].date()} liegen nur {s} Kerzen, die Research "
            f"braucht mindestens {replay.cfg.min_history_bars}."
        )
    if e - s < 2:
        raise ValueError("Zeitraum zu kurz – Startdatum liegt am Ende der Daten.")
    # Eigener Zeitplan: Research am Start und alle research_every Kerzen, sonst keine Abschnitte
    replay.plan = ReplayPlan(idx, tuple(range(s, e, max(1, research_every))), len(idx), ())
    replay.research.plan = replay.plan
    bot = VARIANTS["bot"]
    bots = []
    for key in profiles:
        replay.progress(f"Schnelldurchlauf {replay.cfg.profile(key).label}")
        bots.append(
            replay.run_segment(f"sim-bot-{key}", "simulation", s, e, replay.cfg.profile(key), bot)
        )
    bh = replay.baseline(
        "sim-buy_hold", "simulation", s, e, replay.cfg.profile(profiles[0]), "buy_hold"
    )
    research = [replay.research.by_cutoff[c] for c in sorted(replay.research.by_cutoff)]
    return SimulationResult(
        idx[s], idx[e - 1], replay.cfg.timeframe, replay.data_source, bots, bh, research
    )


def simulate_trend(
    replay: HistoricalReplay,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp | None = None,
    profiles: tuple[tuple[str, str, float], ...] = (
        ("eur100", "100 €", 100.0),
        ("eur10", "10 € (Größe eines ersten Echtgeld-Tests)", 10.0),
    ),
    invest_fraction: float = 1.0,
    cash_yield_pa: float = 0.0,
) -> SimulationResult:
    """Schnelldurchlauf der Trendfolge-Allokation (feste 200-Tage-Regel, keine Research)."""
    from tradikg_bot.trading.allocation import TrendConfig, run_trend

    idx = replay.plan.index
    s = int(idx.searchsorted(_utc(start)))
    e = len(idx) if end is None else int(idx.searchsorted(_utc(end), "right"))
    cfg = TrendConfig(invest_fraction=invest_fraction, cash_yield_pa=cash_yield_pa)
    if s < cfg.sma or e - s < 2:
        raise ValueError(f"Zu früh oder zu kurz: vor dem Start braucht die Regel {cfg.sma} Kerzen.")
    rules = replay.rules

    def min_order(sym: str, px: float) -> float:
        return max(rules[sym].ordermin * px, rules[sym].costmin)

    bots = []
    for key, label, capital in profiles:
        replay.progress(f"Schnelldurchlauf Trendfolge {label}")
        eq, trades, log, alloc = run_trend(replay.data, idx[s], idx[e - 1], capital, min_order, cfg)
        last = {
            sym: float(df["close"][df.index <= idx[e - 1]].iloc[-1])
            for sym, df in replay.data.items()
        }
        exit_cost = sum(
            p.units * last[sym] * (cfg.taker_fee + cfg.spread_bps / 2 * 1e-4)
            for sym, p in alloc.state.positions.items()
        )
        rejections = Counter(x["reason"] for x in log if x["status"] == "rejected")
        bots.append(
            ScenarioResult(
                scenario_id=f"sim-trend-{key}",
                period="simulation",
                variant="trend",
                label="Trendfolge SMA 200",
                kind="bot",
                profile=key,
                profile_label=label,
                capital=capital,
                start=idx[s],
                end=idx[e - 1],
                equity=pd.DataFrame({"equity": eq}),
                trades=trades,
                final_equity=float(eq.iloc[-1]) - exit_cost,
                max_drawdown=max_drawdown(eq, capital),
                fees_eur=float(sum(t["fees_eur"] for t in trades)),
                open_positions_end=len(alloc.state.positions),
                rejections=dict(rejections.most_common(8)),
            )
        )
    bh = replay.baseline(
        "sim-buy_hold", "simulation", s, e, replay.cfg.profile("eur100"), "buy_hold"
    )
    note = (
        "Feste Regel, nicht an die Daten angepasst: Kauf, wenn der Tagesschluss über dem "
        "200-Tage-Durchschnitt liegt; Verkauf darunter; Notfall-Stop 20 % unter dem höchsten "
        "Schluss seit Einstieg. Aufträge als Maker-Limit (0,40 %), sonst Taker (0,80 %). Offene "
        "Positionen am Ende sind abzüglich Verkaufskosten bewertet. "
        f"Höchstens investiert: {invest_fraction:.0%} des Kapitals; Cash-Zins: {cash_yield_pa:.1%} p.a."
    )
    return SimulationResult(
        idx[s], idx[e - 1], replay.cfg.timeframe, replay.data_source, bots, bh, [], note
    )


def rolling_overview(
    replay: HistoricalReplay,
    first: str | pd.Timestamp = "2019-01-01",
    capital: float = 100.0,
    invest_fraction: float = 1.0,
    cash_yield_pa: float = 0.0,
    horizon_days: int = 365,
) -> pd.DataFrame:
    """Bot an jedem Monatsanfang neu mit ``capital`` einsetzen und je 1 Jahr laufen lassen."""
    from tradikg_bot.trading.allocation import TrendConfig, run_trend

    cfg = TrendConfig(invest_fraction=invest_fraction, cash_yield_pa=cash_yield_pa)
    rules, data, idx = replay.rules, replay.data, replay.plan.index
    last_start = idx[-1] - pd.Timedelta(days=horizon_days)
    rows = []
    for a in pd.date_range(_utc(first), last_start, freq="MS"):
        e = a + pd.Timedelta(days=horizon_days)
        eq, trades, _, _ = run_trend(
            data,
            a,
            e,
            capital,
            lambda sym, px: max(rules[sym].ordermin * px, rules[sym].costmin),
            cfg,
        )
        months = eq.resample("ME").last().pct_change().dropna()
        bh = [
            float(df["close"].asof(e) / df["open"].asof(a)) * (1 - cfg.taker_fee) ** 2
            for df in data.values()
            if df.index[0] <= a
        ]
        rows.append(
            {
                "start": a,
                "bot": float(eq.iloc[-1] / capital - 1),
                "buy_hold": float(np.mean(bh) - 1) if bh else float("nan"),
                "max_dd": float(max_drawdown(eq, capital)),
                "worst_month": float(months.min()),
                "months_plus": float((months > 1e-9).mean()),
                "trades": len(trades),
            }
        )
    return pd.DataFrame(rows)


def _overview_html(ov: pd.DataFrame, capital: float) -> str:
    if ov is None or ov.empty:
        return ""
    plus = (ov["bot"] > 0).mean()
    w, h, pad = 900, 220, 30
    lo, hi = min(ov["bot"].min(), 0), max(ov["bot"].max(), 0)
    bw = (w - 2 * pad) / len(ov)

    def y(v: float) -> float:
        return pad + (hi - v) / max(hi - lo, 1e-9) * (h - 2 * pad)

    bars = "".join(
        f'<rect x="{pad + i * bw:.1f}" y="{min(y(v), y(0)):.1f}" width="{max(bw - 1, 1):.1f}" '
        f'height="{abs(y(v) - y(0)):.1f}" class="{"win" if v > 0 else "loss"}"><title>'
        f"Start {r.start.date()}: {_pct(v)} (Buy &amp; Hold {_pct(r.buy_hold)})</title></rect>"
        for i, (v, r) in enumerate(zip(ov["bot"], ov.itertuples(), strict=True))
    )
    chart = (
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Ergebnis je Startmonat">'
        f'<line x1="{pad}" x2="{w - pad}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="base"/>{bars}'
        f'<text x="{pad}" y="{h - 8}" class="axis">{ov["start"].iloc[0].date()}</text>'
        f'<text x="{w - pad}" y="{h - 8}" class="axis" text-anchor="end">{ov["start"].iloc[-1].date()}</text></svg>'
    )
    worst = ov.loc[ov["bot"].idxmin()]
    cards = [
        ("Zeiträume im Plus", f"{plus:.0%} von {len(ov)}", "pos" if plus >= 0.7 else "neg"),
        ("Typisches Jahr (Median)", _pct(ov["bot"].median()), ""),
        ("Schlechtestes Jahr", f"{_pct(worst['bot'])} (Start {worst['start'].date()})", "neg"),
        ("Bestes Jahr", _pct(ov["bot"].max()), "pos"),
        (
            "Ø größter Rückgang",
            f"{_pct(ov['max_dd'].mean())} (schlimmster {_pct(ov['max_dd'].min())})",
            "",
        ),
        ("Schlechtester Monat", _pct(ov["worst_month"].min()), ""),
        ("Monate im Plus (Ø)", f"{ov['months_plus'].mean():.0%}", ""),
        (
            "Buy &amp; Hold im Plus",
            f"{(ov['buy_hold'] > 0).mean():.0%} · schlechtestes Jahr {_pct(ov['buy_hold'].min())}",
            "",
        ),
    ]
    cards_html = "".join(
        f"<div class='card'><div class='k'>{k}</div><div class='v {c}'>{v}</div></div>"
        for k, v, c in cards
    )
    return (
        f"<h2>Überblick: {len(ov)} Ein-Jahres-Zeiträume ({_eur(capital)} Start, jeden Monat neu)</h2>"
        f"<div class='mut'>Jeder Balken ist ein Jahr Bot ab einem Monatsanfang – grün = Gewinn, rot = Verlust "
        f"(Maus darüber zeigt Start und Ergebnis).</div><div class='cards'>{cards_html}</div>{chart}"
    )


# -- Darstellung ----------------------------------------------------------------------------------
def _eur(x: float) -> str:
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(x: float) -> str:
    return f"{x * 100:+.1f} %".replace(".", ",")


def summary_rows(sim: SimulationResult) -> list[dict]:
    rows = []
    for r in [*sim.bots, sim.buy_hold]:
        w = weekly_stats(r.equity["equity"], r.capital) if not r.equity.empty else {}
        rows.append(
            {
                "name": "Buy & Hold" if r is sim.buy_hold else f"Bot – {r.profile_label}",
                "start": r.capital,
                "end": r.final_equity,
                "ret": r.return_pct,
                "trades": r.n_trades,
                "win": r.win_rate,
                "dd": r.max_drawdown,
                "fees": r.fees_eur,
                "weeks": w.get("weeks", 0),
                "pos_weeks": w.get("positive_share", 0.0),
                "worst_week": w.get("worst_week", 0.0),
            }
        )
    return rows


def _svg(series: list[tuple[str, str, pd.Series]], capital: float, marks: list[dict]) -> str:
    """Einfaches Liniendiagramm (inline SVG, ohne Abhängigkeiten); Werte relativ zum Start."""
    w, h, pad = 900, 320, 48
    allv = pd.concat([s / s.iloc[0] * capital for _, _, s in series if len(s)])
    lo, hi = float(min(allv.min(), capital)), float(max(allv.max(), capital))
    t0 = min(s.index[0] for _, _, s in series if len(s))
    t1 = max(s.index[-1] for _, _, s in series if len(s))
    span = max((t1 - t0).total_seconds(), 1.0)

    def xy(ts: pd.Timestamp, v: float) -> tuple[float, float]:
        x = pad + (ts - t0).total_seconds() / span * (w - 2 * pad)
        y = h - pad - (v - lo) / max(hi - lo, 1e-9) * (h - 2 * pad)
        return round(x, 1), round(y, 1)

    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Kapitalverlauf">']
    _, ys = xy(t0, capital)
    parts.append(f'<line x1="{pad}" x2="{w - pad}" y1="{ys}" y2="{ys}" class="base"/>')
    for v in (lo, hi):
        _, y = xy(t0, v)
        parts.append(f'<text x="4" y="{y + 4}" class="axis">{html.escape(_eur(v))}</text>')
    parts.append(f'<text x="{pad}" y="{h - 14}" class="axis">{t0.date()}</text>')
    parts.append(
        f'<text x="{w - pad}" y="{h - 14}" class="axis" text-anchor="end">{t1.date()}</text>'
    )
    for cls, _, s in series:
        if len(s):
            scaled = s / s.iloc[0] * capital
            pts = " ".join(f"{x},{y}" for x, y in (xy(t, float(v)) for t, v in scaled.items()))
            parts.append(f'<polyline points="{pts}" class="{cls}"/>')
    for m in marks:
        x, y = xy(m["ts"], m["value"])
        cls = "win" if m["pnl"] > 0 else "loss"
        parts.append(
            f'<circle cx="{x}" cy="{y}" r="4" class="{cls}"><title>{html.escape(m["title"])}</title></circle>'
        )
    parts.append("</svg>")
    return "".join(parts)


def render_html(sim: SimulationResult) -> str:
    main = sim.bots[0]
    eq = main.equity["equity"]
    bh = sim.buy_hold.equity["equity"]
    marks = []
    for t in main.trades:
        ts = pd.Timestamp(t["exit_ts"])
        val = float(eq[eq.index <= ts].iloc[-1]) if (eq.index <= ts).any() else main.capital
        marks.append(
            {
                "ts": ts,
                "value": val,
                "pnl": t.get("net_pnl_eur", 0.0),
                "title": f"{t['asset']} {t['strategy']}: {_eur(t.get('net_pnl_eur', 0.0))}",
            }
        )
    chart = _svg(
        [
            ("bot", "Bot", pd.concat([pd.Series([main.capital], index=[sim.start]), eq])),
            ("bh", "Buy & Hold", pd.concat([pd.Series([main.capital], index=[sim.start]), bh])),
        ],
        main.capital,
        marks,
    )
    rows = summary_rows(sim)
    cards = "".join(
        f"<div class='card'><div class='k'>{html.escape(r['name'])}</div>"
        f"<div class='v'>{_eur(r['start'])} → <b>{_eur(r['end'])}</b></div>"
        f"<div class='{'pos' if r['ret'] > 0 else 'neg' if r['ret'] < 0 else ''}'>{_pct(r['ret'])}</div>"
        f"<div class='m'>{r['trades']} Trades · Max. Rückgang {_pct(r['dd'])} · "
        f"Wochen im Plus {r['pos_weeks']:.0%} von {r['weeks']} · schlechteste Woche "
        f"{_pct(r['worst_week'])}</div></div>"
        for r in rows
    )
    trades = (
        "".join(
            f"<tr><td>{pd.Timestamp(t['entry_ts']).date()}</td><td>{pd.Timestamp(t['exit_ts']).date()}</td>"
            f"<td>{html.escape(t['asset'])}</td><td>{html.escape(t['strategy'])}</td>"
            f"<td>{t['entry_price']:,.2f}</td><td>{t['exit_price']:,.2f}</td>"
            f"<td>{html.escape(str(t.get('exit_reason', '')))}</td>"
            f"<td class='{'pos' if t.get('net_pnl_eur', 0) > 0 else 'neg'}'>{_eur(t.get('net_pnl_eur', 0.0))}</td></tr>"
            for t in main.trades
        )
        or "<tr><td colspan='8'>Keine Trades – der Bot hat in diesem Zeitraum keinen Vorteil nach Kosten gesehen.</td></tr>"
    )
    decisions = (
        "<tr><td colspan='4'>Feste Regel – keine Research, kein Blick in die Zukunft.</td></tr>"
        if not sim.research
        else "".join(
            f"<tr><td>{r.cutoff_ts.date()}</td><td>{len(r.results)}</td>"
            f"<td>{html.escape(', '.join(r.summary()['confirmed']) or 'keine')}</td>"
            f"<td>{html.escape(', '.join(f'{k}: {v}' for k, v in sorted(r.summary()['verdicts'].items())))}</td></tr>"
            for r in sim.research
        )
    )
    reasons = "".join(
        f"<li>{html.escape(k)} ({v}×)</li>" for k, v in list(main.rejections.items())[:6]
    )
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradikg Schnelldurchlauf</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--bot:#2563eb;--bh:#9ca3af;--pos:#15803d;--neg:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#f2f2f2;--mut:#a1a1a6;--line:#333;--bot:#60a5fa;--bh:#6b7280;--pos:#4ade80;--neg:#f87171}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:16px;max-width:980px;margin-inline:auto}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px}} .mut,.m,.k{{color:var(--mut)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:16px 0}}
.card{{border:1px solid var(--line);border-radius:10px;padding:12px}} .v{{font-size:18px}} .m{{font-size:13px}}
.pos{{color:var(--pos)}} .neg{{color:var(--neg)}}
svg{{width:100%;height:auto;border:1px solid var(--line);border-radius:10px}}
polyline{{fill:none;stroke-width:2}} .bot{{stroke:var(--bot)}} .bh{{stroke:var(--bh);stroke-dasharray:5 4}}
.base{{stroke:var(--mut);stroke-dasharray:2 4}} .axis{{fill:var(--mut);font-size:12px}}
circle.win,rect.win{{fill:var(--pos)}} circle.loss,rect.loss{{fill:var(--neg)}}
.tbl{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}}
.legend span{{margin-right:16px}} .note{{border-left:3px solid var(--mut);padding:4px 12px;color:var(--mut)}}
</style></head><body>
<h1>Schnelldurchlauf: Bot ab {sim.start.date()} eingesetzt</h1>
<div class="mut">Zeitraum {sim.start.date()} – {sim.end.date()} · Kerzen {html.escape(sim.timeframe)} ·
Daten {html.escape(sim.data_source)} · virtuelles Geld, keine echten Orders</div>
<div class="cards">{cards}</div>
{_overview_html(sim.overview, main.capital)}
<h2>Verlauf ab {sim.start.date()}</h2>
<div class="legend"><span style="color:var(--bot)">━ Bot (100 €)</span>
<span style="color:var(--bh)">┅ Buy &amp; Hold</span><span class="pos">● Gewinn-Trade</span>
<span class="neg">● Verlust-Trade</span></div>
{chart}
<h2>Jede Entscheidung des Bots ({html.escape(main.profile_label)})</h2>
<div class="tbl"><table><tr><th>Einstieg</th><th>Ausstieg</th><th>Asset</th><th>Strategie</th>
<th>Einstiegskurs</th><th>Ausstiegskurs</th><th>Grund</th><th>Netto</th></tr>{trades}</table></div>
<p class="note">{html.escape(sim.note)}</p>
<h2>Was der Bot an jedem Prüfzeitpunkt wusste</h2>
<div class="mut">Research nur mit Kerzen vor dem Datum – die Zukunft war unbekannt.</div>
<div class="tbl"><table><tr><th>Datum</th><th>geprüft</th><th>bestätigt</th><th>Urteile</th></tr>{decisions}</table></div>
<h2>Warum Signale verworfen wurden</h2><ul>{reasons or "<li>–</li>"}</ul>
<p class="note">Simulation auf vergangenen Kerzen mit modellierten Gebühren (0,80 %/0,40 %), Spread und
Slippage. Vergangene Ergebnisse sind keine Vorhersage und keine Anlageberatung. Echtgeld bleibt
gesperrt (NOT LIVE READY).</p>
</body></html>"""
