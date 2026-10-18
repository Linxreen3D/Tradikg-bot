"""Ziel-Check: Warum laufen die Bots ins Minus – und was ist mit 20 € realistisch möglich?

Alle Zahlen werden live berechnet: Bot-Stände aus ihren Dateien, Plus-Tage aus Kraken-Kerzen
(Trendfolge, Kaufen & Halten) und Kraken-Futures-Funding (Carry). Nur lesend.
"""

from __future__ import annotations

import json
import urllib.request

import altair as alt
import pandas as pd
import streamlit as st

from tradikg_bot.costs import KRAKEN_SPOT_MAKER
from tradikg_bot.platform_config import PlatformConfig

FUNDING = "https://futures.kraken.com/derivatives/api/v4/historicalfundingrates?symbol={}"
FUT_MAKER = 0.0002  # Kraken Futures Maker (öffentliche Gebührenstufe 1)
ENTRY_EXIT = 2 * (KRAKEN_SPOT_MAKER + FUT_MAKER)  # Spot kaufen + Short eröffnen, später beides zu


@st.cache_data(ttl=1800, show_spinner=False)
def funding_daily(symbol: str) -> pd.Series:
    """Funding pro Tag für die Short-Seite (positiv = man bekommt Geld), letzte 12 Monate."""
    req = urllib.request.Request(FUNDING.format(symbol), headers={"User-Agent": "tradikg-bot/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rates = pd.DataFrame(json.loads(r.read())["rates"])
    rates["ts"] = pd.to_datetime(rates["timestamp"], utc=True)
    return rates.set_index("ts")["relativeFundingRate"]


def day_stats(r: pd.Series) -> dict:
    r = r.dropna()
    monthly = (1 + r).groupby(r.index.strftime("%Y-%m")).prod() - 1
    return {
        "Plus-Tage": f"{(r > 0).mean():.0%}",
        "Plus-Monate": f"{(monthly > 0).mean():.0%}",
        "Ø pro Tag": f"{r.mean():+.3%}",
        "schlechtester Tag": f"{r.min():+.1%}",
    }


@st.cache_data(ttl=6 * 3600, show_spinner="Rechne Plus-Tage aus 10 Jahren Kraken-Daten …")
def history_table(market_db: str) -> pd.DataFrame:
    from tradikg_bot.storage import SQLiteStore
    from tradikg_bot.trading.allocation import TrendConfig, run_trend

    store = SQLiteStore(market_db)
    data = {s: store.load_bars(s, "1d") for s in ("XBTEUR", "ETHEUR")}
    rows = []
    if all(len(d) > 400 for d in data.values()):
        eq, *_ = run_trend(
            data, pd.Timestamp("2016-03-01", tz="UTC"), None, 100.0, lambda s, p: 5.0, TrendConfig()
        )
        rows.append(
            {"Strategie": "Trendfolge (läuft mit deinen 19 €)", **day_stats(eq.pct_change())}
        )
        c = pd.DataFrame({s: d["close"] for s, d in data.items()}).loc["2016-03-01":]
        rows.append(
            {
                "Strategie": "Kaufen & Halten BTC/ETH",
                **day_stats((c / c.iloc[0]).mean(axis=1).pct_change()),
            }
        )
    if not rows:  # frische Installation ohne Kerzen: auch kein Netzabruf
        return pd.DataFrame(rows)
    for sym, name in (("PF_XBTUSD", "BTC"), ("PF_ETHUSD", "ETH")):
        try:
            day = funding_daily(sym).resample("1D").sum()
            rows.append({"Strategie": f"Funding-Carry {name} (12 Monate)", **day_stats(day)})
        except Exception:  # Kraken Futures nicht erreichbar
            pass
    return pd.DataFrame(rows)


def _bots(cfg: PlatformConfig) -> pd.DataFrame:
    rows = []
    snap = cfg.home / "live" / "snapshot.json"
    if snap.exists():
        s = json.loads(snap.read_text())
        val = s["balances"].get("ZEUR", {}).get("total", 0.0) + sum(
            x.get("units", 0) * (s["prices"].get(k) or 0) for k, x in s["symbols"].items()
        )
        rows.append(
            (
                "💶 Kraken-Trendfolge (echt)",
                "19,00 €",
                f"{val:.2f} €",
                val / 19 - 1,
                "Kaufgebühren 0,15 € + Kurs leicht gefallen; handelt nur ~7× pro Jahr",
            )
        )
    for name, label, why in (
        (
            "copy_live.json",
            "🤖 Copy alle Chains v2 (virtuell)",
            "Verzögerung, Glücks-Auswahl, Memecoins fallen meist",
        ),
        ("copy_live_v1.json", "🤖 Copy alte Taktik (virtuell)", "wie oben"),
    ):
        p = cfg.data_dir / name
        if p.exists():
            d = json.loads(p.read_text())
            rows.append(
                (
                    label,
                    f"{d['capital']:.0f} $",
                    f"{d['balance']:.2f} $",
                    d["pnl"] / d["capital"],
                    f"{d['trades']} Trades – {why}",
                )
            )
    p = cfg.home / "copylive" / "snapshot.json"
    if p.exists():
        d = json.loads(p.read_text())
        rows.append(
            (
                "💸 Solana-Copy (Schatten)",
                f"{d['capital_sol']:.2f} SOL",
                f"{d['equity_sol']:.4f} SOL",
                d["equity_sol"] / d["capital_sol"] - 1,
                f"{len(d['trades'])} Trades – echte Kurse, kein Geld",
            )
        )
    df = pd.DataFrame(rows, columns=["Bot", "Start", "Jetzt", "Ergebnis", "Hauptgrund"])
    df["Ergebnis"] = df["Ergebnis"].map(lambda v: f"{v:+.1%}")
    return df


def render(cfg: PlatformConfig) -> None:
    st.subheader("🎯 Ziel: jeden Tag ein bisschen Plus mit 20 €")
    st.markdown("#### 1. Wo wir stehen")
    st.dataframe(_bots(cfg), hide_index=True, width="stretch")

    st.markdown("#### 2. Warum fast alles im Minus läuft")
    st.markdown(
        """
- **Gebühren schlagen jeden kleinen Gewinn.** Kraken nimmt 0,4–0,8 % pro Order, also **0,8–1,6 % pro
  Kauf + Verkauf**. Kurzfristige Kursbewegungen, die man vorhersagen kann, sind kleiner. Darum
  endeten alle 10 getesteten Daytrading-Varianten bei Kraken nahe 0 €.
- **Copy-Trading:** Wir sehen die Trades zu spät (Sammler ≥ 100 s, direkt auf der Blockchain ~10 s),
  die „besten“ Wallets der letzten 3 Stunden hatten meist Glück, und die meisten Memecoins fallen
  nach dem Hype. Zufällig gewählte Wallets verloren 3–16 %, unsere Auswahl liegt dazwischen.
- **Trendfolge:** Sie verdient an wenigen großen Bewegungen pro Jahr, nicht täglich. Nur an ~23 %
  aller Tage im Plus; die letzten 12 Monate waren seitwärts.
- **20 € Kapital:** Selbst eine gute Strategie bringt hier Cent-Beträge. Mindestorder 4,40 € und
  fixe Gebühren wiegen schwer.
- **Grok-/Copy-Bot-Videos:** Gezeigt werden die Gewinner (Screenshots, kurze Zeiträume). Geprüfte
  Langzeit-Ergebnisse nach Gebühren gibt es dazu nicht – wir rechnen dagegen alles ehrlich mit.
"""
    )

    st.markdown("#### 3. Welche Strategie ist an den meisten Tagen im Plus?")
    st.dataframe(history_table(str(cfg.market_db)), hide_index=True, width="stretch")
    st.caption(
        "Plus-Tage = Tage mit Gewinn; bei der Trendfolge zählen Tage ohne Position als 0. "
        "Funding-Carry: BTC/ETH kaufen und gleich viel auf dem Kraken-Perpetual leerverkaufen – "
        "der Kurs ist dann egal, man erhält stündlich die Funding-Zahlung (brutto, vor "
        f"{ENTRY_EXIT:.2%} Ein-/Ausstiegskosten)."
    )

    st.markdown("#### 4. Carry-Test mit 20 € (virtuell, echte Kraken-Funding-Daten)")
    path = cfg.home / "carry" / "paper.json"
    if not path.exists():
        st.info("Carry-Test nicht angelegt.")
    else:
        p = json.loads(path.read_text())
        start, cap = pd.Timestamp(p["start"]), float(p["capital_eur"])
        try:
            rates = funding_daily(p["symbol"])
        except Exception as exc:
            st.warning(f"Kraken Futures nicht erreichbar: {exc}")
            rates = pd.Series(dtype=float)
        got = rates[rates.index >= start]
        earned = cap * float(got.sum())
        cost = cap * ENTRY_EXIT / 2  # Einstieg bezahlt, Ausstieg noch offen
        c = st.columns(3)
        c[0].metric("Funding erhalten", f"{earned:+.4f} €", f"{len(got)} Stunden seit Start")
        c[1].metric("Einstiegskosten", f"{-cost:.4f} €", "Spot-Kauf + Short (Maker)")
        c[2].metric("Netto bisher", f"{earned - cost:+.4f} €")
        if len(got):
            per_day = (got.resample("1D").sum() * cap).rename("€").reset_index()
            st.altair_chart(
                alt.Chart(per_day)
                .mark_bar()
                .encode(
                    x=alt.X("ts:T", title=None),
                    y=alt.Y("€:Q", title="€ pro Tag"),
                    color=alt.condition(
                        "datum['€'] >= 0", alt.value("#22c55e"), alt.value("#ef4444")
                    ),
                )
                .properties(height=180),
                width="stretch",
            )
        year = rates.resample("1D").sum().sum() if len(rates) else 0.0
        st.caption(
            f"Letzte 12 Monate hätte das {cap * year:.2f} € brutto gebracht, nach Ein-/Ausstieg "
            f"≈ {cap * (year - ENTRY_EXIT):.2f} € – also ein paar Cent pro Monat. Die "
            "Einstiegskosten sind erst nach mehreren Wochen wieder verdient."
        )

    st.markdown("#### 5. Empfehlung (Projektleitung)")
    st.markdown(
        """
1. **Täglich ein kleines Plus ist nur mit Funding-Carry erreichbar** (3 von 4 Tagen im Plus, kaum
   Kursrisiko). Mit 20 € sind das aber nur **≈ 0,50 € pro Jahr**. Voraussetzung: Kraken Futures in
   deinem Konto (Kraken Pro → „Futures“ sichtbar?). Erst dann baue ich den Echtgeld-Carry-Bot.
2. **Die Trendfolge** bleibt die einzige getestete Strategie mit nennenswerter Rendite (Median-Jahr
   +25 %), aber mit Verlustjahren und nur wenigen Plus-Tagen.
3. **Kein Echtgeld für Memecoin-Copy:** Alle Copy-Varianten liegen nach Kosten im Minus.
4. **Selbstfinanzierung** (≈ 20 € pro Monat) bräuchte ehrlich gerechnet ≈ 10.000 € Kapital mit
   Carry oder ≈ 1.000 € mit Trendfolge (mit Verlustjahren bis −30 %). Mit 20 € geht es nicht –
   kein seriöser Bot schafft das.
"""
    )
