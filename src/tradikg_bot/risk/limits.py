"""Tägliche Verlust-Abschaltschwelle."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Trendfolge (live/trend_live.py und Paper/Replay): freigegebene Standardwerte.
# Der Backtest übergibt weiterhin sein eigenes Limit (RiskConfig, Standard 10 %)
# und den Kerzentag selbst; diese Konstanten ändern ihn nicht.
TREND_DAILY_LOSS_FRACTION = 0.05
TREND_DAILY_LOSS_TIMEZONE = "Europe/Vienna"
# Nur enger einstellbar. 10 % ist die Obergrenze, nicht das Ziel.
TREND_DAILY_LOSS_FRACTION_MAX = 0.10


def validate_trend_daily_loss(fraction: float, timezone: str) -> None:
    """Prüft Schwelle und Reset-Zeitzone der Trend-Tagesgrenze.

    Höchstens 10 % der Equity zum Tagesbeginn. Ein weiterer Wert wird abgelehnt,
    ein engerer (z. B. 3 %) ist erlaubt.
    """
    if not 0 < fraction <= TREND_DAILY_LOSS_FRACTION_MAX:
        raise ValueError(
            "daily_loss_fraction muss in (0, 0.10] liegen "
            "(höchstens 10 % der Equity zum Tagesbeginn, nur enger einstellbar)"
        )
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Ungültige Zeitzone für den Tagesreset: {timezone}") from exc


@dataclass
class DailyLossGuard:
    """Stoppt neue Einstiege, sobald die Equity ``limit`` unter den Tagesbeginn fällt.

    ``start_day`` übernimmt den Tag vom Aufrufer (Backtest: Kalendertag der Kerze,
    bisher Mitternacht UTC). ``observe`` rollt den Tag selbst um 00:00 in
    ``timezone``. Die Trendfolge nutzt 5 % und Europe/Vienna; dieser Standard gilt
    nur, wenn der Aufrufer ihn übergibt. ``DailyLossGuard()`` ohne Argumente bleibt
    bei 10 % und UTC, damit der bestehende Backtest sich nicht ändert.

    Gerissen wird nur gemeldet (``halted``). Ob offene Positionen verkauft werden,
    entscheidet der Aufrufer: der Backtest stellt glatt, die Trendfolge nicht.
    """

    limit: float = 0.10
    current_day: dt.date | None = None
    day_start_equity: float = 0.0
    halted: bool = False
    halted_days: list[dt.date] = field(default_factory=list)
    timezone: str = "UTC"

    def trading_day(self, now: dt.datetime) -> dt.date:
        """Kalendertag von ``now`` in der Reset-Zeitzone (00:00 lokal)."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt.UTC)
        return now.astimezone(ZoneInfo(self.timezone)).date()

    def start_day(self, day: dt.date, equity: float) -> None:
        self.current_day = day
        self.day_start_equity = equity
        self.halted = False

    def observe(self, now: dt.datetime, equity: float) -> bool:
        """Rollt den lokalen Tag und prüft die Schwelle.

        Am ersten Aufruf eines neuen Tages wird ``equity`` zur Referenz. Ein
        späterer Aufruf am selben Tag ändert die Referenz nicht. Rückgabe True,
        wenn die Schwelle *in diesem Aufruf* neu gerissen wurde.
        """
        day = self.trading_day(now)
        if self.current_day != day:
            self.start_day(day, equity)
        return self.check(equity)

    @property
    def floor(self) -> float:
        return self.day_start_equity * (1 - self.limit)

    def remaining_budget(self, equity: float) -> float:
        if self.halted:
            return 0.0
        return max(0.0, equity - self.floor)

    def check(self, equity: float) -> bool:
        """Aktualisiert den Status; gibt True zurück, wenn die Schwelle *neu* gerissen wurde."""
        if not self.halted and equity <= self.floor:
            self.halted = True
            if self.current_day is not None:
                self.halted_days.append(self.current_day)
            return True
        return False

    def snapshot(self) -> dict[str, Any]:
        """Zustand für die Zustandsdatei (Neustart am selben Tag behält Referenz und Sperre)."""
        return {
            "limit": self.limit,
            "timezone": self.timezone,
            "current_day": None if self.current_day is None else self.current_day.isoformat(),
            "day_start_equity": self.day_start_equity,
            "halted": self.halted,
            "halted_days": [d.isoformat() for d in self.halted_days],
        }

    @classmethod
    def restore(
        cls,
        data: dict[str, Any] | None,
        *,
        limit: float,
        timezone: str,
    ) -> DailyLossGuard:
        """Baut den Guard aus der Datei. Schwelle und Zeitzone kommen aus der Konfiguration."""
        guard = cls(limit=limit, timezone=timezone)
        if not data:
            return guard
        raw_day = data.get("current_day")
        if raw_day:
            guard.current_day = dt.date.fromisoformat(str(raw_day))
        guard.day_start_equity = float(data.get("day_start_equity") or 0.0)
        guard.halted = bool(data.get("halted"))
        guard.halted_days = [dt.date.fromisoformat(str(d)) for d in data.get("halted_days") or []]
        return guard
