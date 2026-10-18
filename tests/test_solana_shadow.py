"""Solana-Copy im Schattenbetrieb gegen eine simulierte Blockchain und simulierte Jupiter-Kurse."""

from __future__ import annotations

import pandas as pd
import pytest

from tradikg_bot.copytrade.solana_live import (
    LAMPORTS,
    SOL,
    ShadowCopyRunner,
    SolanaCopyConfig,
    mint_risk,
    parse_swap,
)

SAFE = {"owner": "Tokenkeg", "data": {"parsed": {"info": {}}}}


def tx(wallet, mint, pre, post):
    def row(v):
        return [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(v)}}]

    return {"meta": {"err": None, "preTokenBalances": row(pre), "postTokenBalances": row(post)}}


class Chain:
    def __init__(self):
        self.sigs: dict[str, list[dict]] = {}
        self.txs: dict[str, dict] = {}
        self.mints: dict[str, dict] = {}
        self.n = 0

    def swap(self, wallet, mint, pre, post):
        self.n += 1
        sig = f"S{self.n}"
        self.sigs.setdefault(wallet, []).insert(0, {"signature": sig, "err": None})
        self.txs[sig] = tx(wallet, mint, pre, post)

    def __call__(self, method, params):
        if method == "getSignaturesForAddress":
            sigs = self.sigs.get(params[0], [])
            until = params[1].get("until")
            out = []
            for s in sigs:
                if s["signature"] == until:
                    break
                out.append(s)
            return out
        if method == "getTransaction":
            return self.txs[params[0]]
        if method == "getAccountInfo":
            return {"value": self.mints.get(params[0], SAFE)}
        raise AssertionError(method)


class Jup:
    """1 SOL = 1.000.000 Token-Einheiten; ``sell_factor`` < 1 simuliert Honeypot/Steuer."""

    def __init__(self):
        self.price = 1_000_000.0
        self.sell_factor = 1.0
        self.impact = 0.001

    def __call__(self, i, o, amount, bps):
        if i == SOL:
            return {
                "outAmount": str(int(amount / LAMPORTS * self.price)),
                "priceImpactPct": str(self.impact),
            }
        return {
            "outAmount": str(int(amount / self.price * LAMPORTS * self.sell_factor)),
            "priceImpactPct": "0",
        }


def runner(tmp_path, chain, jup, leaders=("L1",), cfg=None):
    return ShadowCopyRunner(
        tmp_path / "state.json",
        tmp_path / "snap.json",
        tmp_path / "KILL",
        cfg or SolanaCopyConfig(capital_sol=1.0),
        call=chain,
        quote=jup,
        rank=lambda cur, fired: [w for w in leaders if w not in fired],
        log=lambda m: None,
    )


T0 = pd.Timestamp("2026-10-18 12:00", tz="UTC")


def test_parse_swap_ignores_sol_and_failed_tx():
    t = tx("W", "MINT", 0, 500)
    assert parse_swap(t, "W") == [("MINT", 500.0)]
    assert parse_swap(t, "OTHER") == []
    assert parse_swap({"meta": {"err": {"x": 1}}}, "W") == []
    assert parse_swap(tx("W", SOL, 0, 5), "W") == []


def test_mint_risk_blocks_mint_and_freeze_authority():
    assert mint_risk(SAFE) is None
    assert "Mint-Recht" in mint_risk({"data": {"parsed": {"info": {"mintAuthority": "X"}}}})
    assert "Freeze" in mint_risk({"data": {"parsed": {"info": {"freezeAuthority": "X"}}}})
    assert "Token-2022" in mint_risk({"owner": "TokenzQdBNbLqP5VvEd5Kv5nHPx6sUAVEjvZ6X6gC9v"})


def test_copies_only_new_buys_and_follows_the_sell(tmp_path):
    chain, jup = Chain(), Jup()
    chain.swap("L1", "OLD", 0, 100)  # vor Beobachtungsbeginn – wird nicht nachgekauft
    r = runner(tmp_path, chain, jup)
    r.run_once(T0)
    assert r.st.holdings == {}
    chain.swap("L1", "TOK", 0, 1000)
    r.run_once(T0)
    h = r.st.holdings["TOK"]
    assert h.cost_sol == pytest.approx(0.1 + 0.00011)  # 10 % + Gebühr
    jup.price = 500_000.0  # Token verdoppelt sich
    chain.swap("L1", "TOK", 1000, 0)
    snap = r.run_once(T0)
    assert r.st.holdings == {} and r.st.trades[-1]["pnl_sol"] == pytest.approx(
        0.2 - 0.00011 - h.cost_sol
    )
    assert snap["equity_sol"] == pytest.approx(r.st.cash_sol)
    assert [e["text"][:6] for e in snap["feed"]][-3:] == ["[FOLGE", "[COPY]", "[EXIT]"]


def test_safety_checks_skip_risky_tokens(tmp_path):
    chain, jup = Chain(), Jup()
    r = runner(tmp_path, chain, jup)
    r.run_once(T0)
    chain.mints["RUG"] = {"data": {"parsed": {"info": {"freezeAuthority": "DEV"}}}}
    chain.swap("L1", "RUG", 0, 1)
    r.run_once(T0)
    jup.sell_factor = 0.5  # Rückverkauf bringt nur 50 %
    chain.swap("L1", "HONEY", 0, 1)
    r.run_once(T0)
    jup.sell_factor, jup.impact = 1.0, 0.10
    chain.swap("L1", "THIN", 0, 1)
    r.run_once(T0)
    assert r.st.holdings == {}
    skips = [e["text"] for e in r.st.feed if e["text"].startswith("[SKIP]")]
    assert "Freeze" in skips[0] and "50%" in skips[1] and "Preiseinfluss" in skips[2]
    (tmp_path / "KILL").touch()
    jup.impact = 0.0
    chain.swap("L1", "OK", 0, 1)
    r.run_once(T0)
    assert r.st.holdings == {} and "Kill" in r.st.feed[-1]["text"]


def test_fires_after_two_losses_and_restarts_from_state(tmp_path):
    chain, jup = Chain(), Jup()
    r = runner(tmp_path, chain, jup, leaders=("L1", "L2"))
    r.run_once(T0)
    for mint in ("A", "B"):
        jup.price = 1_000_000.0
        chain.swap("L1", mint, 0, 1)
        r.run_once(T0)
        jup.price = 2_000_000.0  # halbiert sich
        chain.swap("L1", mint, 1, 0)
        r.run_once(T0)
    assert "L1" in r.st.fired and r.st.leaders == ["L2"]
    again = runner(tmp_path, chain, jup, leaders=("L1", "L2"))  # Neustart aus Datei
    again.run_once(T0 + pd.Timedelta(minutes=11))
    assert again.st.leaders == ["L2"] and again.st.cash_sol == pytest.approx(r.st.cash_sol)


def test_loss_limit_stops_new_buys(tmp_path):
    chain, jup = Chain(), Jup()
    r = runner(tmp_path, chain, jup)
    r.run_once(T0)
    r.st.cash_sol = 0.7  # 30 % Verlust
    chain.swap("L1", "TOK", 0, 1)
    r.run_once(T0)
    assert r.st.holdings == {} and "Verlustgrenze" in r.st.feed[-1]["text"]


def test_keeps_watching_replaced_leader_until_position_is_sold(tmp_path):
    chain, jup = Chain(), Jup()
    current = ["L1"]
    r = runner(tmp_path, chain, jup)
    r.rank = lambda cur, fired: list(current)
    r.run_once(T0)
    chain.swap("L1", "TOK", 0, 1)
    r.run_once(T0)
    current[:] = ["L2"]  # L1 wird ersetzt
    r.run_once(T0 + pd.Timedelta(minutes=11))
    chain.swap("L1", "NEW", 0, 1)  # Kauf eines Ersetzten → nicht kopieren
    chain.swap("L1", "TOK", 1, 0)  # aber seinen Verkauf mitmachen
    r.run_once(T0 + pd.Timedelta(minutes=12))
    assert r.st.holdings == {} and r.st.trades[-1]["reason"] == "Trader verkauft"
