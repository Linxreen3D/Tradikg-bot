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
    "logs.sh",
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


def test_systemd_unit_caps_the_slice_and_carries_no_secrets():
    text = (ROOT / "deploy" / "systemd" / "tradikg-live.service.template").read_text()
    assert "EnvironmentFile=__ENV_FILE__" in text
    assert "EnvironmentFile=-" not in text
    assert "--budget 20" in text
    assert "Restart=on-failure" in text
    assert "RestartPreventExitStatus=2" in text
    assert "TRADIKG_HOME=__TRADIKG_HOME__" in text
    assert "KRAKEN_API_KEY=" not in text and "KRAKEN_API_SECRET=" not in text
    assert "live run" in text


def test_linux_unit_install_writes_unit_without_starting_or_copying_secrets(tmp_path):
    script = ROOT / "scripts" / "install_linux_live_unit.sh"
    text = script.read_text()
    for line in text.splitlines():
        assert "systemctl" not in line.split("#", 1)[0]
    assert "rm -rf" not in text
    secret = "TEST-ONLY-NOT-A-REAL-SECRET"
    env_file = tmp_path / "outside" / "kraken.env"
    env_file.parent.mkdir()
    env_file.write_text(f"KRAKEN_API_KEY=test-key\nKRAKEN_API_SECRET={secret}\n")
    env_file.chmod(0o600)
    home = tmp_path / "home"
    unit_root = tmp_path / "xdg"
    base_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(unit_root),
        "TRADIKG_HOME": str(home / "tradikg"),
        "TRADIKG_VENV": str(tmp_path / "venv"),
    }
    bad_mode = tmp_path / "open.env"
    bad_mode.write_text("KRAKEN_API_KEY=x\n")
    bad_mode.chmod(0o644)
    refused = subprocess.run(
        ["bash", str(script), str(bad_mode)], capture_output=True, text=True, env=base_env
    )
    assert refused.returncode != 0 and "0600" in refused.stderr
    inside = subprocess.run(
        ["bash", str(script), str(ROOT / "README.md")],
        capture_output=True,
        text=True,
        env=base_env,
    )
    assert inside.returncode != 0 and "Git" in inside.stderr
    ok = subprocess.run(
        ["bash", str(script), str(env_file)], capture_output=True, text=True, env=base_env
    )
    assert ok.returncode == 0, ok.stderr
    unit = (unit_root / "systemd" / "user" / "tradikg-live.service").read_text()
    assert secret not in unit and "test-key" not in unit
    assert "__" not in unit
    assert "--budget 20" in unit
    assert f"EnvironmentFile={env_file.resolve()}" in unit
    assert "RestartPreventExitStatus=2" in unit
    assert "nicht gestartet" in ok.stdout
    assert not (home / "tradikg" / "live" / "live.pid").exists()


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
