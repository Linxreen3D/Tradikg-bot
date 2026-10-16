# Roadmap

Stand: 16.10.2026. Jede Phase braucht eine **eigene, ausdrückliche Freigabe**. Eine
Freigabe gilt nur für die Phase und den Umfang, für den sie erteilt wurde.

| Phase | Inhalt | Status |
|---|---|---|
| 1 | Backtesting-Fundament (Daten, Kosten, Baseline, Engine) | umgesetzt (PR #1) |
| 2 | Kraken-Spot-Execution-Komponente, fail-closed | umgesetzt, **NOT LIVE READY** (PR #2) |
| 2.5 | Research-, ML- und autonome **Paper**-Plattform, Dashboard, macOS-Betrieb | umgesetzt (PR #3) |
| **2.6** | **Historical Market Replay mit Walk-Forward-Tests** (vor weiteren ML-Modellen) | Pipeline umgesetzt; Lauf auf echten Daten steht aus (Mac) |
| 3 | Einzelner, manuell überwachter Echtgeld-Testtrade (max. 10 €) | blockiert: kein API-Bracket bei Kraken Spot |
| 4 | Autonomer Handelskreislauf **mit Echtgeld** | Logik im Paper-Modus umgesetzt; Echtgeld **nicht freigegeben, nicht aktivierbar** |
| 5 | Langfrist-Investments mit echten Käufen | Grundgerüst (Datenmodelle, Analysen); Käufe nicht implementiert |

## Grundsatz: Freigabestufen

| Stufe | Was erlaubt ist | Wie freigegeben | Im Code |
|---|---|---|---|
| Paper | Simulation inkl. autonomem Kreislauf | ohne Freigabe | vorhanden |
| **Einzeltrade** | **genau eine** Echtgeldorder mit Absicherung | CLI-Flag + `ECHTGELD-FREIGABE EINZELTRADE <Datum> MAX 10.00 EUR <Paar>` | vorhanden, wird beim Senden verbraucht; derzeit durch Kraken-Blocker gesperrt |
| **Autonom live** | Bot eröffnet selbstständig echte Positionen | eigene, befristete Autonomie-Freigabe | **existiert nicht** – der Paper-Kreislauf lehnt Live-Börsen ab (`AutonomyNotAuthorizedError`) |

Eine Einzeltrade-Freigabe wird nie als Erlaubnis für autonomen Handel gewertet.

## Was in Phase 2.5 umgesetzt wurde

Die Anforderungen vom 16.10.2026 für den autonomen Kreislauf sind **im Paper-Modus**
umgesetzt:

1. Netto-Ergebnis je Position nach allen Gebühren aus den Ausführungsdaten.
2. Gewinnaufteilung 33/33/34 erst nach Aufholung früherer Verluste (High-Water-Mark),
   optionale Steuerrücklage (Satz konfigurierbar, Standard 0, keine Steuerberatung).
3. Kapital-Recycling: Die Trading-Balance steht neuen Signalen sofort zur Verfügung,
   gedeckelt durch die Kapitalobergrenze; Rücklagen erhöhen sie nie.
4. Neue Positionen nur bei positivem Netto-Erwartungswert (aus OOS-Belegen),
   ausreichendem Kapital und eingehaltenen Risikolimits.
5. Jede Position mit Stop-Loss und Take-Profit (Paper: simulierter Broker-Bracket).

Außerdem: Market-Data-Engine, Scanner, sechs Strategiefamilien, Research mit
Walk-Forward und Baselines, Regime-Erkennung, ML mit Champion/Challenger,
Nachanalyse und Tagesberichte, Dashboard, macOS-Skripte und launchd.

## Phase 2.6: Historical Replay (Priorität vor weiterem ML)

Eine lauffähige Replay-Pipeline hat Vorrang vor zusätzlichen, komplexeren ML-Modellen:
Erst wenn sich ein Ergebnis über mehrere historische Zeiträume, einen unangetasteten
Holdout und Stresstests hält, lohnt sich weitere Modellierung. Details:
[`HISTORICAL_REPLAY.md`](HISTORICAL_REPLAY.md).

- [x] Datenimport: Kraken-OHLC (720 Kerzen), Kraken-OHLCVT-Download, ccxt (gekennzeichnet)
- [x] Replay nur mit bis dahin bekannten Daten, gleiche Paper-Engine
- [x] Walk-Forward mit Research je Cutoff (Train/Validation/Test), unangetasteter Holdout
- [x] 10 €, 10 € Pilotprofil, 100 €, 1.000 €; Ledger 33/33/34 je Szenario
- [x] Baselines und Robustheit (Gebühren ×2, Ausführung, Volatilität, TP-zuerst-Obergrenze)
- [x] Verteilung über rollierende Fenster inkl. negativer Ergebnisse; Dashboard-Bereich
- [ ] Auf dem Mac mit echten Kraken-Daten (OHLCVT-Download, mehrere Jahre) ausführen
- [ ] Erst danach: weitere ML-Modelle, nur wenn sie im Replay belastbar besser sind

## Voraussetzungen für Phase 3 (Einzeltrade live)

- [ ] Broker mit dokumentiertem, börsenseitigem SL/TP-Bracket (OCO) über die API.
      Kraken Spot: nein. Kandidaten zur Prüfung: Binance Spot (OTOCO), Coinbase
      Advanced Trade (angehängter TP/SL-Bracket). Regulierung/Verfügbarkeit in
      Österreich prüfen; keine Konten automatisch anlegen.
- [ ] Gateway für diesen Broker gegen die offizielle Dokumentation implementiert und
      mit `validate`-/Test-Endpunkten geprüft.
- [ ] Mindestgrößen und tatsächliche Gebühren live abgefragt.
- [ ] Manuelle Abnahme nach `LIVE_READINESS.md`.
- [ ] Deine ausdrückliche Freigabe.

## Voraussetzungen für Phase 4 (autonom live)

- [ ] Phase 3 erfolgreich: mehrere Einzeltrades mit bestätigtem Schutz, sauberem
      Abgleich und korrekter Netto-Abrechnung.
- [ ] Historical Replay auf echten Daten mit Gesamturteil „belastbar“ (Holdout erst danach
      einmal ausgewertet).
- [ ] Mindestens eine Strategie mit Urteil **„bestätigt“** auf echten Marktdaten
      (nicht synthetisch), anschließend mehrere Wochen Paper-Betrieb mit Live-Daten
      ohne ungeklärten SAFE_HALT und mit plausibler Kalibrierung.
- [ ] Autonomie-Freigabe implementiert: befristet, widerrufbar, mit Grenzen (Paare,
      Kapital), Protokollierung jeder Aktivierung.
- [ ] Deine ausdrückliche, separate Freigabe.

## Entscheidungen, die bei dir liegen

1. **Broker-Wahl** für Live-Tests (Kraken bietet keinen API-Bracket).
2. **Steuerrücklage:** Satz für die Rücklage (derzeit 0 %, keine Beratung).
3. **Kapitalobergrenze Paper** (Standard 100 €) und **Live-Pilot** (fix 10 €).
4. **Langfristportfolio:** Bleibt es vorerst eine EUR-Buchposition (aktueller Stand)?
5. **Zeitintervall** des Paper-Betriebs (Standard 1h). Kurze Intervalle verursachen
   bei Kraken-Gebühren (0,40 % Taker) erfahrungsgemäß mehr Kosten als Vorteil.

## Offen (nicht umgesetzt)

- Aktien-Broker-Anbindung und Echtzeit-Aktiendaten (nur vorbereitet; kostenlose
  Daten sind verzögert und inoffiziell).
- Orderbuch-Ungleichgewichte als Strategie (keine kostenlosen historischen
  Orderbuchdaten; nur Live-Erfassung wäre möglich).
- Portfolio-Backtest mit echter Kapitalkonkurrenz über **Research**-Läufe (das Replay
  des Paper-Kreislaufs deckt Multi-Asset ab, die Research-Engine bewertet je Asset).
- Fundamentaldaten für Aktien/ETFs (keine verlässliche kostenlose Quelle angebunden).
- Optionale lokale LLM-Anbindung (bewusst nicht umgesetzt, standardmäßig aus).
- Live-Orderbuch-Tiefe im Paper-Betrieb (derzeit aus Kerzenvolumen abgeleitet).
