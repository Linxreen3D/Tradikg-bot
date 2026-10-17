# Historical Market Replay mit Walk-Forward-Tests

Stand: 16.10.2026 · Modul `src/tradikg_bot/replay/` · nur Simulation, **keine Orders**

Ziel: ehrlich zeigen, was aus 10 €, 100 € oder 1.000 € in verschiedenen historischen
Zeiträumen geworden wäre – mit derselben Logik wie im Paper-Dauerbetrieb und ohne den
Bot auf den Daten zu bewerten, auf denen er trainiert oder optimiert wurde.

## 1. Daten beschaffen (kostenlos)

| Quelle | Abdeckung | Befehl | Hinweis |
|---|---|---|---|
| Kraken-REST `OHLC` | höchstens **720 Kerzen** je Intervall: 1d ≈ 2 Jahre, 4h ≈ 120 Tage, 1h ≈ 30 Tage | `tradikg data fetch-kraken --timeframe 1d` | offiziell, ohne Schlüssel; ältere Kerzen liefert die API nicht |
| Kraken „Downloadable historical OHLCVT“ | vollständige Historie je Paar, 1m–1d | `tradikg data import-kraken <ZIP oder Ordner> --timeframe 1d` | offiziell, Download manuell im Kraken-Support-Center (ZIP); CSV ohne Kopfzeile: `timestamp, open, high, low, close, volume, trades`; Intervalle ohne Handel fehlen |
| ccxt (z. B. Binance) | lange Historie über Paginierung | `tradikg data fetch-ccxt --symbol BTC/EUR --as XBTEUR` | **andere Börse** – Preise/Volumen weichen ab; Quelle wird im Bericht ausgewiesen (`pip install -e ".[data]"`) |

`tradikg data coverage --timeframe 1d` zeigt je Asset Zeitraum, Lücken, Gesamtrendite
und den Anteil der Marktphasen (Aufwärts, Abwärts, Seitwärts, hohe Volatilität). Für
aussagekräftige Ergebnisse sollten mehrere Phasen enthalten sein – 2 Jahre Tageskerzen
aus der Kraken-API sind das Minimum, der OHLCVT-Download ist besser.

In der Entwicklungsumgebung ist das Netz zu Kraken und Binance gesperrt. Dort läuft das
Replay mit **synthetischen** Daten mit eingebauten Marktphasen (Aufwärts, Seitwärts,
Abwärts, Crash, Erholung). Solche Läufe sind im Bericht und im Dashboard immer als
synthetisch markiert und sagen nichts über echte Märkte aus.

## 2. Ausführen

```bash
tradikg data fetch-kraken --timeframe 1d              # oder import-kraken (empfohlen)
tradikg data coverage --timeframe 1d
tradikg replay run --no-holdout                        # während der Entwicklung
tradikg replay run --rules kraken                      # einmalig mit Holdout, echte Mindestgrößen
tradikg replay report                                  # letzter Bericht (Markdown)
```

Dauer auf synthetischen Daten (3 Assets, 730 Tageskerzen, alle Varianten): rund 4
Minuten. `--quick` lässt die Robustheitsvarianten weg, `--no-windows` die Fenster.
Der Bericht liegt unter `~/.tradikg/reports/replay-<Nr>.md`, alle Zahlen in
`~/.tradikg/data/replays.sqlite`, der Dashboard-Bereich heißt **Historical Replay**.

## 3. Zeitliche Trennung – nie auf Bewertungsdaten trainieren

```
|-- nur Research --|- Fold 1 -|- Fold 2 -|- Fold 3 -|- Fold 4 -|--- Holdout ---|
0               Cutoff 1   Cutoff 2   Cutoff 3   Cutoff 4   Holdout-Start    Ende
```

- **Entscheidungszeitpunkte (Cutoffs):** Ab 40 % der Daten, dann alle Folds. An jedem
  Cutoff läuft die Research-Engine nur auf Kerzen **vor** dem Cutoff. Darin trennt sie
  selbst zeitlich **Train (60 %) / Validation (20 %) / Test (20 %)** und
  Walk-Forward. Parameterwahl und OOS-Statistiken steuern den Bot bis zum nächsten
  Cutoff.
- **Bewertung:** nur auf Kerzen nach dem jeweiligen Cutoff.
- **Holdout (letzte 20 %):** fließt in keine Auswahl ein. Seine Research nutzt nur
  Daten davor. Jede Auswertung wird je Daten-Fingerabdruck gezählt; ab der zweiten
  warnen CLI, Bericht und Dashboard, dass er nicht mehr unangetastet ist. Mit
  `--no-holdout` lässt er sich während der Entwicklung aussparen.
- **Nur bekannte Daten:** Signale werden je Abschnitt nur aus Kerzen bis zum
  Abschnittsende berechnet; alle Strategien und Features sind kausal geprüft.
  Ein Test verändert die Zukunft und prüft, dass alle früheren Entscheidungen, Trades
  und Equity-Werte gleich bleiben (`tests/test_replay.py`).

## 4. Gleiche Logik wie im Paper-Betrieb

Das Replay nutzt dieselbe `PaperTradingEngine`: Signal zur Schlusskerze, Ausführung
zur Eröffnung der Folgekerze, Netto-EV-Filter mit OOS-Wahrscheinlichkeiten,
Positionsgröße über das Risiko je Trade, Mindestvolumen/-wert und Tick-Raster,
Gebühren (0,80 % Taker / 0,40 % Maker, Kraken-Pro-Stufe 1, Stand 17.10.2026), Spread (10 bps), Einstiegs- und
Stop-Slippage, SL/TP je Kerze, Cooldowns, Tagesverlust-Abschaltschwelle und Ledger.

**SL und TP in derselben Kerze:** Die Reihenfolge ist aus OHLC nicht bestimmbar. Der
Exit wird als „Reihenfolge unbekannt“ markiert und konservativ als Stop gewertet.
Die Variante „TP zuerst“ läuft nur als gekennzeichnete Obergrenze. Es wird keine
Tick-Reihenfolge erfunden.

## 5. Szenarien

| Bereich | Inhalt |
|---|---|
| Kapital | 10 € (1 % Risiko je Trade), 10 € Pilotprofil (max. 0,50 € Risiko je Trade), 100 €, 1.000 € |
| Gewinnverteilung | je Szenario: Trading-Balance, Auszahlungsreserve, Langfrist, offene Verluste (erst Verlustaufholung, dann 33/33/34) |
| Zeiträume | Walk-Forward (Entwicklungszeitraum), Holdout, rollierende Fenster mit frischem Kapital |
| Baselines | Buy & Hold gleichgewichtet (unter Beachtung der Mindestorder), Buy & Hold erstes Asset, Cash, Zufallseinstiege, Strategiesignale ohne Netto-EV-Filter |
| Robustheit | Gebühren ×2; schlechtere Ausführung (Spread 30 statt 10 bps, Stop-Slippage 100 bps – der Bot kalkuliert weiter mit 10 bps); ungewöhnliche Volatilität (Dochte ×1,5, zufällige Kurslücken ±3–6 typische Kerzen, nur ab Bewertungsbeginn); TP-zuerst-Obergrenze |

## 6. Ergebnis lesen

Der Bericht beginnt mit **„Was wäre aus 100 € geworden?“** für jeden Zeitraum, zeigt
danach die **Verteilung über alle Fenster** (Minimum, 10 %, Median, 90 %, Maximum,
Anteil der Verlustfenster) und endet mit einer strengen **Belastbarkeits-Checkliste**
(echte Daten, Holdout unangetastet und positiv, besser als Buy & Hold, Fenster-Median,
Verlustanteil, ≥ 30 Trades, positiv bei Gebühren ×2, schlechterer Ausführung und
Volatilitätsstress, besser als Zufall und als „ohne Filter“). „Belastbar“ nur, wenn
**alle** Punkte erfüllt sind – auch dann ist es keine Renditezusage und keine
Freigabe für Echtgeld.

„Keine Trades“ ist ein gültiges Ergebnis: Ohne Out-of-Sample-Belege handelt der Bot
nicht. Bei 10 € scheitern viele Signale zusätzlich an den Mindestordergrößen – der
Bericht listet die häufigsten Ablehnungsgründe.

## 7. Grenzen

- Kerzendaten: keine Orderbuchtiefe, kein Tick-Verlauf; Fills sind modelliert.
- Heutige Mindestgrößen werden auf historische Preise angewendet (`--rules kraken`
  lädt die aktuellen Kraken-Werte, sonst Beispielwerte).
- Gebühren sind Annahmen (Kraken-Grundstufe), nicht kontospezifisch verifiziert.
- Die Research-Engine wird im Replay mit weniger Zufalls-Baseline-Läufen (50 statt
  200) ausgeführt, um die Laufzeit zu begrenzen.
- Volatilitätsstress verändert nur die Bewertungsdaten; die Research kennt die
  Originalhistorie (der Bot konnte die Zukunft nicht vorhersehen).
- Synthetische Ergebnisse prüfen nur die Pipeline.
