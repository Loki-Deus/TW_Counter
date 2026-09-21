# TW-Counter Discord Bot

Sammelt Territory-War-Konter (welcher angreifende Anführer schlägt welchen verteidigenden Anführer) aus der eigenen Match-Historie der Gilde. Schwesterprojekt zum **TB-Reminder-Bot** (Phasenansagen), gleicher Stack, getrennte Discord-Anwendung.

## Ablauf

**Ein Konter muss zuerst mit `/tw_add` angelegt werden, bevor `/tw_report` ein Ergebnis dazu akzeptiert.** `/tw_report` gegen einen nicht angelegten Konter schlägt mit einer klaren Fehlermeldung fehl, statt automatisch etwas anzulegen — das ist Absicht, kein Bug.

Jedes gemeldete Ergebnis wird nach Relic-Differenz (Angreifer − Verteidiger) in einen von drei Buckets sortiert: `unterlegen` (Δ ≤ −3), `ausgeglichen` (−2 bis +2), `überlegen` (Δ ≥ +3). Diese Grenzen leben ausschließlich als Konstanten in `config.py` (`UNDER_THRESHOLD`, `OVER_THRESHOLD`).

Zusätzlich zu Sieg/Niederlage kann ein Report optional die Anzahl erzielter **Banner** tragen. Eine Niederlage wird automatisch mit 0 Bannern gespeichert — ein trotzdem eingetragener Wert wird überschrieben, mit Hinweis in der Bestätigung. `/tw_lookup` zeigt den Banner-Durchschnitt pro Angreifer als vierte Spalte. Er wird nur über **Siege mit eingetragenem Banner-Wert** gebildet (Niederlagen zählen nicht als Nullen mit; Angreifer ohne einen solchen Sieg haben eine leere Zelle). Weil jeder gespeicherte Sieg auf einen 1.-Versuch-Wert normalisiert ist (16–20), liegt der Durchschnitt immer in diesem Bereich: 20 = keine eigene Einheit verloren, 16 = vier verloren. Die Sieg-/Niederlage-Quote steht in den Bucket-Spalten.

## Befehle

| Befehl | Berechtigung | Parameter | Verhalten |
|---|---|---|---|
| `/tw_add` | Rolle `SPECIALIST_ROLE_ID` | `verteidiger`, `angreifer` (Autocomplete) | Legt einen leeren Matchup an. Kein Admin-Override. |
| `/tw_report` | Rolle `MEMBER_ROLE_ID` | `verteidiger`, `verteidiger_relic`, `angreifer`, `angreifer_relic`, `ergebnis`, `banner` (optional) | Fügt eine Report-Zeile ein. `angreifer`-Autocomplete zeigt nur Angreifer mit bestehendem Konter gegen den gewählten `verteidiger`. Relic-Range (0–20) wird über `app_commands.Range` clientseitig erzwungen. `banner` bei Niederlage immer 0. |
| `/tw_lookup` | alle | `verteidiger` | Alle Konter gegen einen Verteidiger: Unterlegen/Ausgeglichen/Überlegen-Quote plus Banner-Durchschnitt, primär sortiert nach Ausgeglichen-Quote absteigend. |
| `/tw_zone_add` | Rolle `SPECIALIST_ROLE_ID` | `name`, `bild_url` (optional) | Legt eine TW-Zone an (Kartenreferenz für `/tw_zone_attack`). |
| `/tw_zone_attack` | Administrator ODER `MANAGER_IDS` | `zone`, `verteidiger_1`, `verteidiger_2`/`verteidiger_3` (optional), `mitgliederliste` (optional) | Empfiehlt Angreifer für eine Zone anhand von bis zu drei möglichen Verteidigern (Scouting-Unsicherheit), inklusive Hedge-Hinweis. `mitgliederliste=True` zeigt zusätzlich, welche Gildenmitglieder die empfohlenen Angreifer mit einsatzfähigem Relic-Level besitzen — registrierte Mitglieder werden gepingt, unregistrierte nur namentlich genannt (siehe Abschnitt „Roster-Tracking" unten). |
| `/tw_guild_set` | Administrator ODER `MANAGER_IDS` | `ally_code` | Hinterlegt die interne SWGOH-Gilden-ID über einen beliebigen bekannten Ally-Code. Einmaliger Setup-Schritt für den guild-weiten Roster-Refresh. |
| `/tw_register` | alle | `ally_code` | Verknüpft den eigenen Discord-Account mit einem (bereits bekannten) Ally-Code. Kein Signup-Zwang — Rosterdaten fließen auch ohne das, nur die Discord-Verknüpfung (für Pings) braucht diesen Schritt. War der Account bereits mit einem anderen Ally-Code verknüpft, wird diese Verknüpfung gelöst und die neue gesetzt (die Antwort nennt das). |
| `/tw_register_other` | Administrator ODER `MANAGER_IDS` | `user`, `ally_code` | Wie `/tw_register`, aber für einen anderen Discord-Account — für Mitglieder, die sich nicht selbst registriert haben. Überschreibt eine bestehende Verknüpfung des Ally-Codes und nennt das in der Antwort. |
| `/tw_register_list` | Administrator ODER `MANAGER_IDS` | `nur_offene` (optional) | Listet alle bekannten Spieler mit Ally-Code, getrennt nach „nicht verknüpft" und „verknüpft" (mit Discord-Mention und -ID). `nur_offene: True` zeigt nur die noch nicht Registrierten. Ephemer, bei Bedarf auf mehrere Nachrichten verteilt. |
| `/tw_roster_refresh` | Administrator ODER `MANAGER_IDS` | — | Aktualisiert die Rosterdaten der gesamten Gilde sofort, statt auf den nächtlichen automatischen Refresh zu warten. |
| `/tw_ask` | alle | `frage` (Freitext, Deutsch oder Englisch) | Natürlichsprachliche Anfrage, z.B. „was kontert Darth Vader?". Alternativ: Bot in einer normalen Nachricht @mentionen. |
| `/tw_delete` | Administrator ODER `MANAGER_IDS` | `verteidiger`, `angreifer` | Löscht Matchup inkl. aller Reports (CASCADE), nach Inline-Bestätigung. |
| `/tw_celebrate` | alle | — | Zeigt die Top 3 Melder der meisten TW-Reports. |
| `/tw_help` | alle | — | Befehlsübersicht direkt in Discord, ggf. auf mehrere Nachrichten aufgeteilt (Discords 2000-Zeichen-Limit). |

Alle rollen-/ID-basierten Berechtigungen sind unabhängig voneinander (`is_tw_specialist`, `is_member`, `is_manager` in `bot.py`) — keine impliziert eine andere. `/tw_lookup`, `/tw_ask`, `/tw_celebrate` und `/tw_help` sind für alle offen und laufen über keine dieser Funktionen.

`/tw_characterrefresh` (manueller HTML-Upload für die Charakterliste) wurde entfernt — siehe Abschnitt „Charakter-Katalog" unten.

## Datenmodell

Event-Log, kein Aggregat: `counters` ist der Matchup-Katalog, `reports` ist das Log einzelner Meldungen (inkl. optionalem `banners`-Feld). Jede Bucket-Aggregation wird zur Abfragezeit aus `reports` berechnet, nie persistiert.

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
    result, banners, reported_by_id, reported_at
);

CREATE TABLE zones (
    id, name, image_url, created_at
);

-- Roster-Mirror (siehe Abschnitt "Roster-Tracking")
CREATE TABLE players (
    ally_code PRIMARY KEY, player_name, discord_id, last_synced
);
CREATE TABLE roster_units (
    ally_code REFERENCES players(ally_code) ON DELETE CASCADE,
    unit_id, rarity, gear_tier, relic_tier, omicrons,
    PRIMARY KEY (ally_code, unit_id)
);
CREATE TABLE swgoh_guild (
    id CHECK (id = 1), guild_id, guild_name, set_at
);
CREATE TABLE unit_names (
    unit_id PRIMARY KEY, display_name, updated_at
);
```

`banners` ist über eine schema-verträgliche Migration (`ALTER TABLE`, geschützt gegen doppelte Ausführung) nachgezogen — ein Redeploy auf einer bereits existierenden Datenbank verliert keine Daten.

Exaktes Schema in `db.py`.

## Roster-Tracking

Der Bot spiegelt die Kaderdaten (Gear, Relic, Rarity) der gesamten Gilde automatisch — kein manuelles Signup pro Spieler nötig. Datenquelle ist eine **selbst gehostete `swgoh-comlink`-Instanz** (siehe `docker-compose.yml`), die read-only, unauthentifiziert öffentliche Spieler-APIs des Spiels abfragt.

Ablauf:

1. **Einmalig:** `/tw_guild_set` mit einem beliebigen bekannten Ally-Code — löst die interne SWGOH-Gilden-ID auf und hinterlegt sie.
2. **Automatisch, nächtlich:** der Bot zieht die komplette Mitgliederliste der Gilde über comlink und aktualisiert jeden gefundenen Spieler — unabhängig davon, ob dieser je `/tw_register` genutzt hat.
3. **Optional, pro Spieler:** `/tw_register` verknüpft die eigene Discord-ID mit dem eigenen (ohnehin schon erfassten) Ally-Code. Das ist NICHT die Voraussetzung für Rosterdaten, sondern nur dafür, dass `/tw_zone_attack` diese Person konkret in Discord pingen kann statt nur ihren Ingame-Namen zu nennen.
4. **Bei Bedarf sofort:** `/tw_roster_refresh` (Manager/Admin) statt auf den nächtlichen Task zu warten.
5. **Lücken schließen:** `/tw_register_list` (Manager/Admin) zeigt, wer noch nicht verknüpft ist; `/tw_register_other` verknüpft diese Personen stellvertretend.

`/tw_zone_attack mitgliederliste=True` filtert zusätzlich auf ein einsatzfähiges Relic-Level (Standard: mindestens Relic 1, konfigurierbar über `_MIN_DISPLAY_RELIC_FOR_ATTACK` in `bot.py`) — Level-1-Besitz ohne Relic zählt nicht als brauchbare Angriffsoption. Comlinks roher `relic.currentTier`-Wert entspricht NICHT direkt der im Spiel angezeigten Relic-Stufe (Rohwert − 2 = echte Stufe, siehe `config.relic_tier_to_display()`).

Das gesamte Feature (`/tw_guild_set`, `/tw_register`, `/tw_roster_refresh`, die zugehörigen Refresh-Tasks) sitzt hinter dem Flag `ROSTER_FEATURE_ENABLED` in `bot.py` — aktuell `True`. Auf `False` setzen deaktiviert alle drei Commands vollständig (Discord bekommt sie dann gar nicht erst zum Registrieren angeboten), ohne den restlichen Bot zu beeinträchtigen.

## Charakter-Katalog

Die Autocomplete-Quelle für Anführernamen (`/tw_add`, `/tw_report`, `/tw_lookup`, `/tw_zone_attack`, `/tw_ask`) kommt **direkt von comlink**, nicht mehr aus einer manuell gepflegten Datei:

- Comlinks `get_game_data()` liefert den vollständigen Einheitenkatalog (~11.000 Einträge inkl. Raid-Bosse, NPCs, interner Einheiten).
- `get_localization()` löst die internen Namensschlüssel in lesbaren Text auf.
- Autocomplete zeigt davon **nur Einheiten, die mindestens ein Gildenmitglied laut letztem Roster-Refresh tatsächlich besitzt** (`db.get_owned_unit_display_names()`) — kein ungefilterter 11.000-Einheiten-Katalog voller Raid-Bosse.

Das ersetzt die frühere Lösung: swgoh.gg/characters/ lief hinter einer aktiven Cloudflare-JS-Challenge, weshalb die Charakterliste manuell im Browser gespeichert und per `/tw_characterrefresh` hochgeladen werden musste. Dieser Weg (`character_list.py`, `/tw_characterrefresh`) wurde vollständig entfernt.

**Reale Abhängigkeit, die das mit sich bringt:** die komplette Autocomplete-Funktion hängt jetzt an comlink UND an mindestens einem abgeschlossenen Roster-Refresh. Ohne das ist die Vorschlagsliste leer (kein Absturz, aber auch keine Vorschläge) — anders als vorher, wo die Charakterliste komplett unabhängig von jedem externen Dienst war.

**Bekannte Lücke:** ob comlinks lokalisierte Namen exakt mit historisch bereits gespeicherten `counters`-Einträgen übereinstimmen, lässt sich stichprobenartig über einen Soll-Ist-Abgleich prüfen (Konter-Katalog-Namen gegen `unit_names.display_name`). Bei diesem Bot bereits geprüft: 68 von 68 vorhandenen Namen stimmen exakt überein.

## Natürlichsprachliche Anfragen (`/tw_ask`)

Zweiter, rein lesender Zugriffspfad zusätzlich zu `/tw_lookup` — nutzt dieselben `db.py`-Funktionen, formuliert die Antwort aber über Claude (Haiku 4.5, Anthropic API) in Fließtext statt als Tabelle, und akzeptiert die Frage auf Deutsch oder Englisch statt eines exakten Autocomplete-Werts.

Zwei gleichwertige Trigger: der Slash-Command `/tw_ask frage` und eine @mention des Bots in einer normalen Nachricht. Kein privilegiertes Gateway-Intent nötig — Discord liefert `message.content` für Nachrichten, die den Bot mentionen, auch ohne Message-Content-Intent (dokumentierte Ausnahme).

Implementiert in `smartbot.py`. Zwei-Schritt-Ablauf: Auflösung (Modell ordnet den genannten Namen exakt einem Eintrag aus dem — jetzt comlink-basierten — Charakter-Katalog zu, inkl. Community-Slang) und Synthese (zweiter Modell-Call formuliert die Antwort ausschließlich aus den echten `db.py`-Daten).

Benötigt `ANTHROPIC_API_KEY`.

## Setup

### 1. Discord-Anwendung

Eigene Anwendung im Developer Portal, getrennt von TB-Reminder. **Kein Privileged Gateway Intent nötig** (weder `Intents.members` noch `Intents.message_content`).

OAuth2 → URL Generator: Scopes `bot` + `applications.commands`, Bot-Permission `Send Messages`.

### 2. IDs

Developer Mode in Discord aktivieren, dann:

- `GUILD_ID` — Discord-Server-ID (NICHT die SWGOH-Gilden-ID, siehe unten — zwei völlig unabhängige Namensräume)
- `SPECIALIST_ROLE_ID` — Rolle für `/tw_add`, `/tw_zone_add`
- `MEMBER_ROLE_ID` — Rolle für `/tw_report`, `/tw_register`
- `MANAGER_IDS` — komma-separierte User-IDs für `/tw_delete`, `/tw_zone_attack`, `/tw_guild_set`, `/tw_roster_refresh`, zusätzlich zu Administrator-Rechten
- `OWNER_ID` — historisch für `/tw_characterrefresh` gedacht, das inzwischen entfernt wurde. Aktuell funktional ungenutzt, aber weiterhin als Pflicht-Env-Var geladen (`config.py`) — bewusst nicht in derselben Änderung mit entfernt, um den Deploy-Blast-Radius klein zu halten.

**Wichtig, aus eigener Erfahrung:** Rollen-IDs immer über die Mitgliederliste verifizieren, nicht nur über Server-Einstellungen → Rollen → Rolle kopieren.

Die **SWGOH-Gilden-ID** wird NICHT per Env-Var gesetzt, sondern einmalig über `/tw_guild_set` (siehe Abschnitt „Roster-Tracking").

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
COMLINK_URL=
COMLINK_APP_NAME=
```

`ANTHROPIC_API_KEY` wird für `/tw_ask` benötigt — Console-API-Key von `console.anthropic.com`, **nicht** dasselbe Konto/Billing wie ein Claude-Pro-Abo.

`COMLINK_URL` zeigt auf die selbst gehostete comlink-Instanz — im Docker-Compose-Setup automatisch `http://comlink:3000` (Compose-internes Netzwerk, kein manuelles Setzen nötig, siehe `docker-compose.yml`). Für lokales Ausführen außerhalb Docker: `http://localhost:3000` oder wo auch immer comlink erreichbar ist.

`COMLINK_APP_NAME` identifiziert diesen Bot gegenüber den Spiel-APIs (comlink-Pflichtfeld). Frei wählbar, Default `tw-counter-bot`.

`BOT_TIMEZONE` wird aktuell von keinem Code-Pfad gelesen — für spätere Verwendung vorgemerkt.

### 4. Lokal ausführen

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
mkdir -p data
python bot.py
```

Für `/tw_ask`, `/tw_zone_attack` mit `mitgliederliste`, und die komplette Charakter-Autocomplete: eine erreichbare comlink-Instanz wird vorausgesetzt (siehe unten).

### 5. Deployment (Docker / Portainer)

Zwei Services: `tw-counter` (der Bot) und `comlink` (selbst gehostete `ghcr.io/swgoh-utils/swgoh-comlink`-Instanz, siehe `docker-compose.yml`). `comlink` braucht kein Port-Mapping nach außen — nur `tw-counter` im internen Compose-Netzwerk muss ihn erreichen können, über den Service-Namen (`http://comlink:3000`).

Volume **vor** dem ersten Deploy anlegen — `docker-compose.yml` deklariert `tw-counter-data` als `external: true`:

- Portainer → Volumes → Add volume → Name exakt `tw_counter_tw-counter-data`, Driver `local`

Dann: Stacks → Add stack → `docker-compose.yml` einfügen → Umgebungsvariablen setzen (siehe Abschnitt oben — `DATA_DIR` ist im Compose-File hart auf `/app/data` gesetzt, `COMLINK_URL` hart auf `http://comlink:3000`, beide nicht per `.env` überschreibbar) → Deploy.

Nach dem ersten erfolgreichen Start: einmalig `/tw_guild_set` ausführen (siehe „Roster-Tracking"), danach läuft alles automatisch.

## Hinweise

- `DATA_DIR` enthält `counters.db` — über `.gitignore`s `data/`-Eintrag ausgeschlossen.
- Bot-Neustart nötig nach jeder `.env`-Änderung.
- Discord-Token niemals in Chats, Issues oder Commit-Messages einfügen. Bei Verdacht auf Exposure: Developer Portal → Bot → Reset Token, `.env` aktualisieren, Bot neu starten.
- Comlink ist nicht mit EA/Capital Games affiliiert und spricht APIs an, die nie als öffentliches Developer-API veröffentlicht wurden — De-facto-Standard im SWGOH-Tool-Ökosystem, aber keine vertraglich abgesicherte Garantie. Eigene Einschätzung nötig, insbesondere bei kommerzieller Nutzung.
