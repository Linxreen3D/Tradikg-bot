"""Deterministischer Fake der öffentlichen Kraken-API (Antwortformat wie offiziell dokumentiert)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradikg_bot.live.kraken.client import KrakenUnknownOutcomeError

PAIRS = {
    "XXBTZEUR": {
        "altname": "XBTEUR",
        "base": "XXBT",
        "quote": "ZEUR",
        "lot_decimals": 8,
        "pair_decimals": 1,
        "tick_size": "0.1",
        "ordermin": "0.00005",
        "costmin": "0.5",
        "status": "online",
    },
    "XETHZEUR": {
        "altname": "ETHEUR",
        "base": "XETH",
        "quote": "ZEUR",
        "lot_decimals": 8,
        "pair_decimals": 2,
        "tick_size": "0.01",
        "ordermin": "0.01",
        "costmin": "0.5",
        "status": "online",
    },
    "SOLEUR": {
        "altname": "SOLEUR",
        "base": "SOL",
        "quote": "ZEUR",
        "lot_decimals": 8,
        "pair_decimals": 2,
        "tick_size": "0.01",
        "ordermin": "0.02",
        "costmin": "0.5",
        "status": "online",
    },
    "ILLIQEUR": {
        "altname": "ILLIQEUR",
        "base": "ILLIQ",
        "quote": "ZEUR",
        "lot_decimals": 8,
        "pair_decimals": 4,
        "tick_size": "0.0001",
        "ordermin": "1",
        "costmin": "0.5",
        "status": "online",
    },
    "USDTZEUR": {
        "altname": "USDTEUR",
        "base": "USDT",
        "quote": "ZEUR",
        "lot_decimals": 8,
        "pair_decimals": 4,
        "tick_size": "0.0001",
        "ordermin": "5",
        "costmin": "0.5",
        "status": "online",
    },
    "XXBTZUSD": {
        "altname": "XBTUSD",
        "base": "XXBT",
        "quote": "ZUSD",
        "lot_decimals": 8,
        "pair_decimals": 1,
        "tick_size": "0.1",
        "ordermin": "0.00005",
        "costmin": "0.5",
        "status": "online",
    },
    "DELISTEUR": {
        "altname": "DELISTEUR",
        "base": "DELIST",
        "quote": "ZEUR",
        "lot_decimals": 8,
        "pair_decimals": 4,
        "tick_size": "0.0001",
        "ordermin": "1",
        "costmin": "0.5",
        "status": "cancel_only",
    },
}
TICKER = {
    "XXBTZEUR": (60000.0, 60010.0, 150.0),  # bid, ask, 24h-Volumen (Basis)
    "XETHZEUR": (3000.0, 3000.5, 4000.0),
    "SOLEUR": (150.0, 150.05, 40000.0),
    "ILLIQEUR": (1.0, 1.05, 1000.0),  # Spread ~ 490 bps
    "USDTZEUR": (0.92, 0.9201, 1e7),
}


class FakeKrakenPublic:
    def __init__(
        self, now: pd.Timestamp, interval_minutes: int = 60, n: int = 300, seed: int = 3
    ) -> None:
        self.now = now
        self.calls: list[tuple[str, dict]] = []
        self.fail_next = 0
        rng = np.random.default_rng(seed)
        step = pd.Timedelta(minutes=interval_minutes)
        # letzte Kerze beginnt in der laufenden Periode (unvollständig)
        last_open = now.floor(step)
        self.opens = pd.date_range(end=last_open, periods=n, freq=step)
        self.close = 60000 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))

    def public(self, endpoint, params=None):
        self.calls.append((endpoint, dict(params or {})))
        if self.fail_next:
            self.fail_next -= 1
            raise KrakenUnknownOutcomeError("Timeout (simuliert)")
        if endpoint == "AssetPairs":
            return PAIRS
        if endpoint == "Ticker":
            out = {}
            for key, (bid, ask, vol) in TICKER.items():
                if PAIRS[key]["altname"] in params["pair"].split(","):
                    vwap = (bid + ask) / 2
                    out[key] = {
                        "a": [str(ask), "1", "1"],
                        "b": [str(bid), "1", "1"],
                        "c": [str(bid), "0.1"],
                        "v": ["1", str(vol)],
                        "p": [str(vwap), str(vwap)],
                    }
            return out
        if endpoint == "OHLC":
            since = int(params.get("since", 0))
            rows = []
            for ts, c in zip(self.opens, self.close, strict=True):
                t = int(ts.timestamp())
                if t <= since:
                    continue
                o = c * 0.999
                rows.append(
                    [
                        t,
                        f"{o:.1f}",
                        f"{c * 1.003:.1f}",
                        f"{o * 0.997:.1f}",
                        f"{c:.1f}",
                        f"{c:.1f}",
                        "1.5",
                        10,
                    ]
                )
            return {"XXBTZEUR": rows, "last": int(self.opens[-1].timestamp())}
        raise AssertionError(f"unerwarteter Endpunkt {endpoint}")
