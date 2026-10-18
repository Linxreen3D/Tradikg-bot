"""Copy-Trading nachrechnen – nur mit gesammelten öffentlichen Trades, nur virtuell.

* **Auswahl** zu jedem Zeitpunkt ``t`` nur aus Trades in ``[t − lookback, t)``: abgeschlossene
  Runden (Kauf → Verkauf, FIFO je Token), Rendite (ROI), Trefferquote. Teilnahme: ≥ ``min_rounds``
  Runden, Ø ≥ ``min_trade_usd``, ≤ ``max_trades`` (Hochfrequenz-Bots raus), ROI > 0.
* **Kopieren:** Kauf des gefolgten Traders → wir kaufen ``position_frac`` des Guthabens zum ersten
  echten Preis im selben Pool **nach** ``delay_s`` Sekunden; sein Verkauf → wir verkaufen alles.
  Kosten: DEX-Gebühr, Copy-Bot-Gebühr, Netzgebühr, Preiseinfluss ≈ Ordergröße / (Pool-Reserve/2).
* **Feuern:** nach ``fire_after`` Verlusten in Folge → nächster der aktuellen Rangliste.
* Kein Blick in die Zukunft: Preise stammen aus Trades nach dem jeweiligen Zeitpunkt + Verzögerung,
  die Auswahl nur aus Trades davor.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CopyConfig:
    capital: float = 100.0  # USD (virtuell)
    lookback_h: float = 3.0
    delay_s: float = 10.0
    position_frac: float = 0.10
    max_positions: int = 5
    max_hold_h: float = 12.0
    fire_after: int = 2
    min_rounds: int = 3
    min_trade_usd: float = 20.0
    max_trades: int = 200
    dex_fee: float = 0.0025
    app_fee: float = 0.01
    network_usd: float = 0.01
    price_window_s: float = (
        600.0  # kein Preis innerhalb von 10 min nach Verzögerung → nicht kopieren
    )


def load_trades(db: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = sqlite3.connect(str(db))
    trades = pd.read_sql_query("SELECT * FROM trades", conn)
    pools = pd.read_sql_query("SELECT * FROM pools", conn)
    conn.close()
    trades["ts"] = pd.to_datetime(trades["ts"], utc=True)
    return trades.sort_values("ts").reset_index(drop=True), pools


def wallet_stats(trades: pd.DataFrame) -> pd.DataFrame:
    """Abgeschlossene Runden je Wallet (FIFO je Token) aus den übergebenen Trades."""
    rows = []
    for wallet, g in trades.groupby("wallet", sort=False):
        pnl = cost = 0.0
        wins = rounds = 0
        for _, t in g.groupby("token", sort=False):
            lots: deque[list[float]] = deque()
            for kind, qty, px in zip(t["kind"], t["token_amount"], t["price_usd"], strict=True):
                if kind == "buy":
                    lots.append([qty, px])
                    continue
                left, r_pnl, r_cost = qty, 0.0, 0.0
                while left > 1e-12 and lots:
                    q = min(left, lots[0][0])
                    r_pnl += q * (px - lots[0][1])
                    r_cost += q * lots[0][1]
                    lots[0][0] -= q
                    left -= q
                    if lots[0][0] <= 1e-12:
                        lots.popleft()
                if r_cost > 0:
                    rounds += 1
                    wins += r_pnl > 0
                    pnl += r_pnl
                    cost += r_cost
        rows.append(
            {
                "wallet": wallet,
                "trades": len(g),
                "avg_usd": float(g["volume_usd"].mean()),
                "rounds": rounds,
                "win_rate": wins / rounds if rounds else 0.0,
                "pnl_usd": pnl,
                "roi": pnl / cost if cost else 0.0,
            }
        )
    return pd.DataFrame(rows)


def rank_wallets(
    trades: pd.DataFrame, cfg: CopyConfig, exclude: set[str] = frozenset()
) -> pd.DataFrame:
    st = wallet_stats(trades)
    if st.empty:
        return st
    ok = (
        (st["rounds"] >= cfg.min_rounds)
        & (st["avg_usd"] >= cfg.min_trade_usd)
        & (st["trades"] <= cfg.max_trades)
        & (st["roi"] > 0)
        & ~st["wallet"].isin(exclude)
    )
    return st[ok].sort_values(["roi", "win_rate"], ascending=False).reset_index(drop=True)


@dataclass
class Position:
    token: str
    pool: str
    units: float
    cost_usd: float
    entry_ts: pd.Timestamp
    wallet: str


@dataclass
class CopyResult:
    cfg: CopyConfig
    equity: pd.Series
    trades: list[dict] = field(default_factory=list)
    feed: list[dict] = field(default_factory=list)
    followed: list[dict] = field(default_factory=list)
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    note: str = ""

    @property
    def final(self) -> float:
        return float(self.equity.iloc[-1]) if len(self.equity) else self.cfg.capital


class _Prices:
    """Erster echter Trade-Preis je Pool ab einem Zeitpunkt (aus den gesammelten Trades)."""

    def __init__(self, trades: pd.DataFrame) -> None:
        self.by_pool = {
            p: (g["ts"].dt.tz_convert(None).to_numpy(), g["price_usd"].to_numpy())
            for p, g in trades.groupby("pool", sort=False)
        }

    def after(
        self, pool: str, ts: pd.Timestamp, window_s: float
    ) -> tuple[float, pd.Timestamp] | None:
        if pool not in self.by_pool:
            return None
        t, p = self.by_pool[pool]
        i = int(np.searchsorted(t, np.datetime64(ts.tz_convert(None)), side="left"))
        if i >= len(t) or (pd.Timestamp(t[i]).tz_localize("UTC") - ts).total_seconds() > window_s:
            return None
        return float(p[i]), pd.Timestamp(t[i]).tz_localize("UTC")

    def last_before(self, pool: str, ts: pd.Timestamp) -> float | None:
        if pool not in self.by_pool:
            return None
        t, p = self.by_pool[pool]
        i = int(np.searchsorted(t, np.datetime64(ts.tz_convert(None)), side="right")) - 1
        return float(p[i]) if i >= 0 else None


def simulate_copy(
    trades: pd.DataFrame, pools: pd.DataFrame, cfg: CopyConfig | None = None
) -> CopyResult:
    cfg = cfg or CopyConfig()
    if trades.empty:
        raise ValueError("Keine Trades gesammelt – erst `tradikg copy collect` laufen lassen.")
    start = trades["ts"].iloc[0] + pd.Timedelta(hours=cfg.lookback_h)
    end = trades["ts"].iloc[-1]
    if start >= end:
        hours = (end - trades["ts"].iloc[0]).total_seconds() / 3600
        raise ValueError(
            f"Zu wenig Daten: {hours:.1f} h gesammelt, Rückblick braucht {cfg.lookback_h} h."
        )
    reserve = dict(zip(pools["pool"], pools["reserve_usd"], strict=True))
    prices = _Prices(trades)
    cash, positions = cfg.capital, {}
    res = CopyResult(cfg, pd.Series(dtype=float), start=start, end=end)
    fired: set[str] = set()
    losses = 0
    leader: str | None = None
    curve: dict[pd.Timestamp, float] = {start: cash}

    def cost_factor(usd: float, pool: str) -> float:
        impact = usd / max(reserve.get(pool, 0.0) / 2, 1.0)
        return min(impact, 0.5) + cfg.dex_fee + cfg.app_fee

    def pick(ts: pd.Timestamp) -> str | None:
        window = trades[
            (trades["ts"] >= ts - pd.Timedelta(hours=cfg.lookback_h)) & (trades["ts"] < ts)
        ]
        ranked = rank_wallets(window, cfg, fired)
        if ranked.empty:
            return None
        top = ranked.iloc[0]
        res.followed.append(
            {
                "wallet": top["wallet"],
                "since": ts,
                "roi": top["roi"],
                "win_rate": top["win_rate"],
                "rounds": int(top["rounds"]),
                "pnl_usd": 0.0,
                "copied": 0,
                "fired": False,
            }
        )
        res.feed.append(
            {
                "ts": ts,
                "text": f"Folge jetzt {top['wallet'][:6]}… (ROI {top['roi']:+.0%}, "
                f"{top['win_rate']:.0%} Treffer, {int(top['rounds'])} Runden)",
                "pnl": None,
            }
        )
        return str(top["wallet"])

    def close(pos: Position, ts: pd.Timestamp, why: str) -> None:
        nonlocal cash, losses, leader
        quote = prices.after(pos.pool, ts + pd.Timedelta(seconds=cfg.delay_s), cfg.price_window_s)
        px = quote[0] if quote else prices.last_before(pos.pool, ts) or 0.0
        gross = pos.units * px
        out = gross * (1 - cost_factor(gross, pos.pool)) - cfg.network_usd
        cash += max(out, 0.0)
        pnl = out - pos.cost_usd
        del positions[pos.token]
        res.trades.append(
            {
                "wallet": pos.wallet,
                "token": pos.token,
                "entry_ts": pos.entry_ts,
                "exit_ts": ts,
                "cost_usd": pos.cost_usd,
                "out_usd": out,
                "pnl_usd": pnl,
                "reason": why,
            }
        )
        res.feed.append({"ts": ts, "text": f"[EXIT] {pos.token[:6]}… ({why})", "pnl": pnl})
        for f in res.followed:
            if f["wallet"] == pos.wallet:
                f["pnl_usd"] += pnl
        if pos.wallet != leader:  # Positionen früherer Trader zählen nicht für die Feuer-Regel
            return
        losses = losses + 1 if pnl < 0 else 0
        if losses >= cfg.fire_after:
            fired.add(leader)
            res.followed[-1]["fired"] = True
            res.feed.append(
                {
                    "ts": ts,
                    "text": f"[FIRED] {leader[:6]}… nach {losses} Verlusten in Folge",
                    "pnl": None,
                }
            )
            losses = 0
            leader = None

    events = trades[trades["ts"] >= start]
    last_check = start - pd.Timedelta(minutes=5)  # erste Auswahl sofort beim Start
    for row in events.itertuples(index=False):
        ts = row.ts
        for pos in [
            p for p in positions.values() if ts - p.entry_ts > pd.Timedelta(hours=cfg.max_hold_h)
        ]:
            close(pos, pos.entry_ts + pd.Timedelta(hours=cfg.max_hold_h), "max. Haltedauer")
        if leader is None and ts - last_check >= pd.Timedelta(minutes=5):
            last_check = ts
            leader = pick(ts)
        if leader is None or row.wallet != leader:
            continue
        if row.kind == "buy" and row.token not in positions and len(positions) < cfg.max_positions:
            quote = prices.after(
                row.pool, ts + pd.Timedelta(seconds=cfg.delay_s), cfg.price_window_s
            )
            usd = cash * cfg.position_frac
            if quote is None or usd < 1.0:
                continue
            px, fill_ts = quote
            spend = usd - cfg.network_usd
            units = spend * (1 - cost_factor(spend, row.pool)) / px
            cash -= usd
            positions[row.token] = Position(row.token, row.pool, units, usd, fill_ts, leader)
            res.followed[-1]["copied"] += 1
            res.feed.append(
                {
                    "ts": fill_ts,
                    "text": f"[COPY] {leader[:6]}… kaufte {row.token[:6]}… · wir {usd:.2f} $",
                    "pnl": None,
                }
            )
        elif (
            row.kind == "sell"
            and row.token in positions
            and positions[row.token].wallet == row.wallet
        ):
            close(positions[row.token], ts, "Trader verkauft")
        held = sum(p.units * (prices.last_before(p.pool, ts) or 0.0) for p in positions.values())
        curve[ts] = cash + held
    for pos in list(positions.values()):  # am Ende zum letzten Preis abzüglich Kosten bewerten
        close(pos, end, "Ende der Daten")
    curve[end] = cash
    res.equity = pd.Series(curve).sort_index()
    return res


def coverage(db: str | Path) -> dict:
    """Wie vollständig wurden die Pools erfasst? (Abruf ohne Überlappung = Lücke wahrscheinlich)"""
    conn = sqlite3.connect(str(db))
    n, over = conn.execute("SELECT COUNT(*), COALESCE(SUM(overlap),0) FROM polls").fetchone()
    t0, t1, w, p = conn.execute(
        "SELECT MIN(ts), MAX(ts), COUNT(DISTINCT wallet), COUNT(DISTINCT pool) FROM trades"
    ).fetchone()
    conn.close()
    return {
        "polls": n,
        "complete_share": over / n if n else 0.0,
        "from": t0,
        "to": t1,
        "wallets": w,
        "pools": p,
    }
