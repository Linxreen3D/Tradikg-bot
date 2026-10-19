"""Zentrale Konfiguration.

Sicherheitsprinzip: Es existiert bewusst **kein** Echtgeld-Modus. Jeder Versuch,
einen Live-/Echtgeld-Modus zu konfigurieren, führt zu einem Fehler. Ein Wechsel in
den Echtgeldhandel ist in diesem Projektstand weder implementiert noch vorgesehen.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any


class LiveTradingNotAllowedError(RuntimeError):
    """Wird ausgelöst, wenn Echtgeldhandel angefordert wird."""


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"

    @classmethod
    def parse(cls, value: str) -> TradingMode:
        normalized = value.strip().lower()
        if normalized in {"live", "real", "real_money", "production", "echtgeld"}:
            raise LiveTradingNotAllowedError(
                f"Modus '{value}' ist gesperrt: Es wird ausschließlich virtuell gehandelt."
            )
        return cls(normalized)


@dataclass(frozen=True)
class RiskConfig:
    """Risikoparameter (Anteile als Dezimalzahl, z. B. 0.01 = 1 %)."""

    risk_per_trade: float = 0.01
    max_position_fraction: float = 0.5
    daily_loss_limit: float = (
        0.10  # nur Signal-/Bracket-Backtest; die Trendfolge hat ein eigenes Flag
    )
    min_reward_risk_after_costs: float = 1.0

    def __post_init__(self) -> None:
        if not 0 < self.risk_per_trade <= 0.1:
            raise ValueError("risk_per_trade muss in (0, 0.1] liegen")
        if not 0 < self.max_position_fraction <= 1:
            raise ValueError("max_position_fraction muss in (0, 1] liegen")
        if not 0 < self.daily_loss_limit < 1:
            raise ValueError("daily_loss_limit muss in (0, 1) liegen")
        if self.min_reward_risk_after_costs < 0:
            raise ValueError("min_reward_risk_after_costs darf nicht negativ sein")


@dataclass(frozen=True)
class AppConfig:
    initial_capital: float = 100.0
    currency: str = "EUR"
    mode: TradingMode = TradingMode.BACKTEST
    db_path: Path = Path("data/tradikg.sqlite")
    risk: RiskConfig = field(default_factory=RiskConfig)
    # Analyse-Benchmark: Nettorendite bezogen auf das tägliche Handelsvolumen.
    # Wird nur ausgewertet, niemals als Handelsziel verwendet.
    daily_volume_return_benchmark: float = 0.10

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital muss > 0 sein")
        if not isinstance(self.mode, TradingMode):
            object.__setattr__(self, "mode", TradingMode.parse(str(self.mode)))

    @classmethod
    def from_toml(cls, path: str | Path) -> AppConfig:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppConfig:
        _reject_secrets(raw)
        app = dict(raw.get("app", {}))
        risk_raw = raw.get("risk", {})
        known_risk = {f.name for f in fields(RiskConfig)}
        unknown = set(risk_raw) - known_risk
        if unknown:
            raise ValueError(f"Unbekannte Risikoparameter: {sorted(unknown)}")
        kwargs: dict[str, Any] = {}
        if "initial_capital" in app:
            kwargs["initial_capital"] = float(app["initial_capital"])
        if "currency" in app:
            kwargs["currency"] = str(app["currency"])
        if "mode" in app:
            kwargs["mode"] = TradingMode.parse(str(app["mode"]))
        if "db_path" in app:
            kwargs["db_path"] = Path(app["db_path"])
        if "daily_volume_return_benchmark" in app:
            kwargs["daily_volume_return_benchmark"] = float(app["daily_volume_return_benchmark"])
        return cls(risk=RiskConfig(**risk_raw), **kwargs)


_SECRET_HINTS = ("api_key", "apikey", "secret", "password", "private_key")


def _reject_secrets(raw: dict[str, Any], prefix: str = "") -> None:
    """Verhindert, dass API-Schlüssel o. Ä. in der Konfiguration landen."""
    for key, value in raw.items():
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            raise LiveTradingNotAllowedError(
                f"Konfigurationsschlüssel '{prefix}{key}' ist nicht erlaubt: "
                "API-Schlüssel für Echtgeldhandel werden nicht unterstützt."
            )
        if isinstance(value, dict):
            _reject_secrets(value, prefix=f"{prefix}{key}.")
