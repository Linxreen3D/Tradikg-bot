"""Dashboard-Bereich „Historical Replay“ (nur lesend).

Zeigt je Replay-Lauf und Kapitalszenario: Equity-Kurve Bot vs. Buy & Hold,
Drawdown, ausgeführte Trades, Netto-Ergebnis, Strategieanalyse, Kapitalaufteilung
(33/33/34 nach Verlustaufholung) und die Verteilung über alle Zeitfenster –
einschließlich der negativen Ergebnisse.
"""

from __future__ import annotations

import json

import altair as alt
import pandas as pd
import streamlit as st

from tradikg_bot.dashboard import queries as q
from tradikg_bot.platform_config import PlatformConfig

BOT = "#2a78d6"  # Kategorie-Slot 1
BENCH = "#eb6834"  # Kategorie-Slot 2
MUTED = "#8a8985"  # Referenzlinie (Startkapital), keine Datenreihe

PERIODS = {
    "walk_forward": "Walk-Forward (Entwicklungszeitraum)",
    "holdout": "Holdout (unangetastet)",
    "window": "Rollierendes Fenster",
}


def _eur(v: float | None) -> str:
    return "–" if v is None or pd.isna(v) else f"{v:,.2f} €"


def _pct(v: float | None) -> str:
    return "–" if v is None or pd.isna(v) else f"{v * 100:+.1f} %"


def _period_options(sc: pd.DataFrame, profile: str) -> dict[str, str]:
    bots = sc[(sc["variant"] == "bot") & (sc["profile"] == profile)].sort_values("start")
    out = {}
    for period in ("walk_forward", "holdout"):
        for _, r in bots[bots["period"] == period].iterrows():
            out[r["scenario_id"]] = (
                f"{PERIODS[period]} · {r['start']:%Y-%m-%d} – {r['end']:%Y-%m-%d}"
            )
    for k, (_, r) in enumerate(bots[bots["period"] == "window"].iterrows(), start=1):
        out[r["scenario_id"]] = f"Fenster {k} · {r['start']:%Y-%m-%d} – {r['end']:%Y-%m-%d}"
    return out


def render(cfg: PlatformConfig) -> None:
    runs = q.replay_runs(cfg)
    if runs.empty:
        st.info(
            "Noch kein Replay-Lauf. Auf dem Mac: `tradikg data fetch-kraken --timeframe 1d` "
            "(oder `tradikg data import-kraken <ZIP>`), danach `tradikg replay run`."
        )
        return
    run_id = st.selectbox(
        "Replay-Lauf",
        runs["id"].tolist(),
        format_func=lambda i: (
            f"#{i} · {runs.set_index('id').at[i, 'created_at'][:16]} · "
            f"{runs.set_index('id').at[i, 'data_source']}"
        ),
    )
    run = q.replay_run(cfg, int(run_id))
    sc = q.replay_scenarios(cfg, int(run_id))
    if run is None or sc.empty:
        st.warning("Lauf ohne Szenarien.")
        return
    if run["synthetic"]:
        st.warning("Synthetische Daten – prüft nur die Pipeline, sagt nichts über echte Märkte.")
    if not run["holdout_evaluated"]:
        st.info("Holdout in diesem Lauf bewusst nicht ausgewertet.")
    elif run["holdout_count"] > 1:
        st.warning(
            f"Holdout dieser Daten bereits {run['holdout_count']}-mal ausgewertet – nicht mehr "
            "unangetastet, falls danach etwas geändert wurde."
        )
    st.caption(
        f"Assets: {', '.join(run['symbols'])} · Zeitraster {run['timeframe']} · "
        "Keine echten Orders. Historische Ergebnisse sind keine Vorhersage."
    )

    profiles = (
        sc[sc["variant"] == "bot"][["profile", "profile_label", "capital"]]
        .drop_duplicates("profile")
        .sort_values("capital")
    )
    labels = dict(zip(profiles["profile"], profiles["profile_label"], strict=True))
    keys = list(labels)
    profile = st.radio(
        "Startkapital",
        keys,
        index=keys.index("eur100") if "eur100" in keys else 0,
        format_func=labels.get,
        horizontal=True,
    )
    options = _period_options(sc, profile)
    scenario_id = st.selectbox("Zeitraum", list(options), format_func=options.get)
    bot = sc.set_index("scenario_id").loc[scenario_id]
    bench_id = scenario_id.replace("-bot-", "-buy_hold-")
    bench = (
        sc.set_index("scenario_id").loc[bench_id] if bench_id in set(sc["scenario_id"]) else None
    )

    # -- Kennzahlen ---------------------------------------------------------------------------
    st.markdown(f"**Was wäre aus {_eur(bot['capital'])} geworden?**")
    c = st.columns(5)
    c[0].metric("Bot – Endwert", _eur(bot["final_equity"]), _pct(bot["return_pct"]))
    c[1].metric(
        "Buy & Hold",
        _eur(bench["final_equity"]) if bench is not None else "–",
        _pct(bench["return_pct"]) if bench is not None else None,
    )
    c[2].metric("Nichts tun", _eur(bot["capital"]))
    c[3].metric("Max. Drawdown", _pct(bot["max_drawdown"]))
    c[4].metric("Trades", int(bot["trades"]))
    led = json.loads(bot["ledger_json"] or "{}")
    c = st.columns(4)
    c[0].metric("Trading-Balance", _eur(led.get("trading_balance")))
    c[1].metric("Auszahlungsreserve", _eur(led.get("payout_reserve")))
    c[2].metric("Langfristportfolio", _eur(led.get("longterm_portfolio")))
    c[3].metric("Offene Verluste", _eur(led.get("unrecovered_losses")))
    st.caption(
        "Gewinne werden erst nach Aufholung früherer Verluste verteilt: 33 % Trading, "
        f"33 % Auszahlungsreserve, 34 % Langfrist. Gebühren gesamt {_eur(bot['fees_eur'])}; "
        f"SL+TP in derselben Kerze (konservativ als Stop gewertet): {int(bot['ambiguous_exits'])}."
    )

    # -- Equity und Drawdown ------------------------------------------------------------------
    eq = q.replay_equity(
        cfg, int(run_id), [scenario_id] + ([bench_id] if bench is not None else [])
    )
    if not eq.empty:
        names = {scenario_id: "Bot", bench_id: "Buy & Hold"}
        eq["reihe"] = eq["scenario_id"].map(names)
        color = alt.Color(
            "reihe:N",
            scale=alt.Scale(domain=["Bot", "Buy & Hold"], range=[BOT, BENCH]),
            legend=alt.Legend(title=None, orient="top"),
        )
        lines = (
            alt.Chart(eq)
            .mark_line(strokeWidth=2)
            .encode(
                x=alt.X("ts:T", title=None),
                y=alt.Y("equity:Q", title="EUR", scale=alt.Scale(zero=False)),
                color=color,
                tooltip=[
                    alt.Tooltip("ts:T", title="Zeit"),
                    alt.Tooltip("reihe:N", title="Reihe"),
                    alt.Tooltip("equity:Q", title="Wert", format=",.2f"),
                ],
            )
        )
        start_rule = (
            alt.Chart(pd.DataFrame({"y": [bot["capital"]]}))
            .mark_rule(color=MUTED, strokeDash=[4, 4])
            .encode(y="y:Q")
        )
        st.markdown("**Equity: Bot vs. Buy & Hold** (gestrichelt: Startkapital)")
        st.altair_chart(lines + start_rule, use_container_width=True)
        own = eq[eq["scenario_id"] == scenario_id].copy()
        values = pd.concat([pd.Series([bot["capital"]]), own["equity"]], ignore_index=True)
        dd = (values / values.cummax() - 1).iloc[1:].to_numpy() * 100
        own["drawdown_pct"] = dd
        st.markdown("**Drawdown Bot (%)**")
        st.altair_chart(
            alt.Chart(own)
            .mark_area(color=BOT, opacity=0.6)
            .encode(
                x=alt.X("ts:T", title=None),
                y=alt.Y("drawdown_pct:Q", title="%"),
                tooltip=[
                    alt.Tooltip("ts:T", title="Zeit"),
                    alt.Tooltip("drawdown_pct:Q", title="Drawdown %", format=".2f"),
                ],
            ),
            use_container_width=True,
        )

    # -- Verteilung über alle Fenster ---------------------------------------------------------
    win = sc[(sc["period"] == "window") & (sc["profile"] == profile)]
    if not win.empty:
        dist = win[win["variant"].isin(["bot", "buy_hold"])].copy()
        dist["reihe"] = dist["variant"].map({"bot": "Bot", "buy_hold": "Buy & Hold"})
        bots = dist[dist["reihe"] == "Bot"]["final_equity"]
        capital = float(bot["capital"])
        st.markdown(
            f"**Endwerte in allen {len(bots)} Zeitfenstern** (frisches Kapital je Fenster; "
            "jeder Punkt ist ein Zeitraum – auch die schlechten)"
        )
        c = st.columns(4)
        c[0].metric("Schlechtestes Fenster", _eur(bots.min()))
        c[1].metric("Median", _eur(bots.median()))
        c[2].metric("Bestes Fenster", _eur(bots.max()))
        c[3].metric("Fenster mit Verlust", f"{(bots < capital).mean():.0%}")
        points = (
            alt.Chart(dist)
            .mark_circle(size=90, stroke="white", strokeWidth=2, opacity=1)
            .encode(
                x=alt.X("start:T", title="Fensterbeginn"),
                y=alt.Y("final_equity:Q", title="Endwert (EUR)", scale=alt.Scale(zero=False)),
                color=alt.Color(
                    "reihe:N",
                    scale=alt.Scale(domain=["Bot", "Buy & Hold"], range=[BOT, BENCH]),
                    legend=alt.Legend(title=None, orient="top"),
                ),
                tooltip=[
                    alt.Tooltip("reihe:N", title="Reihe"),
                    alt.Tooltip("start:T", title="Beginn"),
                    alt.Tooltip("end:T", title="Ende"),
                    alt.Tooltip("final_equity:Q", title="Endwert", format=",.2f"),
                    alt.Tooltip("trades:Q", title="Trades"),
                ],
            )
        )
        rule = (
            alt.Chart(pd.DataFrame({"y": [capital]}))
            .mark_rule(color=MUTED, strokeDash=[4, 4])
            .encode(y="y:Q")
        )
        st.altair_chart(rule + points, use_container_width=True)

    # -- Robustheit und Baselines -------------------------------------------------------------
    st.markdown("**Robustheit und Baselines** (Walk-Forward und Holdout)")
    rob = sc[(sc["period"] != "window") & (sc["profile"] == profile)]
    if not rob.empty:
        table = rob.pivot_table(
            index="label", columns="period", values="final_equity", aggfunc="first"
        ).rename(columns=PERIODS)
        table.index.name = "Variante"
        st.dataframe(table.style.format("{:,.2f} €"), use_container_width=True)
        if (sc["upper_bound"] == 1).any():
            st.caption("„Obergrenze: TP zuerst“ ist nicht realistisch und nur als Spanne gedacht.")

    # -- Trades und Strategien ------------------------------------------------------------------
    trades = q.replay_trades(cfg, int(run_id), [scenario_id])
    st.markdown("**Strategieanalyse**")
    stats = q.replay_strategy_stats(trades)
    if stats.empty:
        st.write(
            "Keine Trades in diesem Zeitraum – ohne Out-of-Sample-Belege handelt der Bot nicht."
        )
    else:
        st.dataframe(stats, use_container_width=True, hide_index=True)
        st.markdown("**Ausgeführte Trades**")
        cols = [
            c
            for c in (
                "asset",
                "strategy",
                "regime",
                "entry_ts",
                "exit_ts",
                "entry_price",
                "exit_price",
                "stop_loss",
                "take_profit",
                "volume",
                "fees_eur",
                "net_pnl_eur",
                "exit_reason",
                "intrabar_ambiguous",
                "assessment",
            )
            if c in trades.columns
        ]
        st.dataframe(trades[cols], use_container_width=True, hide_index=True)

    with st.expander("Vollständiger Bericht inkl. Belastbarkeits-Checkliste"):
        st.markdown(run["report_md"])
