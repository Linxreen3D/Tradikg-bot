# Echtgeld auf Linux: Trend-Bot mit 20 €

Dieselbe Regel wie in `docs/LIVE_START.md` (200-Tage-Linie, BTC und ETH, Notfall-Stop bei Kraken).
Der Dauerbetrieb auf Linux ist eine **systemd-User-Unit**. Es gibt keine launchd-Unit für den
Echtgeld-Bot; die vorhandenen launchd-Vorlagen bleiben nur für Paper und Dashboard auf dem Mac.

Nichts davon startet den Bot von allein. Zuerst der Probelauf, danach die Unit, und erst dann
startest du den Dienst selbst.

## Was der Bot anfassen darf

Das Kraken-Konto kann mehr Guthaben halten (zum Beispiel rund 100 €). Der Bot handelt nur seine
**eigene Scheibe von 20 €** (`--budget` über 20 wird abgelehnt). Käufe werden aus dem Rest dieser
Scheibe und dem **frei verfügbaren** EUR-Guthaben berechnet, nicht aus dem ganzen Kontostand.

Der Abbruch bei einem API-Schlüssel **mit Auszahlungsrecht** bleibt. Exit-Code 2 (Auszahlung,
Margin, verlorener Zustand, fehlende Zugangsdaten, Budget über 20 €) startet systemd nicht neu.

## 1. Zugangsdaten außerhalb des Repos

Keine Schlüssel ins Repository, in den Chat oder in die Unit-Datei. Auf Linux gibt es keinen
macOS-Schlüsselbund; der Bot liest dann `KRAKEN_API_KEY` und `KRAKEN_API_SECRET` aus der Umgebung.
Auf einem Mac gewinnt der Schlüsselbund, die Variablen sind nur der Fallback.

Datei zum Beispiel unter `$HOME/.config/tradikg/kraken.env` (nicht im Git-Arbeitsverzeichnis),
Modus `0600`:

```bash
umask 077
mkdir -p "$HOME/.config/tradikg"
install -m 600 /dev/null "$HOME/.config/tradikg/kraken.env"
```

Inhalt nur auf dem Rechner eintragen (Platzhalter, keine echten Werte hier):

```text
KRAKEN_API_KEY=<aus dem Kraken-API-Dialog>
KRAKEN_API_SECRET=<aus dem Kraken-API-Dialog>
```

Rechte prüfen: `stat -c '%a' "$HOME/.config/tradikg/kraken.env"` muss `600` zeigen.
Die Datei steht in `.gitignore` nicht automatisch, weil sie außerhalb des Repos liegen muss.
`scripts/install_linux_live_unit.sh` bricht ab, wenn sie im Git-Arbeitsverzeichnis liegt oder
nicht Modus `0600` hat. Das Skript schreibt keine Schlüssel und liest den Dateiinhalt nicht.

Key-Rechte wie in `docs/LIVE_START.md`: kein Withdraw.

## 2. Probelauf, ohne Order

`tradikg live check` ist der validate-Pfad (`dry_run`): der API-Client darf nicht handeln,
`AddOrder` geht nur mit `validate=true` raus, der Zustand wird nicht gespeichert. Die
Bestätigungsflagge für echtes Geld gehört hier **nicht** hin.

`TRADIKG_HOME` ist das Datenverzeichnis (Voreinstellung `$HOME/.tradikg`):

| Pfad | Inhalt |
| --- | --- |
| `$TRADIKG_HOME/live/trend_state.json` | Mengen, Stops, offene Bot-Orders |
| `$TRADIKG_HOME/live/KILL_SWITCH` | vorhanden = keine neuen Käufe, Stops bleiben |
| `$TRADIKG_HOME/live/live.log` | Bot-Log |
| `$TRADIKG_HOME/live/live.lock` | eine Instanz |
| `$TRADIKG_HOME/live/snapshot.json` | Kontostand fürs Dashboard |
| `$TRADIKG_HOME/live/journal.sqlite` | Nonces |

```bash
cd /pfad/zum/Tradikg-bot
python3 -m venv .venv && .venv/bin/pip install -e .
mkdir -p "${TRADIKG_HOME:-$HOME/.tradikg}/live"
systemd-run --user --pipe --wait \
  --property=Environment=TRADIKG_HOME="${TRADIKG_HOME:-$HOME/.tradikg}" \
  --property=EnvironmentFile="$HOME/.config/tradikg/kraken.env" \
  --working-directory="$PWD" \
  "$PWD/.venv/bin/tradikg" live check --budget 20
```

Erwartet: Auszahlungsrecht „nein (gut)“, danach Zeilen `[PROBE] … nicht ausgeführt`. Es liegt
keine neue Order bei Kraken. Bei `ABBRUCH` oder `ABGELEHNT` nicht weitermachen.

## 3. User-Unit installieren (startet nichts)

```bash
scripts/install_linux_live_unit.sh "$HOME/.config/tradikg/kraken.env"
```

Das schreibt `~/.config/systemd/user/tradikg-live.service` aus
`deploy/systemd/tradikg-live.service.template` und ruft **kein** `systemctl` auf.
Die Unit setzt `TRADIKG_HOME`, lädt die EnvironmentFile und startet:

```text
tradikg live run --budget 20 --invest-fraction 1.0 --i-understand-this-uses-real-money
```

`Restart=on-failure`, `RestartSec=30`, `RestartPreventExitStatus=2`.

Wenn der Probelauf sauber war, den Dienst selbst einschalten (Linger, damit er ohne Login läuft):

```bash
systemctl --user daemon-reload
systemctl --user enable --now tradikg-live.service
loginctl enable-linger "$USER"
```

## 4. Stoppen und Kill-Switch

```bash
systemctl --user stop tradikg-live.service    # Prozess weg, Stops bei Kraken bleiben
tradikg live kill                             # keine neuen Käufe, Stops bleiben
tradikg live kill --release                   # Kill-Switch aus
tradikg live status
```

Dafür muss `TRADIKG_HOME` in der Shell denselben Wert haben wie in der Unit.
