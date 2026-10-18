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
                    from tradikg_bot.copytrade.live import TACTICS, run_live

                    for name, (label, ccfg) in TACTICS.items():
                        snap = run_live(db, cfg.data_dir / name, ccfg, label)
                        print(
                            f"Live-Copy {label.split(':')[0]}: {snap['balance']:.2f} $ "
                            f"({snap['pnl']:+.2f} $), {snap['trades']} Trades, "
                            f"folge {len(snap['leaders'])}",
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


def cmd_solana(args: argparse.Namespace) -> int:
    """Solana-Copy mit echten Jupiter-Kursen – derzeit nur Schattenbetrieb (nichts signiert)."""
    import pandas as pd

    from tradikg_bot.copytrade.solana_live import (
        ShadowCopyRunner,
        SolanaCopyConfig,
        collector_rank,
        kraken_sol_eur,
    )
    from tradikg_bot.service.ops import AlreadyRunningError, InstanceLock

    cfg, db = _db(args)
    root = cfg.home / "copylive"
    root.mkdir(parents=True, exist_ok=True)
    kill = root / "KILL_SWITCH"
    if args.solana_cmd == "kill":
        kill.unlink(missing_ok=True) if args.release else kill.touch()
        print("Kill Switch " + ("gelöst" if args.release else "AKTIV – keine neuen Käufe"))
        return 0
    if args.solana_cmd == "status":
        snap = root / "snapshot.json"
        if not snap.exists():
            print("Noch nicht gestartet.")
            return 0
        s = json.loads(snap.read_text())
        print(
            f"{s['mode']}: {s['equity_sol']:.4f} SOL (Start {s['capital_sol']:.4f}), "
            f"{len(s['holdings'])} offen, {len(s['trades'])} Trades, folgt {len(s['leaders'])}"
        )
        for e in s["feed"][-10:]:
            when = pd.Timestamp(e["ts"]).tz_convert("Europe/Vienna")
            print(f"  {when:%H:%M:%S} {e['text']}")
        return 0
    try:
        lock = InstanceLock(root / "run.lock").acquire()
    except AlreadyRunningError as exc:
        print(f"Abgelehnt: {exc}")
        return 1

    def log(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    runner = ShadowCopyRunner(
        root / "state.json",
        root / "snapshot.json",
        kill,
        SolanaCopyConfig(capital_sol=args.capital_sol),
        rank=collector_rank(db),
        sol_eur=kraken_sol_eur,
        log=log,
    )
    log("Solana-Copy im SCHATTENBETRIEB gestartet – echte Kurse, keine echten Orders")
    try:
        while True:
            try:
                runner.run_once()
            except Exception as exc:  # Netz/RPC – nächster Versuch
                log(f"Fehler: {exc}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    copy = sub.add_parser("copy", help="Copy-Trading-Simulator (nur virtuell)")
    cs = copy.add_subparsers(dest="copy_cmd", required=True)
    p = cs.add_parser("collect", help="Öffentliche DEX-Trades aller Chains sammeln (GeckoTerminal)")
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
    p = cs.add_parser("solana", help="Solana-Copy mit echten Kursen (Schattenbetrieb)")
    p.add_argument("solana_cmd", choices=["run", "status", "kill"])
    p.add_argument("--platform-config")
    p.add_argument("--capital-sol", type=float, default=0.20, help="Schatten-Kapital in SOL")
    p.add_argument("--interval", type=float, default=10.0, help="Sekunden je Durchgang")
    p.add_argument("--release", action="store_true", help="Kill Switch lösen")
    p.set_defaults(func=cmd_solana, platform_cmd=True)
