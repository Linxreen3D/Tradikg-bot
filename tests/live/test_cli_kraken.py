from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tradikg_bot.cli import main
from tradikg_bot.live import cli_kraken
from tradikg_bot.live.paper import PaperExchange

from helpers import MECHANICS_FEES
from live_helpers import DOC_SECRET, DOC_SIGNATURE

ROOT = Path(__file__).resolve().parents[2]


def test_paper_trade_is_blocked_like_kraken(tmp_path, capsys):
    rc = main(
        [
            "kraken",
            "paper-trade",
            "--stop-loss",
            "58000",
            "--take-profit",
            "64000",
            "--journal",
            str(tmp_path / "p.sqlite"),
        ]
    )
    assert rc == 2
    assert "BLOCKIERT (wie bei Kraken live)" in capsys.readouterr().out


def test_paper_trade_with_test_double_bracket(tmp_path, capsys):
    rc = main(
        [
            "kraken",
            "paper-trade",
            "--stop-loss",
            "58000",
            "--take-profit",
            "66000",  # lohnt sich nach 0,80 % Taker
            "--simulate-bracket",
            "--journal",
            str(tmp_path / "p.sqlite"),
        ]
    )
    assert rc == 0 and "protected" in capsys.readouterr().out
    assert main(["kraken", "status", "--journal", str(tmp_path / "p.sqlite")]) == 0


def test_reset_halt_requires_note(tmp_path):
    with pytest.raises(SystemExit):
        main(["kraken", "reset-halt", "--journal", str(tmp_path / "j.sqlite")])
    assert (
        main(["kraken", "reset-halt", "--note", "geprüft", "--journal", str(tmp_path / "j.sqlite")])
        == 0
    )


def test_live_trade_without_credentials_sends_nothing(tmp_path, capsys, monkeypatch):
    for var in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET", "TRADIKG_KRAKEN_CREDENTIALS"):
        monkeypatch.delenv(var, raising=False)
    rc = main(
        [
            "kraken",
            "live-trade",
            "--stop-loss",
            "58000",
            "--take-profit",
            "64000",
            "--journal",
            str(tmp_path / "l.sqlite"),
        ]
    )
    assert rc == 2 and "KEINE ORDER GESENDET" in capsys.readouterr().out


def test_live_trade_blocked_without_approval_even_if_bracket_existed(tmp_path, capsys, monkeypatch):
    fake = PaperExchange(native_bracket=True, fees=MECHANICS_FEES)
    fake.is_live = True
    fake.set_book("XBTEUR", 60000.0, 60010.0)
    monkeypatch.setattr(cli_kraken, "_live_gateway", lambda allow_trading, journal: fake)
    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", DOC_SECRET)
    monkeypatch.delenv("TRADIKG_LIVE_APPROVAL", raising=False)
    rc = main(
        [
            "kraken",
            "live-trade",
            "--stop-loss",
            "58000",
            "--take-profit",
            "64000",
            "--i-understand-this-uses-real-money",
            "--journal",
            str(tmp_path / "l.sqlite"),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 2 and "TRADIKG_LIVE_APPROVAL" in out and fake.orders == {}


def test_no_credentials_tracked_in_git():
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert not [
        f
        for f in files
        if f.endswith((".env", ".pem", ".key"))
        or ("credential" in f.lower() and f.endswith(".json"))
    ]
    secret_like = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")
    for f in files:
        path = ROOT / f
        if path.is_file() and path.suffix in {".py", ".toml", ".md", ".yml", ".json", ".txt"}:
            found = set(secret_like.findall(path.read_text(errors="ignore"))) - {
                DOC_SECRET,
                DOC_SIGNATURE,
            }
            assert not found, f"Möglicher Schlüssel in {f}"
