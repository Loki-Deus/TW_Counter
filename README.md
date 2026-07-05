[README.md](https://github.com/user-attachments/files/29680817/README.md)
# TW-Counter Discord Bot

Sammelt Territory-War-Konter (welcher angreifende Anführer schlägt welchen verteidigenden Anführer) aus der eigenen Match-Historie der Gilde. Schwesterprojekt zum **TB-Reminder-Bot** (Phasenansagen), gleicher Stack, getrennte Discord-Anwendung.

## Ablauf

**Ein Konter muss zuerst mit `/tw_add` angelegt werden, bevor `/tw_report` ein Ergebnis dazu akzeptiert.** `/tw_report` gegen einen nicht angelegten Konter schlägt mit einer klaren Fehlermeldung fehl, statt automatisch etwas anzulegen — das ist Absicht, kein Bug. Details für Gildenmitglieder siehe `TW_Counter_Bot_Anleitung.md`.

Jedes gemeldete Ergebnis wird nach Relic-Differenz (Angreifer − Verteidiger) in einen von drei Buckets sortiert: `unterlegen` (Δ ≤ −3), `ausgeglichen` (−2 bis +2), `überlegen` (Δ ≥ +3). Diese Grenzen leben ausschließlich als Konstanten in `config.py` (`UNDER_THRESHOLD`, `OVER_THRESHOLD`).

## Befehle

| Befehl | Berechtigung | Parameter | Verhalten |
|---|---|---|---|
| `/tw_add` | Rolle `SPECIALIST_ROLE_ID` | `verteidiger`, `angreifer` (Autocomplete) | Legt einen leeren Matchup an. Kein Admin-Override. |
| `/tw_report` | Rolle `MEMBER_ROLE_ID` | `verteidiger`, `verteidiger_relic`, `angreifer`, `angreifer_relic`, `ergebnis` | Fügt eine Report-Zeile ein. `angreifer`-Autocomplete zeigt nur Angreifer mit bestehendem Konter gegen den gewählten `verteidiger`. Relic-Range (0–20) wird über `app_commands.Range` clientseitig erzwungen. |
| `/tw_lookup` | alle | `verteidiger` | Alle Konter gegen einen Verteidiger, primär sortiert nach Ausgeglichen-Quote absteigend; Konter ohne Daten in diesem Bucket sinken ans Ende. |
| `/tw_delete` | Administrator ODER `MANAGER_IDS` | `verteidiger`, `angreifer` | Löscht Matchup inkl. aller Reports (CASCADE), nach Inline-Bestätigung. |
| `/tw_characterrefresh` | exakt `OWNER_ID` | `datei` (Attachment) | Siehe Abschnitt „Charakterliste" unten. |
| `/tw_help` | alle | — | Ephemere Befehlsübersicht direkt in Discord. |

Alle vier rollen-/ID-basierten Berechtigungen sind unabhängig voneinander (`is_tw_specialist`, `is_member`, `is_manager`, `is_owner` in `bot.py`) — keine impliziert eine andere, keine hat einen versteckten Admin-Override außer `/tw_delete`, wo er explizit gewollt ist.

## Datenmodell

Event-Log, kein Aggregat: `counters` ist der Matchup-Katalog, `reports` ist das Log einzelner Meldungen. Jede Bucket-Aggregation wird zur Abfragezeit aus `reports` berechnet, nie persistiert — erlaubt rückwirkende Neu-Bucketung, falls sich die Grenzen je ändern.

```sql
CREATE TABLE counters (
    id, defending_leader, attacking_leader,
    submitted_by_id, submitted_by_name, submitted_at
);
CREATE UNIQUE INDEX idx_counter_matchup ON counters(defending_leader, attacking_leader);

CREATE TABLE reports (
    id, counter_id REFERENCES counters(id) ON DELETE CASCADE,
    attacker_relic, defender_relic,
    delta INTEGER GENERATED ALWAYS AS (attacker_relic - defender_relic) VIRTUAL,
    result, reported_by_id, reported_at
);
CREATE INDEX idx_reports_counter ON reports(counter_id);
```

`delta` ist eine generierte Spalte (SQLite ≥ 3.31, Python 3.12 erfüllt das) — von der Engine abgeleitet, nie applikationsseitig geschrieben. Exaktes Schema in `db.py`.

## Charakterliste — warum kein Live-Fetch

Die Autocomplete-Daten für Charakternamen kommen **nicht** von einer API. `swgoh.gg/characters/` läuft hinter einer aktiven Cloudflare-JS-Challenge; das wurde direkt getestet (curl unter Windows/schannel, curl unter WSL/OpenSSL mit identischem Browser-User-Agent, Python `requests`) — jeder automatisierte Client außer einem echten Browser bekommt `cf-mitigated: challenge` statt der Seite. Es gibt deshalb bewusst keinen Netzwerk-Fetch in `character_list.py`.

Stattdessen: `swgoh.gg/characters/` im Browser öffnen, als HTML speichern, per `/tw_characterrefresh` (nur `OWNER_ID`) hochladen. Der Bot validiert (parst und zählt gefundene Charaktere) **bevor** er die bestehende Datei überschreibt — eine fehlgeschlagene Validierung lässt die zuletzt funktionierende Version unangetastet. Ergebnis wird zusätzlich nach `characters.json` gecacht und beim Start sowie im wöchentlichen `tasks.loop(hours=168)` erneut eingelesen (kein neuer Netzwerk-Request, nur ein erneutes Parsen der aktuell abgelegten Datei).

Kein manuelles Update seit einer Weile → Autocomplete zeigt schlicht keine neuen Charaktere, fällt aber nie auf eine leere Liste zurück, solange irgendwann einmal erfolgreich hochgeladen wurde.

## Setup

### 1. Discord-Anwendung

Eigene Anwendung im Developer Portal, getrennt von TB-Reminder — eigener Token, eigene OAuth2-Einladung. **Kein Privileged Gateway Intent nötig** (kein `Intents.members` — nichts im Code enumeriert `role.members`, alle Berechtigungsprüfungen laufen über `interaction.user.roles` direkt aus dem Interaction-Payload).

OAuth2 → URL Generator: Scopes `bot` + `applications.commands`, Bot-Permission `Send Messages` (kein `Mention Everyone`, keine Embed-Rechte nötig — nur Text und Code-Blöcke).

### 2. IDs

Developer Mode in Discord aktivieren, dann:

- `GUILD_ID` — Server-ID
- `SPECIALIST_ROLE_ID` — Rolle für `/tw_add`
- `MEMBER_ROLE_ID` — Rolle für `/tw_report` (kann identisch mit `SPECIALIST_ROLE_ID` sein, muss aber nicht)
- `MANAGER_IDS` — komma-separierte User-IDs für `/tw_delete`, zusätzlich zu Administrator-Rechten
- `OWNER_ID` — einzelne User-ID für `/tw_characterrefresh`

**Wichtig, aus eigener Erfahrung:** Rollen-IDs immer über die Mitgliederliste verifizieren (Rechtsklick auf den eigenen Namen → zugewiesene Rollen), nicht nur über Server-Einstellungen → Rollen → Rolle kopieren. Bei gleichnamigen Rollen können beide Wege unterschiedliche IDs liefern.

### 3. Umgebungsvariablen

```
DISCORD_TOKEN=
GUILD_ID=
SPECIALIST_ROLE_ID=
MEMBER_ROLE_ID=
MANAGER_IDS=
OWNER_ID=
DATA_DIR=./data
BOT_TIMEZONE=Europe/Vienna
```

`BOT_TIMEZONE` wird aktuell von keinem Code-Pfad gelesen (keine zeitpunktbasierte Planung wie beim TB-Reminder-Bot) — für spätere Verwendung vorgemerkt, kein totes Feld aus Versehen.

### 4. Lokal ausführen

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
mkdir -p data
python bot.py
```

Charakterliste beim allerersten Start: entweder vorher `swgoh_characters.html` manuell nach `DATA_DIR` legen, oder Bot ohne Charakterdaten starten (loggt eine Warnung, stürzt nicht ab) und per `/tw_characterrefresh` nachliefern.

### 5. Deployment (Docker / Portainer)

Volume **vor** dem ersten Deploy anlegen — `docker-compose.yml` deklariert `tw-counter-data` als `external: true`, Compose/Portainer legt eine external deklarierte Volume nicht automatisch an, sondern erwartet sie bereits vorhanden:

- Portainer → Volumes → Add volume → Name exakt `tw_counter_tw-counter-data`, Driver `local`
- Kein SSH nötig — komplett über Portainers UI abbildbar

Dann: Stacks → Add stack → `docker-compose.yml` einfügen → sechs Umgebungsvariablen setzen (`DISCORD_TOKEN`, `GUILD_ID`, `SPECIALIST_ROLE_ID`, `MEMBER_ROLE_ID`, `MANAGER_IDS`, `OWNER_ID` — `DATA_DIR` ist im Compose-File hart auf `/app/data` gesetzt, nicht per `.env` überschreibbar, muss passend zum Volume-Mount bleiben) → Deploy.

Charakterliste danach per `/tw_characterrefresh` befüllen — kein Zugriff auf das Container-Dateisystem nötig.

## Hinweise

- `DATA_DIR` enthält `counters.db`, `characters.json` und `swgoh_characters.html` — alle drei über `.gitignore`s `data/`-Eintrag ausgeschlossen, nicht einzeln aufgezählt (eine frühere Version zählte Dateinamen einzeln auf, das ließ `swgoh_characters.html` durchrutschen; es landete einmal in einem lokalen Commit, wurde aber vor dem Push per `git rm --cached` + `commit --amend` wieder entfernt — daher jetzt Verzeichnis-Ausschluss statt Dateiname-Enumeration).
- Bot-Neustart nötig nach jeder `.env`-Änderung — Environment-Variablen werden nur beim Prozessstart gelesen, kein Live-Reload.
- Discord-Token niemals in Chats, Issues oder Commit-Messages einfügen. Bei Verdacht auf Exposure: Developer Portal → Bot → Reset Token, `.env` aktualisieren, Bot neu starten.
