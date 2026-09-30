# Monitoring

Schlankes, selbst gehostetes Monitoring-System mit Weboberfläche. Es überwacht Websites, APIs, Server,
Ports, Cronjobs, Backups, Smart-Home- und IoT-Geräte und benachrichtigt bei Störungen über Discord,
Telegram, ntfy, E-Mail, Slack oder einen eigenen Webhook.

- **Ein Container**, eine SQLite-Datei, keine weiteren Dienste nötig
- **Weboberfläche** (Deutsch, Hell/Dunkel, mobil nutzbar) mit Verlauf, Diagrammen und Statuswechseln
- **REST-API** für alles, was die Oberfläche kann, und ein **Prometheus-Endpunkt** für Grafana
- **Globale Benutzer- und Rechteverwaltung** mit Rollen, auf Wunsch in einer gemeinsamen MariaDB
- **Handy-App (PWA)** für den Homebildschirm, mit **Push-Benachrichtigungen** bei Störungen

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
cp .env.example .env      # Zugangsdaten eintragen (v. a. MONITOR_USER_DB_PASSWORD)
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
| `MONITOR_USER_DB_HOST` | leer | MariaDB/MySQL-Host für die Benutzerverwaltung. Leer = Benutzer in der lokalen SQLite-Datei |
| `MONITOR_USER_DB_PORT` | `3306` | Port der Benutzer-Datenbank |
| `MONITOR_USER_DB_NAME` / `_USER` / `_PASSWORD` | leer | Datenbankname und Zugangsdaten |
| `MONITOR_USER_DB_TABLE` | `users` | Tabelle mit den Benutzerkonten |
| `MONITOR_VAPID_SUBJECT` | `MONITOR_PUBLIC_URL` | Kontaktadresse für Push-Dienste (`mailto:…` oder `https://…`) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IP(s) des Reverse Proxys, dessen `X-Forwarded-For` vertraut wird (wichtig für die Login-Sperre nach Fehlversuchen) |

## Gemeinsame Benutzer-Datenbank (MariaDB, z. B. aus Plesk)

Die Benutzerkonten können in einer externen MariaDB liegen und dort auch von anderen Anwendungen
genutzt werden. Die Messdaten bleiben in der lokalen SQLite-Datei.

- **Benutzertabelle** (`MONITOR_USER_DB_TABLE`, Standard `users`): Benötigt werden nur die Spalten
  `id`, `username` und `password_hash`. Weitere Spalten sind erlaubt. Existiert die Tabelle
  noch nicht, wird sie angelegt. Existiert sie ohne diese Spalten, bricht der Start mit einer
  klaren Meldung ab.
- **Passwörter** werden als bcrypt im PHP-Format (`$2y$…`) gespeichert. PHP-Anwendungen können sie
  also direkt mit `password_verify()` prüfen, und Hashes aus `password_hash()` funktionieren hier
  umgekehrt genauso.
- **Sitzungen und API-Keys** landen in eigenen Tabellen `monitor_sessions` und `monitor_api_keys`.
- Enthält die Tabelle bereits Benutzer, entfällt die Ersteinrichtung und man meldet sich mit einem
  vorhandenen Konto an.
- Ist die Datenbank nicht erreichbar, liefern Login und Oberfläche einen Fehler (503).
  Prüfungen und Alarme laufen trotzdem weiter.

**Plesk mit Docker:** Die MariaDB von Plesk lauscht normalerweise nur auf `127.0.0.1`. Deshalb nutzt
die `docker-compose.yml` das Host-Netzwerk (`network_mode: host`) und `MONITOR_USER_DB_HOST=127.0.0.1`.
Die Oberfläche ist dann direkt auf Port 8080 des Servers erreichbar.

## Benutzer, Rollen und Rechte

Unter **Benutzer & Rechte** verwalten Administratoren Konten und Rollen. Rechte hängen an Rollen,
Rollen an Benutzern. Änderungen gelten sofort, auch für bestehende Anmeldungen und API-Keys.

| Berechtigung | Erlaubt |
|---|---|
| `monitoring.view` | Monitoring ansehen (Übersicht, Verläufe, Prometheus) |
| `monitoring.edit` | Monitore und Benachrichtigungen anlegen, ändern und löschen |
| `users.manage` | Benutzer, Rollen und Rechte verwalten |
| `*` | alles, in allen Anwendungen |

Beim Start werden drei Rollen angelegt: **Administrator** (`*`), **Monitoring-Bearbeiter** und
**Monitoring-Betrachter**. Das erste Konto aus der Ersteinrichtung wird Administrator.

- **Global nutzbar:** Die Tabellen `roles`, `role_permissions` und `user_roles` liegen neben der
  Benutzertabelle. Andere Anwendungen können dieselben Rollen nutzen und eigene Berechtigungen wie
  `shop.orders.view` vergeben. `shop.*` erlaubt dabei alles in der Anwendung „shop“. Eine
  Berechtigung prüfst du per SQL:
  ```sql
  SELECT 1 FROM user_roles ur JOIN role_permissions rp ON rp.role_id = ur.role_id
  WHERE ur.user_id = ? AND rp.permission IN ('shop.orders.view', 'shop.*', '*');
  ```
- **Aussperr-Schutz:** Der letzte Benutzer mit Verwaltungsrechten kann weder gelöscht noch
  herabgestuft werden.
- **API-Keys** gehören einem Benutzer und haben genau dessen Rechte.
- **Vorhandene Benutzer anderer Anwendungen** können sich sofort anmelden, haben aber keine Rechte,
  bis ihnen eine Rolle zugewiesen wird. Gibt es noch keinen Administrator, geht das auf dem Server so:
  ```bash
  docker compose exec monitoring python -m app.cli grant-admin <benutzername>
  docker compose exec monitoring python -m app.cli list-users
  ```

## Handy-App und Push-Benachrichtigungen

Die Oberfläche ist eine installierbare Web-App (PWA). **Voraussetzung ist HTTPS**, in Plesk z. B.
über eine Subdomain mit Let's Encrypt und Proxy auf Port 8080.

1. **Auf den Homebildschirm legen:**
   - Android (Chrome): *Anbindung & Einstellungen → App installieren* oder im Browsermenü „App installieren“.
   - iPhone (Safari): *Teilen → Zum Home-Bildschirm*.
2. **Push aktivieren:** In der App unter *Anbindung & Einstellungen → Push aktivieren*.
   - Auf dem iPhone geht das ab iOS 16.4 und nur in der vom Homebildschirm geöffneten App.
   - Für Bearbeiter wird dabei automatisch der Kanal **App-Push** angelegt und allen Monitoren
     zugeordnet.
3. Bei jedem Statuswechsel kommt eine Benachrichtigung, auch bei geschlossener App. Störungen
   bleiben sichtbar, bis man sie antippt. Das Antippen öffnet den betroffenen Monitor.

Push geht an alle Geräte von Benutzern mit `monitoring.view`. Die nötigen Schlüssel (VAPID)
erzeugt der Server beim ersten Start selbst.

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
| App-Push | nichts, Geräte aktivieren Push in der App |
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
# alle Tests zusätzlich mit Benutzern/Rechten in einer Wegwerf-MariaDB:
MONITOR_TEST_MARIADB="127.0.0.1:3306:user:passwort:datenbank" pytest
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
  userstore.py   Benutzer, Rollen und Rechte: SQLite oder MariaDB
  webpush.py     Web-Push an die installierte App
  cli.py         Kommandozeile (Administrator ernennen, Benutzer auflisten)
  db.py          SQLite-Schema und Zugriff
  static/        Weboberfläche und PWA (Manifest, Service Worker, Icons), ohne Build-Schritt
agent/           Agent-Skripte für Linux und Windows
tests/           pytest-Suite
```
