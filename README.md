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
| `/tw_ask` | alle | `frage` (Freitext, Deutsch oder Englisch) | Natürlichsprachliche Anfrage, z.B. „was kontert Darth Vader?". Alternativ: Bot in einer normalen Nachricht @mentionen, Frage direkt dranschreiben — siehe Abschnitt „Natürlichsprachliche Anfragen" unten. |
| `/tw_delete` | Administrator ODER `MANAGER_IDS` | `verteidiger`, `angreifer` | Löscht Matchup inkl. aller Reports (CASCADE), nach Inline-Bestätigung. |
| `/tw_characterrefresh` | exakt `OWNER_ID` | `datei` (Attachment) | Siehe Abschnitt „Charakterliste" unten. |
| `/tw_celebrate` | alle | — | Zeigt die Top 3 Melder der meisten TW-Reports. |
| `/tw_help` | alle | — | Ephemere Befehlsübersicht direkt in Discord. |

Alle vier rollen-/ID-basierten Berechtigungen sind unabhängig voneinander (`is_tw_specialist`, `is_member`, `is_manager`, `is_owner` in `bot.py`) — keine impliziert eine andere, keine hat einen versteckten Admin-Override außer `/tw_delete`, wo er explizit gewollt ist. `/tw_lookup`, `/tw_ask`, `/tw_celebrate` und `/tw_help` sind für alle offen und laufen über keine dieser vier Funktionen.

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

## Natürlichsprachliche Anfragen (`/tw_ask`)

Zweiter, rein lesender Zugriffspfad zusätzlich zu `/tw_lookup` — nutzt dieselben `db.py`-Funktionen (`get_attackers_for_defender`, `get_bucket_stats`), formuliert die Antwort aber über Claude (Haiku 4.5, Anthropic API) in Fließtext statt als Tabelle, und akzeptiert die Frage auf Deutsch oder Englisch statt eines exakten Autocomplete-Werts.

Zwei gleichwertige Trigger auf denselben Query-Layer: der Slash-Command `/tw_ask frage` und eine @mention des Bots in einer normalen Nachricht (`@TW-Counter was kontert Darth Vader?`). Kein privilegiertes Gateway-Intent nötig für den @mention-Weg — Discord liefert `message.content` für Nachrichten, die den Bot mentionen, auch ohne Message-Content-Intent (dokumentierte Ausnahme, kein Workaround). `intents` in `bot.py` bleibt unverändert bei `Intents.default()`.

Implementiert in `smartbot.py`, nicht in `bot.py` selbst. Zwei-Schritt-Ablauf:

1. **Auflösung.** Das Modell bekommt die vollständige `character_names`-Liste als Enum-Constraint auf ein Tool (`lookup_counters`) mitgegeben und muss den in der Frage genannten Namen exakt einem Eintrag zuordnen — inklusive gängiger Community-Kürzel (z.B. "CLS" → "Commander Luke Skywalker"). Ist die Zuordnung nicht eindeutig (kein Treffer, oder mehrere plausible Treffer wie "Lord Vader" vs. "Darth Vader" — beide real existierende, unterschiedliche Einheiten), ruft das Modell stattdessen `report_unresolved` auf; die Antwort ist dann ein lokal formatiertes Template, nicht vom Modell selbst formuliert, um keine falschen Kandidatennamen zu riskieren.
2. **Synthese.** Nur bei erfolgreicher Auflösung: ein zweiter Modell-Call bekommt die echten `db.py`-Daten als Tool-Result und formuliert daraus die Antwort — ausdrücklich beschränkt darauf, nichts zu behaupten, was nicht in diesen Daten steht.

Benötigt `ANTHROPIC_API_KEY` (siehe Umgebungsvariablen unten). Erwartete Kosten bei gildentypischem Nutzungsvolumen: im Cent-Bereich pro Monat, nicht relevant genug, um Budget-Tracking zu rechtfertigen — siehe Anthropics Pricing-Seite für aktuelle Tokenpreise, falls sich das Nutzungsvolumen deutlich ändert.

## Setup

### 1. Discord-Anwendung

Eigene Anwendung im Developer Portal, getrennt von TB-Reminder — eigener Token, eigene OAuth2-Einladung. **Kein Privileged Gateway Intent nötig** — weder `Intents.members` (nichts im Code enumeriert `role.members`, alle Berechtigungsprüfungen laufen über `interaction.user.roles` direkt aus dem Interaction-Payload) noch `Intents.message_content` (die @mention-Anfragen aus dem Abschnitt „Natürlichsprachliche Anfragen" oben laufen über Discords dokumentierte Mention-Ausnahme, nicht über dieses Intent).

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
ANTHROPIC_API_KEY=
```

`ANTHROPIC_API_KEY` wird für `/tw_ask` benötigt (siehe Abschnitt oben) — Console-API-Key von `console.anthropic.com`, **nicht** dasselbe Konto/Billing wie ein Claude-Pro-Abo, auch wenn dieselbe E-Mail-Adresse für beides genutzt werden kann.

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

Dann: Stacks → Add stack → `docker-compose.yml` einfügen → sieben Umgebungsvariablen setzen (`DISCORD_TOKEN`, `GUILD_ID`, `SPECIALIST_ROLE_ID`, `MEMBER_ROLE_ID`, `MANAGER_IDS`, `OWNER_ID`, `ANTHROPIC_API_KEY` — `DATA_DIR` ist im Compose-File hart auf `/app/data` gesetzt, nicht per `.env` überschreibbar, muss passend zum Volume-Mount bleiben) → Deploy.

**`docker-compose.yml` muss dafür den `ANTHROPIC_API_KEY` im `environment:`-Block des Services durchreichen** (`- ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}`, wie die übrigen Variablen dort) — sonst kommt der Wert aus Portainer nie im Container an, selbst wenn er im Stack korrekt gesetzt ist. Prüfen, ob diese Zeile bereits vorhanden ist, bevor `/tw_ask` im deployten Container erwartet wird.

Charakterliste danach per `/tw_characterrefresh` befüllen — kein Zugriff auf das Container-Dateisystem nötig.

## Hinweise

- `DATA_DIR` enthält `counters.db`, `characters.json` und `swgoh_characters.html` — alle drei über `.gitignore`s `data/`-Eintrag ausgeschlossen, nicht einzeln aufgezählt (eine frühere Version zählte Dateinamen einzeln auf, das ließ `swgoh_characters.html` durchrutschen; es landete einmal in einem lokalen Commit, wurde aber vor dem Push per `git rm --cached` + `commit --amend` wieder entfernt — daher jetzt Verzeichnis-Ausschluss statt Dateiname-Enumeration).
- Bot-Neustart nötig nach jeder `.env`-Änderung — Environment-Variablen werden nur beim Prozessstart gelesen, kein Live-Reload.
- Discord-Token niemals in Chats, Issues oder Commit-Messages einfügen. Bei Verdacht auf Exposure: Developer Portal → Bot → Reset Token, `.env` aktualisieren, Bot neu starten.
