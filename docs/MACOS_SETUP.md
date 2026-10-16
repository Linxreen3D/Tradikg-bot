# Installation und Betrieb auf dem MacBook Air M2

Ziel: Der Paper-Bot läuft auf deinem Mac selbstständig, ohne Claude und ohne
kostenpflichtige Dienste. Es wird **nur virtuell** gehandelt.

## 1. Voraussetzungen

- macOS auf Apple Silicon (M1/M2/M3), mindestens 2 GB freier Speicher
- Homebrew (https://brew.sh) und Python 3.12: `brew install python@3.12`
- Git: `xcode-select --install`

## 2. Installation

```bash
git clone https://github.com/Linxreen3D/Tradikg-bot.git
cd Tradikg-bot
git checkout feature/autonomous-paper-platform   # bis die PRs gemergt sind
scripts/check_environment.sh                     # Prüfung: OS, Architektur, Python
scripts/setup_mac.sh                             # venv, Abhängigkeiten, ~/.tradikg anlegen
```

`setup_mac.sh` löscht keine Daten. Bei einem Update werden vorhandene Datenbanken
vorher automatisch gesichert (`~/.tradikg/backups/`).

## 3. Erste Schritte (in dieser Reihenfolge)

```bash
source .venv/bin/activate

# a) Echte Kraken-Kurse laden und Strategien prüfen (dauert einige Minuten)
tradikg research run --source kraken --timeframe 1h
tradikg research report            # Bericht lesen: "bestätigt", "hypothese", "verworfen"

# b) Optional: ML-Modell trainieren (wird nur Champion, wenn es belastbar besser ist)
tradikg ml train --source cache --timeframe 1h

# c) Historisches Replay mit denselben Regeln wie im Dauerbetrieb
tradikg paper replay --source cache --timeframe 1h

# c2) Historical Replay mit Walk-Forward (siehe docs/HISTORICAL_REPLAY.md)
tradikg data fetch-kraken --timeframe 1d    # 720 Tageskerzen; besser: data import-kraken <ZIP>
tradikg replay run --no-holdout             # Holdout erst ganz am Ende einmal auswerten

# d) Paper-Dauerbetrieb starten und Dashboard öffnen
scripts/start_bot.sh
scripts/start_dashboard.sh          # http://127.0.0.1:8501
```

**Wichtig:** Ohne Research-Ergebnisse handelt der Bot nicht, weil er ohne
Out-of-Sample-Belege keinen Vorteil annimmt. Auch mit Research-Ergebnissen sind
„keine Trades“ ein normales und gültiges Ergebnis.

Kraken liefert pro Abruf höchstens 720 Kerzen (bei 1h ≈ 30 Tage). Für längere
Historien wächst der Cache mit der Zeit, oder du importierst Daten
(`tradikg import-csv`, `tradikg fetch` mit `pip install -e ".[data]"`).

## 4. Autostart mit launchd

```bash
scripts/setup_mac.sh --install-launchd
```

Richtet zwei Benutzerdienste in `~/Library/LaunchAgents` ein:

- `com.tradikg.paper` – Paper-Dauerbetrieb, Neustart nach Absturz (KeepAlive), wartet
  auf Netzwerk, läuft unter `caffeinate -i` (verhindert Leerlauf-Ruhezustand)
- `com.tradikg.dashboard` – Dashboard auf 127.0.0.1:8501

Steuerung: `scripts/start_bot.sh`, `scripts/stop_bot.sh`, `scripts/status_bot.sh`.
Eine zweite Instanz verhindert die Instanzsperre (`~/.tradikg/run/paper.lock`).

## 5. Ruhezustand und Dauerbetrieb – ehrliche Einordnung

Ein MacBook ist kein Server. **24/7-Betrieb ist nicht garantiert.**

- `caffeinate -i` verhindert nur den Leerlauf-Ruhezustand, solange der Bot läuft.
- **Deckel zu ohne externen Monitor = Ruhezustand.** Dann läuft nichts.
- Für längeren Betrieb: Netzteil anschließen und in *Systemeinstellungen →
  Batterie → Optionen* „Automatischen Ruhezustand bei Netzbetrieb verhindern, wenn
  das Display aus ist“ aktivieren. Alternativ (Admin): `sudo pmset -c sleep 0`.
- Nach dem Aufwachen holt der Bot verpasste Kerzen nach: Stop-Loss/Take-Profit
  offener Paper-Positionen werden verarbeitet, **neue Einstiege auf veralteten
  Kerzen gibt es nicht**.
- Für einen späteren Echtgeldbetrieb gilt: Schutzorders müssen **beim Broker**
  liegen, damit sie auch bei schlafendem Mac greifen (Voraussetzung in der Roadmap).

## 6. Not-Aus, Sperren, Berichte

```bash
tradikg paper kill --reason "manuell"     # keine neuen Einstiege
tradikg paper kill --release
tradikg paper status                      # Salden, offene Positionen, Sperren
tradikg paper reset-halt --note "geprüft" # SAFE_HALT nach Prüfung aufheben
tradikg paper report --day 2026-10-16     # Tagesbericht mit Verbesserungshypothesen
tradikg backup                            # Datenbanken sichern
tradikg doctor --network                  # Umgebung und Kraken-Erreichbarkeit prüfen
```

## 7. Logs und Fehlersuche

- Strukturierte Logs: `~/.tradikg/logs/tradikg.jsonl` (rotierend, Schlüssel geschwärzt)
- launchd-Ausgaben: `~/.tradikg/logs/launchd.*.log`
- Status: `~/.tradikg/run/status.json`
- Beschädigte Datenbank: Der Bot startet nicht und meldet das. Backup aus
  `~/.tradikg/backups/<Zeitstempel>/` zurückkopieren (Bot vorher stoppen).

## 8. Zugangsdaten (erst für spätere Live-Phasen)

Für den Paper-Betrieb sind **keine** API-Schlüssel nötig. Für spätere, separat
freizugebende Live-Tests werden Schlüssel nur aus dem macOS-Schlüsselbund, aus
Umgebungsvariablen oder aus einer Datei außerhalb des Repositorys gelesen:

```bash
security add-generic-password -s tradikg-kraken -a api-key -w
security add-generic-password -s tradikg-kraken -a api-secret -w
```

Niemals Auszahlungsrechte vergeben.

## 9. Deinstallation

```bash
scripts/stop_bot.sh
launchctl bootout gui/$(id -u)/com.tradikg.dashboard 2>/dev/null
rm ~/Library/LaunchAgents/com.tradikg.*.plist
# Daten bleiben in ~/.tradikg erhalten, bis du sie selbst löschst.
```
