"""CLI: ``tradikg live …`` – Echtgeld-Trendfolge auf Kraken (kleines Budget, siehe trend_live).

tradikg live check                      # Probelauf: Kraken prüft Orders nur, nichts passiert
tradikg live dry-run                    # Übergabe: Zustand prüfen, nichts ordern, Stempel schreiben
tradikg live run --i-understand-this-uses-real-money   # echter Betrieb (stündlicher Abgleich)
tradikg live status                     # Zustand + offene Orders
tradikg live kill [--release]           # keine neuen Käufe (Stops bleiben)
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from pathlib import Path

import pandas as pd

from tradikg_bot.platform_config import PlatformConfig

CONFIRM_FLAG = "--i-understand-this-uses-real-money"


def _paths(cfg: PlatformConfig) -> dict[str, Path]:
    root = cfg.home / "live"
    root.mkdir(parents=True, exist_ok=True)
    return {
        "state": root / "trend_state.json",
        "journal": root / "journal.sqlite",
        "kill": root / "KILL_SWITCH",
        "log": root / "live.log",
        "lock": root / "live.lock",
        "pid": root / "live.pid",
        "dry_run_ok": root / "dry_run_ok.json",
        "snapshot": root / "snapshot.json",
    }


def _logger(path: Path):
    def log(msg: str) -> None:
        line = f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M:%S} UTC  {msg}"
        print(line, flush=True)
        with open(path, "a") as fh:
            fh.write(line + "\n")

    return log


def _trend_config(args: argparse.Namespace):
    from tradikg_bot.live.trend_live import LiveTrendConfig

    # Beide Grenzen bleiben aus, solange das jeweilige Flag fehlt.
    # Der Linux-Dienst setzt sie ausdrücklich. Weiter als 0.45 scheitert in
    # LiveTrendConfig (validate_trend_drawdown, nur enger).
    drawdown = getattr(args, "drawdown_kill_fraction", None)
    return LiveTrendConfig(
        budget_eur=args.budget,
        invest_fraction=args.invest_fraction,
        daily_loss_enabled=bool(getattr(args, "daily_loss", False)),
        daily_loss_fraction=float(getattr(args, "daily_loss_fraction", 0.05)),
        drawdown_enabled=drawdown is not None,
        drawdown_fraction=0.45 if drawdown is None else float(drawdown),
    )


def _context(args: argparse.Namespace, dry_run: bool):
    from tradikg_bot.live.journal import ExecutionJournal
    from tradikg_bot.live.kraken.client import KrakenClient
    from tradikg_bot.live.kraken.credentials import load_credentials
    from tradikg_bot.live.kraken.gateway import KrakenSpotGateway
    from tradikg_bot.market.kraken_data import KrakenMarketData
    from tradikg_bot.storage import SQLiteStore

    cfg = PlatformConfig.load(getattr(args, "platform_config", None))
    cfg.ensure_dirs()
    paths = _paths(cfg)
    journal = ExecutionJournal(paths["journal"])
    client = KrakenClient(
        load_credentials(), nonce_source=journal.next_nonce, allow_trading=not dry_run
    )
    market = KrakenMarketData(KrakenClient(), SQLiteStore(cfg.market_db))
    tcfg = _trend_config(args)

    def closes(sym: str) -> pd.Series:
        market.update_cache(sym, "1d")  # nur abgeschlossene Tageskerzen
        return market.store.load_bars(sym, "1d")["close"]

    log = _logger(paths["log"])
    gateway = KrakenSpotGateway(client)
    return gateway, tcfg, paths, log, closes


def _runner(args: argparse.Namespace, dry_run: bool):
    from tradikg_bot.live.trend_live import LiveTrendRunner

    gateway, tcfg, paths, log, closes = _context(args, dry_run)
    runner = LiveTrendRunner(
        gateway,
        paths["state"],
        tcfg,
        closes,
        paths["kill"],
        dry_run,
        log,
        snapshot_path=paths["snapshot"],
    )
    return runner, tcfg, paths, log


def _write_pid(path: Path) -> None:
    path.write_text(str(os.getpid()))


def _clear_pid(path: Path) -> None:
    try:
        if path.exists() and path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        return


def _refuse_other_instance(paths: dict[str, Path], gateway, cfg) -> str | None:
    """PID-Datei, Börsen-Heuristik. Die Sperrdatei nimmt der Aufrufer selbst."""
    import datetime as dt

    from tradikg_bot.live.handover import EXCHANGE_HEURISTIC_LIMIT, peer_reasons
    from tradikg_bot.live.trend_live import LiveSafetyError

    try:
        orders = list(gateway.open_orders())
    except Exception as exc:
        raise LiveSafetyError("Offene Orders nicht lesbar – Start verweigert") from exc
    reasons = peer_reasons(paths["pid"], orders, cfg.symbols, dt.datetime.now(dt.UTC))
    if not reasons:
        return None
    return (
        "Eine andere Instanz könnte laufen:\n- "
        + "\n- ".join(reasons)
        + "\n"
        + EXCHANGE_HEURISTIC_LIMIT
    )


def cmd_check(args: argparse.Namespace) -> int:
    from tradikg_bot.live.kraken.credentials import CredentialsError
    from tradikg_bot.live.trend_live import LiveSafetyError

    try:
        runner, tcfg, _, log = _runner(args, dry_run=True)
    except CredentialsError as exc:
        print(f"Kein API-Key gefunden: {exc}\nSiehe docs/LIVE_START.md (Schlüsselbund).")
        return 2
    gw = runner.gw
    log(
        f"PROBELAUF – Budget {tcfg.budget_eur:.2f} €, je Coin max. {tcfg.slot_eur:.2f} €, "
        "keine Ausführung"
    )
    try:
        withdraw = gw.has_withdrawal_permission()
        log(f"Auszahlungsrecht des Keys: {'JA – bitte ändern!' if withdraw else 'nein (gut)'}")
        bal = {k: round(v.total, 8) for k, v in gw.balances().items() if v.total}
        log(f"Guthaben: {bal}")
        runner.run_once()
    except LiveSafetyError as exc:
        log(f"ABBRUCH: {exc}")
        return 2
    log("Probelauf fertig. Die [PROBE]-Zeilen zeigen, was der echte Lauf jetzt tun würde.")
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Validate-only: keine Orders. Schreibt bei sauberem Abgleich den Live-Stempel."""
    import datetime as dt

    from tradikg_bot.live.handover import execute_dry_run
    from tradikg_bot.live.kraken.credentials import CredentialsError
    from tradikg_bot.live.trend_live import LiveSafetyError

    try:
        gateway, tcfg, paths, log, closes = _context(args, dry_run=True)
        text = execute_dry_run(
            gateway=gateway,
            paths=paths,
            cfg=tcfg,
            closes=closes,
            log=log,
            now=lambda: dt.datetime.now(dt.UTC),
            import_from=Path(args.import_state) if args.import_state else None,
            reconstruct=bool(args.reconstruct),
            base_of=lambda sym: gateway.pair_rules(sym).base,
        )
    except CredentialsError as exc:
        print(f"Kein API-Key gefunden: {exc}\nSiehe docs/LIVE_START.md und docs/CUTOVER.md.")
        return 2
    except LiveSafetyError as exc:
        print(f"ABBRUCH: {exc}")
        return 2
    print(text)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from tradikg_bot.live.handover import marker_block_reason
    from tradikg_bot.live.trend_live import LiveSafetyError
    from tradikg_bot.service.ops import AlreadyRunningError, InstanceLock

    if not args.confirm:
        print(f"Echtgeld! Start nur mit {CONFIRM_FLAG}. Vorher `tradikg live dry-run` ausführen.")
        return 2
    runner, tcfg, paths, log = _runner(args, dry_run=False)
    try:
        if runner.gw.has_withdrawal_permission():
            raise LiveSafetyError(
                "API-Key hat Auszahlungsrechte – bitte Key ohne Auszahlung anlegen"
            )
        peer = _refuse_other_instance(paths, runner.gw, tcfg)
    except LiveSafetyError as exc:
        print(f"Abgelehnt: {exc}")
        return 1
    if peer:
        print(f"Abgelehnt: {peer}")
        return 1
    if args.require_dry_run:
        block = marker_block_reason(
            paths["dry_run_ok"],
            budget_eur=tcfg.budget_eur,
            state_path=paths["state"],
            daily_loss_enabled=tcfg.daily_loss_enabled,
            daily_loss_fraction=tcfg.daily_loss_fraction,
            drawdown_enabled=tcfg.drawdown_enabled,
            drawdown_fraction=tcfg.drawdown_fraction,
        )
        if block:
            print(f"Abgelehnt: {block}")
            return 2
    try:
        lock = InstanceLock(paths["lock"]).acquire()
    except AlreadyRunningError as exc:
        print(f"Abgelehnt: {exc}")
        return 1
    _write_pid(paths["pid"])
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # stop_live.sh: sauber beenden
    log(f"ECHTBETRIEB gestartet – Budget {tcfg.budget_eur:.2f} €, Symbole {list(tcfg.symbols)}")
    if tcfg.daily_loss_enabled:
        log(
            f"Tagesverlustgrenze an: {tcfg.daily_loss_fraction:.0%} "
            f"der Scheibe, Reset 00:00 {tcfg.daily_loss_timezone}"
        )
    if tcfg.drawdown_enabled:
        log(
            f"Drawdown-Sperre an: {tcfg.drawdown_fraction:.0%} unter dem Höchststand der Scheibe. "
            "Lösen nur mit tradikg live drawdown-reset."
        )
    try:
        while not stop.is_set():
            try:
                runner.run_once()
            except LiveSafetyError as exc:
                log(f"ABBRUCH: {exc}")
                return 2
            except Exception as exc:  # Netzwerk o. Ä.: nächster Versuch beim nächsten Durchgang
                log(f"Fehler im Durchgang (nächster Versuch in 5 min): {exc}")
            if args.once:
                break
            stop.wait(300 - time.time() % 300 + 30)  # alle 5 min: Ausführungen, Stops, Tagesregel
    except KeyboardInterrupt:
        pass
    finally:
        _clear_pid(paths["pid"])
        lock.release()
        log("Echtbetrieb beendet (Stops bei Kraken bleiben aktiv).")
    return 0


def cmd_buy_now(args: argparse.Namespace) -> int:
    """Vom Nutzer ausgelöst: wartende Kauf-Limits sofort marktnah kaufen und Stop setzen."""
    from tradikg_bot.live.trend_live import LiveSafetyError
    from tradikg_bot.service.ops import AlreadyRunningError, InstanceLock

    if not args.confirm:
        print(f"Echtgeld! Sofortkauf nur mit {CONFIRM_FLAG}.")
        return 2
    runner, _, paths, log = _runner(args, dry_run=False)
    try:
        lock = InstanceLock(paths["lock"]).acquire()
    except AlreadyRunningError:
        print("Echtgeld-Bot läuft – bitte scripts/live_buy_now.sh nutzen (stoppt, kauft, startet).")
        return 1
    try:
        log("SOFORTKAUF vom Nutzer ausgelöst")
        runner.buy_now()
    except LiveSafetyError as exc:
        log(f"ABBRUCH: {exc}")
        return 2
    finally:
        lock.release()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from tradikg_bot.live.trend_live import LiveState

    cfg = PlatformConfig.load(getattr(args, "platform_config", None))
    paths = _paths(cfg)
    st = LiveState.load(paths["state"])
    print(f"Kill Switch: {'AKTIV' if paths['kill'].exists() else 'aus'}")
    if not st.symbols:
        print("Noch kein Echtbetrieb gelaufen.")
    dd = st.drawdown
    flag = paths["state"].parent / "DRAWDOWN_HALT"
    if dd and dd.get("halted"):
        print(
            f"Drawdown-Sperre: AKTIV (Höchststand {float(dd.get('peak_equity') or 0):.2f} €, "
            f"Grenze −{float(dd.get('limit') or 0):.0%}). "
            "Neue Käufe aus. Lösen: tradikg live drawdown-reset"
        )
    elif flag.exists():
        print(
            "Hinweisdatei DRAWDOWN_HALT liegt, ohne aktive Sperre in der Zustandsdatei. "
            "Sie blockiert keine Käufe. Lösen einer echten Sperre: tradikg live drawdown-reset"
        )
    elif dd:
        print(
            f"Drawdown-Sperre: nicht ausgelöst (Höchststand "
            f"{float(dd.get('peak_equity') or 0):.2f} €)"
        )
    for sym, s in st.symbols.items():
        print(
            f"{sym}: Menge {s.units:.8f} zu Ø {s.entry_price:.2f} | Stop {s.stop_price:.2f} "
            f"({s.stop_txid or '–'}) | Kauf offen {s.buy_txid or '–'} | Verkauf offen "
            f"{s.sell_txid or '–'} | Sperre {s.blocked} | Tag {s.last_candle} | realisiert "
            f"{s.realized_eur:+.2f} €"
        )
    if paths["log"].exists():
        print("\nLetzte Einträge:\n" + "".join(paths["log"].read_text().splitlines(True)[-15:]))
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    cfg = PlatformConfig.load(getattr(args, "platform_config", None))
    kill = _paths(cfg)["kill"]
    if args.release:
        kill.unlink(missing_ok=True)
        print("Kill Switch gelöst – neue Käufe wieder erlaubt.")
    else:
        kill.touch()
        print("Kill Switch AKTIV – keine neuen Käufe. Stops bei Kraken bleiben bestehen.")
    return 0


def cmd_drawdown_reset(args: argparse.Namespace) -> int:
    """Drawdown-Sperre lösen. Kill-Switch, Stops und Budget bleiben wie sie sind."""
    from tradikg_bot.live.trend_live import release_drawdown_halt

    cfg = PlatformConfig.load(getattr(args, "platform_config", None))
    state = _paths(cfg)["state"]
    release_drawdown_halt(state)
    print(
        "Drawdown-Sperre gelöst. Der nächste Durchgang setzt den Höchststand auf die "
        "aktuelle Equity. Es wird nichts verkauft; Stops bei Kraken bleiben. "
        "Die Kill-Switch-Datei ist unverändert. "
        f"(Datei {state.parent / 'DRAWDOWN_HALT'} entfernt, falls sie lag.)"
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    live = sub.add_parser("live", help="Echtgeld-Trendfolge auf Kraken (kleines Budget)")
    ls = live.add_subparsers(dest="live_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--platform-config", help="Pfad zur Plattform-Konfiguration (TOML)")
        p.add_argument("--budget", type=float, default=20.0, help="Budget in EUR (max. 20)")
        p.add_argument("--invest-fraction", type=float, default=1.0)
        p.add_argument(
            "--daily-loss",
            action="store_true",
            help="Tagesverlustgrenze einschalten (Linux-Lauf: ausdrücklich an)",
        )
        p.add_argument(
            "--daily-loss-fraction",
            type=float,
            default=0.05,
            help="Anteil der Scheibe zum Tagesbeginn (Standard 0.05, höchstens 0.10)",
        )
        p.add_argument(
            "--drawdown-kill-fraction",
            type=float,
            default=None,
            help=(
                "Drawdown-Sperre einschalten: Anteil unter dem Höchststand der Scheibe "
                "(höchstens 0.45, nur enger). Ohne dieses Flag bleibt sie aus."
            ),
        )

    p = ls.add_parser("check", help="Probelauf: Kraken prüft Orders nur (validate)")
    common(p)
    p.set_defaults(func=cmd_check, platform_cmd=True)
    p = ls.add_parser("dry-run", help="Übergabe-Probelauf ohne Orders, danach Live-Stempel")
    common(p)
    p.add_argument("--import-state", help="Zustandsdatei vom Mac prüfen und übernehmen")
    p.add_argument(
        "--reconstruct",
        action="store_true",
        help="Zustand nur aus offenen Bot-Orders (userref) bauen, innerhalb der Scheibe",
    )
    p.set_defaults(func=cmd_dry_run, platform_cmd=True)
    p = ls.add_parser("run", help="Echtbetrieb starten (stündlicher Abgleich)")
    common(p)
    p.add_argument(CONFIRM_FLAG, dest="confirm", action="store_true")
    p.add_argument("--once", action="store_true", help="nur ein Durchgang")
    p.add_argument(
        "--require-dry-run",
        action="store_true",
        help="Start nur, wenn ein passender dry-run-Stempel vorliegt",
    )
    p.set_defaults(func=cmd_run, platform_cmd=True)
    p = ls.add_parser("buy-now", help="Wartende Kauf-Limits sofort marktnah kaufen + Stop setzen")
    common(p)
    p.add_argument(CONFIRM_FLAG, dest="confirm", action="store_true")
    p.set_defaults(func=cmd_buy_now, platform_cmd=True)
    p = ls.add_parser("status", help="Zustand, Stops und letzte Log-Einträge")
    p.add_argument("--platform-config")
    p.set_defaults(func=cmd_status, platform_cmd=True)
    p = ls.add_parser("kill", help="Kill Switch: keine neuen Käufe")
    p.add_argument("--platform-config")
    p.add_argument("--release", action="store_true")
    p.set_defaults(func=cmd_kill, platform_cmd=True)
    p = ls.add_parser(
        "drawdown-reset",
        help="Drawdown-Sperre lösen (nicht der Kill Switch; Stops bleiben)",
    )
    p.add_argument("--platform-config")
    p.set_defaults(func=cmd_drawdown_reset, platform_cmd=True)
