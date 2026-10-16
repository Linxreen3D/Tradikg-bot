# Kraken-Pro-Spot: Execution-Komponente für den Live-Pilot (max. 10 €)

Stand: 16.10.2026 · Branch `feature/kraken-spot-execution`

## Entscheidung: **NOT LIVE READY**

Ein Echtgeldtest ist mit dieser Komponente derzeit **nicht freigegeben**. Die
Live-Freigabe ist im Code gesperrt: `tradikg kraken live-trade` bricht immer ab,
bevor eine Order gesendet wird.

**Gründe (alle müssen behoben sein, bevor ein Live-Test möglich ist):**

1. **Kein börsenseitiger Stop-Loss-/Take-Profit-Bracket über die Kraken-Spot-API
   nachweisbar** (Anforderung 6, Ausschlusskriterium). Die offizielle Spot-API
   bietet zur Verknüpfung nur *Conditional Close*: eine OTO-Order mit genau einem
   Folgeauftrag. Kraken schreibt selbst, dass sie *„cannot be used to set both a
   profit target and stop loss“*. TP/SL mit OCO-Verknüpfung ist für Spot nur in der
   Kraken-Pro-Oberfläche dokumentiert (Blogartikel 2025), nicht als API-Parameter.
   Eine simulierte Absicherung (der Bot beobachtet den Kurs und verkauft selbst)
   ist ausdrücklich ausgeschlossen und wurde nicht gebaut.
2. **Live-Integrationstests gegen Kraken nicht durchführbar.** Die
   Netzwerkrichtlinie dieser Entwicklungsumgebung blockiert `api.kraken.com`
   (HTTP 403 am Proxy). Echte Mindestgrößen, Spreads und Orderbuchtiefen wurden
   nicht live abgefragt.
3. **Tatsächliche Gebühren nicht ermittelt.** Seit 08.09.2026 liefert `AssetPairs`
   leere Gebührenfelder. Die echten Kontogebühren gibt nur der private Endpunkt
   `TradeVolume` aus, und dafür wird ein API-Key benötigt, der hier (richtigerweise)
   nicht vorliegt.
4. **Mindestgrößen schränken den 10-€-Rahmen stark ein.** Laut Kraken-Support
   liegt ETH bei 0,01 ETH Mindestvolumen, das passt nicht in 10 €. BTC liegt bei
   0,0001 BTC und belegt damit den Großteil der 10 €. Live verifiziert ist das nicht.
5. **Keine manuelle Freigabe durch dich** (absichtlich: eine Freigabe ist erst
   nach deiner Prüfung möglich).

## Recherche: Gibt es verknüpfte SL/TP-Orders über die Kraken-Spot-API?

| Mechanismus | Über API? | Beide Seiten (SL **und** TP)? | OCO? | Bewertung |
|---|---|---|---|---|
| `AddOrder` mit `close[ordertype]`/`close[price]`/`close[price2]` (Conditional Close) | ja (REST, WS v2 `conditional`) | **nein**, genau ein Folgeauftrag | nein | OTO, **kein Bracket-Ersatz** |
| Zwei getrennte Verkaufsorders (Stop + Take-Profit) | ja | ja | **nein**, keine Verknüpfung | Ohne OCO kann nach Auslösung einer Seite die andere offen bleiben. Ob Kraken den Bestand doppelt bindet („Insufficient funds“), ist nicht offiziell dokumentiert. Das wäre ein selbstgebautes OCO, also simulierte Absicherung |
| Kraken Pro „Take Profit / Stop Loss“ (Bracket, OCO) | **nur Oberfläche dokumentiert** | ja | ja | kein API-Parameter auffindbar |
| `AddOrderBatch` | ja | mehrere Orders | nein | Orders sind unabhängig, keine OCO-Logik |
| Futures-Brackets | ja (Futures-API) | ja | ja | **nicht Spot**; Hebelprodukt, ausgeschlossen |

Die Komponente setzt deshalb `native_bracket=False` für Kraken Spot
(`live/kraken/gateway.py`), und das Freigabe-Gate sperrt den Live-Handel.

**Unsicherheit:** Die Kraken-Domains waren aus der Entwicklungsumgebung nicht
direkt abrufbar. Die Aussagen stützen sich auf Suchergebnisse aus der offiziellen
Dokumentation, dem Support und dem Blog (Quellen unten). Die Einträge im
Changelog für 2025/2026 waren nur unvollständig sichtbar. **Empfehlung:** beim
Kraken-Support schriftlich nachfragen, ob TP/SL-Brackets mit OCO für Spot per
REST, WebSocket v2 oder FIX verfügbar sind. Bestätigt Kraken einen solchen
API-Parameter, lässt sich `submit_bracket` implementieren. Die übrige Ausführung
(Zustandsmaschine, Abgleich, Sperren) ist bereits darauf ausgelegt und getestet.

### Quellen

- Kraken API, Add Order: https://docs.kraken.com/api/docs/rest-api/add-order
- Kraken API, WebSocket v2 add_order (conditional = OTO): https://docs.kraken.com/api/docs/websocket-v2/add_order
- Kraken API, Add Order Batch: https://docs.kraken.com/api-reference/trading/add-order-batch
- Kraken Support, Conditional Close: https://support.kraken.com/articles/360038640052-conditional-close
- Kraken Support, Bracket-Orders auf Kraken Pro: https://support.kraken.com/articles/bracket-orders-on-kraken-pro
- Kraken Blog, TP/SL für Spot und Margin: https://blog.kraken.com/product/pro/take-profit-stop-loss-spot-and-margin-trading
- Kraken API, Release Notes (u. a. 08.09.2026 AssetPairs-Gebühren veraltet, 16.06.2026 TradeVolume `fee_schedule`): https://docs.kraken.com/api/docs/change-log
- Kraken API, Get Trade Volume: https://docs.kraken.com/api/docs/rest-api/get-trade-volume
- Kraken API, Get Tradable Asset Pairs (`ordermin`, `costmin`, `tick_size`): https://docs.kraken.com/api/docs/rest-api/get-tradable-asset-pairs
- Kraken Support, Mindestordergrößen: https://support.kraken.com/hc/en-us/articles/205893708-Minimum-order-size-volume-for-trading
- Kraken Support, Mindestwert (cost minimum): https://support.kraken.com/articles/12425041458708-cost-minimum-for-trading

## Mindestgrößen und Gebühren

Laut Kraken-Support (nicht live verifiziert; Kraken ändert die Werte ohne Ankündigung):

| Paar | Mindestvolumen (Basis) | ≈ EUR (Kursannahme) | Mindestwert (Quote) | Passt in 10 €? |
|---|---|---|---|---|
| XBT/EUR | 0,0001 BTC | ≈ 6–9 € (bei 60–90 k€) | ≈ 0,45–0,50 EUR | knapp, ein Trade belegt fast das ganze Budget |
| ETH/EUR | 0,01 ETH | ≈ 30 € (bei 3 k€) | ≈ 0,45–0,50 EUR | **nein** |
| SOL/EUR, XRP/EUR | nicht gefunden | – | – | live prüfen |

Die Komponente lädt `ordermin`, `costmin`, `tick_size`, `pair_decimals` und
`lot_decimals` **vor jeder Order** live über `AssetPairs`. Geprüft wird der
Mindestwert für **alle drei Orders**: Einstieg, Stop-Loss zum Stoppkurs und
Take-Profit zum Zielkurs.

**Gebühren:** ausschließlich über `TradeVolume` (privat). `fees` liefert den Taker-,
`fees_maker` den Maker-Satz des Kontos. Ohne erfolgreiche Abfrage gibt es keine
Order. Der Paper-Modus nimmt 0,40 % Taker und 0,25 % Maker an (Kraken-Pro-Grundstufe,
nicht verifiziert).

## Was umgesetzt ist (Anforderungen 1–10)

| # | Anforderung | Umsetzung | Status |
|---|---|---|---|
| 1 | Öffentliche Marktdaten, sicherer authentifizierter Orderzugriff | `live/kraken/client.py`: HMAC-SHA512-Signatur (geprüft gegen das offizielle Kraken-Beispiel), persistente monotone Nonce, Endpunkt-Allowlist, Fehlerklassen *abgelehnt / unbekannt / nicht gesendet* | umgesetzt, nur gegen Emulator getestet |
| 2 | Echtgeld standardmäßig aus, separate Freigabe | `live/gate.py`: CLI-Flag **und** tagesgebundener Freigabetext in `TRADIKG_LIVE_APPROVAL` (`ECHTGELD-FREIGABE EINZELTRADE <Datum> MAX 10.00 EUR <Paar>`). Die Freigabe gilt für **genau einen Trade** und wird beim Senden verbraucht; autonomer Handel ist nicht möglich (siehe `ROADMAP.md`, Phase 4). Zusätzlich Prüfungen auf Bracket, fehlende Auszahlungsrechte und keine Margin | umgesetzt |
| 3 | Globale Obergrenze 10 € inkl. offener Orders/Positionen | `LiveRiskConfig` (harte Obergrenze 10 €, nicht erhöhbar). Exposure wird **aus dem Börsenzustand** berechnet: offene Kauforders + Bestände der Pilot-Assets. Orders auf fremden Paaren → Sperre | umgesetzt |
| 4 | Kein Margin/Hebel/Futures/Short, keine Ein-/Auszahlungen | Parameter `leverage`, `reduce_only`, `settle-position` werden blockiert. Nur Kauf zum Einstieg. Nur Spot-Endpunkte. Withdraw/Deposit/WalletTransfer sind nicht erreichbar. Margin-Positionen führen zur Sperre | umgesetzt |
| 5 | Mindestgrößen, Präzision, Liquidität, Spread, Gebühren, Slippage | `live/precheck.py`: 18 Einzelprüfungen mit Begründung, eine fehlgeschlagene Prüfung verhindert die Order | umgesetzt |
| 6 | Börsenseitiger SL **und** TP für jede Position | **Für Kraken Spot nicht erfüllbar** (siehe Recherche). Der Ablauf ist generisch implementiert (`submit_bracket`), bei Kraken wird er verweigert | **blockiert** |
| 7 | Fehlende Bestätigung, Teilausführung, Timeouts, Neustarts | Zustandsmaschine `live/executor.py` + Write-Ahead-Journal `live/journal.py` (siehe unten) | umgesetzt, im Paper-Modus getestet |
| 8 | Tagesverlust 10 % des Pilot-Kapitals, keine Wiederauffüllung | Limit 1,00 €/UTC-Tag. Neue Trades nur, wenn der Worst Case ins Restbudget passt. Verluste senken die Obergrenze dauerhaft, Gewinne erhöhen sie nicht | umgesetzt |
| 9 | Keine Schlüssel im Repo, keine Withdrawal-Berechtigung | Schlüssel nur aus Umgebungsvariablen oder einer Datei außerhalb jedes Git-Verzeichnisses (Modus 0600). `.gitignore`-Muster. Ein Test durchsucht alle Repo-Dateien nach Schlüsseln. Das Gate prüft per `WithdrawMethods` (erwartet *Permission denied*) | umgesetzt |
| 10 | Erst Integrations- und Paper-Tests, keine eigenmächtige Echtgeldorder | 88 neue Tests + 4 Opt-in-Tests (Emulator über echtes HTTP, Paper-Szenarien). **Es wurde keine Echtgeldorder ausgelöst**, und es gab keinen Kontakt zu einem echten Kraken-Konto | umgesetzt |

### Fehler- und Neustartverhalten (Anforderung 7)

| Situation | Verhalten |
|---|---|
| Order eindeutig abgelehnt (`EOrder:*`, ungültige Argumente) | Trade `REJECTED`, keine Sperre |
| Timeout oder 5xx nach dem Senden | **kein erneutes Senden**. Abgleich über `userref`; gefunden → normal weiter, nicht gefunden → `UNRESOLVED` + **SAFE_HALT** |
| Teilausführung ≥ Mindestvolumen | Schutz muss exakt das ausgeführte Volumen abdecken |
| Teilausführung < Mindestvolumen | Rest ist nicht absicherbar → SAFE_HALT |
| SL/TP nicht innerhalb der Frist bestätigt | SAFE_HALT; ein noch offener *Einstieg* wird storniert, **Schutzorders werden nie storniert** |
| Schutzorder verschwindet (z. B. manuell storniert) | SAFE_HALT |
| SL oder TP ausgelöst, Gegenorder bleibt offen (OCO defekt) | SAFE_HALT |
| Absturz während des Sendens, Neustart | Journal-Eintrag `SUBMITTING`. Ist die Order nicht auffindbar → SAFE_HALT (es wird nicht geraten) |
| Neustart mit geschützter Position | Abgleich mit der Börse, Zustand bleibt erhalten |
| Unbekannte Orders oder Bestände auf dem Konto | SAFE_HALT (der Pilot braucht ein eigenes Sub-Konto) |
| SAFE_HALT | bleibt über Neustarts bestehen. Aufheben nur mit `tradikg kraken reset-halt --note "<Prüfergebnis>"` |

## Offene Risiken

1. **Kein API-Bracket bei Kraken Spot.** Das ist der Hauptblocker (siehe oben).
2. **Kurslücken.** Ein Stop-Loss kann deutlich unter dem Stoppkurs ausgeführt werden. Ein Test zeigt, dass das Tageslimit von 1 € dabei um etwa 9 % überschritten werden kann. Danach sind weitere Trades gesperrt, verhindern lässt sich die Überschreitung aber nicht.
3. **Mindestgrößen gegenüber 10 €.** Ein einziger BTC-Trade belegt fast das ganze Budget. Bei 0,5 € maximalem Verlust pro Trade (inkl. Kosten) darf der Stop nur etwa 4 % unter dem Einstieg liegen. ETH ist ausgeschlossen.
4. **Fixe Kostenquote.** Bei 0,40 % Taker pro Seite und geschätzter Slippage kostet ein Round-Trip etwa 1 %. Das frisst kleine Zielabstände auf. Der Kostenfilter blockiert solche Trades.
5. **Zuordnung der Kraken-Folgeorders.** Wie Kraken automatisch erzeugte Schutzorders in `OpenOrders`/`ClosedOrders` kennzeichnet (Übernahme von `userref`, Verweis auf die Ursprungsorder), ist nicht verifiziert. Das muss vor einer späteren Bracket-Implementierung mit einem echten Konto geprüft werden.
6. **Berechtigungsprobe.** `WithdrawMethods` gilt als Probe für Auszahlungsrechte. Verlangt Kraken dafür nur *Query Funds*, sperrt das Gate fälschlich (fail-closed, also sicher).
7. **Nonce bei mehreren Prozessen** mit demselben Key: Es darf nur eine Instanz laufen. Pro Bot wird ein eigener Key empfohlen.
8. **Abgleich nur per Polling**, kein WebSocket-Executions-Feed. Ein Totmannschalter (`CancelAllOrdersAfter`) wurde bewusst nicht genutzt, weil er auch Schutzorders stornieren würde.
9. **Sub-Konto nötig.** Fremdbestände in BTC/ETH/SOL/XRP lösen eine Sperre aus.

## Optionen (Entscheidung liegt bei dir)

- **A, empfohlen:** Kraken-Support fragen, ob TP/SL-Brackets mit OCO für Spot über die API verfügbar sind. Bei einem Ja den dokumentierten Parameter in `KrakenSpotGateway.submit_bracket` implementieren, gegen ein echtes Sub-Konto mit `validate=true` testen und erneut bewerten.
- **B:** Andere Börse mit dokumentierten Spot-OCO- oder OTOCO-Orders über die API prüfen. Die Gateway-Schnittstelle ist dafür vorbereitet.
- **C, erfüllt Anforderung 6 nicht:** nur Stop-Loss per Conditional Close (OTO) und Take-Profit manuell. Das ginge nur mit einer ausdrücklichen Änderung der Anforderung durch dich und ist **nicht** implementiert.

## Runbook: was du auf deinem Mac prüfen kannst (ohne Echtgeldorder)

```bash
pip install -e ".[dev]"

# 1) Öffentliche Marktregeln (kein Key nötig)
tradikg kraken market-check --pair XBTEUR --pair SOLEUR --pair XRPEUR --pair ETHEUR
KRAKEN_PUBLIC_INTEGRATION=1 pytest tests/live/test_kraken_public_live.py -s

# 2) Kraken-Sub-Konto anlegen, max. 10 € einzahlen. API-Key NUR mit:
#    Query Funds, Query Open Orders & Trades, Query Closed Orders & Trades
#    (für den späteren Live-Test zusätzlich: Create & Modify Orders, Cancel/Close Orders)
#    NICHT: Withdraw Funds, Deposit Funds, Export Data, Earn
export KRAKEN_API_KEY=...            # nur in der Shell, nie in eine Datei im Repo
export KRAKEN_API_SECRET=...

# 3) Nur lesende Prüfung inkl. tatsächlicher Gebühren und Kraken-Validierung (validate=true)
tradikg kraken preflight --pair XBTEUR --validate-order

# 4) Paper-Ablauf mit echten Kraken-Kursen (keine Order an Kraken)
tradikg kraken paper-trade --pair XBTEUR --live-prices --stop-loss <SL> --take-profit <TP>
```

`preflight` endet mit `LIVE READY` oder `NOT LIVE READY` und listet die Gründe auf.
Solange der Bracket-Blocker besteht, lautet das Ergebnis immer `NOT LIVE READY`.

## Tests (in dieser Umgebung ausgeführt)

```
pytest -q            # Python 3.13.16
179 passed, 4 skipped   (91 aus Sprint 1 + 88 neue; 4 = Opt-in-Tests gegen echte Kraken-API)

pytest -q            # Python 3.11.17 / pandas 2.1.4
179 passed, 4 skipped

KRAKEN_PUBLIC_INTEGRATION=1 pytest tests/live/test_kraken_public_live.py
-> FAILED: Tunnel connection failed: 403 Forbidden (api.kraken.com durch Netzwerkrichtlinie blockiert)

ruff check src tests && ruff format --check src tests  -> sauber
```
