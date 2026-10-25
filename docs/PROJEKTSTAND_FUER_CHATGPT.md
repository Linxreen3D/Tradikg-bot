# Projektstand Tradikg-Bot – Briefing für ChatGPT

Stand: 18.10.2026, 12:15 Uhr Wiener Zeit. Bitte lies alles, bevor du antwortest. Alle Zahlen stammen
aus tatsächlich ausgeführten Läufen auf echten Kraken-Daten bzw. aus dem laufenden Betrieb.
Keine Zugangsdaten enthalten.

**Deine Rolle:** Du bist mein kritischer Sparringspartner. Ich will mit dir besprechen, was wir als
Nächstes machen. Bitte sei ehrlich, rechne nach, und sag klar, wenn eine Idee nach Gebühren nicht
aufgeht. Keine Versprechen, keine Anlageberatung – mir geht es um eine nüchterne Bewertung.

---

## 1. Wer ich bin, was ich will

- Privatperson in Österreich, MacBook Air M2, Kraken-Konto, kleines Kapital (aktuell 19 € echt).
- Wunsch: ein selbstständiger Krypto-Bot, der oft handelt („Daytrading“, kleine Chancen nutzen),
  idealerweise wie die Copy-Trading-Bots aus Videos (Memecoins auf Solana), mit möglichst wenig
  Verlustrisiko. Ursprüngliches Zwischenziel: in ≥ 70 % der Zeiträume im Plus.
- Vorstellung „jeder Trade soll nur die Gebühren reinholen und mit Plus rausgehen“ – siehe Abschnitt 7,
  warum das so nicht funktioniert.

## 2. Technik (kurz)

- Python 3.12, eigenes Paket `tradikg_bot`, 365 automatische Tests, Lint mit ruff.
  GitHub `Linxreen3D/Tradikg-bot`, Arbeitsbranch `mac/first-real-test`, Pull Request #5 (nicht gemergt).
- Daten: offizielle Kraken-OHLCVT-Daten (BTC ab 2013, ETH ab 2015, SOL ab 2021; 1d/4h/1h/15m),
  danach Kraken-REST bis heute. SQLite unter `~/.tradikg/`.
- Werkzeuge: Paper-Engine (Kraken-Gebühren, Spread, Slippage, Mindestorder, Ausführung erst zur
  nächsten Kerze, Stop vor Ziel), Walk-Forward-Replay, Research-Engine mit Train/Test-Trennung und
  Zufallsbaseline, Schnelldurchlauf `tradikg sim` (Startdatum wählen, vorspulen, Überblick über alle
  1-Jahres-Zeiträume), Streamlit-Dashboard mit Live-Ansicht.
- Methodik: Entscheidungen nur mit Daten vor dem jeweiligen Zeitpunkt; Varianten werden auf
  2016/2019–2022 gewählt und **einmal** auf 2023–2026 geprüft; gescheiterte Varianten werden nicht
  nachjustiert.

## 3. Gebühren – der entscheidende Faktor

- **Kraken Spot Stufe 1:** 0,40 % Maker / 0,80 % Taker je Order → Kauf + Verkauf = **0,8–1,6 %**.
- Mindestorder Kraken: ≈ 4–5 € je Coin. Spread BTC/EUR heute ≈ 0,0001 %, Tagesumsatz 8,3 Mio. €.
- **One Trading (Wien)** wurde als günstigere Börse geprüft (0,10/0,20 %, „API Trading“
  −0,005/0,015 % gelistet). **Ergebnis 18.10.2026:** Der Euro-Spot-Handel (BTC/EUR, ETH/EUR) ist dort
  **geschlossen** (letzte Kerze 18.01.2026). Aktiv sind nur BTC/USDC und ETH/USDC (Spread 0,04–0,05 %,
  Tagesumsatz nur ≈ 650 bzw. 7.400 USDC), der Euro-Zugang USDC/EUR hat ein leeres Orderbuch.
  Sonst nur Futures (mit Hebel). → Für uns aktuell keine Alternative. Eine Support-Anfrage läuft noch.

## 4. Ergebnisse aller getesteten Ansätze

### 4.1 Signal-Bot (7 Strategiefamilien, Stop/Ziel, Kostenfilter) – verworfen
1h/15m/5m: 0 Trades, weil jedes Ziel kleiner als die Kosten war. 1d über 13 Jahre: 100 € → 127 €
(Buy & Hold: 2.954 €). Schnelldurchläufe ab 2023/2024/2025: −2,6 % bis −6,3 %.

### 4.2 Trendfolge mit 200-Tage-Linie – beste Variante, läuft mit Echtgeld
Regel: BTC und ETH je zur Hälfte; kaufen, wenn Tagesschluss über dem 200-Tage-Durchschnitt; verkaufen
darunter; Notfall-Stop 20 % unter dem Höchststand seit Kauf (nachgezogen); Maker-Limit, sonst Taker.

82 Ein-Jahres-Zeiträume (Start monatlich 01/2019–10/2025), 100 € Start:

| investiert max. | Jahre im Plus | Median-Jahr | schlechtestes Jahr | schlechtester Monat |
|---|---|---|---|---|
| 100 % | 76 % | +25 % | −30 % | −16 % |
| 50 % | 79 % | +13 % | −16 % | −9 % |
| Buy & Hold | 67 % | +45 % | −70 % | – |

- Nur ≈ 37 % der **Monate** im Plus (Monate ohne Position zählen als 0). „≥ 70 % Monate im Plus“
  ist damit nicht erreichbar. Letzte 12 Monate ≈ −1 %.
- Eine Variante mit Volatilitätssteuerung sah auf 2016–2022 gut aus, fiel auf 2023–2026 durch → verworfen.
- Einschränkung: Die 200-Tage-Regel ist allgemein bekannt; kein Parameter optimiert, aber auch kein
  unabhängiger Beweis. Der echte Test ist der Vorwärtsbetrieb.

### 4.3 Grid-Daytrading (Netz aus 5 Kauf-Stufen im Abstand 3 %, Verkauf 3 % höher) – kein Vorteil
12 Monate: 100 € → 102 € (91 Trades). Seit 01/2023: 100 € → 84 € (522 Trades). Mit 19 € nicht machbar
(1,90 € je Stufe < Mindestorder).

### 4.4 Daytrading-Studie (5 Strategien aus Literatur/Praxis, 15m/1h)
Bollinger-Rückkehr, RSI(2), Intraday-Momentum (Shen/Urquhart/Wang 2022), Donchian-Ausbruch, VWAP-Rückkehr.

| Gebühren | Ergebnis (100 € Start, Train 2019–22 und Test 2023–26) |
|---|---|
| Kraken (0,4/0,8 %) | **alle Varianten → nahezu 0 €** |
| 0,1/0,2 % | alle verlieren (22–61 €) |
| ohne Gebühren | nur Donchian und VWAP in beiden Zeiträumen positiv |
| −0,005/0,015 % (One-Trading-API) | Donchian/VWAP positiv – aber nur bei sehr geringer Slippage |

Fazit: Es gibt einen kleinen Brutto-Effekt, die Gebühren eines Privatkunden fressen ihn aber um ein
Vielfaches auf. Häufiger handeln = schlechter. Da One Trading keinen Euro-Spot mehr anbietet, ist
Daytrading laut unserem vorher festgelegten Plan beendet.

### 4.5 Funding-Carry (Idee, nicht gebaut)
Spot kaufen + gleich viel auf dem Perpetual-Future leerverkaufen → kursneutral, man kassiert Funding.
Kraken-Futures, letzte 12 Monate: +3,2 % (BTC) / +3,5 % (ETH), 77 % der Monate im Plus, schlechtester
Monat −0,28 % (vor Gebühren). Offen: Verfügbarkeit von Kraken-Futures in Österreich für mich,
Mindestgrößen, Sicherheiten/Liquidationsrisiko auf der Short-Seite.

### 4.6 Copy-Trading (Memecoins auf Solana) – nur virtuell
- Sammler: alle 2,5 min die 20 trendenden Solana-Pools + je 300 letzte Trades mit Wallet-Adresse
  (öffentliche GeckoTerminal-API). Lücken bei sehr aktiven Pools möglich.
- Simulator: bewertet Wallets nur mit Trades der letzten 3 Stunden (abgeschlossene Runden, ROI,
  Trefferquote), folgt den besten 3, prüft alle 30 min neu, kopiert jeden Kauf mit 10 % des Guthabens
  zum **ersten echten Preis nach 10 s Verzögerung**; Kosten: DEX 0,25 %, Copy-Bot-Gebühr 1 % je Seite,
  0,01 $ Netz, Preiseinfluss; feuert eine Wallet nach 2 Verlusten in Folge.
- Rückblick über die Nacht: 100 $ → 102,80 $ (19 Trades) – kurzer Zeitraum, keine Aussagekraft.
- Live-Simulation seit 18.10. 11:41 Wiener Zeit: **99,17 $, 0 abgeschlossene Trades, 2 offene**.
- Echtes Copy-Trading bräuchte eine Solana-Wallet mit privatem Schlüssel und DEX-Anbindung; Kraken
  handelt diese Coins nicht. Prüfpunkt: nach 7 Tagen ≥ 100 kopierte Trades netto im Plus **und**
  besser als eine zufällig gewählte Wallet – sonst beenden.

## 5. Was gerade läuft

1. **Echtgeld-Trendfolge (19 € Budget, Kraken):** gekauft am 18.10. 11:58 Wiener Zeit
   – BTC 0,00012834 zu 73.064,50 € (Gebühr 0,075 €), Stop bei Kraken 58.451,60 €
   – ETH 0,00434782 zu 2.156,86 € (Gebühr 0,075 €), Stop bei Kraken 1.725,48 €
   – Wert laut API jetzt ≈ 18,84 € (Differenz = Gebühren + Spread). Der Bot prüft alle 5 Minuten;
     verkauft wird nur, wenn der Tagesschluss unter die 200-Tage-Linie fällt oder der Stop greift.
   – Sicherheit: API-Key ohne Auszahlungsrecht (Bot bricht sonst ab), Budget hart ≤ 50 €, nur Spot,
     Stops liegen bei Kraken (wirken auch bei ausgeschaltetem Mac), Kill Switch, keine doppelten
     Orders nach Absturz.
2. **Grid-Paper-Bot (100 € virtuell):** seit 18.10. nachts, bisher keine abgeschlossenen Trades (Kontostand 100,00 €).
3. **Copy-Live-Simulation (100 $ virtuell):** siehe 4.6.
4. **Dashboard:** zeigt alle drei live (Echtgeld mit Grafiken, Copy-Bot im Stil der Videos).

## 6. Entscheidungsplan (vorher festgelegte Ja/Nein-Kriterien)

| # | Was | Status |
|---|---|---|
| 1 | One-Trading-Support: API-Gebühren für Privatkunden? | Frage wird gesendet, Antwort offen |
| 2 | One-Trading-Orderbuch messen (Spread ≤ 5 Bp., Tiefe ≥ 5.000 €) | **❌ Euro-Spot geschlossen** |
| 3 | Daytrading-Paper-Bot mit One-Trading-Kursen | entfällt wegen 2 |
| 5 | Kraken-Futures im Konto verfügbar? (für Funding-Carry) | ich prüfe |
| 6 | Copy-Trading nach 7 Tagen auswerten (≈ 25.10.) | läuft |
| 7 | Echtgeld-Trendfolge verhält sich wie im Test | läuft |
| 8 | Grid-Paper nach 3 Wochen ≥ 100 € | läuft, sonst abschalten |

## 7. Was wir gelernt haben (meine ehrliche Zusammenfassung)

- „Nur mit Plus rausgehen“ geht nicht: Ob ein Trade aufgeht, weiß man vorher nicht. Wer nie mit
  Verlust verkauft, hat sein Geld irgendwann in fallenden Coins festsitzen (genau das passiert im Grid-Test).
- Bei 0,8–1,6 % Kosten pro Runde braucht jede Strategie Bewegungen deutlich größer als das – deshalb
  gewinnt seltener Handel (Trendfolge) und verliert häufiger Handel.
- Mit 19–100 € ist kein spürbares Einkommen möglich: selbst +25 % im Jahr wären bei 100 € 25 €.
- Videos mit 42 $ → 1.885 $ in 24 h sind nicht überprüfbar; unser Simulator rechnet Verzögerung und
  Gebühren ehrlich mit.

## 8. Fragen, die ich mit dir besprechen will

1. Was sollte ich als Nächstes tun: Trendfolge einfach laufen lassen, Funding-Carry bauen, Copy-Trading
   weiter beobachten – oder etwas ganz anderes?
2. Gibt es einen Ansatz, der bei 0,4/0,8 % Gebühren und 20–100 € Kapital realistisch einen Vorteil haben
   kann? Oder eine EU-Börse mit Euro-Spot und deutlich niedrigeren Gebühren für Privatkunden per API?
3. Ist unser Copy-Trading-Modell (Preis nach Verzögerung, Preiseinfluss, Auswahl nach kurzfristigem ROI)
   realistisch genug? Welche Risiken übersehen wir bei Memecoins (Rug Pulls, Bots, MEV/Sandwich)?
4. Ist die Echtgeld-Absicherung ausreichend?
5. Ab welchem Kapital und mit welcher Erwartung lohnt sich das Ganze überhaupt?
