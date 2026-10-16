"""Prüft die macOS-Skripte und launchd-Vorlagen (Syntax, Platzhalter, Sicherheitsregeln)."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "scripts").glob("*.sh"))
REQUIRED = {
    "setup_mac.sh",
    "start_bot.sh",
    "stop_bot.sh",
    "status_bot.sh",
    "start_dashboard.sh",
    "run_tests.sh",
    "check_environment.sh",
}


def test_all_required_scripts_exist_and_are_executable():
    names = {p.name for p in SCRIPTS}
    assert names >= REQUIRED
    for p in SCRIPTS:
        if p.name != "_common.sh":
            assert p.stat().st_mode & 0o111, f"{p.name} ist nicht ausführbar"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck nicht installiert")
def test_shellcheck_clean():
    subprocess.run(
        ["shellcheck", "-x", "-P", "SCRIPTDIR", *map(str, SCRIPTS)],
        check=True,
        cwd=ROOT / "scripts",
    )


def test_scripts_never_delete_trading_data_or_go_live():
    for p in SCRIPTS:
        text = p.read_text()
        assert "rm -rf" not in text
        assert "live-trade" not in text and "TRADIKG_LIVE_APPROVAL" not in text


def test_dashboard_binds_localhost_only():
    text = (ROOT / "scripts" / "start_dashboard.sh").read_text()
    assert "--server.address 127.0.0.1" in text


@pytest.mark.parametrize(
    "template", sorted((ROOT / "deploy" / "launchd").glob("*.template")), ids=lambda p: p.name
)
def test_launchd_templates_are_valid_plists(template):
    rendered = (
        template.read_text()
        .replace("__REPO_DIR__", "/r")
        .replace("__VENV_DIR__", "/v")
        .replace("__TRADIKG_HOME__", "/h")
    )
    plist = plistlib.loads(rendered.encode())
    assert plist["Label"].startswith("com.tradikg.")
    assert plist["KeepAlive"]["SuccessfulExit"] is False
    args = " ".join(plist["ProgramArguments"])
    assert "live" not in args.split()


def test_check_environment_runs(tmp_path):
    out = subprocess.run(
        ["bash", str(ROOT / "scripts" / "check_environment.sh")],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "TRADIKG_HOME": str(tmp_path / "home"),
            "TRADIKG_VENV": str(tmp_path / "novenv"),
        },
    )
    assert "Datenverzeichnis" in out.stdout
