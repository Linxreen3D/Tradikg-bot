"""Konfiguration der lokalen Plattform (Paper-Dauerbetrieb, Research, Dashboard).

Datenablage standardmäßig unter ``~/.tradikg`` (überschreibbar mit ``TRADIKG_HOME``):

    ~/.tradikg/
      config.toml        lokale Konfiguration (nie API-Schlüssel!)
      data/              SQLite-Datenbanken (market, research, models, paper)
      logs/              strukturierte Logs (JSON-Zeilen, rotiert)
      backups/           automatische Datenbank-Backups
      run/               Sperrdatei, Status, Heartbeat, KILL_SWITCH

Es gibt nur den Modus ``paper``. ``live`` und Schlüsselfelder werden abgelehnt.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from tradikg_bot.config import LiveTradingNotAllowedError, _reject_secrets
from tradikg_bot.risk.limits import (
    TREND_DAILY_LOSS_FRACTION,
    TREND_DAILY_LOSS_TIMEZONE,
    validate_trend_daily_loss,
)

DEFAULT_HOME = Path("~/.tradikg")


@dataclass(frozen=True)
class PlatformConfig:
    mode: str = "paper"
    timeframe: str = "1h"
    capital_eur: float = 100.0
    symbols: tuple[str, ...] = ("XBTEUR", "ETHEUR", "SOLEUR", "XRPEUR")
    use_scanner: bool = False
    strategies: tuple[str, ...] = ()  # leer = alle Strategiefamilien
    research_random_runs: int = 200
    min_history_bars: int = 250
    backfill_bars: int = 48
    tax_rate: float = 0.0
    log_level: str = "INFO"
    dashboard_port: int = 8501
    local_llm_enabled: bool = False  # optional, standardmäßig aus; nicht implementiert
    strategy_mode: str = "signals"  # signals = Bracket, trend = 200-Tage-Allokation, grid = Netz
    invest_fraction: float = 1.0  # trend: höchstens investierter Kapitalanteil (Risikoregler)
    cash_yield_pa: float = 0.0  # trend: Zins auf Cash, nur wenn er tatsächlich gezahlt wird
    # Trend-Tagesverlustgrenze. Aus = bisheriges Verhalten. Höchstens 10 %, Standard 5 %.
    daily_loss_enabled: bool = False
    daily_loss_fraction: float = TREND_DAILY_LOSS_FRACTION
    daily_loss_timezone: str = TREND_DAILY_LOSS_TIMEZONE
    home: Path = field(default=DEFAULT_HOME)

    def __post_init__(self) -> None:
        if self.mode.lower() != "paper":
            raise LiveTradingNotAllowedError(
                f"Modus '{self.mode}' ist gesperrt – die Plattform läuft nur im Paper-Modus."
            )
        if self.strategy_mode not in ("signals", "trend", "grid"):
            raise ValueError("strategy_mode muss 'signals', 'trend' oder 'grid' sein")
        if not 0 < self.invest_fraction <= 1:
            raise ValueError("invest_fraction muss in (0, 1] liegen")
        if not 0 <= self.cash_yield_pa < 0.2:
            raise ValueError("cash_yield_pa muss in [0, 0.2) liegen")
        if self.capital_eur <= 0:
            raise ValueError("capital_eur muss > 0 sein")
        if not 0 <= self.tax_rate < 1:
            raise ValueError("tax_rate muss in [0, 1) liegen")
        validate_trend_daily_loss(self.daily_loss_fraction, self.daily_loss_timezone)
        object.__setattr__(self, "home", Path(self.home).expanduser())

    # -- Pfade -------------------------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.home / "backups"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def market_db(self) -> Path:
        return self.data_dir / "market.sqlite"

    @property
    def research_db(self) -> Path:
        return self.data_dir / "research.sqlite"

    @property
    def replay_db(self) -> Path:
        return self.data_dir / "replays.sqlite"

    @property
    def replay_dir(self) -> Path:
        return self.data_dir / "replays"

    @property
    def models_db(self) -> Path:
        return self.data_dir / "models.sqlite"

    @property
    def paper_dir(self) -> Path:
        return self.data_dir / "paper"

    @property
    def kill_switch(self) -> Path:
        return self.paper_dir / "KILL_SWITCH"

    @property
    def status_file(self) -> Path:
        return self.run_dir / "status.json"

    @property
    def lock_file(self) -> Path:
        return self.run_dir / "paper.lock"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.log_dir, self.backup_dir, self.run_dir, self.paper_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- Laden -------------------------------------------------------------------------------
    @classmethod
    def load(
        cls, path: str | Path | None = None, env: dict[str, str] | None = None
    ) -> PlatformConfig:
        env = dict(os.environ) if env is None else env
        home = Path(env.get("TRADIKG_HOME", str(DEFAULT_HOME))).expanduser()
        cfg_path = Path(path) if path else home / "config.toml"
        raw: dict[str, Any] = {}
        if cfg_path.exists():
            with open(cfg_path, "rb") as fh:
                raw = tomllib.load(fh)
        _reject_secrets(raw)
        section = dict(raw.get("platform", {}))
        known = {f.name for f in fields(cls)}
        unknown = set(section) - known
        if unknown:
            raise ValueError(f"Unbekannte Plattform-Parameter: {sorted(unknown)}")
        for key in ("symbols", "strategies"):
            if key in section:
                section[key] = tuple(section[key])
        section.setdefault("home", home)
        return cls(**section)


DEFAULT_CONFIG_TOML = """# Tradikg-Plattform – lokale Konfiguration (Paper-Betrieb)
# Niemals API-Schlüssel in diese Datei schreiben (wird abgelehnt).

[platform]
mode = "paper"              # einziger erlaubter Modus
timeframe = "1h"            # 1m, 5m, 15m, 30m, 1h, 4h, 1d
capital_eur = 100.0         # virtuelles Startkapital
symbols = ["XBTEUR", "ETHEUR", "SOLEUR", "XRPEUR"]
use_scanner = false         # true: handelbare Paare automatisch über den Scanner wählen
strategies = []             # leer = alle Strategiefamilien
research_random_runs = 200
min_history_bars = 250
backfill_bars = 48          # nach Schlafmodus/Netzausfall nachzuholende Kerzen
tax_rate = 0.0              # Steuerrücklage (keine steuerliche Beratung)
log_level = "INFO"
dashboard_port = 8501
local_llm_enabled = false   # optional, nicht implementiert
strategy_mode = "signals"   # "trend" = 200-Tage-Allokation (für 1d gedacht)
invest_fraction = 1.0       # trend: max. investierter Anteil (0.5 = halbes Risiko)
cash_yield_pa = 0.0         # trend: Zins auf Cash (nur wenn real gezahlt)
daily_loss_enabled = false  # trend: Tagesverlustgrenze (5 %, 00:00 Europe/Vienna). Aus = wie bisher
daily_loss_fraction = 0.05  # höchstens 0.10, nur enger. ~1 € an einem 20-€-Konto
daily_loss_timezone = "Europe/Vienna"
"""
