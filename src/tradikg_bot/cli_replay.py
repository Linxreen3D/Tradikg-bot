"""CLI: ``tradikg data …`` (historische Daten) und ``tradikg replay …`` (Walk-Forward-Replay).

Nur Datenabruf und Simulation. Kein Befehl hier sendet Orders.
"""

from __future__ import annotations

import argparse

import pandas as pd

from tradikg_bot.platform_config import PlatformConfig

SYNTHETIC_BASE_PRICES = {"XBTEUR": 60000.0, "ETHEUR": 3000.0, "SOLEUR": 150.0, "XRPEUR": 0.5}


def _cfg(args: argparse.Namespace) -> PlatformConfig:
    return PlatformConfig.load(getattr(args, "platform_config", None))


def _symbols(args: argparse.Namespace, cfg: PlatformConfig) -> list[str]:
    return args.symbols.split(",") if getattr(args, "symbols", None) else list(cfg.symbols)


# -- data ---------------------------------------------------------------------------------------
def cmd_data_fetch_kraken(args: argparse.Namespace) -> int:
    from tradikg_bot.live.kraken.client import KrakenClient
    from tradikg_bot.market.kraken_data import KrakenMarketData
    from tradikg_bot.storage import SQLiteStore

    cfg = _cfg(args)
    cfg.ensure_dirs()
    md = KrakenMarketData(KrakenClient(), SQLiteStore(cfg.market_db))
    for s in _symbols(args, cfg):
        n = md.update_cache(s, args.timeframe)
        print(f"{s} {args.timeframe}: {n} neue Kerzen (Kraken liefert höchstens 720 je Abruf)")
    return 0


def cmd_data_import_kraken(args: argparse.Namespace) -> int:
    from tradikg_bot.replay.data import import_kraken_ohlcvt
    from tradikg_bot.storage import SQLiteStore

    cfg = _cfg(args)
    cfg.ensure_dirs()
    symbols = _symbols(args, cfg)
    data = import_kraken_ohlcvt(args.path, symbols, args.timeframe)
    store = SQLiteStore(cfg.market_db)
    for s in symbols:
        if s not in data:
            print(f"{s}: keine Datei {s}_<Minuten>.csv gefunden")
            continue
        n = store.save_bars(s, args.timeframe, data[s], source="kraken-ohlcvt")
        print(
            f"{s} {args.timeframe}: {n} Kerzen importiert ({data[s].index[0].date()} – "
            f"{data[s].index[-1].date()})"
        )
    return 0


def cmd_data_fetch_ccxt(args: argparse.Namespace) -> int:
    from tradikg_bot.data.sources import fetch_crypto_ohlcv
    from tradikg_bot.storage import SQLiteStore

    cfg = _cfg(args)
    cfg.ensure_dirs()
    bars = fetch_crypto_ohlcv(args.symbol, args.timeframe, args.since, exchange_id=args.exchange)
    n = SQLiteStore(cfg.market_db).save_bars(
        args.as_symbol, args.timeframe, bars, source=f"ccxt:{args.exchange}"
    )
    print(
        f"{args.as_symbol} {args.timeframe}: {n} Kerzen von {args.exchange} ({args.symbol}). "
        "Hinweis: andere Börse als Kraken – Preise und Volumen weichen ab."
    )
    return 0


def cmd_data_coverage(args: argparse.Namespace) -> int:
    from tradikg_bot.replay.data import coverage
    from tradikg_bot.storage import SQLiteStore

    cfg = _cfg(args)
    store = SQLiteStore(cfg.market_db)
    data = {s: store.load_bars(s, args.timeframe) for s in _symbols(args, cfg)}
    data = {s: d for s, d in data.items() if not d.empty}
    if not data:
        print("Keine Daten im Cache. Erst `tradikg data fetch-kraken` oder `import-kraken`.")
        return 1
    for c in coverage(data, args.timeframe):
        phases = ", ".join(f"{k} {v:.0%}" for k, v in sorted(c.regime_share.items()))
        print(
            f"{c.symbol:8} {c.bars:6} Kerzen {c.start.date()} – {c.end.date()} | Lücken "
            f"{c.missing_bars} | Gesamt {c.total_return:+.0%} | Quellen "
            f"{','.join(store.bar_sources(c.symbol, args.timeframe))} | Phasen: {phases}"
        )
    return 0


# -- replay -------------------------------------------------------------------------------------
def _load(args: argparse.Namespace, cfg: PlatformConfig) -> tuple[dict, str, bool]:
    symbols = _symbols(args, cfg)
    if args.source == "synthetic":
        from tradikg_bot.replay.data import synthetic_market_phases

        freq = pd.Timedelta(args.timeframe.replace("m", "min").replace("d", "D"))
        data = {
            s: synthetic_market_phases(
                n=args.bars or 730,
                seed=i + 1,
                freq=freq,
                base_price=SYNTHETIC_BASE_PRICES.get(s, 100.0),
                correlation_seed=99,
            )
            for i, s in enumerate(symbols)
        }
        return data, "synthetisch (Marktphasen)", True
    from tradikg_bot.storage import SQLiteStore

    store = SQLiteStore(cfg.market_db)
    data = {}
    sources: set[str] = set()
    for s in symbols:
        bars = store.load_bars(s, args.timeframe)
        if args.bars:
            bars = bars.iloc[-args.bars :]
        if not bars.empty:
            data[s] = bars
            sources.update(store.bar_sources(s, args.timeframe))
    return data, "cache:" + "+".join(sorted(sources) or ["leer"]), False


def _rules(args: argparse.Namespace, symbols: list[str]):
    if args.rules != "kraken":
        return None
    from tradikg_bot.live.kraken.client import KrakenClient
    from tradikg_bot.market.kraken_data import KrakenMarketData

    pairs = KrakenMarketData(KrakenClient()).tradable_pairs("ZEUR")
    by_name = {p.altname: p for p in pairs}
    return {s: by_name[s] for s in symbols if s in by_name} or None


def cmd_replay_run(args: argparse.Namespace) -> int:
    from tradikg_bot.replay.report import render_replay_report
    from tradikg_bot.replay.store import ReplayStore, holdout_fingerprint
    from tradikg_bot.replay.walkforward import VARIANTS, HistoricalReplay, ReplayConfig

    cfg = _cfg(args)
    cfg.ensure_dirs()
    data, source, synthetic = _load(args, cfg)
    if not data:
        print("Keine Daten. Erst `tradikg data fetch-kraken` bzw. `tradikg data import-kraken`.")
        return 1
    rcfg = ReplayConfig(
        timeframe=args.timeframe,
        holdout_frac=args.holdout_frac,
        folds=args.folds,
        strategies=tuple(args.strategies.split(",")) if args.strategies else cfg.strategies,
        research_random_runs=args.research_random_runs,
        min_history_bars=args.min_history_bars,
        evaluate_holdout=not args.no_holdout,
        rolling_windows=not args.no_windows,
        variants=("bot", "no_filter", "random") if args.quick else tuple(VARIANTS),
    )
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S")
    replay = HistoricalReplay(
        data,
        cfg.replay_dir / stamp,
        rcfg,
        data_source=source,
        synthetic=synthetic,
        rules=_rules(args, list(data)),
        progress=lambda msg: print(f"  … {msg}", flush=True),
    )
    store = ReplayStore(cfg.replay_db)
    fingerprint = holdout_fingerprint(replay.data, replay.plan, args.timeframe)
    previous = store.holdout_count(fingerprint)
    if rcfg.evaluate_holdout and previous:
        print(
            f"WARNUNG: Der Holdout dieser Daten wurde bereits {previous}-mal ausgewertet. "
            "Wurde danach am Bot etwas geändert, ist er nicht mehr unangetastet."
        )
    for row in replay.plan.describe():
        print(
            f"{row['abschnitt']:28} Research {row['research_daten']:48} Bewertung "
            f"{row['bewertung']}"
        )
    result = replay.run()
    count = store.register_holdout(fingerprint) if rcfg.evaluate_holdout else previous
    report = render_replay_report(result, count)
    run_id = store.save(result, report, fingerprint, count)
    out = cfg.home / "reports" / f"replay-{run_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print()
    for s in result.select(variant="bot", profile="eur100"):
        if s.period != "window":
            print(
                f"100 € im {s.period:12} ({s.start.date()} – {s.end.date()}): "
                f"{s.final_equity:8.2f} € | {s.n_trades} Trades | Max. DD {s.max_drawdown:.1%}"
            )
    windows = [
        s.final_equity for s in result.select(period="window", variant="bot", profile="eur100")
    ]
    if windows:
        print(
            f"100 € in {len(windows)} Fenstern: min {min(windows):.2f} €, Median "
            f"{sorted(windows)[len(windows) // 2]:.2f} €, max {max(windows):.2f} €"
        )
    print(f"\nBericht: {out} (Replay-Lauf #{run_id}). Keine echten Orders.")
    return 0


def cmd_replay_report(args: argparse.Namespace) -> int:
    from tradikg_bot.replay.store import ReplayStore

    report = ReplayStore(_cfg(args).replay_db).report(args.run_id)
    print(report or "Noch kein Replay-Lauf vorhanden.")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--platform-config", help="Pfad zur Plattform-Konfiguration (TOML)")

    data = sub.add_parser("data", help="Historische Kursdaten für Research und Replay")
    ds = data.add_subparsers(dest="data_cmd", required=True)
    p = ds.add_parser("fetch-kraken", help="Kraken-OHLC laden (max. 720 Kerzen je Abruf)")
    common(p)
    p.add_argument("--symbols")
    p.add_argument("--timeframe", default="1d")
    p.set_defaults(func=cmd_data_fetch_kraken, platform_cmd=True)
    p = ds.add_parser("import-kraken", help="Kraken-OHLCVT-Download (ZIP/Ordner) importieren")
    common(p)
    p.add_argument("path", help="ZIP-Archiv oder Ordner mit <PAIR>_<Minuten>.csv")
    p.add_argument("--symbols")
    p.add_argument("--timeframe", default="1d")
    p.set_defaults(func=cmd_data_import_kraken, platform_cmd=True)
    p = ds.add_parser("fetch-ccxt", help="Lange Historie einer anderen Börse via ccxt laden")
    common(p)
    p.add_argument("--exchange", default="binance")
    p.add_argument("--symbol", required=True, help="ccxt-Symbol, z. B. BTC/EUR")
    p.add_argument("--as", dest="as_symbol", required=True, help="Name im Cache, z. B. XBTEUR")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--since", default="2019-01-01")
    p.set_defaults(func=cmd_data_fetch_ccxt, platform_cmd=True)
    p = ds.add_parser("coverage", help="Abdeckung und Marktphasen der gespeicherten Daten")
    common(p)
    p.add_argument("--symbols")
    p.add_argument("--timeframe", default="1d")
    p.set_defaults(func=cmd_data_coverage, platform_cmd=True)

    replay = sub.add_parser("replay", help="Historical Market Replay mit Walk-Forward-Tests")
    rs = replay.add_subparsers(dest="replay_cmd", required=True)
    p = rs.add_parser("run", help="Walk-Forward-Replay mit Holdout, Fenstern und Stresstests")
    common(p)
    p.add_argument("--source", choices=["cache", "synthetic"], default="cache")
    p.add_argument("--symbols")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--bars", type=int, default=0, help="nur die letzten N Kerzen (0 = alle)")
    p.add_argument("--strategies")
    p.add_argument("--folds", type=int, default=4)
    p.add_argument("--holdout-frac", type=float, default=0.2)
    p.add_argument("--research-random-runs", type=int, default=50)
    p.add_argument("--min-history-bars", type=int, default=150)
    p.add_argument("--no-holdout", action="store_true", help="Holdout bewusst nicht auswerten")
    p.add_argument("--no-windows", action="store_true", help="ohne rollierende Fenster")
    p.add_argument("--quick", action="store_true", help="ohne Robustheitsvarianten")
    p.add_argument(
        "--rules",
        choices=["example", "kraken"],
        default="example",
        help="Mindestgrößen: Beispielwerte oder aktuell von Kraken (öffentliche API)",
    )
    p.set_defaults(func=cmd_replay_run, platform_cmd=True)
    p = rs.add_parser("report", help="Replay-Bericht anzeigen")
    common(p)
    p.add_argument("--run-id", type=int)
    p.set_defaults(func=cmd_replay_report, platform_cmd=True)
