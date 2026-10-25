"""Strategie-Audit (Punkte 1–3): Risiko, Robustheit, Kapital/Gebühren – Markdown auf stdout.

    .venv/bin/python scripts/strategy_audit.py > /tmp/audit.md

Nutzt nur lokale Kraken-Kerzen (~/.tradikg/data/market.sqlite) und öffentliche Kraken-Endpunkte
(AssetPairs, Depth). Keine Orders, kein API-Key.
"""

from __future__ import annotations

import math

import pandas as pd

from tradikg_bot.costs import KRAKEN_SPOT_MAKER, KRAKEN_SPOT_TAKER
from tradikg_bot.live.kraken.client import KrakenClient
from tradikg_bot.live.kraken.gateway import KrakenSpotGateway
from tradikg_bot.platform_config import PlatformConfig
from tradikg_bot.storage import SQLiteStore
from tradikg_bot.trading.allocation import TrendConfig, run_trend

SYMS = ("XBTEUR", "ETHEUR")
START = pd.Timestamp("2016-03-01", tz="UTC")  # ab hier haben beide Coins 200 Tageskerzen
TRAIN_END = pd.Timestamp("2022-12-31", tz="UTC")


def load():
    st = SQLiteStore(PlatformConfig.load(None).market_db)
    d1 = {s: st.load_bars(s, "1d") for s in SYMS}
    h1 = {s: st.load_bars(s, "1h") for s in SYMS}
    return d1, h1


def pct(x: float, nd: int = 1) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{nd}f} %"


def stats(eq: pd.Series) -> dict:
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    dd = float((eq / eq.cummax() - 1).min())
    vol = float(r.std() * math.sqrt(365))
    down = r[r < 0]
    return {
        "Rendite p.a.": cagr,
        "Volatilität p.a.": vol,
        "Sharpe (rf 0)": float(r.mean() * 365 / vol) if vol else float("nan"),
        "Sortino": float(r.mean() * 365 / (down.std() * math.sqrt(365))) if len(down) else 0,
        "Max. Drawdown": dd,
        "Calmar": cagr / abs(dd) if dd else float("nan"),
        "schlechtester Tag": float(r.min()),
    }


def exposure(trades: list[dict], alloc, index: pd.DatetimeIndex, frac: float) -> pd.Series:
    """Investierter Anteil je Tag (Gewicht je Coin = frac / Anzahl Coins)."""
    w = frac / len(SYMS)
    x = pd.Series(0.0, index=index)
    spans = [(pd.Timestamp(t["entry_ts"]), pd.Timestamp(t["exit_ts"])) for t in trades]
    spans += [(pd.Timestamp(p.entry_ts), index[-1]) for p in alloc.state.positions.values()]
    for a, b in spans:
        x[(x.index >= a) & (x.index < b)] += w
    return x


def buy_hold(d1, index) -> pd.Series:
    """50/50 kaufen und halten (einmal Taker-Gebühr beim Kauf)."""
    c = pd.DataFrame({s: d1[s]["close"] for s in SYMS}).reindex(index).ffill()
    return 100 * (1 - KRAKEN_SPOT_TAKER) * (c / c.iloc[0]).mean(axis=1)


def stop_gaps(trades, d1, h1, stop_frac=0.2) -> pd.DataFrame:
    """Je Notfall-Stop: Stop-Niveau nachrechnen und den ungünstigsten Stundenkurs der Auslöse-
    Stunde (Tief) als pessimistische Ausführung nehmen (Kraken-Stop = Market-Order)."""
    rows = []
    for t in trades:
        if t["exit_reason"] != "notfall_stop":
            continue
        sym, a, b = t["asset"], pd.Timestamp(t["entry_ts"]), pd.Timestamp(t["exit_ts"])
        closes = d1[sym]["close"]
        peak = max(t["entry_price"], float(closes[(closes.index >= a) & (closes.index < b)].max()))
        stop = peak * (1 - stop_frac)
        h = h1[sym]
        day = h[(h.index >= b) & (h.index < b + pd.Timedelta(days=1)) & (h["low"] <= stop)]
        if day.empty:
            continue
        bar = day.iloc[0]
        fill_open = min(float(bar["open"]), stop)
        rows.append(
            {
                "Coin": sym,
                "Tag": b.date(),
                "Stop": stop,
                "Lücke zum Eröffnungskurs": fill_open / stop - 1,
                "Lücke bis Stunden-Tief": float(bar["low"]) / stop - 1,
                "Ergebnis ab Einstieg netto": t["net_pnl_eur"] / (t["volume"] * t["entry_price"]),
            }
        )
    return pd.DataFrame(rows)


def section_risk(res, d1, h1) -> list[str]:
    eq, trades, alloc = res[1.0]
    out = ["## 1. Risiko je Position, je Tag und im Portfolio (Trendfolge, 100 % investiert)\n"]
    g = stop_gaps(trades, d1, h1)
    net = pd.Series([t["net_pnl_eur"] / (t["volume"] * t["entry_price"]) for t in trades])
    out.append(f"Trades 03/2016–10/2026: {len(trades)}, davon Notfall-Stops: {len(g)}.\n")
    if len(g):
        out.append(
            "| Kennzahl | Median | schlechtester Fall |\n|---|---|---|\n"
            f"| Kurslücke Stop → Eröffnung der Auslöse-Stunde | "
            f"{pct(g['Lücke zum Eröffnungskurs'].median(), 2)} | "
            f"{pct(g['Lücke zum Eröffnungskurs'].min(), 2)} |\n"
            f"| Stop → Tief der Auslöse-Stunde (pessimistisch) | "
            f"{pct(g['Lücke bis Stunden-Tief'].median(), 2)} | "
            f"{pct(g['Lücke bis Stunden-Tief'].min(), 2)} |\n"
            f"| Ergebnis je Stop-Trade ab Einstieg, netto (nachgezogener Stop) | "
            f"{pct(g['Ergebnis ab Einstieg netto'].median())} | "
            f"{pct(g['Ergebnis ab Einstieg netto'].min())} |\n"
        )
    if len(g):
        worst = g.nsmallest(3, "Lücke bis Stunden-Tief")
        out.append(
            "Größte Lücken (pessimistisch): "
            + "; ".join(
                f"{w.Coin} {w.Tag}: {pct(w['Lücke bis Stunden-Tief'])}" for _, w in worst.iterrows()
            )
            + ".\n"
        )
    out.append(f"Schlechtester Einzeltrade netto (alle Ausstiege): {pct(net.min())}.\n")
    r = eq.pct_change().dropna()
    bh = buy_hold(d1, eq.index).pct_change().dropna()
    rets = pd.DataFrame({s: d1[s]["close"].pct_change() for s in SYMS}).loc[START:]
    worst_gap = g["Lücke bis Stunden-Tief"].min() if len(g) else 0.0
    max_pos = 0.20 + KRAKEN_SPOT_MAKER + KRAKEN_SPOT_TAKER - worst_gap
    out.append(
        "| Ebene | historisch schlimmster Wert | Grenze laut Regel |\n|---|---|---|\n"
        f"| Tag (Strategie, Schluss zu Schluss) | {pct(r.min())} "
        "| keine Tagesgrenze im Trend-Bot |\n"
        f"| Tag (Kaufen & Halten 50/50) | {pct(bh.min())} | – |\n"
        f"| Position ab Einstieg (Stop 20 % + 0,4 % + 0,8 % Gebühren + Lücke) | "
        f"{pct(net.min())} | ≈ {pct(-max_pos)} |\n"
        f"| Portfolio, Max. Drawdown | {pct(stats(eq)['Max. Drawdown'])} | – |\n"
        f"| Korrelation BTC/ETH (Tagesrenditen) | {rets.corr().iloc[0, 1]:.2f} | – |\n"
    )
    for budget in (19, 100, 500, 1000):
        out.append(
            f"- {budget} €: beide Stops gleichzeitig, schlechteste gemessene Lücke → "
            f"≈ {budget * max_pos:.2f} € Verlust ab Einstieg."
        )
    out.append("")
    return out


def section_robust(res, d1) -> list[str]:
    out = ["## 2. Robustheit der 200-Tage-Trendfolge\n"]
    eq1, tr1, al1 = res[1.0]
    eq5, tr5, al5 = res[0.5]
    bh = buy_hold(d1, eq1.index)
    x1 = exposure(tr1, al1, eq1.index, 1.0)
    # Gleiches durchschnittliches Risiko: täglich auf mittleren Investitionsgrad umgeschichtet
    bh_r = bh.pct_change().fillna(0)
    matched = 100 * (1 + x1.mean() * bh_r).cumprod()
    curves = {
        "Trend 100 %": eq1,
        "Trend 50 %": eq5,
        "Kaufen & Halten 50/50": bh,
        f"Kaufen & Halten mit {x1.mean():.0%} investiert": matched,
    }
    out.append("### Kalenderjahre (getrennt, nicht überlappend)\n")
    head = "| Jahr | " + " | ".join(curves) + " | investiert (Trend 100 %) |"
    out += [head, "|" + "---|" * (len(curves) + 2)]
    plus = dict.fromkeys(curves, 0)
    years = sorted(set(eq1.index.year))
    for y in years:
        row = [str(y) if y < 2026 else "2026 (bis 17.10.)"]
        for k, c in curves.items():
            cy = c[c.index.year == y]
            prev = c[c.index.year < y]
            base = prev.iloc[-1] if len(prev) else cy.iloc[0]
            ret = cy.iloc[-1] / base - 1
            plus[k] += ret > 0
            row.append(pct(ret))
        row.append(f"{x1[x1.index.year == y].mean():.0%}")
        out.append("| " + " | ".join(row) + " |")
    out.append(
        "| Jahre im Plus | " + " | ".join(f"{plus[k]}/{len(years)}" for k in curves) + " | |\n"
    )
    out.append("### Unabhängige Zeitblöcke und risikobereinigte Kennzahlen\n")
    blocks = {
        "2016-03–2019 (Auswahl)": ("2016-03-01", "2019-12-31"),
        "2020–2022 (Auswahl)": ("2020-01-01", "2022-12-31"),
        "2023–10/2026 (Test)": ("2023-01-01", "2026-12-31"),
        "gesamt": ("2016-03-01", "2026-12-31"),
    }
    keys = ["Rendite p.a.", "Max. Drawdown", "Sharpe (rf 0)", "Sortino", "Calmar"]
    for name, (a, b) in blocks.items():
        out.append(f"**{name}**\n")
        out.append("| | " + " | ".join(keys) + " |")
        out.append("|" + "---|" * (len(keys) + 1))
        for k, c in curves.items():
            s = stats(c.loc[a:b])
            vals = [
                pct(s[x]) if x in ("Rendite p.a.", "Max. Drawdown") else f"{s[x]:.2f}" for x in keys
            ]
            out.append(f"| {k} | " + " | ".join(vals) + " |")
        out.append("")
    out.append(
        f"Investitionsgrad Trend 100 %: Ø {x1.mean():.0%} der Tage (Rest Bargeld ohne Zins). "
        f"Trades gesamt: {len(tr1)} "
        f"(≈ {len(tr1) / ((eq1.index[-1] - eq1.index[0]).days / 365.25):.1f} pro Jahr).\n"
    )
    return out


def section_capital(res, gw) -> list[str]:
    out = ["## 3. Gebühren und Kapital: Was ist bei 19 / 100 / 500 / 1.000 € ausführbar?\n"]
    rules = {s: gw.pair_rules(s) for s in SYMS}
    books = {s: gw.order_book(s, 100) for s in SYMS}
    lines = []
    for s in SYMS:
        r, b = rules[s], books[s]
        mid = (b.best_bid + b.best_ask) / 2
        min_eur = max(r.ordermin * mid, r.costmin)
        spread = (b.best_ask - b.best_bid) / mid

        def impact(eur, b=b, mid=mid):  # Market-Kauf über das Buch: Ø-Preis gegen Mitte
            got, left = 0.0, eur
            for px, vol in b.asks:
                take = min(left, px * vol)
                got += take / px
                left -= take
                if left <= 0:
                    break
            return (eur / got) / mid - 1

        lines.append(
            f"| {s} | {min_eur:.2f} € | {spread * 1e4:.2f} Bp. | {impact(1000) * 1e4:.2f} Bp. |"
        )
    out.append(
        "Kraken heute (öffentliche API): Mindestorder, Spread und Preiseinfluss einer 1.000-€-"
        "Market-Order.\n\n| Paar | Mindestorder | Spread | Preiseinfluss 1.000 € |\n"
        "|---|---|---|---|"
    )
    out += lines
    eq1, tr1, _ = res[1.0]
    yrs = (eq1.index[-1] - eq1.index[0]).days / 365.25
    fee_share = sum(t["fees_eur"] for t in tr1) / yrs / eq1.mean()
    min_btc = max(rules["XBTEUR"].ordermin * books["XBTEUR"].best_ask, rules["XBTEUR"].costmin)
    min_eth = max(rules["ETHEUR"].ordermin * books["ETHEUR"].best_ask, rules["ETHEUR"].costmin)
    min_any = max(min_btc, min_eth) * (1 + KRAKEN_SPOT_TAKER)
    rt_maker, rt_taker = 2 * KRAKEN_SPOT_MAKER, 2 * KRAKEN_SPOT_TAKER
    out.append(
        f"\nGebühren Kraken Stufe 1: {KRAKEN_SPOT_MAKER:.2%} Maker / {KRAKEN_SPOT_TAKER:.2%} "
        f"Taker → Kauf+Verkauf {rt_maker:.1%}–{rt_taker:.1%}. Spread und Preiseinfluss sind bei "
        "diesen Größen "
        f"gegenüber den Gebühren vernachlässigbar (< 0,05 %). Trendfolge: Gebühren ≈ "
        f"{fee_share:.1%} des Ø-Kapitals pro Jahr.\n"
    )
    out.append(
        "| Strategie | Ordergröße | 19 € | 100 € | 500 € | 1.000 € | Kosten je Runde |\n"
        "|---|---|---|---|---|---|---|"
    )

    def ok(size):
        return "✅" if size >= min_any else f"❌ ({size:.2f} € < {min_any:.2f} €)"

    rows = [
        ("Trendfolge 100 %", lambda c: c / 2, "0,8–1,6 %"),
        ("Trendfolge 50 %", lambda c: c / 4, "0,8–1,6 %"),
        ("Grid (5 Stufen je Coin)", lambda c: c / 2 / 5, "0,8 % (Ziel 3 %)"),
        ("Daytrading-Studie (volles Kapital)", lambda c: c / 2, "0,8–1,6 %"),
        ("Signal-Bot (1 % Risiko, Stop ≈ 15 %)", lambda c: c * 0.01 / 0.15, "0,8–1,6 %"),
    ]
    for name, size, cost in rows:
        cells = " | ".join(ok(size(c)) for c in (19, 100, 500, 1000))
        out.append(f"| {name} | {size(100):.2f} € bei 100 € | {cells} | {cost} |")
    out.append("")
    return out


def section_copy(db: str) -> list[str]:
    """Punkt 6: Wann sieht unser Sammler einen Trade? Und was ändert das am Ergebnis?"""
    import sqlite3

    from tradikg_bot.copytrade.simulate import CopyConfig, _Prices, load_trades, simulate_copy

    trades, pools = load_trades(db)
    conn = sqlite3.connect(db)
    polls = pd.read_sql_query("SELECT ts, pool, newest FROM polls", conn)
    conn.close()
    polls["ts"] = pd.to_datetime(polls["ts"], utc=True)
    polls["newest"] = pd.to_datetime(polls["newest"], utc=True)
    seen = pd.merge_asof(
        trades[trades["ts"] >= polls["ts"].min()].sort_values("ts"),
        polls.sort_values("newest").rename(columns={"ts": "seen"}),
        left_on="ts",
        right_on="newest",
        by="pool",
        direction="forward",
    ).dropna(subset=["seen"])
    # Der Abruf-Zeitstempel ist der Start des Durchgangs; abgefragt wurde der Pool frühestens,
    # als sein neuester Trade schon existierte → Untergrenze der echten Verzögerung.
    seen_at = seen[["seen", "newest"]].max(axis=1)
    lag = (seen_at - seen["ts"]).dt.total_seconds()
    q = lag.quantile([0.5, 0.9]).round().astype(int).tolist()
    gap = polls.sort_values("ts").groupby("pool")["ts"].diff().dt.total_seconds().median()
    conn = sqlite3.connect(db)
    complete = conn.execute("SELECT AVG(overlap) FROM polls").fetchone()[0]
    conn.close()
    out = [
        "## 6. Copy-Trading-Simulation: Audit\n",
        f"Daten: {len(trades):,} Trades aus {trades['pool'].nunique()} Pools, "
        f"{trades['ts'].min():%d.%m. %H:%M} – {trades['ts'].max():%d.%m. %H:%M} UTC.\n",
        f"**Beobachtungsverzögerung** (Trade auf der Blockchain → erstmals von unserem Sammler "
        f"gesehen, Untergrenze, {len(lag):,} Trades): Median ≥ {q[0]} s, 90 % ≥ {q[1]} s. "
        f"Jeder Pool wird im Median nur alle {gap:.0f} s abgefragt; {1 - complete:.0%} der "
        "Abrufe hatten keine Überlappung mit dem vorigen (dazwischen verpasste Trades). "
        "Der Simulator nimmt 10 s an.\n",
        "| Variante | Endwert (Start 100 $) | Trades | Gewinner | veraltete Ausstiegspreise "
        "| ohne spätere Verkäufe im Pool |",
        "|---|---|---|---|---|---|",
    ]
    prices = _Prices(trades)
    last_sell = trades[trades["kind"] == "sell"].groupby("pool")["ts"].max()
    runs = [("Beste Wallets, 10 s", CopyConfig())]
    runs += [(f"Beste Wallets, {d} s (gemessen)", CopyConfig(delay_s=float(d))) for d in q]
    runs += [(f"Zufällige Wallets (Seed {i}), 10 s", CopyConfig(random_seed=i)) for i in range(5)]
    for name, cfg in runs:
        r = simulate_copy(trades, pools, cfg)
        tr = pd.DataFrame(r.trades)
        stale = no_sell = 0
        tok_pool = dict(zip(trades["token"], trades["pool"], strict=False))
        for t in r.trades:
            pool = tok_pool.get(t["token"])
            ts = t["exit_ts"] + pd.Timedelta(seconds=cfg.delay_s)
            stale += prices.after(pool, ts, cfg.price_window_s) is None
            no_sell += last_sell.get(pool, pd.Timestamp(0, tz="UTC")) <= t["entry_ts"]
        wins = int((tr["pnl_usd"] > 0).sum()) if len(tr) else 0
        out.append(f"| {name} | {r.final:.2f} $ | {len(tr)} | {wins} | {stale} | {no_sell} |")
    out.append("")
    return out


def main() -> None:
    import sys

    if "--copy" in sys.argv:
        print("\n".join(section_copy(str(PlatformConfig.load(None).data_dir / "copy.sqlite"))))
        return
    d1, h1 = load()
    data = {s: d1[s].loc[: d1[s].index.max()] for s in SYMS}
    gw = KrakenSpotGateway(KrakenClient())

    def min_order(sym, px):
        r = gw.pair_rules(sym)
        return max(r.ordermin * px, r.costmin)

    res = {}
    for frac in (1.0, 0.5):
        eq, trades, _, alloc = run_trend(
            data, START, None, 100.0, min_order, TrendConfig(invest_fraction=frac)
        )
        res[frac] = (eq, trades, alloc)
    lines = ["# Strategie-Audit – Zahlen (automatisch erzeugt)\n"]
    lines += section_risk(res, d1, h1)
    lines += section_robust(res, d1)
    lines += section_capital(res, gw)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
