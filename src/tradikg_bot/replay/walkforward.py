"""Historical Market Replay mit Walk-Forward-Tests (nur Simulation, keine Orders).

Zeitliche Trennung (kein Shuffle, keine Überschneidung)::

    |-- nur Research --|- Fold 1 -|- Fold 2 -|- ... -|---- Holdout (unangetastet) ----|
    0              Cutoff 1   Cutoff 2                Holdout-Start                 Ende

* An jedem Entscheidungszeitpunkt (Cutoff) läuft die Research-Engine **nur auf
  Kerzen vor dem Cutoff**. Darin trennt sie selbst zeitlich Train (60 %),
  Validation (20 %) und Test (20 %). Gewählte Parameter und OOS-Statistiken steuern
  den Bot bis zum nächsten Cutoff. Bewertet wird der Bot ausschließlich auf Kerzen
  **nach** dem Cutoff – also nie auf Daten, auf denen er trainiert oder optimiert wurde.
* Gehandelt wird mit **derselben** Paper-Engine wie im Dauerbetrieb: gleiche
  Signale, Netto-EV-Filter, Risiko- und Positionsgrößenlogik, Mindestordergrößen,
  Gebühren, Spread, Slippage, SL/TP und Ledger (Verlustaufholung, dann 33/33/34).
  Signale werden je Abschnitt nur auf Kerzen bis zum Abschnittsende berechnet.
* Der Holdout am Ende fließt in keine Auswahl ein. Seine Research nutzt nur Daten
  vor dem Holdout. Er lässt sich mit ``evaluate_holdout=False`` aussparen, solange
  am Ansatz noch gearbeitet wird; wie oft er ausgewertet wurde, zählt der Store.
* Rollierende Fenster (frisches Kapital je Fenster) zeigen die Verteilung der
  Ergebnisse über verschiedene Zeiträume – ausdrücklich auch die negativen.
* SL und TP in derselben Kerze: Standard „Stop zuerst“ (konservativ); „TP zuerst“
  läuft nur als gekennzeichnete Obergrenze. Es wird keine Tick-Reihenfolge erfunden.
"""

from __future__ import annotations

import math
import re
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tradikg_bot.costs import KRAKEN_SPOT_ASSUMED
from tradikg_bot.domain import Instrument
from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.models import FeeRates, PairRules
from tradikg_bot.live.paper import PAPER_RULES, generic_rules
from tradikg_bot.research.engine import (
    ResearchConfig,
    ResearchEngine,
    StrategyResearch,
    research_many,
)
from tradikg_bot.research.store import OosStats, oos_stats_from_results
from tradikg_bot.strategies.families import strategy_registry
from tradikg_bot.trading.engine import Bar, PaperEngineConfig, PaperTradingEngine
from tradikg_bot.trading.probability import EmpiricalProbability, ProbabilityEstimate
from tradikg_bot.trading.runner import DEFAULT_PAPER_FEES, PaperPaths, open_engine, strategies_for
from tradikg_bot.trading.sources import PrecomputedSignals, RawSignal

OHLCV = ["open", "high", "low", "close", "volume"]


# -- Szenarien ------------------------------------------------------------------------------
@dataclass(frozen=True)
class CapitalProfile:
    key: str
    label: str
    capital: float
    risk_overrides: tuple[tuple[str, Any], ...] = ()

    def risk(self, pairs: list[str]) -> LiveRiskConfig:
        return LiveRiskConfig.for_paper(
            self.capital, allowed_pairs=tuple(pairs), **dict(self.risk_overrides)
        )


DEFAULT_PROFILES = (
    CapitalProfile("eur10", "10 € (1 % Risiko je Trade)", 10.0),
    CapitalProfile(
        "eur10_pilot",
        "10 € Pilotprofil (max. 0,50 € Risiko je Trade)",
        10.0,
        (("risk_per_trade_fraction", None), ("max_loss_per_trade_eur", 0.5)),
    ),
    CapitalProfile("eur100", "100 €", 100.0),
    CapitalProfile("eur1000", "1.000 €", 1000.0),
)


@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    kind: str  # "bot" | "robustness" | "baseline"
    fee_mult: float = 1.0
    simulated_spread_bps: float | None = None
    stop_slippage_bps: float = 20.0
    ambiguous_policy: str = "stop_first"
    volatility_stress: bool = False
    ev_filter: bool = True
    random_entries: bool = False
    upper_bound: bool = False


VARIANTS = {
    v.key: v
    for v in (
        Variant("bot", "Bot (Standard)", "bot"),
        Variant("fees_x2", "Gebühren ×2", "robustness", fee_mult=2.0),
        Variant(
            "worse_execution",
            "Schlechtere Ausführung (Spread 30 bps statt 10, Stop-Slippage 100 bps)",
            "robustness",
            simulated_spread_bps=30.0,
            stop_slippage_bps=100.0,
        ),
        Variant(
            "volatility_stress",
            "Ungewöhnliche Volatilität (Dochte ×1,5, zufällige Kurslücken)",
            "robustness",
            volatility_stress=True,
        ),
        Variant(
            "tp_first",
            "Obergrenze: TP zuerst bei SL+TP in derselben Kerze (nicht realistisch)",
            "robustness",
            ambiguous_policy="take_profit_first",
            upper_bound=True,
        ),
        Variant(
            "no_filter",
            "Baseline: Strategiesignale ohne Netto-EV-Filter",
            "baseline",
            ev_filter=False,
        ),
        Variant(
            "random",
            "Baseline: Zufallseinstiege (gleiche Risiko-/Kostenlogik)",
            "baseline",
            ev_filter=False,
            random_entries=True,
        ),
    )
}

ANALYTIC_BASELINES = {
    "buy_hold": "Buy & Hold (gleichgewichtet)",
    "buy_hold_first": "Buy & Hold (nur erstes Asset)",
    "cash": "Nichts tun (Cash)",
}


@dataclass(frozen=True)
class ReplayConfig:
    timeframe: str = "1d"
    holdout_frac: float = 0.2
    first_cutoff_frac: float = 0.4
    folds: int = 4
    window_bars: int | None = None  # Standard: 2 Folds
    window_step_bars: int | None = None  # Standard: halber Fold
    profiles: tuple[CapitalProfile, ...] = DEFAULT_PROFILES
    window_profiles: tuple[str, ...] = ("eur10_pilot", "eur100", "eur1000")
    variant_profile: str = "eur100"
    variants: tuple[str, ...] = tuple(VARIANTS)
    strategies: tuple[str, ...] = ()
    research_random_runs: int = 50
    min_history_bars: int = 150
    seed_bars: int = 500
    min_expected_r: float = 0.02
    evaluate_holdout: bool = True
    rolling_windows: bool = True
    seed: int = 7

    def profile(self, key: str) -> CapitalProfile:
        return next(p for p in self.profiles if p.key == key)


# -- Zeitplan -------------------------------------------------------------------------------
@dataclass(frozen=True)
class ReplayPlan:
    index: pd.DatetimeIndex
    cutoffs: tuple[int, ...]
    holdout_start: int
    windows: tuple[tuple[int, int], ...]

    @property
    def end(self) -> int:
        return len(self.index)

    @property
    def decision_points(self) -> tuple[int, ...]:
        return (*self.cutoffs, self.holdout_start)

    def research_cutoff(self, i: int) -> int:
        """Letzter Entscheidungszeitpunkt ≤ i: Dessen Research kennt nur Kerzen davor."""
        eligible = [c for c in self.decision_points if c <= i]
        if not eligible:
            raise ValueError("Vor dem ersten Cutoff wird nicht gehandelt")
        return eligible[-1]

    def boundaries(self, start: int, end: int) -> list[int]:
        return sorted({start, *(c for c in self.decision_points if start < c < end)})

    def describe(self) -> list[dict[str, Any]]:
        idx, rows = self.index, []
        points = [*self.cutoffs, self.holdout_start, self.end]
        for k, c in enumerate(self.cutoffs):
            rows.append(
                {
                    "abschnitt": f"Walk-Forward-Fold {k + 1}",
                    "research_daten": f"{idx[0].date()} – {idx[c - 1].date()} (nur Vergangenheit)",
                    "bewertung": f"{idx[c].date()} – {idx[points[k + 1] - 1].date()}",
                    "kerzen": points[k + 1] - c,
                }
            )
        rows.append(
            {
                "abschnitt": "Holdout (unangetastet)",
                "research_daten": f"{idx[0].date()} – {idx[self.holdout_start - 1].date()}",
                "bewertung": f"{idx[self.holdout_start].date()} – {idx[-1].date()}",
                "kerzen": self.end - self.holdout_start,
            }
        )
        return rows


def make_plan(index: pd.DatetimeIndex, cfg: ReplayConfig) -> ReplayPlan:
    n = len(index)
    holdout = int(n * (1 - cfg.holdout_frac))
    first = max(int(n * cfg.first_cutoff_frac), cfg.min_history_bars)
    if holdout - first < cfg.folds * 2 or n - holdout < 2:
        raise ValueError(
            f"Zu wenig Daten für Walk-Forward ({n} Kerzen): mindestens Vorlauf "
            f"{cfg.min_history_bars}, {cfg.folds} Folds und ein Holdout werden benötigt"
        )
    fold = (holdout - first) / cfg.folds
    cutoffs = tuple(first + round(k * fold) for k in range(cfg.folds))
    windows: tuple[tuple[int, int], ...] = ()
    if cfg.rolling_windows:
        w = cfg.window_bars or max(2, round(2 * fold))
        step = cfg.window_step_bars or max(1, round(fold / 2))
        if first + w <= holdout:
            windows = tuple((s, s + w) for s in range(first, holdout - w + 1, step))
    return ReplayPlan(index, cutoffs, holdout, windows)


# -- Signalquellen und Wahrscheinlichkeiten für Baselines -----------------------------------
class AlwaysTradeProbability:
    """Baseline ohne Filter: unterstellt jedem Signal eine hohe Trefferquote."""

    def estimate(self, strategy, symbol, regime, reward_risk, features=None) -> ProbabilityEstimate:
        return ProbabilityEstimate(0.95, "baseline: ohne Netto-EV-Filter", 0)


class RandomEntrySignals:
    """Zufallseinstiege mit ATR-basierten SL/TP (2 ATR / 3 ATR) – kausal und reproduzierbar."""

    name = "zufall"

    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        rate: float = 0.03,
        seed: int = 0,
        stop_atr: float = 2.0,
        take_atr: float = 3.0,
        window: int = 14,
    ) -> None:
        self._sig: dict[str, pd.DataFrame] = {}
        for i, sym in enumerate(sorted(data)):
            df = data[sym]
            prev = df["close"].shift(1)
            tr = pd.concat(
                [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
                axis=1,
            ).max(axis=1)
            atr = tr.rolling(window).mean()
            entry = np.random.default_rng([seed, i]).random(len(df)) < rate
            self._sig[sym] = pd.DataFrame(
                {"entry": entry, "stop": stop_atr * atr, "take": take_atr * atr}, index=df.index
            )

    def signals_at(self, symbol: str, ts: pd.Timestamp, history: pd.DataFrame) -> list[RawSignal]:
        sig = self._sig.get(symbol)
        if sig is None or ts not in sig.index:
            return []
        row = sig.loc[ts]
        return [
            RawSignal(
                self.name, bool(row["entry"]), float(row["stop"]), float(row["take"]), "-", None
            )
        ]


def stress_volatility(
    df: pd.DataFrame,
    start: pd.Timestamp,
    wick_mult: float = 1.5,
    gap_prob: float = 0.03,
    gap_mult: float = 3.0,
    seed: int = 0,
) -> pd.DataFrame:
    """Verstärkt ab ``start`` Dochte und fügt zufällige Kurslücken (±3–6 typische Kerzen) ein.

    Kerzen vor ``start`` bleiben unverändert – der Bot kennt die Zukunft nicht.
    """
    out = df.copy()
    mask = out.index >= start
    n = int(mask.sum())
    if n == 0:
        return out
    hist = df[~mask] if (~mask).sum() > 10 else df
    typical = float((hist["close"] / hist["open"] - 1).abs().median()) or 0.01
    rng = np.random.default_rng(seed)
    hit = rng.random(n) < gap_prob
    gaps = np.where(hit, rng.choice([-1.0, 1.0], n) * gap_mult * typical * rng.uniform(1, 2, n), 0)
    factor = np.cumprod(1 + gaps)
    seg = out.loc[mask, ["open", "high", "low", "close"]].mul(factor, axis=0)
    body_hi = seg[["open", "close"]].max(axis=1)
    body_lo = seg[["open", "close"]].min(axis=1)
    seg["high"] = body_hi + (seg["high"] - body_hi) * wick_mult
    seg["low"] = np.maximum(body_lo - (body_lo - seg["low"]) * wick_mult, body_lo * 0.5)
    out.loc[mask, ["open", "high", "low", "close"]] = seg
    return out


# -- Research je Entscheidungszeitpunkt -------------------------------------------------------
@dataclass
class CutoffResearch:
    cutoff: int
    cutoff_ts: pd.Timestamp
    symbols: list[str]
    results: list[StrategyResearch]
    stats: list[OosStats]

    def summary(self) -> dict[str, Any]:
        verdicts = Counter(r.verdict for r in self.results)
        return {
            "cutoff_ts": self.cutoff_ts.isoformat(),
            "symbols": self.symbols,
            "results": len(self.results),
            "verdicts": dict(verdicts),
            "confirmed": sorted(
                f"{r.strategy}/{r.symbol}" for r in self.results if r.verdict == "bestätigt"
            ),
        }


class ResearchCache:
    """Research nur mit Kerzen vor dem jeweiligen Cutoff; Ergebnis je Cutoff zwischengespeichert."""

    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        plan: ReplayPlan,
        cfg: ReplayConfig,
        data_source: str,
    ) -> None:
        self.data, self.plan, self.cfg, self.source = data, plan, cfg, data_source
        registry = strategy_registry()
        names = list(cfg.strategies) or list(registry)
        self.specs = {n: registry[n] for n in names}
        self.engine = ResearchEngine(
            Instrument.default_for("X", "crypto"),
            KRAKEN_SPOT_ASSUMED,
            ResearchConfig(random_runs=cfg.research_random_runs, seed=cfg.seed),
        )
        self.by_cutoff: dict[int, CutoffResearch] = {}

    def get(self, cutoff: int) -> CutoffResearch:
        if cutoff not in self.by_cutoff:
            ts = self.plan.index[cutoff]
            past = {s: df[df.index < ts] for s, df in self.data.items()}
            past = {s: df for s, df in past.items() if len(df) >= self.cfg.min_history_bars}
            results = research_many(self.engine, self.specs, past, self.source) if past else []
            self.by_cutoff[cutoff] = CutoffResearch(
                cutoff, ts, sorted(past), results, oos_stats_from_results(results)
            )
        return self.by_cutoff[cutoff]


# -- Ergebnisse ---------------------------------------------------------------------------------
@dataclass
class ScenarioResult:
    scenario_id: str
    period: str  # walk_forward | holdout | window
    variant: str
    label: str
    kind: str  # bot | robustness | baseline
    profile: str
    profile_label: str
    capital: float
    start: pd.Timestamp
    end: pd.Timestamp
    equity: pd.DataFrame  # Index: Zeitpunkt (Kerzenschluss); Spalten siehe _EQUITY_COLS
    trades: list[dict[str, Any]] = field(default_factory=list)
    final_equity: float = 0.0
    max_drawdown: float = 0.0
    fees_eur: float = 0.0
    ambiguous_exits: int = 0
    halted_days: int = 0
    open_positions_end: int = 0
    ledger: dict[str, float] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    upper_bound: bool = False
    note: str = ""

    @property
    def net_profit(self) -> float:
        return self.final_equity - self.capital

    @property
    def return_pct(self) -> float:
        return self.final_equity / self.capital - 1

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float | None:
        if not self.trades:
            return None
        return sum(1 for t in self.trades if t.get("net_pnl_eur", 0) > 0) / len(self.trades)

    def metrics(self) -> dict[str, Any]:
        return {
            "final_equity": self.final_equity,
            "net_profit": self.net_profit,
            "return_pct": self.return_pct,
            "max_drawdown": self.max_drawdown,
            "trades": self.n_trades,
            "win_rate": self.win_rate,
            "fees_eur": self.fees_eur,
            "ambiguous_exits": self.ambiguous_exits,
            "halted_days": self.halted_days,
            "open_positions_end": self.open_positions_end,
        }


_EQUITY_COLS = ["equity", "trading_balance", "payout_reserve", "longterm_portfolio", "unrecovered"]


def max_drawdown(values: pd.Series | np.ndarray, start: float) -> float:
    arr = np.concatenate([[start], np.asarray(values, dtype=float)])
    peak = np.maximum.accumulate(arr)
    return float((arr / peak - 1).min())


def _reason_key(text: str | None) -> str:
    text = re.sub(r"[-+]?\d[\d.,]*", "#", text or "unbekannt")
    return text[:140]


@dataclass
class ReplayResult:
    config: ReplayConfig
    plan: ReplayPlan
    data_source: str
    synthetic: bool
    symbols: list[str]
    scenarios: list[ScenarioResult]
    research: list[dict[str, Any]]
    rules_note: str = ""

    def select(self, **kw: Any) -> list[ScenarioResult]:
        return [s for s in self.scenarios if all(getattr(s, k) == v for k, v in kw.items())]


# -- Orchestrierung -----------------------------------------------------------------------------
class HistoricalReplay:
    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        work_dir: str | Path,
        cfg: ReplayConfig | None = None,
        data_source: str = "unknown",
        synthetic: bool = False,
        rules: dict[str, PairRules] | None = None,
        fees: FeeRates = DEFAULT_PAPER_FEES,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        if not data:
            raise ValueError("Keine Daten für das Replay")
        self.cfg = cfg or ReplayConfig()
        self.data = {s: df.sort_index() for s, df in data.items() if not df.empty}
        self.symbols = list(self.data)
        index = pd.DatetimeIndex(sorted(set().union(*[set(d.index) for d in self.data.values()])))
        self.plan = make_plan(index, self.cfg)
        self.work_dir = Path(work_dir)
        self.data_source, self.synthetic, self.fees = data_source, synthetic, fees
        self.rules = rules or {
            s: PAPER_RULES.get(s) or generic_rules(s, float(df["close"].iloc[0]))
            for s, df in self.data.items()
        }
        self.rules_note = (
            "übergeben (z. B. aktuelle Kraken-AssetPairs)"
            if rules
            else "Beispielwerte (nicht von Kraken geladen)"
        )
        self.research = ResearchCache(self.data, self.plan, self.cfg, data_source)
        self.progress = progress or (lambda _msg: None)
        self._stressed: dict[int, dict[str, pd.DataFrame]] = {}
        self._rows_cache: dict[int, dict[str, dict[pd.Timestamp, tuple[float, ...]]]] = {}

    # Daten je Variante
    def _dataset(self, variant: Variant, start: int) -> tuple[int, dict[str, pd.DataFrame]]:
        if not variant.volatility_stress:
            return -1, self.data
        if start not in self._stressed:
            ts = self.plan.index[start]
            self._stressed[start] = {
                s: stress_volatility(df, ts, seed=self.cfg.seed + i)
                for i, (s, df) in enumerate(self.data.items())
            }
        return start, self._stressed[start]

    def _rows(self, key: int, data: dict[str, pd.DataFrame]):
        if key not in self._rows_cache:
            self._rows_cache[key] = {
                s: dict(zip(df.index, df[OHLCV].itertuples(index=False, name=None), strict=True))
                for s, df in data.items()
            }
        return self._rows_cache[key]

    # Ein Abschnitt mit der echten Paper-Engine
    def run_segment(
        self,
        scenario_id: str,
        period: str,
        start: int,
        end: int,
        profile: CapitalProfile,
        variant: Variant,
    ) -> ScenarioResult:
        cfg, idx = self.cfg, self.plan.index
        root = self.work_dir / scenario_id
        if root.exists():
            shutil.rmtree(root)
        key, data = self._dataset(variant, start)
        rows = self._rows(key, data)
        fees = FeeRates(
            self.fees.taker * variant.fee_mult,
            self.fees.maker * variant.fee_mult,
            f"{self.fees.source} ×{variant.fee_mult:g}",
        )
        engine_cfg = PaperEngineConfig(
            timeframe=cfg.timeframe,
            capital_eur=profile.capital,
            simulated_spread_bps=variant.simulated_spread_bps,
            min_history_bars=cfg.min_history_bars,
            min_expected_r=cfg.min_expected_r,
        )
        engine = open_engine(
            PaperPaths(root),
            self.rules,
            AlwaysTradeProbability(),
            RandomEntrySignals({}),
            engine_cfg,
            fees=fees,
            risk=profile.risk(self.symbols),
        )
        engine.gw.stop_slippage_bps = variant.stop_slippage_bps
        engine.gw.ambiguous_policy = variant.ambiguous_policy
        seed_from = idx[max(0, start - cfg.seed_bars)]
        engine.seed_history(
            {s: df[(df.index >= seed_from) & (df.index < idx[start])] for s, df in data.items()}
        )
        bounds = self.plan.boundaries(start, end)
        names = list(cfg.strategies) or None
        for k, b in enumerate(bounds):
            nxt = bounds[k + 1] if k + 1 < len(bounds) else end
            research = self.research.get(self.plan.research_cutoff(b))
            last_visible = idx[nxt - 1]
            visible = {s: df[df.index <= last_visible] for s, df in data.items()}
            if variant.random_entries:
                engine.source = RandomEntrySignals(visible, seed=cfg.seed)
            else:
                engine.source = PrecomputedSignals(
                    visible,
                    strategies_for(list(visible), research.stats, names),
                    with_features=False,
                )
            engine.probability = (
                EmpiricalProbability(research.stats)
                if variant.ev_filter
                else AlwaysTradeProbability()
            )
            for t in range(b, nxt):
                ts = idx[t]
                bars = {s: Bar(*r[ts]) for s, r in rows.items() if ts in r}
                engine.step(ts, bars)
        result = self._collect(engine, scenario_id, period, profile, variant, start, end)
        engine.store.close()
        engine.journal.close()
        engine.ledger.close()
        return result

    def _collect(
        self,
        engine: PaperTradingEngine,
        scenario_id: str,
        period: str,
        profile: CapitalProfile,
        variant: Variant,
        start: int,
        end: int,
    ) -> ScenarioResult:
        conn = engine.store.conn
        eq = pd.read_sql_query(
            "SELECT ts, equity, positions_value, trading_balance, payout_reserve, "
            "longterm_portfolio, unrecovered, status FROM equity ORDER BY ts",
            conn,
        )
        eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
        eq = eq.set_index("ts")
        trades = engine.store.analyses()
        final = profile.capital
        if not eq.empty:
            last = eq.iloc[-1]
            # offene Positionen zum letzten Schlusskurs abzüglich Verkaufsgebühr bewerten
            final = float(last["equity"] - last["positions_value"] * engine.gw.fees.taker)
        b = engine.ledger.balances()
        intents = Counter(
            f"{status}: {_reason_key(result)}"
            for status, result in conn.execute(
                "SELECT status, result FROM intents WHERE status NOT IN ('executed', 'pending')"
            ).fetchall()
        )
        signals = Counter(
            f"signal: {_reason_key(reason)}"
            for (reason,) in conn.execute(
                "SELECT reason FROM signals_log WHERE status = 'rejected'"
            ).fetchall()
        )
        halted = conn.execute(
            "SELECT COUNT(*) FROM engine_kv WHERE key LIKE 'day_halt:%'"
        ).fetchone()[0]
        return ScenarioResult(
            scenario_id=scenario_id,
            period=period,
            variant=variant.key,
            label=variant.label,
            kind=variant.kind,
            profile=profile.key,
            profile_label=profile.label,
            capital=profile.capital,
            start=self.plan.index[start],
            end=self.plan.index[end - 1],
            equity=eq[_EQUITY_COLS + ["status"]],
            trades=trades,
            final_equity=final,
            max_drawdown=max_drawdown(eq["equity"], profile.capital) if not eq.empty else 0.0,
            fees_eur=float(sum(t.get("fees_eur", 0.0) for t in trades)),
            ambiguous_exits=sum(1 for t in trades if t.get("intrabar_ambiguous")),
            halted_days=int(halted),
            open_positions_end=int(len(engine.journal.trades(active_only=True))),
            ledger={
                "trading_balance": b.trading_balance,
                "payout_reserve": b.payout_reserve,
                "longterm_portfolio": b.longterm_portfolio,
                "unrecovered_losses": b.unrecovered_losses,
                "realized_net_pnl": b.total_net_pnl,
            },
            rejections=dict((intents + signals).most_common(8)),
            upper_bound=variant.upper_bound,
        )

    # Analytische Baselines (gleiche Kosten: Taker-Gebühr und halber Spread je Seite)
    def baseline(
        self,
        scenario_id: str,
        period: str,
        start: int,
        end: int,
        profile: CapitalProfile,
        kind: str,
    ) -> ScenarioResult:
        idx = self.plan.index
        step = pd.Timedelta(idx[1] - idx[0]) if len(idx) > 1 else pd.Timedelta(0)
        stamps = idx[start:end]
        capital = profile.capital
        half = 10.0 / 2 * 1e-4  # angenommener Spread wie im Paper-Betrieb (10 bps)
        fee = self.fees.taker
        candidates = [s for s in self.symbols if idx[start] in self.data[s].index]
        if kind == "buy_hold_first":
            candidates = candidates[:1]
        # Reichen das Kapital nicht für alle Mindestorders, werden die Assets mit den
        # kleinsten Mindestorders gewählt (so viele wie möglich, gleichgewichtet).
        candidates.sort(key=lambda s: self._min_cost(s, idx[start]))
        chosen: list[str] = []
        if kind != "cash":
            for k in range(len(candidates), 0, -1):
                alloc = capital / k
                mins = [self._min_cost(s, idx[start]) for s in candidates[:k]]
                if alloc >= max(mins):
                    chosen = candidates[:k]
                    break
        units, cash = {}, capital
        for s in chosen:
            alloc = capital / len(chosen)
            price = float(self.data[s].at[idx[start], "open"]) * (1 + half)
            units[s] = alloc / (price * (1 + fee))
            cash -= alloc
        closes = {s: self.data[s]["close"].reindex(stamps).ffill() for s in chosen}
        values = pd.Series(cash, index=stamps, dtype=float)
        for s in chosen:
            values = values + closes[s].to_numpy() * units[s]
        exit_cost = sum(
            float(closes[s].iloc[-1]) * units[s] * (1 - (1 - half) * (1 - fee)) for s in chosen
        )
        final = float(values.iloc[-1] - exit_cost)
        eq = pd.DataFrame({"equity": values.to_numpy()}, index=stamps + step)
        for col in _EQUITY_COLS[1:]:
            eq[col] = np.nan
        eq["status"] = "baseline"
        note = ""
        if kind != "cash" and not chosen:
            note = "Kapital unter Mindestordergröße – bleibt Cash"
        elif len(chosen) < len(candidates):
            note = f"nur {len(chosen)} von {len(candidates)} Assets (Mindestordergröße)"
        return ScenarioResult(
            scenario_id=scenario_id,
            period=period,
            variant=kind,
            label=ANALYTIC_BASELINES[kind],
            kind="baseline",
            profile=profile.key,
            profile_label=profile.label,
            capital=capital,
            start=idx[start],
            end=idx[end - 1],
            equity=eq,
            final_equity=final,
            max_drawdown=max_drawdown(values, capital),
            fees_eur=sum(capital / len(chosen) * fee for _ in chosen) if chosen else 0.0,
            note=note,
        )

    def _min_cost(self, symbol: str, ts: pd.Timestamp) -> float:
        r = self.rules[symbol]
        return max(r.ordermin * float(self.data[symbol].at[ts, "open"]), r.costmin)

    # Gesamtlauf
    def run(self) -> ReplayResult:
        cfg, plan = self.cfg, self.plan
        scenarios: list[ScenarioResult] = []
        periods = [("walk_forward", plan.cutoffs[0], plan.holdout_start)]
        if cfg.evaluate_holdout:
            periods.append(("holdout", plan.holdout_start, plan.end))
        variants = [VARIANTS[v] for v in cfg.variants if v in VARIANTS and v != "bot"]
        for period, start, end in periods:
            for profile in cfg.profiles:
                self.progress(f"{period}: Bot mit {profile.label}")
                scenarios.append(
                    self.run_segment(
                        f"{period}-bot-{profile.key}", period, start, end, profile, VARIANTS["bot"]
                    )
                )
                for kind in ANALYTIC_BASELINES:
                    scenarios.append(
                        self.baseline(
                            f"{period}-{kind}-{profile.key}", period, start, end, profile, kind
                        )
                    )
            profile = cfg.profile(cfg.variant_profile)
            for v in variants:
                self.progress(f"{period}: {v.label} ({profile.label})")
                scenarios.append(
                    self.run_segment(
                        f"{period}-{v.key}-{profile.key}", period, start, end, profile, v
                    )
                )
        for w, (start, end) in enumerate(plan.windows):
            for key in cfg.window_profiles:
                profile = cfg.profile(key)
                self.progress(f"Fenster {w + 1}/{len(plan.windows)}: {profile.label}")
                scenarios.append(
                    self.run_segment(
                        f"window{w + 1:02d}-bot-{key}",
                        "window",
                        start,
                        end,
                        profile,
                        VARIANTS["bot"],
                    )
                )
                scenarios.append(
                    self.baseline(
                        f"window{w + 1:02d}-buy_hold-{key}",
                        "window",
                        start,
                        end,
                        profile,
                        "buy_hold",
                    )
                )
        research = [self.research.get(c).summary() for c in sorted(self.research.by_cutoff)]
        return ReplayResult(
            cfg,
            plan,
            self.data_source,
            self.synthetic,
            self.symbols,
            scenarios,
            research,
            self.rules_note,
        )


def distribution(values: list[float], capital: float) -> dict[str, float]:
    """Verteilung der Endwerte über Zeiträume (inkl. negativer Ergebnisse)."""
    if not values:
        return {}
    arr = np.asarray(values, dtype=float)
    return {
        "n": float(len(arr)),
        "min": float(arr.min()),
        "p10": float(np.percentile(arr, 10)),
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
        "max": float(arr.max()),
        "share_loss": float((arr < capital).mean()),
        "share_gain": float((arr > capital).mean()),
    }


def is_finite(x: float | None) -> bool:
    return x is not None and math.isfinite(x)
