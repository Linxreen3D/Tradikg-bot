"""Lokales Streamlit-Dashboard (nur lesend, nur 127.0.0.1).

Start: ``scripts/start_dashboard.sh`` oder
``streamlit run src/tradikg_bot/dashboard/app.py --server.address 127.0.0.1``

Das Dashboard zeigt gespeicherte Ergebnisse an. Es kann weder Orders senden noch
den Live-Modus aktivieren und gibt keine API-Schlüssel aus.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from tradikg_bot.dashboard import queries as q
from tradikg_bot.dashboard import replay_view
from tradikg_bot.platform_config import PlatformConfig

SERIES = "#2a78d6"  # Referenzfarbe Slot 1 (einreihige Diagramme)


def eur(v: float | None, digits: int = 2) -> str:
    return "–" if v is None else f"{v:,.{digits}f} €"


def pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:.2f} %"


def main() -> None:
    st.set_page_config(page_title="Tradikg Paper", page_icon="📈", layout="wide")
    cfg = PlatformConfig.load()
    st.title("Tradikg – Paper-Trading & Research")
    st.caption("Nur virtuelles Kapital. Kein Echtgeldhandel. Keine Renditeversprechen.")
    if st.button("Aktualisieren"):
        st.rerun()

    tabs = st.tabs(
        [
            "Übersicht",
            "Trading",
            "Analytics",
            "Historical Replay",
            "AI Research",
            "System Health",
            "Einstellungen",
        ]
    )
    b = q.balances(cfg)
    windows = q.pnl_windows(cfg)
    h = q.health(cfg)

    with tabs[0]:
        c = st.columns(4)
        c[0].metric("Gesamtkapital", eur(b["total_capital"]))
        c[1].metric("Trading-Balance", eur(b["trading_balance"]))
        c[2].metric("Auszahlungsreserve", eur(b["payout_reserve"]))
        c[3].metric("Langfristportfolio", eur(b["longterm_portfolio"]))
        c = st.columns(4)
        c[0].metric("Heute", eur(windows["today"], 4))
        c[1].metric("7 Tage", eur(windows["week"], 4))
        c[2].metric("Gesamt", eur(windows["total"], 4))
        c[3].metric("Nettorendite", pct(b["net_return"]))
        st.write(
            f"**Modus:** paper · **offene Verluste (vor Verteilung aufzuholen):** "
            f"{eur(b['unrecovered_losses'], 4)} · **Steuerrücklage:** "
            f"{eur(b['tax_reserve'], 4)}"
        )
        blocked = h["status"].get("entries_blocked")
        if h["safe_halt"]:
            st.error(f"⛔ SAFE_HALT: {h['safe_halt']}")
        elif h["kill_switch"]:
            st.warning("⚠️ Kill Switch aktiv – keine neuen Einstiege")
        elif blocked:
            st.warning(f"⚠️ Neue Einstiege gesperrt: {blocked}")
        else:
            st.success("✅ System bereit (Paper)")

    with tabs[1]:
        st.subheader("Offene Positionen")
        st.dataframe(q.open_positions(cfg), use_container_width=True, hide_index=True)
        st.subheader("Letzte Trades")
        trades = q.closed_trades(cfg)
        cols = [
            c
            for c in (
                "id",
                "pair",
                "strategy",
                "entry_avg_price",
                "exit_avg_price",
                "stop_loss_price",
                "take_profit_price",
                "filled_volume",
                "entry_fee",
                "exit_fee",
                "realized_pnl",
                "exit_reason",
                "closed_at",
                "assessment",
            )
            if c in trades.columns
        ]
        st.dataframe(
            trades[cols].iloc[::-1].head(100) if not trades.empty else trades,
            use_container_width=True,
            hide_index=True,
        )
        st.subheader("Signale (inkl. Ablehnungsgrund)")
        st.dataframe(q.recent_signals(cfg), use_container_width=True, hide_index=True)
        st.caption(f"Einsetzbares Kapital: {eur(min(cfg.capital_eur, b['trading_balance']))}")

    with tabs[2]:
        curve = q.equity_curve(cfg)
        perf = q.performance(cfg)
        o = perf["overall"]
        c = st.columns(4)
        c[0].metric("Trades", o["trades"])
        c[1].metric("Trefferquote", pct(o["win_rate"]))
        c[2].metric(
            "Profit Factor", "–" if o["profit_factor"] is None else f"{o['profit_factor']:.2f}"
        )
        c[3].metric("Erwartungswert/Trade", eur(o["expectancy"], 4))
        if not curve.empty:
            import altair as alt

            frame = curve.reset_index()[["ts", "equity", "drawdown"]]
            frame["drawdown_pct"] = frame["drawdown"] * 100
            base = alt.Chart(frame).encode(x=alt.X("ts:T", title=None))
            st.markdown("**Equity (EUR)**")
            st.altair_chart(
                base.mark_line(color=SERIES, strokeWidth=2).encode(
                    y=alt.Y("equity:Q", title="EUR", scale=alt.Scale(zero=False)),
                    tooltip=[
                        alt.Tooltip("ts:T", title="Zeit"),
                        alt.Tooltip("equity:Q", title="Equity", format=",.2f"),
                    ],
                ),
                use_container_width=True,
            )
            st.markdown("**Drawdown (%)**")
            st.altair_chart(
                base.mark_area(color=SERIES, opacity=0.6).encode(
                    y=alt.Y("drawdown_pct:Q", title="%"),
                    tooltip=[
                        alt.Tooltip("ts:T", title="Zeit"),
                        alt.Tooltip("drawdown_pct:Q", title="Drawdown %", format=".2f"),
                    ],
                ),
                use_container_width=True,
            )
        for title, key in (
            ("Nach Asset", "by_asset"),
            ("Nach Strategie", "by_strategy"),
            ("Nach Marktphase", "by_regime"),
            ("Nach Wochentag", "by_weekday"),
        ):
            st.markdown(f"**{title}**")
            st.dataframe(perf[key], use_container_width=True, hide_index=True)
        st.markdown("**Kosten**")
        st.json(perf["costs"])
        reports = q.daily_reports(cfg)
        if not reports.empty:
            day = st.selectbox("Tagesbericht", reports["day"])
            st.markdown(reports.set_index("day").loc[day, "markdown"])

    with tabs[3]:
        replay_view.render(cfg)

    with tabs[4]:
        st.subheader("Strategie-Ranking (letzter Research-Lauf, Testdaten)")
        st.caption("„bestätigt“ gilt nur für die untersuchten Daten – keine Gewinngarantie.")
        st.dataframe(q.research_results(cfg), use_container_width=True, hide_index=True)
        st.subheader("Research-Läufe")
        st.dataframe(q.research_runs(cfg), use_container_width=True, hide_index=True)
        st.subheader("Modelle (Champion/Challenger)")
        st.dataframe(q.models(cfg), use_container_width=True, hide_index=True)
        st.dataframe(q.model_events(cfg), use_container_width=True, hide_index=True)
        st.subheader("Prognosekalibrierung (vorhergesagt vs. beobachtet)")
        st.dataframe(q.calibration(cfg), use_container_width=True, hide_index=True)

    with tabs[5]:
        status = h["status"]
        c = st.columns(4)
        c[0].metric("Letzter Durchgang", status.get("updated_at", "–"))
        c[1].metric("CPU", "–" if h["cpu_percent"] is None else f"{h['cpu_percent']:.0f} %")
        c[2].metric("RAM", "–" if h["ram_percent"] is None else f"{h['ram_percent']:.0f} %")
        c[3].metric("Kill Switch", "aktiv" if h["kill_switch"] else "aus")
        st.write(f"**Datenfeed – letzter Erfolg:** {status.get('data_last_success', '–')}")
        st.write(f"**Veraltete Feeds:** {status.get('stale_feeds', [])}")
        st.write(f"**Datenfehler:** {status.get('data_errors', {})}")
        st.write(f"**Risk Engine / SAFE_HALT:** {h['safe_halt'] or 'keine Sperre'}")
        st.write(f"**Trading-Instanz (PID-Sperre):** {h['lock_pid'] or 'läuft nicht'}")
        st.dataframe(h["processes"], use_container_width=True, hide_index=True)
        st.subheader("Fehler- und Warnprotokoll")
        st.dataframe(h["errors"], use_container_width=True, hide_index=True)

    with tabs[6]:
        st.subheader("Einstellungen (nur Anzeige)")
        st.json(q.settings(cfg))
        st.info(
            "Änderungen in ~/.tradikg/config.toml. Der Live-Modus ist gesperrt und erfordert "
            "eine separate, ausdrückliche Freigabe außerhalb des Dashboards."
        )
        st.caption(f"Stand: {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC")


main()
