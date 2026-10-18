# Plan: Wie wir die Erfolgschancen wirklich verbessern

Stand 18.10.2026, nachts. Grundlage: alle Tests dieses Projekts (13 Jahre Kraken-Daten, 15m bis 1d)
plus zwei neue Messungen von heute Nacht. Prinzip: **jede Idee bekommt einen Test mit vorher
festgelegtem Ja/Nein-Kriterium** – kein Weiterbasteln, wenn die Zahlen Nein sagen.

---

## 1. Die wichtigste Erkenntnis

Unser Problem ist **nicht** die Strategie, sondern die **Gebühr pro Trade**. Dieselben fünf
Daytrading-Strategien ergeben (100 € Start, Trendfilter, realistische Stop-Ausführung):

| Gebühren (Maker/Taker) | Donchian-Ausbruch 1h | VWAP-Rückkehr 15m |
|---|---|---|
| Kraken Stufe 1 (0,40/0,80 %) – heute | 0,61 € / 0,32 € | 0,22 € / 6,31 € |
| One Trading Start (0,10/0,20 %) | 50,84 € / 34,65 € | 24,47 € / 60,28 € |
| One Trading ab 10.000 € Monatsvolumen (0,04/0,06 %) | 119,20 € / 86,48 € | 65,56 € / 97,75 € |
| **One Trading „API Trading“ (−0,005/0,015 %)** | **161,99 € / 120,34 €** | **109,56 € / 125,59 €** |

(Werte: Training 2019–2022 / Test 2023–09/2026. Stop-Slippage in den Zeilen: 0,5 % / 0,1 % /
0,05 % / 0,05 % – die unteren Zeilen sind also auch bei der Slippage günstiger angenommen.) Nur im letzten Fall sind zwei Strategien **in
beiden Zeiträumen im Plus**. Vorbehalte: Mit sehr vorsichtigen 0,5 % Stop-Slippage verlieren
auch dort alle – real ist die Slippage bei 50-€-Orders aber winzig (Kraken BTC/EUR heute: Spread
0,01 Basispunkte, 127.685 € Tiefe in 10 Stufen). Die Kerze-für-Kerze-Simulation kann trotzdem
nicht jeden Effekt einer echten Ausführung abbilden.

**Offen und entscheidend:** One Trading listet „API Trading: −0,005 % Maker / 0,015 % Taker, alle
Volumen“ ([Gebührenseite](https://www.onetrading.com/fees), [Support](https://support.onetrading.com/hc/en-gb/articles/21953902771473-What-fees-can-I-expect-to-pay-for-trading-on-the-Exchange)),
die API-Dokumentation kennt aber nur die normalen Stufen ([Fee Groups](https://docs.onetrading.com/rest/public/fee-groups)).
Ob ein Privatkunde diese Konditionen bekommt, steht nirgends. **Das klären wir mit einer Frage
an den Support – statt zu raten.** Außerdem lieferte die öffentliche Orderbuch-Schnittstelle von
One Trading heute Nacht nur Fehler; auch das ist zu prüfen.

## 2. Zweite neue Erkenntnis: Funding-Carry (Monate stabil im Plus)

Spot kaufen und gleich viel auf dem Perpetual-Future leerverkaufen → Kursrisiko neutral, man
erhält die Funding-Zahlungen. Kraken-Futures, öffentliche Daten (nur letzte 12 Monate verfügbar):

| | BTC | ETH |
|---|---|---|
| Summe 12 Monate | +3,2 % | +3,5 % |
| Monate im Plus | 77 % | 77 % |
| schlechtester Monat | −0,28 % | −0,14 % |

Das passt am besten zu deinem Wunsch „meiste Monate im Plus, kaum Minus“ – bringt aber wenig
(vor Gebühren; in Bullenjahren ist Funding typischerweise deutlich höher, hier nicht gemessen).
Offen: Ob Kraken-Futures für dich in Österreich verfügbar sind, Mindestgrößen, Sicherheiten für
die Short-Seite (Liquidationsrisiko bei starkem Anstieg ohne ausreichend Sicherheit).

## 3. Entscheidungsplan (in dieser Reihenfolge)

| # | Was | Wer | Ja-Kriterium (vorher festgelegt) | Wenn Nein |
|---|---|---|---|---|
| 1 | One-Trading-Support fragen (Text unten) | **du** (2 Minuten) | Privatkunde bekommt per API ≤ 0,02 % Maker / ≤ 0,03 % Taker | Daytrading endgültig beenden |
| 2 | One-Trading-Anbindung (nur lesen) + Orderbuch/Spread messen | ich | BTC/EUR- und ETH/EUR-Spread ≤ 5 Bp., Tiefe ≥ 5.000 € | Daytrading beenden |
| 3 | Donchian + VWAP als **Paper-Bot mit One-Trading-Kursen**, 3–4 Wochen | ich baue, Bot läuft | nach ≥ 60 Trades netto im Plus **und** im Rahmen des Schnelldurchlaufs | beenden |
| 4 | Erst dann kleines Echtgeld bei One Trading | du entscheidest | – | – |
| 5 | Funding-Carry: Verfügbarkeit Kraken-Futures in deinem Konto prüfen | **du** (Kraken Pro → Futures sichtbar?) | verfügbar, Mindestgröße ≤ 25 € | Idee parken |
| 6 | Copy-Trading-Simulator nach **7 Tagen** Sammeln auswerten | ich | netto im Plus über ≥ 100 kopierte Trades **und** besser als ein zufällig gewählter Trader | Copy-Trading beenden |
| 7 | Echtgeld-Trendfolge (19 €) | läuft | verhält sich wie im Schnelldurchlauf | Kill Switch |
| 8 | Grid-Paper-Bot | läuft | nach 3 Wochen ≥ 100 € | abschalten |

## 4. Was ich abstellen würde

- **Signal-/Bracket-Bot**: in keinem Test nach Kosten ein Vorteil → nicht weiterentwickeln.
- **Daytrading bei Kraken**: Gebührenstufe macht jede getestete Strategie zunichte → nur noch über
  Punkt 1–3 bei One Trading.
- **Echtes Copy-Trading / Memecoins**: frühestens nach Punkt 6 – und nur, wenn der Simulator nach
  allen Kosten klar überzeugt.

## 5. Ehrliche Erwartung

- „Selbst finanzieren“ hängt am Kapital, nicht nur an der Strategie. Beispiel: Selbst +2 % pro Monat
  (sehr gut, nicht garantiert) wären bei 100 € nur 2 € im Monat.
- Der schnellste Hebel für Geld bleibt deshalb nicht der Bot, sondern das, was du schon kannst
  (3D, UGC). Der Bot ist ein Langfristprojekt – und mit den Entscheidungspunkten oben ein ehrliches.

## 6. Text für den One-Trading-Support (zum Kopieren)

> Hello, I am a retail client (EU, Austria) and want to trade BTC/EUR and ETH/EUR via the REST API
> with my own algorithm (small volume, starting around 100–1,000 EUR, roughly 10,000 EUR monthly
> volume). Your fee page lists "API Trading: Maker −0.005 %, Taker 0.015 % for all volumes".
> 1) Does this API fee schedule apply to retail accounts that trade via a personal API key, or is it
> limited to institutional/market-maker accounts? 2) If it requires an application, what are the
> requirements? 3) Are there minimum order sizes for BTC_EUR and ETH_EUR? Thank you!

## 7. Was jetzt schon läuft (keine Handlung nötig)

- Echtgeld-Trendfolge 19 € (Kraken, Stop bei Kraken nach Kauf)
- Grid-Paper-Bot 100 € virtuell
- Copy-Sammler (öffentliche Daten) – Auswertung frühestens in 7 Tagen sinnvoll
- Alles sichtbar im Dashboard-Tab „🎛 Cockpit“
