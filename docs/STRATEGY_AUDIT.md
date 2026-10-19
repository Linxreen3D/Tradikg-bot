# Strategy Quality & Risk Audit – 18.10.2026

Branch `audit/strategy-risk`. Ausgangslage: Echtgeld-Trendfolge mit 19 € (BTC + ETH gekauft am
18.10.2026, Stops bei Kraken), Grid-Paper-Bot (100 € virtuell), Copy-Simulation (100 $ virtuell).
**Keine Echtgeld-Position, keine Order und keine API-Berechtigung wurde verändert.** Der laufende
Echtgeld-Bot läuft unverändert vom alten Stand weiter; Änderungen aus diesem Branch wirken erst nach
Merge **und** Neustart durch dich.

Zahlen: `scripts/strategy_audit.py` (Punkte 1–3, Anhang [STRATEGY_AUDIT_ZAHLEN.md](STRATEGY_AUDIT_ZAHLEN.md))
und `scripts/strategy_audit.py --copy` (Punkt 6). Daten: Kraken-Tageskerzen BTC ab 2013, ETH ab 2015,
1h-Kerzen für Kurslücken, öffentliches Kraken-Orderbuch vom 18.10.2026.

---

## Kurzfassung

| Strategie | Urteil | Begründung (nach Gebühren) |
|---|---|---|
| **Trendfolge SMA 200** | **weiterentwickeln – aber nur beobachten, nicht ausbauen** | 9 von 11 Kalenderjahren im Plus, Testblock 2023–2026 +30,6 % p.a. bei −24,7 % Max. DD (Kaufen & Halten: +26,6 % bei −61,3 %). Über den ganzen Zeitraum aber **nicht** besser risikobereinigt als Kaufen & Halten (Sharpe 1,06 = 1,06) – der Vorteil ist ein kleinerer Drawdown, keine Zusatzrendite. |
| Grid-Daytrading | deaktiviert lassen | seit 2023: 100 € → 84 €; mit 19 € nicht ausführbar |
| Daytrading (5 Strategien) | deaktiviert lassen | bei Kraken-Gebühren alle ≈ 0 €; auch bei 0,1/0,2 % noch negativ – mehr Kapital ändert daran nichts |
| Signal-/Bracket-Bot | deaktiviert lassen | kein Vorteil nach Kosten; mit 19 € nicht ausführbar |
| Copy-Trading (Solana) | **unentschieden, nur Simulation** | mit realistischer Verzögerung 100 $ → 99,85 $ (32 h, 19 Trades); besser als zufällige Wallets (Ø 90 $), aber kein Vorteil nachgewiesen |

Gefundene Schwachstellen und was damit passiert ist:

| # | Befund | Schwere | Status |
|---|---|---|---|
| A | Echtgeld-Bot: Geht die Zustandsdatei verloren, während Stops bei Kraken offen sind, hätte der Bot beim nächsten Tagessignal **ein zweites Mal gekauft** (Budget doppelt) und die alte Position nicht mehr per Signal verkauft. | hoch (selten) | **behoben im Branch** (Bot hält mit Fehlermeldung an), Test |
| B | Die 10-%-Tagesverlust-Schwelle (`DailyLossGuard` / `daily_loss_fraction`) gilt nur für den Bracket-Pilot und die Backtests. Der Trend-Bot hatte keine Tagesgrenze; sein Positionsrisiko bleibt ≈ 21 % (Kurslücke bis ≈ 37 %). | mittel | **neu entschieden 19.10.2026, 21:01 Uhr Wien (Mirko)** – ersetzt die Entscheidung vom 18.10. Optionale Tagesverlustgrenze: Standard 5 % der Equity zum Tagesbeginn, Reset 00:00 Europe/Vienna, im Echtbetrieb standardmäßig **aus**, höchstens 10 % (nur enger). Bei Erreichen nur neue Käufe gesperrt; keine Zwangsverkäufe, Kraken-Stops bleiben. |
| C | Copy-Simulation nimmt 10 s Verzögerung an; unser Sammler sieht Trades erst nach ≥ 100 s (Median). Damit sinkt das Ergebnis von +3,0 % auf −0,2 %. | hoch für die Aussagekraft | gemessen, Punkt 6 |
| D | Copy-Simulation: Ausstieg zum letzten bekannten Preis, wenn ein Pool nicht mehr erfasst wird; Rug Pulls, Verkaufssperren und MEV sind nicht modelliert. | mittel | dokumentiert, Kennzahlen im Bericht |
| E | Trendfolge: Die Sperre „nach Stop erst neues Kreuzen von unten“ hielt den Bot in Bullenjahren fast komplett draußen (2017: 7 % investiert, 2021: 24 %). | Hypothese | **nicht geändert** – nur auf getrennten Daten testen |

---

## 1. Portfolio-Risiko (Echtgeld-Trendfolge)

**Die alte 10-%-Tagesgrenze und der 20-%-Stop gehören zu verschiedenen Bots.** `daily_loss_limit = 0.10`
steht in `config.py` / `live/config.py` und wird nur vom Signal-Backtest und dem alten Bracket-Bot
genutzt. Der Trend-Bot (`live/trend_live.py`) hatte bis zum 19.10.2026 keine eigene Tagesgrenze.
Seit der Entscheidung vom 19.10.2026, 21:01 Uhr Wien, hat er eine **optionale** Grenze (Standard
5 % der Equity zum Tagesbeginn, Reset 00:00 Europe/Vienna, im Echtbetrieb aus, höchstens 10 %).
Sie sperrt nur neue Käufe. Der Schutz innerhalb des Tages bleibt der Stop bei Kraken.

| Ebene | Wert | Herleitung |
|---|---|---|
| **Position** (je Coin, ab Einstieg) | **≈ −21,2 %** im Normalfall | Stop 20 % unter Einstieg/Höchststand + 0,4–0,8 % Kauf- + 0,8 % Verkaufsgebühr |
| Position mit Kurslücke | Median −1,4 %, schlimmster Fall **−16,3 %** zusätzlich | 20 Stop-Fälle seit 2016; Stop → Tief der Auslöse-Stunde (pessimistisch, da Kraken-Stop = Market-Order). Fälle: ETH 02.08.2020 (Flash-Crash bei Kraken) −16,3 %, ETH 10.10.2025 −9,5 %, ETH 14.07.2019 −7,4 % |
| Position, pessimistische Obergrenze | ≈ −37,5 % | −21,2 % + schlimmste gemessene Lücke |
| Schlechtester simulierter Trade | −18,1 % | Simulation mit 0,5 % Stop-Slippage |
| **Tag** (Strategie, 100 % investiert) | −17,1 % schlimmster Tag seit 2016 | Kaufen & Halten 50/50: −41,0 % |
| **Portfolio** Max. Drawdown | −37,6 % (100 %), −23,7 % (50 %) | Trailing-Stop gibt Gewinne bis 20 % wieder ab |
| Korrelation BTC/ETH | 0,67 | beide Stops fallen oft in dieselbe Woche |

Deine 19 € konkret (Stops 58.451,60 € und 1.725,48 € = je 20 % unter Kauf):

| Szenario | Verlust |
|---|---|
| ein Stop greift exakt | ≈ 2,0 € |
| beide Stops greifen exakt | ≈ 4,0 € (≈ 21 %) |
| beide mit der schlimmsten gemessenen Kurslücke | ≈ 7,1 € (≈ 37 %) |
| Totalverlust | ausgeschlossen, solange Kraken funktioniert; mehr als 19 € kann der Bot nicht einsetzen |

Bei 100 / 500 / 1.000 € skaliert das linear: pessimistisch ≈ 37,5 / 188 / 375 €.

**Entscheidung des Nutzers (18.10.2026): Option 1.** Der Echtgeld-Bot bleibt zu 100 % investiert; ≈ 21 % Risiko je Position (pessimistisch ≈ 37 % mit Kurslücke) sind bewusst akzeptiert. Die damalige 10-%-Tagesverlust-Grenze des Bracket-Bots galt nicht für die Trendfolge.

**Entscheidung von Mirko (19.10.2026, 21:01 Uhr Wien): ersetzt die Entscheidung vom 18.10. zu Punkt B.**
Der Trend-Bot bekommt eine eigene Tagesverlustgrenze. Standard sind 5 % der Equity zum Beginn des
Wiener Tages (00:00 Europe/Vienna) – an einem Konto von etwa 20 € ist das rund 1 € an einem schlechten
Tag, normale Schwankungen bleiben möglich, die Zahl wird später nachjustiert. Die Grenze ist
konfigurierbar, aber nur enger als 10 %, und im Echtbetrieb standardmäßig **aus**. Ist sie an und
gerissen, werden neue Käufe bis zum nächsten Wiener Tag gesperrt. Es gibt keine Zwangsverkäufe;
Stops bei Kraken, Kill-Switch, Auszahlungs-Abbruch und die 50-€-Budgetdecke bleiben unverändert.
`DailyLossGuard` im Backtest und `daily_loss_fraction` im Bracket-Pilot (weiter 10 %, mit
Glattstellung im Backtest) sind davon getrennt.

Optionen, die zur Wahl standen:
1. *So lassen*: ≈ 21 % je Position bewusst akzeptieren und die 10-%-Tagesgrenze als „gilt nicht für
   Trendfolge“ dokumentieren.
2. *Risikoregler 50 %* (`invest_fraction 0.5`): getestet (Max. DD −23,7 %, schlechtestes Jahr −4,9 %),
   halbiert alle Werte oben. Bei 19 € liegt ein Anteil von 4,75 € aber knapp über der Mindestorder (4,42 €).
3. *Engerer Stop* (z. B. 10 %): **nicht getestet** – würde öfter ausgelöst und mehr Gebühren kosten;
   nur nach einem Test auf getrennten Daten.

## 2. Robustheit der 200-Tage-Trendfolge

Durchgehender Lauf 03/2016–10/2026 (beide Coins), 100 € Start, Kraken-Gebühren, Details im Anhang.

| | Trend 100 % | Trend 50 % | Kaufen & Halten 50/50 | Kaufen & Halten mit gleichem Ø-Investitionsgrad (32 %) |
|---|---|---|---|---|
| Kalenderjahre im Plus | **9/11** | 9/11 | 7/11 | 7/11 |
| schlechtestes Kalenderjahr | −9,7 % (2022) | −4,9 % | −79,8 % (2018) | −32,7 % |
| Rendite p.a. gesamt | +33,8 % | +19,1 % | +67,8 % | +25,9 % |
| Max. Drawdown | −37,6 % | −23,7 % | −90,9 % | −48,3 % |
| Sharpe / Sortino / Calmar | 1,06 / 0,97 / 0,90 | 1,01 / 0,89 / 0,81 | 1,06 / 1,44 / 0,75 | 1,06 / 1,44 / 0,54 |

Unabhängige Blöcke (Rendite p.a., Max. DD, Sharpe):

| Block | Trend 100 % | Kaufen & Halten 50/50 | gleicher Investitionsgrad |
|---|---|---|---|
| 2016–2019 (Auswahlzeitraum) | +38,0 %, −34,8 %, 1,14 | +107,3 %, −90,9 %, 1,26 | +37,4 %, −48,3 %, 1,26 |
| 2020–2022 (Auswahlzeitraum) | +32,7 %, −29,7 %, 1,03 | +82,4 %, −75,6 %, 1,14 | +31,6 %, −33,1 %, 1,14 |
| **2023–10/2026 (Test)** | **+30,6 %, −24,7 %, 1,00** | +26,6 %, −61,3 %, 0,70 | +11,1 %, −23,8 %, 0,70 |

Bewertung:
- **Belastbar:** In jedem Block deutlich kleinere Drawdowns als Kaufen & Halten, im Testblock auch
  besser risikobereinigt. Das Ergebnis hängt nicht an überlappenden Fenstern.
- **Nicht belastbar:** Über alle Jahre ist die Sharpe-Ratio gleich der von Kaufen & Halten, in den
  Auswahlblöcken sogar etwas schlechter. Die Strategie ist ein Risiko-Reduzierer, keine Rendite-Maschine.
- **Einschränkungen:** Die 200-Tage-Regel ist allgemein bekannt und wurde vor dem Einbau auf diesen
  Daten angesehen; der Testblock ist daher nicht völlig unberührt. Nur zwei stark korrelierte Coins.
  Das Bargeld liegt im Mittel 68 % der Zeit ohne Zins herum.
- **Hypothese E** (nur auf getrennten Daten testen, z. B. SOL oder Kerzen nach dem 18.10.2026): Die
  Sperre nach einem Stop kostet in Bullenjahren den Großteil des Anstiegs (2017: +91 % statt +3.490 %,
  7 % investiert).

## 3. Gebühren und Kapital

Kraken am 18.10.2026: Mindestorder BTC 4,39 €, ETH 4,31 € (mit Gebühr 4,42 €). Spread 0,01 / 0,46 Bp.,
Preiseinfluss einer 1.000-€-Market-Order 0,01 / 0,39 Bp. → **bei 19–1.000 € zählen nur die Gebühren**
(0,8–1,6 % pro Runde); Spread und Slippage sind < 0,05 %.

| Strategie | 19 € | 100 € | 500 € | 1.000 € | Ergebnis nach Kosten |
|---|---|---|---|---|---|
| Trendfolge 100 % | ✅ | ✅ | ✅ | ✅ | Gebühren ≈ 3 % des Kapitals pro Jahr; 6,8 Trades/Jahr |
| Trendfolge 50 % | ✅ (knapp: 4,75 € je Coin) | ✅ | ✅ | ✅ | halbes Risiko, halbe Rendite |
| Grid (5 Stufen) | ❌ (1,90 € je Stufe) | ✅ | ✅ | ✅ | negativ seit 2023 |
| Daytrading | ✅ | ✅ | ✅ | ✅ | ausführbar, aber bei jeder Kapitalgröße negativ |
| Signal-Bot | ❌ (1,27 € je Trade) | ✅ | ✅ | ✅ | kein Vorteil |

In Euro (Trendfolge 100 %; Median der 1-Jahres-Zeiträume +25 %, schlechtester −30 %, schlechtestes
Kalenderjahr −9,7 %):

| Kapital | typisches Jahr (Median) | schlechtester 1-Jahres-Zeitraum |
|---|---|---|
| 19 € | +4,75 € | −5,70 € |
| 100 € | +25 € | −30 € |
| 500 € | +125 € | −150 € |
| 1.000 € | +250 € | −300 € |

Kein Kapitalbetrag macht Daytrading bei Kraken profitabel: Die Studie verliert schon bei 0,1/0,2 %
Gebühren. Mit 1.000 € und ≈ 1 Runde pro Tag (≈ 60.000 € Monatsvolumen) käme man in eine niedrigere
Kraken-Stufe – deren genaue Sätze habe ich nicht live geprüft; dass sie unter 0,1/0,2 % liegen,
ist bei diesem Volumen nicht zu erwarten.

## 4. Strategy-Health-Ansicht (Dashboard, neuer Tab „🩺 Strategy-Health“)

- Echtgeld: investiert / frei / Obergrenze / Auslastung.
- **Ausführung gegen Plan:** jeder Kauf/Verkauf mit Entscheidungskurs (Tagesschluss), Abweichung,
  Gebühr in %. Aktuell: Gebühren Ø 0,80 % statt geplant 0,40 % (Sofortkauf als Taker), gekauft
  Ø 0,88 % über dem Tagesschluss, zu dem der Test kauft.
- **Performance gegen den Test:** aktuelles Ergebnis als Perzentil der historischen Zeiträume gleicher
  Länge (nur Zeiten mit beiden Coins im Bestand), Warnung unter 5 %.
- Letzte Signale; abgelehnte/übersprungene Aktionen und Abbrüche mit Grund (aus `live.log`).
- Nachanalyse abgeschlossener Trades (Punkt 5) für Echtgeld und Paper-Bots.
- Paper-Bot: letzte Signale und Ablehnungen nach Grund; Audit-Urteil je Strategie.
- Außerdem: Im Live-Tab heißt „Budget 19 €“ jetzt „Obergrenze Bot … (kein Kontostand)“ – das war
  die Verwechslung mit den 19,75 €.

## 5. Nachanalyse im Trade-Journal

`trading/analysis.py`: `classify_trade` ordnet jeden abgeschlossenen Trade genau einer Ursache zu –
**Gebührenproblem** (brutto +, netto −), **Ausführungsfehler** (> 0,5 % schlechter als der
Entscheidungskurs), **Prognosefehler** (Verlust schlechter als die schlechtesten 10 % der Verluste im
Test, derzeit −8,2 %), sonst **normaler statistischer Verlust**. `hypotheses` sammelt daraus
Verbesserungsideen mit dem festen Zusatz „nur auf getrennten Daten testen“; nichts wird automatisch
aktiviert. Der bestehende Bracket-Bot-Bericht (`analyze_trade`) bleibt unverändert.

## 6. Copy-Trading-Simulation

Daten: 153.833 öffentliche Trades aus 26 trendenden Solana-Pools, 17.10. 02:13 – 18.10. 10:37 UTC
(≈ 32 Stunden). Ergebnisse 100 $ Start, alle Kosten wie bisher (DEX 0,25 %, Copy-Bot 1 % je Seite,
0,01 $ Netz, Preiseinfluss).

| Variante | Endwert | Trades | Gewinner |
|---|---|---|---|
| Beste Wallets, 10 s Verzögerung (bisherige Annahme) | 103,02 $ | 19 | 8 |
| Beste Wallets, **100 s** (gemessene Untergrenze, Median) | **99,85 $** | 19 | 8 |
| Beste Wallets, 244 s (gemessene Untergrenze, 90 %) | 98,35 $ | 19 | 6 |
| Zufällige aktive Wallets, 10 s (5 Durchläufe) | 84,44–96,70 $ (Ø 90,15 $) | 30–35 | 2–15 |

Prüfung der einzelnen Punkte:

| Punkt | Befund | Wirkung |
|---|---|---|
| **Verzögerte Transaktionen** | Unser Sammler fragt jeden Pool nur alle ≈ 276 s ab und sieht einen Trade frühestens nach ≥ 100 s (Median, Untergrenze; der gespeicherte Abruf-Zeitstempel ist der Start des Durchgangs). Die Simulation nahm 10 s an. | **zu optimistisch**: bei realistischer Verzögerung ≈ ±0 statt +3 % |
| Blick in die Zukunft | Auswahl nur aus Trades vor dem Zeitpunkt ✅, Preise erst nach Verzögerung ✅. Aber: Der Kauf des Vorbilds wurde zum Blockchain-Zeitpunkt „gesehen“, nicht zum Abruf-Zeitpunkt (siehe oben). Preiseinfluss nutzt die **aktuelle** Pool-Reserve statt der damaligen. | ersterer Punkt quantifiziert, zweiter klein |
| Unrealistische Wallet-Performance | Rangliste nach ROI aus 3 Stunden über tausende Wallets → Glückstreffer werden ausgewählt. Gegenprobe: zufällige aktive Wallets verlieren 3–16 %. Die Auswahl filtert also Verlierer heraus, ein echter Vorsprung nach Kosten ist **nicht** nachgewiesen (19 Trades, 32 Stunden). | Gate am 25.10. nur gegen Zufall und mit ≥ 100 s auswerten |
| Survivorship Bias | Erfasst werden nur Pools, die *gerade* trenden. Fällt ein Pool aus der Liste, gibt es keine Preise mehr: 5 von 19 Ausstiegen nutzten einen veralteten Preis. Stürzt ein Token danach ab (Rug Pull), sieht die Simulation das nicht. | zu optimistisch, Größe unbekannt |
| Slippage / Liquidität | Preiseinfluss ≈ Ordergröße / halbe Reserve; bei 10 $ in Pools mit ≥ 10.000 $ Reserve < 0,2 %. 8 % der Abrufe ohne Überlappung → dazwischen verpasste Trades (auch Verkäufe des Vorbilds). | klein bei 100 $, wächst mit Kapital |
| MEV / Sandwich | nicht modelliert (angenommen 0). Kauf-Orders mit Slippage-Toleranz werden auf Solana regelmäßig „gesandwicht“. | zu optimistisch |
| Rug Pulls / Verkaufssperren | nicht modelliert: Die Simulation kann immer zum Marktpreis verkaufen (keine Honeypots, Freeze-Authority, Transfer-Steuern). In allen kopierten Pools gab es nach unserem Einstieg noch Verkäufe anderer. | zu optimistisch, in diesen Daten nicht beobachtet |
| Netzgebühr | pauschal 0,01 $; in heißen Memecoin-Phasen sind Prioritäts-Gebühren/Tips oft höher. | leicht zu optimistisch |

Neu im Code: `CopyConfig(random_seed=…)` wählt zufällige aktive Wallets (gleiche Mindestanforderungen,
ohne ROI-Filter) als Vergleichsmaßstab für das Gate; Test vorhanden. **Nicht** geändert: die
Standard-Verzögerung der Live-Simulation (bleibt 10 s, damit die laufende Kurve vergleichbar bleibt) –
für die Gate-Bewertung gilt aber die gemessene Verzögerung.

**Fazit:** Nach realistischer Verzögerung ist das Copy-Ergebnis bisher **null** (99,85 $), besser als
Zufall, aber ohne Nachweis eines Vorteils. Mehrere Risiken (MEV, Rug Pulls, Survivorship) machen die
Simulation eher noch zu optimistisch. Kein Echtgeld.

## 7. Keine erzwungenen Trades

Geprüft: Keine Strategie hat eine Mindestanzahl an Trades oder ein Handelsziel. `max_positions` (Copy)
und der Anteil je Coin (Trend) sind Kapitalgrenzen; `max_trades` im Copy-Simulator schließt nur
Hochfrequenz-Bots als Vorbilder aus; `min_trades_select` der Research-Engine ist eine statistische
Mindestprobe für die *Auswahl*, kein Handelszwang. Neuer Test
`test_no_forced_trades_and_position_never_exceeds_risk_cap`: ohne Signal 0 Trades; mit Signal nie
mehr als der erlaubte Anteil.

## 8. Wiederanlauf nach Mac-Ausfall, Neustart, API-Timeout

Neue Tests in `tests/live/test_trend_live.py` gegen eine simulierte Kraken-Börse:

| Fall | Ergebnis |
|---|---|
| Neustart mit Zustandsdatei, Stops offen | ✅ Stops unverändert (gleiche txid, Preis, Menge), nichts neu gesendet |
| Stop während Mac aus ausgeführt | ✅ Verkauf verbucht, Sperre gesetzt, kein Rückkauf |
| API-Timeout mitten im Durchgang | ✅ nichts geändert, Zustandsdatei unverändert; nächster Durchgang zieht Stop korrekt nach |
| Timeout beim Stornieren eines Stops (Nachziehen) | ✅ nächster Durchgang setzt wieder einen Stop (Lücke höchstens bis zum nächsten 5-Minuten-Durchgang) |
| Absturz zwischen Senden und Speichern | ✅ gleiche Kennung (userref) → Order wird übernommen, nicht doppelt gesendet |
| **Zustandsdatei verloren, Bot-Orders offen** | ❌ → ✅ **behoben**: Bot hält mit Hinweis an statt erneut zu kaufen |
| Zustandsdatei beschädigt | ✅ Bot startet nicht (Fehler), Stops bei Kraken bleiben aktiv |

Bekannte Grenze: Schläft der Mac, werden Signal-Ausstiege (Schluss unter SMA 200) verpasst; der Stop
bei Kraken schützt weiter. Die einzige Code-Änderung am Echtgeld-Bot ist die Prüfung in
`_refuse_if_state_lost` (nur beim Start ohne Zustandsdatei, keine Handelslogik geändert).

## 9. Welche Strategie zeigt nach Gebühren einen belastbaren Vorteil?

- **Belastbar (eingeschränkt):** Trendfolge SMA 200 – als *Risikobegrenzung* gegenüber Kaufen &
  Halten (Drawdown halbiert bis gedrittelt, 9/11 Jahre im Plus). Keine belastbare Mehrrendite.
- **Unentschieden:** Copy-Trading – Datenbasis 1–2 Tage, wichtige Kosten/Risiken nicht modellierbar.
- **Deaktiviert lassen:** Grid, alle Daytrading-Varianten, Signal-/Bracket-Bot.

## 10. Priorisierung

| Prio | Maßnahme | Finanzieller Nutzen | Sicherheit | Aufwand | Wer |
|---|---|---|---|---|---|
| 1 | Branch mergen und Echtgeld-Bot neu starten (Zustandsverlust-Schutz A) | – | hoch | klein | du (Freigabe) |
| 2 | ~~Risiko je Position festlegen (B)~~ – erledigt: 100 %, ≈ 21 % akzeptiert | – | – | – | du ✅ |
| 3 | Copy-Gate (Tag 7) nur mit gemessener Verzögerung und gegen Zufalls-Wallets auswerten (C) | verhindert Fehlentscheidung | mittel | klein (gebaut) | ich, am 25.10. |
| 4 | Mac nicht schlafen lassen, solange der Bot läuft (Energie-Einstellungen) | kleine | mittel | keiner | du |
| 5 | Hypothese E auf getrennten Daten testen | möglich, unbewiesen | – | mittel | später |
| – | Neue Strategien | – | – | – | erst nach 1–3 |

**Technische Empfehlung:** Nur die Trendfolge weiterführen – unverändert, mit dem
Zustandsverlust-Schutz und einer bewussten Risiko-Entscheidung (Punkt 1). Kein Ausbau, keine neuen
Strategien, bis der Vorwärtsbetrieb einige Monate läuft. Grid, Daytrading und Signal-Bot bleiben aus.
Copy-Trading bleibt reine Simulation; Echtgeld dort frühestens, wenn das Gate mit realistischer
Verzögerung **und** besser als Zufall besteht – und selbst dann nur mit dem Wissen, dass Rug Pulls,
Verkaufssperren und MEV in der Simulation fehlen.
