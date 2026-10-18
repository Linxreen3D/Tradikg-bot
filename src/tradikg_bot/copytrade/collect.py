"""Sammler für öffentliche DEX-Trades (GeckoTerminal-API, ohne Schlüssel).

Je Durchgang: trendende Solana-Pools abrufen und je Pool die letzten (bis zu 300) Trades mit
Wallet-Adresse speichern. Doppelte Trades (gleicher tx_hash) werden ignoriert. Liest nur
öffentliche Daten, sendet nichts, besitzt keine Wallet.

Grenze: Pro Abruf liefert ein Pool nur die letzten 300 Trades – bei sehr aktiven Pools fehlen
zwischen zwei Abrufen Trades. Die Abdeckung wird im Bericht ausgewiesen.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

API = "https://api.geckoterminal.com/api/v2/networks/solana"
UA = {
    "User-Agent": "tradikg-bot/0.1 (copy-trading simulator, read-only)",
    "Accept": "application/json",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
    pool TEXT PRIMARY KEY, token TEXT, name TEXT, dex TEXT, reserve_usd REAL,
    created_at TEXT, first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    tx_hash TEXT, pool TEXT, token TEXT, ts TEXT, wallet TEXT, kind TEXT,
    token_amount REAL, price_usd REAL, volume_usd REAL,
    PRIMARY KEY (tx_hash, pool)
);
CREATE INDEX IF NOT EXISTS trades_ts ON trades (ts);
CREATE INDEX IF NOT EXISTS trades_wallet ON trades (wallet, ts);
CREATE TABLE IF NOT EXISTS polls (
    ts TEXT, pool TEXT, n INTEGER, oldest TEXT, newest TEXT, overlap INTEGER
);
"""


def http_json(url: str, retries: int = 3, wait: float = 20.0) -> Any:
    """GET mit Rücksicht auf das öffentliche Limit: bei HTTP 429 warten und erneut versuchen."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == retries:
                raise
            time.sleep(wait * (attempt + 1))
    raise RuntimeError("unerreichbar")


class CopyStore:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def save_pool(self, pool: dict[str, Any], now: str) -> None:
        self.conn.execute(
            """INSERT INTO pools VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(pool) DO UPDATE SET reserve_usd=excluded.reserve_usd,
               last_seen=excluded.last_seen, name=excluded.name""",
            (
                pool["pool"],
                pool["token"],
                pool["name"],
                pool["dex"],
                pool["reserve_usd"],
                pool["created_at"],
                now,
                now,
            ),
        )

    def save_trades(self, rows: list[tuple]) -> int:
        before = self.conn.total_changes
        self.conn.executemany("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?)", rows)
        return self.conn.total_changes - before


def parse_pool(raw: dict[str, Any]) -> dict[str, Any]:
    a, rel = raw["attributes"], raw["relationships"]
    return {
        "pool": a["address"],
        "token": rel["base_token"]["data"]["id"].split("_", 1)[-1],
        "name": a.get("name", ""),
        "dex": rel["dex"]["data"]["id"],
        "reserve_usd": float(a.get("reserve_in_usd") or 0),
        "created_at": a.get("pool_created_at") or "",
    }


def parse_trades(pool: dict[str, Any], raw: list[dict[str, Any]]) -> list[tuple]:
    """Nur Trades des Basis-Tokens gegen die Quote (Kauf = Token erhalten, Verkauf = abgegeben)."""
    rows = []
    for t in raw:
        a = t["attributes"]
        buy = a["kind"] == "buy"
        token_addr = a["to_token_address"] if buy else a["from_token_address"]
        if token_addr != pool["token"]:
            continue
        amount = float(a["to_token_amount"] if buy else a["from_token_amount"])
        price = float(a["price_to_in_usd"] if buy else a["price_from_in_usd"])
        rows.append(
            (
                a["tx_hash"],
                pool["pool"],
                pool["token"],
                a["block_timestamp"],
                a["tx_from_address"],
                a["kind"],
                amount,
                price,
                float(a["volume_in_usd"] or 0),
            )
        )
    return rows


def _log(msg: str) -> None:
    print(msg, flush=True)  # sofort ins Log (auch im Hintergrund mit nohup)


def collect_once(
    store: CopyStore,
    fetch: Callable[[str], Any] = http_json,
    pause: float = 6.0,
    log: Callable[[str], None] = _log,
) -> int:
    """Ein Durchgang. ``pause`` hält das öffentliche Limit (≈ 30 Abrufe/Minute) ein."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    pools = [parse_pool(p) for p in fetch(f"{API}/trending_pools")["data"]]
    new = 0
    for pool in pools:
        time.sleep(pause)
        try:
            raw = fetch(f"{API}/pools/{pool['pool']}/trades")["data"]
        except Exception as exc:  # einzelner Pool fehlgeschlagen – nächster Durchgang
            log(f"Pool {pool['name']}: {exc}")
            continue
        store.save_pool(pool, now)
        rows = parse_trades(pool, raw)
        added = store.save_trades(rows)
        new += added
        if rows:
            store.conn.execute(
                "INSERT INTO polls VALUES (?,?,?,?,?,?)",
                (
                    now,
                    pool["pool"],
                    len(rows),
                    min(r[3] for r in rows),
                    max(r[3] for r in rows),
                    int(added < len(rows)),
                ),
            )
    log(f"{now}: {len(pools)} Pools, {new} neue Trades")
    return new
