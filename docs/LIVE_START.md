# Echtgeld-Start: Trendfolge mit 20 € auf Kraken

Stand 18.10.2026. Regel wie im Schnelldurchlauf (200-Tage-Durchschnitt, BTC und ETH je 10 €,
Notfall-Stop 20 % unter dem Höchststand **direkt bei Kraken**). Keine Anlageberatung – nur Geld
einsetzen, dessen Verlust du verkraften kannst. Im schlechtesten Jahr seit 2019 hätte die Regel bei
100 % Investitionsgrad rund −30 % verloren (bei 20 € ≈ −6 €).

## 1. Kraken vorbereiten (du)

1. 20 € auf dein Kraken-Konto einzahlen (EUR-Guthaben).
2. Kraken Pro → Einstellungen → **API** → neuen Schlüssel anlegen. Nur diese Rechte (Bezeichnungen
   sinngemäß, Kraken benennt sie gelegentlich um):
   - Guthaben abfragen (*Query Funds*)
   - Offene Orders & Trades abfragen, Geschlossene Orders & Trades abfragen
   - Orders erstellen & ändern (*Create & Modify Orders*)
   - Orders stornieren (*Cancel/Close Orders*)
   - **Alles andere aus** – vor allem **Auszahlen (Withdraw)**, Einzahlen, Earn/Staking, Export.
     Der Bot bricht ab, wenn der Schlüssel Auszahlungsrechte hat.
3. Schlüssel und Secret **nirgends** speichern oder in einen Chat kopieren – nur im nächsten Schritt.

## 2. Schlüssel in den macOS-Schlüsselbund (du, im Terminal)

Jeder Befehl fragt nach dem Wert (Eingabe ist unsichtbar, landet nicht im Verlauf):

```bash
security add-generic-password -U -s tradikg-kraken -a api-key -w
security add-generic-password -U -s tradikg-kraken -a api-secret -w
```

## 3. Probelauf (ohne Ausführung)

```bash
cd ~/TradingBot && source .venv/bin/activate
tradikg live check
```

Erwartet: „Auszahlungsrecht des Keys: nein (gut)“, dein Guthaben (`ZEUR: 20.0`) und für BTC und
ETH je eine Zeile `[PROBE] Kraken hat geprüft (nicht ausgeführt): buy …`. Kraken prüft dabei
Paar, Mindestgröße, Preisformat und Rechte – es wird **nichts** gekauft. Bei „ABBRUCH“ oder
„ABGELEHNT“ nicht weitermachen.

## 4. Echtbetrieb starten (du)

```bash
scripts/start_live.sh 20 1.0
```

Der Prozess läuft im Hintergrund, prüft stündlich (2 Minuten nach der vollen Stunde) und trifft
nach jedem Tagesschluss (00:00 UTC = 02:00 Uhr in Österreich) die Tagesentscheidung.

Was dann passiert:
- Liegen BTC/ETH über dem 200-Tage-Durchschnitt, legt der Bot sofort je ein **Kauf-Limit
  (post-only, 0,40 % Gebühr)** zum letzten Schlusskurs. Wird es bis zum nächsten Tagesschluss
  nicht erreicht, kauft er marktnah (IOC, 0,80 %).
- Spätestens eine Stunde nach dem Kauf liegt ein **Stop-Loss bei Kraken** (20 % unter dem
  Höchststand). Er schützt auch, wenn dein Mac schläft oder aus ist, und wird täglich
  nachgezogen.
- Fällt der Tagesschluss unter den Durchschnitt: Stop stornieren, Verkauf-Limit; wenn bis zum
  nächsten Tagesschluss nicht ausgeführt, Market-Verkauf.

## 5. Beobachten und eingreifen

```bash
tradikg live status        # Mengen, Stops, offene Orders, letzte Log-Zeilen
tradikg live kill          # keine neuen Käufe (Stops bleiben)
tradikg live kill --release
scripts/stop_live.sh       # Prozess beenden – Stops bei Kraken bleiben aktiv
```

Alles ist zusätzlich in Kraken Pro unter „Orders“ sichtbar (Bot-Orders haben eine `userref`).
Notfall: In Kraken Pro offene Orders stornieren und Bestände von Hand verkaufen – der Bot gleicht
das beim nächsten Durchgang ab und verkauft nie mehr, als er selbst gekauft hat.

## Grenzen

- Budget fest 20 € (technisch höchstens 50 €), nur BTC/ETH, nur Spot, kein Hebel.
- Ohne laufenden Mac keine neuen Entscheidungen; der Stop bei Kraken schützt trotzdem.
- Gebühren laut öffentlicher Kraken-Seite (Stufe 1); dein Konto kann abweichen.
- Mindestorder BTC 0,00006 BTC (≈ 4,35 € bei 72.500 €): mit 10 € je Coin erst ab einem BTC-Kurs
  von rund 160.000 € ein Problem (wird dann protokolliert, kein Kauf).

## Sofortkauf (ab 18.10.2026)

Wartende Kauf-Limits durch einen sofortigen, marktnahen Kauf ersetzen (Taker 0,80 % statt
Maker 0,40 %) – nur wenn die Regel ohnehin kaufen würde. Direkt danach setzt der Bot den Stop bei
Kraken. Das Skript stoppt den Bot, kauft, startet ihn wieder (jetzt mit 5-Minuten-Takt):

```bash
scripts/live_buy_now.sh 19 1.0
```

Der Bot schreibt alle 5 Minuten seinen echten Kontostand nach `~/.tradikg/live/snapshot.json`;
das Dashboard zeigt ihn oben im Tab „🔴 Live-Bot“ („💶 Echtgeld bei Kraken“).
