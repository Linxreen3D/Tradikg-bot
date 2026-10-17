# Tradikg-Bot

Lokal betriebene Research- und Paper-Trading-Plattform für einen MacBook Air M2
(16 GB RAM). Sie analysiert Märkte, prüft Strategiehypothesen statistisch, handelt
**virtuell** und autonom, wertet jeden Trade aus und testet Verbesserungen
kontrolliert. Alles läuft lokal, ohne Claude-/OpenAI-Sitzung und ohne
kostenpflichtige Dienste.

> **Status: Nur virtueller Handel. Echtgeld: NOT LIVE READY.**
> Ein autonomer Echtgeldmodus existiert nicht. Die Kraken-Anbindung ist mehrfach
> gesperrt, weil die Kraken-Spot-API keinen börsenseitigen Stop-Loss/Take-Profit-
> Bracket (OCO) anbietet. Eine Echtgeld-Freigabe gilt immer nur für **einen** Trade.
> API-Schlüssel nur aus dem macOS-Schlüsselbund, Umgebungsvariablen oder einer Datei
> außerhalb des Repositorys. **Keine Anlageberatung, keine Renditeversprechen.**

| Dokument | Inhalt |
|---|---|
| [`docs/MACOS_SETUP.md`](docs/MACOS_SETUP.md) | Installation und Dauerbetrieb auf dem Mac |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Komponenten, Datenfluss, Entscheidungen |
| [`docs/SPRINT_MASTER.md`](docs/SPRINT_MASTER.md) | Umgesetzt, Testergebnisse, Risiken, nächste Schritte |
| [`docs/LIVE_READINESS.md`](docs/LIVE_READINESS.md) | Live-Readiness, Broker-Alternativen, Sicherheits-Checkliste |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Phasen und Freigabestufen |
| [`docs/KRAKEN_LIVE_PILOT.md`](docs/KRAKEN_LIVE_PILOT.md) | Kraken-Recherche und Pilot-Komponente |
| [`docs/SPRINT_01.md`](docs/SPRINT_01.md) | Sprint 1 (Backtesting-Fundament) |

---

## Schnellstart (macOS, Apple Silicon)

```bash
git clone https://github.com/Linxreen3D/Tradikg-bot.git && cd Tradikg-bot
scripts/check_environment.sh
scripts/setup_mac.sh                         # venv, Abhängigkeiten, ~/.tradikg
source .venv/bin/activate
tradikg research run --source kraken         # Strategien auf echten Kraken-Daten prüfen
tradikg research report
tradikg data fetch-kraken --timeframe 1d     # Tageskerzen (oder data import-kraken <ZIP>)
tradikg replay run --no-holdout              # Historical Replay mit Walk-Forward
scripts/start_bot.sh                         # autonomer Paper-Dauerbetrieb
scripts/start_dashboard.sh                   # http://127.0.0.1:8501
```

Autostart über launchd: `scripts/setup_mac.sh --install-launchd`. Details,
Energieeinstellungen und Fehlersuche: [`docs/MACOS_SETUP.md`](docs/MACOS_SETUP.md).

Manuelle Installation: `pip install -e ".[ml,dashboard,dev]"` (Python 3.11–3.13).
Der Kern braucht nur `numpy` und `pandas`; `ml` (scikit-learn), `dashboard`
(Streamlit) und `data` (ccxt, yfinance) sind optional.

## Befehle

```bash
tradikg init | doctor [--network] | backup
tradikg research run --source kraken|cache|synthetic [--symbols XBTEUR,ETHEUR] [--timeframe 1h]
tradikg research report [--run-id N]
tradikg ml train --source cache
tradikg data fetch-kraken | fetch-kraken-full | import-kraken <ZIP> | fetch-ccxt | coverage | quality
tradikg replay run [--no-holdout] [--quick] [--rules kraken] | report    # Walk-Forward-Replay
tradikg paper run [--once] | replay --source cache | status | report [--day YYYY-MM-DD]
tradikg paper kill [--release] | reset-halt --note "Prüfergebnis"
tradikg kraken market-check | preflight | paper-trade | status   # Kraken-Pilot (Live gesperrt)
tradikg sample-data | import-csv | fetch | backtest | runs        # Sprint-1-Backtesting
```

## Tests und Lint

```bash
scripts/run_tests.sh      # ruff + pytest (309 Tests, 4 Opt-in-Tests gegen die echte Kraken-API)
KRAKEN_PUBLIC_INTEGRATION=1 pytest tests/live/test_kraken_public_live.py   # mit Internet
```

Die GitHub-Actions-CI prüft Lint und Tests unter Python 3.11, 3.12 und 3.13.

---

## Architektur (Kurzfassung)

```
src/tradikg_bot/
├── market/        Kraken-Marktdaten (OHLC, Ticker), Cache, Rate-Limit, Retry, Scanner, Quellen
├── features/      kausale Features, Regime-Erkennung
├── strategies/    Strategiefamilien (Trend, Momentum, Mean Reversion, Volatilität, Volumen, Struktur)
├── research/      Walk-Forward-Research, Baselines, Kosten-Sensitivität, Berichte
├── ml/            Triple-Barrier-Datensatz, Modelle, Kalibrierung, Register, Champion/Challenger
├── replay/        Historical Replay: Walk-Forward, Holdout, Fenster, Baselines, Stresstests
├── trading/       autonomer Paper-Kreislauf, Signal-Ranking, Ledger 33/33/34, Nachanalyse
├── live/          Executor, Journal, Gate, Paper-Börse, Kraken-Client/Gateway (Live gesperrt)
├── invest/        Langfrist-Portfolio (Grundgerüst, keine Käufe)
├── dashboard/     Streamlit-App (nur lokal, nur lesend)
├── service/       Paper-Dauerbetrieb, Instanzsperre, Logs, Backups
├── backtest/, data/, costs/, risk/, storage/   Sprint-1-Fundament
└── cli.py, cli_platform.py, cli_replay.py
scripts/           macOS-Skripte        deploy/launchd/   launchd-Vorlagen
```

Ausführlich: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Historical Replay:
[`docs/HISTORICAL_REPLAY.md`](docs/HISTORICAL_REPLAY.md).

### Schutz vor Look-Ahead-Bias

1. **Ausführung frühestens zur nächsten Kerze.** Ein Signal aus Kerze `t`
   (berechnet nach deren Schluss) wird zum Eröffnungskurs von `t+1` ausgeführt.
2. **Positionsgröße nur aus bekannten Daten.** Referenz ist der Schlusskurs von
   `t` und der Kapitalstand vor der Eröffnung. Den Eröffnungskurs kennt man beim
   Platzieren der Order noch nicht.
3. **Kausale Indikatoren.** Sie nutzen nur `rolling`/`ewm` ohne `center` und nie
   `shift(-n)`.
4. **Automatische Prüfung.** `check_no_lookahead` vergleicht die Signale auf der
   vollen Historie mit Signalen auf abgeschnittenen Historien. Die Engine führt
   diese Prüfung vor jedem Backtest aus und bricht ab, wenn eine Strategie in die
   Zukunft schaut. Die Tests zeigen, dass die Prüfung `shift(-1)` und zentrierte
   gleitende Durchschnitte erkennt.
5. **Intrabar konservativ.** Liegen Stop-Loss und Take-Profit in derselben
   Kerze, zählt der Stop-Loss. Kurslücken werden zum Eröffnungskurs ausgeführt.
6. **Keine unfertigen Kerzen.** Beim Resampling und bei Online-Abrufen werden
   Kerzen verworfen, deren Intervall noch läuft.

### Kostenmodell

| Baustein | Kraken-Annahme (`KRAKEN_SPOT_ASSUMED`, Research + Paper) | Krypto generisch (`CRYPTO_SPOT_DEFAULT`, Sprint-1-Backtest) | Aktien (`STOCK_NEOBROKER_DEFAULT`) |
|---|---|---|---|
| Kommission | 0,80 % Taker (Paper: Maker 0,40 % für TP-Limits; Kraken-Pro-Stufe 1, Stand 17.10.2026) | 0,10 % vom Nennwert | 1,00 € fix pro Order |
| Spread (gesamt) | 10 bps | 10 bps | 10 bps |
| Slippage | 5 bps je Marktorder (Stop: 50 bps Annahme im Worst Case) | 5 bps | 5 bps |

Marktorders (Einstieg, Stop-Loss, Signal-Ausstieg) zahlen den halben Spread und
die Slippage. Take-Profit wird als Limit-Order zum Limitkurs ausgeführt. Die
Kommission fällt bei jeder Order an. Alle Werte sind **Annahmen**: Sie müssen für
die tatsächlich genutzte Börse oder den Broker geprüft werden und lassen sich per
CLI überschreiben.

Bei 100 € Kapital fällt eine fixe Ordergebühr stark ins Gewicht. Bei 20 €
Positionsgröße kosten zwei Orders à 1 € bereits mehr als 10 %. Deshalb prüft der
Sizer vor jedem Einstieg, ob der Nettogewinn am Take-Profit nach allen Kosten das
Nettorisiko bis zum Stop mindestens im eingestellten Verhältnis übersteigt
(`min_reward_risk_after_costs`). Trades, bei denen das nicht der Fall ist, werden
verworfen und mit Grund protokolliert.

### Risikoregeln

Für den autonomen Paper-Kreislauf und den (gesperrten) Live-Pfad gelten dieselben
Prüfungen (`live/precheck.py`): Mindestvolumen und Mindestwert für Einstieg, SL und TP,
Präzision, Spread, Liquidität, Slippage, Worst-Case-Verlust, Chance/Risiko nach
Kosten, Kapitalobergrenze inkl. offener Orders und Bestände, Tagesverlustbudget.
Die Tagesverlust-Abschaltschwelle beträgt 10 % der Trading-Balance zu Tagesbeginn
(UTC), realisiert plus unrealisiert. Sie ist eine Abschaltregel, keine Garantie für
einen maximalen Verlust. Es gibt keine feste Höchstzahl von Trades pro Tag.

Sprint-1-Backtest-Engine:

- Jede Position hat Stop-Loss und Take-Profit. Einstiege ohne gültige Marken werden verworfen.
- Der Worst-Case-Verlust bis zum Stop inklusive aller Kosten ist höchstens `risk_per_trade` (Standard 1 %) der Equity.
- Eine Position ist höchstens `max_position_fraction` (Standard 50 %) des Kapitals groß. Es gibt keinen Hebel und keine Leerverkäufe.
- Tägliche Abschaltschwelle von 10 % (UTC-Tag): Neue Positionen werden nur so groß, dass ein Stop das Resttagesbudget nicht übersteigt. Wird die Schwelle trotzdem erreicht, zum Beispiel durch eine Kurslücke, stellt die Engine zur nächsten Eröffnung glatt und lässt bis zum Folgetag keine neuen Einstiege zu.

### Analyse-Benchmark „10 % auf das Tagesvolumen“

Der Bericht zeigt das Nettoergebnis eines Tages geteilt durch das an diesem Tag
gehandelte Volumen (Käufe plus Verkäufe). Diese Kennzahl dient **nur der
Auswertung**. Strategie, Sizer und Engine lesen sie nie aus, es gibt also keinen
Anreiz, mehr zu handeln, um ein Ziel zu erreichen.
