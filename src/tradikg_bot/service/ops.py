"""Betriebsfunktionen: Instanzsperre, strukturierte Logs, Backups, Integritätsprüfung, Status."""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    """Verhindert mehrere gleichzeitig laufende Trading-Instanzen (``fcntl.flock``).

    Die Sperre wird vom Betriebssystem bei Prozessende automatisch freigegeben –
    auch nach einem Absturz. Eine veraltete Sperrdatei blockiert daher nicht.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> InstanceLock:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 - bleibt bis release() offen
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.seek(0)
            other = fh.read().strip()
            fh.close()
            raise AlreadyRunningError(
                f"Es läuft bereits eine Instanz (PID {other or 'unbekannt'})"
            ) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is not None:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


_SECRET_PATTERNS = [re.compile(r"(?i)(api[-_ ]?(key|secret|sign)\s*[:=]\s*)\S+")]


class RedactSecrets(logging.Filter):
    """Entfernt Schlüsselwerte aus Logmeldungen (Umgebungswerte und typische Muster)."""

    def __init__(self) -> None:
        super().__init__()
        self.values = [
            v for k, v in os.environ.items() if k in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET") and v
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for v in self.values:
            msg = msg.replace(v, "***")
        for pat in _SECRET_PATTERNS:
            msg = pat.sub(r"\1***", msg)
        record.msg, record.args = msg, ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_dir: Path, level: str = "INFO", name: str = "tradikg") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(level.upper())
    logger.handlers.clear()
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / f"{name}.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for h in (file_handler, console):
        h.addFilter(RedactSecrets())
        logger.addHandler(h)
    return logger


def integrity_check(db: Path) -> None:
    """Bricht ab, wenn eine Datenbank beschädigt ist (kein stilles Weiterarbeiten)."""
    if not db.exists():
        return
    with sqlite3.connect(db) as conn:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"Datenbank {db} ist beschädigt: {result}. Backup wiederherstellen.")


def backup_databases(data_dir: Path, backup_dir: Path, keep: int = 14) -> list[Path]:
    """Konsistente Online-Backups aller SQLite-Dateien (sqlite3-Backup-API)."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target_dir = backup_dir / stamp
    created = []
    for db in sorted(data_dir.rglob("*.sqlite")):
        rel = db.relative_to(data_dir)
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as src, sqlite3.connect(dest) as dst:
            src.backup(dst)
        created.append(dest)
    snapshots = sorted(p for p in backup_dir.iterdir() if p.is_dir())
    for old in snapshots[:-keep]:
        for f in sorted(old.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        old.rmdir()
    return created


def write_status(path: Path, status: dict[str, Any]) -> None:
    """Atomar schreiben (tmp + rename), damit ein Absturz keine halbe Datei hinterlässt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2, default=str))
    os.replace(tmp, path)


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
