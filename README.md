# Monitoring

Schlankes, selbst gehostetes Monitoring-System mit Weboberfläche. Es überwacht Websites, APIs, Server,
Ports, Cronjobs, Backups, Smart-Home- und IoT-Geräte und benachrichtigt bei Störungen über Discord,
Telegram, ntfy, E-Mail, Slack oder einen eigenen Webhook.

- **Ein Container**, eine SQLite-Datei, keine weiteren Dienste nötig
- **Weboberfläche** (Deutsch, Hell/Dunkel, mobil nutzbar) mit Verlauf, Diagrammen und Statuswechseln
- **REST-API** für alles, was die Oberfläche kann, und ein **Prometheus-Endpunkt** für Grafana

## Wie werden Systeme angebunden?

Alles läuft über **HTTP(S)**. Das funktioniert mit praktisch jedem System, geht durch Firewalls und
Proxys und braucht keine Spezialsoftware. Es gibt zwei Richtungen:

| Richtung | Typ | Wofür | Auf dem Zielsystem nötig |
|---|---|---|---|
| **Pull**: der Server prüft | `http` | Websites, REST-APIs, Health-Endpunkte (Statuscode, Text, TLS-Zertifikat) | nichts |
| | `tcp` | Datenbanken, SSH, Mail, beliebige Ports | nichts |
| | `ping` | Router, Server, Netzwerkgeräte | nichts |
| | `dns` | Namensauflösung, optional mit erwarteter IP | nichts |
| **Push**: das System meldet sich | `push` | Server-Agent (CPU/RAM/Disk), Cronjobs, Backups, Home Assistant, IoT, eigene Skripte | nur ausgehendes HTTP(S), z. B. `curl` |

**Pull** für alles, was der Monitoring-Server erreichen kann. **Push** für alles hinter NAT/Firewall
und für alles, was *von innen* gemessen werden muss (Auslastung, Job-Erfolg, Sensorwerte). Bleibt ein
Push-Signal länger als *Intervall + Karenzzeit* aus, gibt es Alarm, wie bei einem Totmannschalter.

## Schnellstart

```bash
docker compose up -d
```

Danach <http://localhost:8080> öffnen. Beim ersten Aufruf legst du das Administrator-Konto an.

In `docker-compose.yml` sollte `MONITOR_PUBLIC_URL` auf die Adresse zeigen, unter der die Oberfläche
von außen erreichbar ist. Sie wird für die Anbindungs-Beispiele und für Links in Benachrichtigungen
verwendet.

### Ohne Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080
```

Voraussetzung ist Python 3.11 oder neuer. Für Ping-Prüfungen muss `ping` installiert sein.
**Nur mit einem Worker starten**, denn der Prüf-Scheduler läuft im selben Prozess.

## Konfiguration

| Variable | Standard | Bedeutung |
|---|---|---|
| `MONITOR_DATA_DIR` | `./data` (Docker: `/data`) | Speicherort der SQLite-Datenbank |
| `MONITOR_PUBLIC_URL` | leer | Externe URL, z. B. `https://monitor.example.com` |
| `MONITOR_SECURE_COOKIES` | `false` | `true`, sobald die Oberfläche über HTTPS läuft |
| `MONITOR_RETENTION_DAYS` | `30` | Wie lange Messwerte aufbewahrt werden. Statuswechsel bleiben mindestens 90 Tage. |
| `MONITOR_SESSION_DAYS` | `30` | Gültigkeit einer Anmeldung |
| `MONITOR_MAX_CONCURRENT_CHECKS` | `50` | Maximal gleichzeitig laufende Prüfungen |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IP(s) des Reverse Proxys, dessen `X-Forwarded-For` vertraut wird (wichtig für die Login-Sperre nach Fehlversuchen) |

## Push-Schnittstelle

Jeder Push-Monitor hat eine geheime URL `/api/push/<token>`. Die Oberfläche zeigt sie mit
fertigen Beispielen für curl, Linux, Windows, Home Assistant und Python an.

```bash
# Einfacher Heartbeat (z. B. am Ende eines Cronjobs)
curl -fsS "https://monitor.example.com/api/push/<token>"

# Fehler melden
curl -fsS "https://monitor.example.com/api/push/<token>?status=down&msg=Backup+fehlgeschlagen"

# Mit Metriken (JSON)
curl -fsS -X POST "https://monitor.example.com/api/push/<token>" \
  -H "Content-Type: application/json" \
  -d '{"status": "up", "message": "web01", "metrics": {"cpu": 42.5, "disk": 71}}'
```

| Feld | Bedeutung |
|---|---|
| `status` | `up`/`ok` (Standard) oder `down`/`fail`/`error` |
| `message` / `msg` | Freitext, der in der Oberfläche und in Benachrichtigungen erscheint |
| `latency` / `ping` | Optionale Antwortzeit in ms |
| `metrics` | Objekt mit Zahlenwerten. Bei GET zählt jeder weitere Query-Parameter als Metrik. |

Für Metriken lassen sich **Grenzwerte** festlegen (z. B. `cpu > 90`, `disk >= 95`, `temp > 70`).
Wird einer erreicht, geht der Monitor auf Störung.

### Agents für Server

Die Agent-Skripte liefert der Server selbst unter `/agent/…` aus:

- **Linux**: `agent/linux-agent.sh`. Braucht nur `bash`, `awk` und `curl` und meldet CPU, RAM,
  Swap, Festplatte(n), Load, Prozesse und Uptime. Aufruf per Cron jede Minute oder dauerhaft mit
  `MONITOR_LOOP=60`.
- **Windows**: `agent/windows-agent.ps1`. Meldet CPU, RAM, Laufwerke, Prozesse und Uptime.
  Aufruf als geplante Aufgabe.

Die fertigen Installationsbefehle mit der passenden URL stehen auf der Detailseite jedes Push-Monitors.

## Benachrichtigungen

Kanäle werden unter *Benachrichtigungen* angelegt und pro Monitor ausgewählt. Mit „Testen“ lässt sich
jeder Kanal sofort prüfen. Benachrichtigt wird bei jedem Statuswechsel (OK → Störung und zurück).
Mit *Fehlversuche bis Alarm* vermeidest du Fehlalarme durch einzelne Aussetzer.

| Kanal | Benötigt |
|---|---|
| Discord | Webhook-URL |
| Telegram | Bot-Token (von @BotFather) und Chat-ID |
| ntfy | Topic, optional eigener Server und Token |
| E-Mail | SMTP-Server, Absender, Empfänger (STARTTLS/SSL) |
| Slack / Mattermost | Incoming-Webhook-URL |
| Webhook | Beliebige URL; erhält JSON mit Monitor, Status und Meldung |

## REST-API und Prometheus

Unter *Anbindung & Einstellungen* werden API-Keys erzeugt. Damit ist die komplette API nutzbar
(Monitore anlegen, ändern, pausieren usw.). Die interaktive Dokumentation liegt unter `/docs`.

```bash
curl -H "Authorization: Bearer <API-KEY>" https://monitor.example.com/api/monitors
```

`/metrics` liefert Status, Antwortzeiten und alle Push-Metriken im Prometheus-Format:

```yaml
scrape_configs:
  - job_name: monitoring
    scheme: https
    authorization:
      credentials: <API-KEY>
    static_configs:
      - targets: ["monitor.example.com"]
```

## Betrieb

- **HTTPS**: Den Container hinter einen Reverse Proxy stellen (Caddy, Traefik, nginx) und
  `MONITOR_SECURE_COOKIES=true` setzen. Beispiel für Caddy: `monitor.example.com { reverse_proxy monitoring:8080 }`
- **Backup**: Alle Daten liegen in `monitor.db` im Datenverzeichnis bzw. Volume.
- **Ping im Container**: Das Image enthält `iputils-ping`. Scheitert Ping trotzdem (etwa bei älteren
  Docker-Versionen ohne unprivilegierte ICMP-Sockets), hilft
  `sysctls: ["net.ipv4.ping_group_range=0 2147483647"]` in der Compose-Datei.

## Entwicklung

```bash
pip install -r requirements-dev.txt
pytest
```

Aufbau:

```
app/
  main.py        App-Factory, Lebenszyklus, statische Dateien
  api.py         REST-API, Push-Endpunkt, Prometheus
  checks.py      HTTP-, TCP-, Ping- und DNS-Prüfungen, Auswertung von Push-Daten
  scheduler.py   Hintergrund-Loop: fällige Prüfungen, ausbleibende Signale, Aufräumen
  service.py     Statuslogik (Wiederholungen, Statuswechsel), Auswertungen
  notifier.py    Benachrichtigungskanäle
  auth.py        Passwörter, Sitzungen, API-Keys
  db.py          SQLite-Schema und Zugriff
  static/        Weboberfläche (ohne Build-Schritt, keine externen Abhängigkeiten)
agent/           Agent-Skripte für Linux und Windows
tests/           pytest-Suite
```
