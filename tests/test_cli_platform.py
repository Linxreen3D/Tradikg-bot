from __future__ import annotations

import pytest

from tradikg_bot.cli import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADIKG_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_init_is_idempotent_and_never_overwrites_config(home, capsys):
    assert main(["init"]) == 0
    cfg = home / "config.toml"
    cfg.write_text(cfg.read_text().replace("capital_eur = 100.0", "capital_eur = 50.0"))
    assert main(["init"]) == 0
    assert "capital_eur = 50.0" in cfg.read_text()
    assert "unverändert" in capsys.readouterr().out


def test_doctor_reports_paper_mode(home, capsys):
    main(["init"])
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "paper (Echtgeld deaktiviert)" in out and rc in (0, 1)


def test_research_replay_status_report_and_controls(home, capsys):
    main(["init"])
    assert (
        main(
            [
                "research",
                "run",
                "--source",
                "synthetic",
                "--symbols",
                "AAAEUR",
                "--bars",
                "900",
                "--random-runs",
                "5",
                "--strategies",
                "sma_crossover,volume_breakout",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Bericht:" in out and (home / "reports" / "research-1.md").exists()
    assert main(["research", "report"]) == 0
    assert "synthetische Daten" in capsys.readouterr().out
    assert (
        main(["paper", "replay", "--source", "synthetic", "--symbols", "AAAEUR", "--bars", "600"])
        == 0
    )
    assert "Replay (synthetic)" in capsys.readouterr().out
    assert main(["paper", "status"]) == 0
    assert main(["paper", "kill", "--reason", "Test"]) == 0
    assert (home / "data" / "paper" / "KILL_SWITCH").exists()
    assert main(["paper", "kill", "--release"]) == 0
    assert not (home / "data" / "paper" / "KILL_SWITCH").exists()
    assert main(["paper", "reset-halt", "--note", "geprüft"]) == 0
    assert main(["paper", "report", "--day", "2026-01-01"]) == 0
    assert "Keine abgeschlossenen Trades" in capsys.readouterr().out
    assert main(["backup"]) == 0
    assert any((home / "backups").iterdir())


def test_ml_train_without_enough_data_reports_honestly(home, capsys):
    main(["init"])
    assert (
        main(["ml", "train", "--source", "synthetic", "--symbols", "AAAEUR", "--bars", "400"]) == 0
    )
    out = capsys.readouterr().out
    assert "Datensatz:" in out


def test_live_mode_cannot_be_configured(home):
    from tradikg_bot.config import LiveTradingNotAllowedError

    main(["init"])
    (home / "config.toml").write_text('[platform]\nmode = "live"\n')
    with pytest.raises(LiveTradingNotAllowedError):
        main(["paper", "status"])
