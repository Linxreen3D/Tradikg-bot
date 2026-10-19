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
Die alte Annahme war damit doppelt so optimistisch wie die öffentliche Grundstufe. Die
Zuordnung Tabelle↔Tab wurde aus der Reihenfolge auf der Seite abgeleitet. Kontospezifische
Gebühren bräuchten einen privaten Schlüssel und wurden nicht abgefragt.

**Entscheidung (17.10.2026, vom Nutzer freigegeben):** Standard überall auf Stufe 1
umgestellt – eine Quelle `costs.fees.KRAKEN_SPOT_TAKER/MAKER` (0,008/0,004) für Research,
Paper, Replay, Scanner und `kraken market-check`. Mechanik-Tests (Ablauf, OCO, Neustart)
nutzen bewusst feste Testgebühren (`tests/helpers.MECHANICS_FEES`), damit sie die Abläufe
und nicht die Gebührenhöhe prüfen; `tests/test_fees.py` sichert die Standardwerte ab.
Beobachtung dabei: Der synthetische Vorteil im E2E-Test überlebt 0,80 % Taker nicht mehr
(keine Trades) – ein Hinweis, wie stark Gebühren kleine Vorteile auffressen.

## Phase D – Erster historischer Test (nur echte Daten, Holdout NICHT ausgewertet)

Alle Läufe: `tradikg replay run --symbols XBTEUR,ETHEUR,SOLEUR --no-holdout --rules kraken`,
Gebühren 0,80 % Taker / 0,40 % Maker, Spread 10 bps, Standardparameter (4 Folds,
Holdout 20 %, 50 Zufallsläufe in der Research, min. 150 Kerzen Historie). **Keine Strategie-
oder Parameteränderung vorgenommen.** Die Walk-Forward-Daten (bis 25.05.2026 bei 1d) sind
durch das Ansehen dieser Ergebnisse nicht mehr unabhängig; der Holdout (1d: 26.05.–16.10.2026)
ist unangetastet.

### Tageskerzen (1d, 720 Kerzen, Bericht `replay-2.md`, Laufzeit ca. 1 min)

| Zeitraum | Bot (100 €) | Buy & Hold | Cash | Trades |
|---|---|---|---|---|
| Walk-Forward 11.08.2025 – 25.05.2026 | 100,00 € | 52,88 € | 100,00 € | 0 |
| 5 rollierende Fenster (je ~4,5 Monate) | 100,00 € in allen | 47,05 – 74,30 € (Median 69,55 €) | 100,00 € | 0 |

- **Was wurde aus 100 €?** 100,00 €, netto 0,00 €, **0 Trades**, Drawdown 0 %. Gleiches Bild
  für 10 €, 10-€-Pilotprofil und 1.000 €.
- **Warum verworfen?** Alle 93 Signale am Netto-EV-Filter: 85× „keine OOS-Belege“, 8×
  „Strategie verworfen“. Die Research hat an keinem der 4 Entscheidungszeitpunkte eine
  Strategie bestätigt (14–20 Hypothesen, 1–7 verworfen).
- **Welche Strategie hat funktioniert?** Keine wurde out-of-sample bestätigt. Ohne Filter
  (alle Strategiesignale): 97,26 € bei 7 Trades (−2,7 %). Zufallseinstiege: 100,82 € bei
  4 Trades (+0,8 %, Zufall).
- **Kosten / Drawdown / Aufteilung:** 0 € Gebühren, 0 % Drawdown, keine realisierten
  Gewinne → nichts auf Trading/Rücklage/Langfrist verteilt.
- **Besser als Buy & Hold?** Ja (100 € vs. 52,88 €) – aber nur, weil der Bot in einem
  fallenden Markt nicht investiert war. Das ist **keine** nachgewiesene Fähigkeit.
- **Besser als Zufall?** Nein (100,00 € vs. 100,82 €).
- **Gebühren ×2 / schlechtere Ausführung / Volatilitätsstress:** jeweils 100,00 €, 0 Trades –
  ohne Trades nicht aussagekräftig.
- **Belastbarkeit:** nicht belastbar (u. a. 0 statt ≥ 30 Trades, nicht besser als Zufall).

### Intraday (1h / 15m / 5m) – Berichte `replay-3.md` bis `replay-5.md`

| Auflösung | Lauf | Laufzeit | Walk-Forward | Bot (100 €) | Buy & Hold | ohne Filter | Zufall |
|---|---|---|---|---|---|---|---|
| 1h (2606 Kerzen) | `--quick` (mit Fenstern) | 84 s | 13.08. – 25.09.2026 | 100,00 € / 0 Trades | 144,17 € | 100,00 € / 0 | 100,00 € / 0 |
| 15m (8832) | `--quick --no-windows` | 153 s | 06.08. – 12.09.2026 | 100,00 € / 0 Trades | 128,26 € | 100,00 € / 0 | 100,00 € / 0 |
| 5m (letzte 8640) | `--quick --no-windows --bars 8640` | 149 s | 12.09. – 24.09.2026 | 100,00 € / 0 Trades | 110,43 € | 100,00 € / 0 | 100,00 € / 0 |

- Research bestätigt an keinem Entscheidungszeitpunkt eine Strategie (je 21 Hypothesen).
- **Selbst die Baselines ohne Netto-EV-Filter und mit Zufallseinstiegen handeln nicht.**
  Ablehnungsgründe (1h, 100 €, ohne Filter): 213× „Netto-EV nach Kosten zu klein“, 176×
  „Chance/Risiko nach Kosten negativ“. Bei 0,80 % je Seite (≥ 1,6 % Round-Trip plus Spread/
  Slippage) sind die Zielabstände (3 ATR) auf Stunden-/Minutenkerzen kleiner als die Kosten.
  Das ist ein **struktureller Befund**: Intraday-Handel mit diesen Strategien ist bei
  Kraken-Stufe-1-Gebühren nicht sinnvoll.
- Buy & Hold war im Quartal Jul–Sep 2026 klar positiv (+10 bis +44 %). Der Bot hat diese
  Bewegung nicht mitgenommen – **schlechter als Buy & Hold**.
- Einschränkung: Intraday-Zeiträume sind kurz (5 Wochen bzw. 12 Tage Bewertung), eine
  Marktphase. Robustheitsvarianten wurden mit `--quick` ausgelassen (ohne Trades ohnehin
  ohne Aussage).

### Gebühren-Empfindlichkeit (Diagnose, keine Entscheidung)

Zusatzlauf mit der **alten** Annahme 0,40 %/0,25 % in einem getrennten Datenordner (der
echte Store blieb unberührt), `--quick --no-windows`:

- **1d:** 3 Trades, **99,12 €** (−0,9 %, Max. DD −0,9 %); ohne Filter 95,82 € (11 Trades),
  Zufall 100,27 €. Research bestätigt auch hier keine Strategie.
- **1h:** weiterhin 0 Trades.

Zweiter Diagnoselauf mit **0,20 % Taker / 0,10 % Maker** (Grundstufe One Trading laut deren
Gebührenseite, 17.10.2026): **1d: 9 Trades, 93,90 € (−6,1 %, Max. DD −6,4 %)**; 1h: 0 Trades.

Auch mit halbierten oder geviertelten Gebühren also kein positives Ergebnis – niedrigere
Gebühren lassen mehr Signale durch, die dann Geld verlieren. Die Strategien selbst haben auf
diesen Daten keinen Vorteil. Gebühren ×2 (1,6 %) im Standardlauf:
0 Trades.

### Belastbarkeits-Checkliste (alle Läufe)

Nicht belastbar: 0 statt ≥ 30 Trades, nicht besser als Zufall (1d), schlechter als Buy & Hold
(Intraday), Holdout nicht ausgewertet. **Negatives Ergebnis:** Auf echten Kraken-Daten hat der
Bot mit den vorhandenen Strategien keinen nachweisbaren Vorteil nach Kosten.

### Vorbereitet: einmaliger Holdout-Lauf (NICHT ausgeführt – wartet auf Freigabe)

```bash
tradikg replay run --symbols XBTEUR,ETHEUR,SOLEUR --timeframe 1d --rules kraken
```

Eingefroren: Standardwerte (`--folds 4 --holdout-frac 0.2 --research-random-runs 50
--min-history-bars 150`), alle Strategien, Gebühren 0,80 %/0,40 %, Spread 10 bps,
1d-Cache mit genau 720 Kerzen bis 16.10.2026 (Holdout 26.05. – 16.10.2026, 144 Kerzen).
**Vor dem Lauf kein `data fetch-kraken --timeframe 1d`**, sonst verschieben sich Plan und
Holdout-Grenzen. Laufzeit ca. 1–2 min. Der Lauf wird je Daten-Fingerabdruck gezählt; jeder
weitere Holdout-Lauf wird als „nicht mehr unangetastet“ markiert.

## Phase E – Dashboard

`scripts/start_dashboard.sh` → http://127.0.0.1:8501 (nur lokal). Alle 7 Tabs laden ohne Fehler
(Server-Log und Browser-Konsole leer). Abdeckung der geforderten Bereiche:

| Gefordert | Tab | Stand mit echten Daten |
|---|---|---|
| Übersicht, Kapitalentwicklung | Übersicht, Analytics (Equity/Drawdown) | 100 € virtuell, noch keine Trades |
| Historical Replay, Strategie-Vergleich | Historical Replay (Lauf-Auswahl, Baselines, Robustheit) | Läufe #1–#5 sichtbar |
| Backtest-Ergebnisse | – | Sprint-1-`tradikg backtest` wird nicht angezeigt (bekannt, nicht nachgebaut) |
| Paper Trading, Trade Journal | Trading (Positionen, Trades, Signale mit Ablehnungsgrund) | Signale ab dem ersten Durchgang |
| Gebührenanalyse | Analytics → Kosten | leer, solange keine Trades |
| ML-Research | AI Research | Research-Lauf #1 (1h) |
| Systemstatus | System Health (Feed, Kill Switch, SAFE_HALT, PID-Sperre, Fehlerprotokoll) | ok |

## Phase F – Autonomes Paper Trading (100 € virtuell, echte Kraken-Daten)

- `tradikg research run --source cache --timeframe 1h --symbols XBTEUR,ETHEUR,SOLEUR` (43 s):
  alle 21 Strategie/Asset-Kombinationen „hypothese“ mit **0 Test-Trades** – bei Kostenfaktor
  ×1 blockiert der Kostenfilter jedes Signal (bei ×0 gäbe es Trades). Der Paper-Bot läuft
  daher passiv und protokolliert nur Signale. Das ist gewollt (kein erzwungener Handel).
- `scripts/start_bot.sh`: läuft, 300 Vorlaufkerzen geladen, Durchgang kurz nach Kerzenschluss.
- **Instanzsperre:** `start_bot.sh` meldet „Läuft bereits“. **Fehler gefunden und behoben:**
  `tradikg paper run --once` nahm die Sperre nicht und lief parallel zum Dienst (Gefahr
  doppelter Verarbeitung). Jetzt abgelehnt mit Exit-Code 1; Test
  `test_paper_run_once_is_refused_while_service_holds_the_lock`.
- **Signale mit Ablehnungsgrund:** erster Durchgang 17:00:20 UTC (Kerze 16:00): 3 Signale
  (mean_reversion XBT/ETH/SOL, Regime „range“), alle abgelehnt: „Netto-EV −0,54 bis −0,62 R nach
  Kosten < 0,02 (prior, keine OOS-Belege)“ – sichtbar in `signals_log` und im Dashboard.
- **Neustart ohne Doppelverarbeitung:** `stop_bot.sh` → `start_bot.sh`: vorher und nachher 302
  verarbeitete Kerzen (alle eindeutig), 3 Signale; geordnetes Beenden im Log, kein erneuter
  Vorlauf.
- **Ohne Claude-Sitzung:** der Dienst läuft per `nohup` unabhängig vom Terminal weiter – aber nur,
  solange der Mac wach ist (siehe Phase G).
- **Backup/Wiederherstellung:** `tradikg backup` (69 Datenbanken, 93 MB) in eine Kopie
  zurückgespielt: `integrity_check` ok für market/research/paper/replays, Daten lesbar.
- Verbindungsausfall (WLAN aus): **nicht geprüft** – braucht deine Zustimmung. Die Logik ist
  durch Tests abgedeckt (Retry, Stale-Erkennung, kein Einstieg auf veralteten Daten).

## Phase G – macOS-Dauerbetrieb

- Neu: `scripts/logs.sh` (`--errors`, `-f`) und `scripts/setup_mac.sh --uninstall-launchd`.
- launchd-Autostart **nicht eingerichtet** – wartet auf deine Zustimmung
  (`scripts/setup_mac.sh --install-launchd`).
- **Schlafmodus (gemessen, nichts geändert):** `pmset -g` zeigt `sleep 1` (Akku: Ruhezustand nach
  1 min), `standby 1`, `powernap 1`. Deckel zu = Ruhezustand = **kein Betrieb**. Nach dem
  Aufwachen erkennt der Dienst den Zeitsprung und holt bis zu 48 Kerzen nach, ohne auf
  veralteten Daten einzusteigen. Für Dauerbetrieb (nur Vorschlag): Netzteil anschließen und
  unter Systemeinstellungen → Batterie → Optionen „Automatischen Ruhezustand bei
  ausgeschaltetem Display verhindern (Netzteil)“ aktivieren; alternativ den Dienst mit
  `caffeinate -i` umhüllen (kein Systemeingriff, wirkt nur solange der Dienst läuft).

## Phase H – Kraken-Zugang (nur erklärt, nichts angelegt)

- Daten, Research, Replay und Paper laufen **ohne Schlüssel** (bestätigt).
- Für einen späteren, separat freizugebenden 10-€-Test – API-Schlüssel nur mit:
  Guthaben abfragen (Query Funds), offene/geschlossene Orders abfragen, Orders erstellen/ändern,
  Orders stornieren. **Zwingend aus:** Auszahlung, Einzahlung, alles Weitere (Ledger, Export,
  Staking, Earn). Das Gate prüft fehlende Auszahlungsrechte per `WithdrawMethods`.
- Ablage nur im macOS-Schlüsselbund:
  `security add-generic-password -s tradikg-kraken -a api-key -w` und `-a api-secret -w`.
- Verbindungstest ohne Order: `tradikg kraken preflight`.
- **OCO erneut geprüft (17.10.2026, offizielle AddOrder-Doku):** weiterhin nur ein
  `close[…]`-Folgeauftrag, kein serverseitiges SL+TP-Bracket. Ergebnis bleibt
  **NOT LIVE READY**. Alternativen mit API-Bracket (Binance OTOCO, Coinbase Advanced) siehe
  `docs/LIVE_READINESS.md` – nicht geprüft für Österreich.

## Verbesserungen 1–3 (17.10.2026, freigegeben)

### 1. Volle Kraken-Historie für 16 MB statt 9,5 GB

`tradikg data fetch-kraken-full --symbols XBTEUR,ETHEUR,SOLEUR --timeframes 1d,4h,1h` liest per
HTTP-Range nur das ZIP-Verzeichnis und die gewünschten CSVs aus den 5 Archivteilen (5 s,
16,2 MB übertragen). Das Archiv reicht bis **30.09.2026**; danach REST-API.

| Asset | 1d | 4h | 1h | ab |
|---|---|---|---|---|
| XBTEUR | 4780 | 28530 | 113581 | 10.09.2013 |
| ETHEUR | 4089 | 24490 | 97089 | 07.08.2015 |
| SOLEUR | 1948 | 11686 | 46723 | 17.06.2021 |

Qualität: keine ungültigen Kerzen/Duplikate; XBTEUR 2013/14 mit vereinzelten Lücken bis 3 Tage
(damals wenig Handel). Phasen 1d XBTEUR: Aufwärts 27 %, Abwärts 16 %, Seitwärts 36 %, hohe Vola 17 %.

### 2. Wochenkennzahlen

Replay-Bericht und Dashboard zeigen „Wochen im Plus“, schlechteste und Median-Woche (Woche
ohne Veränderung zählt nicht als Plus).

### 3. Neue Strategie `regime_breakout` und ehrlicher Test

Trendfolge mit Regimefilter: Long nur, wenn der Schluss über SMA 100/200 liegt und das Hoch der
letzten 20/55 Kerzen überschritten wird; Stop 3 ATR, Ziel 6 ATR, Ausstieg nur über SL/TP (wie
Paper/Live). Parameter vorab aus der Literatur festgelegt, nicht auf den Daten optimiert.

Alle Läufe `--no-holdout --rules kraken --quick --no-windows`, Gebühren 0,80 %/0,40 %.
Walk-Forward 1d: **09.12.2018 – 03.03.2024** (Bullen 2019/2021, Corona-Crash 2020, Bär 2022).

| Lauf | Bot (100 €) | Trades | Max. DD | Wochen im Plus | schlechteste Woche | ohne Filter | Zufall | Buy & Hold |
|---|---|---|---|---|---|---|---|---|
| 1d, alle Strategien (#6) | **126,69 €** | 65 | −7,6 % | 32 % von 274 | −2,9 % | 136,16 € | 105,65 € | 2.954,69 € (schlechteste Woche −44 %) |
| 1d, nur regime_breakout (#7) | **121,04 €** | 40 | −5,5 % | 21 % | −1,8 % | 131,54 € | 105,65 € | 2.954,69 € |
| 4h, alle Strategien (#8, 11 min) | 98,93 € | 75 | −9,3 % | 15 % | −2,6 % | 97,70 € | 109,23 € | 2.327,41 € |

- In #6 trägt `regime_breakout` am meisten bei (32 Trades, 56 % Treffer, +15,12 €).
- Research auf der vollen 1d-Historie (Bericht `research-2.md`, Testzeitraum ≈ letzte 20 % =
  Mitte 2024 – Okt. 2026): **keine Strategie bestätigt**. Gepoolt über alle OOS-Abschnitte ist
  `regime_breakout` die beste Familie (82 Trades, PF 1,22, Ø +0,12 R), aber **nicht signifikant**
  (t = 0,85). `momentum_breakout` ist klar negativ (t = −1,77).

**Bewertung (ehrlich):**
- Erstmals ein positives Walk-Forward-Ergebnis nach allen Kosten, besser als Zufall und Cash, mit
  kleinem Drawdown. Aber: weit hinter Buy & Hold in diesem Bullenmarkt-Zeitraum, der Netto-EV-
  Filter kostet hier Rendite (ohne Filter besser), und statistisch nicht gesichert.
- **„Wöchentlich Plus“ wird nicht erreicht:** 21–32 % der Wochen im Plus, weil der Bot meist in
  Cash wartet (flache Wochen zählen nicht). Dafür ist die schlechteste Woche nur −1,8 bis −2,9 %.
- 4h ist schlechter als 1d: mehr Trades, mehr Gebühren (10,65 €), schlechter als Zufall.
- Transparenz: Die Strategie wurde nach Ansicht der Marktentwicklung 2025/26 entworfen; der
  Research-Test (2024–2026) überlappt den Replay-Holdout (ab 03/2024). Der Holdout ist daher
  **nicht mehr vollständig unabhängig**. Der wirklich unabhängige Test ist der Paper-Betrieb
  ab jetzt.

**Paper-Betrieb umgestellt:** `timeframe = "1d"` in `~/.tradikg/config.toml`, 100 € virtuell; alter
1h-Zustand (0 Trades) nach `~/.tradikg/data/paper-1h-20261017` umbenannt (nicht gelöscht).
Erste Tagesentscheidung 18.10.2026 00:00 UTC.

## Schnelldurchlauf (`tradikg sim`, Dashboard-Tab „Schnelldurchlauf“)

Bot an einem vergangenen Datum einsetzen: Research nur mit Kerzen davor (alle 90 Kerzen
wiederholt), dann Kerze für Kerze bis heute mit der Paper-Engine. Ausgabe: HTML-Seite mit
Kapitalverlauf, jeder Entscheidung und den Research-Ständen (`~/.tradikg/reports/sim-*.html`).

```bash
tradikg sim --start 2025-10-17 --symbols XBTEUR,ETHEUR,SOLEUR   # 1d, ca. 2,5 min
```

Ergebnisse 1d, alle Strategien, Gebühren 0,80 %/0,40 %:

| Start | Bot 100 € | Trades (TP/SL) | Max. DD | Wochen im Plus | 10-€-Pilot | Buy & Hold 100 € |
|---|---|---|---|---|---|---|
| 17.10.2025 | 93,91 € (−6,1 %) | 6 (0/6) | −7,1 % | 15 % | 7,93 € (−20,7 %) | 66,13 € (−33,9 %) |
| 17.10.2024 | 93,70 € (−6,3 %) | 17 (4/13) | −8,1 % | 16 % | 8,99 € (−10,1 %) | 88,95 € (−11,1 %) |
| 01.01.2023 | 97,36 € (−2,6 %) | 37 (14/23) | −8,2 % | 18 % | 10,09 € (+0,9 %) | 543,53 € (+443,5 %) |

Der Bot verliert in allen drei Zeiträumen leicht; er begrenzt Verluste in fallenden Märkten
(besser als Buy & Hold 2025/26), verpasst aber Aufwärtsphasen fast vollständig. Das
10-€-Profil schneidet relativ schlechter ab: Kraken-Mindestorder ≈ 4,35 € bedeutet ~45 % des
Kapitals je Position, dadurch Drawdowns bis −25 %.

## Trendfolge-Allokation (18.10.2026, freigegeben)

`strategy_mode = "trend"` (Paper-Dienst) bzw. `tradikg sim --mode trend` (Standard). Feste Regel,
nicht an Daten angepasst: Kauf, wenn der Tagesschluss über dem SMA 200 liegt; Verkauf darunter;
Notfall-Stop 20 % unter dem höchsten Schluss seit Einstieg (nachgezogen), danach Sperre bis zum
nächsten Kreuzen von unten. Aufträge als Maker-Limit zum letzten Schluss (0,40 %), wenn nicht
erreicht Taker zum nächsten Schluss (0,80 %). BTC/ETH je zur Hälfte, Kraken-Mindestorder beachtet.

Warum: Der Bracket-Bot nutzte im Schnitt nur 6,56 € von 100 € je Trade (1 % Risiko bei weitem
Stop) und verpasste Aufwärtsphasen. Für Live wäre nur **ein** serverseitiger Stop nötig (Kraken
`close[ordertype]=stop-loss`), kein SL+TP-OCO.

Schnelldurchlauf bis 16.10.2026 (BTC+ETH, 1d):

| Start | 100 € → | Max. DD | Trades | 10 € → | Buy & Hold 100 € → (Max. DD) |
|---|---|---|---|---|---|
| 01.01.2020 | 639,21 € | −32,9 % | 49 | 63,92 € | 1.479,55 € (−75,5 %) |
| 01.01.2021 | 332,80 € | −32,9 % | 42 | 33,28 € | 326,61 € (−75,0 %) |
| 01.01.2022 | 222,86 € | −24,7 % | 32 | 26,06 € | 120,44 € (−65,6 %) |
| 01.01.2023 | 273,99 € | −24,7 % | 26 | 27,40 € | 326,15 € (−56,5 %) |
| 17.10.2024 | 142,20 € | −24,7 % | 13 | 13,85 € | 101,32 € (−58,5 %) |
| 17.10.2025 | 97,98 € | −17,4 % | 6 | 11,25 € | 70,33 € (−54,7 %) |

Alle 82 Ein-Jahres-Zeiträume (Start monatlich 01/2019 – 10/2025):

| | im Plus | Median | schlechtester | bester | Ø Max. DD | Monate im Plus |
|---|---|---|---|---|---|---|
| Bot 100 € | **76 %** | +25 % | −30 % (Start 12/2021) | +133 % | −24 % | 37 % |
| Bot 10 € | **83 %** | +22 % | −19 % | +133 % | −21 % | 35 % |
| Buy & Hold | 67 % | +45 % | −70 % | +1000 % | – | – |

Ehrliche Einordnung: deutlich besser als der Bracket-Bot und mit viel kleineren Einbrüchen als
Buy & Hold, aber in starken Bullenmärkten hinter Buy & Hold (nur in 39 % der Jahre besser).
„Wöchentlich im Plus“ ist damit nicht erreichbar (rund ein Drittel der Monate im Plus, der Rest
flach in Cash oder leicht negativ). Die 200-Tage-Regel ist bekannt und wurde vor dem Einbau kurz
auf denselben Daten angesehen – kein Parameter wurde optimiert, aber auch das ist keine
Garantie. Der unabhängige Test ist der Paper-Betrieb ab 18.10.2026. Mit 10 € ist BTC bei hohen
Kursen teils nicht kaufbar (Mindestorder > 5 €), dann hält der Bot nur ETH.

Paper-Betrieb umgestellt: `strategy_mode = "trend"`, `symbols = ["XBTEUR", "ETHEUR"]`, 1d, 100 €
virtuell. Vorheriger 1d-Signalzustand (0 Trades) nach `paper-1d-signals-20261018` umbenannt.

## Ziel „≥ 70 % im Plus, minimales Verlustrisiko“ – Analyse (18.10.2026)

**Zerlegung der Monate (Trendfolge 100 %, 2019–2026):** 28 % reine Cash-Monate (Rendite exakt 0,
zählen nie als Plus); in investierten Monaten nur 51 % im Plus. Monate im Plus gesamt 37 %.

**Studie mit Trainings-/Testtrennung** (vorab festgelegte 8 Varianten, Auswahl nur auf 2016–2022,
einmalige Prüfung auf 2023–2026; Code nicht übernommen, nur Ergebnis):
- Ohne Cash-Zins erreicht keine Variante mehr als 46 % Plus-Monate. Mit 2 % Zins p.a. 66–74 %.
- Volatilitätssteuerung senkte im Training den Max. DD von −63…−74 % auf −23…−31 %. Ausgewählt
  wurde „SMA 200 monatlich + Vola-Ziel 25 %“ (78 % Plus-Monate, Max. DD −23 % im Training).
- **Test 2023–2026: durchgefallen** – 62 % Plus-Monate (mit Zins), nur 38 % der 1-Jahres-Zeiträume im
  Plus, ≈ 4 % p.a. Der bestehende SMA-200-Bot war im selben Test besser (68 %, +26 % p.a.).
  Deshalb nicht übernommen.

**Übernommen: Risikoregler `invest_fraction`** (lineare Skalierung, keine Datenanpassung) und
optionaler `cash_yield_pa` (nur setzen, wenn Zins real gezahlt wird). 82 Ein-Jahres-Zeiträume
(Start monatlich 01/2019 – 10/2025), 100 €, ohne Zins:

| investiert max | Jahre im Plus | Median-Jahr | schlechtestes Jahr | Ø Max. DD (schlimmster) | schlechtester Monat |
|---|---|---|---|---|---|
| 100 % | 76 % | +25 % | −30 % | −24 % (−38 %) | −16 % |
| 75 % | 76 % | +19 % | −23 % | −19 % (−31 %) | −13 % |
| **50 %** | **79 %** | **+13 %** | **−16 %** | **−14 % (−24 %)** | **−9 %** |
| 30 % | 79 % | +8 % | −10 % | −9 % (−16 %) | −6 % |

Monate im Plus bleiben bei ≈ 37 % (mit 2 % Cash-Zins ≈ 62 %) – das Ziel „70 % der *Monate*“ ist mit
ehrlichen Mitteln nicht erreichbar; „70 % der *12-Monats-Zeiträume*“ ist erreicht.

Schnelldurchlauf mit 50 % bis 16.10.2026: ab 01.12.2021 (schlechtester Startpunkt) 150,00 €
(Max. DD −18,5 %; Buy & Hold 96,81 €, −71,7 %), ab 2022 160,73 €, ab 2023 177,83 €, ab 17.10.2024
120,53 €, ab 17.10.2025 99,28 € (−0,7 %, Max. DD −9,0 %; Buy & Hold 70,33 €).
**10 € bei 50 %:** Anteil 2,50 € liegt unter der Kraken-Mindestorder → kaum handelbar; ein 10-€-Test
braucht `invest_fraction = 1.0`.

Gefundener Fehler: `TrendPaperEngine` übernahm die konfigurierte `TrendConfig` nicht (Standard
100 %). Behoben, Test `test_trend_mode_uses_configured_invest_fraction`.

Paper-Betrieb: `invest_fraction = 0.5`, 100 € virtuell, BTC/ETH, 1d (frischer Zustand ab 18.10.2026).

## Grid-Daytrading (18.10.2026, auf Wunsch, nur Paper)

Netz 3 % (5 Stufen je Coin, Maker-Limits), Notbremse −10 % (Taker), neue Käufe nur bei
Tagesschluss über SMA 200. Einstellungen auf 2019–2022 gewählt (alle Varianten 1–3 % Abstand,
mit/ohne Filter verloren dort 32–83 %). Schnelldurchlauf mit realistischer Füllung (kein Rückkauf
derselben Stufe in derselben Stunde):

| Start | Grid 100 € → | Trades | Max. DD | Buy & Hold |
|---|---|---|---|---|
| 17.10.2025 | 102,05 € (+2,1 %) | 91 | −8,5 % | 69,90 € |
| 01.01.2023 | 84,45 € (−15,5 %) | 522 | −25,6 % | 324,41 € |

Kein belegter Vorteil; die Trendfolge war im selben Zeitraum klar besser. Paper-Betrieb läuft seit
18.10.2026 01:48 UTC im Grid-Modus (1h, 100 € virtuell). Vorheriger Trend-Zustand:
`paper-trend50-20261018`. Echtgeld-Modul (`tradikg live`) bleibt Trendfolge, nicht gestartet.
