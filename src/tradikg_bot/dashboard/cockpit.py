"""Cockpit: alles auf einen Blick – Echtgeld-Bot, Paper-Bot, Copy-Sammler, Kurse mit Orders."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from tradikg_bot.dashboard import queries as q
from tradikg_bot.platform_config import PlatformConfig

BOT, LIMIT, STOP, GRID, TREND = "#2a78d6", "#1f9d55", "#d64545", "#8a8985", "#b7791f"
SYMBOLS = ("XBTEUR", "ETHEUR")


def _alive(pid_file: Path) -> int | None:
    try:
        pid = int(pid_file.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _bars(cfg: PlatformConfig, sym: str, tf: str, since_ms: int = 0) -> pd.DataFrame:
    df = q._df(
        cfg.market_db,
        "SELECT ts, close FROM bars WHERE symbol = ? AND timeframe = ? AND ts >= ? ORDER BY ts",
        (sym, tf, since_ms),
    )
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def _live(cfg: PlatformConfig) -> tuple[dict, dict[str, float], list[str]]:
    root = cfg.home / "live"
    state_file, log_file = root / "trend_state.json", root / "live.log"
    state = json.loads(state_file.read_text()).get("symbols", {}) if state_file.exists() else {}
    log = log_file.read_text().splitlines() if log_file.exists() else []
    limits: dict[str, float] = {}  # txid → Limitpreis aus dem Protokoll
    for line in log:
        m = re.search(r"\[ORDER\] \w+ \w+ [\d.]+ @ ([\d.]+) → (\S+)", line)
        if m:
            limits[m.group(2)] = float(m.group(1))
    return state, limits, log[-8:]


def _grid_state(cfg: PlatformConfig) -> dict | None:
    df = q._df(q.paper_db(cfg), "SELECT value FROM engine_kv WHERE key = 'grid_state'")
    return json.loads(df.iloc[0, 0]) if not df.empty else None


def _price_chart(sym: str, bars: pd.DataFrame, lines: list[tuple[str, float, str, str]]):
    base = (
        alt.Chart(bars)
        .mark_line(color=BOT, strokeWidth=2)
        .encode(
            x=alt.X("ts:T", title=None),
            y=alt.Y("close:Q", title="EUR", scale=alt.Scale(zero=False)),
            tooltip=[
                alt.Tooltip("ts:T", title="Zeit"),
                alt.Tooltip("close:Q", title="Kurs", format=",.2f"),
            ],
        )
    )
    if not lines:
        return base
    rules = pd.DataFrame(lines, columns=["label", "y", "color", "dash"])
    layer = (
        alt.Chart(rules)
        .mark_rule(strokeWidth=1.5)
        .encode(
            y="y:Q",
            color=alt.Color(
                "label:N",
                scale=alt.Scale(domain=list(rules["label"]), range=list(rules["color"])),
                legend=alt.Legend(title=None, orient="bottom"),
            ),
            strokeDash=alt.StrokeDash(
                "dash:N",
                scale=alt.Scale(
                    domain=["solid", "dashed", "dotted"], range=[[1, 0], [8, 4], [2, 3]]
                ),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("label:N", title=""),
                alt.Tooltip("y:Q", title="Preis", format=",.2f"),
            ],
        )
    )
    return (base + layer).properties(title=sym, height=260)


def render(cfg: PlatformConfig) -> None:
    now = pd.Timestamp.now(tz="Europe/Vienna")
    st.caption(f"Stand {now:%d.%m.%Y %H:%M} (Wien) · „Aktualisieren“ oben lädt neu")
    live_pid = _alive(cfg.home / "live" / "live.pid")
    paper_pid = _alive(cfg.run_dir / "paper.pid")
    copy_pid = _alive(cfg.run_dir / "copy_collect.pid")
    state, limits, live_log = _live(cfg)
    last = {s: _bars(cfg, s, "1h").tail(1) for s in SYMBOLS}
    price = {s: float(b["close"].iloc[-1]) for s, b in last.items() if not b.empty}

    # -- Status-Leiste ------------------------------------------------------------------------
    c = st.columns(3)
    c[0].metric(
        "💶 Echtgeld-Bot (Kraken)",
        "läuft ✅" if live_pid else "aus ⛔",
        help="Trendfolge, echtes Geld, Stop liegt bei Kraken",
    )
    c[1].metric(
        "🧪 Paper-Bot (virtuell)",
        "läuft ✅" if paper_pid else "aus ⛔",
        help=f"Modus: {cfg.strategy_mode}, {cfg.timeframe}",
    )
    c[2].metric(
        "🔎 Copy-Sammler",
        "läuft ✅" if copy_pid else "aus ⛔",
        help="sammelt öffentliche Solana-Trades für den Simulator",
    )

    # -- Echtgeld ------------------------------------------------------------------------------
    st.subheader("💶 Echtgeld-Bot")
    rows, invested, pnl = [], 0.0, 0.0
    for sym in SYMBOLS:
        s = state.get(sym, {})
        units, entry, px = s.get("units", 0.0), s.get("entry_price", 0.0), price.get(sym, 0.0)
        value = units * px
        invested += value
        pnl += units * (px - entry) if units else 0.0
        if units > 0:
            status = f"gekauft · Stop {s.get('stop_price', 0):,.2f} €"
        elif s.get("buy_txid"):
            lim = limits.get(s["buy_txid"], 0)
            status = f"Kauf-Limit offen @ {lim:,.2f} € ({s.get('buy_eur', 0):.2f} €)"
        elif s.get("blocked"):
            status = "gesperrt nach Notfall-Stop"
        else:
            status = "wartet (unter 200-Tage-Linie)" if s else "noch nicht gestartet"
        rows.append(
            {
                "Coin": sym,
                "Status": status,
                "Menge": f"{units:.8f}",
                "Kurs jetzt": f"{px:,.2f} €",
                "Wert": f"{value:,.2f} €",
                "Ergebnis offen": f"{units * (px - entry):+.2f} €" if units else "–",
                "realisiert": f"{s.get('realized_eur', 0):+.2f} €",
            }
        )
    m = st.columns(3)
    m[0].metric("In Coins investiert", f"{invested:,.2f} €")
    m[1].metric("Ergebnis offen (geschätzt)", f"{pnl:+.2f} €")
    m[2].metric("Realisiert", f"{sum(v.get('realized_eur', 0) for v in state.values()):+.2f} €")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # -- Kurse mit allen Orders ----------------------------------------------------------------
    st.subheader("📈 Kurse der letzten 7 Tage mit Orders und Grenzen")
    grid = _grid_state(cfg) if cfg.strategy_mode == "grid" else None
    since = int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)).timestamp() * 1000)
    cols = st.columns(2)
    for col, sym in zip(cols, SYMBOLS, strict=True):
        bars = _bars(cfg, sym, "1h", since)
        if bars.empty:
            col.info(f"{sym}: keine Kursdaten")
            continue
        lines: list[tuple[str, float, str, str]] = []
        daily = _bars(cfg, sym, "1d").tail(200)
        if len(daily) == 200:
            lines.append(
                ("200-Tage-Linie (Trendgrenze)", float(daily["close"].mean()), TREND, "dashed")
            )
        s = state.get(sym, {})
        if s.get("buy_txid") and s["buy_txid"] in limits:
            lines.append(("Echtgeld: Kauf-Limit", limits[s["buy_txid"]], LIMIT, "solid"))
        if s.get("units", 0) > 0 and s.get("stop_price"):
            lines.append(("Echtgeld: Stop bei Kraken", s["stop_price"], STOP, "solid"))
        if grid and sym in grid.get("refs", {}):
            ref = grid["refs"][sym]
            held = grid.get("lots", {}).get(sym, {})
            for k in range(1, 6):
                lvl = ref * (1 - 0.03 * k)
                if bars["close"].min() * 0.9 <= lvl:
                    tag = "gekauft" if str(k) in held else "Kauf-Stufe"
                    lines.append((f"Paper-Grid {tag} {k}", lvl, GRID, "dotted"))
        col.altair_chart(_price_chart(sym, bars, lines), use_container_width=True)

    # -- Paper-Bot -----------------------------------------------------------------------------
    st.subheader(f"🧪 Paper-Bot (virtuell, {cfg.strategy_mode})")
    eq = q.equity_curve(cfg).reset_index()
    trades = q._df(
        q.paper_db(cfg), "SELECT payload FROM trade_analysis ORDER BY trade_id DESC LIMIT 50"
    )
    tlist = [json.loads(p) for p in trades["payload"]] if not trades.empty else []
    m = st.columns(4)
    cur = float(eq["equity"].iloc[-1]) if not eq.empty else cfg.capital_eur
    m[0].metric("Guthaben", f"{cur:,.2f} €", f"{cur - cfg.capital_eur:+.2f} €")
    m[1].metric("Abgeschlossene Trades", len(tlist))
    m[2].metric("Offene Positionen", int(eq["open_positions"].iloc[-1]) if not eq.empty else 0)
    m[3].metric(
        "Gewinn-Trades", f"{sum(t.get('net_pnl_eur', 0) > 0 for t in tlist)} / {len(tlist)}"
    )
    if len(eq) > 1:
        st.altair_chart(
            alt.Chart(eq)
            .mark_line(color=BOT, strokeWidth=2)
            .encode(
                x=alt.X("ts:T", title=None),
                y=alt.Y("equity:Q", title="Guthaben EUR", scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("ts:T", title="Zeit"), alt.Tooltip("equity:Q", format=",.2f")],
            ),
            use_container_width=True,
        )
    if tlist:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Ausstieg": t["exit_ts"][:16],
                        "Coin": t["asset"],
                        "Einstieg": f"{t['entry_price']:,.2f}",
                        "Ausstieg-Kurs": f"{t['exit_price']:,.2f}",
                        "Grund": t.get("exit_reason"),
                        "Netto": f"{t['net_pnl_eur']:+.2f} €",
                    }
                    for t in tlist[:10]
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Noch keine abgeschlossenen Trades – der Bot wartet auf seine Einstiegspreise.")

    # -- Copy-Trading ---------------------------------------------------------------------------
    st.subheader("🔎 Copy-Trading-Simulator (virtuell)")
    stats = q._df(
        cfg.data_dir / "copy.sqlite",
        "SELECT COUNT(*) n, COUNT(DISTINCT wallet) w, COUNT(DISTINCT pool) p, MAX(ts) t "
        "FROM trades",
    )
    m = st.columns(4)
    if not stats.empty:
        r = stats.iloc[0]
        m[0].metric("Gesammelte Trades", f"{int(r['n']):,}".replace(",", "."))
        m[1].metric("Wallets", f"{int(r['w']):,}".replace(",", "."))
        m[2].metric("Pools", int(r["p"]))
        m[3].metric("Letzter Trade (UTC)", str(r["t"])[11:16] if r["t"] else "–")
    latest = cfg.home / "reports" / "copy-latest.json"
    if latest.exists():
        s = json.loads(latest.read_text())
        st.caption(
            f"Letzte Auswertung {s['created'][:16]} UTC: {s['capital']:.2f} $ → "
            f"**{s['final']:.2f} $** "
            f"({s['trades']} kopierte Trades, {s['followed']} Trader gefolgt). "
            "Neue Auswertung: Tab „▶ Testdurchlauf“ → „Copy-Trading auswerten“."
        )

    with st.expander("Protokoll Echtgeld-Bot (letzte Einträge)"):
        st.code("\n".join(live_log) or "–")
