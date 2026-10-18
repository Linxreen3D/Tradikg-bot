"""Live-Betrieb des Copy-Simulators (virtuell): nach jedem Sammel-Durchgang ab festem Startzeitpunkt
neu rechnen und den Stand als JSON für das Dashboard schreiben. Deterministisch: dieselben Daten
ergeben denselben Stand – kein Zustand, der auseinanderlaufen kann."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from tradikg_bot.copytrade.simulate import CopyConfig, load_trades, simulate_copy


def run_live(db: Path, out: Path, cfg: CopyConfig | None = None) -> dict:
    cfg = cfg or CopyConfig()
    state = json.loads(out.read_text()) if out.exists() else {}
    start = pd.Timestamp(state.get("start") or pd.Timestamp.now(tz="UTC").floor("min"))
    since = (start - pd.Timedelta(hours=cfg.lookback_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    trades, pools = load_trades(db, since)
    base = {
        "start": start.isoformat(),
        "capital": cfg.capital,
        "updated": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    if trades.empty or trades["ts"].iloc[-1] <= start:
        snap = {
            **base,
            "balance": cfg.capital,
            "pnl": 0.0,
            "trades": 0,
            "wins": 0,
            "leaders": [],
            "followed": [],
            "open": [],
            "feed": [],
            "curve": [],
            "losses": {},
        }
        out.write_text(json.dumps(snap))
        return snap
    res = simulate_copy(trades, pools, cfg, start=start, close_at_end=False)
    eq = res.equity
    if len(eq) > 600:  # für das Dashboard ausdünnen
        eq = eq.iloc[:: max(1, len(eq) // 600)]
    balance = float(res.equity.iloc[-1])
    snap = {
        **base,
        "balance": balance,
        "pnl": balance - cfg.capital,
        "trades": len(res.trades),
        "wins": sum(t["pnl_usd"] > 0 for t in res.trades),
        "leaders": res.leaders,
        "losses": res.losses,
        "followed": [
            {k: (str(v) if isinstance(v, pd.Timestamp) else v) for k, v in f.items()}
            for f in res.followed
        ],
        "open": [{**o, "since": str(o["since"])} for o in res.open_positions],
        "feed": [{"ts": str(e["ts"]), "text": e["text"], "pnl": e["pnl"]} for e in res.feed[-80:]],
        "curve": [[str(t), float(v)] for t, v in eq.items()],
        "fire_after": cfg.fire_after,
        "delay_s": cfg.delay_s,
    }
    out.write_text(json.dumps(snap, default=float))
    return snap
