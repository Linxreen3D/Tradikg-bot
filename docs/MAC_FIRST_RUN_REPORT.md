# Erster Echtdaten-Test auf dem Mac

Stand 17.10.2026, MacBook Air M2, macOS 27.0.1, Python 3.12.14, Branch `mac/first-real-test`
(von `integration/mac-test` @ 749b7eb). Nur öffentliche Kraken-Schnittstellen, kein API-Schlüssel.

## Phase A/B – Installation

- `scripts/setup_mac.sh` (venv `.venv`, Extras ml/dashboard/dev), `tradikg init`: ok.
- `scripts/run_tests.sh`: 309 bestanden, 4 übersprungen (Opt-in Kraken-Live). Der
  shellcheck-Test wurde vorher übersprungen, weil `.venv/bin` nicht im PATH war – behoben.
- `KRAKEN_PUBLIC_INTEGRATION=1 pytest tests/live/test_kraken_public_live.py`: 4 bestanden.
- `tradikg doctor --network`: alle Prüfungen OK (Modus paper, Kill Switch aus, Kraken erreichbar).

## Phase C – Echte historische Daten

Quellen:

- **1d**: Kraken-REST `OHLC` (`tradikg data fetch-kraken`), 720 Kerzen je Asset.
- **1h/15m/5m**: offizielle Quartalsdatei `Kraken_OHLCVT_2026Q3.zip` (576 MB, Stand
  06.10.2026) von der [Kraken-Hilfeseite](https://support.kraken.com/hc/en-us/articles/360047124832).
  Die Komplett-Historie (5 Teile à ca. 2 GB, bis 30.06.2026) wurde wegen Speicherplatz nicht
  geladen. Paarnamen im ZIP entsprechen den Projektnamen (`XBTEUR_60.csv` usw.).
- **1h** zusätzlich per REST bis heute ergänzt (überlappt mit dem Quartal → keine Lücke).
  **15m/5m** bewusst nur aus dem ZIP: die REST-Historie (720 Kerzen = 7,5 bzw. 2,5 Tage)
  würde eine Lücke 01.10.–09.10. erzeugen. Daher kein `--start/--end` nötig, solange 15m/5m
  nicht per REST nachgeladen werden.

| Asset | Auflösung | Kerzen | Zeitraum (UTC) | Quelle | Fehlend | Ungültig/Duplikate |
|---|---|---|---|---|---|---|
| XBTEUR | 1d | 720 | 2024-10-27 – 2026-10-16 | kraken (REST) | 0 | 0 / 0 |
| ETHEUR | 1d | 720 | 2024-10-27 – 2026-10-16 | kraken (REST) | 0 | 0 / 0 |
| SOLEUR | 1d | 720 | 2024-10-27 – 2026-10-16 | kraken (REST) | 0 | 0 / 0 |
| XBTEUR | 1h | 2606 | 2026-07-01 – 2026-10-17 13:00 | ZIP + REST | 0 | 0 / 0 |
| ETHEUR | 1h | 2606 | 2026-07-01 – 2026-10-17 13:00 | ZIP + REST | 0 | 0 / 0 |
| SOLEUR | 1h | 2606 | 2026-07-01 – 2026-10-17 13:00 | ZIP + REST | 0 | 0 / 0 |
| XBTEUR | 15m | 8832 | 2026-07-01 – 2026-09-30 | ZIP | 0 | 0 / 0 |
| ETHEUR | 15m | 8832 | 2026-07-01 – 2026-09-30 | ZIP | 0 | 0 / 0 |
| SOLEUR | 15m | 8832 | 2026-07-01 – 2026-09-30 | ZIP | 0 | 0 / 0 |
| XBTEUR | 5m | 26492 | 2026-07-01 – 2026-09-30 | ZIP | 4 (3 Lücken, max 15 min) | 0 / 0 |
| ETHEUR | 5m | 26490 | 2026-07-01 – 2026-09-30 | ZIP | 6 (5 Lücken, max 15 min) | 0 / 0 |
| SOLEUR | 5m | 26489 | 2026-07-01 – 2026-09-30 | ZIP | 7 (6 Lücken, max 15 min) | 0 / 0 |

Qualitätsbericht: `tradikg data quality --timeframes 1d,1h,15m,5m` → `~/.tradikg/reports/
data-quality-*.md/.json`. Alle Zeitstempel UTC, keine offene letzte Kerze im Cache. Ausreißer
(robuster z-Wert > 8; 1h: 6–13, 5m: 67–94 je Asset) sind echte Marktbewegungen von 2–6 %
(z. B. 19.08.2026 15:00 UTC gleichzeitig in XBT und ETH), keine Datenfehler – sie bleiben drin.
Lücken in 5m = Intervalle ohne Handel (Kraken liefert dort keine Kerze).

**Einschränkung:** Intraday deckt nur ~3,5 Monate (ein Quartal) ab – wenige Marktphasen,
statistisch dünn. 1d deckt 2 Jahre ab.

### Handelsregeln (Kraken AssetPairs/Ticker, öffentlich, 17.10.2026 ~14:20 UTC)

| Paar | Mindestmenge | ≈ EUR | Tick | Spread | 24h-Volumen |
|---|---|---|---|---|---|
| XBTEUR | 0,00006 BTC | 4,35 | 0,1 | 0,0 bps | ≈ 14,8 Mio. EUR |
| ETHEUR | 0,002 ETH | 4,31 | 0,01 | 0,6 bps | ≈ 10,9 Mio. EUR |
| SOLEUR | 0,05 SOL | 4,63 | 0,01 | 1,1 bps | ≈ 3,8 Mio. EUR |

Die heutigen Mindestgrößen werden im Replay (`--rules kraken`) auf historische Preise
angewendet – eine Näherung.

### Gebühren – Abweichung von der Annahme

Projektannahme: **0,40 % Taker / 0,25 % Maker**. Kraken-Gebührenseite
(kraken.com/features/fee-schedule, Kraken Pro → plattformübergreifende Stufen → Spot-Krypto,
abgerufen 17.10.2026): Stufe 1 (30-Tage-Volumen < 2.500 USD) **0,80 % Taker / 0,40 % Maker**.
Die Annahme ist damit doppelt so optimistisch wie die öffentliche Grundstufe. Die Zuordnung
Tabelle↔Tab wurde aus der Reihenfolge auf der Seite abgeleitet. Kontospezifische Gebühren
bräuchten einen privaten Schlüssel und wurden nicht abgefragt. Das Replay rechnet eine
Variante „Gebühren ×2“ mit, die der Grundstufe entspricht.
