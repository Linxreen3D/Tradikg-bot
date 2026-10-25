"""Neustart nach Absturz: validate-only, nichts gespeichert, keine Kraken-Verbindung."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from test_linux_cutover import DAY, NOW, ROOT, _paths, _stop_order
from test_trend_live import RULES, FakeKraken, Market

from tradikg_bot.live.handover import execute_restart_reconcile, write_dry_run_marker
from tradikg_bot.live.trend_live import (
    LiveSafetyError as Safety,
)
from tradikg_bot.live.trend_live import (
    LiveState,
    LiveTrendConfig,
    LiveTrendRunner,
    SymbolState,
)

COMMIT = "0123456789abcdef"


def _cfg(**overrides) -> LiveTrendConfig:
    params = dict(
        budget_eur=20.0,
        invest_fraction=1.0,
        sma=3,
        daily_loss_enabled=True,
        daily_loss_fraction=0.05,
        drawdown_enabled=True,
        drawdown_fraction=0.45,
    )
    params.update(overrides)
    return LiveTrendConfig(**params)


def _stamp(paths: dict[str, Path], cfg: LiveTrendConfig, commit: str = COMMIT) -> None:
    write_dry_run_marker(
        paths["dry_run_ok"],
        budget_eur=cfg.budget_eur,
        daily_loss_enabled=cfg.daily_loss_enabled,
        daily_loss_fraction=cfg.daily_loss_fraction,
        drawdown_enabled=cfg.drawdown_enabled,
        drawdown_fraction=cfg.drawdown_fraction,
        code_commit=commit,
    )


def _held(paths: dict[str, Path], volume: float, price: float, *, stop: str | None) -> None:
    last = DAY
    LiveState(
        {
            "XBTEUR": SymbolState(
                units=volume,
                entry_price=110.0,
                peak=110.0,
                stop_txid=stop,
                stop_price=price,
                stop_units=volume if stop else 0.0,
                last_candle=last,
                realized_eur=0.0,
            ),
            "ETHEUR": SymbolState(last_candle=last),
        }
    ).save(paths["state"])


def _arm(tmp_path: Path, *, volume: float = 0.09, on_exchange: bool = True):
    """Zustand mit Stop, Stempel, dann eine Zustandsänderung wie nach einem Lauf."""
    fake, market = FakeKraken(eur=100.0), Market()
    market.day(110, 8)
    rules = RULES["XBTEUR"]
    size = rules.round_volume_down(volume)
    price = rules.round_price_down(110.0 * 0.8)
    if on_exchange:
        order = _stop_order(volume, opened_at=NOW.timestamp() - 86400)
        fake.orders[order.txid] = order
        fake.refs[order.txid] = order.client_ref
        size, price = order.volume, order.price or price
    fake.bal["XXBT"] = size
    paths = _paths(tmp_path)
    cfg = _cfg()
    _held(paths, size, price, stop="TSTOP")
    _stamp(paths, cfg)
    raw = json.loads(paths["state"].read_text())
    raw["symbols"]["XBTEUR"]["realized_eur"] = -0.25  # der Lauf hat den Zustand geschrieben
    paths["state"].write_text(json.dumps(raw, indent=2))
    return fake, market, paths, cfg


def _files(paths: dict[str, Path]) -> dict[str, bytes | None]:
    found = {}
    for key in ("state", "dry_run_ok", "kill"):
        path = paths[key]
        found[key] = path.read_bytes() if path.exists() else None
    return found


def _reconcile(fake, market, paths, cfg, logs: list[str], **kwargs) -> str:
    return execute_restart_reconcile(
        gateway=fake,
        paths=paths,
        cfg=cfg,
        closes=market,
        log=logs.append,
        now=lambda: NOW,
        code_commit=kwargs.get("code_commit", COMMIT),
    )


def test_crash_restart_passes_after_a_clean_reconcile(tmp_path):
    fake, market, paths, cfg = _arm(tmp_path)
    before_orders = set(fake.orders)
    before = _files(paths)
    logs: list[str] = []
    text = _reconcile(fake, market, paths, cfg, logs)
    assert "OK" in text and "Neustart erlaubt" in text and "nichts gespeichert" in text
    assert "Neustart verweigert" not in text
    assert _files(paths) == before
    assert set(fake.orders) == before_orders
    assert not fake.validated
    assert all(line for line in logs)
    assert not paths["kill"].exists()


def test_restart_refuses_a_missing_stop(tmp_path):
    fake, market, paths, cfg = _arm(tmp_path, on_exchange=False)
    before = _files(paths)
    with pytest.raises(Safety, match="Stop an der Börse fehlt") as raised:
        _reconcile(fake, market, paths, cfg, [])
    assert "Neustart verweigert" in str(raised.value)
    assert "Kein Neustart" in str(raised.value)
    assert "Neustart erlaubt" not in str(raised.value)
    assert _files(paths) == before
    assert not fake.validated and not fake.orders

    # Dieselbe Lücke, wenn die Datei selbst keinen Stop mehr nennt: der Abgleich
    # allein sähe „keine Stop-Txid auf beiden Seiten“ und würde durchwinken.
    naked = json.loads(paths["state"].read_text())
    naked["symbols"]["XBTEUR"]["stop_txid"] = None
    naked["symbols"]["XBTEUR"]["stop_units"] = 0
    paths["state"].write_text(json.dumps(naked))
    before = _files(paths)
    with pytest.raises(Safety, match="Stop an der Börse fehlt"):
        _reconcile(fake, market, paths, cfg, [])
    assert _files(paths) == before


def test_restart_refuses_a_foreign_instance(tmp_path):
    fake, market, paths, cfg = _arm(tmp_path)
    before = _files(paths)
    proc = subprocess.Popen(["sleep", "30"])  # noqa: S603 - lokale PID, kein Shell
    try:
        paths["pid"].write_text(str(proc.pid))
        with pytest.raises(Safety, match="Instanz") as raised:
            _reconcile(fake, market, paths, cfg, [])
        assert "PID" in str(raised.value) and "Kein Neustart" in str(raised.value)
        assert not fake.validated
        assert _files(paths) == before
    finally:
        proc.kill()
        proc.wait(timeout=5)

    paths["pid"].unlink()
    fresh = _stop_order(0.09, opened_at=NOW.timestamp() - 30)
    # Der liegende Stop bleibt, zusätzlich eine frische Bot-Order.
    fake.orders[fresh.txid] = fresh
    fake.refs[fresh.txid] = fresh.client_ref
    before = _files(paths)
    with pytest.raises(Safety, match="opentm"):
        _reconcile(fake, market, paths, cfg, [])
    assert _files(paths) == before and not fake.validated


def test_restart_refuses_a_state_order_mismatch(tmp_path):
    fake, market, paths, cfg = _arm(tmp_path, volume=0.05)
    raw = json.loads(paths["state"].read_text())
    raw["symbols"]["XBTEUR"]["units"] = 0.09  # Datei und Börse nennen verschiedene Mengen
    raw["symbols"]["XBTEUR"]["stop_units"] = 0.09
    paths["state"].write_text(json.dumps(raw, indent=2))
    before = _files(paths)
    with pytest.raises(Safety, match="ABWEICHUNG") as raised:
        _reconcile(fake, market, paths, cfg, [])
    message = str(raised.value)
    assert "Neustart verweigert" in message and "Kein Neustart" in message
    assert "Stop an der Börse fehlt" not in message
    assert "Neustart erlaubt" not in message
    assert _files(paths) == before
    assert not fake.validated
    assert len(fake.orders) == 1


def test_restart_refuses_a_config_change(tmp_path):
    fake, market, paths, cfg = _arm(tmp_path)
    before = _files(paths)
    before_orders = {txid: (order.volume, order.status) for txid, order in fake.orders.items()}

    class Untouched(FakeKraken):
        def has_withdrawal_permission(self):
            raise AssertionError("Konfigurationsfehler darf die Börse nicht fragen")

        def open_orders(self):
            raise AssertionError("Konfigurationsfehler darf die Börse nicht fragen")

    untouched = Untouched(eur=100.0)
    cases = [
        (_cfg(budget_eur=10.0), COMMIT, "Budget"),
        (_cfg(daily_loss_fraction=0.04), COMMIT, "Tagesverlust"),
        (_cfg(drawdown_fraction=0.30), COMMIT, "Drawdown"),
        (cfg, "anderer-commit", "Code-Stand"),
    ]
    for changed, commit, needle in cases:
        with pytest.raises(Safety, match=needle) as raised:
            _reconcile(untouched, market, paths, changed, [], code_commit=commit)
        assert "Neustart verweigert" in str(raised.value)
        assert "Kein Neustart" in str(raised.value)
        assert _files(paths) == before
    left = {txid: (order.volume, order.status) for txid, order in fake.orders.items()}
    assert left == before_orders
    assert not fake.validated


def test_restart_refuses_withdrawal_and_a_trading_client(tmp_path):
    fake, market, paths, cfg = _arm(tmp_path)
    before = _files(paths)
    fake.withdraw = True
    with pytest.raises(Safety, match="Auszahlungsrechte"):
        _reconcile(fake, market, paths, cfg, [])
    assert _files(paths) == before and not fake.validated

    fake.withdraw = False

    class Client:
        allow_trading = True

    fake.client = Client()
    with pytest.raises(Safety, match="Handelsfreigabe"):
        _reconcile(fake, market, paths, cfg, [])
    assert _files(paths) == before and not fake.validated


def test_restart_blocks_a_real_order_attempt(tmp_path, monkeypatch):
    fake, market, paths, cfg = _arm(tmp_path)
    before = _files(paths)

    def place(self):
        self.gw.add_order({"type": "buy", "volume": "1"}, validate=False)
        return self.state

    monkeypatch.setattr(LiveTrendRunner, "run_once", place)
    with pytest.raises(Safety, match="Order zu legen"):
        _reconcile(fake, market, paths, cfg, [])
    assert _files(paths) == before
    assert not fake.validated
    assert list(fake.orders) == ["TSTOP"]


def _loop(tmp_path: Path, script: str, **env: str) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "tradikg"
    fake.write_text(script)
    fake.chmod(0o755)
    assert stat.S_IMODE(fake.stat().st_mode) & 0o111
    merged = {
        **os.environ,
        "TRADIKG_BIN": str(fake),
        "TRADIKG_REPO": str(tmp_path),
        "TRADIKG_RESTART_DELAY_S": "0",
        "CALL_LOG": str(tmp_path / "calls.log"),
        "HOME": str(tmp_path),
        "KRAKEN_API_SECRET": "super-secret-value",
        "KRAKEN_API_KEY": "super-secret-value",
    }
    merged.update(env)
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "tradikg_run_live_loop.sh")],
        capture_output=True,
        text=True,
        env=merged,
        timeout=10,
        check=False,
    )


_FAKE = """#!/bin/bash
printf '%s\\n' "$*" >> "$CALL_LOG"
if [ "$1 $2" = "live restart-check" ]; then
  checks=$(grep -c 'live restart-check' "$CALL_LOG" || true)
  if [ "$checks" -ge "${FAIL_CHECK_ON:-99}" ]; then
    printf '%s\\n' "Neustart verweigert: Stop an der Börse fehlt. Kein Neustart."
    exit 2
  fi
    printf '%s\\n' "[ABGLEICH] XBTEUR OK"
    printf '%s\\n' "Abgleich bestanden. Neustart erlaubt."
  exit 0
fi
if [ "$1 $2" = "live run" ]; then
  runs=$(grep -c 'live run ' "$CALL_LOG" || true)
  if [ "$runs" -ge 2 ]; then
    exit "${SECOND_RUN_EXIT:-0}"
  fi
  exit "${FIRST_RUN_EXIT:-1}"
fi
exit 99
"""


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.log").read_text().splitlines()


def test_loop_restarts_after_a_clean_reconcile(tmp_path):
    proc = _loop(tmp_path, _FAKE, FIRST_RUN_EXIT="1", SECOND_RUN_EXIT="0")
    assert proc.returncode == 0, proc.stderr
    calls = _calls(tmp_path)
    assert [line.split()[1] for line in calls] == ["restart-check", "run", "restart-check", "run"]
    run = calls[1]
    assert "--budget" in run and "20" in run
    assert "--daily-loss-fraction" in run and "0.05" in run
    assert "--drawdown-kill-fraction" in run and "0.45" in run
    assert "--require-dry-run" in run and "--i-understand-this-uses-real-money" in run
    assert "--require-dry-run" not in calls[0]
    text = proc.stdout + proc.stderr
    assert "Neustart erlaubt" in text
    assert "Bot abgestürzt" in text and "Bot sauber beendet" in text
    assert "super-secret-value" not in text


def test_loop_stops_on_reconcile_failure_or_bot_status_2(tmp_path):
    refused = _loop(tmp_path, _FAKE, FIRST_RUN_EXIT="1", FAIL_CHECK_ON="2")
    assert refused.returncode == 2
    kinds = [line.split()[1] for line in _calls(tmp_path)]
    assert kinds == ["restart-check", "run", "restart-check"]
    assert "Neustart verweigert" in refused.stdout
    assert "super-secret-value" not in refused.stdout + refused.stderr

    other = tmp_path / "status2"
    other.mkdir()
    stopped = _loop(other, _FAKE, FIRST_RUN_EXIT="2")
    assert stopped.returncode == 2
    assert [line.split()[1] for line in _calls(tmp_path / "status2")] == ["restart-check", "run"]
    assert "Status 2" in stopped.stdout
    assert "super-secret-value" not in stopped.stdout + stopped.stderr
