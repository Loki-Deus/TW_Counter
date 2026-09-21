"""
SQLite-Datenzugriffsschicht für den TW-Counter-Bot.

Event-Log-Modell (siehe TW_Counter_Bot_CONTEXT.md, Abschnitt "Datenmodell"):
`counters` ist der Matchup-Katalog (defending_leader, attacking_leader),
`reports` ist das Event-Log der einzelnen Meldungen. Jede Bucket-Aggregation
wird zur Abfragezeit aus `reports` abgeleitet, nie persistiert — das erlaubt
rückwirkende Neu-Bucketung, falls sich die Bucket-Grenzen je verschieben.

`delta` (attacker_relic - defender_relic) ist eine
GENERATED ALWAYS AS ... VIRTUAL Spalte auf `reports` — von der Engine
abgeleitet, nicht applikationsseitig geschrieben. Erfordert SQLite >= 3.31.
Verifiziert: dieses Environment läuft mit SQLite 3.45.1 (python3 -c
"import sqlite3; print(sqlite3.sqlite_version)") — für das Ziel-Deployment
(python:3.12-slim im Dockerfile) nicht separat geprüft, aber jede
python:3.12-Variante bringt eine deutlich neuere SQLite-Version mit als die
Mindestanforderung.
"""

import json
import logging
import sqlite3
import time
from contextlib import contextmanager

from config import DB_PATH, OVER_THRESHOLD, UNDER_THRESHOLD

logger = logging.getLogger(__name__)


class CounterExistsError(Exception):
    """/tw_add: Matchup (defending_leader, attacking_leader) existiert bereits."""


class CounterNotFoundError(Exception):
    """/tw_report oder /tw_delete: referenziertes Matchup existiert nicht."""


class ZoneExistsError(Exception):
    """/tw_zone_add: Zone mit diesem Namen existiert bereits."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    defending_leader  TEXT NOT NULL,
    attacking_leader  TEXT NOT NULL,
    submitted_by_id   TEXT NOT NULL,
    submitted_by_name TEXT NOT NULL,
    submitted_at      INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_counter_matchup
    ON counters(defending_leader, attacking_leader);

CREATE TABLE IF NOT EXISTS reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    counter_id     INTEGER NOT NULL REFERENCES counters(id) ON DELETE CASCADE,
    attacker_relic INTEGER NOT NULL,
    defender_relic INTEGER NOT NULL,
    delta          INTEGER GENERATED ALWAYS AS (attacker_relic - defender_relic) VIRTUAL,
    result         INTEGER NOT NULL,
    banners        INTEGER,
    reported_by_id TEXT NOT NULL,
    reported_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_counter ON reports(counter_id);

-- Zonen-Referenz für /tw_zone_attack: reine Nachschlagetabelle (Name +
-- optionale Bild-URL), keine Fremdschlüssel auf counters/reports. Eine
-- Zone ist Kartengeometrie, keine Match-Historie -- ändert sich nur, wenn
-- sich das TW-Kartenlayout ändert, nicht pro Report oder pro Season.
CREATE TABLE IF NOT EXISTS zones (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    image_url  TEXT,
    created_at INTEGER NOT NULL
);

-- Roster-Mirror (siehe roster.py). players ist über ally_code verkettet,
-- NICHT über discord_id -- der Refresh läuft guild-weit über comlinks
-- Gilden-Mitgliederliste (fetch_guild_members()), nicht mehr nur für
-- Discord-Nutzer, die manuell /tw_register genutzt haben. Ein Spieler hat
-- also Rosterdaten, OHNE je einen Discord-Account verknüpft zu haben --
-- discord_id ist deshalb nullable, nicht mehr der Primärschlüssel.
-- /tw_register setzt nur noch diese optionale Verknüpfung.
CREATE TABLE IF NOT EXISTS players (
    ally_code   TEXT PRIMARY KEY,
    player_name TEXT,
    discord_id  TEXT UNIQUE,
    player_id   TEXT,
    last_synced INTEGER
);
-- Kein "CREATE INDEX ... ON players(player_id)" hier -- das lief einmal
-- GEGEN eine bestehende players-Tabelle, bevor die untenstehende Migration
-- die Spalte überhaupt angelegt hatte ("no such column: player_id"), weil
-- CREATE TABLE IF NOT EXISTS bei einer schon existierenden Tabelle ein
-- No-Op ist. Der Index wird stattdessen ausschließlich in
-- _migrate_players_add_player_id() erzeugt, NACHDEM die Spalte sicher
-- existiert -- exakt derselbe Fehler wie einmal zuvor bei
-- idx_roster_units_ally, siehe dortige Historie in db.py.

-- roster_units wird bei jedem Refresh komplett ersetzt (DELETE + INSERT,
-- siehe save_roster()), nicht inkrementell gepflegt -- ein Refresh liefert
-- ohnehin den vollständigen aktuellen Kaderstand, Diffing brächte hier
-- keinen Vorteil.
CREATE TABLE IF NOT EXISTS roster_units (
    ally_code   TEXT NOT NULL REFERENCES players(ally_code) ON DELETE CASCADE,
    unit_id     TEXT NOT NULL,
    rarity      INTEGER,
    gear_tier   INTEGER,
    relic_tier  INTEGER,
    omicrons    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (ally_code, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_roster_units_ally ON roster_units(ally_code);

-- Singleton-Tabelle (id per CHECK auf genau 1 erzwungen): die interne
-- SWGOH-Gilden-ID, NICHT zu verwechseln mit config.GUILD_ID (Discord-
-- Server-ID für die Slash-Command-Registrierung -- zwei völlig
-- unabhängige Namensräume, die nur zufällig beide "Gilde"/"guild" heißen).
-- Wird über /tw_guild_set gesetzt, nicht per Env-Var -- ein einmaliger
-- Admin-Vorgang, kein Redeploy-Grund.
CREATE TABLE IF NOT EXISTS swgoh_guild (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    guild_id   TEXT NOT NULL,
    guild_name TEXT,
    set_at     INTEGER NOT NULL
);

-- unit_id <-> Anzeigename-Brücke (siehe roster.fetch_unit_names()). Bei
-- jedem Refresh komplett ersetzt (DELETE + INSERT), analog zu
-- roster_units -- Comlinks Einheitenliste ändert sich mit Patches, nicht
-- inkrementell aus Bot-Sicht.
--
-- WICHTIG, nicht durch dieses Schema lösbar: display_name kommt aus
-- Comlinks Lokalisierung (ENG_US), NICHT aus character_list.py's
-- swgoh.gg-Scrape, der die bereits bestehenden Anzeigenamen in
-- counters.attacking_leader/defending_leader liefert. Ob beide Quellen für
-- jede Einheit exakt denselben String produzieren, ist unverifiziert --
-- siehe bot.py's format_zone_attack() für den Umgang mit Nichttreffern.
CREATE TABLE IF NOT EXISTS unit_names (
    unit_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);
"""

# Einzige SQL-seitige Quelle für die Bucket-Grenzen — interpoliert aus
# config.py, damit dieser CASE nie von bucket_for_delta() abweichen kann.
# (Reine int-Konstanten aus config.py, keine Nutzereingabe — unkritisch
# als String-Interpolation.)
_BUCKET_CASE = f"""
        CASE
            WHEN r.delta <= {UNDER_THRESHOLD} THEN 'under'
            WHEN r.delta <  {OVER_THRESHOLD}  THEN 'even'
            ELSE 'over'
        END
"""


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        _migrate_players_schema(conn)
        conn.executescript(SCHEMA)
        _migrate_reports_add_banners(conn)
        _migrate_players_add_player_id(conn)


def _migrate_players_schema(conn: sqlite3.Connection) -> None:
    """
    MUSS vor conn.executescript(SCHEMA) laufen, nicht danach -- anders als
    _migrate_reports_add_banners() unten. CREATE TABLE IF NOT EXISTS legt
    eine Tabelle mit der ALTEN Spaltenform nicht neu an, sie existiert ja
    schon; die alte Form muss also VORHER weg, damit SCHEMAs CREATE TABLE
    danach mit der neuen Form frisch greift.

    Frühere Version von players nutzte discord_id als Primärschlüssel
    (Selbstregistrierung pro Discord-Nutzer); ally_code ist jetzt der
    Primärschlüssel, weil der Roster-Refresh guild-weit über comlinks
    Mitgliederliste läuft, nicht mehr nur für einzeln per /tw_register
    verknüpfte Discord-Nutzer.

    Erkennungslogik, siehe Bug-Historie: die ursprüngliche Version prüfte
    "ally_code" not in columns" -- FALSCH, weil die alte Tabellenform
    ally_code BEREITS als gewöhnliche UNIQUE-Spalte hatte, nur nicht als
    Primärschlüssel. Diese Prüfung griff nie, players wurde nie gedroppt,
    und SCHEMAs "CREATE INDEX idx_roster_units_ally ON roster_units(ally_code)"
    schlug beim ersten echten Produktivstart mit "no such column: ally_code"
    fehl -- die ALTE roster_units-Tabelle (Spalte discord_id, kein
    ally_code) blieb durch die wirkungslose CREATE TABLE IF NOT EXISTS
    bestehen. registered_at existiert dagegen NUR in der alten Form (siehe
    SCHEMA oben: die neue players-Definition hat dieses Feld nicht mehr) --
    eindeutiger Marker statt eines mehrdeutigen.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(players)").fetchall()}
    if "registered_at" in columns:
        logger.warning(
            "players-Tabelle in alter Form gefunden (discord_id als "
            "Primärschlüssel) -- wird samt roster_units gedroppt und in "
            "neuer Form (ally_code als Primärschlüssel) neu angelegt. "
            "Vorherige Ally-Code-Registrierungen sind danach weg."
        )
        conn.execute("DROP TABLE IF EXISTS roster_units")
        conn.execute("DROP TABLE IF EXISTS players")


def _migrate_reports_add_banners(conn: sqlite3.Connection) -> None:
    """
    ALTER TABLE ... ADD COLUMN ist in SQLite -- anders als die CREATE TABLE
    IF NOT EXISTS-Statements in SCHEMA -- NICHT idempotent: ein zweiter
    Aufruf gegen eine Tabelle, die die Spalte schon hat, wirft. Für frische
    Installationen ist banners bereits Teil von SCHEMAs reports-Definition
    oben und diese Funktion tut nichts; für bereits deployte Datenbanken
    (vor dieser Änderung angelegt) fehlt die Spalte und wird hier
    nachgezogen. Der PRAGMA-Check davor macht das für beide Fälle sicher
    wiederholt aufrufbar, bei jedem init_db()-Lauf (also bei jedem
    Bot-Start).
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}
    if "banners" not in columns:
        conn.execute("ALTER TABLE reports ADD COLUMN banners INTEGER")


def _migrate_players_add_player_id(conn: sqlite3.Connection) -> None:
    """
    Analog zu _migrate_reports_add_banners() -- ALTER TABLE ADD COLUMN ist
    nicht idempotent, PRAGMA-Check davor macht das sicher wiederholbar.

    player_id (comlinks interne Spieler-ID) wird für
    db.delete_players_not_in() gebraucht: Gilden-Abgänge zuverlässig zu
    erkennen braucht einen stabilen Schlüssel, der direkt mit
    fetch_guild_members()'s Rückgabewerten (auch playerIds) vergleichbar
    ist -- ally_code allein reicht dafür nicht, weil die Gilden-
    Mitgliederliste selbst nur playerIds liefert, nie Ally-Codes (siehe
    roster.fetch_guild_members()-Docstring). Bereits VOR dieser Änderung
    gespeicherte Spieler haben hier NULL, bis der nächste Refresh sie neu
    schreibt -- absichtlich kein Problem: db.delete_players_not_in()
    lässt player_id IS NULL-Zeilen unangetastet, siehe dortiger Docstring.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(players)").fetchall()}
    if "player_id" not in columns:
        conn.execute("ALTER TABLE players ADD COLUMN player_id TEXT")
    # Unconditional (IF NOT EXISTS), NICHT nur im obigen if-Zweig: muss
    # sowohl nach einem frischen ALTER TABLE als auch bei einer bereits
    # vorhandenen Spalte (frischer Install über SCHEMA) sicher greifen.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_players_player_id ON players(player_id)")


def add_counter(
    defending_leader: str,
    attacking_leader: str,
    submitted_by_id: str,
    submitted_by_name: str,
) -> int:
    """Legt einen leeren Matchup an (keine Relic-/Ergebnisdaten). Raises CounterExistsError bei Duplikat."""
    with get_connection() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO counters "
                "(defending_leader, attacking_leader, submitted_by_id, submitted_by_name, submitted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    defending_leader,
                    attacking_leader,
                    submitted_by_id,
                    submitted_by_name,
                    int(time.time()),
                ),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise CounterExistsError(
                f"Matchup {attacking_leader} vs {defending_leader} existiert bereits."
            ) from e


def get_counter(defending_leader: str, attacking_leader: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM counters WHERE defending_leader = ? AND attacking_leader = ?",
            (defending_leader, attacking_leader),
        ).fetchone()


def get_attackers_for_defender(defending_leader: str) -> list[str]:
    """Alle existierenden Angreifer-Konter gegen einen Verteidiger — Basis für /tw_report Autocomplete."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT attacking_leader FROM counters WHERE defending_leader = ? ORDER BY attacking_leader",
            (defending_leader,),
        ).fetchall()
        return [r["attacking_leader"] for r in rows]


def add_report(
    defending_leader: str,
    attacking_leader: str,
    attacker_relic: int,
    defender_relic: int,
    result: int,
    reported_by_id: str,
    banners: int | None = None,
) -> int:
    """
    Fügt eine Report-Zeile ein. result: 1 = Sieg, 0 = Niederlage.
    banners: optional, nicht erzwungen (Stakeholder-Vorgabe) -- bleibt bei
    Nichtangabe NULL statt 0, damit ein fehlender Wert den späteren
    Durchschnitt in get_banner_stats() nicht nach unten verfälscht.
    Relic-Range-Validierung (MIN_RELIC..MAX_RELIC) ist bewusst nicht hier,
    sondern Sache der Command-Ebene in bot.py — dort entsteht die
    nutzerseitige Fehlermeldung vor dem DB-Zugriff.
    """
    with get_connection() as conn:
        counter = conn.execute(
            "SELECT id FROM counters WHERE defending_leader = ? AND attacking_leader = ?",
            (defending_leader, attacking_leader),
        ).fetchone()
        if counter is None:
            raise CounterNotFoundError(
                f"Kein Matchup {attacking_leader} vs {defending_leader} — erst /tw_add nutzen."
            )

        cur = conn.execute(
            "INSERT INTO reports "
            "(counter_id, attacker_relic, defender_relic, result, banners, reported_by_id, reported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                counter["id"],
                attacker_relic,
                defender_relic,
                result,
                banners,
                reported_by_id,
                int(time.time()),
            ),
        )
        return cur.lastrowid


def delete_counter(defending_leader: str, attacking_leader: str) -> bool:
    """Löscht ein Matchup (CASCADE auf reports). Gibt False zurück, wenn es nicht existierte."""
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM counters WHERE defending_leader = ? AND attacking_leader = ?",
            (defending_leader, attacking_leader),
        )
        return cur.rowcount > 0


def get_bucket_stats(defending_leader: str) -> list[sqlite3.Row]:
    """
    Kern-Aggregation für /tw_lookup — pro (attacking_leader, bucket) Summe
    aus Siegen/Niederlagen. INNER JOIN: Konter ohne Reports tauchen hier
    NICHT auf. Der Aufrufer (bot.py) muss reportlose Konter separat über
    get_attackers_for_defender() ergänzen und als "keine Berichte"
    formatieren — das ist hier bewusst nicht mitgemacht, weil es
    Presentation-Logik ist, keine Datenzugriffs-Logik.
    """
    with get_connection() as conn:
        return conn.execute(
            f"""
            SELECT
                c.attacking_leader,
                {_BUCKET_CASE} AS bucket,
                SUM(r.result)            AS wins,
                COUNT(*) - SUM(r.result) AS losses
            FROM counters c
            JOIN reports r ON r.counter_id = c.id
            WHERE c.defending_leader = ?
            GROUP BY c.attacking_leader, bucket
            """,
            (defending_leader,),
        ).fetchall()


def get_banner_stats(defending_leader: str) -> list[sqlite3.Row]:
    """
    Durchschnittliche Banner-Anzahl pro Angreifer gegen defending_leader,
    berechnet NUR über SIEGE mit eingetragenem Banner-Wert.
    ANDERS als get_bucket_stats() NICHT nach Bucket gruppiert: Banner ist
    ein optionales, vom Relic-Delta unabhängiges Feld -- es steht als
    eigene, vierte Spalte neben den drei Buckets, nicht als deren
    Aufteilung (Stakeholder-Vorgabe, siehe Chat).

    Bewusst nur Siege (geändert): Niederlagen tragen per Spielregel immer 0
    Banner (bot.py erzwingt das). Vorher flossen sie als Nullen in den
    Durchschnitt ein, während Siege OHNE eingetragenen Banner-Wert (Feld ist
    optional) gar nicht zählten. Bei Angreifern mit vielen Siegen ohne
    Banner-Angabe ergab das Werte weit unter dem Bereich 16-20, den ein
    korrigierter Sieg immer hat (Beispiel: 19 Siege, 5 Niederlagen, nur 8
    Siege mit Banner -> Schnitt 11,1 statt 18). Die Sieg-/Niederlage-Quote
    steht ohnehin in den Bucket-Spalten; die Banner-Spalte soll nur zeigen,
    wie sauber ein Sieg mit diesem Angreifer typischerweise ausfällt
    (20 = keine eigene Einheit verloren, 16 = vier verloren, siehe
    config.correct_banner_penalty()).

    Angreifer ohne einen einzigen Sieg mit Banner-Wert fehlen im Ergebnis
    (leere Zelle in /tw_lookup) statt mit 0.0 aufzutauchen. banner_count ist
    die Anzahl der Siege, die in den Schnitt eingehen.
    """
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT
                c.attacking_leader,
                AVG(r.banners) AS avg_banners,
                COUNT(*)       AS banner_count
            FROM counters c
            JOIN reports r ON r.counter_id = c.id
            WHERE c.defending_leader = ? AND r.result = 1 AND r.banners IS NOT NULL
            GROUP BY c.attacking_leader
            """,
            (defending_leader,),
        ).fetchall()


def get_top_reporters(limit: int = 3) -> list[sqlite3.Row]:
    """
    Aggregation für /tw_celebrate: zählt reports pro reported_by_id.
    Reine ID-Zählung — Anzeige-Namen werden nicht in reports gespeichert,
    Auflösung zu Discord-Displaynamen ist Sache von bot.py (Live-Lookup
    über interaction.guild, siehe resolve_display_name()).
    """
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT reported_by_id, COUNT(*) AS report_count
            FROM reports
            GROUP BY reported_by_id
            ORDER BY report_count DESC, reported_by_id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def add_zone(name: str, image_url: str | None) -> int:
    """Legt eine TW-Zone an. Raises ZoneExistsError bei Namens-Duplikat."""
    with get_connection() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO zones (name, image_url, created_at) VALUES (?, ?, ?)",
                (name, image_url, int(time.time())),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise ZoneExistsError(f"Zone '{name}' existiert bereits.") from e


def get_zone_names() -> list[str]:
    """Für Zonen-Autocomplete -- direkt aus der DB gelesen, kein In-Memory-
    Cache wie bei character_names: die Tabelle ist klein (eine Handvoll
    Zonen pro Kartenlayout) und ändert sich selten genug, dass ein
    zusätzlicher Cache-Invalidierungspfad seinen Aufwand nicht wert ist."""
    with get_connection() as conn:
        rows = conn.execute("SELECT name FROM zones ORDER BY name").fetchall()
        return [r["name"] for r in rows]


def get_zone(name: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM zones WHERE name = ?", (name,)).fetchone()


def set_swgoh_guild(guild_id: str, guild_name: str | None) -> None:
    """
    Setzt/ersetzt die eine hinterlegte SWGOH-Gilden-ID -- Singleton, id=1
    per CHECK-Constraint in SCHEMA erzwungen (dieser Bot verwaltet aktuell
    genau eine Gilde). NICHT zu verwechseln mit config.GUILD_ID (Discord-
    Server-ID).
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO swgoh_guild (id, guild_id, guild_name, set_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                guild_id = excluded.guild_id,
                guild_name = excluded.guild_name,
                set_at = excluded.set_at
            """,
            (guild_id, guild_name, int(time.time())),
        )


def get_swgoh_guild() -> sqlite3.Row | None:
    """None, wenn noch nie /tw_guild_set gelaufen ist."""
    with get_connection() as conn:
        return conn.execute("SELECT * FROM swgoh_guild WHERE id = 1").fetchone()


def upsert_player(ally_code: str, player_name: str, player_id: str | None = None) -> None:
    """
    Legt einen Spieler an oder aktualisiert Name und (falls angegeben)
    player_id -- fasst discord_id NICHT an. Für den guild-weiten Refresh:
    jedes über roster.fetch_guild_members() gefundene Mitglied bekommt
    hier einen Datensatz, unabhängig davon, ob es je /tw_register genutzt
    hat.

    player_id per COALESCE geschrieben, nicht direkt überschrieben: ein
    Aufrufer ohne bekannte player_id (player_id=None) darf eine bereits
    gespeicherte NICHT stillschweigend auf NULL zurücksetzen -- das würde
    db.delete_players_not_in() sonst nachträglich wieder blind machen für
    genau diesen Spieler.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO players (ally_code, player_name, player_id)
            VALUES (?, ?, ?)
            ON CONFLICT(ally_code) DO UPDATE SET
                player_name = excluded.player_name,
                player_id = COALESCE(excluded.player_id, players.player_id)
            """,
            (ally_code, player_name, player_id),
        )


def link_discord_id(ally_code: str, discord_id: str) -> None:
    """
    /tw_register: verknüpft eine Discord-ID mit einem Ally-Code. Legt
    KEINEN neuen players-Datensatz an -- der Aufrufer (bot.py) ruft vorher
    im selben Ablauf upsert_player() bzw. save_roster(), die das schon tun.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE players SET discord_id = ? WHERE ally_code = ?",
            (discord_id, ally_code),
        )


def get_player_by_ally_code(ally_code: str) -> sqlite3.Row | None:
    """Ein Spieler über den Ally-Code, oder None, wenn unbekannt."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM players WHERE ally_code = ?", (ally_code,)
        ).fetchone()


def get_player_by_discord_id(discord_id: str) -> sqlite3.Row | None:
    """Der Spieler, dem diese Discord-ID zugeordnet ist, oder None.
    discord_id ist UNIQUE -- es gibt höchstens einen."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM players WHERE discord_id = ?", (discord_id,)
        ).fetchone()


def unlink_discord_id(discord_id: str) -> None:
    """Löst die Discord-Verknüpfung dieser ID (setzt discord_id auf NULL),
    lässt den players-Datensatz und das Roster unangetastet. Nötig vor
    link_discord_id(), wenn dieselbe Discord-ID bereits an einem ANDEREN
    Ally-Code hängt: discord_id ist UNIQUE, das direkte UPDATE würde mit
    sqlite3.IntegrityError scheitern (ließ sich vorher über /tw_register mit
    einem zweiten Ally-Code auslösen)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE players SET discord_id = NULL WHERE discord_id = ?",
            (discord_id,),
        )


def get_all_players() -> list[sqlite3.Row]:
    """
    Alle bekannten Spieler, ob mit Discord verknüpft oder nicht -- nicht
    für den Refresh selbst gebraucht (der geht über
    roster.fetch_guild_members() direkt gegen comlink), sondern für Fälle,
    in denen der aktuelle DB-Stand ohne neuen comlink-Call ausgelesen
    werden soll.
    """
    with get_connection() as conn:
        return conn.execute("SELECT * FROM players").fetchall()


def save_roster(ally_code: str, player_name: str, units: list[dict]) -> None:
    """
    Ersetzt das gespeicherte Roster eines Spielers vollständig, über
    ally_code. `units`: Liste von Dicts wie von roster.fetch_roster()
    geliefert.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE players SET player_name = ?, last_synced = ? WHERE ally_code = ?",
            (player_name, int(time.time()), ally_code),
        )
        conn.execute("DELETE FROM roster_units WHERE ally_code = ?", (ally_code,))
        conn.executemany(
            """
            INSERT INTO roster_units
                (ally_code, unit_id, rarity, gear_tier, relic_tier, omicrons)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    ally_code,
                    u["unit_id"],
                    u["rarity"],
                    u["gear_tier"],
                    u["relic_tier"],
                    json.dumps(u["omicrons"], ensure_ascii=False),
                )
                for u in units
            ],
        )


def save_unit_names(mapping: dict[str, str]) -> None:
    """
    Ersetzt die komplette unit_names-Tabelle. `mapping`: {unit_id:
    display_name}, wie von roster.fetch_unit_names() geliefert. Komplett-
    Ersatz statt Diffing, analog zu save_roster() -- ein Refresh liefert
    ohnehin den vollständigen aktuellen Katalog.
    """
    with get_connection() as conn:
        conn.execute("DELETE FROM unit_names")
        now = int(time.time())
        conn.executemany(
            "INSERT INTO unit_names (unit_id, display_name, updated_at) VALUES (?, ?, ?)",
            [(unit_id, name, now) for unit_id, name in mapping.items()],
        )


def get_display_name_to_unit_id_map() -> dict[str, str]:
    """
    Anzeigename -> unit_id, die für /tw_zone_attack mit
    mitgliederliste=True gebrauchte Richtung: counters.attacking_leader
    (Anzeigename) muss auf roster_units.unit_id abgebildet werden, nicht
    umgekehrt. Ein leeres Dict, solange nie ein Refresh gelaufen ist --
    kein Fehler, der Aufrufer muss das ohnehin pro Angreifer einzeln
    behandeln (siehe bot.py).

    KORRIGIERT (siehe Chat-Verlauf): frühere Version baute das Dict direkt
    aus SELECT unit_id, display_name FROM unit_names -- das ignorierte,
    dass comlinks Rohkatalog für praktisch jeden Charakter ein Dutzend+
    Nicht-Spieler-Varianten mit demselben Anzeigenamen enthält
    (Stern-Stufen-Encounter-Previews wie "GENERALSKYWALKER:ONE_STAR" bis
    "SEVEN_STAR", Raid-/TB-Boss-Formen wie "PVE_GENERALSKYWALKER:*",
    GLE-Inherit-Event-Varianten etc. -- 660 von 11000+ Anzeigenamen
    betroffen, live gegen die eigene DB geprüft). Ein simples dict-
    comprehension übernahm irgendeine dieser IDs, je nach SQL-Ergebnis-
    reihenfolge effektiv zufällig -- bei "General Skywalker" traf es NICHT
    die echte, von 45 Gildenmitgliedern besessene ID (GENERALSKYWALKER),
    sondern eine Nicht-Spieler-Variante, wodurch /tw_zone_attack
    fälschlich "niemand besitzt das" meldete.

    Jetzt: pro Anzeigename wird die unit_id gewählt, die in roster_units
    tatsächlich von den meisten Spielern besessen wird. Nicht-Spieler-
    Varianten haben per Definition NIE echte Roster-Einträge (kein Spieler
    "besitzt" einen Raid-Boss oder eine Encounter-Preview), fallen also
    über den INNER JOIN automatisch heraus, nicht nur zufällig manchmal.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT n.display_name, r.unit_id, COUNT(DISTINCT r.ally_code) AS owner_count
            FROM unit_names n
            JOIN roster_units r ON r.unit_id = n.unit_id
            GROUP BY n.display_name, r.unit_id
            ORDER BY n.display_name, owner_count DESC
            """
        ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            # Erste Zeile pro display_name (dank ORDER BY owner_count DESC)
            # hat die meisten Besitzer -- setdefault() ignoriert alle
            # weiteren Zeilen für denselben Namen.
            result.setdefault(row["display_name"], row["unit_id"])
        return result


def get_owners_of_unit(unit_id: str, min_relic_tier: int = 0) -> list[sqlite3.Row]:
    """
    Gildenmitglieder, die eine bestimmte Einheit besitzen -- MIT und OHNE
    verknüpfte Discord-ID (kein Filter mehr auf discord_id IS NOT NULL,
    siehe Chat-Verlauf: frühere Version schloss unregistrierte Mitglieder
    komplett aus, statt sie namentlich ohne Ping zu zeigen). discord_id ist
    NULL für Mitglieder ohne /tw_register -- der Aufrufer (bot.py's
    format_zone_attack()) muss das selbst unterscheiden: nur bei
    vorhandener discord_id pingen, sonst player_name als Klartext anzeigen.

    min_relic_tier filtert Besitzer ohne einsatzfähiges Relic-Level heraus
    (Stakeholder-Vorgabe: level-1-Besitz ohne Relic ist für eine
    Angriffsempfehlung nicht relevant). relic_tier IS NULL (keine
    Relic-Angabe überhaupt, z.B. Einheit unterhalb Gear 13) wird IMMER
    ausgeschlossen, unabhängig vom Schwellwert.

    min_relic_tier erwartet comlinks ROHEN Wert, nicht die im Spiel
    angezeigte Relic-Stufe -- die beiden sind NICHT identisch, siehe
    config.relic_tier_to_display() für die verifizierte Umrechnung
    (roher Wert - 2 = echte Relic-Stufe, bestätigt gegen die offizielle
    relicTierDefinition-Tabelle UND einen echten Live-Datenpunkt). Der
    Aufrufer (bot.py) übergibt bereits einen über
    config.display_relic_to_raw() umgerechneten Wert.

    Sortiert nach relic_tier/gear_tier absteigend -- die am besten
    ausgerüsteten Besitzer zuerst.
    """
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT p.player_name, p.discord_id, r.gear_tier, r.relic_tier, r.rarity
            FROM roster_units r
            JOIN players p ON p.ally_code = r.ally_code
            WHERE r.unit_id = ? AND r.relic_tier IS NOT NULL AND r.relic_tier >= ?
            ORDER BY r.relic_tier DESC, r.gear_tier DESC
            """,
            (unit_id, min_relic_tier),
        ).fetchall()


def get_owned_unit_display_names() -> list[str]:
    """
    Sortierte Liste aller Anzeigenamen, die mindestens ein Gildenmitglied
    laut letztem Roster-Refresh tatsächlich besitzt -- Ersatz für
    character_list.py's swgoh.gg-Scrape als Autocomplete-Quelle für
    /tw_add, /tw_report, /tw_lookup, /tw_zone_attack und /tw_ask (siehe
    Chat-Verlauf).

    Bewusst NICHT der komplette Comlink-Einheitenkatalog (11000+ Einträge
    inkl. Raid-Bosse, NPCs, interne/nicht spielbare Einheiten) -- der INNER
    JOIN auf roster_units filtert automatisch auf tatsächlich besessene
    Einheiten, weil roster_units by construction nur enthält, was echte
    Spieler-Roster über comlink tatsächlich geliefert haben.

    Tradeoff, bewusst in Kauf genommen: eine Einheit, die niemand in der
    Gilde besitzt, taucht hier nicht auf, selbst wenn sie eine legitime
    spielbare Einheit ist -- in der Praxis meist irrelevant, da ein
    TW-Konter ohnehin nur für Anführer gemeldet werden kann, die die
    eigene Gilde tatsächlich einsetzt. Ohne registrierte Gilde oder vor dem
    ersten Roster-Refresh (siehe /tw_guild_set, /tw_roster_refresh) ist
    diese Liste leer -- Autocomplete liefert dann keine Vorschläge, kein
    Absturz.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT n.display_name
            FROM unit_names n
            JOIN roster_units r ON r.unit_id = n.unit_id
            ORDER BY n.display_name
            """
        ).fetchall()
        return [row["display_name"] for row in rows]


def delete_players_not_in(player_ids: list[str]) -> int:
    """
    Entfernt alle players-Zeilen (und per CASCADE ihre roster_units),
    deren player_id NICHT (mehr) in `player_ids` auftaucht -- echte
    Gilden-Abgänge, bestätigt durch Abwesenheit in der autoritativen
    comlink-Mitgliederliste selbst (roster.fetch_guild_members()), NICHT
    durch einen fehlgeschlagenen Einzel-Fetch für diesen Spieler (der
    könnte transient sein -- ein Netzwerk-Hänger beim Abrufen EINES
    Spielers heißt nicht, dass er die Gilde verlassen hat).

    KORRIGIERT, ECHTER BUG (siehe Chat-Verlauf): frühere Version schloss
    player_id IS NULL-Zeilen von der Löschung grundsätzlich aus, gedacht
    als Schutz für ein aktives Mitglied, dessen player_id in GENAU DIESEM
    Durchlauf noch nicht rückgeschrieben wurde. Tatsächlich bekommt JEDES
    aktuell aktive, erfolgreich abgefragte Mitglied seine player_id über
    upsert_player() bereits VOR diesem Aufruf gesetzt (siehe bot.py's
    _refresh_guild_rosters(): der Verarbeitungs-Loop läuft immer zuerst).
    Eine Zeile, die HIER TROTZDEM noch player_id IS NULL hat, kann also
    nur ein Alt-Datensatz von VOR Einführung dieser Spalte sein
    (_migrate_players_add_player_id()), dessen Spieler die Gilde bereits
    verlassen hat, BEVOR player_id je erfasst wurde -- so ein Spieler
    taucht in KEINEM zukünftigen fetch_guild_members()-Ergebnis je wieder
    auf, seine player_id bliebe unter der alten Logik für immer NULL und
    er damit für immer unlöschbar. Genau das ist in der Praxis passiert:
    alte, längst ausgetretene Mitglieder blieben trotz erfolgreich
    laufendem nächtlichen Refresh dauerhaft in der Liste.

    Verbleibendes, deutlich engeres Restrisiko: ein Spieler, der aktuell
    tatsächlich noch Mitglied ist, aber dessen Einzel-Abfrage seit
    Einführung dieser Spalte bei JEDEM einzelnen Durchlauf fehlgeschlagen
    ist (nicht nur einmal transient), hätte ebenfalls noch player_id IS
    NULL und würde jetzt fälschlich als Abgang gewertet. Deutlich enger
    als der ursprüngliche Bug (der ausnahmslos JEDEN Alt-Geist betraf) und
    zusätzlich durch die updated>0-Bedingung in
    bot.py's _refresh_guild_rosters() abgefedert, die diese Bereinigung
    nur ausführt, wenn der Durchlauf mindestens einen Spieler tatsächlich
    erfolgreich verarbeitet hat.

    SICHERHEITSSPERRE bleibt: eine leere player_ids-Liste löscht NICHTS
    und gibt 0 zurück, statt "niemand ist mehr Mitglied" zu unterstellen.
    Eine leere Liste hier bedeutet fast immer einen comlink-Fehler oder
    einen Bug in fetch_guild_members(), nicht eine tatsächlich leere
    Gilde -- ohne diese Sperre würde ein solcher Fehler die gesamte
    players-Tabelle leeren, nicht nur nichts tun.
    """
    if not player_ids:
        return 0
    with get_connection() as conn:
        placeholders = ",".join("?" * len(player_ids))
        cur = conn.execute(
            f"""
            DELETE FROM players
            WHERE player_id IS NULL OR player_id NOT IN ({placeholders})
            """,
            player_ids,
        )
        return cur.rowcount