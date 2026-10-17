from __future__ import annotations

import json
import logging
import sqlite3

import pandas as pd
import pytest

from tradikg_bot.config import LiveTradingNotAllowedError
from tradikg_bot.platform_config import PlatformConfig
from tradikg_bot.service.ops import (
    AlreadyRunningError,
    InstanceLock,
    RedactSecrets,
    backup_databases,
    integrity_check,
    read_status,
)
from tradikg_bot.service.paper_service import PaperService

from market_fakes import FakeKrakenPublic, advance

NOW = pd.Timestamp("2026-10-16 10:20", tz="UTC")


def config(tmp_path, **kw) -> PlatformConfig:
    return PlatformConfig(home=tmp_path / "home", symbols=("XBTEUR",), min_history_bars=100, **kw)


def service(tmp_path, fake):
    return PaperService(config(tmp_path), api=fake, now=lambda: fake.now)


def test_platform_config_rejects_live_and_secrets(tmp_path):
    with pytest.raises(LiveTradingNotAllowedError):
        PlatformConfig(mode="live")
    cfg = tmp_path / "c.toml"
    cfg.write_text('[platform]\napi_secret = "x"\n')
    with pytest.raises(LiveTradingNotAllowedError):
        PlatformConfig.load(cfg)
    cfg.write_text('[platform]\ncapital_eur = 50.0\nsymbols = ["XBTEUR"]\n')
    loaded = PlatformConfig.load(cfg, env={"TRADIKG_HOME": str(tmp_path / "h")})
    assert loaded.capital_eur == 50.0 and loaded.home == tmp_path / "h"
    cfg.write_text("[platform]\nfoo = 1\n")
    with pytest.raises(ValueError):
        PlatformConfig.load(cfg)


def test_first_start_seeds_history_without_trading_then_processes_new_bars(tmp_path):
    fake = FakeKrakenPublic(NOW, n=300)
    svc = service(tmp_path, fake)
    status = svc.run_once()
    assert status["processed_steps"] == 1  # nur die neueste Kerze
    assert svc.engine.journal.trades() == []
    assert svc.engine.store.step_done(fake.opens[-100])  # Vorlauf als verarbeitet markiert
    assert not svc.engine.store.step_done(fake.opens[-250])  # nur nötiger Vorlauf
    advance(fake, 1)
    status = svc.run_once()
    assert status["processed_steps"] == 1 and status["stale_feeds"] == []
    stored = read_status(svc.cfg.status_file)
    assert stored["mode"] == "paper" and "balances" in stored


def test_catch_up_after_sleep_processes_exits_but_no_new_entries(tmp_path):
    fake = FakeKrakenPublic(NOW, n=300)
    svc = service(tmp_path, fake)
    svc.run_once()
    advance(fake, 6)  # Mac hat 6 Stunden geschlafen
    blocked = []
    original = svc.engine.step

    def spy(ts, bars, allow_entries=True):
        blocked.append(allow_entries)
        return original(ts, bars, allow_entries)

    svc.engine.step = spy
    svc.run_once()
    assert blocked[:-2] and not any(blocked[:-2])  # alte Kerzen: keine Einstiege
    assert blocked[-1] is True


def test_data_feed_errors_are_reported_not_fatal(tmp_path):
    fake = FakeKrakenPublic(NOW, n=300)
    svc = service(tmp_path, fake)
    svc.run_once()
    fake.fail_next = 100
    svc.md.attempts = 2
    svc.md._sleep = lambda s: None
    fake.now = fake.now + pd.Timedelta(hours=4)
    status = svc.run_once()
    assert "XBTEUR" in status["data_errors"] and "XBTEUR" in status["stale_feeds"]


def test_single_instance_lock(tmp_path):
    lock = tmp_path / "x.lock"
    with InstanceLock(lock), pytest.raises(AlreadyRunningError):
        InstanceLock(lock).acquire()
    InstanceLock(lock).acquire().release()  # nach Freigabe wieder möglich


def test_corrupt_database_is_detected(tmp_path):
    db = tmp_path / "bad.sqlite"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100 + b"garbage" * 200)
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        integrity_check(db)


def test_backups_are_consistent_and_rotated(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "a.sqlite") as c:
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (1)")
    backups = tmp_path / "backups"
    created = backup_databases(data, backups, keep=2)
    with sqlite3.connect(created[0]) as c:
        assert c.execute("SELECT x FROM t").fetchone() == (1,)
    import time

    for _ in range(3):
        time.sleep(1.1)
        backup_databases(data, backups, keep=2)
    assert len([p for p in backups.iterdir() if p.is_dir()]) == 2


def test_log_redaction(monkeypatch):
    monkeypatch.setenv("KRAKEN_API_SECRET", "SUPERSECRETVALUE")
    record = logging.LogRecord(
        "t", logging.INFO, "", 0, "secret=%s api_key=abc123", ("SUPERSECRETVALUE",), None
    )
    RedactSecrets().filter(record)
    assert "SUPERSECRETVALUE" not in record.msg and "abc123" not in record.msg


def test_status_file_is_valid_json(tmp_path):
    fake = FakeKrakenPublic(NOW, n=300)
    svc = service(tmp_path, fake)
    svc.run_once()
    json.loads(svc.cfg.status_file.read_text())


def test_trend_mode_runs_restarts_and_writes_dashboard_tables(tmp_path):
    fake = FakeKrakenPublic(NOW, n=400)
    cfg = config(tmp_path, strategy_mode="trend")
    svc = PaperService(cfg, api=fake, now=lambda: fake.now)
    assert svc.run_once()["processed_steps"] == 1
    assert len(svc.engine.closes["XBTEUR"]) >= 200  # Vorlauf reicht für SMA 200
    advance(fake, 30)
    assert svc.run_once()["processed_steps"] == 30
    state = svc.engine.store.get_json("trend_state")
    signals = svc.engine.store.conn.execute("SELECT COUNT(*) FROM signals_log").fetchone()[0]
    rows = svc.engine.store.conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0]
    assert state is not None and rows == 31 and signals >= 0
    # Neustart: Zustand wird geladen, nichts doppelt verarbeitet
    again = PaperService(cfg, api=fake, now=lambda: fake.now)
    assert again.run_once()["processed_steps"] == 0
    assert again.engine.alloc.state.to_json() == state


def test_trend_mode_uses_configured_invest_fraction(tmp_path):
    fake = FakeKrakenPublic(NOW, n=400)
    cfg = config(tmp_path, strategy_mode="trend", invest_fraction=0.5, cash_yield_pa=0.01)
    svc = PaperService(cfg, api=fake, now=lambda: fake.now)
    assert svc.engine.alloc.cfg.invest_fraction == 0.5
    assert svc.engine.alloc.cfg.cash_yield_pa == 0.01
