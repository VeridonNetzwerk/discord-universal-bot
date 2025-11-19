# Discord Bot

Funktionsreicher Discord-Bot mit Ticket-System, Verifizierungs-Panel, Musikplayer (yt-dlp/FFmpeg) und umfangreicher In-Server-Konfiguration.

## Features

- **Ticket-System**: Persistentes Panel, private Threads pro Ticket, Claim-Buttons, Admin-Kommandos zum Schliessen/Uebernehmen.
- **Verifizierung**: Button-Panel, automatische Rollenzuweisung, Begruessung neuer Mitglieder.
- **Musik**: yt-dlp + FFmpeg basierter Player (aehnlich Greenbot) mit Queue, Play/Pause/Skip/Stop/Queue/Join/Leave (Prefix oder Slash).
- **In-Server Konfiguration**: `/config` Hybrid-Commands zum Setzen aller IDs und Werte (Kanaele, Rollen, Praefix, FFmpeg-Path etc.).
- **Web-Interface**: Passwortgeschuetzte Verwaltungsoberflaeche (Login per `.env`) zum Bearbeiten der Config und zum Senden von Nachrichten.
- **Persistente Einstellungen**: JSON-Datei (`data/config.json`) synchronisiert mit `.env` Overrides.

## Schnellstart

```bash
pip install -r requirements.txt
python bot.py
```

### Umgebungsvariablen (.env)
- `DISCORD_TOKEN` - Bot Token (Pflicht)
- Alle IDs/Parameter siehe `.env` Beispiel. Werte koennen spaeter auch via `/config` geaendert werden.
- `FFMPEG_PATH` falls FFmpeg nicht im PATH liegt.
- `WEB_USERNAME` / `WEB_PASSWORD` fuer das Management-Webinterface (optional, aktiviert das Interface).
- `WEB_HOST` (default `127.0.0.1`) und `WEB_PORT` (default `8080`) fuer Bind-Adresse/Port des Webinterfaces.
- Optionale Flags: `ENABLE_MESSAGE_CONTENT_INTENT`, `ENABLE_MEMBERS_INTENT`, `ENABLE_PRESENCE_INTENT` ("true"/"false").
- Wenn Message-Content oder Members Intent aktiv sind, muessen sie im [Discord Developer Portal](https://discord.com/developers/applications) fuer die App freigeschaltet werden.

## Wichtige Commands

- `/config show|setchannel|setrole|setvalue|reload|reset`
- `/ticketpanel [channel]`, `/ticketclose [reason]`, `/ticketaccept [member] [thread]`
- `/verifypanel [channel]`
- `/play <suche>`, `/skip`, `/pause`, `/resume`, `/stop`, `/queue`, `/join`, `/leave`

## Audio Backend

- Installiere [FFmpeg](https://ffmpeg.org/) und stelle sicher, dass das Binary im PATH liegt oder passe `FFMPEG_PATH` an.
- `yt-dlp` wird automatisch installiert und liefert die Audio-Streams (YouTube, SoundCloud etc.).

## Entwicklung

- Python 3.10+
- discord.py 2.3+
- yt-dlp 2024+

```bash
# optional: Format / Lint
python -m py_compile bot.py
```

Viel Spass beim Anpassen! Alle Kernfunktionen sind modular als Cogs implementiert (`cogs/`).

