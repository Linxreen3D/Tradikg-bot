"""Strategy-Health: Signale, Ablehnungen, echte Kosten, Kapital, Performance gegen den Test.

Quellen (nur lesend): Protokoll und Snapshot des Echtgeld-Bots (``~/.tradikg/live``), Paper-DB,
Kerzen in ``market.sqlite``. Die Vergleichswerte stammen aus einem Schnelldurchlauf derselben
Regel über 2016–heute.
"""

from __future__ import annotations

import json
import re

import pandas as pd
import streamlit as st

from tradikg_bot.dashboard import queries as q
from tradikg_bot.platform_config import PlatformConfig
from tradikg_bot.trading.analysis import classify_trade, hypotheses

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC  (?:\[([A-ZÄÖÜ]+)[^\]]*\] ?)?(.*)$")
TAG = re.compile(r"(\w+): Schluss ([\d.]+), SMA \d+ ([\d.]+) → (\w+)")
BUY = re.compile(r"(\w+) ([\d.]+) zu ([\d.]+) \(Gebühr ([\d.]+) €\)")
SELL = re.compile(r"(\w+) ([\d.]+) zu ([\d.]+) \((.+?)\), Ergebnis ≈ ([+-]?[\d.]+) €")
PROBLEMS = ("SKIP", "ABGELEHNT", "JETZT", "KILL", "UNKLAR", "ABGLEICH")
PLAN_FEE = 0.004  # Test: Maker-Limit zum Schlusskurs
# Audit 18.10.2026 (docs/STRATEGY_AUDIT.md) – Stand der Bewertung, kein Live-Wert
VERDICTS = [
    (
        "Trendfolge SMA 200",
        "weiterentwickeln (beobachten)",
        "+33,8 % p.a., Max. DD −37,6 %, 9/11 Jahre",
    ),
    ("Grid-Daytrading", "deaktiviert lassen", "seit 2023: 100 € → 84 €"),
    ("Daytrading (5 Strategien)", "deaktiviert lassen", "bei Kraken-Gebühren alle ≈ 0 €"),
    ("Signal-/Bracket-Bot", "deaktiviert lassen", "kein Vorteil nach Kosten"),
    (
        "Copy-Trading (Solana)",
        "unentschieden – nur Simulation",
        "realistische Verzögerung: 100 $ → 99,85 $",
    ),
]


def parse_log(lines: list[str]) -> pd.DataFrame:
    rows = []
    for line in lines:
        m = LINE.match(line)
        if m:
            rows.append({"ts": pd.Timestamp(m[1], tz="UTC"), "tag": m[2] or "", "text": m[3]})
    df = pd.DataFrame(rows, columns=["ts", "tag", "text"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)  # auch leer datetime (für .dt)
    return df


def fills(ev: pd.DataFrame) -> pd.DataFrame:
    """Käufe/Verkäufe mit Entscheidungskurs (Tagesschluss) und Abweichung davon."""
    close: dict[str, float] = {}
    rows = []
    for r in ev.itertuples():
        if r.tag == "TAG" and (m := TAG.search(r.text)):
            close[m[1]] = float(m[2])
        elif r.tag == "KAUF" and (m := BUY.search(r.text)):
            qty, px, fee = float(m[2]), float(m[3]), float(m[4])
            ref = close.get(m[1])
            rows.append(
                {
                    "Zeit": r.ts,
                    "Coin": m[1],
                    "Art": "Kauf",
                    "Kurs": px,
                    "Entscheidungskurs": ref,
                    "Abweichung": ref / px - 1 if ref else None,  # negativ = teurer gekauft
                    "Gebühr €": fee,
                    "Gebühr %": fee / (qty * px),
                }
            )
        elif r.tag == "VERKAUF" and (m := SELL.search(r.text)):
            px, ref = float(m[3]), close.get(m[1])
            rows.append(
                {
                    "Zeit": r.ts,
                    "Coin": m[1],
                    "Art": f"Verkauf ({m[4]})",
                    "Kurs": px,
                    "Entscheidungskurs": ref,
                    "Abweichung": px / ref - 1 if ref else None,
                    "Gebühr €": None,
                    "Gebühr %": None,
                    "Ergebnis €": float(m[5]),
                    "Wert €": float(m[2]) * px,
                }
            )
    return pd.DataFrame(rows)


@st.cache_data(ttl=6 * 3600, show_spinner="Vergleichswerte aus dem Schnelldurchlauf …")
def reference(market_db: str) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Equity der Regel (100 % investiert) 2016–heute und Nettoergebnis je Trade in %."""
    from tradikg_bot.storage import SQLiteStore
    from tradikg_bot.trading.allocation import TrendConfig, run_trend

    store = SQLiteStore(market_db)
    data = {s: store.load_bars(s, "1d") for s in ("XBTEUR", "ETHEUR")}
    if any(len(d) < 400 for d in data.values()):
        return pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=bool)
    eq, trades, _, alloc = run_trend(
        data, pd.Timestamp("2016-03-01", tz="UTC"), None, 100.0, lambda s, p: 5.0, TrendConfig()
    )
    pct = pd.Series([t["net_pnl_eur"] / (t["volume"] * t["entry_price"]) for t in trades])
    # Nur Zeiträume, in denen beide Coins gehalten wurden – so wie der Echtgeld-Bot gerade
    held = pd.Series(0, index=eq.index)
    spans = [(t["entry_ts"], t["exit_ts"]) for t in trades]
    spans += [(p.entry_ts, eq.index[-1]) for p in alloc.state.positions.values()]
    for a, b in spans:
        held[(held.index >= pd.Timestamp(a)) & (held.index < pd.Timestamp(b))] += 1
    return eq, pct, held >= 2


def render(cfg: PlatformConfig) -> None:
    st.caption(
        "Wie gesund ist jede Strategie? Echte Ausführung gegen Test-Annahmen, Ablehnungen mit "
        "Grund, Kapital und Performance im Vergleich zur Historie. Nur lesend."
    )
    root = cfg.home / "live"
    lines = (root / "live.log").read_text().splitlines() if (root / "live.log").exists() else []
    ev = parse_log(lines)
    snap = (
        json.loads((root / "snapshot.json").read_text())
        if (root / "snapshot.json").exists()
        else {}
    )
    eq_ref, trade_ref, both_held = reference(str(cfg.market_db))

    st.subheader("💶 Echtgeld-Trendfolge")
    if snap:
        bal, prices = snap.get("balances", {}), snap.get("prices", {})
        eur = bal.get("ZEUR", {}).get("total", 0.0)
        syms = snap.get("symbols", {})
        invested = sum(s.get("units", 0) * (prices.get(k) or 0) for k, s in syms.items())
        cost = sum(s.get("units", 0) * s.get("entry_price", 0) for s in syms.values())
        c = st.columns(4)
        c[0].metric("Investiert", f"{invested:.2f} €", f"{invested - cost:+.2f} € seit Kauf")
        c[1].metric("Frei (Euro)", f"{eur:.2f} €")
        c[2].metric("Obergrenze (Budget)", f"{snap.get('budget_eur', 0):.2f} €")
        c[3].metric("Auslastung", f"{invested / max(snap.get('budget_eur', 1), 1e-9):.0%}")
    else:
        st.info("Noch kein Snapshot des Echtgeld-Bots.")

    f = fills(ev)
    st.markdown("**Ausführung gegen Plan** (Test: Maker-Limit zum Tagesschluss, 0,40 % Gebühr)")
    if f.empty:
        st.write("Noch keine Ausführungen.")
    else:
        show = f.copy()
        show["Zeit"] = show["Zeit"].dt.tz_convert("Europe/Vienna").dt.strftime("%d.%m. %H:%M")
        for col in ("Abweichung", "Gebühr %"):
            show[col] = show[col].map(lambda v: "–" if pd.isna(v) else f"{v:+.2%}")
        st.dataframe(show, hide_index=True, width="stretch")
        buys = f[f["Art"] == "Kauf"]
        if not buys.empty:
            st.caption(
                f"Gebühren tatsächlich Ø {buys['Gebühr %'].mean():.2%} statt geplant "
                f"{PLAN_FEE:.2%}; gekauft im Schnitt {-buys['Abweichung'].mean():.2%} "
                "über dem Entscheidungskurs (Tagesschluss, zu dem der Test kauft)."
            )

    st.markdown("**Performance gegen den Test**")
    first = f[f["Art"] == "Kauf"]["Zeit"].min() if not f.empty else None
    if snap and first is not None and len(eq_ref) and cost:
        days = max(int((pd.Timestamp.now(tz="UTC") - first).days), 1)
        ret = invested / cost - 1
        dist = (eq_ref.shift(-days) / eq_ref - 1)[both_held].dropna()
        rank = float((dist < ret).mean())
        st.write(
            f"Seit dem ersten Kauf ({days} Tag(e)): **{ret:+.2%}** ohne Kaufgebühren. "
            f"Im Test (Zeiten mit BTC und ETH im Bestand) lagen {days}-Tages-Ergebnisse "
            f"zwischen {dist.quantile(0.05):+.1%} (5 %) und "
            f"{dist.quantile(0.95):+.1%} (95 %); der aktuelle Wert liegt beim {rank:.0%}-Perzentil."
        )
        if rank < 0.05:
            st.warning("Schlechter als 95 % der historischen Zeiträume – Regel prüfen.")
    else:
        st.write("Noch zu früh für einen Vergleich.")

    c = st.columns(2)
    with c[0]:
        st.markdown("**Letzte Signale**")
        sig = ev[ev["tag"] == "TAG"].tail(10)
        st.dataframe(
            sig.assign(ts=sig["ts"].dt.tz_convert("Europe/Vienna").dt.strftime("%d.%m. %H:%M"))[
                ["ts", "text"]
            ].iloc[::-1],
            hide_index=True,
            width="stretch",
        )
    with c[1]:
        st.markdown("**Abgelehnt / übersprungen / Abbruch** (mit Grund)")
        bad = ev[ev["tag"].isin(PROBLEMS) | ev["text"].str.startswith(("ABBRUCH", "Fehler"))]
        if bad.empty:
            st.write("Keine.")
        else:
            st.dataframe(
                bad.assign(ts=bad["ts"].dt.tz_convert("Europe/Vienna").dt.strftime("%d.%m. %H:%M"))[
                    ["ts", "tag", "text"]
                ].iloc[::-1],
                hide_index=True,
                width="stretch",
            )

    st.subheader("🔬 Nachanalyse abgeschlossener Trades")
    p10 = float(trade_ref[trade_ref < 0].quantile(0.10)) if (trade_ref < 0).any() else None
    rows = []
    sells = f[f["Art"].str.startswith("Verkauf")] if not f.empty else f
    for _, r in sells.iterrows():
        net, value = r["Ergebnis €"], r["Wert €"]
        gross = net + value * (PLAN_FEE + 0.008)  # Gebühren geschätzt (Maker rein, Taker raus)
        rows.append(
            {
                "Quelle": "Echtgeld",
                "Coin": r["Coin"],
                "Ergebnis €": net,
                "Ursache": classify_trade(gross, net, r["Abweichung"], p10, net / value),
            }
        )
    paper = q._df(q.paper_db(cfg), "SELECT payload FROM trade_analysis")
    for p in paper["payload"] if not paper.empty else []:
        t = json.loads(p)
        value = t.get("volume", 0) * t.get("entry_price", 0) or 1.0
        rows.append(
            {
                "Quelle": f"Paper ({t.get('strategy', '?')})",
                "Coin": t.get("asset"),
                "Ergebnis €": t["net_pnl_eur"],
                "Ursache": classify_trade(
                    t["gross_pnl_eur"],
                    t["net_pnl_eur"],
                    None,
                    p10 if t.get("strategy") == "trend_sma200" else None,
                    t["net_pnl_eur"] / value,
                ),
            }
        )
    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True, width="stretch")
        for h in hypotheses(list(df["Ursache"])):
            st.write(f"💡 {h}")
    else:
        st.write("Noch keine abgeschlossenen Trades – kein Trade ist ein gültiges Ergebnis.")
    if p10 is not None:
        st.caption(
            f"„Prognosefehler“ = Verlust schlechter als {p10:.1%} (schlechteste 10 % der Verluste "
            "im Test 2016–heute). Hypothesen werden nie automatisch aktiviert."
        )

    st.subheader("🧪 Paper-Bot: Signale und Ablehnungen")
    sig = q.recent_signals(cfg, 500)
    if sig.empty:
        st.write("Noch keine Signale im Paper-Bot.")
    else:
        st.dataframe(sig.head(15), hide_index=True, width="stretch")
        rej = sig[sig["status"] == "rejected"]
        if not rej.empty:
            st.dataframe(
                rej.groupby("reason").size().rename("Anzahl").reset_index(),
                hide_index=True,
                width="stretch",
            )

    st.subheader("📋 Strategien: Bewertung aus dem Audit")
    st.dataframe(
        pd.DataFrame(VERDICTS, columns=["Strategie", "Empfehlung", "Beleg"]),
        hide_index=True,
        width="stretch",
    )
