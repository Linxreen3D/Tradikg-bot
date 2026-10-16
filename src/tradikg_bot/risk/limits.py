"""Tägliche Verlust-Abschaltschwelle."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class DailyLossGuard:
    """Stoppt neue Einstiege, sobald die Equity ``limit`` unter den Tagesbeginn fällt.

    Tagesgrenze ist Mitternacht UTC. Die Referenz ist die Equity zum Ende der
    letzten Kerze des Vortags (bzw. das Startkapital).
    """

    limit: float = 0.10
    current_day: dt.date | None = None
    day_start_equity: float = 0.0
    halted: bool = False
    halted_days: list[dt.date] = field(default_factory=list)

    def start_day(self, day: dt.date, equity: float) -> None:
        self.current_day = day
        self.day_start_equity = equity
        self.halted = False

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
