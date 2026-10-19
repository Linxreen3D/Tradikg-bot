# ruff: noqa: E501  (HTML/CSS-Vorlage)
"""HTML-Bericht des Copy-Trading-Simulators (Aufbau angelehnt an übliche Copy-Bot-Dashboards)."""

from __future__ import annotations

import html

from tradikg_bot.copytrade.simulate import CopyResult
from tradikg_bot.replay.simulate import _svg


def _usd(x: float) -> str:
    return f"{x:,.2f} $"


def _pct(x: float) -> str:
    return f"{x * 100:+.1f} %"


def render_copy_html(res: CopyResult, cov: dict, sensitivity: list[tuple[str, float]]) -> str:
    cfg, eq = res.cfg, res.equity
    pnl = res.final - cfg.capital
    losses = 0
    for t in reversed(res.trades):
        if t["pnl_usd"] < 0:
            losses += 1
        else:
            break
    now = res.followed[-1]["wallet"] if res.followed and not res.followed[-1]["fired"] else "–"
    marks = [
        {
            "ts": t["exit_ts"],
            "value": float(eq[eq.index <= t["exit_ts"]].iloc[-1])
            if (eq.index <= t["exit_ts"]).any()
            else cfg.capital,
            "pnl": t["pnl_usd"],
            "title": f"{t['token'][:8]}… {_usd(t['pnl_usd'])} ({t['reason']})",
        }
        for t in res.trades
    ]
    chart = _svg([("bot", "Bot", eq)], cfg.capital, marks) if len(eq) > 1 else ""
    wins = sum(t["pnl_usd"] > 0 for t in res.trades)
    who = (
        "".join(
            f"<tr><td>{'🔥 ' if f['fired'] else ''}{html.escape(f['wallet'][:10])}…</td><td>{f['since']:%d.%m. %H:%M}</td>"
            f"<td>{_pct(f['roi'])}</td><td>{f['win_rate']:.0%}</td><td>{f['rounds']}</td><td>{f['copied']}</td>"
            f"<td class='{'pos' if f['pnl_usd'] > 0 else 'neg' if f['pnl_usd'] < 0 else ''}'>{_usd(f['pnl_usd'])}</td>"
            f"<td>{'gefeuert' if f['fired'] else 'folgen' if f['wallet'] == now else 'beendet'}</td></tr>"
            for f in res.followed
        )
        or "<tr><td colspan='8'>Kein Trader erfüllte die Kriterien.</td></tr>"
    )
    feed = "".join(
        f"<tr><td>{e['ts']:%d.%m. %H:%M:%S}</td><td>{html.escape(e['text'])}</td>"
        f"<td class='{'pos' if (e['pnl'] or 0) > 0 else 'neg' if (e['pnl'] or 0) < 0 else ''}'>{'' if e['pnl'] is None else _usd(e['pnl'])}</td></tr>"
        for e in list(reversed(res.feed))[:60]
    )
    sens = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{_usd(v)}</td><td>{_pct(v / cfg.capital - 1)}</td></tr>"
        for k, v in sensitivity
    )
    hours = (res.end - res.start).total_seconds() / 3600 if res.start is not None else 0
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Copy-Trading-Simulator</title>
<style>
:root{{--bg:#0f1020;--fg:#eef;--mut:#9aa;--line:#2a2c48;--bot:#7aa2ff;--bh:#666;--pos:#4ade80;--neg:#f87171}}
@media (prefers-color-scheme:light){{:root{{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--bot:#2563eb;--pos:#15803d;--neg:#b91c1c}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif;margin:0 auto;padding:16px;max-width:1000px}}
h1{{font-size:22px;margin:0}} h2{{font-size:16px;margin:24px 0 8px}} .mut{{color:var(--mut)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:14px 0}}
.card{{border:1px solid var(--line);border-radius:10px;padding:10px}} .k{{color:var(--mut);font-size:12px;text-transform:uppercase}} .v{{font-size:20px}}
.pos{{color:var(--pos)}} .neg{{color:var(--neg)}}
svg{{width:100%;height:auto;border:1px solid var(--line);border-radius:10px}} polyline{{fill:none;stroke-width:2}} .bot{{stroke:var(--bot)}}
.base{{stroke:var(--mut);stroke-dasharray:2 4}} .axis{{fill:var(--mut);font-size:12px}} circle.win{{fill:var(--pos)}} circle.loss{{fill:var(--neg)}}
.tbl{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px}} td,th{{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;white-space:nowrap}}
.note{{border-left:3px solid var(--mut);padding:4px 12px;color:var(--mut)}}
</style></head><body>
<h1>Copy-Trading-Simulator <span class="mut">· nur virtuell</span></h1>
<div class="mut">Daten {html.escape(str(cov.get("from")))} – {html.escape(str(cov.get("to")))} UTC · {cov.get("pools")} Pools · {cov.get("wallets")} Wallets ·
bewertet {hours:.1f} h nach {cfg.lookback_h:g} h Rückblick · Verzögerung {cfg.delay_s:g} s</div>
<div class="cards">
<div class="card"><div class="k">Guthaben</div><div class="v">{_usd(res.final)}</div><div class="mut">Start {_usd(cfg.capital)}</div></div>
<div class="card"><div class="k">Gesamt P&amp;L</div><div class="v {"pos" if pnl > 0 else "neg"}">{_usd(pnl)}</div><div class="mut">{_pct(pnl / cfg.capital)}</div></div>
<div class="card"><div class="k">Trades kopiert</div><div class="v">{len(res.trades)}</div><div class="mut">{wins} Gewinne · {len(res.trades) - wins} Verluste</div></div>
<div class="card"><div class="k">Folge jetzt</div><div class="v">{html.escape(now[:10])}{"…" if now != "–" else ""}</div><div class="mut">Feuer-Regel: {cfg.fire_after} Verluste in Folge · aktuell {losses}</div></div>
</div>
{chart}
<h2>Wem wir gefolgt sind</h2>
<div class="mut">Auswahl nur aus Trades vor dem jeweiligen Zeitpunkt (ROI/Trefferquote der abgeschlossenen Runden im Rückblick).</div>
<div class="tbl"><table><tr><th>Wallet</th><th>seit</th><th>ROI vorher</th><th>Treffer</th><th>Runden</th><th>kopiert</th><th>unser Ergebnis</th><th>Status</th></tr>{who}</table></div>
<h2>Was Verzögerung und Kosten ausmachen</h2>
<div class="tbl"><table><tr><th>Variante</th><th>Endwert</th><th>Ergebnis</th></tr>{sens}</table></div>
<h2>Copy-Feed (neueste zuerst)</h2>
<div class="tbl"><table><tr><th>Zeit (UTC)</th><th>Ereignis</th><th>Ergebnis</th></tr>{feed}</table></div>
<p class="note">Simulation mit öffentlichen GeckoTerminal-Trades. Kosten: DEX {cfg.dex_fee:.2%}, Copy-Bot {cfg.app_fee:.0%} je Seite,
Netz {cfg.network_usd:.2f} $ je Transaktion, Preiseinfluss ≈ Ordergröße / (Pool-Reserve/2). Kauf zum ersten echten Preis
<b>nach</b> der Verzögerung. Erfasste Abrufe vollständig (Überlappung): {cov.get("complete_share", 0):.0%} – bei sehr aktiven Pools fehlen
Trades, die Auswahl ist dann ungenauer. Kurze Zeiträume sind Zufall; erst Tage bis Wochen ergeben ein Bild.
Keine Wallet, keine Orders, keine Anlageberatung.</p>
</body></html>"""
