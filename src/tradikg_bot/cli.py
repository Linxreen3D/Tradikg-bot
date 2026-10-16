"""Kommandozeile: ``tradikg <befehl>`` bzw. ``python -m tradikg_bot <befehl>``.

Backtest-Befehle handeln ausschließlich virtuell. Die Kraken-Pilot-Befehle
(``tradikg kraken …``) sind in ``tradikg_bot/live/cli_kraken.py`` beschrieben;
Echtgeldorders sind dort mehrfach gesperrt.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from tradikg_bot.backtest import BacktestEngine, format_report
from tradikg_bot.config import AppConfig
from tradikg_bot.costs import default_fee_model
from tradikg_bot.data import (
    generate_synthetic_ohlcv,
    load_csv,
    timeframe_to_timedelta,
    validate_ohlcv,
)
from tradikg_bot.domain import AssetClass, Instrument
from tradikg_bot.storage import SQLiteStore
from tradikg_bot.strategies import SmaCrossoverParams, SmaCrossoverStrategy

DEFAULT_CONFIG = Path("config/default.toml")


def _load_config(path: str | None) -> AppConfig:
    if path:
        return AppConfig.from_toml(path)
    if DEFAULT_CONFIG.exists():
        return AppConfig.from_toml(DEFAULT_CONFIG)
    return AppConfig()


def _store(args: argparse.Namespace, cfg: AppConfig) -> SQLiteStore:
    return SQLiteStore(args.db or cfg.db_path)


def _register(store: SQLiteStore, symbol: str, asset_class: str) -> Instrument:
    instrument = store.get_instrument(symbol) or Instrument.default_for(symbol, asset_class)
    store.upsert_instrument(instrument)
    return instrument


def cmd_sample_data(args: argparse.Namespace, cfg: AppConfig) -> int:
    bars = generate_synthetic_ohlcv(
        n_bars=args.bars,
        start=args.start,
        freq="1D",
        start_price=args.price,
        seed=args.seed,
        annual_vol=args.vol,
    )
    with _store(args, cfg) as store:
        _register(store, args.symbol, args.asset_class)
        n = store.save_bars(args.symbol, "1d", bars, source="synthetic")
    print(f"{n} synthetische Tageskerzen für {args.symbol} gespeichert (keine echten Marktdaten).")
    return 0


def _save_validated(args, cfg, raw, source: str) -> int:
    bars, report = validate_ohlcv(raw, expected_freq=timeframe_to_timedelta(args.timeframe))
    with _store(args, cfg) as store:
        _register(store, args.symbol, args.asset_class)
        n = store.save_bars(args.symbol, args.timeframe, bars, source=source)
    print(f"Validierung: {report.summary()}")
    print(f"{n} Kerzen für {args.symbol} ({args.timeframe}) gespeichert.")
    return 0


def cmd_import_csv(args: argparse.Namespace, cfg: AppConfig) -> int:
    raw = load_csv(args.path, timestamp_column=args.timestamp_column)
    return _save_validated(args, cfg, raw, source=f"csv:{Path(args.path).name}")


def cmd_fetch(args: argparse.Namespace, cfg: AppConfig) -> int:
    from tradikg_bot.data import fetch_crypto_ohlcv, fetch_stock_ohlcv

    if args.asset_class == AssetClass.CRYPTO:
        raw = fetch_crypto_ohlcv(
            args.symbol, args.timeframe, since=args.start, until=args.end, exchange_id=args.exchange
        )
        source = f"ccxt:{args.exchange}"
    else:
        raw = fetch_stock_ohlcv(args.symbol, args.timeframe, start=args.start, end=args.end)
        source = "yfinance"
    return _save_validated(args, cfg, raw, source=source)


def cmd_list_data(args: argparse.Namespace, cfg: AppConfig) -> int:
    with _store(args, cfg) as store:
        series = store.list_series()
    print("Keine Daten gespeichert." if series.empty else series.to_string(index=False))
    return 0


def cmd_backtest(args: argparse.Namespace, cfg: AppConfig) -> int:
    with _store(args, cfg) as store:
        if args.csv:
            raw = load_csv(args.csv)
            bars, report = validate_ohlcv(raw, expected_freq=timeframe_to_timedelta(args.timeframe))
            print(f"Validierung: {report.summary()}")
            instrument = Instrument.default_for(args.symbol, args.asset_class or "crypto")
        else:
            bars = store.load_bars(args.symbol, args.timeframe, args.start, args.end)
            if bars.empty:
                print(
                    f"Keine Daten für {args.symbol} ({args.timeframe}). "
                    "Erst 'sample-data', 'import-csv' oder 'fetch' ausführen.",
                    file=sys.stderr,
                )
                return 1
            instrument = store.get_instrument(args.symbol) or Instrument.default_for(
                args.symbol, args.asset_class or "crypto"
            )
        if args.asset_class and instrument.asset_class != AssetClass(args.asset_class):
            instrument = Instrument.default_for(args.symbol, args.asset_class)

        fees = default_fee_model(instrument.asset_class)
        overrides = {
            k: v
            for k, v in {
                "commission_rate": args.commission_rate,
                "fixed_commission": args.fixed_fee,
                "spread_bps": args.spread_bps,
                "slippage_bps": args.slippage_bps,
            }.items()
            if v is not None
        }
        fees = dataclasses.replace(fees, **overrides)

        strategy = SmaCrossoverStrategy(
            SmaCrossoverParams(
                fast=args.fast,
                slow=args.slow,
                trend=None if args.trend == 0 else args.trend,
                atr_stop_mult=args.atr_stop,
                atr_take_profit_mult=args.atr_tp,
            )
        )
        engine = BacktestEngine(instrument, fees, cfg.risk, initial_capital=cfg.initial_capital)
        result = engine.run(
            bars, strategy, benchmark_daily_volume_return=cfg.daily_volume_return_benchmark
        )
        print(format_report(result, currency=cfg.currency))
        if not args.no_save:
            run_id = store.save_backtest(result, timeframe=args.timeframe)
            print(f"\nErgebnis als Lauf #{run_id} in {store.path} gespeichert.")
        if args.export_trades:
            Path(args.export_trades).parent.mkdir(parents=True, exist_ok=True)
            result.trades_frame().to_csv(args.export_trades, index=False)
            print(f"Trades exportiert nach {args.export_trades}")
    return 0


def cmd_runs(args: argparse.Namespace, cfg: AppConfig) -> int:
    with _store(args, cfg) as store:
        runs = store.load_runs()
    print("Keine Backtest-Läufe gespeichert." if runs.empty else runs.to_string(index=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradikg", description="Tradikg-Bot – Backtesting (ausschließlich virtuell)"
    )
    parser.add_argument("--config", help="Pfad zur TOML-Konfiguration")
    parser.add_argument("--db", help="Pfad zur SQLite-Datenbank (überschreibt Konfiguration)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sample-data", help="Synthetische Demo-Daten erzeugen und speichern")
    p.add_argument("--symbol", default="DEMO-EUR")
    p.add_argument("--asset-class", choices=[a.value for a in AssetClass], default="crypto")
    p.add_argument("--bars", type=int, default=1500)
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--price", type=float, default=100.0)
    p.add_argument("--vol", type=float, default=0.6, help="annualisierte Volatilität")
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(func=cmd_sample_data)

    def add_series_args(sp: argparse.ArgumentParser, asset_default: str | None) -> None:
        sp.add_argument("--symbol", required=True)
        sp.add_argument("--timeframe", default="1d")
        sp.add_argument(
            "--asset-class", choices=[a.value for a in AssetClass], default=asset_default
        )

    p = sub.add_parser("import-csv", help="OHLCV-CSV validieren und importieren")
    p.add_argument("path")
    p.add_argument("--timestamp-column", default=None)
    add_series_args(p, "crypto")
    p.set_defaults(func=cmd_import_csv)

    p = sub.add_parser("fetch", help="Kostenlose öffentliche Kursdaten laden (optional)")
    add_series_args(p, "crypto")
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--exchange", default="binance", help="ccxt-Börsen-ID (nur Krypto)")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("list-data", help="Gespeicherte Kursreihen anzeigen")
    p.set_defaults(func=cmd_list_data)

    p = sub.add_parser("backtest", help="Baseline-Strategie backtesten")
    add_series_args(p, None)
    p.add_argument("--csv", help="Statt Datenbank direkt eine CSV verwenden")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--fast", type=int, default=20)
    p.add_argument("--slow", type=int, default=50)
    p.add_argument("--trend", type=int, default=100, help="0 deaktiviert den Trendfilter")
    p.add_argument("--atr-stop", type=float, default=2.0)
    p.add_argument("--atr-tp", type=float, default=3.0)
    p.add_argument("--commission-rate", type=float, default=None)
    p.add_argument("--fixed-fee", type=float, default=None)
    p.add_argument("--spread-bps", type=float, default=None)
    p.add_argument("--slippage-bps", type=float, default=None)
    p.add_argument("--no-save", action="store_true", help="Ergebnis nicht speichern")
    p.add_argument("--export-trades", help="Trades zusätzlich als CSV exportieren")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("runs", help="Gespeicherte Backtest-Läufe anzeigen")
    p.set_defaults(func=cmd_runs)

    from tradikg_bot.cli_platform import register as register_platform
    from tradikg_bot.live.cli_kraken import register

    register(sub)
    register_platform(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "kraken" or getattr(args, "platform_cmd", False):
        return int(args.func(args))
    cfg = _load_config(args.config)
    return int(args.func(args, cfg))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
