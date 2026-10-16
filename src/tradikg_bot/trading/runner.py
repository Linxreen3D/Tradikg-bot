"""Aufbau des Paper-Kreislaufs und historisches Replay (= Multi-Asset-Backtest).

Das Replay füttert historische Kerzen Zeitschritt für Zeitschritt in denselben
Kreislauf, der auch im laufenden Paper-Betrieb arbeitet. Damit sind Backtest und
Paper-Betrieb dieselbe Logik (gleiche Signale, Kosten, Risiko- und Ausführungspfade).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from tradikg_bot.live.config import LiveRiskConfig
from tradikg_bot.live.journal import ExecutionJournal
from tradikg_bot.live.models import FeeRates, PairRules
from tradikg_bot.live.paper import PaperExchange
from tradikg_bot.research.store import OosStats
from tradikg_bot.strategies.base import Strategy
from tradikg_bot.strategies.families import strategy_registry
from tradikg_bot.trading.engine import (
    Bar,
    KillSwitch,
    PaperEngineConfig,
    PaperTradingEngine,
    StepReport,
)
from tradikg_bot.trading.ledger import CapitalLedger
from tradikg_bot.trading.probability import ProbabilityModel
from tradikg_bot.trading.sources import SignalSource
from tradikg_bot.trading.store import PaperStore

DEFAULT_PAPER_FEES = FeeRates(0.004, 0.0025, "Annahme: Kraken-Pro-Grundstufe (nicht verifiziert)")


@dataclass
class PaperPaths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "paper.sqlite"

    @property
    def kill_switch(self) -> Path:
        return self.root / "KILL_SWITCH"


def strategies_for(
    symbols: list[str], stats: list[OosStats], enabled: list[str] | None = None
) -> dict[str, list[Strategy]]:
    """Strategien je Symbol mit den in der Research gewählten Parametern (sonst Standard)."""
    registry = strategy_registry()
    names = enabled or list(registry)
    chosen = {(s.strategy, s.symbol): s.params for s in stats if s.regime == "*" and s.params}
    out: dict[str, list[Strategy]] = {}
    for sym in symbols:
        out[sym] = []
        for name in names:
            spec = registry[name]
            params = chosen.get((name, sym))
            out[sym].append(spec.build(type(spec.grid[0])(**params) if params else spec.grid[0]))
    return out


def open_engine(
    paths: PaperPaths,
    rules: dict[str, PairRules],
    probability: ProbabilityModel,
    source: SignalSource,
    config: PaperEngineConfig,
    fees: FeeRates = DEFAULT_PAPER_FEES,
    risk: LiveRiskConfig | None = None,
) -> PaperTradingEngine:
    paths.root.mkdir(parents=True, exist_ok=True)
    store = PaperStore(paths.db)
    state = store.get("paper_exchange")
    if state is not None:
        try:
            gateway = PaperExchange.from_state(json.loads(state))
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Gespeicherter Paper-Börsenzustand ist beschädigt ({exc}). Kein automatisches "
                "Zurücksetzen – Datenbank aus dem Backup wiederherstellen oder bewusst neu anlegen."
            ) from exc
        for name, r in rules.items():
            gateway.rules.setdefault(name, r)
    else:
        gateway = PaperExchange(
            rules=dict(rules),
            fees=fees,
            balances_={"ZEUR": config.capital_eur},
            native_bracket=True,
        )
    journal = ExecutionJournal(paths.db)
    ledger = CapitalLedger(paths.db, config.capital_eur, tax_rate=config.tax_rate)
    return PaperTradingEngine(
        gateway,
        store,
        journal,
        ledger,
        probability,
        source,
        config,
        risk=risk,
        kill_switch=KillSwitch(paths.kill_switch),
    )


def replay(engine: PaperTradingEngine, data: dict[str, pd.DataFrame]) -> list[StepReport]:
    stamps = sorted(set().union(*[set(df.index) for df in data.values()]))
    reports = []
    for ts in stamps:
        bars = {
            sym: Bar(*(float(df.at[ts, c]) for c in ("open", "high", "low", "close", "volume")))
            for sym, df in data.items()
            if ts in df.index
        }
        reports.append(engine.step(pd.Timestamp(ts), bars))
    return reports
