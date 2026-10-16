# Master-Sprint: Research-, ML- und autonome Paper-Plattform

Stand: 16.10.2026 · Branch `feature/autonomous-paper-platform` · baut auf PR #2 (→ PR #1) auf

## Ergebnis in einem Satz

Die vertikale Kette **Marktdaten → Signal → Kosten-/Risikoprüfung → virtueller Trade →
Abschluss → Nachanalyse → SQLite → Dashboard** funktioniert vollständig und autonom im
Paper-Modus. Echtgeld bleibt deaktiviert (**NOT LIVE READY**, siehe `LIVE_READINESS.md`).

## Implementierte Funktionen

| Bereich | Umsetzung |
|---|---|
| Grundlage | Executor: mehrere Positionen (eine je Paar), Tageslimit = 10 % der Trading-Balance zu Tagesbeginn, injizierbare Uhr, Paper-Konfiguration > 10 € nur mit `paper=True` (von Live-Börsen abgelehnt) |
| Marktdaten | Kraken OHLC (1m–1w, nur abgeschlossene Kerzen), Ticker (Spread, 24h-Volumen), SQLite-Cache inkrementell, Token-Bucket-Rate-Limit, Retry mit exponentiellem Backoff, Stale-Erkennung, Datenvalidierung |
| Scanner | nur über Kraken handelbare EUR-Paare (online, keine Fiat-/Stablecoin-Basis), Filter nach Mindestorder ≤ Kapital, Spread, 24h-Volumen; Ablehnungsgründe protokolliert |
| Aktien | Quelle vorbereitet und ehrlich als verzögert/inoffiziell/nicht handelbar markiert; Handelszeiten (Xetra, ohne Feiertage) |
| Strategien | Trend (SMA, EMA+ADX+Volumen), Momentum (Donchian+ROC+RSI), Mean Reversion (Bollinger/Z-Score), Volatilitäts-Ausbruch (Squeeze), Volumen-Ausbruch, Marktstruktur (bestätigte Swings) – alle kausal geprüft |
| Research | Train/Validation/Test chronologisch, Walk-Forward (expandierend), Buy&Hold- und Zufallseinstiegs-Baseline (Monte-Carlo), Kosten ×0/×1/×2, Ergebnisse je Marktphase, Urteil *bestätigt/hypothese/verworfen*, Markdown-Berichte, OOS-Statistiken in SQLite |
| Regime | regelbasiert: trend_up, trend_down, range, high_volatility |
| ML | Triple-Barrier-Labels (TP vor SL), Embargo zwischen Bereichen, Basisraten-Baseline vs. logistische Regression, Brier/Log-Loss/ECE/Reliability, Modellregister (Version, Datenbereiche, Features, Parameter, Metriken), Prognosen ↔ Ergebnisse, Champion/Challenger mit Bootstrap-KI, im Live-Modus nie automatisch |
| Signalbewertung | Netto-EV nach Gebühren, Spread, Slippage; Ausführungswahrscheinlichkeit aus Liquidität; Korrelationsabschlag; Ranking nach erwartetem Gewinn je riskiertem Euro |
| Autonomer Paper-Kreislauf | Zeitschritt-Zustandsmaschine, Ausführung zur Eröffnung der Folgekerze, SL/TP je Kerze (konservativ), Abgleich, Idempotenz (Schritte, Absichten, Buchungen), Neustart aus SQLite, Kill Switch, SAFE_HALT, Tagesabschaltung, dynamische Cooldowns, Datenqualitätsprüfung; **keine feste Trade-Anzahl** |
| Ledger | Verlustaufholung (High-Water-Mark), dann 33/33/34; optionale Steuerrücklage (Standard 0 %); idempotent; Korrekturbuchungen; Rücklagen erhöhen die Kapitalobergrenze nie |
| Nachanalyse | je Trade: Asset, Strategie, Modell, Regime, Prognose, Ein-/Ausstieg, SL/TP, Größe, Gebühren, Spread, Slippage, Netto, Haltedauer, MFE/MAE, erwartet vs. tatsächlich, Prozessbewertung; Tagesbericht mit Hypothesen (nie automatisch aktiviert) |
| Langfrist-Engine | Datenmodelle, Schnittstellen, Konzentration (HHI), Exposure, Benchmark-Vergleich, Rebalancing-Vorschläge – keine Käufe, keine erfundenen Fundamentaldaten |
| Dashboard | Streamlit, nur 127.0.0.1, nur lesend: Übersicht, Trading, Analytics, AI Research, System Health, Einstellungen; Live-Modus nicht schaltbar; keine Schlüssel |
| Betrieb | `~/.tradikg`, Instanzsperre, JSON-Logs mit Schlüssel-Schwärzung, Integritätsprüfung, rotierende Backups, atomarer Status, Nachholen nach Schlafmodus ohne neue Einstiege |
| macOS | `setup_mac.sh`, `start/stop/status_bot.sh`, `start_dashboard.sh`, `run_tests.sh`, `check_environment.sh` (bash 3.2, shellcheck-sauber), launchd-Vorlagen, Keychain |
| CLI | `tradikg init / doctor / backup / research run|report / ml train / paper run|replay|status|kill|reset-halt|report` |

## Testergebnisse

```
Frischer Clone (Linux, Python 3.13): setup_mac.sh -> Exit 0
  ruff check / ruff format --check     -> sauber (108 Dateien)
  pytest                               -> 291 passed, 4 skipped
Python 3.11.17 + pandas 3.0.6 / numpy 2.4.6 -> 291 passed, 4 skipped
Python 3.11.17 + pandas 2.2.3 / numpy 2.2.6 -> 291 passed, 4 skipped
Übersprungen: 4 Opt-in-Tests gegen die echte Kraken-API (Netz hier blockiert, 403)
```

Abgedeckte Pflichtszenarien (alle simuliert): mehrere Signale gleichzeitig ·
nur 5 € verfügbar · Teilausführung · Timeout nach Annahme · abgelehnte Stop-Loss-Order ·
Verbindungsabbruch/unklares Ergebnis · Neustart · Tagesverlustlimit · Gebühren
übersteigen Gewinn · 20 Signale in Folge · keine Signale · stehender Datenfeed ·
doppelte Meldung desselben Trades · parallele Aufrufe · Datenlücke · beschädigter
Zustand · Look-Ahead (alle Strategien, Features, Regime).

**Nicht unter echten Bedingungen geprüft:** Kraken-API (Netz blockiert), macOS-Hardware
und launchd, Dashboard auf dem Mac, Strategien auf echten Marktdaten. Details:
`LIVE_READINESS.md`.

## Ehrliche Einordnung der Ergebnisse

- Auf synthetischen Zufallsdaten bestätigt die Research-Engine keine Strategie und der
  Paper-Bot handelt nicht – das ist gewollt.
- Auf synthetischen Daten **mit** eingebautem Vorteil findet die Kette den Vorteil und
  handelt; das prüft nur die Pipeline, nicht echte Märkte.
- Mit Kraken-Gebühren (Annahme 0,40 % Taker) sind kurze Zeitintervalle in den Tests
  kostenbedingt fast immer unprofitabel. Ergebnisse auf echten Daten stehen aus.
- Es gibt keine Renditeversprechen. „bestätigt“ heißt nur: auf ungesehenen Testdaten
  nach Kosten positiv und besser als Zufall.

## Nachtrag: Historical Market Replay (Phase 2.6)

Branch `feature/historical-replay` (baut auf diesem PR auf). Walk-Forward-Replay mit
derselben Paper-Engine, Research je Entscheidungszeitpunkt nur auf Vergangenheitsdaten,
unangetastetem Holdout (Auswertungen gezählt), rollierenden Fenstern, Kapitalszenarien
10 €/100 €/1.000 €, Baselines und Stresstests; Bericht „Was wäre aus 100 € geworden?“
und Dashboard-Bereich. Details und Grenzen: `HISTORICAL_REPLAY.md`.

Pipeline-Lauf auf **synthetischen** Daten (3 Assets, 730 Tageskerzen) – sagt nichts
über echte Märkte: 100 € → 99,77 € im Walk-Forward, 100,50 € im Holdout, 5 Fenster
zwischen 99,18 € und 100,39 €; Gesamturteil „nicht belastbar“ (u. a. synthetische
Daten, nur 6 Trades, negativ bei Gebühren ×2, nicht besser als Zufallseinstiege).

## Offene Funktionen

Siehe `ROADMAP.md` (Abschnitt „Offen“): Aktienbroker/Echtzeit-Aktiendaten,
Orderbuch-Strategien, Fundamentaldaten, Research-Portfolio-Backtest mit
Kapitalkonkurrenz, Live-Broker mit API-Bracket, Autonomie-Freigabe für Echtgeld,
optionale lokale LLM-Anbindung.

## Bekannte Risiken

1. Kein API-Bracket bei Kraken Spot – Live bleibt gesperrt.
2. Kurslücken können Stop-Loss und Tagesverlustschwelle überschreiten (die Schwelle ist
   eine Abschaltregel, keine Verlustgarantie).
3. Paper-Fills sind modelliert (Kerzen-OHLC, angenommener Spread 10 bps, Slippage);
   echte Ausführung kann schlechter sein.
4. Die Wahrscheinlichkeit beruht auf „Gewinn nach Kosten“ der OOS-Trades (inkl.
   Signal-Exits) und wird im EV als TP-/SL-Ausgang verwendet – eine Näherung.
5. Kurze Kraken-Historie (720 Kerzen je Abruf) begrenzt die Aussagekraft der Research,
   bis sich der Cache gefüllt hat oder Daten importiert wurden.
6. Ein MacBook schläft bei geschlossenem Deckel; 24/7-Betrieb ist nicht garantiert.
7. Gebührenannahmen sind nicht kontospezifisch verifiziert.

## Empfohlene nächste Schritte

1. PR #1, #2 und diesen PR prüfen und mergen (Reihenfolge #1 → #2 → dieser).
2. Auf dem Mac installieren (`docs/MACOS_SETUP.md`), Research mit echten Kraken-Daten
   laufen lassen und den Bericht lesen.
3. Mehrere Wochen Paper-Dauerbetrieb, Dashboard und Tagesberichte beobachten.
4. Broker-Entscheidung für einen späteren Live-Test (API-Bracket erforderlich).
5. Erst danach: Gateway für den gewählten Broker, manuelle Abnahme, Einzeltrade-Freigabe.
