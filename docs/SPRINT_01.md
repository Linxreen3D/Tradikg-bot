# Sprint 1: Fundament (Daten, Kosten, Baseline, Backtesting)

## Sprint-Ziel

Eine modulare Projektstruktur mit Datenhaltung, Gebührenberechnung,
Verarbeitung historischer Daten, einer regelbasierten Baseline-Strategie und
einer testbaren Backtesting-Engine. Die Engine soll Look-Ahead-Bias verhindern
und Handelskosten einrechnen. Echte Orders, API-Schlüssel und ein Echtgeld-Modus
gehören nicht zum Umfang.

## Umgesetzt

| Anforderung | Umsetzung | Nachweis (Tests) |
|---|---|---|
| Modulare Projektstruktur | `src/`-Layout, Pakete `data`, `storage`, `costs`, `strategies`, `risk`, `backtest`, CLI | gesamte Suite |
| Datenhaltung | SQLite: `instruments`, `bars` (idempotentes Upsert), `backtest_runs`, `trades` | `test_storage_cli.py` |
| Historische Datenverarbeitung | Normalisierung (UTC, Spalten, Epoch-ms), Sortierung, Duplikate, ungültige Kerzen, Lückenbericht, Resampling ohne unfertige Kerzen, chronologischer Split | `test_data.py` |
| Datenquellen | CSV, synthetische GBM-Daten, optional ccxt (Krypto) und yfinance (Aktien) ohne API-Schlüssel | `test_data.py` (Online-Adapter mit Mocks) |
| Gebührenberechnung | Kommission (prozentual, Minimum, fix), Spread, Slippage, Round-Trip-Kosten, Break-even | `test_fees.py` |
| Kosten bei jeder Entscheidung | Sizer rechnet Worst-Case-Verlust und Nettogewinn nach Kosten. Einstiege mit zu schwachem Netto-CRV werden verworfen | `test_risk.py`, `test_engine.py` |
| Baseline-Strategie | SMA-Crossover (20/50) mit Trendfilter (SMA 100), SL = 2×ATR, TP = 3×ATR, nur long | `test_strategy_lookahead.py` |
| Backtesting-Engine | ereignisgesteuert, Ausführung zur nächsten Eröffnung, SL/TP intrabar, Kurslücken, konservative Reihenfolge, Glattstellung am Datenende | `test_engine.py` |
| Look-Ahead-Bias verhindern | Ausführung bei t+1, Sizing mit Schluss t, kausale Indikatoren, automatischer Kausalitätscheck vor jedem Lauf | `test_strategy_lookahead.py`, `test_engine_has_no_lookahead_when_future_changes` |
| SL/TP für jede Position | Pflicht in der Engine; Signale ohne gültige Marken werden verworfen | `test_entry_without_stop_or_take_profit_is_rejected`, `test_every_trade_has_stop_loss_and_take_profit` |
| 10 %-Tagesverlustschwelle | `DailyLossGuard`: Tagesbudget begrenzt die Positionsgröße, bei Verletzung Glattstellung und Sperre bis zum Folgetag | `test_daily_loss_limit_liquidates_and_blocks_entries`, `test_daily_loss_guard` |
| Einfache Positionsgröße | Fixed-Fractional (1 % Risiko inkl. Kosten), Kapitalgrenze, Stückelung, Mindestordergröße | `test_risk.py` |
| Kennzahlen | Rendite, CAGR, Max-Drawdown, Sharpe, Sortino, Calmar, Exposure, Trefferquote, Profit-Faktor, R-Multiple, Kostenaufschlüsselung, Buy & Hold, Exit- und Ablehnungsgründe | `test_metrics.py`, `test_engine.py` |
| Benchmark 10 % auf Tagesvolumen | nur in der Auswertung, kein Einfluss auf Entscheidungen | `test_daily_volume_benchmark_is_analysis_only` |
| Kein Echtgeld | Modi nur `backtest`/`paper`; `live` u. ä. werfen einen Fehler; Secrets in der Konfiguration werden abgelehnt; keine Order-Schnittstelle | `test_config_domain.py`, `test_cli_refuses_live_config` |
| Trade-Daten für spätere Analyse | Je Trade: Kosten, Exit-Grund, geplantes Risiko, R-Multiple, MAE/MFE, Haltedauer, in SQLite | `test_backtest_persistence` |
| Tests, Lint, CI | 91 pytest-Tests, ruff, GitHub-Actions-Matrix 3.11–3.13 | siehe unten |

### Testlauf (in diesem Sprint tatsächlich ausgeführt)

```
$ pytest -q                      # Python 3.13.16, pandas 3.0.5, numpy 2.5.3
91 passed

$ pytest -q                      # Python 3.11.17, pandas 2.1.4, numpy 1.26.4 (Kompatibilität)
91 passed

$ ruff check src tests && ruff format --check src tests
All checks passed! / 30 files already formatted
```

## Noch offen (spätere Sprints, bewusst nicht umgesetzt)

Die aktuelle Planung steht in [`ROADMAP.md`](ROADMAP.md).

- Automatische Auswahl zwischen Krypto und Aktien anhand der Handelsmöglichkeiten (Scanner/Ranking über mehrere Instrumente)
- Portfolio-Backtest mit mehreren Instrumenten gleichzeitig
- Dynamische Positionsgrößen über Fixed-Fractional hinaus (z. B. volatilitäts- oder modellbasiert)
- Lokale ML-Modelle, Feature-Pipeline, Walk-Forward-Validierung
- Automatische Analyse jedes Trades und separates Testen von Verbesserungen (Champion/Challenger). Die nötigen Trade-Daten werden bereits gespeichert
- Gewinnaufteilung 33 % / 33 % / 34 % nach Aufholung früherer Verluste (High-Water-Mark-Logik)
- Paper-Trading-Loop mit Live-Daten und automatisierter Dauerbetrieb (Scheduler, launchd auf macOS)
- Lokales Web-Dashboard
- Währungsumrechnung (USD-Aktien, USDT-Paare → EUR)

## Einschränkungen und Annahmen

- **Kein Nachweis von Profitabilität.** Die Baseline ist eine Referenz und nicht optimiert. Auf den synthetischen Demo-Daten verliert sie leicht (−4,7 % bei −77,8 % Buy & Hold). Synthetische Daten sagen nichts über echte Märkte aus.
- **Online-Datenquellen wurden nicht live getestet.** Die Sandbox blockiert Börsen-APIs (HTTP 403). ccxt- und yfinance-Adapter sind nur mit Mocks getestet. yfinance ist eine inoffizielle Schnittstelle und kann sich ändern.
- **Kostenannahmen sind Platzhalter** (0,1 % Krypto-Taker, 1 € Aktienorder, 10 bps Spread, 5 bps Slippage). Sie müssen pro Börse oder Broker geprüft werden. Slippage ist konstant und nicht volumen- oder volatilitätsabhängig. Maker/Taker wird nicht unterschieden.
- **OHLC-Kerzen gelten als Mittelkurse.** Echte Bid/Ask-Daten fehlen.
- **Intrabar-Verlauf ist unbekannt.** Treffen SL und TP dieselbe Kerze, wird der Stop-Loss angenommen (konservativ). Der Stop-Loss wird zum Stoppkurs plus Slippage gefüllt. Bei extremen Bewegungen kann die echte Ausführung schlechter sein.
- **SL/TP werden am tatsächlichen Füllkurs verankert**, nicht am Signalkurs.
- **Tagesverlustschwelle:** Die Equity wird zum Kerzenschluss geprüft, die Glattstellung erfolgt zur nächsten Eröffnung. Bei Tageskerzen fallen Tag und Kerze zusammen, die Schwelle wirkt dann vor allem über die Begrenzung der Positionsgröße. Kurslücken können die Schwelle überschreiten. Tagesgrenze ist UTC-Mitternacht.
- **Nur ein Instrument, nur long, keine Hebel**, keine Dividenden (yfinance liefert bereinigte Kurse), keine Steuern, keine Börsenzeiten oder Feiertage über die Daten hinaus.
- **Ganze Aktien:** Standardmäßig sind nur ganze Stücke handelbar. Bei 100 € Kapital und 1 % Risiko verwirft der Sizer viele Aktientrades (`below_min_size` oder `reward_after_costs_too_low`). Das ist bei diesen Annahmen realistisch.
- **Annualisierung:** Die Perioden pro Jahr werden aus der Datenabdeckung geschätzt.
- **Look-Ahead-Prüfung ist stichprobenbasiert** (25 Zeitpunkte). Sie findet typische Fehler, beweist aber formal nicht, dass eine Strategie kausal ist. Ausführung bei t+1 und Sizing mit Daten von t gelten unabhängig davon immer.
