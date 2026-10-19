# Übergabe: Mac-Bot aus, Linux-Bot an

Beide Bots nutzen **dasselbe Kraken-Konto** und **verschiedene API-Keys**. Der Mac kann BTC und ETH
halten, mit offenen Stop-Loss-Orders, deren `userref` vom Bot stammt. Der Linux-Bot übernimmt diese
Orders. Er kauft sie nicht noch einmal und legt keinen zweiten Stop.

Nichts in dieser Checkliste startet den Linux-Bot, solange du den letzten Schritt nicht selbst
machst. Keine Schlüssel in den Chat, ins Repo oder in die Unit-Datei. Schritte für den Rechner
selbst: `docs/LINUX_LIVE.md`.

## 1. Mac anhalten

Auf dem Mac, im Repo:

```bash
tradikg live kill
scripts/stop_live.sh
```

`tradikg live kill` legt `~/.tradikg/live/KILL_SWITCH` an. Neue Käufe sind aus, die Stops bei Kraken
bleiben. `scripts/stop_live.sh` beendet den Prozess, den `scripts/start_live.sh` per `nohup`
gestartet hat (SIGTERM auf die PID in `~/.tradikg/live/live.pid`).

## 2. Prüfen, dass der Mac wirklich steht

- `ps aux | grep 'tradikg live run'` zeigt keinen laufenden Bot.
- Die PID aus `live.pid` ist tot, oder die Datei ist weg (`stop_live.sh` löscht sie).
- `~/.tradikg/live/live.log` bekommt ein paar Minuten lang keine neuen Zeilen.
- `KILL_SWITCH` bleibt liegen, damit ein versehentlicher Neustart auf dem Mac nicht kauft.

`live.pid` und `live.lock` gehören nur zu diesem Rechner. Nicht auf den Linux-Rechner kopieren.

## 3. Zustand kopieren

Nur diese Datei:

```text
~/.tradikg/live/trend_state.json
```

Optional zusätzlich `~/.tradikg/live/heartbeat.json`. Eine frische Heartbeat-Datei vom Mac blockiert
den Echtstart auf dem Linux-Rechner für 15 Minuten. Das ist gewollt, wenn der Mac gerade noch
geschrieben hat. Eine alte Datei blockiert nicht.

Nicht kopieren: `live.pid`, `live.lock`, `kraken.env`, API-Keys.

Auf dem Linux-Rechner, ohne den Dienst zu starten:

```bash
tradikg live import /pfad/zur/kopierten/trend_state.json
```

Der Befehl prüft die Form der Datei und legt sie nach `$TRADIKG_HOME/live/trend_state.json`.
Er liest keine API-Keys und sendet keine Order. Eine unlesbare oder unsinnige Datei wird abgelehnt.
Liegt schon eine Datei am Ziel, brauchst du `--replace`.

Die Datei wird danach nicht blind geglaubt. Mengen, die keine offene Bot-Order erklären, zählt der
Bot nicht. Fremde BTC/ETH auf dem Konto (ohne Bot-`userref`) bleiben außen vor. Alles zusammen muss
in die eigene Scheibe von 20 € passen.

Ohne Datei geht es auch: `tradikg live check` baut den Zustand aus offenen Orders mit Bot-`userref`.
Eine Stop-Menge ist die Bot-Position. Der ganze Kontostand ist es nicht.

## 4. Probelauf und Vergleich

```bash
systemd-run --user --pipe --wait \
  --property=Environment=TRADIKG_HOME="${TRADIKG_HOME:-$HOME/.tradikg}" \
  --property=EnvironmentFile="$HOME/.config/tradikg/kraken.env" \
  --working-directory="$PWD" \
  "$PWD/.venv/bin/tradikg" live check --budget 20
```

In der Ausgabe jede Zeile `[ÜBERNAHME]` mit dem Mac vergleichen:

- dieselben Coins, dieselbe Stop-Order-Id, Menge wie der Stop und nicht der ganze Bestand
- Summe der Einstiege höchstens 20 €
- kein `[PROBE] buy`, der eine zweite Position eröffnen würde
- kein zweite Stop-Order

`[ZWEITE INSTANZ]` heißt: nicht live gehen. Erst die Ursache unten beseitigen und den Probelauf
wiederholen.

## 5. Erst dann live

```bash
systemctl --user daemon-reload
systemctl --user enable --now tradikg-live.service
```

Der erste Echtlauf übernimmt die vorhandenen Stops und speichert den abgeglichenen Zustand.
Exit-Code 2 (zweite Instanz, Auszahlung, Scheibe über 20 €) startet systemd nicht erneut.

Wenn der Linux-Bot läuft und die Stops dieselben Ids haben, den **Mac-API-Key bei Kraken
deaktivieren**. Bis dahin kann ein versehentlich gestarteter Mac-Prozess noch handeln. Den
Kill-Switch auf dem Mac erst entfernen, wenn der Key aus ist.

## Was „zweite Instanz“ sehen kann – und was nicht

Der Linux-Rechner sieht die Mac-Sperre und die Mac-PID nicht. Geprüft wird:

| Signal | Was es fängt | Grenze |
| --- | --- | --- |
| `live.lock` (flock) und `live.pid` | zweiter Prozess **auf diesem** Rechner | nicht den Mac |
| `heartbeat.json` | frischer Heartbeat eines anderen Rechnernamens, oder einer lebenden anderen PID | nur wenn die Datei kopiert wurde und jünger als 15 Minuten ist; ein toter lokaler PID-Eintrag blockiert nicht |
| Bot-Orders und geschlossene Bot-Orders jünger als 15 Minuten | der andere Bot hat gerade eine Order gelegt oder einen Trade gemacht | Kraken nennt den API-Key nicht. Ein stiller Mac, der nur alle 5 Minuten liest und nichts ändert, ist unsichtbar. Ein Stop von gestern ist kein Alarm. Zeitstempel ohne Uhrzeit zählen nicht als „gerade eben“. |

15 Minuten sind länger als der 5-Minuten-Takt. Nach dem letzten echten Order-Ereignis des Macs
kurz warten, wenn der Probelauf `[ZWEITE INSTANZ]` wegen einer frischen Bot-Order meldet.
