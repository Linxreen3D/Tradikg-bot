"""Kapitalbuchhaltung mit Verlustaufholung (High-Water-Mark) und 33/33/34-Aufteilung.

Regeln je abgeschlossener Position mit Netto-Ergebnis ``p`` (nach allen Gebühren):

* ``p < 0``: Die Trading-Balance sinkt um ``|p|``, die offenen Verluste
  (``unrecovered``) steigen um ``|p|``.
* ``p > 0``: Zuerst werden offene Verluste aufgeholt (``r = min(p, unrecovered)``,
  zurück in die Trading-Balance). Erst der Rest wird verteilt:
  optionale Steuerrücklage (Satz konfigurierbar, Standard 0 – **keine steuerliche
  Beratung**), danach 33 % Trading-Balance, 33 % Auszahlungsreserve und 34 %
  Langfristportfolio. Der Rundungsrest geht in das Langfristportfolio, die Summe
  ist exakt.

Eigenschaften:
* **Idempotent:** Jede Buchung hat einen eindeutigen Schlüssel (z. B. Trade-ID).
  Eine doppelte Meldung desselben Trades bucht nichts doppelt.
* **Korrekturen** (z. B. nachträgliche Gebührenkorrektur) werden als eigene
  Buchung mit eigenem Schlüssel erfasst und folgen denselben Regeln.
* **Prüfbar:** Alle Salden ergeben sich als Summen der Buchungszeilen.
* Die Konten sind Buchpositionen auf demselben (Paper-)Konto. Es gibt keine
  automatischen Transfers, Ein- oder Auszahlungen.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SPLIT_TRADING, SPLIT_PAYOUT, SPLIT_LONGTERM = 0.33, 0.33, 0.34

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_key TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    net_pnl REAL NOT NULL,
    recovered REAL NOT NULL,
    to_trading REAL NOT NULL,
    to_payout REAL NOT NULL,
    to_longterm REAL NOT NULL,
    to_tax REAL NOT NULL,
    unrecovered_after REAL NOT NULL,
    note TEXT
);
"""


@dataclass(frozen=True)
class LedgerBalances:
    initial_capital: float
    trading_balance: float
    payout_reserve: float
    longterm_portfolio: float
    tax_reserve: float
    unrecovered_losses: float
    total_net_pnl: float

    @property
    def high_water_mark(self) -> float:
        """Stand, den die Trading-Balance wieder erreichen muss, bevor verteilt wird."""
        return self.trading_balance + self.unrecovered_losses

    @property
    def total_capital(self) -> float:
        return (
            self.trading_balance + self.payout_reserve + self.longterm_portfolio + self.tax_reserve
        )


@dataclass(frozen=True)
class LedgerEntry:
    entry_key: str
    ts: str
    kind: str
    net_pnl: float
    recovered: float
    to_trading: float
    to_payout: float
    to_longterm: float
    to_tax: float
    unrecovered_after: float
    note: str | None


class CapitalLedger:
    def __init__(self, path: str | Path, initial_capital: float, tax_rate: float = 0.0) -> None:
        if initial_capital <= 0:
            raise ValueError("initial_capital muss > 0 sein")
        if not 0 <= tax_rate < 1:
            raise ValueError("tax_rate muss in [0, 1) liegen")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        stored = self._meta("initial_capital")
        if stored is None:
            self._set_meta("initial_capital", repr(initial_capital))
        elif float(stored) != initial_capital:
            raise ValueError(
                f"Ledger wurde mit Startkapital {stored} angelegt, nicht {initial_capital}"
            )
        self.initial_capital = initial_capital
        self.tax_rate = tax_rate

    def close(self) -> None:
        self._conn.close()

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM ledger_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO ledger_meta(key, value) VALUES (?, ?)", (key, value)
        )

    # -- Buchung ---------------------------------------------------------------------------
    def has(self, entry_key: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM ledger_entries WHERE entry_key = ?", (entry_key,)
            ).fetchone()
            is not None
        )

    def book(
        self,
        entry_key: str,
        net_pnl: float,
        ts: dt.datetime,
        kind: str = "trade",
        note: str | None = None,
    ) -> LedgerEntry:
        """Bucht ein Netto-Ergebnis genau einmal (atomar, idempotent)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.entry(entry_key)
            if existing is not None:
                self._conn.execute("ROLLBACK")
                return existing
            unrecovered = self._unrecovered()
            recovered = to_payout = to_longterm = to_tax = 0.0
            if net_pnl < 0:
                to_trading = net_pnl
                unrecovered += -net_pnl
            else:
                recovered = min(net_pnl, unrecovered)
                unrecovered -= recovered
                rest = net_pnl - recovered
                to_tax = rest * self.tax_rate
                distributable = rest - to_tax
                share_trading = distributable * SPLIT_TRADING
                to_payout = distributable * SPLIT_PAYOUT
                to_longterm = distributable - share_trading - to_payout
                to_trading = recovered + share_trading
            self._conn.execute(
                "INSERT INTO ledger_entries(entry_key, ts, kind, net_pnl, recovered, to_trading, "
                "to_payout, to_longterm, to_tax, unrecovered_after, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_key,
                    ts.isoformat(),
                    kind,
                    net_pnl,
                    recovered,
                    to_trading,
                    to_payout,
                    to_longterm,
                    to_tax,
                    max(0.0, unrecovered),
                    note,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.entry(entry_key)  # type: ignore[return-value]

    def adjust(self, adjustment_key: str, delta: float, ts: dt.datetime, note: str) -> LedgerEntry:
        """Korrekturbuchung (z. B. Gebührenkorrektur) – eigene, eindeutige Buchung."""
        return self.book(adjustment_key, delta, ts, kind="adjustment", note=note)

    def _unrecovered(self) -> float:
        row = self._conn.execute(
            "SELECT unrecovered_after FROM ledger_entries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return float(row[0]) if row else 0.0

    # -- Abfragen --------------------------------------------------------------------------
    def entry(self, entry_key: str) -> LedgerEntry | None:
        row = self._conn.execute(
            "SELECT entry_key, ts, kind, net_pnl, recovered, to_trading, to_payout, to_longterm, "
            "to_tax, unrecovered_after, note FROM ledger_entries WHERE entry_key = ?",
            (entry_key,),
        ).fetchone()
        return LedgerEntry(*row) if row else None

    def entries(self) -> list[LedgerEntry]:
        rows = self._conn.execute(
            "SELECT entry_key, ts, kind, net_pnl, recovered, to_trading, to_payout, to_longterm, "
            "to_tax, unrecovered_after, note FROM ledger_entries ORDER BY id"
        ).fetchall()
        return [LedgerEntry(*r) for r in rows]

    def balances(self, before: dt.datetime | None = None) -> LedgerBalances:
        where, params = "", ()
        if before is not None:
            where, params = "WHERE ts < ?", (before.isoformat(),)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(to_trading),0), COALESCE(SUM(to_payout),0), "
            f"COALESCE(SUM(to_longterm),0), COALESCE(SUM(to_tax),0), COALESCE(SUM(net_pnl),0) "
            f"FROM ledger_entries {where}",
            params,
        ).fetchone()
        last = self._conn.execute(
            f"SELECT unrecovered_after FROM ledger_entries {where} ORDER BY id DESC LIMIT 1", params
        ).fetchone()
        return LedgerBalances(
            initial_capital=self.initial_capital,
            trading_balance=self.initial_capital + row[0],
            payout_reserve=row[1],
            longterm_portfolio=row[2],
            tax_reserve=row[3],
            unrecovered_losses=float(last[0]) if last else 0.0,
            total_net_pnl=row[4],
        )

    def day_start_trading_balance(self, day: dt.date) -> float:
        start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
        return self.balances(before=start).trading_balance

    def capital_limit(self, cap_eur: float) -> float:
        """Einsetzbares Kapital: Trading-Balance, gedeckelt durch die Obergrenze.

        Gewinnrücklagen (Auszahlung/Langfrist/Steuer) erhöhen die Obergrenze nie.
        """
        return max(0.0, min(cap_eur, self.balances().trading_balance))
