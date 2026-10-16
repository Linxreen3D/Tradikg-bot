# Tradikg-Bot

Lokal betreibbarer Trading-Bot mit KI-Unterstützung als Ziel, gedacht für ein
MacBook Air M2 (16 GB RAM). Der Bot läuft komplett lokal, ohne kostenpflichtige
Dienste und ohne Claude-API-Aufrufe.

> **Status: Sprint 1. Es wird nur virtuell gehandelt.**
> Es gibt keine Order-Anbindung an Börsen oder Broker, keine Unterstützung für
> API-Schlüssel und keinen Echtgeld-Modus. Fordert eine Konfiguration `live` an
> oder enthält sie Felder wie `api_key`, bricht das Programm mit einem Fehler ab.
> Die Ergebnisse sind keine Anlageberatung.

Was in diesem Sprint umgesetzt wurde, was noch offen ist und welche
Einschränkungen gelten, steht in [`docs/SPRINT_01.md`](docs/SPRINT_01.md).

---

## Installation (macOS / Apple Silicon)

Voraussetzung ist Python 3.11 oder neuer, zum Beispiel über `brew install python@3.12`.

```bash
git clone https://github.com/linxreen3d/tradikg-bot.git
cd tradikg-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # Kern + Test-/Lint-Werkzeuge
pip install -e ".[data]"         # optional: kostenlose Online-Datenquellen (ccxt, yfinance)
```

Ohne die Extras braucht der Kern nur `numpy` und `pandas`. Daten werden lokal in
SQLite gespeichert (`data/tradikg.sqlite`), eine separate Datenbank ist nicht nötig.

## Ausführung

Alle Befehle aus dem Projektverzeichnis starten. Dann wird `config/default.toml`
automatisch geladen.

```bash
# 1) Daten bereitstellen (eine der drei Varianten)
tradikg sample-data --symbol DEMO-EUR                       # synthetisch, offline (keine echten Marktdaten)
tradikg import-csv kurse.csv --symbol BTC-EUR --timeframe 1d --asset-class crypto
tradikg fetch --symbol BTC/EUR --timeframe 1d --asset-class crypto --start 2022-01-01   # benötigt .[data]
tradikg fetch --symbol AAPL    --timeframe 1d --asset-class stock  --start 2022-01-01   # benötigt .[data]

# 2) Gespeicherte Kursreihen anzeigen
tradikg list-data

# 3) Baseline-Strategie backtesten (Ergebnis + Trades landen in SQLite)
tradikg backtest --symbol DEMO-EUR
tradikg backtest --symbol DEMO-EUR --fast 10 --slow 30 --trend 0 --export-trades reports/trades.csv
tradikg backtest --symbol AAPL --fixed-fee 1.0 --spread-bps 10 --slippage-bps 5

# 4) Frühere Läufe anzeigen
tradikg runs
```

Statt `tradikg …` funktioniert auch `python -m tradikg_bot …`.

CSV-Dateien brauchen eine Zeitspalte (`timestamp`, `date`, `datetime` oder
`time`) sowie `open, high, low, close, volume`. Die Groß-/Kleinschreibung ist
egal. Zeitstempel ohne Zeitzone werden als UTC gelesen. Ein Zeitstempel bezeichnet
die **Öffnungszeit** der Kerze.

## Tests und Lint

```bash
pytest            # 91 Tests
ruff check src tests && ruff format --check src tests
```

Die GitHub-Actions-CI führt Lint und Tests unter Python 3.11, 3.12 und 3.13 aus.

---

## Architektur

```
src/tradikg_bot/
├── config.py            Konfiguration (TOML), nur Modi backtest/paper, Sperren für Live-Modus und Secrets
├── domain.py            AssetClass, Instrument (Stückelung, Mindestordergröße), ExitReason
├── data/
│   ├── validation.py    Normalisierung auf UTC-OHLCV, Duplikate, ungültige Kerzen, Lückenbericht
│   ├── processing.py    Resampling ohne unfertige Kerzen, chronologischer Train/Test-Split
│   └── sources.py       CSV, synthetische Daten, optional ccxt (Krypto) und yfinance (Aktien)
├── storage/
│   └── sqlite_store.py  Tabellen instruments, bars, backtest_runs, trades
├── costs/fees.py        Kommission (prozentual, minimal, fix), Spread, Slippage, Break-even
├── indicators.py        kausale Indikatoren: SMA, EMA, ATR (Wilder), RSI
├── strategies/
│   ├── base.py          Strategie-Schnittstelle und Signalvertrag
│   └── sma_crossover.py Baseline: SMA-Crossover, Trendfilter, ATR-basierter SL/TP
├── risk/
│   ├── sizing.py        Positionsgröße nach Worst-Case-Verlust inkl. Kosten, Kosten-/CRV-Filter
│   └── limits.py        tägliche Verlust-Abschaltschwelle (10 %)
├── backtest/
│   ├── engine.py        ereignisgesteuerte Engine (Ausführung zur nächsten Eröffnung)
│   ├── lookahead.py     empirischer Test auf Look-Ahead-Bias
│   ├── metrics.py       Performance-, Risiko- und Kostenkennzahlen, Tagesvolumen-Benchmark
│   └── report.py        Textbericht
└── cli.py               Kommandozeile
```

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

| Baustein | Krypto-Standard (`CRYPTO_SPOT_DEFAULT`) | Aktien-Standard (`STOCK_NEOBROKER_DEFAULT`) |
|---|---|---|
| Kommission | 0,10 % vom Nennwert | 1,00 € fix pro Order |
| Spread (gesamt) | 10 bps | 10 bps |
| Slippage | 5 bps je Marktorder | 5 bps je Marktorder |

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

### Risikoregeln (in der Engine umgesetzt)

- Jede Position hat Stop-Loss und Take-Profit. Einstiege ohne gültige Marken werden verworfen.
- Der Worst-Case-Verlust bis zum Stop inklusive aller Kosten ist höchstens `risk_per_trade` (Standard 1 %) der Equity.
- Eine Position ist höchstens `max_position_fraction` (Standard 50 %) des Kapitals groß. Es gibt keinen Hebel und keine Leerverkäufe.
- Tägliche Abschaltschwelle von 10 % (UTC-Tag): Neue Positionen werden nur so groß, dass ein Stop das Resttagesbudget nicht übersteigt. Wird die Schwelle trotzdem erreicht, zum Beispiel durch eine Kurslücke, stellt die Engine zur nächsten Eröffnung glatt und lässt bis zum Folgetag keine neuen Einstiege zu.

### Analyse-Benchmark „10 % auf das Tagesvolumen“

Der Bericht zeigt das Nettoergebnis eines Tages geteilt durch das an diesem Tag
gehandelte Volumen (Käufe plus Verkäufe). Diese Kennzahl dient **nur der
Auswertung**. Strategie, Sizer und Engine lesen sie nie aus, es gibt also keinen
Anreiz, mehr zu handeln, um ein Ziel zu erreichen.
