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


def _feed_html(feed: list[dict], unit: str = "$", digits: int = 2) -> str:
    rows = []
    for e in reversed(feed[-30:]):
        t = e["text"]
        color = BLUE if t.startswith("[COPY]") else RED if t.startswith("[FIRED]") else GREY
        color = "#f59e0b" if t.startswith("[SKIP]") else color
        pnl = e.get("pnl")
        if pnl is not None:
            color = GREEN if pnl > 0 else RED
        right = f"{pnl:+.{digits}f} {unit}" if pnl is not None else ""
        rows.append(
            f"<div style='display:flex;justify-content:space-between;gap:8px;font:12px ui-monospace,"
            f"monospace;padding:2px 0;color:{color}'><span>{pd.Timestamp(e['ts']).tz_convert('Europe/Vienna'):%H:%M:%S} {html.escape(t)}</span>"
            f"<span>{right}</span></div>"
        )
    return "".join(rows) or "<div style='opacity:.7'>Noch keine Ereignisse.</div>"


COIN = {"XBTEUR": ("XXBT", "BTC"), "ETHEUR": ("XETH", "ETH")}


def render_real(cfg: PlatformConfig) -> None:
    """Echtes Kraken-Guthaben aus dem Snapshot des Echtgeld-Bots (alle 5 min aktualisiert)."""
    st.markdown("### 💶 Echtgeld bei Kraken · LIVE")

    @st.fragment(run_every=15)
    def real() -> None:
        path = cfg.home / "live" / "snapshot.json"
        if not path.exists():
            st.info(
                "Noch kein Kontostand – erscheint nach dem nächsten Durchgang des Echtgeld-Bots "
                "(nach Neustart mit dem neuen Stand alle 5 min)."
            )
            return
        s = json.loads(path.read_text())
        bal, prices = s.get("balances", {}), s.get("prices", {})
        eur = bal.get("ZEUR", {}).get("total", 0.0)
        eur_held = bal.get("ZEUR", {}).get("held", 0.0)
        coins, value = [], 0.0
        for sym, (_asset, name) in COIN.items():
            st_ = s.get("symbols", {}).get(sym, {})
            units, px = st_.get("units", 0.0), prices.get(sym) or 0.0
            value += units * px
            coins.append((name, units, px, st_))
        total = eur + value
        age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(s["updated"])).total_seconds() / 60
        st.markdown(
            f"{'🟢' if age < 12 else '🟠'} Stand {pd.Timestamp(s['updated']).tz_convert('Europe/Vienna'):%H:%M:%S} "
            f"(vor {age:.0f} min)"
        )
        c = st.columns(4)
        c[0].markdown(
            _card(
                "GESAMTWERT",
                f"{total:,.2f} €",
                f"Obergrenze Bot {s.get('budget_eur', 0):.2f} € (kein Kontostand)",
            ),
            unsafe_allow_html=True,
        )
        c[1].markdown(
            _card("EURO", f"{eur:,.2f} €", f"davon {eur_held:,.2f} € in offenen Kauf-Orders"),
            unsafe_allow_html=True,
        )
        for col, (name, units, px, st_) in zip(c[2:], coins, strict=True):
            if units > 0:
                pnl = units * (px - st_.get("entry_price", 0))
                sub = f"Wert {units * px:,.2f} € · {pnl:+.2f} € · Stop {st_.get('stop_price', 0):,.0f} €"
                col.markdown(
                    _card(name, f"{units:.6f}", sub, GREEN if pnl >= 0 else RED),
                    unsafe_allow_html=True,
                )
            elif st_.get("buy_txid"):
                col.markdown(
                    _card(
                        name,
                        "Kauf-Order offen",
                        f"{st_.get('buy_eur', 0):.2f} € · Kurs {px:,.2f} €",
                    ),
                    unsafe_allow_html=True,
                )
            else:
                col.markdown(_card(name, "–", "keine Position"), unsafe_allow_html=True)
        _real_charts(cfg, s, eur)

    real()


def _buy_times(cfg: PlatformConfig) -> dict[str, pd.Timestamp]:
    """Zeitpunkt des letzten Kaufs je Coin aus dem Protokoll des Echtgeld-Bots."""
    log = cfg.home / "live" / "live.log"
    out: dict[str, pd.Timestamp] = {}
    for line in log.read_text().splitlines() if log.exists() else []:
        if "[KAUF]" in line:
            sym = line.split("[KAUF]")[1].split()[0]
            out[sym] = pd.Timestamp(line[:19], tz="UTC")
    return out


def _real_charts(cfg: PlatformConfig, s: dict, eur: float) -> None:
    from tradikg_bot.dashboard import cockpit

    held = {sym: v for sym, v in s.get("symbols", {}).items() if v.get("units", 0) > 0}
    if not held:
        return
    since = min(_buy_times(cfg).values(), default=pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1))
    frames = []
    for sym, v in held.items():  # Wert je Stunde = Menge × Schlusskurs (+ aktueller Kurs am Ende)
        b = cockpit._bars(cfg, sym, "1h", int((since - pd.Timedelta(hours=1)).timestamp() * 1000))
        b = pd.concat(
            [
                b,
                pd.DataFrame(
                    {
                        "ts": [pd.Timestamp(s["updated"])],
                        "close": [s["prices"].get(sym) or b["close"].iloc[-1]],
                    }
                ),
            ]
        )
        frames.append(b.set_index("ts")["close"].rename(sym) * v["units"])
    value = pd.concat(frames, axis=1).sort_index().ffill().dropna().sum(axis=1) + eur
    curve = value[value.index >= since - pd.Timedelta(hours=1)].reset_index()
    curve.columns = ["ts", "wert"]
    start = s.get("budget_eur", 0.0)
    left, right = st.columns([2, 3])
    with left:
        st.markdown("**Wert deines Echtgelds seit dem Kauf**")
        if len(curve) > 1:
            line = (
                alt.Chart(curve)
                .mark_line(point=True, color=GREEN if curve["wert"].iloc[-1] >= start else RED)
                .encode(
                    x=alt.X("ts:T", title=None),
                    y=alt.Y("wert:Q", title="€", scale=alt.Scale(zero=False)),
                    tooltip=[
                        alt.Tooltip("ts:T", title="Zeit"),
                        alt.Tooltip("wert:Q", title="Wert", format=",.2f"),
                    ],
                )
            )
            ref = (
                alt.Chart(pd.DataFrame({"y": [start]}))
                .mark_rule(color=GREY, strokeDash=[4, 4])
                .encode(y="y:Q")
            )
            st.altair_chart((line + ref).properties(height=240), use_container_width=True)
            st.caption(
                f"gestrichelt: eingesetzte {start:.2f} € · Stunden-Schlusskurse, letzter Punkt live"
            )
        else:
            st.caption("Verlauf erscheint mit der nächsten vollen Stunde.")
    with right:
        cols = st.columns(len(held))
        for col, (sym, v) in zip(cols, held.items(), strict=True):
            bars = cockpit._bars(
                cfg,
                sym,
                "1h",
                int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)).timestamp() * 1000),
            )
            if bars.empty:
                continue
            lines = [("Kaufpreis", v["entry_price"], cockpit.LIMIT, "solid")]
            if v.get("stop_price"):
                lines.append(("Stop bei Kraken", v["stop_price"], cockpit.STOP, "solid"))
            col.altair_chart(
                cockpit._price_chart(sym, bars, lines).properties(height=240),
                use_container_width=True,
            )


def render_solana(cfg: PlatformConfig) -> None:
    """Solana-Copy mit echten Jupiter-Kursen (Schattenbetrieb bzw. später Echtgeld)."""
    st.markdown("### 💸 Solana-Copy · echte Kurse")

    @st.fragment(run_every=10)
    def sol() -> None:
        path = cfg.home / "copylive" / "snapshot.json"
        if not path.exists():
            st.info("Noch nicht gestartet: `tradikg copy solana run`")
            return
        s = json.loads(path.read_text())
        eur = s.get("sol_eur") or 0.0
        eq, cap = s["equity_sol"], s["capital_sol"]
        pnl = eq - cap
        age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(s["updated"])).total_seconds()
        real = s.get("mode") == "echtgeld"
        st.markdown(
            f"{'🟢 LIVE' if age < 60 else f'🟠 seit {age / 60:.0f} min kein Durchgang'} · "
            f"{'**ECHTGELD**' if real else '**SCHATTENBETRIEB** – echte Wallets und Kurse, keine Orders'}"
            f" · gestartet {pd.Timestamp(s['start']).tz_convert('Europe/Vienna'):%d.%m. %H:%M}"
        )
        c = st.columns(4)
        c[0].markdown(
            _card("WERT", f"{eq:.4f} SOL", f"≈ {eq * eur:,.2f} € · Start {cap:.4f} SOL"),
            unsafe_allow_html=True,
        )
        trades = s.get("trades", [])
        wins = sum(t["pnl_sol"] > 0 for t in trades)
        c[1].markdown(
            _card(
                "ERGEBNIS",
                f"{pnl:+.4f} SOL",
                f"{pnl / cap:+.1%} ≈ {pnl * eur:+.2f} € · {len(trades)} Trades, {wins} Gewinne",
                GREEN if pnl > 0 else RED if pnl < 0 else "",
            ),
            unsafe_allow_html=True,
        )
        c[2].markdown(
            _card(
                "FOLGT JETZT",
                f"{len(s['leaders'])} Wallets",
                " · ".join(w[:6] + "…" for w in s["leaders"]) or "suche …",
            ),
            unsafe_allow_html=True,
        )
        skips = sum(e["text"].startswith("[SKIP]") for e in s.get("feed", []))
        c[3].markdown(
            _card("SCHUTZ", f"{skips} abgelehnt", "Rug-/Honeypot-/Liquiditäts-Prüfung"),
            unsafe_allow_html=True,
        )
        left, right = st.columns([3, 2])
        curve = pd.DataFrame(s.get("curve", []), columns=["ts", "sol"])
        with left:
            if len(curve) > 1:
                curve["ts"] = pd.to_datetime(curve["ts"], utc=True)
                st.altair_chart(
                    alt.Chart(curve)
                    .mark_line(color=GREEN if pnl >= 0 else RED, strokeWidth=2)
                    .encode(
                        x=alt.X("ts:T", title=None),
                        y=alt.Y("sol:Q", title="SOL", scale=alt.Scale(zero=False)),
                    )
                    .properties(height=240),
                    use_container_width=True,
                )
        with right:
            st.markdown(
                "<div style='max-height:260px;overflow-y:auto'>"
                f"{_feed_html(s.get('feed', []), 'SOL', 4)}</div>",
                unsafe_allow_html=True,
            )
        if s.get("holdings"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Token": h["mint"][:10] + "…",
                            "von": h["leader"][:6] + "…",
                            "seit": f"{pd.Timestamp(h['since']).tz_convert('Europe/Vienna'):%H:%M:%S}",
                            "Einsatz SOL": round(h["cost_sol"], 4),
                            "Wert SOL": None
                            if h["value_sol"] is None
                            else round(h["value_sol"], 4),
                        }
                        for h in s["holdings"]
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )

    sol()


def render(cfg: PlatformConfig) -> None:
    render_real(cfg)
    st.divider()
    render_solana(cfg)
    st.divider()
    st.markdown("### 🤖 Copy-Bot · LIVE · virtuell")
    st.caption(
        "Folgt automatisch den 3 besten aktiven Tradern **aller Blockchains** (Solana, Base, BSC, …). "
        "Rangliste alle 10 min; gewechselt wird, sobald ein anderer Trader mindestens 25 % besser "
        "dasteht (Score = ROI mit Abschlag für wenige Runden). Kopiert zum Kurs nach 100 s "
        "Verzögerung (so spät sieht unser Sammler die Trades) inkl. DEX-, Bot- und Netzgebühr. "
        "Kein echtes Geld."
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
        alt_path = cfg.data_dir / "copy_live_v1.json"
        if alt_path.exists():
            a = json.loads(alt_path.read_text())
            st.caption(
                f"Vergleich gleiche Daten, alte Taktik (beste ROI, alle 30 min): {a['balance']:,.2f} USD "
                f"({a['pnl']:+,.2f} USD, {a['trades']} Trades) – neue Taktik: {bal:,.2f} USD."
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
