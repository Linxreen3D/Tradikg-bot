"""Freigabe-Gate für Echtgeldorders.

Echtgeldhandel ist standardmäßig gesperrt. Eine Order an eine Live-Börse ist nur
möglich, wenn **alle** Bedingungen erfüllt sind:

1. Kommandozeilen-Flag ``--i-understand-this-uses-real-money``
2. Umgebungsvariable ``TRADIKG_LIVE_APPROVAL`` mit exakt dem Freigabetext für
   **heute (UTC)**, die Obergrenze und das Paar, z. B.
   ``ECHTGELD-FREIGABE 2026-10-17 MAX 10.00 EUR XBTEUR``.
   Die Freigabe läuft um Mitternacht UTC ab und muss täglich neu erteilt werden.
3. Die Börse bietet über die API einen vollständigen SL/TP-Bracket (OCO) an.
4. Der API-Key hat **keine** Auszahlungsberechtigung.
5. Es existieren keine Margin-Positionen.

Bedingungen 3 bis 5 werden vom Gate selbst geprüft und können nicht per
Konfiguration übersprungen werden.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from tradikg_bot.live.gateway import ExchangeGateway

APPROVAL_ENV = "TRADIKG_LIVE_APPROVAL"


class LiveTradingBlockedError(RuntimeError):
    def __init__(self, reasons: list[str]):
        super().__init__("Echtgeldhandel gesperrt:\n- " + "\n- ".join(reasons))
        self.reasons = reasons


def approval_phrase(day: dt.date, cap_eur: float, pair: str) -> str:
    return f"ECHTGELD-FREIGABE {day.isoformat()} MAX {cap_eur:.2f} EUR {pair}"


@dataclass(frozen=True)
class LiveApproval:
    cli_flag: bool
    phrase: str | None

    @classmethod
    def from_env(cls, cli_flag: bool, env: dict[str, str] | None = None) -> LiveApproval:
        env = dict(os.environ) if env is None else env
        return cls(cli_flag=cli_flag, phrase=env.get(APPROVAL_ENV))


def check_live_gate(
    gateway: ExchangeGateway,
    approval: LiveApproval | None,
    pair: str,
    cap_eur: float,
    today: dt.date | None = None,
) -> None:
    """Wirft ``LiveTradingBlockedError`` mit allen Gründen, sonst kein Rückgabewert."""
    if not gateway.is_live:
        return
    today = today or dt.datetime.now(dt.UTC).date()
    reasons: list[str] = []
    expected = approval_phrase(today, cap_eur, pair)
    if approval is None or not approval.cli_flag:
        reasons.append("Flag --i-understand-this-uses-real-money fehlt")
    if approval is None or approval.phrase != expected:
        reasons.append(f"{APPROVAL_ENV} fehlt oder passt nicht (erwartet: '{expected}')")
    caps = gateway.capabilities
    if not caps.native_bracket:
        reasons.append(
            "Kein börsenseitiger SL/TP-Bracket (OCO) über die API verfügbar: " + caps.evidence
        )
    try:
        if gateway.has_withdrawal_permission():
            reasons.append("API-Key hat Auszahlungsberechtigung – Key ohne 'Withdraw' anlegen")
    except Exception as exc:  # unklar = gesperrt
        reasons.append(f"Auszahlungsberechtigung nicht prüfbar: {exc}")
    try:
        if gateway.has_margin_positions():
            reasons.append("Offene Margin-Positionen vorhanden")
    except Exception as exc:
        reasons.append(f"Margin-Positionen nicht prüfbar: {exc}")
    if reasons:
        raise LiveTradingBlockedError(reasons)
