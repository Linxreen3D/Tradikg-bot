from __future__ import annotations

import pandas as pd
import pytest

from tradikg_bot.copytrade.collect import CopyStore, collect_once, parse_trades
from tradikg_bot.copytrade.simulate import CopyConfig, rank_wallets, simulate_copy, wallet_stats

T0 = pd.Timestamp("2026-10-18 00:00", tz="UTC")
NO_COST = dict(dex_fee=0.0, app_fee=0.0, network_usd=0.0)


def tr(sec, wallet, kind, qty, px, pool="P1", token="TOK1"):
    return {
        "tx_hash": f"{wallet}{sec}{kind}{pool}",
        "pool": pool,
        "token": token,
        "ts": T0 + pd.Timedelta(seconds=sec),
        "wallet": wallet,
        "kind": kind,
        "token_amount": qty,
        "price_usd": px,
        "volume_usd": qty * px,
    }


def history(good="GOOD", bad="BAD"):
    """Erste Stunde: GOOD macht 3 Gewinn-Runden, BAD 3 Verlust-Runden (je 100 $)."""
    rows = []
    for k in range(3):
        s = 600 * k
        rows += [tr(s, good, "buy", 100, 1.0), tr(s + 60, good, "sell", 100, 1.2)]
        rows += [tr(s + 1, bad, "buy", 100, 1.0), tr(s + 61, bad, "sell", 100, 0.8)]
    return rows


def frame(rows):
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


POOLS = pd.DataFrame({"pool": ["P1", "P2", "P3"], "reserve_usd": [1e12, 1e12, 1e12]})


def test_wallet_stats_fifo_rounds():
    st = wallet_stats(
        frame(
            [tr(0, "W", "buy", 100, 1.0), tr(1, "W", "buy", 100, 2.0), tr(2, "W", "sell", 150, 3.0)]
        )
    ).iloc[0]
    assert st["rounds"] == 1 and st["pnl_usd"] == pytest.approx(100 * 2 + 50 * 1)
    assert st["roi"] == pytest.approx(250 / 200)


def test_ranking_excludes_losers_dust_and_bots():
    rows = history() + [
        tr(10 + i, "BOT", k, 100, 1.0 + 0.01 * i) for i in range(300) for k in ("buy",)
    ]
    rows += [
        tr(5000 + i, "DUST", k, 1, 1.0 + (k == "sell")) for i in range(3) for k in ("buy", "sell")
    ]
    ranked = rank_wallets(frame(rows), CopyConfig())
    assert list(ranked["wallet"]) == ["GOOD"]


def test_copy_uses_price_after_delay_and_costs():
    later = 3600  # direkt nach dem Rückblick von 1 h
    rows = history() + [
        tr(later, "GOOD", "buy", 100, 1.0),  # Trader kauft zu 1,0
        tr(later + 5, "X", "buy", 1, 1.05),  # vor Ablauf der Verzögerung
        tr(later + 12, "X", "buy", 1, 1.10),  # erster Preis nach 10 s → unser Kauf
        tr(later + 60, "GOOD", "sell", 100, 1.6),  # Trader verkauft
        tr(later + 75, "X", "sell", 1, 1.50),  # unser Verkauf
        tr(later + 9000, "X", "buy", 1, 1.5),
    ]
    res = simulate_copy(frame(rows), POOLS, CopyConfig(lookback_h=1, delay_s=10, **NO_COST))
    (t,) = res.trades
    assert t["cost_usd"] == pytest.approx(10.0)  # 10 % von 100 $
    assert t["out_usd"] == pytest.approx(10.0 / 1.10 * 1.50)
    assert res.final == pytest.approx(90 + 10 / 1.1 * 1.5)
    costly = simulate_copy(frame(rows), POOLS, CopyConfig(lookback_h=1, delay_s=10))
    assert costly.final < res.final  # Gebühren kosten


def test_fire_after_two_losses_then_next_trader():
    later = 3600
    rows = history() + [
        tr(s, "NEXT", k, 100, p)
        for s, k, p in (
            (2000, "buy", 1.0),
            (2060, "sell", 1.1),
            (2400, "buy", 1.0),
            (2460, "sell", 1.05),
            (2800, "buy", 1.0),
            (2860, "sell", 1.02),
        )
    ]
    for i, tok in enumerate(("A", "B")):
        s = later + 1000 * i
        rows += [
            tr(s, "GOOD", "buy", 100, 1.0, pool=f"P{i + 2}", token=tok),
            tr(s + 15, "X", "buy", 1, 1.0, pool=f"P{i + 2}", token=tok),
            tr(s + 60, "GOOD", "sell", 100, 0.5, pool=f"P{i + 2}", token=tok),
            tr(s + 75, "X", "sell", 1, 0.5, pool=f"P{i + 2}", token=tok),
        ]
    rows += [tr(later + 9000, "X", "buy", 1, 1.0)]
    res = simulate_copy(frame(rows), POOLS, CopyConfig(lookback_h=1, **NO_COST))
    assert [f["wallet"] for f in res.followed][:2] == ["GOOD", "NEXT"]
    assert res.followed[0]["fired"] and any("[FIRED]" in e["text"] for e in res.feed)


def test_too_little_data_is_reported():
    with pytest.raises(ValueError, match="Zu wenig Daten"):
        simulate_copy(frame(history()), POOLS, CopyConfig(lookback_h=5))


def test_collector_parses_and_dedupes(tmp_path):
    pool = {
        "attributes": {
            "address": "P1",
            "name": "TOK / SOL",
            "reserve_in_usd": "5000",
            "pool_created_at": "2026-10-01T00:00:00Z",
        },
        "relationships": {
            "base_token": {"data": {"id": "solana_TOK1"}},
            "dex": {"data": {"id": "pumpswap"}},
        },
    }
    trade = {
        "attributes": {
            "tx_hash": "h1",
            "block_timestamp": "2026-10-18T00:00:00Z",
            "tx_from_address": "W",
            "kind": "buy",
            "from_token_address": "SOL",
            "to_token_address": "TOK1",
            "from_token_amount": "0.1",
            "to_token_amount": "1000",
            "price_from_in_usd": "100",
            "price_to_in_usd": "0.01",
            "volume_in_usd": "10",
        }
    }
    api = {"trending_pools": {"data": [pool]}, "trades": {"data": [trade, trade]}}

    def fetch(url):
        return api["trades" if url.endswith("/trades") else "trending_pools"]

    store = CopyStore(tmp_path / "c.sqlite")
    assert collect_once(store, fetch, pause=0, log=lambda m: None) == 1
    assert collect_once(store, fetch, pause=0, log=lambda m: None) == 0  # gleicher tx nicht doppelt
    rows = parse_trades({"pool": "P1", "token": "TOK1"}, [trade])
    assert rows[0][5:8] == ("buy", 1000.0, 0.01)


def test_report_renders(tmp_path):
    from tradikg_bot.copytrade.report import render_copy_html

    later = 3600
    rows = history() + [
        tr(later, "GOOD", "buy", 100, 1.0),
        tr(later + 12, "X", "buy", 1, 1.1),
        tr(later + 60, "GOOD", "sell", 100, 1.6),
        tr(later + 75, "X", "sell", 1, 1.5),
        tr(later + 9000, "X", "buy", 1, 1.5),
    ]
    res = simulate_copy(frame(rows), POOLS, CopyConfig(lookback_h=1))
    page = render_copy_html(
        res,
        {"from": "a", "to": "b", "pools": 1, "wallets": 3, "complete_share": 0.5},
        [("Verzögerung 10 s", res.final)],
    )
    assert "Copy-Trading-Simulator" in page and "GOOD" in page and "<svg" in page
