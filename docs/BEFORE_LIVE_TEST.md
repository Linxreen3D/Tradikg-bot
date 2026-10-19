# Abschlussbericht Mac-Test und was vor dem 10-€-Echtgeldtest fehlt

Stand 17.10.2026 · Branch `mac/first-real-test` · Details: [MAC_FIRST_RUN_REPORT.md](MAC_FIRST_RUN_REPORT.md)

> **Ergebnis: NOT LIVE READY.** Auf echten Kraken-Daten hat keine Strategie nach Kosten einen
> Vorteil gezeigt. Kein Echtgeldtest, keine echten Orders, kein API-Schlüssel angelegt.

## 1. Was auf dem Mac installiert und getestet wurde

- MacBook Air M2, macOS 27.0.1, Python 3.12.14, venv `.venv` mit Extras ml/dashboard/dev.
- `scripts/run_tests.sh`: 314 bestanden, 4 übersprungen (Opt-in-Livetests; separat mit
  `KRAKEN_PUBLIC_INTEGRATION=1` ausgeführt: 4 bestanden). Lint/Format sauber.
- `tradikg doctor --network`: alle Prüfungen OK.
- Tatsächlich ausgeführt: Datenimport, Qualitätsbericht, 5 Replays + 2 Gebühren-Diagnosen,
  Research 1h, Dashboard (alle Tabs), Paper-Dienst mit Instanzsperre, Neustart, Backup und
  Wiederherstellung in einer Kopie, E2E-Test.
- **Nicht** ausgeführt: Holdout-Lauf, launchd-Autostart, WLAN-Ausfalltest (jeweils ohne Freigabe).

## 2. Datenquellen

| Quelle | Auflösung | Kerzen je Asset (XBT/ETH/SOL) | Zeitraum |
|---|---|---|---|
| Kraken REST `OHLC` | 1d | 720 | 27.10.2024 – 16.10.2026 |
| Kraken OHLCVT `2026Q3.zip` + REST | 1h | 2606 (läuft im Paper-Betrieb weiter) | 01.07. – 17.10.2026 |
| Kraken OHLCVT `2026Q3.zip` | 15m | 8832 | 01.07. – 30.09.2026 |
| Kraken OHLCVT `2026Q3.zip` | 5m | 26492 / 26490 / 26489 | 01.07. – 30.09.2026 |

Qualität: keine ungültigen Kerzen, keine Duplikate, UTC; 5m mit 3–7 Lücken ≤ 15 min.

## 3. Backtest / Replay starten

```bash
source .venv/bin/activate
tradikg data quality --timeframes 1d,1h,15m,5m
tradikg replay run --symbols XBTEUR,ETHEUR,SOLEUR --timeframe 1d --no-holdout --rules kraken
tradikg replay run --symbols XBTEUR,ETHEUR,SOLEUR --timeframe 1h --no-holdout --rules kraken --quick
tradikg replay report
```

## 4. Dashboard

`scripts/start_dashboard.sh` → http://127.0.0.1:8501 (nur lokal).

## 5. Paper Trading starten und stoppen

```bash
scripts/start_bot.sh      # 100 € virtuell, echte Kraken-Daten, 1h
scripts/status_bot.sh
scripts/logs.sh --errors
scripts/stop_bot.sh
```

Autostart (erst nach Freigabe): `scripts/setup_mac.sh --install-launchd`, entfernen mit
`--uninstall-launchd`. Läuft nur, solange der Mac wach ist.

## 6. Ergebnisse auf echten historischen Daten (inkl. negativer)

| Auflösung | Bot (100 €) | Buy & Hold | ohne Filter | Zufall |
|---|---|---|---|---|
| 1d (WF 08/2025–05/2026) | 100,00 € / 0 Trades | 52,88 € | 97,26 € / 7 | 100,82 € / 4 |
| 1h (WF 08–09/2026) | 100,00 € / 0 | 144,17 € | 100,00 € / 0 | 100,00 € / 0 |
| 15m | 100,00 € / 0 | 128,26 € | 100,00 € / 0 | 100,00 € / 0 |
| 5m | 100,00 € / 0 | 110,43 € | 100,00 € / 0 | 100,00 € / 0 |

Gebühren-Diagnose 1d (gleiche Strategien, nur andere Gebühren): 0,80 % → 0 Trades; 0,40 % →
99,12 € (3 Trades); 0,20 % → **93,90 € (9 Trades, −6,1 %)**. Niedrigere Gebühren erzeugen mehr
Trades, aber Verluste: **die Strategien selbst haben keinen Vorteil**.

## 7. Bestehende Einschränkungen

- Intraday-Historie nur ein Quartal (Speicherplatz: 12 GB frei, Komplett-Historie ≈ 10 GB).
- Gebühren nicht kontospezifisch verifiziert; heutige Mindestgrößen auf historische Preise.
- Kerzendaten ohne Orderbuch; Fills modelliert.
- Mac schläft auf Akku nach 1 min, Deckel zu = kein Betrieb.
- Sprint-1-Backtests nicht im Dashboard.

## 8. Was vor dem 10-€-Echtgeldtest fehlt

1. **Ein nachgewiesener Vorteil nach Kosten** – auf Walk-Forward *und* einmaligem Holdout,
   ≥ 30 Trades, besser als Zufall und Buy & Hold, stabil bei Gebühren ×2. Aktuell: keiner.
2. **Börse mit serverseitigem SL/TP-Bracket per API.** Kraken Spot: nein (geprüft 17.10.2026).
   Alternativen (Binance OTOCO, Coinbase Advanced) für Österreich/MiCA ungeprüft.
3. Mehrwöchiger Paper-Betrieb ohne Fehler, mit Trades, deren Ergebnis zum Replay passt.
4. Praktischer Verbindungsausfall-Test (WLAN) und launchd-Dauerbetrieb mit Netzteil.
5. API-Schlüssel nur mit Abfrage-/Order-Rechten (ohne Auszahlung) im Schlüsselbund,
   `tradikg kraken preflight` ohne Order.
6. Ausdrückliche Einzelfreigabe für genau **einen** Trade.
