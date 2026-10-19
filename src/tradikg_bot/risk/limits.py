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


# Drawdown vom Höchststand der Bot-Equity. −30 % hätte den Trend 2019 beendet,
# deshalb liegt die weiteste erlaubte Schwelle bei 45 %. Nur enger einstellbar.
TREND_DRAWDOWN_FRACTION = 0.45
DRAWDOWN_HALT_NAME = "DRAWDOWN_HALT"


def validate_trend_drawdown(fraction: float) -> None:
    """Höchstens 45 % unter dem Höchststand. Ein weiterer Wert wird abgelehnt."""
    if not 0 < fraction <= TREND_DRAWDOWN_FRACTION:
        raise ValueError(
            "drawdown_fraction muss in (0, 0.45] liegen "
            "(höchstens 45 % unter dem Höchststand der Equity, nur enger einstellbar)"
        )


def format_drawdown_banner(info: dict[str, Any] | None) -> str:
    """Eine Zeile für Snapshot-Dashboard. Leer, wenn der Bot noch keinen Stand geschrieben hat."""
    if not info:
        return ""
    if not info.get("enabled"):
        return "Drawdown-Sperre: aus"
    peak = float(info.get("peak_equity") or 0.0)
    equity = float(info.get("equity") or 0.0)
    limit = float(info.get("limit") or TREND_DRAWDOWN_FRACTION)
    dd = abs(float(info.get("drawdown") or 0.0))
    if info.get("halted"):
        return (
            f"DRAWDOWN-SPERRE aktiv: Equity {equity:.2f} € ist {dd:.0%} unter dem Höchststand "
            f"{peak:.2f} € (Grenze −{limit:.0%}). Keine neuen Käufe. "
            "Lösen nur mit `tradikg live drawdown-reset`. "
            "DRAWDOWN_HALT ist nur ein Hinweis; Löschen hebt die Sperre nicht auf. "
            "Verkäufe und Kraken-Stops laufen weiter."
        )
    return (
        f"Drawdown {dd:.1%} vom Höchststand {peak:.2f} € "
        f"(Grenze −{limit:.0%}, Equity {equity:.2f} €). Sperre aus."
    )


@dataclass
class DrawdownGuard:
    """Sperrt neue Käufe, wenn die Equity ``limit`` unter ihren Höchststand fällt.

    Die Sperre löst sich nicht von selbst, auch nicht bei Erholung oder an einem
    neuen Tag. ``reset`` merkt eine Lösung vor; der nächste ``observe`` setzt den
    Höchststand auf die dann gesehene Equity. Es wird nichts verkauft.
    """

    limit: float = TREND_DRAWDOWN_FRACTION
    peak_equity: float = 0.0
    halted: bool = False
    rebase: bool = False
    peak_at: str | None = None
    halted_at: str | None = None

    @property
    def floor(self) -> float:
        return self.peak_equity * (1 - self.limit)

    def drawdown_from(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return equity / self.peak_equity - 1.0

    def observe(self, equity: float, now: dt.datetime | None = None) -> bool:
        """Zieht den Höchststand nach. True, wenn die Sperre *jetzt* neu einrastet.

        Nach ``reset`` wird der Höchststand auf ``equity`` gesetzt und diese
        Beobachtung löst nicht aus.
        """
        stamp = now.isoformat() if now is not None else None
        if self.rebase:
            self.peak_equity = max(0.0, equity)
            self.peak_at = stamp
            self.halted = False
            self.halted_at = None
            self.rebase = False
            return False
        if self.peak_equity <= 0:
            self.peak_equity = max(0.0, equity)
            self.peak_at = stamp
            return False
        if not self.halted and equity > self.peak_equity:
            self.peak_equity = equity
            self.peak_at = stamp
        if not self.halted and equity <= self.floor:
            self.halted = True
            self.halted_at = stamp
            return True
        return False

    def reset(self) -> None:
        """Manuelle Lösung. Der nächste ``observe`` setzt den Höchststand neu."""
        self.halted = False
        self.halted_at = None
        self.rebase = True

    def snapshot(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "peak_equity": self.peak_equity,
            "halted": self.halted,
            "rebase": self.rebase,
            "peak_at": self.peak_at,
            "halted_at": self.halted_at,
        }

    @classmethod
    def restore(cls, data: dict[str, Any] | None, *, limit: float) -> DrawdownGuard:
        guard = cls(limit=limit)
        if not data:
            return guard
        guard.peak_equity = float(data.get("peak_equity") or 0.0)
        guard.halted = bool(data.get("halted"))
        guard.rebase = bool(data.get("rebase"))
        guard.peak_at = data.get("peak_at")
        guard.halted_at = data.get("halted_at")
        return guard
