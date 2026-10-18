"""Copy-Trading auf Solana mit echten Kursen – Stufe 1: **Schattenbetrieb** (nichts wird signiert).

* **Wen kopieren:** alle 10 min die besten aktiven Solana-Wallets aus dem Sammler (Taktik v2:
  Score mit Runden-Abschlag, Wechsel nur bei +25 %, 2 Verluste in Folge → gefeuert).
* **Wann:** Die gefolgten Wallets werden **direkt auf der Blockchain** beobachtet (öffentliches
  Solana-RPC, alle ~10 s) – nicht über den langsamen Sammler.
* **Zu welchem Preis:** echte Jupiter-Angebote (Route über alle Solana-DEX) im Moment der Erkennung.
* **Schutz vor dem Kauf:** Mint-/Freeze-Recht des Tokens entzogen (sonst Rug-Pull-/Sperr-Gefahr),
  keine Token-2022-Sonderfunktionen, Preiseinfluss ≤ 3 %, Rückverkauf sofort möglich und verliert
  höchstens 8 % (Honeypot-/Steuer-Schutz), höchstens 5 Positionen, 10 % je Kauf, 12 h Haltedauer,
  Verlustgrenze 20 % (danach keine neuen Käufe), Kill-Switch-Datei.

Stufe 2 (Echtgeld) nutzt exakt diese Entscheidungen, signiert aber mit einer eigenen Wallet –
erst nachdem der Nutzer sie selbst angelegt und gefüllt hat.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

SOL = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {
    SOL,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
TOKEN_2022 = "TokenzQdBNbLqP5VvEd5Kv5nHPx6sUAVEjvZ6X6gC9v"
RPC_URL = "https://api.mainnet-beta.solana.com"
JUP_URL = "https://lite-api.jup.ag/swap/v1/quote"
LAMPORTS = 1_000_000_000
UA = {"User-Agent": "tradikg-bot/0.1 (copy shadow, read-only)", "Content-Type": "application/json"}


@dataclass(frozen=True)
class SolanaCopyConfig:
    capital_sol: float = 0.20  # ≈ 19 € bei ~92 €/SOL
    position_frac: float = 0.10
    max_positions: int = 5
    max_hold_h: float = 12.0
    fire_after: int = 2
    max_impact: float = 0.03
    max_roundtrip_loss: float = 0.08
    loss_limit: float = 0.20
    slippage_bps: int = 300
    fee_sol: float = 0.00011  # Netzgebühr + Priority-Fee je Swap (Schätzung)
    reserve_sol: float = 0.01  # bleibt für Gebühren/Kontomiete immer liegen
    leaders: int = 3
    rerank_min: float = 10.0


@dataclass
class Holding:
    mint: str
    units: int  # kleinste Einheit des Tokens
    cost_sol: float
    since: str
    leader: str


@dataclass
class ShadowState:
    cash_sol: float
    start: str
    holdings: dict[str, Holding] = field(default_factory=dict)
    leaders: list[str] = field(default_factory=list)
    fired: list[str] = field(default_factory=list)
    losses: dict[str, int] = field(default_factory=dict)
    last_sig: dict[str, str] = field(default_factory=dict)
    next_rank: str = ""
    feed: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, cfg: SolanaCopyConfig) -> ShadowState:
        if not path.exists():
            return cls(cfg.capital_sol, pd.Timestamp.now(tz="UTC").isoformat())
        raw = json.loads(path.read_text())
        raw["holdings"] = {k: Holding(**v) for k, v in raw.get("holdings", {}).items()}
        return cls(**raw)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), default=str))
        tmp.replace(path)


def _post(url: str, body: dict, retries: int = 3) -> Any:
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode(), UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unerreichbar")


def rpc(method: str, params: list) -> Any:
    out = _post(RPC_URL, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in out:
        raise RuntimeError(f"RPC {method}: {out['error']}")
    return out["result"]


def jupiter_quote(in_mint: str, out_mint: str, amount: int, slippage_bps: int) -> dict | None:
    url = (
        f"{JUP_URL}?inputMint={in_mint}&outputMint={out_mint}&amount={int(amount)}"
        f"&slippageBps={slippage_bps}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=20) as r:
            q = json.loads(r.read())
    except urllib.error.HTTPError:
        return None  # keine Route (z. B. Token nicht handelbar)
    return q if "outAmount" in q else None


def parse_swap(tx: dict, wallet: str) -> list[tuple[str, float]]:
    """Token-Änderungen des Unterzeichners (ohne SOL/Stablecoins): +x = gekauft, −x = verkauft."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []

    def bal(rows: list[dict]) -> dict[str, float]:
        out: dict[str, float] = {}
        for b in rows or []:
            if b.get("owner") == wallet and b["mint"] not in QUOTE_MINTS:
                out[b["mint"]] = out.get(b["mint"], 0.0) + float(b["uiTokenAmount"]["amount"])
        return out

    pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    return [
        (m, post.get(m, 0.0) - pre.get(m, 0.0))
        for m in set(pre) | set(post)
        if post.get(m, 0.0) != pre.get(m, 0.0)
    ]


def mint_risk(account: dict | None) -> str | None:
    """Grund gegen den Kauf oder None. Mint-Recht = neue Token drucken (Verwässerung/Rug),
    Freeze-Recht = Konten einfrieren (Verkaufssperre), Token-2022 = evtl. Transfer-Steuer."""
    if not account:
        return "Token-Daten nicht lesbar"
    if account.get("owner") == TOKEN_2022:
        return "Token-2022 (mögliche Transfer-Steuer/Sperre)"
    info = account.get("data", {}).get("parsed", {}).get("info", {})
    if info.get("mintAuthority"):
        return "Mint-Recht nicht entzogen (Token kann nachgedruckt werden)"
    if info.get("freezeAuthority"):
        return "Freeze-Recht nicht entzogen (Verkauf kann gesperrt werden)"
    return None


class ShadowCopyRunner:
    """Ein Durchgang = Leader prüfen, neue Swaps der Leader lesen, kopieren (im Schatten)."""

    def __init__(
        self,
        state_path: Path,
        snapshot_path: Path,
        kill_switch: Path,
        cfg: SolanaCopyConfig | None = None,
        call: Callable[[str, list], Any] = rpc,
        quote: Callable[[str, str, int, int], dict | None] = jupiter_quote,
        rank: Callable[[list[str], set[str]], list[str]] | None = None,
        sol_eur: Callable[[], float | None] = lambda: None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.cfg = cfg or SolanaCopyConfig()
        self.state_path, self.snapshot_path, self.kill = state_path, snapshot_path, kill_switch
        self.call, self.quote, self.rank, self.sol_eur, self.log = call, quote, rank, sol_eur, log
        self.st = ShadowState.load(state_path, self.cfg)
        self.values: dict[str, float] = {}
        self._valued = 0.0

    # -- Hilfen ---------------------------------------------------------------------------------
    def _feed(self, text: str, pnl: float | None = None) -> None:
        self.st.feed = [*self.st.feed[-79:], {"ts": _now(), "text": text, "pnl": pnl}]
        self.log(text)

    def equity(self) -> float:
        return self.st.cash_sol + sum(
            self.values.get(m, h.cost_sol) for m, h in self.st.holdings.items()
        )

    def _value(self, h: Holding) -> float | None:
        q = self.quote(h.mint, SOL, h.units, self.cfg.slippage_bps)
        return int(q["outAmount"]) / LAMPORTS if q else None

    # -- Ablauf ---------------------------------------------------------------------------------
    def run_once(self, now: pd.Timestamp | None = None) -> dict:
        now = now or pd.Timestamp.now(tz="UTC")
        if self.rank and (not self.st.next_rank or now >= pd.Timestamp(self.st.next_rank)):
            self._rerank(now)
        for leader in list(self.st.leaders):
            for mint, delta in self._new_swaps(leader):
                if delta > 0:
                    self._buy(leader, mint, now)
                elif mint in self.st.holdings and self.st.holdings[mint].leader == leader:
                    self._sell(mint, "Trader verkauft")
        for mint, h in list(self.st.holdings.items()):
            if now - pd.Timestamp(h.since) > pd.Timedelta(hours=self.cfg.max_hold_h):
                self._sell(mint, "max. Haltedauer")
        if time.time() - self._valued >= 60:  # Bewertung jede Minute (Jupiter-Limit schonen)
            self._valued = time.time()
            for mint, h in self.st.holdings.items():
                v = self._value(h)
                if v is not None:
                    self.values[mint] = v
        self.st.save(self.state_path)
        return self._snapshot(now)

    def _rerank(self, now: pd.Timestamp) -> None:
        new = self.rank(self.st.leaders, set(self.st.fired))
        for w in set(new) - set(self.st.leaders):
            self._feed(f"[FOLGE] {w[:6]}… (beste aktive Solana-Wallet)")
            self.st.last_sig.pop(w, None)  # ab jetzt beobachten, Altes nicht nachkaufen
        for w in set(self.st.leaders) - set(new):
            self._feed(f"[WECHSEL] {w[:6]}… raus – anderer Trader klar besser oder inaktiv")
        self.st.leaders = new
        self.st.next_rank = (now + pd.Timedelta(minutes=self.cfg.rerank_min)).isoformat()

    def _new_swaps(self, leader: str) -> list[tuple[str, float]]:
        params: dict[str, Any] = {"limit": 20, "commitment": "confirmed"}
        if self.st.last_sig.get(leader):
            params["until"] = self.st.last_sig[leader]
        sigs = self.call("getSignaturesForAddress", [leader, params]) or []
        first_time = leader not in self.st.last_sig
        if sigs or first_time:
            self.st.last_sig[leader] = sigs[0]["signature"] if sigs else ""
        if first_time or not sigs:
            return []  # erst ab Beobachtungsbeginn kopieren
        out = []
        for s in reversed(sigs):  # älteste zuerst
            if s.get("err"):
                continue
            try:
                tx = self.call(
                    "getTransaction",
                    [
                        s["signature"],
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 1,
                            "commitment": "confirmed",
                        },
                    ],
                )
            except Exception as exc:  # einzelne Transaktion unlesbar – überspringen, weiter
                self.log(f"Transaktion {s['signature'][:8]}… übersprungen: {exc}")
                continue
            if tx:
                out += parse_swap(tx, leader)
        return out

    def _buy(self, leader: str, mint: str, now: pd.Timestamp) -> None:
        cfg, st = self.cfg, self.st
        if mint in st.holdings or len(st.holdings) >= cfg.max_positions:
            return
        why = None
        spend = round(self.equity() * cfg.position_frac, 6)
        if self.kill.exists():
            why = "Kill Switch aktiv"
        elif self.equity() < cfg.capital_sol * (1 - cfg.loss_limit):
            why = f"Verlustgrenze {cfg.loss_limit:.0%} erreicht – keine neuen Käufe"
        elif st.cash_sol - spend - cfg.fee_sol < cfg.reserve_sol:
            why = "zu wenig freies SOL"
        else:
            why = mint_risk(
                (self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}).get("value")
            )
        q = None
        if not why:
            q = self.quote(SOL, mint, int(spend * LAMPORTS), cfg.slippage_bps)
            if not q:
                why = "keine Route bei Jupiter"
            elif float(q.get("priceImpactPct") or 0) > cfg.max_impact:
                why = f"Preiseinfluss {float(q['priceImpactPct']):.1%} zu hoch"
        if not why:
            back = self.quote(mint, SOL, int(q["outAmount"]), cfg.slippage_bps)
            ret = int(back["outAmount"]) / LAMPORTS / spend if back else 0.0
            if ret < 1 - cfg.max_roundtrip_loss:
                why = f"Rückverkauf brächte nur {ret:.0%} zurück (Honeypot/Steuer?)"
        if why:
            self._feed(f"[SKIP] {leader[:6]}… kaufte {mint[:6]}… – {why}")
            return
        st.cash_sol -= spend + cfg.fee_sol
        st.holdings[mint] = Holding(mint, int(q["outAmount"]), spend + cfg.fee_sol, _now(), leader)
        self._feed(f"[COPY] {leader[:6]}… kaufte {mint[:6]}… · wir {spend:.4f} SOL (Jupiter-Kurs)")

    def _sell(self, mint: str, why: str) -> None:
        h = self.st.holdings.pop(mint)
        q = self.quote(mint, SOL, h.units, self.cfg.slippage_bps)
        out = (int(q["outAmount"]) / LAMPORTS if q else 0.0) - self.cfg.fee_sol
        pnl = out - h.cost_sol
        self.st.cash_sol += max(out, 0.0)
        self.values.pop(mint, None)
        self.st.trades.append(
            {
                **asdict(h),
                "exit": _now(),
                "out_sol": out,
                "pnl_sol": pnl,
                "reason": why if q else f"{why} – keine Route, Totalverlust",
            }
        )
        self._feed(f"[EXIT] {mint[:6]}… ({why}{'' if q else ', NICHT verkaufbar'})", pnl)
        if h.leader in self.st.leaders:
            n = self.st.losses.get(h.leader, 0) + 1 if pnl < 0 else 0
            self.st.losses[h.leader] = n
            if n >= self.cfg.fire_after:
                self.st.fired.append(h.leader)
                self.st.leaders.remove(h.leader)
                self.st.next_rank = ""  # sofort Ersatz suchen
                self._feed(f"[FIRED] {h.leader[:6]}… nach {n} Verlusten in Folge")

    def _snapshot(self, now: pd.Timestamp) -> dict:
        eq = self.equity()
        eur = self.sol_eur()
        snap = {
            "mode": "schatten",
            "updated": now.isoformat(),
            "start": self.st.start,
            "capital_sol": self.cfg.capital_sol,
            "equity_sol": eq,
            "cash_sol": self.st.cash_sol,
            "sol_eur": eur,
            "leaders": self.st.leaders,
            "losses": self.st.losses,
            "fired": self.st.fired,
            "holdings": [
                {**asdict(h), "value_sol": self.values.get(m)} for m, h in self.st.holdings.items()
            ],
            "trades": self.st.trades[-50:],
            "feed": self.st.feed[-60:],
        }
        path = self.snapshot_path
        curve = json.loads(path.read_text()).get("curve", []) if path.exists() else []
        snap["curve"] = [*curve[-2000:], [now.isoformat(), eq]]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, default=str))
        tmp.replace(path)
        return snap


def _now() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


def collector_rank(db: Path, lookback_h: float = 3.0, active_min: float = 60.0):
    """Rangliste aus dem Sammler (nur Solana-Pools), Taktik v2 mit Wechsel-Schwelle."""
    from tradikg_bot.copytrade.simulate import (
        CopyConfig,
        _with_hysteresis,
        load_trades,
        rank_wallets,
    )

    ccfg = CopyConfig(rank_shrink=5, switch_margin=0.25)

    def rank(current: list[str], fired: set[str]) -> list[str]:
        now = pd.Timestamp.now(tz="UTC")
        since = (now - pd.Timedelta(hours=lookback_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        trades, pools = load_trades(db, since)
        sol = set(pools.loc[pools.get("network", "solana") == "solana", "pool"])
        w = trades[trades["pool"].isin(sol)]
        active = set(w.loc[w["ts"] >= now - pd.Timedelta(minutes=active_min), "wallet"])
        ranked = rank_wallets(w[w["wallet"].isin(active)], ccfg, fired)
        return _with_hysteresis(ranked, current, 3, ccfg.switch_margin)

    return rank


def kraken_sol_eur() -> float | None:
    try:
        req = urllib.request.Request(
            "https://api.kraken.com/0/public/Ticker?pair=SOLEUR",
            headers={"User-Agent": UA["User-Agent"]},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return float(next(iter(json.loads(r.read())["result"].values()))["c"][0])
    except Exception:  # Kurs fehlt – Anzeige nur in SOL
        return None
