"""Linux-Schleife ohne systemd: venv, Verweigerung, Hook. Keine Kraken-Aufrufe."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = ROOT / "deploy" / "linux" / "run_live_loop.sh"
ENSURE = ROOT / "deploy" / "linux" / "ensure_running.sh"
HOOK = ROOT / "deploy" / "linux" / "install_login_hook.sh"
SECRET = "super-secret-value"
KEY_VALUE = "dummy-key-value"
SECRET_VALUE = "dummy-secret-value"

PY3 = """#!/bin/bash
set -euo pipefail
printf 'python3 %s\\n' "$*" >> "$CALL_LOG"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  dest="${3:-.venv}"
  mkdir -p "$dest/bin"
  cp "$FAKE_PY" "$dest/bin/python"
  cp "$FAKE_PIP" "$dest/bin/pip"
  cp "$FAKE_TRADIKG" "$dest/bin/tradikg"
  chmod 755 "$dest/bin/python" "$dest/bin/pip" "$dest/bin/tradikg"
  exit 0
fi
printf 'python3 unexpected\\n' >> "$CALL_LOG"
exit 1
"""

PY3_FORBIDDEN = """#!/bin/bash
printf 'python3 %s\\n' "$*" >> "$CALL_LOG"
exit 1
"""

HEALTHY_PY = """#!/bin/bash
exit 0
"""

PIP = """#!/bin/bash
set -euo pipefail
printf 'pip %s\\n' "$*" >> "$CALL_LOG"
exit "${FAKE_PIP_EXIT:-0}"
"""

TRADIKG_OK = """#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$1 $2" in
  "live restart-check") exit 0 ;;
  "live run") exit 0 ;;
esac
exit 99
"""

TRADIKG_REFUSE = """#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "$CALL_LOG"
if [ "$1 $2" = "live restart-check" ]; then
  printf '%s\\n' "Neustart verweigert: Stop an der Börse fehlt. Kein Neustart."
  exit 2
fi
printf 'live run should not happen\\n' >> "$CALL_LOG"
exit 99
"""

TRADIKG_SLEEP = """#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "$CALL_LOG"
if [ "$1 $2" = "live restart-check" ]; then
  exit 0
fi
if [ "$1 $2" = "live run" ]; then
  sleep 30
  exit 0
fi
exit 99
"""


def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def _layout(tmp: Path) -> dict[str, Path]:
    repo = tmp / "repo"
    home = tmp / "home"
    repo.mkdir()
    config = home / ".config" / "tradikg"
    config.mkdir(parents=True)
    env_file = config / "live.env"
    env_file.write_text(f"KRAKEN_API_KEY={KEY_VALUE}\nKRAKEN_API_SECRET={SECRET_VALUE}\n")
    env_file.chmod(0o600)
    return {
        "repo": repo,
        "home": home,
        "bin": tmp / "bin",
        "log": home / ".tradikg" / "live" / "loop.log",
        "calls": tmp / "calls.log",
        "pid": home / ".tradikg" / "live" / "loop.pid",
    }


def _env(paths: dict[str, Path], **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{paths['bin']}:/usr/bin:/bin",
        "HOME": str(paths["home"]),
        "XDG_CONFIG_HOME": str(paths["home"] / ".config"),
        "TRADIKG_HOME": str(paths["home"] / ".tradikg"),
        "TRADIKG_REPO": str(paths["repo"]),
        "TRADIKG_LOOP_LOG": str(paths["log"]),
        "TRADIKG_RESTART_DELAY_S": "0",
        "CALL_LOG": str(paths["calls"]),
        "FAKE_PY": str(paths["bin"] / "venv-python"),
        "FAKE_PIP": str(paths["bin"] / "venv-pip"),
        "FAKE_TRADIKG": str(paths["bin"] / "venv-tradikg"),
        "KRAKEN_API_KEY": SECRET,
        "KRAKEN_API_SECRET": SECRET,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra)
    return env


def _install_fakes(paths: dict[str, Path], tradikg: str, *, python: str = PY3) -> None:
    _exe(paths["bin"] / "python3", python)
    _exe(paths["bin"] / "venv-python", HEALTHY_PY)
    _exe(paths["bin"] / "venv-pip", PIP)
    _exe(paths["bin"] / "venv-tradikg", tradikg)


def _healthy_venv(repo: Path, tradikg: str) -> None:
    bindir = repo / ".venv" / "bin"
    _exe(bindir / "python", HEALTHY_PY)
    _exe(bindir / "pip", PIP)
    _exe(bindir / "tradikg", tradikg)


def _run(
    script: Path, env: dict[str, str], timeout: float = 20
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _alarms(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[ALARM]")]


def _assert_no_secrets(proc: subprocess.CompletedProcess[str], *paths: Path) -> None:
    chunks = [proc.stdout, proc.stderr]
    for path in paths:
        if path.exists():
            chunks.append(path.read_text())
    blob = "\n".join(chunks)
    assert SECRET not in blob
    assert KEY_VALUE not in blob
    assert SECRET_VALUE not in blob


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    text = path.read_text().strip()
    if not text.isdigit() or int(text) <= 1:
        return None
    pid = int(text)
    return pid if _alive(pid) else None


def _wait_pid(path: Path, timeout: float = 10) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = _read_pid(path)
        if pid is not None:
            return pid
        time.sleep(0.05)
    raise AssertionError("Schleife hat keine lebende PID geschrieben")


def _wait_call(path: Path, needle: str, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text():
            return path.read_text()
        time.sleep(0.05)
    found = path.read_text() if path.exists() else ""
    raise AssertionError(f"{needle!r} fehlt in {found!r}")


def _kill(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                return
        for _ in range(25):
            if not _alive(pid):
                return
            time.sleep(0.1)


def test_loop_script_keeps_flags_and_does_not_name_the_secret_field():
    text = LOOP.read_text()
    assert "python3 -m venv .venv && .venv/bin/pip install -e ." in text
    assert "--budget 20" in text
    assert "--daily-loss-fraction 0.05" in text
    assert "--drawdown-kill-fraction 0.45" in text
    assert "--require-dry-run" in text
    assert "live restart-check" in text
    lowered = text.lower()
    assert "api_secret" not in lowered
    for script in (LOOP, ENSURE, HOOK):
        assert script.stat().st_mode & 0o111


def test_missing_venv_is_rebuilt_and_alarms(tmp_path: Path):
    paths = _layout(tmp_path)
    _install_fakes(paths, TRADIKG_OK)
    proc = _run(LOOP, _env(paths))
    calls = paths["calls"].read_text()
    log = paths["log"].read_text()
    assert proc.returncode == 0, proc.stderr + proc.stdout
    alarms = _alarms(log)
    assert alarms and "Virtuelle Umgebung fehlt oder ist defekt" in alarms[0]
    assert "python3 -m venv .venv" in calls
    assert "pip install -e ." in calls
    assert "live restart-check" in calls and "live run" in calls
    run = next(line for line in calls.splitlines() if line.startswith("live run"))
    assert "--budget" in run and "20" in run
    assert "--daily-loss-fraction" in run and "0.05" in run
    assert "--drawdown-kill-fraction" in run and "0.45" in run
    assert "--require-dry-run" in run and "--i-understand-this-uses-real-money" in run
    check = next(line for line in calls.splitlines() if "restart-check" in line)
    assert "--require-dry-run" not in check
    assert (paths["repo"] / ".venv" / "bin" / "tradikg").is_file()
    _assert_no_secrets(proc, paths["log"], paths["calls"])


def test_broken_import_rebuilds_the_venv(tmp_path: Path):
    paths = _layout(tmp_path)
    _install_fakes(paths, TRADIKG_OK)
    _exe(paths["repo"] / ".venv" / "bin" / "python", "#!/bin/bash\nexit 1\n")
    proc = _run(LOOP, _env(paths))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    log = paths["log"].read_text()
    assert _alarms(log)
    assert "Virtuelle Umgebung fehlt oder ist defekt" in log
    assert "python3 -m venv .venv" in paths["calls"].read_text()
    _assert_no_secrets(proc, paths["log"])


def test_failed_install_alarms_and_does_not_start(tmp_path: Path):
    paths = _layout(tmp_path)
    _install_fakes(paths, TRADIKG_OK)
    proc = _run(LOOP, _env(paths, FAKE_PIP_EXIT="1"))
    calls = paths["calls"].read_text() if paths["calls"].exists() else ""
    log = paths["log"].read_text()
    assert proc.returncode == 2
    alarms = _alarms(log)
    assert any("Virtuelle Umgebung fehlt oder ist defekt" in line for line in alarms)
    assert any("Installation fehlgeschlagen" in line for line in alarms)
    assert "pip install -e ." in calls
    assert "live restart-check" not in calls
    assert "live run" not in calls
    assert _read_pid(paths["pid"]) is None
    _assert_no_secrets(proc, paths["log"], paths["calls"])


def test_refused_restart_check_exits_2_and_alarms(tmp_path: Path):
    paths = _layout(tmp_path)
    _install_fakes(paths, TRADIKG_REFUSE, python=PY3_FORBIDDEN)
    _healthy_venv(paths["repo"], TRADIKG_REFUSE)
    proc = _run(LOOP, _env(paths))
    calls = paths["calls"].read_text()
    log = paths["log"].read_text()
    assert proc.returncode == 2
    alarms = _alarms(proc.stdout) + _alarms(log)
    assert any(line.startswith("[ALARM]") and "Neustart verweigert" in line for line in alarms)
    assert any("Kein Neustart" in line for line in alarms)
    assert "live restart-check" in calls
    assert "live run" not in calls
    assert "python3" not in calls
    assert _read_pid(paths["pid"]) is None
    _assert_no_secrets(proc, paths["log"], paths["calls"])


def test_ensure_running_is_idempotent(tmp_path: Path):
    paths = _layout(tmp_path)
    _install_fakes(paths, TRADIKG_SLEEP, python=PY3_FORBIDDEN)
    _healthy_venv(paths["repo"], TRADIKG_SLEEP)
    env = _env(paths)
    pid: int | None = None
    try:
        first = _run(ENSURE, env)
        assert first.returncode == 0, first.stderr
        assert first.stdout == ""
        assert first.stderr == ""
        pid = _wait_pid(paths["pid"])
        _wait_call(paths["calls"], "live run")
        second = _run(ENSURE, env)
        assert second.returncode == 0
        assert second.stdout == ""
        assert second.stderr == ""
        time.sleep(0.4)
        calls = paths["calls"].read_text()
        assert calls.count("live restart-check") == 1
        assert calls.count("live run") == 1
        assert _read_pid(paths["pid"]) == pid
        _assert_no_secrets(first, paths["log"], paths["calls"])
        _assert_no_secrets(second, paths["log"])
    finally:
        if pid is None:
            pid = _read_pid(paths["pid"])
        if pid is not None:
            _kill(pid)


def test_second_loop_is_refused(tmp_path: Path):
    paths = _layout(tmp_path)
    _install_fakes(paths, TRADIKG_SLEEP, python=PY3_FORBIDDEN)
    _healthy_venv(paths["repo"], TRADIKG_SLEEP)
    env = _env(paths)
    pid: int | None = None
    try:
        started = _run(ENSURE, env)
        assert started.returncode == 0, started.stderr
        pid = _wait_pid(paths["pid"])
        _wait_call(paths["calls"], "live run")
        second = _run(LOOP, env)
        assert second.returncode == 2
        alarms = _alarms(second.stdout) + _alarms(paths["log"].read_text())
        refused = [
            line
            for line in alarms
            if "Neustart verweigert" in line and "Schleife läuft bereits" in line
        ]
        assert refused
        assert paths["calls"].read_text().count("live run") == 1
        _assert_no_secrets(second, paths["log"])
    finally:
        if pid is None:
            pid = _read_pid(paths["pid"])
        if pid is not None:
            _kill(pid)


def test_login_hook_installer_is_idempotent(tmp_path: Path):
    bashrc = tmp_path / ".bashrc"
    profile = tmp_path / ".profile"
    bashrc.write_text("# keep-this-line\n# old tradikg-live-ensure\n")
    profile.write_text("# profile-keep\n")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "KRAKEN_API_KEY": SECRET,
        "KRAKEN_API_SECRET": SECRET,
    }
    first = _run(HOOK, env)
    second = _run(HOOK, env)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    _assert_no_secrets(first)
    _assert_no_secrets(second)
    assert "keep-this-line" not in first.stdout
    for path, keep in ((bashrc, "# keep-this-line"), (profile, "# profile-keep")):
        text = path.read_text()
        assert text.count("tradikg-live-ensure") == 1
        assert keep in text
        assert ">/dev/null" in text
        assert "TRADIKG_ENSURE_DONE" in text
        assert str(ENSURE) in text
        line = next(row for row in text.splitlines() if "tradikg-live-ensure" in row)
        syntax = subprocess.run(
            ["/bin/bash", "-n", "-c", line],
            capture_output=True,
            text=True,
            check=False,
        )
        assert syntax.returncode == 0, syntax.stderr
        fake = tmp_path / f"fake-{path.name}"
        count = tmp_path / f"count-{path.name}"
        _exe(fake, "#!/bin/bash\nprintf 'x\\n' >> \"$COUNT\"\n")
        safe = line.replace(str(ENSURE), str(fake))
        ran = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", f"{safe}\n{safe}\n"],
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "COUNT": str(count)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert ran.returncode == 0, ran.stderr
        assert ran.stdout == ""
        assert ran.stderr == ""
        assert count.read_text() == "x\n"
