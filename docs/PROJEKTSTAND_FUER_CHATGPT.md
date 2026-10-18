# Projektstand Tradikg-Bot – vollständige Beschreibung zur Zweitmeinung

Stand: 18.10.2026. Dieses Dokument fasst das Projekt so zusammen, dass ein anderes KI-System
(z. B. ChatGPT) oder ein Mensch es ohne Zugriff auf den Code prüfen kann. Es enthält keine
Zugangsdaten. Alle Zahlen stammen aus tatsächlich ausgeführten Läufen auf echten Kraken-Daten.

---

## 1. Ziel und Rahmen

- **Nutzer:** Privatperson in Österreich, MacBook Air M2, Kraken-Konto, kleines Budget.
- **Wunsch des Nutzers:** Ein selbstständiger Krypto-Bot, der möglichst viel Gewinn macht, idealerweise
  häufig handelt („Daytrading“) und sich monatlich „selbst finanziert“. Zwischenziel: in ≥ 70 % der
  Zeiträume im Plus, Verlustrisiko möglichst klein.
- **Börse:** Kraken Spot (EUR-Paare XBTEUR, ETHEUR). Kein Hebel, keine Derivate.
- **Gebühren (öffentliche Kraken-Pro-Seite, Stufe 1, < 2.500 USD 30-Tage-Volumen, abgerufen
  17.10.2026):** 0,40 % Maker / **0,80 % Taker** je Order. Ein Kauf + Verkauf kostet also 0,8–1,6 %.
- **Mindestorder Kraken:** BTC 0,00006 (≈ 4,35 € bei 72.500 €), ETH 0,002 (≈ 4,27 €), mind. 0,45 €.

## 2. Technik

- Python 3.12, eigenes Paket `tradikg_bot`, 349 automatische Tests (pytest), Lint mit ruff.
- Repository: GitHub `Linxreen3D/Tradikg-bot`, Arbeitsbranch `mac/first-real-test`, Pull Request #5
  gegen `integration/mac-test` (nicht gemergt).
- Datenhaltung: SQLite unter `~/.tradikg/` (Kerzen, Research, Paper-Konto, Replays, Logs).
- **Daten:** offizielle Kraken-OHLCVT-Daten. Das ~9,5-GB-Komplettarchiv wird per HTTP-Range nur
  teilweise gelesen (nur benötigte CSVs, z. B. 16–21 MB). Verfügbar: BTC ab 09/2013, ETH ab 08/2015,
  SOL ab 06/2021, jeweils 1d, 4h, 1h, 15m (BTC 15m: 448.136 Kerzen). Danach REST-API bis heute.
  Qualitätsbericht: keine ungültigen Kerzen/Duplikate, wenige Lücken in frühen Jahren.
- **Paper-Engine:** simuliert Kraken realistisch – Maker/Taker-Gebühren, Spread, Slippage,
  Mindestorder, Ausführung erst zur nächsten Kerze, SL/TP-Reihenfolge konservativ („Stop zuerst“),
  Ledger 33/33/34 (Gewinnverteilung erst nach Verlustaufholung), Kill Switch, Instanzsperre,
  Neustart ohne Doppelverarbeitung.
- **Werkzeuge:** Walk-Forward-Replay mit Holdout, Research-Engine (Train/Validation/Test,
  Zufallsbaseline), Schnelldurchlauf `tradikg sim` (Bot an einem vergangenen Datum einsetzen und
  vorspulen; Überblick über alle 1-Jahres-Zeiträume seit 2019), Dashboard (Streamlit, lokal).

## 3. Methodik (gegen Selbsttäuschung)

- Entscheidungen nur mit Daten **vor** dem jeweiligen Zeitpunkt (Kausalitätstests im Code).
- Varianten werden **vorher** festgelegt, auf 2016/2019–2022 ausgewählt und **einmal** auf 2023–2026
  geprüft. Gescheiterte Varianten werden nicht nachjustiert.
- Fehler, die im Lauf gefunden wurden (alle behoben und mit Tests abgesichert): zu optimistischer
  Rückkauf derselben Grid-Stufe in derselben Kerze; Doppelzählung bei Teilverkäufen im Live-Modul;
  `paper run --once` ohne Instanzsperre; Risikoregler wurde vom Paper-Dienst nicht übernommen.

## 4. Ergebnisse aller getesteten Ansätze

### 4.1 Ursprünglicher Signal-Bot (7 Strategiefamilien, Bracket SL/TP, Netto-EV-Filter)
- 1d (2 Jahre): 0 Trades – keine Strategie nach Kosten bestätigt. Buy & Hold im selben Zeitraum −47 %.
- 1h/15m/5m: 0 Trades – Kostenfilter blockiert alles (Ziele kleiner als 1,6 % Round-Trip).
- Mit 13 Jahren Historie (1d, Walk-Forward 12/2018–03/2024): 100 € → 126,69 €, 65 Trades, Max. DD −7,6 %
  (Buy & Hold 2.954 €). Nutzte im Schnitt nur 6,56 € je Trade (1 % Risiko bei weitem Stop).
- Schnelldurchlauf ab 10/2025, 10/2024, 01/2023: −6,1 %, −6,3 %, −2,6 %.

### 4.2 Trendfolge-Allokation (aktuell beste Variante)
Regel: BTC und ETH je zur Hälfte. Kauf, wenn Tagesschluss > 200-Tage-Durchschnitt; Verkauf darunter;
Notfall-Stop 20 % unter dem höchsten Schluss seit Einstieg (nachgezogen), danach Sperre bis neues
Kreuzen; Orders als Maker-Limit, sonst Taker. Risikoregler `invest_fraction`.

82 Ein-Jahres-Zeiträume (Start monatlich 01/2019–10/2025), 100 € Start, ohne Cash-Zins:

| investiert max. | Jahre im Plus | Median-Jahr | schlechtestes Jahr | Ø Max. DD (schlimmster) | schlechtester Monat |
|---|---|---|---|---|---|
| 100 % | 76 % | +25 % | −30 % | −24 % (−38 %) | −16 % |
| 50 % | 79 % | +13 % | −16 % | −14 % (−24 %) | −9 % |
| Buy & Hold | 67 % | +45 % | −70 % | – | – |

- Monate im Plus nur ≈ 37 % (Cash-Monate zählen als 0); mit 2 % Cash-Zins ≈ 62 %.
- Variante „Volatilitätssteuerung + monatliche Entscheidung“ wurde auf 2016–2022 gewählt (78 %
  Plus-Monate), fiel aber auf 2023–2026 durch (38 % Plus-Jahre, ≈ 4 % p.a.) → nicht übernommen.
- Einschränkung: Die 200-Tage-Regel ist bekannt und wurde vor dem Einbau auf denselben Daten
  angesehen; kein Parameter optimiert, aber kein unabhängiger Beweis. Letzte 12 Monate ≈ −1 %.

### 4.3 Grid-Daytrading (Netzhandel, nur Paper)
Netz 3 % unter Referenzkurs, 5 Stufen je Coin, Verkauf 3 % über Kaufpreis (Maker), Notbremse −10 %
(Taker), Käufe nur im Aufwärtstrend. Einstellungen auf 2019–2022 gewählt (dort verloren alle
Abstände 1–3 % zwischen 32 und 83 %).
- 12 Monate bis 10/2026: 100 € → 102,05 € (91 Trades). Seit 01/2023: 84,45 € (522 Trades).
- Mit 19 € real nicht möglich (1,90 € je Stufe < Kraken-Mindestorder).

### 4.4 Daytrading-Studie (5 Strategien aus Literatur/Praxis, 15m/1h, je mit/ohne Trendfilter)
Bollinger-Rückkehr (1h), RSI(2)-Connors (1h), Intraday-Momentum nach Shen/Urquhart/Wang 2022 (15m),
Donchian-Ausbruch mit Trailing-Stop (1h), VWAP-Rückkehr (15m). Volles Kapital je Trade, Maker-Einstieg,
Maker-Ziel, Taker-Stop. 100 € (50 € BTC + 50 € ETH):

| Gebühren | Ergebnis |
|---|---|
| Kraken Stufe 1 (0,4/0,8 %) | **alle 10 Varianten → nahezu 0 €** (Train und Test), 0,4–1,6 Trades/Tag |
| Profi (0,1/0,2 %) | alle verlieren: 22–61 € |
| ohne Gebühren | nur Donchian (Train 199 €, Test 150 €) und VWAP (123 €, 131 €) in beiden Zeiträumen positiv |

Fazit: Es gibt einen kleinen Brutto-Effekt, aber die Gebühren eines Privatkunden fressen ihn um ein
Vielfaches auf. Häufiges Handeln verschlechtert das Ergebnis.

## 5. Was aktuell läuft

1. **Paper-Bot (virtuell, 100 €):** Grid-Daytrading auf 1h-Kerzen, BTC/ETH, echte Kraken-Kurse,
   seit 18.10.2026 01:48 UTC. Sichtbar im Dashboard.
2. **Echtgeld-Bot (19 €, gestartet vom Nutzer am 18.10.2026 01:52 UTC):** Trendfolge (Abschnitt 4.2),
   100 % investiert, je ≈ 9,42 € BTC/ETH als Maker-Kauflimit. Sicherheit:
   - API-Key nur mit Abfrage-/Order-Rechten, **ohne Auszahlung** (vom Bot geprüft, sonst Abbruch).
   - Budget hart ≤ 50 €, nur Spot, nur eigene Mengen (Kennung `userref`), idempotent nach Absturz.
   - **Stop-Loss liegt direkt bei Kraken** (funktioniert auch bei schlafendem Mac), wird täglich
     nachgezogen. Kraken-Spot-API kann nur *einen* Folgeauftrag (kein SL+TP-OCO) – deshalb ist nur
     die Trendfolge (braucht keinen Take-Profit) für Echtgeld vorgesehen.
   - Probelauf mit `validate=true` war erfolgreich; Kill Switch vorhanden.

## 6. Ehrliche Gesamteinschätzung

- Mit Kraken-Privatkunden-Gebühren hat keine getestete Daytrading-Strategie einen Vorteil.
- Die Trendfolge mit Risikoregler ist die einzige Variante mit robusten Ergebnissen (76–79 % der
  1-Jahres-Zeiträume im Plus), handelt aber selten (≈ 6–8 Trades/Jahr/Coin) und hat Verlustjahre.
- Mit 20 € ist „Selbstfinanzierung“ nicht realistisch: +25 % Median-Jahr ≈ 5 € pro Jahr.
  Für ≈ 20 € pro Monat bräuchte es im Mittel ≈ 1.000 € Kapital – mit Verlustjahren von bis zu −30 %.
- Nicht unabhängig bestätigt: Der eigentliche Test ist der Vorwärts-Betrieb ab 18.10.2026.

## 7. Fragen an den Prüfer

1. Sind Methodik (Train/Test-Trennung, Kausalität, Gebührenmodell) und Ausführungsmodell plausibel?
2. Übersehen wir einen Ansatz, der bei 0,4/0,8 % Gebühren und 20–100 € Kapital realistisch einen
   Vorteil haben kann?
3. Ist die Echtgeld-Absicherung (Stop bei Kraken, Budgetdeckel, Rechte ohne Auszahlung) ausreichend?
4. Ist die Erwartung (kein verlässliches Monatseinkommen aus kleinem Kapital) zutreffend?

## 8. Wichtige Befehle (zur Nachprüfung)

```bash
tradikg data quality --timeframes 1d,1h,15m       # Datenqualität
tradikg sim --start 2025-01-01 --overview         # Trendfolge-Schnelldurchlauf + 1-Jahres-Überblick
tradikg sim --mode grid --timeframe 1h --start 2023-01-01
tradikg live status                                # Echtgeld-Bot (nur lesend)
scripts/run_tests.sh                               # alle Tests
```

Quellen Recherche: Shen, Urquhart & Wang (2022) „Bitcoin Intraday Time-Series Momentum“, Financial
Review; „On the Intraday Behavior of Bitcoin“, Ledger; Theseus-Abschlussarbeit 2025 (Bollinger vs.
Momentum); Kraken-Gebührenseite und Kraken-API-Doku (AddOrder).

## 9. Nachtrag: Copy-Trading-Simulator (18.10.2026, nur virtuell)

Anlass: Video eines „Copy-Trading-Bots“ (Memecoins auf Solana, 42 $ → 1.885 $ in 24 h, nicht
überprüfbar). Gebaut wurde ausschließlich ein Simulator – keine Wallet, keine Orders.

- **Sammler** `tradikg copy collect`: alle ~2,5 min die 20 trendenden Solana-Pools und je Pool die letzten
  300 Trades (mit Wallet-Adresse) von der öffentlichen GeckoTerminal-API in SQLite. Grenze: bei sehr
  aktiven Pools fehlen Trades zwischen zwei Abrufen (Abdeckung wird im Bericht ausgewiesen).
- **Simulator** `tradikg copy simulate`: Auswahl des besten Traders nur aus Trades der letzten
  3 Stunden (≥ 3 abgeschlossene Runden, Ø ≥ 20 $, keine Hochfrequenz-Bots, ROI > 0); jeder Kauf wird mit
  10 % des Guthabens kopiert, Verkauf mitgemacht, max. 5 Positionen, max. 12 h Haltedauer.
  Ausführung zum **ersten echten Preis nach der Verzögerung** (Standard 10 s); Kosten: DEX 0,25 %,
  Copy-Bot 1 % je Seite, 0,01 $ Netz, Preiseinfluss ≈ Ordergröße / (Reserve/2). Feuern nach 2
  Verlusten in Folge. Vergleich: Trader selbst ohne Verzögerung/Kosten sowie 2/10/30/60 s.
- Erster Lauf nach ~10 min Sammeln: 100 $ → 99,37 $ bei nur 2 kopierten Trades – noch ohne Aussagekraft.
- Offene Frage an den Prüfer: Ist dieses Modell (insbesondere Preis nach Verzögerung, Preiseinfluss,
  Auswahl nach kurzfristigem ROI) realistisch genug, um Copy-Trading von Memecoins fair zu bewerten?

## 10. Nachtrag: Gebühren-Hebel und nächste Entscheidungen

Siehe `docs/PLAN_NAECHSTE_SCHRITTE.md`. Kurz: Daytrading-Strategien (Donchian 1h, VWAP 15m) wären nur
mit One-Tradings „API Trading“-Gebühren (−0,005/0,015 %, Verfügbarkeit für Privatkunden ungeklärt)
in Training und Test positiv; mit 0,5 % Stop-Slippage auch dort nicht. Funding-Carry auf Kraken-Futures
(12 Monate): +3,2–3,5 %, 77 % Monate im Plus, schlechtester Monat −0,28 %. Entscheidungsplan mit
festen Ja/Nein-Kriterien.
