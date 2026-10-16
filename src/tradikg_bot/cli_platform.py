"""CLI der Plattform: ``tradikg init | doctor | research | ml | paper | backup``.

Alle Befehle arbeiten im Paper-/Research-Modus. Es gibt hier keinen Befehl, der
echte Orders sendet.
"""

from __future__ import annotations

import argparse
import importlib
import platform
import shutil
import sys

import pandas as pd

from tradikg_bot.platform_config import DEFAULT_CONFIG_TOML, PlatformConfig


def _cfg(args: argparse.Namespace) -> PlatformConfig:
    return PlatformConfig.load(getattr(args, "platform_config", None))


# -- init / doctor / backup -------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    cfg.ensure_dirs()
    cfg_file = cfg.home / "config.toml"
    if not cfg_file.exists():
        cfg_file.write_text(DEFAULT_CONFIG_TOML)
        print(f"Konfiguration angelegt: {cfg_file}")
    else:
        print(f"Konfiguration vorhanden (unverändert): {cfg_file}")
    from tradikg_bot.ml.registry import ModelRegistry
    from tradikg_bot.research.store import ResearchStore
    from tradikg_bot.storage import SQLiteStore

    SQLiteStore(cfg.market_db).close()
    ResearchStore(cfg.research_db).close()
    ModelRegistry(cfg.models_db).close()
    print(f"Datenverzeichnis: {cfg.data_dir}")
    return 0


def doctor_checks(cfg: PlatformConfig, network: bool = False) -> list[tuple[str, str, str]]:
    """(Prüfung, Status OK/WARN/FEHLER, Detail)"""
    out: list[tuple[str, str, str]] = []
    v = sys.version_info
    out.append(
        (
            "Python",
            "OK" if (3, 11) <= v[:2] <= (3, 13) else "FEHLER",
            f"{platform.python_version()} (unterstützt: 3.11–3.13)",
        )
    )
    system, machine = platform.system(), platform.machine()
    out.append(
        (
            "Plattform",
            "OK" if (system, machine) == ("Darwin", "arm64") else "WARN",
            f"{system} {machine} (Ziel: macOS arm64)",
        )
    )
    for mod, required in (
        ("numpy", True),
        ("pandas", True),
        ("sklearn", False),
        ("streamlit", False),
        ("psutil", False),
    ):
        try:
            m = importlib.import_module(mod)
            out.append((f"Paket {mod}", "OK", getattr(m, "__version__", "?")))
        except ImportError:
            out.append((f"Paket {mod}", "FEHLER" if required else "WARN", "nicht installiert"))
    try:
        cfg.ensure_dirs()
        probe = cfg.run_dir / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        out.append(("Verzeichnisse", "OK", str(cfg.home)))
    except OSError as exc:
        out.append(("Verzeichnisse", "FEHLER", str(exc)))
    free = shutil.disk_usage(cfg.home).free / 1e9
    out.append(("Freier Speicher", "OK" if free > 1 else "WARN", f"{free:.1f} GB"))
    from tradikg_bot.service.ops import integrity_check

    for db in sorted(cfg.data_dir.rglob("*.sqlite")):
        try:
            integrity_check(db)
            out.append((f"DB {db.name}", "OK", "Integrität geprüft"))
        except Exception as exc:
            out.append((f"DB {db.name}", "FEHLER", str(exc)))
    out.append(
        (
            "Kill Switch",
            "WARN" if cfg.kill_switch.exists() else "OK",
            "aktiv" if cfg.kill_switch.exists() else "nicht aktiv",
        )
    )
    out.append(("Modus", "OK", "paper (Echtgeld deaktiviert)"))
    if network:
        try:
            from tradikg_bot.live.kraken.client import KrakenClient

            KrakenClient(timeout=10).public("Time")
            out.append(("Kraken-API", "OK", "erreichbar"))
        except Exception as exc:
            out.append(("Kraken-API", "FEHLER", f"nicht erreichbar: {exc}"))
    return out


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = doctor_checks(_cfg(args), network=args.network)
    for name, status, detail in checks:
        print(f"[{status:6}] {name}: {detail}")
    return 1 if any(s == "FEHLER" for _, s, _ in checks) else 0


def cmd_backup(args: argparse.Namespace) -> int:
    from tradikg_bot.service.ops import backup_databases

    cfg = _cfg(args)
    created = backup_databases(cfg.data_dir, cfg.backup_dir, keep=args.keep)
    print(f"{len(created)} Datenbank(en) gesichert nach {cfg.backup_dir}")
    return 0


# -- Daten für Research/ML --------------------------------------------------------------------
def _load_data(
    cfg: PlatformConfig, source: str, symbols: list[str], timeframe: str, bars: int
) -> tuple[dict[str, pd.DataFrame], str]:
    if source == "synthetic":
        from tradikg_bot.data import generate_synthetic_ohlcv

        data = {
            s: generate_synthetic_ohlcv(
                bars,
                freq=pd.Timedelta(timeframe.replace("m", "min")),
                seed=i,
                periods_per_year=8760,
                annual_vol=0.7,
            )
            for i, s in enumerate(symbols)
        }
        return data, "synthetic"
    from tradikg_bot.storage import SQLiteStore

    store = SQLiteStore(cfg.market_db)
    if source == "kraken":
        from tradikg_bot.live.kraken.client import KrakenClient
        from tradikg_bot.market.kraken_data import KrakenMarketData

        md = KrakenMarketData(KrakenClient(), store)
        for s in symbols:
            n = md.update_cache(s, timeframe)
            print(f"{s}: {n} neue Kerzen von Kraken")
    data = {s: store.load_bars(s, timeframe).iloc[-bars:] for s in symbols}
    return {s: d for s, d in data.items() if not d.empty}, f"kraken-cache:{timeframe}"


def cmd_research_run(args: argparse.Namespace) -> int:
    from tradikg_bot.costs import KRAKEN_SPOT_ASSUMED
    from tradikg_bot.domain import Instrument
    from tradikg_bot.research.engine import ResearchConfig, ResearchEngine, research_many
    from tradikg_bot.research.report import render_report
    from tradikg_bot.research.store import ResearchStore
    from tradikg_bot.strategies.families import strategy_registry

    cfg = _cfg(args)
    cfg.ensure_dirs()
    symbols = args.symbols.split(",") if args.symbols else list(cfg.symbols)
    data, source = _load_data(cfg, args.source, symbols, args.timeframe, args.bars)
    if not data:
        print("Keine Daten vorhanden. Zuerst --source kraken nutzen oder Daten importieren.")
        return 1
    registry = strategy_registry()
    names = (
        args.strategies.split(",") if args.strategies else list(cfg.strategies) or list(registry)
    )
    rcfg = ResearchConfig(random_runs=args.random_runs)
    engine = ResearchEngine(Instrument.default_for("X", "crypto"), KRAKEN_SPOT_ASSUMED, rcfg)
    results = research_many(engine, {n: registry[n] for n in names}, data, source)
    report = render_report(results, rcfg, source, args.timeframe)
    store = ResearchStore(cfg.research_db)
    run_id = store.save(
        results,
        report,
        source,
        args.timeframe,
        {"random_runs": args.random_runs, "symbols": list(data)},
    )
    out = cfg.home / "reports" / f"research-{run_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    for r in results:
        s = r.test.stats if r.test else None
        print(f"{r.strategy:20} {r.symbol:8} {r.verdict:10} Test-Trades {s.n if s else 0}")
    print(f"\nBericht: {out} (Lauf #{run_id})")
    return 0


def cmd_research_report(args: argparse.Namespace) -> int:
    from tradikg_bot.research.store import ResearchStore

    report = ResearchStore(_cfg(args).research_db).report(args.run_id)
    print(report or "Noch kein Research-Lauf vorhanden.")
    return 0


def cmd_ml_train(args: argparse.Namespace) -> int:
    from tradikg_bot.ml.dataset import build_dataset
    from tradikg_bot.ml.registry import ModelRegistry
    from tradikg_bot.ml.train import train_and_evaluate
    from tradikg_bot.research.store import ResearchStore
    from tradikg_bot.trading.runner import strategies_for

    cfg = _cfg(args)
    symbols = args.symbols.split(",") if args.symbols else list(cfg.symbols)
    data, _ = _load_data(cfg, args.source, symbols, args.timeframe, args.bars)
    stats = ResearchStore(cfg.research_db).oos_stats()
    dataset = build_dataset(data, strategies_for(list(data), stats, list(cfg.strategies) or None))
    tf = pd.Timedelta(args.timeframe.replace("m", "min"))
    result = train_and_evaluate(dataset, ModelRegistry(cfg.models_db), embargo=48 * tf)
    print(f"Datensatz: {len(dataset)} Signale")
    for note in result.notes:
        print(f"Hinweis: {note}")
    for name, m in result.metrics.items():
        print(
            f"{name}: Brier {m['brier']:.4f}, LogLoss {m['log_loss']:.4f}, ECE {m['ece']:.3f}, "
            f"n={m['n']}"
        )
    if result.decision:
        print(
            "Entscheidung:",
            "Champion gewechselt" if result.decision.promote else "Champion bleibt",
            "–",
            "; ".join(result.decision.reasons),
        )
    return 0


# -- Paper ----------------------------------------------------------------------------------
def cmd_paper_run(args: argparse.Namespace) -> int:
    from tradikg_bot.service.ops import setup_logging
    from tradikg_bot.service.paper_service import PaperService

    cfg = _cfg(args)
    log = setup_logging(cfg.log_dir, cfg.log_level)
    service = PaperService(cfg, logger=log)
    if args.once:
        status = service.run_once()
        print(
            f"Schritte verarbeitet: {status['processed_steps']}, Sperre: "
            f"{status['entries_blocked']}"
        )
        return 0
    service.run_forever()
    return 0


def cmd_paper_replay(args: argparse.Namespace) -> int:
    from tradikg_bot.live.paper import PAPER_RULES, generic_rules
    from tradikg_bot.ml.registry import ModelRegistry
    from tradikg_bot.research.store import ResearchStore
    from tradikg_bot.trading.engine import PaperEngineConfig
    from tradikg_bot.trading.probability import EmpiricalProbability, ModelProbability
    from tradikg_bot.trading.runner import PaperPaths, open_engine, replay, strategies_for
    from tradikg_bot.trading.sources import PrecomputedSignals

    cfg = _cfg(args)
    symbols = args.symbols.split(",") if args.symbols else list(cfg.symbols)
    data, source = _load_data(cfg, args.source, symbols, args.timeframe, args.bars)
    stats = ResearchStore(cfg.research_db).oos_stats()
    rules = {
        s: PAPER_RULES.get(s) or generic_rules(s, float(d["close"].iloc[0]))
        for s, d in data.items()
    }
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S")
    root = cfg.data_dir / "replays" / stamp
    probability = ModelProbability(ModelRegistry(cfg.models_db), EmpiricalProbability(stats))
    signals = PrecomputedSignals(
        data, strategies_for(list(data), stats, list(cfg.strategies) or None)
    )
    engine = open_engine(
        PaperPaths(root),
        rules,
        probability,
        signals,
        PaperEngineConfig(
            timeframe=args.timeframe,
            capital_eur=cfg.capital_eur,
            min_history_bars=cfg.min_history_bars,
        ),
    )
    reports = replay(engine, data)
    b = engine.ledger.balances()
    print(f"Replay ({source}) über {len(reports)} Schritte -> {root}")
    print(
        f"Trades geschlossen: {sum(len(r.closed) for r in reports)}, Netto {b.total_net_pnl:+.4f}"
        f" EUR, Trading {b.trading_balance:.2f}, Reserve {b.payout_reserve:.2f}, "
        f"Langfrist {b.longterm_portfolio:.2f}"
    )
    if not stats:
        print("Hinweis: keine Research-Ergebnisse -> ohne OOS-Belege wird nicht gehandelt.")
    return 0


def _paper_engine_store(cfg: PlatformConfig):
    from tradikg_bot.live.journal import ExecutionJournal
    from tradikg_bot.trading.ledger import CapitalLedger

    db = cfg.paper_dir / "paper.sqlite"
    return ExecutionJournal(db), CapitalLedger(db, cfg.capital_eur, tax_rate=cfg.tax_rate)


def cmd_paper_status(args: argparse.Namespace) -> int:
    from tradikg_bot.service.ops import read_status

    cfg = _cfg(args)
    journal, ledger = _paper_engine_store(cfg)
    b = ledger.balances()
    print(
        f"Trading-Balance {b.trading_balance:.4f} | Auszahlungsreserve {b.payout_reserve:.4f} | "
        f"Langfrist {b.longterm_portfolio:.4f} | offene Verluste {b.unrecovered_losses:.4f}"
    )
    print(
        f"SAFE_HALT: {journal.halt_reason or 'nein'} | Kill Switch: "
        f"{'aktiv' if cfg.kill_switch.exists() else 'aus'}"
    )
    for t in journal.trades(active_only=True):
        print(
            f"offen: #{t.id} {t.pair} {t.filled_volume} @ {t.entry_avg_price} "
            f"SL {t.stop_loss_price} TP {t.take_profit_price}"
        )
    status = read_status(cfg.status_file)
    print(f"Letzter Durchgang: {status['updated_at'] if status else 'noch keiner'}")
    return 0


def cmd_paper_kill(args: argparse.Namespace) -> int:
    from tradikg_bot.trading.engine import KillSwitch

    cfg = _cfg(args)
    ks = KillSwitch(cfg.kill_switch)
    if args.release:
        ks.release()
        print("Kill Switch gelöst – neue Einstiege wieder möglich.")
    else:
        ks.engage(args.reason)
        print("Kill Switch aktiv – keine neuen Einstiege. Offene Positionen bleiben geschützt.")
    return 0


def cmd_paper_reset_halt(args: argparse.Namespace) -> int:
    journal, _ = _paper_engine_store(_cfg(args))
    print(f"Bisherige Sperre: {journal.halt_reason or 'keine'}")
    journal.clear_halt(args.note)
    print("SAFE_HALT aufgehoben.")
    return 0


def cmd_paper_report(args: argparse.Namespace) -> int:
    from tradikg_bot.trading.analysis import daily_report
    from tradikg_bot.trading.store import PaperStore

    cfg = _cfg(args)
    store = PaperStore(cfg.paper_dir / "paper.sqlite")
    day = args.day or pd.Timestamp.now(tz="UTC").date().isoformat()
    md, _ = daily_report(day, store.analyses(day))
    print(md)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--platform-config", help="Pfad zur Plattform-Konfiguration (TOML)")

    p = sub.add_parser("init", help="Verzeichnisse, Konfiguration und Datenbanken anlegen")
    common(p)
    p.set_defaults(func=cmd_init, platform_cmd=True)

    p = sub.add_parser("doctor", help="Umgebung prüfen")
    common(p)
    p.add_argument("--network", action="store_true", help="auch Kraken-Erreichbarkeit prüfen")
    p.set_defaults(func=cmd_doctor, platform_cmd=True)

    p = sub.add_parser("backup", help="Datenbanken sichern")
    common(p)
    p.add_argument("--keep", type=int, default=14)
    p.set_defaults(func=cmd_backup, platform_cmd=True)

    research = sub.add_parser("research", help="Strategie-Research")
    rs = research.add_subparsers(dest="research_cmd", required=True)
    p = rs.add_parser("run", help="Walk-Forward-Research ausführen")
    common(p)
    p.add_argument("--symbols")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--source", choices=["kraken", "cache", "synthetic"], default="cache")
    p.add_argument("--strategies")
    p.add_argument("--random-runs", type=int, default=200)
    p.add_argument("--bars", type=int, default=5000)
    p.set_defaults(func=cmd_research_run, platform_cmd=True)
    p = rs.add_parser("report", help="Research-Bericht anzeigen")
    common(p)
    p.add_argument("--run-id", type=int)
    p.set_defaults(func=cmd_research_report, platform_cmd=True)

    ml = sub.add_parser("ml", help="Machine Learning")
    ms = ml.add_subparsers(dest="ml_cmd", required=True)
    p = ms.add_parser("train", help="Baseline + Challenger trainieren und vergleichen")
    common(p)
    p.add_argument("--symbols")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--source", choices=["cache", "synthetic"], default="cache")
    p.add_argument("--bars", type=int, default=5000)
    p.set_defaults(func=cmd_ml_train, platform_cmd=True)

    paper = sub.add_parser("paper", help="Autonomer Paper-Handel")
    ps = paper.add_subparsers(dest="paper_cmd", required=True)
    p = ps.add_parser("run", help="Paper-Dauerbetrieb mit Kraken-Daten")
    common(p)
    p.add_argument("--once", action="store_true", help="nur einen Durchgang")
    p.set_defaults(func=cmd_paper_run, platform_cmd=True)
    p = ps.add_parser("replay", help="Historisches Replay (Multi-Asset-Backtest)")
    common(p)
    p.add_argument("--symbols")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--source", choices=["cache", "synthetic"], default="cache")
    p.add_argument("--bars", type=int, default=2000)
    p.set_defaults(func=cmd_paper_replay, platform_cmd=True)
    p = ps.add_parser("status", help="Salden, offene Positionen, Sperren")
    common(p)
    p.set_defaults(func=cmd_paper_status, platform_cmd=True)
    p = ps.add_parser("kill", help="Not-Aus setzen oder lösen")
    common(p)
    p.add_argument("--reason", default="manuell")
    p.add_argument("--release", action="store_true")
    p.set_defaults(func=cmd_paper_kill, platform_cmd=True)
    p = ps.add_parser("reset-halt", help="SAFE_HALT nach Prüfung aufheben")
    common(p)
    p.add_argument("--note", required=True)
    p.set_defaults(func=cmd_paper_reset_halt, platform_cmd=True)
    p = ps.add_parser("report", help="Tagesbericht")
    common(p)
    p.add_argument("--day")
    p.set_defaults(func=cmd_paper_report, platform_cmd=True)
