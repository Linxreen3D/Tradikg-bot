"""CLI: ``tradikg copy collect | simulate`` – Copy-Trading nur virtuell (keine Wallet)."""

from __future__ import annotations

import argparse
import json
import threading
import time

from tradikg_bot.platform_config import PlatformConfig


def _db(args: argparse.Namespace):
    cfg = PlatformConfig.load(getattr(args, "platform_config", None))
    cfg.ensure_dirs()
    return cfg, cfg.data_dir / "copy.sqlite"


def cmd_collect(args: argparse.Namespace) -> int:
    from tradikg_bot.copytrade.collect import CopyStore, collect_once
    from tradikg_bot.service.ops import AlreadyRunningError, InstanceLock

    cfg, db = _db(args)
    store = CopyStore(db)
    if args.once:
        collect_once(store)
        return 0
    try:
        lock = InstanceLock(cfg.run_dir / "copy_collect.lock").acquire()
    except AlreadyRunningError as exc:
        print(f"Abgelehnt: {exc}")
        return 1
    stop = threading.Event()
    try:
        while not stop.is_set():
            started = time.time()
            try:
                collect_once(store)
                if not args.no_live:
                    from tradikg_bot.copytrade.live import run_live

                    snap = run_live(db, cfg.data_dir / "copy_live.json")
                    print(
                        f"Live-Copy: {snap['balance']:.2f} $ ({snap['pnl']:+.2f} $), "
                        f"{snap['trades']} Trades, folge {len(snap['leaders'])}",
                        flush=True,
                    )
            except Exception as exc:  # Netz/API – nächster Durchgang
                print(f"Fehler: {exc}", flush=True)
            stop.wait(max(5.0, args.interval - (time.time() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from dataclasses import replace

    import pandas as pd

    from tradikg_bot.copytrade.report import render_copy_html
    from tradikg_bot.copytrade.simulate import CopyConfig, coverage, load_trades, simulate_copy

    cfg, db = _db(args)
    if not db.exists():
        print("Noch keine Daten – erst `tradikg copy collect` laufen lassen.")
        return 1
    trades, pools = load_trades(db)
    ccfg = CopyConfig(capital=args.capital, lookback_h=args.lookback_hours, delay_s=args.delay)
    try:
        res = simulate_copy(trades, pools, ccfg)
    except ValueError as exc:
        print(f"Abgebrochen: {exc}")
        return 1
    sensitivity = [
        (
            "Trader selbst (0 s, ohne Kosten) – Obergrenze",
            simulate_copy(
                trades, pools, replace(ccfg, delay_s=0, dex_fee=0, app_fee=0, network_usd=0)
            ).final,
        )
    ]
    for d in (2, 10, 30, 60):
        r = res if d == args.delay else simulate_copy(trades, pools, replace(ccfg, delay_s=d))
        sensitivity.append((f"Verzögerung {d} s, mit Kosten", r.final))
    cov = coverage(db)
    out = cfg.home / "reports" / f"copy-{pd.Timestamp.now(tz='UTC'):%Y%m%dT%H%M%S}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_copy_html(res, cov, sensitivity))
    summary = {
        "created": f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%dT%H:%M}",
        "capital": ccfg.capital,
        "final": res.final,
        "trades": len(res.trades),
        "followed": len(res.followed),
        "report": str(out),
    }
    (out.parent / "copy-latest.json").write_text(json.dumps(summary))
    print(
        f"Copy-Simulation {res.start:%d.%m. %H:%M} – {res.end:%d.%m. %H:%M} UTC: "
        f"{ccfg.capital:.2f} $ -> {res.final:.2f} $ | {len(res.trades)} kopierte Trades | "
        f"{len(res.followed)} Trader gefolgt"
    )
    for k, v in sensitivity:
        print(f"  {k:48} {v:8.2f} $")
    print(f"Bericht: {out}  (nur virtuell)")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    copy = sub.add_parser("copy", help="Copy-Trading-Simulator (nur virtuell)")
    cs = copy.add_subparsers(dest="copy_cmd", required=True)
    p = cs.add_parser("collect", help="Öffentliche Solana-DEX-Trades sammeln (GeckoTerminal)")
    p.add_argument("--platform-config")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=150.0, help="Sekunden je Durchgang")
    p.add_argument("--no-live", action="store_true", help="ohne virtuellen Live-Copy-Bot")
    p.set_defaults(func=cmd_collect, platform_cmd=True)
    p = cs.add_parser("simulate", help="Kopieren der besten Wallets nachrechnen (virtuell)")
    p.add_argument("--platform-config")
    p.add_argument("--capital", type=float, default=100.0, help="virtuelles Startkapital in USD")
    p.add_argument("--lookback-hours", type=float, default=3.0, help="Rückblick für die Auswahl")
    p.add_argument("--delay", type=float, default=10.0, help="Verzögerung in Sekunden")
    p.set_defaults(func=cmd_simulate, platform_cmd=True)
