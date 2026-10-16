# Roadmap

Stand: 16.10.2026. Jede Phase braucht eine **eigene, ausdrückliche Freigabe**. Eine
Freigabe gilt nur für die Phase und den Umfang, für den sie erteilt wurde.

| Phase | Inhalt | Status |
|---|---|---|
| 1 | Backtesting-Fundament (Daten, Kosten, Baseline, Engine) | umgesetzt (PR #1) |
| 2 | Kraken-Spot-Execution-Komponente, fail-closed | umgesetzt, **NOT LIVE READY** (PR #2) |
| 3 | Einzelner, manuell überwachter Echtgeld-Testtrade (max. 10 €) | blockiert: kein API-Bracket bei Kraken Spot |
| 4 | **Autonomer Handelskreislauf** (siehe unten) | geplant, **nicht implementiert**, separate Freigabe nötig |
| später | Signal-/ML-Modelle, Auswahl Krypto/Aktien, Trade-Analyse, Dashboard | offen |

## Grundsatz: Freigabestufen

| Stufe | Was erlaubt ist | Wie freigegeben | Im Code |
|---|---|---|---|
| Paper | Simulation, keine echten Orders | ohne Freigabe | vorhanden |
| **Einzeltrade** | **genau eine** Echtgeldorder mit Absicherung | CLI-Flag + `ECHTGELD-FREIGABE EINZELTRADE <Datum> MAX 10.00 EUR <Paar>` | vorhanden, wird beim Senden verbraucht |
| **Autonom** | Bot eröffnet selbstständig neue Positionen | eigene, befristete Autonomie-Freigabe (Phase 4) | **existiert nicht** |

Eine Einzeltrade-Freigabe wird **nie** als Erlaubnis für autonomen Handel gewertet.
Der Executor verbraucht sie atomar im Journal, bevor die Order gesendet wird. Ein
zweiter Trade mit derselben Freigabe wird abgelehnt (Test
`test_single_trade_approval_is_consumed_and_not_reusable`). Eine Schleife, die
selbstständig neue Positionen eröffnet, gibt es im Code nicht.

## Phase 4: Autonomer Handelskreislauf (Anforderung vom 16.10.2026)

Nach **einmaliger, ausdrücklicher Aktivierung** soll der Bot ohne Freigabe pro
Order handeln. Sobald eine Position vollständig geschlossen ist:

1. **Netto-Ergebnis ermitteln:** tatsächlicher Gewinn oder Verlust nach allen
   Gebühren, aus den Ausführungsdaten der Börse (Füllkurse, Gebühren), nicht aus
   Schätzungen.
2. **Gewinnaufteilung erst nach Verlustaufholung:** Ein Gewinn gleicht zuerst noch
   nicht aufgeholte Verluste aus. Erst der Rest wird verteilt: **33 %
   Trading-Balance, 33 % Auszahlungsreserve, 34 % Langfristportfolio**.
3. **Kapital-Recycling:** Die Trading-Balance steht automatisch wieder für neue
   Signale zur Verfügung.
4. **Neue Positionen nur, wenn** der geschätzte Erwartungswert nach Kosten positiv
   ist, genug Kapital vorhanden ist und alle Risikolimits eingehalten sind.
5. **Jede neue Position** wird mit einem börsenseitig bestätigten Stop-Loss **und**
   Take-Profit abgesichert. Ohne Bestätigung gilt SAFE_HALT wie heute.

**Für den ersten Echtgeld-Pilot gelten weiter:** maximal 10 € Gesamtbudget und eine
Tagesverlust-Abschaltschwelle von 10 % (1 €).

### Voraussetzungen, bevor Phase 4 überhaupt freigegeben werden kann

- [ ] Börsenseitiger SL/TP-Bracket (OCO) über die API ist bestätigt. Bei Kraken Spot ist das derzeit nicht der Fall, siehe `KRAKEN_LIVE_PILOT.md`.
- [ ] Phase 3 erfolgreich: mehrere Einzeltrades mit bestätigtem Schutz, sauberem Abgleich und korrekter Netto-Abrechnung.
- [ ] Signalquelle mit belastbarer Erwartungswert-Schätzung: Out-of-Sample- bzw. Walk-Forward-Statistik nach Kosten, Mindest-Stichprobe, untere Konfidenzgrenze > 0.
- [ ] Autonomer Kreislauf mehrere Tage im Paper-Betrieb mit Live-Kursen gelaufen, ohne ungeklärten SAFE_HALT.
- [ ] Du hast die Ergebnisse geprüft und erteilst die Autonomie-Freigabe ausdrücklich.

### Geplanter Entwurf (nicht implementiert)

**Autonomie-Freigabe**, getrennt von der Einzeltrade-Freigabe:
- eigener Text, z. B. `AUTONOM-FREIGABE <von> BIS <bis> MAX 10.00 EUR PAARE <…> MAX-TRADES-PRO-TAG <n>`
- **befristet**: Höchstdauer, danach automatisch beendet, keine stillschweigende Verlängerung
- jederzeit **widerrufbar** (Kill-Switch per CLI-Befehl bzw. Datei). Ein Widerruf verhindert neue Positionen; offene Positionen bleiben durch ihren SL/TP geschützt
- jede Aktivierung, Verlängerung und jeder Widerruf wird im Journal protokolliert

**Kapital-Ledger** (Buchhaltung im Journal, persistent):

| Konto | Zufluss | Abfluss |
|---|---|---|
| `trading_balance` | Startkapital; Verlustaufholung; 33 % der verteilten Gewinne | Verluste |
| `unrecovered_losses` | jeder Verlust | Verlustaufholung durch spätere Gewinne |
| `payout_reserve` | 33 % der verteilten Gewinne | nur manuell |
| `long_term_portfolio` | 34 % der verteilten Gewinne | nur manuell |

Ablauf je geschlossener Position mit Netto-Ergebnis `p`:
- Ist `p < 0`: `trading_balance -= |p|` und `unrecovered_losses += |p|`.
- Ist `p > 0`: Zuerst `r = min(p, unrecovered_losses)` zurück in `trading_balance`. Der Rest `p - r` wird 33/33/34 verteilt; Rundung auf Cent, der Rundungsrest geht ins Langfristportfolio.

Die Konten sind **reine Buchhaltung auf demselben Börsenkonto**. Es gibt weiterhin
keine automatischen Auszahlungen, Einzahlungen oder Transfers. Auszahlungsreserve
und Langfristportfolio werden für neue Trades nie verwendet.

**Erwartungswert-Filter:** Jedes Signal braucht eine Schätzung `E[netto] = p_TP · Gewinn_netto − p_SL · Verlust_netto − Kosten`. Geöffnet wird nur, wenn die untere Konfidenzgrenze über 0 liegt. Ohne Schätzung wird nicht gehandelt.

**Ablauf:** Signal → Erwartungswert-Filter → Vorab-Prüfungen (bestehende 18) → Bracket-Order → Bestätigung → Überwachung (Abgleich) → Schließung → Netto-Abrechnung → Ledger → Freigabe der Trading-Balance → nächstes Signal.

### Klärungsbedarf (deine Entscheidung vor Phase 4)

1. **Verhältnis zur 10-€-Obergrenze:** Darf der 33-%-Gewinnanteil die Trading-Balance über 10 € heben, oder bleibt die Obergrenze fix und der Überschuss geparkt? Vorschlag für den Pilot: Obergrenze bleibt **fix 10 €**, der Überschuss wird nicht eingesetzt.
2. **Bisherige Regel „keine automatische Wiederauffüllung“:** Bisher senkt jeder Verlust die Obergrenze dauerhaft. Mit Phase 4 würden spätere Gewinne Verluste aufholen und so die Trading-Balance wieder auffüllen, aber nur aus eigenen Handelsgewinnen, nie durch Einzahlungen oder aus Reserve oder Portfolio. Bitte bestätigen, dass das so gemeint ist.
3. **Langfristportfolio:** Bleibt es eine reine Buchposition (EUR), oder soll es später real investiert werden? Letzteres wäre eine eigene Phase mit eigener Freigabe.
4. **Rahmen der Autonomie:** Höchstdauer der Freigabe, maximale Trades pro Tag, erlaubte Paare.
5. **Steuern:** Gewinne sind ggf. steuerpflichtig. Ob die Auszahlungsreserve eine Steuerrücklage enthalten soll, ist offen.
