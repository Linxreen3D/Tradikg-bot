"""Paper-Dauerbetrieb: echte Kraken-Marktdaten -> autonomer Paper-Kreislauf.

Ablauf je Durchgang (``run_once``):
1. Neue abgeschlossene Kerzen je Symbol in den Cache laden (Rate-Limit, Retry).
2. Beim ersten Start Vorlaufdaten ohne Handel laden (``seed_history``).
3. Alle noch nicht verarbeiteten Zeitschritte in Reihenfolge an den Kreislauf
   geben. Liegt ein Schritt mehr als zwei Intervalle zurück (Schlafmodus,
   Netzausfall), werden nur Exits verarbeitet, keine neuen Einstiege.
4. Status und Heartbeat schreiben.

``run_forever`` wartet bis kurz nach dem nächsten Kerzenschluss, erkennt
Zeitsprünge (Schlafmodus) und arbeitet Fehler mit Backoff ab. Der Prozess
benötigt weder Claude noch OpenAI.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

import pandas as pd

from tradikg_bot.live.kraken.client import KrakenClient
from tradikg_bot.live.paper import PAPER_RULES
from tradikg_bot.market.kraken_data import KrakenMarketData, PublicApi, is_stale
from tradikg_bot.market.scanner import ScanConfig, scan_markets
from tradikg_bot.ml.registry import ModelRegistry
from tradikg_bot.platform_config import PlatformConfig
from tradikg_bot.research.store import ResearchStore
from tradikg_bot.service.ops import InstanceLock, integrity_check, write_status
from tradikg_bot.storage import SQLiteStore
from tradikg_bot.trading.engine import Bar, PaperEngineConfig
from tradikg_bot.trading.probability import EmpiricalProbability, ModelProbability
from tradikg_bot.trading.runner import PaperPaths, open_engine, strategies_for
from tradikg_bot.trading.sources import RollingSignals


class PaperService:
    def __init__(
        self,
        cfg: PlatformConfig,
        api: PublicApi | None = None,
        now: Callable[[], pd.Timestamp] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("tradikg")
        self._now = now or (lambda: pd.Timestamp.now(tz="UTC"))
        cfg.ensure_dirs()
        for db in (cfg.market_db, cfg.research_db, cfg.models_db, cfg.paper_dir / "paper.sqlite"):
            integrity_check(db)
        self.market_store = SQLiteStore(cfg.market_db)
        self.md = KrakenMarketData(api or KrakenClient(), self.market_store, now=self._now)
        self.research = ResearchStore(cfg.research_db)
        self.registry = ModelRegistry(cfg.models_db)
        self.symbols = self._resolve_symbols()
        rules = self._rules()
        self.errors: dict[str, str] = {}
        if cfg.strategy_mode == "trend":
            from tradikg_bot.trading.allocation import TrendConfig, TrendPaperEngine

            tcfg = TrendConfig(invest_fraction=cfg.invest_fraction, cash_yield_pa=cfg.cash_yield_pa)
            self.engine = TrendPaperEngine(
                PaperPaths(cfg.paper_dir), rules, cfg.capital_eur, cfg.timeframe, cfg.tax_rate, tcfg
            )
            self.log.info("Modus: Trendfolge-Allokation (SMA 200) für %s", self.symbols)
            return
        stats = self.research.oos_stats()
        if not stats:
            self.log.warning("Keine Research-Ergebnisse: ohne OOS-Belege wird nicht gehandelt.")
        probability = ModelProbability(self.registry, EmpiricalProbability(stats))
        source = RollingSignals(strategies_for(self.symbols, stats, list(cfg.strategies) or None))
        self.engine = open_engine(
            PaperPaths(cfg.paper_dir),
            rules,
            probability,
            source,
            PaperEngineConfig(
                timeframe=cfg.timeframe,
                capital_eur=cfg.capital_eur,
                min_history_bars=cfg.min_history_bars,
                tax_rate=cfg.tax_rate,
            ),
        )
        self.errors: dict[str, str] = {}

    def _resolve_symbols(self) -> list[str]:
        if not self.cfg.use_scanner:
            return list(self.cfg.symbols)
        try:
            result = scan_markets(
                self.md, ScanConfig(capital_eur=self.cfg.capital_eur, max_candidates=10)
            )
            symbols = [c.rules.altname for c in result.candidates]
            self.log.info("Scanner: %s", symbols)
            return symbols or list(self.cfg.symbols)
        except Exception as exc:
            self.log.warning("Scanner fehlgeschlagen (%s) – nutze konfigurierte Symbole", exc)
            return list(self.cfg.symbols)

    def _rules(self) -> dict[str, Any]:
        try:
            available = {r.altname: r for r in self.md.tradable_pairs("ZEUR")}
        except Exception as exc:
            self.log.warning("Handelsregeln nicht abrufbar (%s) – Beispielwerte", exc)
            available = {}
        rules = {}
        for sym in self.symbols:
            if sym in available:
                rules[sym] = available[sym]
            elif sym in PAPER_RULES:
                rules[sym] = PAPER_RULES[sym]
            else:
                self.log.warning("%s ist bei Kraken nicht handelbar und wird ignoriert", sym)
        self.symbols = [s for s in self.symbols if s in rules]
        return rules

    # -- ein Durchgang -------------------------------------------------------------------------
    def run_once(self) -> dict[str, Any]:
        now = self._now()
        tf, step = self.cfg.timeframe, self.engine.cfg.step
        for sym in self.symbols:
            try:
                self.md.update_cache(sym, tf)
                self.errors.pop(sym, None)
            except Exception as exc:
                self.errors[sym] = str(exc)
                self.log.error("Marktdaten %s: %s", sym, exc)
        data = {s: self.market_store.load_bars(s, tf) for s in self.symbols}
        data = {s: df for s, df in data.items() if not df.empty}
        last = self.engine.store.last_step()
        if last is None and data:
            newest = max(df.index[-1] for df in data.values())
            warmup = max(self.cfg.min_history_bars, getattr(self.engine, "min_history", 0))
            seeded = self.engine.seed_history(
                {s: df[df.index < newest].iloc[-(warmup + 50) :] for s, df in data.items()}
            )
            self.log.info("Erststart: %s Vorlaufkerzen ohne Handel geladen", seeded)
            last = self.engine.store.last_step()
        stamps = sorted(
            {ts for df in data.values() for ts in df.index if last is None or ts > last}
        )
        processed = 0
        for ts in stamps:
            bars = {
                s: Bar(*(float(df.at[ts, c]) for c in ("open", "high", "low", "close", "volume")))
                for s, df in data.items()
                if ts in df.index
            }
            fresh = now - (ts + step) <= 2 * step
            report = self.engine.step(ts, bars, allow_entries=fresh)
            processed += 1
            if report.executed or report.closed:
                self.log.info(
                    "Schritt %s: eröffnet %s, geschlossen %s", ts, report.executed, report.closed
                )
        stale = [s for s, df in data.items() if is_stale(df.index[-1], tf, now)]
        stale += [s for s in self.symbols if s not in data]
        status = {
            "updated_at": now.isoformat(),
            "mode": "paper",
            "symbols": self.symbols,
            "processed_steps": processed,
            "last_step": str(self.engine.store.last_step()),
            "stale_feeds": stale,
            "data_errors": self.errors,
            "data_last_success": str(self.md.last_success),
            "entries_blocked": self.engine.entries_blocked(),
            "safe_halt": self.engine.journal.halt_reason,
            "kill_switch": self.engine.kill.engaged,
            "balances": self.engine.ledger.balances().__dict__,
        }
        write_status(self.cfg.status_file, status)
        self.engine.store.set_health("heartbeat", now.isoformat(), now)
        self.engine.store.set_health("stale_feeds", ",".join(stale), now)
        self.engine.store.set_health("data_errors", str(self.errors), now)
        return status

    # -- Dauerbetrieb ------------------------------------------------------------------------------
    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        step_s = self.engine.cfg.step.total_seconds()
        backoff = 30.0
        with InstanceLock(self.cfg.lock_file):
            self.log.info("Paper-Service gestartet (PID-Sperre %s)", self.cfg.lock_file)
            while not stop.is_set():
                wall_before, mono_before = time.time(), time.monotonic()
                try:
                    self.run_once()
                    backoff = 30.0
                except Exception:
                    self.log.exception(
                        "Durchgang fehlgeschlagen – erneuter Versuch in %ss", backoff
                    )
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 900.0)
                    continue
                now = time.time()
                wait = step_s - (now % step_s) + 20.0  # kurz nach Kerzenschluss
                stop.wait(wait)
                jump = (time.time() - wall_before) - (time.monotonic() - mono_before)
                if jump > 60:
                    self.log.warning(
                        "Zeitsprung von %.0f s erkannt (Schlafmodus?) – hole nach", jump
                    )
            self.log.info("Paper-Service beendet")
