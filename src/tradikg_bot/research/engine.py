"""Strategy Research Engine: systematische, ehrliche Prüfung von Strategiehypothesen.

Vorgehen je Strategie und Asset (alle Bereiche zeitlich getrennt, kein Shuffle):

1. **Kausalitätsprüfung** (Look-Ahead-Test) auf den vollen Daten.
2. **Train** (60 %): Jede Parametervariante wird bewertet; zulässig sind nur
   Varianten mit genügend Trades und positivem Erwartungswert.
3. **Validation** (20 %): Unter den zulässigen Varianten wird die beste gewählt.
4. **Test** (20 %): Die gewählte Variante wird **einmal** auf ungesehenen Daten
   ausgewertet. Testdaten fließen nie in die Auswahl ein.
5. **Walk-Forward**: Expandierendes In-Sample-Fenster, Auswahl nur mit Daten vor
   dem jeweiligen Out-of-Sample-Block.
6. **Baselines**: Buy & Hold sowie Zufallseinstiege mit identischer Anzahl,
   identischen SL/TP-Abständen und Kosten (Monte-Carlo).
7. **Kosten-Sensitivität**: Gebühren, Spread und Slippage ×0, ×1, ×2.
8. **Marktphasen**: Ergebnisse je Regime zum Signalzeitpunkt.

Urteil:
* ``bestätigt``: genügend Test-Trades, positiver Erwartungswert bei ×1 **und** ×2
  Kosten, Profit-Faktor über Schwelle, signifikant besser als Zufallseinstiege,
  Walk-Forward-OOS positiv. Gilt nur für die untersuchten Daten.
* ``verworfen``: Erwartungswert nach Kosten ≤ 0 auf den Testdaten.
* ``hypothese``: alles dazwischen (z. B. zu wenige Trades für eine Aussage).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from tradikg_bot.backtest import BacktestEngine, check_no_lookahead
from tradikg_bot.backtest.engine import Trade
from tradikg_bot.config import RiskConfig
from tradikg_bot.costs.fees import FeeModel
from tradikg_bot.domain import Instrument
from tradikg_bot.features.regime import detect_regime
from tradikg_bot.strategies.base import Strategy, validate_signals
from tradikg_bot.strategies.families import StrategySpec


class Verdict:
    CONFIRMED = "bestätigt"
    REJECTED = "verworfen"
    HYPOTHESIS = "hypothese"


@dataclass(frozen=True)
class ResearchConfig:
    train_frac: float = 0.6
    val_frac: float = 0.2
    wf_folds: int = 4
    wf_initial_frac: float = 0.5
    min_trades_select: int = 5
    min_test_trades: int = 30
    min_profit_factor: float = 1.15
    alpha: float = 0.05
    random_runs: int = 200
    fee_multipliers: tuple[float, ...] = (0.0, 1.0, 2.0)
    initial_capital: float = 100.0
    seed: int = 42


@dataclass
class TradeStats:
    n: int = 0
    wins: int = 0
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    profit_factor: float | None = None
    expectancy_eur: float | None = None
    expectancy_r: float | None = None
    net_profit: float = 0.0
    net_return: float = 0.0
    max_drawdown: float = 0.0
    t_stat_r: float | None = None
    total_costs: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def trade_stats(trades: list[Trade], initial_capital: float) -> TradeStats:
    if not trades:
        return TradeStats()
    net = np.array([t.net_pnl for t in trades])
    r = np.array([t.r_multiple for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    equity = initial_capital + np.cumsum(net)
    peak = np.maximum.accumulate(np.concatenate([[initial_capital], equity]))[1:]
    std_r = r.std(ddof=1) if len(r) > 1 else 0.0
    return TradeStats(
        n=len(trades),
        wins=int((net > 0).sum()),
        win_rate=float((net > 0).mean()),
        avg_win=float(wins.mean()) if len(wins) else None,
        avg_loss=float(losses.mean()) if len(losses) else None,
        profit_factor=float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,
        expectancy_eur=float(net.mean()),
        expectancy_r=float(r.mean()),
        net_profit=float(net.sum()),
        net_return=float(net.sum() / initial_capital),
        max_drawdown=float((equity / peak - 1).min()),
        t_stat_r=float(r.mean() / std_r * np.sqrt(len(r))) if std_r > 0 else None,
        total_costs=float(sum(t.total_cost for t in trades)),
    )


class _Masked(Strategy):
    """Lässt Einstiege nur in ``[start, end)`` zu; Indikatoren nutzen die Vorlaufdaten."""

    def __init__(self, base: Strategy, start: pd.Timestamp, end: pd.Timestamp | None = None):
        self.base, self.start, self.end = base, start, end
        self.name = base.name

    @property
    def warmup_bars(self) -> int:
        return self.base.warmup_bars

    def params(self) -> dict[str, Any]:
        return self.base.params()

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        sig = validate_signals(self.base.generate_signals(bars), bars)
        outside = bars.index < self.start
        if self.end is not None:
            outside |= bars.index >= self.end
        sig.loc[outside, "entry"] = False
        return sig


class _RandomEntries(Strategy):
    """Zufallseinstiege mit den SL/TP-Abständen der Originalstrategie (kostenbewusste Baseline)."""

    name = "random_baseline"

    def __init__(
        self, signals: pd.DataFrame, n_entries: int, rng: np.random.Generator, start: pd.Timestamp
    ) -> None:
        self.signals, self.n, self.rng, self.start = signals, n_entries, rng, start

    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        sig = self.signals.reindex(bars.index).copy()
        sig["entry"] = False
        sig["exit"] = False
        eligible = np.flatnonzero(
            (bars.index >= self.start)
            & sig["stop_distance"].gt(0).to_numpy()
            & sig["take_profit_distance"].gt(0).to_numpy()
        )
        if len(eligible):
            picks = self.rng.choice(eligible, size=min(self.n, len(eligible)), replace=False)
            sig.iloc[picks, sig.columns.get_loc("entry")] = True
        return sig


@dataclass
class SegmentResult:
    params: dict[str, Any]
    stats: TradeStats
    trades: list[Trade] = field(default_factory=list, repr=False)


@dataclass
class StrategyResearch:
    strategy: str
    family: str
    symbol: str
    data_source: str
    causal: bool
    chosen_params: dict[str, Any] | None
    selection_note: str
    train: SegmentResult | None
    validation: SegmentResult | None
    test: SegmentResult | None
    walk_forward: TradeStats
    walk_forward_trades: list[Trade] = field(default_factory=list, repr=False)
    fee_sensitivity: dict[str, TradeStats] = field(default_factory=dict)
    random_baseline: dict[str, float] = field(default_factory=dict)
    buy_and_hold_test: float | None = None
    regimes: dict[str, TradeStats] = field(default_factory=dict)
    regime_of_trade: dict[str, str] = field(default_factory=dict, repr=False)
    verdict: str = Verdict.HYPOTHESIS
    reasons: list[str] = field(default_factory=list)


def _scaled_fees(fees: FeeModel, m: float) -> FeeModel:
    return dataclasses.replace(
        fees,
        commission_rate=fees.commission_rate * m,
        min_commission=fees.min_commission * m,
        fixed_commission=fees.fixed_commission * m,
        spread_bps=fees.spread_bps * m,
        slippage_bps=fees.slippage_bps * m,
    )


class ResearchEngine:
    def __init__(
        self,
        instrument: Instrument,
        fees: FeeModel,
        cfg: ResearchConfig | None = None,
        risk: RiskConfig | None = None,
    ) -> None:
        self.instrument, self.fees = instrument, fees
        self.cfg = cfg or ResearchConfig()
        self.risk = risk or RiskConfig()

    def _run(
        self,
        bars: pd.DataFrame,
        strategy: Strategy,
        start: pd.Timestamp,
        end: pd.Timestamp | None,
        fees: FeeModel | None = None,
    ) -> list[Trade]:
        stop = len(bars) if end is None else int(bars.index.searchsorted(end))
        window = bars.iloc[:stop]
        engine = BacktestEngine(
            self.instrument, fees or self.fees, self.risk, self.cfg.initial_capital
        )
        res = engine.run(window, _Masked(strategy, start, end), verify_causality=False)
        return [t for t in res.trades if t.entry_time >= start]

    def _select(
        self,
        spec: StrategySpec,
        bars: pd.DataFrame,
        fit_start: pd.Timestamp,
        fit_end: pd.Timestamp,
        sel_end: pd.Timestamp | None,
    ) -> tuple[Any, SegmentResult | None, SegmentResult | None, str]:
        """Train auf [fit_start, fit_end), Auswahl auf [fit_end, sel_end)."""
        candidates = []
        for params in spec.grid:
            st = spec.build(params)
            tr = self._run(bars, st, fit_start, fit_end)
            s = trade_stats(tr, self.cfg.initial_capital)
            if s.n >= self.cfg.min_trades_select and (s.expectancy_r or 0) > 0:
                candidates.append((params, SegmentResult(st.params(), s, tr)))
        if not candidates:
            return spec.grid[0], None, None, "keine Variante im Training zulässig -> Standard"
        if sel_end is None:  # nur In-Sample-Auswahl (Walk-Forward)
            best = max(candidates, key=lambda c: c[1].stats.expectancy_r or -1e9)
            return best[0], best[1], None, "beste Trainingsvariante"
        scored = []
        for params, train_res in candidates:
            st = spec.build(params)
            vt = self._run(bars, st, fit_end, sel_end)
            vs = trade_stats(vt, self.cfg.initial_capital)
            score = vs.expectancy_r if vs.n >= self.cfg.min_trades_select else -1e9
            scored.append((score, params, train_res, SegmentResult(st.params(), vs, vt)))
        score, params, train_res, val_res = max(scored, key=lambda x: x[0])
        note = "beste Validierungsvariante" if score > -1e9 else "Validierung ohne genug Trades"
        return params, train_res, val_res, note

    def research(
        self, spec: StrategySpec, bars: pd.DataFrame, symbol: str, data_source: str = "unknown"
    ) -> StrategyResearch:
        cfg, n, idx = self.cfg, len(bars), bars.index
        causal = check_no_lookahead(
            spec.build(spec.grid[0]), bars, n_checkpoints=20, raise_on_violation=False
        ).ok
        t0 = idx[0]
        t_val = idx[int(n * cfg.train_frac)]
        t_test = idx[int(n * (cfg.train_frac + cfg.val_frac))]
        result = StrategyResearch(
            spec.name,
            spec.family,
            symbol,
            data_source,
            causal,
            None,
            "",
            None,
            None,
            None,
            TradeStats(),
        )
        if not causal:
            result.verdict = Verdict.REJECTED
            result.reasons.append("Look-Ahead-Bias festgestellt")
            return result
        params, train_res, val_res, note = self._select(spec, bars, t0, t_val, t_test)
        chosen = spec.build(params)
        test_trades = self._run(bars, chosen, t_test, None)
        result.chosen_params, result.selection_note = chosen.params(), note
        result.train, result.validation = train_res, val_res
        result.test = SegmentResult(
            chosen.params(), trade_stats(test_trades, cfg.initial_capital), test_trades
        )

        # Kosten-Sensitivität
        for m in cfg.fee_multipliers:
            tr = (
                test_trades
                if m == 1.0
                else self._run(bars, chosen, t_test, None, _scaled_fees(self.fees, m))
            )
            result.fee_sensitivity[f"x{m:g}"] = trade_stats(tr, cfg.initial_capital)

        # Baselines
        test_bars = bars[bars.index >= t_test]
        rt_cost = 2 * (self.fees.market_impact + self.fees.commission_rate)
        result.buy_and_hold_test = float(
            test_bars["close"].iloc[-1] / test_bars["open"].iloc[0] - 1 - rt_cost
        )
        result.random_baseline = self._random_baseline(
            bars, chosen, t_test, max(1, len(test_trades)), result.test.stats.net_profit
        )

        # Walk-Forward (Auswahl nur mit Daten vor dem jeweiligen OOS-Block)
        wf_trades: list[Trade] = []
        start_i = int(n * cfg.wf_initial_frac)
        bounds = np.linspace(start_i, n, cfg.wf_folds + 1).astype(int)
        for a, b in zip(bounds[:-1], bounds[1:], strict=True):
            if b - a < 2:
                continue
            fold_params, _, _, _ = self._select(spec, bars, t0, idx[a], None)
            end = idx[b] if b < n else None
            wf_trades += self._run(bars, spec.build(fold_params), idx[a], end)
        result.walk_forward = trade_stats(wf_trades, cfg.initial_capital)
        result.walk_forward_trades = wf_trades

        # Marktphasen (Regime der Signalkerze = Kerze vor dem Einstieg)
        regime = detect_regime(bars).shift(1)
        groups: dict[str, list[Trade]] = {}
        for t in test_trades:
            label = str(regime.get(t.entry_time, "unknown"))
            groups.setdefault(label, []).append(t)
            result.regime_of_trade[t.entry_time.isoformat()] = label
        for t in wf_trades:
            result.regime_of_trade.setdefault(
                t.entry_time.isoformat(), str(regime.get(t.entry_time, "unknown"))
            )
        result.regimes = {k: trade_stats(v, cfg.initial_capital) for k, v in groups.items()}
        self._judge(result)
        return result

    def _random_baseline(
        self,
        bars: pd.DataFrame,
        strategy: Strategy,
        start: pd.Timestamp,
        n_entries: int,
        strategy_profit: float,
    ) -> dict[str, float]:
        signals = validate_signals(strategy.generate_signals(bars), bars)
        rng = np.random.default_rng(self.cfg.seed)
        profits = []
        engine = BacktestEngine(self.instrument, self.fees, self.risk, self.cfg.initial_capital)
        for _ in range(self.cfg.random_runs):
            rnd = _RandomEntries(signals, n_entries, rng, start)
            res = engine.run(bars, rnd, verify_causality=False)
            profits.append(sum(t.net_pnl for t in res.trades if t.entry_time >= start))
        arr = np.array(profits) if profits else np.array([0.0])
        return {
            "runs": float(len(profits)),
            "mean_net_profit": float(arr.mean()),
            "p95_net_profit": float(np.percentile(arr, 95)),
            "p_value": float((np.sum(arr >= strategy_profit) + 1) / (len(arr) + 1)),
        }

    def _judge(self, r: StrategyResearch) -> None:
        cfg, test = self.cfg, r.test.stats if r.test else TradeStats()
        x2 = r.fee_sensitivity.get("x2", TradeStats())
        if test.n == 0:
            r.verdict = Verdict.HYPOTHESIS
            r.reasons.append("keine Trades im Testzeitraum")
            return
        if (test.expectancy_eur or 0) <= 0:
            r.verdict = Verdict.REJECTED
            r.reasons.append(f"Test-Erwartungswert nach Kosten {test.expectancy_eur:.4f} EUR ≤ 0")
            return
        checks = {
            f"≥ {cfg.min_test_trades} Test-Trades": test.n >= cfg.min_test_trades,
            "positiv bei doppelten Kosten": (x2.expectancy_eur or 0) > 0,
            f"Profit-Faktor ≥ {cfg.min_profit_factor}": (test.profit_factor or 0)
            >= cfg.min_profit_factor,
            f"besser als Zufall (p ≤ {cfg.alpha})": r.random_baseline.get("p_value", 1.0)
            <= cfg.alpha,
            "Walk-Forward-OOS positiv": (r.walk_forward.expectancy_eur or 0) > 0,
        }
        failed = [k for k, ok in checks.items() if not ok]
        if failed:
            r.verdict = Verdict.HYPOTHESIS
            r.reasons += [f"nicht erfüllt: {k}" for k in failed]
        else:
            r.verdict = Verdict.CONFIRMED
            r.reasons.append("alle Kriterien erfüllt (nur für diese Daten und diesen Zeitraum)")


def research_many(
    engine: ResearchEngine,
    specs: dict[str, StrategySpec],
    data: dict[str, pd.DataFrame],
    data_source: str,
) -> list[StrategyResearch]:
    out = []
    for symbol, bars in data.items():
        for spec in specs.values():
            out.append(engine.research(spec, bars, symbol, data_source))
    return out


def pooled_stats(
    results: list[StrategyResearch], strategy: str, initial_capital: float
) -> TradeStats:
    trades = []
    for r in results:
        if r.strategy == strategy and r.test:
            trades += r.test.trades + r.walk_forward_trades
    trades.sort(key=lambda t: t.entry_time)
    return trade_stats(trades, initial_capital)
