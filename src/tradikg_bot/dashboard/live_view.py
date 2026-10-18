"""Live-Ansicht des virtuellen Copy-Bots (aktualisiert sich selbst alle 15 s)."""
# ruff: noqa: E501  (HTML-Bausteine)

from __future__ import annotations

import html
import json

import altair as alt
import pandas as pd
import streamlit as st

from tradikg_bot.platform_config import PlatformConfig

GREEN, RED, BLUE, GREY = "#22c55e", "#ef4444", "#3b82f6", "#8a8985"


def _card(label: str, value: str, sub: str = "", color: str = "") -> str:
    style = f"color:{color};" if color else ""
    return (
        f"<div style='border:1px solid rgba(128,128,128,.3);border-radius:12px;padding:12px 14px'>"
        f"<div style='font-size:11px;letter-spacing:.06em;opacity:.7'>{label}</div>"
        f"<div style='font-size:26px;font-weight:700;{style}'>{value}</div>"
        f"<div style='font-size:12px;opacity:.7'>{sub}</div></div>"
    )


def _feed_html(feed: list[dict]) -> str:
    rows = []
    for e in reversed(feed[-30:]):
        t = e["text"]
        color = BLUE if t.startswith("[COPY]") else RED if t.startswith("[FIRED]") else GREY
        pnl = e.get("pnl")
        if pnl is not None:
            color = GREEN if pnl > 0 else RED
        right = f"{pnl:+.2f} $" if pnl is not None else ""
        rows.append(
            f"<div style='display:flex;justify-content:space-between;gap:8px;font:12px ui-monospace,"
            f"monospace;padding:2px 0;color:{color}'><span>{pd.Timestamp(e['ts']).tz_convert('Europe/Vienna'):%H:%M:%S} {html.escape(t)}</span>"
            f"<span>{right}</span></div>"
        )
    return "".join(rows) or "<div style='opacity:.7'>Noch keine Ereignisse.</div>"


def render(cfg: PlatformConfig) -> None:
    st.markdown("### 🤖 Copy-Bot · LIVE · virtuell")
    st.caption(
        "Folgt automatisch den 3 besten aktiven Solana-Tradern (Rangliste alle 30 min), kopiert deren "
        "Käufe/Verkäufe zum Kurs nach 10 s Verzögerung inkl. Gebühren. Kein echtes Geld."
    )

    @st.fragment(run_every=15)
    def live() -> None:
        path = cfg.data_dir / "copy_live.json"
        if not path.exists():
            st.info(
                "Live-Bot startet gerade – erster Stand nach dem nächsten Sammel-Durchgang (~3 min)."
            )
            return
        s = json.loads(path.read_text())
        updated = pd.Timestamp(s["updated"])
        age = (pd.Timestamp.now(tz="UTC") - updated).total_seconds() / 60
        live_ok = age < 8
        cap, bal, pnl = s["capital"], s["balance"], s["pnl"]
        leaders = s.get("leaders", [])
        losses = max(s.get("losses", {}).values() or [0])
        badge = "🟢 LIVE" if live_ok else f"🟠 seit {age:.0f} min keine Daten"
        st.markdown(
            f"{badge} · gestartet {pd.Timestamp(s['start']).tz_convert('Europe/Vienna'):%d.%m. %H:%M} · "
            f"Stand {updated.tz_convert('Europe/Vienna'):%H:%M:%S}"
        )
        c = st.columns(4)
        c[0].markdown(
            _card("GUTHABEN", f"{bal:,.2f} $", f"Start {cap:,.2f} $"), unsafe_allow_html=True
        )
        c[1].markdown(
            _card(
                "GESAMT P&L",
                f"{pnl:+,.2f} $",
                f"{pnl / cap:+.1%} · {s['trades']} Trades, {s['wins']} Gewinne",
                GREEN if pnl > 0 else RED if pnl < 0 else "",
            ),
            unsafe_allow_html=True,
        )
        c[2].markdown(
            _card(
                "KOPIERE JETZT",
                f"{len(leaders)} Trader",
                " · ".join(w[:6] + "…" for w in leaders) or "suche aktive Trader …",
            ),
            unsafe_allow_html=True,
        )
        c[3].markdown(
            _card(
                "FEUER-REGEL",
                f"{s.get('fire_after', 2)} Verluste in Folge",
                f"aktuell höchstens {losses} in Folge",
            ),
            unsafe_allow_html=True,
        )
        left, right = st.columns([3, 2])
        curve = pd.DataFrame(s.get("curve", []), columns=["ts", "balance"])
        with left:
            st.markdown("**Guthaben-Verlauf**")
            if len(curve) > 1:
                curve["ts"] = pd.to_datetime(curve["ts"], utc=True)
                base = (
                    alt.Chart(pd.DataFrame({"y": [cap]}))
                    .mark_rule(color=GREY, strokeDash=[4, 4])
                    .encode(y="y:Q")
                )
                line = (
                    alt.Chart(curve)
                    .mark_line(color=GREEN if pnl >= 0 else RED, strokeWidth=2)
                    .encode(
                        x=alt.X("ts:T", title=None),
                        y=alt.Y("balance:Q", title="$", scale=alt.Scale(zero=False)),
                        tooltip=[
                            alt.Tooltip("ts:T", title="Zeit"),
                            alt.Tooltip("balance:Q", format=",.2f"),
                        ],
                    )
                )
                st.altair_chart((line + base).properties(height=280), use_container_width=True)
            else:
                st.caption("Verlauf erscheint nach den ersten Ereignissen.")
        with right:
            st.markdown("**Copy-Feed**")
            st.markdown(
                f"<div style='max-height:300px;overflow-y:auto'>{_feed_html(s.get('feed', []))}</div>",
                unsafe_allow_html=True,
            )
        st.markdown("**Wem wir folgen**")
        latest: dict[str, dict] = {}
        for f in s.get("followed", []):
            latest[f["wallet"]] = f
        cards = sorted(latest.values(), key=lambda f: (f["wallet"] not in leaders, -f["pnl_usd"]))[
            :6
        ]
        cols = st.columns(max(1, len(cards)))
        for col, f in zip(cols, cards, strict=False):
            status = (
                ("GEFEUERT", RED)
                if f["fired"]
                else ("FOLGT", GREEN)
                if f["wallet"] in leaders
                else ("BANK", GREY)
            )
            col.markdown(
                f"<div style='border:1px solid {status[1]};border-radius:12px;padding:10px;text-align:center'>"
                f"<div style='font-size:11px;color:{status[1]};font-weight:700'>{status[0]}</div>"
                f"<div style='font:600 13px ui-monospace,monospace'>{f['wallet'][:8]}…</div>"
                f"<div style='font-size:12px;opacity:.75'>ROI {f['roi']:+.0%} · {f['win_rate']:.0%} Treffer</div>"
                f"<div style='font-size:13px;color:{GREEN if f['pnl_usd'] > 0 else RED if f['pnl_usd'] < 0 else ''}'>"
                f"uns: {f['pnl_usd']:+.2f} $ · {f['copied']} kopiert</div></div>",
                unsafe_allow_html=True,
            )
        if s.get("open"):
            st.markdown("**Offene Positionen**")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Token": o["token"][:10] + "…",
                            "von": o["wallet"][:6] + "…",
                            "seit": f"{pd.Timestamp(o['since']).tz_convert('Europe/Vienna'):%H:%M:%S}",
                            "Einsatz": f"{o['cost_usd']:.2f} $",
                            "Wert jetzt": f"{o['value_usd']:.2f} $",
                            "Ergebnis": f"{o['value_usd'] - o['cost_usd']:+.2f} $",
                        }
                        for o in s["open"]
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

    live()
