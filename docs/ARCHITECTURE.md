# Architektur

Stand: 16.10.2026 · Branch `feature/autonomous-paper-platform` (baut auf PR #2 → PR #1 auf)

Tradikg ist eine lokal betriebene Research- und Paper-Trading-Plattform für einen
MacBook Air M2. Sie läuft ohne Claude- oder OpenAI-Sitzung und ohne kostenpflichtige
Dienste. **Echtgeldhandel ist deaktiviert.** Einen autonomen Echtgeldmodus gibt es nicht.

## 1. Bestandsaufnahme vor diesem Sprint

| Bereich | Stand (PR #1/#2) | Befund | Maßnahme in diesem Sprint |
|---|---|---|---|
| Datenmodelle | `domain.py`, `live/models.py` | zwei Modellwelten (Backtest vs. Börse) | bleiben getrennt: Backtest für Einzel-Asset-Research, Börsenmodelle für Paper/Live |
| Backtesting | `backtest/engine.py` (1 Asset, long-only) | korrekt (t+1-Ausführung, konservatives Intrabar) | weiterverwendet als Research-Kern; Multi-Asset über das Replay des Paper-Kreislaufs |
| Risiko | `risk/` (Backtest), `live/precheck.py` (Order) | Live-Executor erlaubte nur **eine** Position; Tageslimit fix an Pilotkapital | mehrere Positionen (eine je Paar); Tageslimit = 10 % der Trading-Balance zu Tagesbeginn |
| Kostenmodell | `costs/fees.py` | Research nutzte 0,1 %, Paper 0,4 % → zu optimistische OOS-Werte | `KRAKEN_SPOT_ASSUMED` für Research und Paper |
| Kraken | `live/kraken/*` | kein API-Bracket (OCO) → Live gesperrt | unverändert gesperrt; OHLC ergänzt |
| Konfiguration | `config.py` | Backtest-Konfiguration | neue `platform_config.py` (nur Paper) |
| SQLite | `storage/`, `live/journal.py` | Zeitstempel `closed_at` aus Wanduhr → im Replay falscher Tag | injizierbare Uhr im Executor |
| CLI | `cli.py`, `live/cli_kraken.py` | – | `cli_platform.py` ergänzt |
| Tests/CI | 179 Tests, 3.11–3.13 | `.gitignore` schloss `src/tradikg_bot/data/` aus (CI rot) | behoben; Tests im frischen Clone |
| Schutzmechanismen | Gate, SAFE_HALT, Einzeltrade-Freigabe, Endpunkt-Allowlist | gut | unverändert; Paper-Konfiguration wird von Live-Börsen abgelehnt |

Gefundene und behobene Fehler: `.gitignore`-Lücke (Datenpaket fehlte im Repository),
Endlosschleife bei unbestätigter Schutzorder im Paper-Betrieb (stehende
Simulationsuhr), falsche Tageszuordnung im Replay, inkonsistente Kostenannahmen.

## 2. Komponenten

| # | Komponente | Modul(e) | Status |
|---|---|---|---|
| 1 | Market Data Engine | `market/kraken_data.py`, `market/resilience.py`, `storage/` | umgesetzt (Kraken OHLC/Ticker, Cache, Rate-Limit, Retry, Stale-Erkennung) |
| 2 | Market Scanner | `market/scanner.py`, `market/sources.py` | umgesetzt (nur handelbare EUR-Paare); Aktienquelle vorbereitet, nicht handelbar |
| 3 | Feature Engineering | `features/engineering.py`, `indicators.py` | umgesetzt (15 kausale, skalenfreie Merkmale) |
| 4 | Strategy Engine | `strategies/families.py` | umgesetzt (7 Strategien in 6 Familien) |
| 5 | Machine Learning Engine | `ml/` | umgesetzt (Baseline vs. logistische Regression, Kalibrierung) |
| 6 | Market Regime Detector | `features/regime.py` | umgesetzt (regelbasiert) |
| 7 | Signal Ranking Engine | `trading/signals.py`, `trading/probability.py` | umgesetzt (Netto-EV, Korrelation, Ausführungswahrscheinlichkeit) |
| 8 | Risk Manager | `live/precheck.py`, `live/executor.py`, `trading/engine.py` | umgesetzt (gleiche Regeln für Paper und Live) |
| 9 | Execution Engine | `live/executor.py`, `live/gateway.py`, `live/paper.py`, `live/kraken/` | umgesetzt; Live gesperrt |
| 10 | Portfolio Manager | `trading/ledger.py`, `invest/portfolio.py` | Ledger umgesetzt; Langfrist-Engine als Grundgerüst |
| 11 | Trade Journal | `live/journal.py`, `trading/store.py` | umgesetzt (Write-Ahead, Neustart) |
| 12 | Strategy Research Engine | `research/` | umgesetzt (Walk-Forward, Baselines, Berichte) |
| 13 | Backtesting Engine | `backtest/` (Einzel-Asset), `trading/runner.replay` (Multi-Asset) | umgesetzt |
| 14 | Live Paper-Trading Engine | `trading/engine.py`, `service/paper_service.py` | umgesetzt |
| 15 | Analytics Dashboard | `dashboard/` | umgesetzt (Streamlit, nur lokal, nur lesend) |
| 16 | macOS Service Manager | `service/ops.py`, `scripts/`, `deploy/launchd/` | umgesetzt (ungetestet auf echter macOS-Hardware) |

Jede Komponente ist ohne Netz und ohne die anderen Komponenten testbar
(Fakes für Kraken, Paper-Börse, synthetische Daten).

## 3. Datenfluss (vertikaler Durchstich)

```
Kraken OHLC/Ticker ──► Market Data Engine ──► SQLite-Cache (market.sqlite)
                                                  │
                         ┌────────────────────────┴───────────────────────┐
                         ▼                                                ▼
              Strategy Research Engine                         Paper-Dauerbetrieb
   (Train/Val/Test, Walk-Forward, Baselines,         (service/paper_service.py, je Kerze)
    Kosten x0/x1/x2, Regime) ─► research.sqlite                 │
                         │      (nur OOS-Statistiken)            ▼
                         └──────────────► Wahrscheinlichkeit (OOS, optional ML-Champion)
                                                                 │
   Strategien ─► Signal ─► Netto-EV nach Kosten ─► Ranking ─► Absicht (nächste Kerze)
                                                                 │
                                   LiveExecutor (gleiche Prüfungen wie Live) + PaperExchange
                                                                 │
                     SL/TP je Kerze ─► Abgleich ─► Ledger 33/33/34 ─► Nachanalyse
                                                                 │
                                                  paper.sqlite ─► Dashboard (nur lesend)
```

## 4. Architekturentscheidungen

1. **Eine Ausführungslogik für Paper und Live.** Der autonome Paper-Kreislauf nutzt den
   `LiveExecutor` (Vorab-Prüfungen, Write-Ahead-Journal, Abgleich, SAFE_HALT) mit einer
   Paper-Börse. Zustände und Datenbanken sind strikt getrennt (`paper.sqlite` vs.
   `live_journal.sqlite`). Was im Paper-Betrieb getestet wird, ist derselbe Code.
2. **Paper simuliert einen Broker mit börsenseitigem Bracket.** Das ist die
   Zielarchitektur. Kraken Spot erfüllt sie per API nicht; deshalb bleibt Live gesperrt
   und die Paper-Ergebnisse sind **nicht** 1:1 auf Kraken übertragbar.
3. **Ohne Belege kein Trade.** Die Gewinnwahrscheinlichkeit stammt nur aus
   Out-of-Sample-Statistiken (Test + Walk-Forward, nie Training) und wird auf die
   Vorannahme „kein Vorteil“ geschrumpft. Ohne Research-Ergebnisse handelt der Bot nicht.
4. **Keine feste Trade-Anzahl.** Begrenzt wird über Kapital, Risiko je Trade,
   Tagesverlust, eine Position je Paar, eindeutige Signalschlüssel und dynamische
   Cooldowns (exponentiell nach Verlustserien, nach technischen Fehlern).
5. **Einstieg zur Eröffnung der Folgekerze** – in Replay und Live-Paper gleich. Das
   ist frei von Look-Ahead und macht Replay und Paper-Betrieb vergleichbar.
6. **Interpretierbare Modelle zuerst.** ML (logistische Regression) wird nur Champion,
   wenn sie die Baseline auf unabhängigen Daten belastbar schlägt. Modelle werden als
   JSON-Parameter gespeichert, ohne Pickle.
7. **SQLite statt Server-Datenbank.** WAL, `synchronous=FULL`, Integritätsprüfung beim
   Start, konsistente Online-Backups, idempotente Schreibvorgänge.
8. **Streamlit** für das Dashboard (schnelles, wartbares MVP). Es bindet nur an
   127.0.0.1 und öffnet die Datenbanken schreibgeschützt.
9. **Keine neuen Pflichtabhängigkeiten.** Kern: numpy, pandas. Optional: scikit-learn
   (Training), Streamlit/psutil (Dashboard), ccxt/yfinance (zusätzliche Datenquellen).
   Ohne Ollama oder LLM lauffähig; `local_llm_enabled` ist standardmäßig aus und nicht
   implementiert.

## 5. Datenablage

```
~/.tradikg/
  config.toml           Plattform-Konfiguration (nur Paper)
  data/market.sqlite    Kerzen-Cache
  data/research.sqlite  Research-Läufe, OOS-Statistiken, Berichte
  data/models.sqlite    Modellregister, Prognosen + Ergebnisse
  data/paper/paper.sqlite  Paper-Journal, Ledger, Absichten, Equity, Analysen
  data/paper/KILL_SWITCH   Not-Aus (Datei vorhanden = keine neuen Einstiege)
  data/replays/…        historische Replays (getrennt vom laufenden Paper-Konto)
  logs/tradikg.jsonl    strukturierte Logs (Schlüssel geschwärzt)
  backups/<Zeitstempel>/ Datenbank-Backups
  run/status.json, run/paper.lock
```

Echtgeld-Komponenten (gesperrt) nutzen `data/live_journal.sqlite` im Repository-Ordner
bzw. den per CLI angegebenen Pfad – nie die Paper-Datenbank.
