# Live-Readiness-Bericht

Stand: 17.10.2026

## Entscheidung: **NOT LIVE READY**

Echtgeldhandel bleibt deaktiviert. Ein autonomer Echtgeldmodus existiert nicht und
lässt sich nicht aktivieren: Der autonome Kreislauf lehnt jede Live-Börse mit
`AutonomyNotAuthorizedError` ab, und der Einzeltrade-Pfad ist durch den Kraken-Blocker
gesperrt. In diesem Sprint wurden keine echten Orders gesendet.

### Gründe

1. **Kraken Spot bietet keinen börsenseitigen SL/TP-Bracket (OCO) über die API.**
   Die Spot-API kennt nur *Conditional Close* (OTO, ein Folgeauftrag). TP/SL mit OCO
   ist für Spot nur in der Kraken-Pro-Oberfläche dokumentiert (Blog Januar 2025).
   Erneute Prüfung der offiziellen AddOrder-Doku am 17.10.2026: Ordertypen market, limit,
   iceberg, stop-loss(-limit), take-profit(-limit), trailing-stop(-limit), settle-position;
   nur *ein* `close[…]`-Folgeauftrag, kein OCO. Inoffizielle
   Endpunkte oder UI-Automatisierung werden nicht verwendet.
2. **Keine Prüfung mit echten Orders.** Seit 17.10.2026 laufen öffentliche Kraken-Daten
   (OHLC, AssetPairs, Ticker) auf dem Mac; private Endpunkte und echte Orders wurden
   nie genutzt.
3. **Kein bestätigter Vorteil auf echten Daten.** Research und Walk-Forward-Replay auf
   echten Kraken-Daten (1d: 2 Jahre, 1h/15m/5m: Q3 2026) bestätigen keine Strategie nach
   Kosten; der Bot hat in keinem Zeitraum gehandelt (docs/MAC_FIRST_RUN_REPORT.md).
4. **Kostenstruktur.** Bei 0,80 % Taker-Gebühr je Seite (Kraken-Pro-Stufe 1, Stand 17.10.2026) und
   ATR-basierten Stops auf Stundenbasis fressen die Kosten in den Tests regelmäßig
   jeden Vorteil auf. Das ist kein Fehler, sondern ein realistisches Ergebnis.

## Nachtrag 18.10.2026: Trendfolge-Echtbetrieb mit kleinem Budget

Auf ausdrücklichen Wunsch des Nutzers gebaut: `tradikg live` (docs/LIVE_START.md). Die
Trendfolge braucht **keinen SL+TP-Bracket**, nur einen einzelnen serverseitigen Stop-Loss – das
bietet die Kraken-Spot-API. Budget hart ≤ 50 € (Standard 20 €), nur BTC/ETH Spot, eigene Mengen
per `userref`, idempotent, Abbruch bei Auszahlungsrecht/Margin, Probelauf mit `validate=true`.
Der bestehende Bracket-/Signal-Pfad bleibt **NOT LIVE READY**.

## Alternative Broker (nur Recherche, keine Konten angelegt)

| Anbieter | API-Bracket für Spot | Quelle | Offene Punkte |
|---|---|---|---|
| Kraken Pro | **nein** (nur Conditional Close/OTO; TP/SL-OCO nur in der Oberfläche) | docs.kraken.com, Kraken-Support, Kraken-Blog | – |
| Binance Spot | **ja**, OTOCO-Order-List (`POST /api/v3/orderList/otoco`, seit 11.06.2024): Einstieg + OCO aus Stop-Loss und Take-Profit | developers.binance.com | Verfügbarkeit/Regulierung in Österreich, Mindestgrößen, Teilausführungsverhalten |
| Coinbase Advanced Trade | **ja**, angehängter TP/SL (`attached_order_configuration.trigger_bracket_gtc`); die Gegenseite wird bei Auslösung storniert | docs.cdp.coinbase.com, Coinbase-Hilfe | Verhalten bei Teilausführung, EUR-Paare, Gebühren für kleine Orders |

Die Execution-Schnittstelle (`ExchangeGateway.submit_bracket`) ist brokerunabhängig.
Ein neues Gateway muss nur diese Schnittstelle gegen die offizielle Dokumentation
implementieren; Vorab-Prüfungen, Journal, Abgleich und SAFE_HALT bleiben gleich.

## Sicherheits-Checkliste

| Anforderung | Status | Nachweis |
|---|---|---|
| Fail closed (Unklares Ergebnis -> Sperre) | ✅ | `live/executor.py`, Tests `test_unknown_order_outcome_halts_safely`, `test_timeout_before_accept_halts_instead_of_guessing` |
| Keine Echtgeldorder ohne Freigabe | ✅ | Gate (Flag + tagesgebundener Text), Einzeltrade-Freigabe wird verbraucht |
| Kein autonomer Echtgeldmodus | ✅ | `AutonomyNotAuthorizedError`, Test `test_live_gateway_is_refused` |
| Keine Auszahlung über die API | ✅ | Endpunkt-Allowlist, Probe auf Auszahlungsrecht |
| Keine Schlüssel in Repo/Logs/Dashboard | ✅ | `.gitignore`, Repo-Scan-Test, Log-Schwärzung, Dashboard-Test |
| Geheimnisverwaltung | ✅ | macOS-Schlüsselbund, Umgebungsvariablen, Datei außerhalb des Repos (0600) |
| Order-Idempotenz / keine Doppelorders | ✅ | `userref`/Client-Referenz, eine Position je Paar, eindeutige Signalschlüssel |
| Antwort geht verloren, Order trotzdem angenommen | ✅ | Abgleich über Client-Referenz, nie erneutes Senden (`test_timeout_after_accept_is_reconciled`) |
| Bestätigte Orderzustände / Reconciliation | ✅ | `reconcile()` beim Start und vor jedem Trade |
| Sperre bei unbekannten Positionen/Orders | ✅ | `test_unknown_balance_or_order_on_account_halts` |
| Sperre bei nicht bestätigter Schutzorder | ✅ | `test_missing_protection_halts…`, `test_rejected_stop_loss_order_halts` |
| Handelsstopp bei veralteten Daten | ✅ | Stale-Erkennung, Nachholen ohne Einstiege |
| Not-Aus | ✅ | Kill Switch (Datei/CLI) |
| Manuelle Wiederfreigabe nach kritischen Fehlern | ✅ | `reset-halt --note` (Begründung Pflicht) |
| Paper und Live getrennt | ✅ | eigene Datenbanken; Paper-Konfiguration wird von Live-Börsen abgelehnt |
| Börsenseitiger SL **und** TP | ❌ | Kraken-Blocker (siehe oben) |
| Tests unter echten Börsenbedingungen | ❌ | Netzwerk blockiert; nur Emulator/Fakes |

## Was simuliert bestanden wurde – und was nicht real geprüft ist

| Bereich | Simuliert/emuliert bestanden | Unter echten Bedingungen geprüft |
|---|---|---|
| Kraken-Signatur | ✅ (offizielles Dokumentationsbeispiel) | ❌ |
| Kraken-HTTP (Timeouts, Fehlerklassen) | ✅ lokaler Emulator über echtes HTTP | ❌ |
| Kraken OHLC/Ticker/Scanner | ✅ Fake im offiziellen Antwortformat | ❌ |
| Bracket-Ausführung, Teilausführung, OCO | ✅ Paper-Börse mit Fehlerinjektion | ❌ (bei Kraken nicht möglich) |
| Neustart, Schlafmodus-Nachholen, Instanzsperre | ✅ | ❌ auf echter macOS-Hardware |
| launchd-Dienste | ✅ plist-Validität | ❌ nicht auf macOS geladen |
| Dashboard | ✅ AppTest + Screenshot (Linux, Chromium) | ❌ nicht auf dem Mac geöffnet |
| Strategien/Research/ML | ✅ synthetische Daten (mit/ohne eingebauten Vorteil) | ❌ keine echten Marktdaten |

## Manuelles Abnahmeverfahren (vor jeder Live-Phase)

1. `scripts/check_environment.sh` und `tradikg doctor --network` ohne Fehler.
2. Mindestens 4 Wochen Paper-Dauerbetrieb mit echten Kraken-Daten. Danach
   Tagesberichte, Kalibrierung und SAFE_HALT-Historie im Dashboard prüfen.
3. Historical Replay auf echten Daten (`tradikg replay run`): Gesamturteil „belastbar“,
   Holdout dabei erstmals ausgewertet.
4. Research auf echten Daten: mindestens eine Strategie „bestätigt“; das Ergebnis im
   Bericht nachvollziehen (Testdaten, Kosten ×2, Zufalls-Baseline, Walk-Forward).
5. Broker mit API-Bracket gewählt, Gateway implementiert, mit Test-/Validate-Endpunkten
   geprüft; Mindestgrößen und Gebühren live abgefragt.
6. Sub-Konto mit maximal 10 €, API-Key **ohne** Auszahlungsrechte.
7. Not-Aus, SAFE_HALT und `reset-halt` einmal bewusst ausgelöst und geprüft.
8. Ein einzelner Testtrade nur mit deiner ausdrücklichen Einzeltrade-Freigabe; danach
   Abgleich von Journal, Broker-Kontoauszug und Ledger.
