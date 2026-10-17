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
from dataclasses import dataclass, field

import pandas as pd

from tradikg_bot.replay.walkforward import (
    VARIANTS,
    CutoffResearch,
    HistoricalReplay,
    ReplayPlan,
    ScenarioResult,
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
    decisions = "".join(
        f"<tr><td>{r.cutoff_ts.date()}</td><td>{len(r.results)}</td>"
        f"<td>{html.escape(', '.join(r.summary()['confirmed']) or 'keine')}</td>"
        f"<td>{html.escape(', '.join(f'{k}: {v}' for k, v in sorted(r.summary()['verdicts'].items())))}</td></tr>"
        for r in sim.research
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
circle.win{{fill:var(--pos)}} circle.loss{{fill:var(--neg)}}
.tbl{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}}
.legend span{{margin-right:16px}} .note{{border-left:3px solid var(--mut);padding:4px 12px;color:var(--mut)}}
</style></head><body>
<h1>Schnelldurchlauf: Bot ab {sim.start.date()} eingesetzt</h1>
<div class="mut">Zeitraum {sim.start.date()} – {sim.end.date()} · Kerzen {html.escape(sim.timeframe)} ·
Daten {html.escape(sim.data_source)} · virtuelles Geld, keine echten Orders</div>
<div class="cards">{cards}</div>
<div class="legend"><span style="color:var(--bot)">━ Bot (100 €)</span>
<span style="color:var(--bh)">┅ Buy &amp; Hold</span><span class="pos">● Gewinn-Trade</span>
<span class="neg">● Verlust-Trade</span></div>
{chart}
<h2>Jede Entscheidung des Bots ({html.escape(main.profile_label)})</h2>
<div class="tbl"><table><tr><th>Einstieg</th><th>Ausstieg</th><th>Asset</th><th>Strategie</th>
<th>Einstiegskurs</th><th>Ausstiegskurs</th><th>Grund</th><th>Netto</th></tr>{trades}</table></div>
<h2>Was der Bot an jedem Prüfzeitpunkt wusste</h2>
<div class="mut">Research nur mit Kerzen vor dem Datum – die Zukunft war unbekannt.</div>
<div class="tbl"><table><tr><th>Datum</th><th>geprüft</th><th>bestätigt</th><th>Urteile</th></tr>{decisions}</table></div>
<h2>Warum Signale verworfen wurden</h2><ul>{reasons or "<li>–</li>"}</ul>
<p class="note">Simulation auf vergangenen Kerzen mit modellierten Gebühren (0,80 %/0,40 %), Spread und
Slippage. Vergangene Ergebnisse sind keine Vorhersage und keine Anlageberatung. Echtgeld bleibt
gesperrt (NOT LIVE READY).</p>
</body></html>"""
