# Cutover: Mac-Bot auf die Linux-Box (gleiche Kraken-Konto, anderer API-Key)

Checkliste für den Umzug heute Nacht. Nichts davon merged den PR, und nichts davon
darf eine Order legen, bevor Schritt 6 „OK“ zeigt. API-Key und Secret werden nicht
ausgegeben, nicht ins Repo gelegt und nicht ins Log geschrieben.

Der Linux-Lauf ist fest verdrahtet:

- Budget **20 €** (`MAX_BUDGET_EUR`, nicht anhebbar). Das Konto kann ~100 € halten;
  der Bot handelt nur seine Scheibe.
- Tagesverlustgrenze **an, 5 %** der Scheibe (00:00 Europe/Vienna).
- Drawdown-Sperre **an, 45 %** unter dem Höchststand der Scheibe
  (`--drawdown-kill-fraction 0.45`). Enger ist erlaubt, weiter nicht.
  Der Schalter ist dieses CLI-Flag. Eine Umgebungsvariable liest der Bot dafür nicht.
- Neustart nur bei Fehler: `Restart=on-failure`, aber nicht bei Exit 2
  (`RestartPreventExitStatus=2`). Die Schleife
  `~/.local/bin/tradikg_run_live_loop.sh` startet den Bot. Nach einem Absturz
  und vor jedem Start prüft sie validate-only. Scheitert der Abgleich, endet
  sie mit Exit 2 und systemd hebt sie nicht wieder an.
- Zugangsdaten aus `~/.config/tradikg/live.env` (`chmod 600`), Fallback nach dem
  macOS-Schlüsselbund. Auszahlungsrecht bricht weiter ab.

Layout wie auf dem Mac, unter `TRADIKG_HOME` (Standard `~/.tradikg`):

| Datei | Bedeutung |
| --- | --- |
| `live/trend_state.json` | Zustand (Mengen, Stops, Tagesgrenze) |
| `live/KILL_SWITCH` | keine neuen Käufe, Stops bleiben |
| `live/live.pid` | PID von `start_live.sh` / dem Linux-Prozess |
| `live/live.lock` | lokale Sperre (`flock`) |
| `live/dry_run_ok.json` | Stempel des Probelaufs (Konfiguration und Commit, ohne Geheimnisse, ohne Zustandshash) |

## 1. Mac-Bot stoppen

Auf dem Mac, im Repo. Zuerst die Kill-Datei, damit ein noch laufender Durchgang
nicht mehr kauft, dann den nohup-Prozess aus `scripts/start_live.sh`:

```bash
tradikg live kill
scripts/stop_live.sh
```

`stop_live.sh` schickt SIGTERM an die PID in `~/.tradikg/live/live.pid`.
Stops, die schon bei Kraken liegen, bleiben liegen. Die Kill-Datei
`~/.tradikg/live/KILL_SWITCH` auf dem Mac **liegen lassen**, damit ein versehentliches
`scripts/start_live.sh` dort nichts mehr kauft. Diese Datei **nicht** auf die Box kopieren.

## 2. Prüfen, dass der Mac wirklich steht

```bash
PID="$HOME/.tradikg/live/live.pid"
if [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; then
  echo "NOCH AKTIV – nicht weitermachen"
  exit 1
fi
pgrep -af "tradikg live" || echo "kein tradikg-live-Prozess"
test -f "$HOME/.tradikg/live/KILL_SWITCH" && echo "Kill-Datei liegt (gut, solange der Mac aus bleibt)"
```

Erst weitermachen, wenn kein Prozess mehr läuft. Mindestens 15 Minuten nach der
letzten Order des Macs warten: die Börsen-Heuristik (unten) blockiert frische
Bot-Orders in diesem Fenster.

## 3. Nur die Zustandsdatei kopieren

```bash
# auf dem Mac
ls -l ~/.tradikg/live/trend_state.json
# auf die Box, Beispiel (Pfad anpassen, keine Keys im Kommando):
scp ~/.tradikg/live/trend_state.json BOX:~/.tradikg/live/trend_state.from-mac.json
```

Nicht kopieren: `live.pid`, `live.lock`, `KILL_SWITCH`, `live.env`, JSON mit
API-Schlüsseln, Shell-History. Das Journal (`journal.sqlite`) muss nicht mit:
die Nonce ist die Uhrzeit in Mikrosekunden.

## 4. Linux vorbereiten (noch kein Start)

```bash
cd /pfad/zum/Tradikg-bot
python3 -m venv .venv && .venv/bin/pip install -e .
scripts/install_linux_unit.sh
sudo loginctl enable-linger "$USER"
```

`install_linux_unit.sh` legt `~/.config/systemd/user/tradikg-live.service` an,
kopiert die Neustart-Schleife nach `~/.local/bin/tradikg_run_live_loop.sh` und,
falls noch keine Datei existiert, eine leere `~/.config/tradikg/live.env` mit
`chmod 600`. Key und Secret **in dieser Datei auf der Box** eintragen
(`KRAKEN_API_KEY`, `KRAKEN_API_SECRET`), Datei nicht cat-en und nicht committen.
Danach noch einmal `chmod 600 ~/.config/tradikg/live.env`. Die Datei liegt
außerhalb des Repos. Der Key braucht dieselben Rechte wie auf dem Mac und
**kein** Auszahlungsrecht.

```bash
systemctl --user daemon-reload
# nicht start, nicht enable --now
```

## 5. Probelauf (validate only, keine Orders)

```bash
mkdir -p ~/.tradikg/live
.venv/bin/tradikg live dry-run \
  --budget 20 --invest-fraction 1.0 \
  --daily-loss --daily-loss-fraction 0.05 \
  --drawdown-kill-fraction 0.45 \
  --import-state ~/.tradikg/live/trend_state.from-mac.json
```

Das prüft die Datei (Scheibe ≤ 20 €, keine Geheimnisfelder), übernimmt sie nach
`~/.tradikg/live/trend_state.json`, gleicht sie mit den offenen Bot-Orders ab
und lässt Kraken geplante Orders nur validieren (`validate=true`). Es wird
nichts gekauft, nichts storniert, nichts gespeichert außer der geprüften
Zustandsdatei und dem Stempel `dry_run_ok.json`.

Ohne Datei, nur wenn an der Börse noch Bot-Orders mit `userref` liegen:

```bash
.venv/bin/tradikg live dry-run \
  --budget 20 --invest-fraction 1.0 \
  --daily-loss --daily-loss-fraction 0.05 \
  --drawdown-kill-fraction 0.45 \
  --reconstruct
```

Rekonstruktion übernimmt Stops und offene Käufe/Verkäufe anhand der `userref`
und nur innerhalb der Scheibe. Münzen und EUR ohne Bot-Kennung (der Rest der
~100 €, manuelle Trades) werden nicht zur Position. Liegt der so geschätzte
Einstand über 20 €, bricht der Lauf ab – dann die Mac-Datei importieren, dort
steht der echte Einstand. Eine schon vorhandene Zustandsdatei wird nicht
überschrieben.

## 6. Abgleich lesen

Jede Zeile `[ABGLEICH]` muss `OK` sein: dieselbe Stop-Txid, dieselbe Menge,
dieselbe Kauf-Txid. `ABWEICHUNG` heißt nicht live gehen.

Erwartung, wenn der Mac eine Position mit Stop hält:

- Menge in der Datei = Stop-Menge an der Börse, nicht der ganze Münzbestand.
- Kein zweiter Stop, kein zweiter Kauf für dieselbe Münze.
- Probe-Zeilen (`[PROBE]`) beschreiben nur, was ein späterer Lauf täte. Steht
  dort nichts, ist der letzte Bot-Tag noch der aktuelle Kerzentag – es wird
  bis zum nächsten Tagesschluss nichts Neues entschieden.
- `Auszahlungsrecht` darf nicht durchrutschen: der Lauf bricht ab, wenn der Key
  auszahlen darf (dieselbe Prüfung wie im Echtbetrieb).

Der Stempel wird nur bei `OK` geschrieben. Er merkt sich Budget, Tagesverlust,
Drawdown und den Code-Stand (`git rev-parse HEAD`), nicht den Hash der
Zustandsdatei. Die Datei ändert sich, sobald der Bot läuft; das allein ist kein
Grund, einen Neustart zu verweigern. Ein anderes Budget, eine andere
Tagesgrenze, eine andere Drawdown-Schwelle oder ein anderer Commit verlangt
einen neuen manuellen Probelauf. Ein Stempel ohne `code_commit` (älterer
Probelauf) zählt als anderer Code-Stand: vor dem ersten Start dieses Stands
den Probelauf mit denselben Flags einmal wiederholen. Probelauf und Schleife
müssen dieselben Flags tragen.

## 7. Erst jetzt live

```bash
systemctl --user enable --now tradikg-live.service
systemctl --user status tradikg-live.service
.venv/bin/tradikg live status
```

Die Unit startet `~/.local/bin/tradikg_run_live_loop.sh`. Die Schleife setzt
Budget 20, Tagesverlustgrenze 5 %, Drawdown-Sperre 45 %
(`--drawdown-kill-fraction 0.45`) und `--require-dry-run`.
`Restart=on-failure` gilt für die Schleife selbst. Exit 2
(`RestartPreventExitStatus=2`) und ein sauberes Ende (Exit 0) starten sie nicht
neu. Ohne `enable-linger` stirbt sie beim Abmelden. Die 45 % sind die weiteste
erlaubte Schwelle; enger geht über dasselbe Flag, weiter wird abgelehnt. Lösen
einer ausgelösten Sperre nur mit `tradikg live drawdown-reset` (nichts wird
dabei verkauft).

Vor jedem Start der Schleife, und damit auch nach einem Absturz, läuft
`tradikg live restart-check`:

- Kraken-Client ohne Handelsfreigabe (`allow_trading=False`).
- `AddOrder` nur mit `validate=true`. Ein Storno oder eine echte Order bricht ab.
- Nichts wird gespeichert: weder `trend_state.json` noch der Stempel noch die
  Kill-Datei. Die Sperrdatei wird nur kurz genommen und wieder freigegeben.
  Der Validate-Durchgang schreibt nur in ein temporäres Verzeichnis, das danach
  verschwindet.
- Offene Bot-Orders gegen `trend_state.json`.
- Börsenseitiger Stop für jede Menge, die nicht gerade in einem Bot-Verkauf liegt.
- Zweite Instanz: lebende PID, gehaltene Sperrdatei, Bot-Order mit `opentm` in
  den letzten 15 Minuten.

Nur wenn das passt, startet `tradikg live run`. Sonst eine Zeile
`Neustart verweigert: … Kein Neustart.` und Exit 2. Die Schleife startet den Bot
dann nicht erneut, und systemd auch nicht. Ein Konfigurationswechsel (Budget,
Tagesgrenze, Drawdown, Commit) ist so ein Abbruch: zuerst einen neuen
manuellen `tradikg live dry-run`, danach `systemctl --user start`.

Kill-Switch, Auszahlungsabbruch, börsenseitiger Stop, 5-%-Tagesgrenze,
Drawdown −45 % (nur manuell lösbar), `MAX_BUDGET_EUR=20` und die Verweigerung
einer zweiten Instanz bleiben wie sie sind. Der Abgleich lockert keine davon.
Eine Bot-Order mit `opentm` in den letzten 15 Minuten blockiert den Neustart
(dieselbe Heuristik wie beim ersten Start). Die Schleife versucht es dann nicht
noch einmal; nach dem Fenster `systemctl --user start` erneut, wenn keine
andere Instanz läuft.

Läuft die Unit schon, nach diesem Stand `scripts/install_linux_unit.sh` noch
einmal ausführen (neue Schleife nach `~/.local/bin`, neue `ExecStart`-Zeile)
und `systemctl --user daemon-reload`. Nicht `enable --now`, solange der
Probelauf nicht `OK` ist.

## Nicht gleichzeitig mit dem Mac

Der Start verweigert, wenn:

- `~/.tradikg/live/live.pid` auf diesem Rechner einen lebenden Prozess nennt, oder
- `live.lock` gehalten wird, oder
- eine Bot-Order (`userref`) ein `opentm` in den letzten 15 Minuten hat.

Grenzen, die der Check nicht schließt:

- Kraken zeigt nicht, welcher API-Key die Order gelegt hat. Mac-Key und
  Linux-Key sehen gleich aus.
- Ein stiller Bot, der gerade keine Order anfasst, ist an der Börse unsichtbar.
  Deshalb Schritt 2: Prozess auf dem Mac wirklich beenden, nicht nur auf die
  Heuristik vertrauen.
- Alte Stops ohne frisches `opentm` sind kein Laufsignal. Sie werden übernommen
  und nicht ein zweites Mal gelegt, solange Preis und Menge passen.
- Die PID-Datei gilt nur lokal. Eine kopierte Mac-PID sagt nichts über den Mac.

## Zurück

```bash
systemctl --user stop tradikg-live.service
systemctl --user disable tradikg-live.service
```

Erst wenn auf der Box kein Prozess mehr läuft, auf dem Mac die Kill-Datei lösen
(`tradikg live kill --release`) und dort bei Bedarf `scripts/start_live.sh` wieder
starten. Nie beide gleichzeitig.
