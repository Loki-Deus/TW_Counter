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

-- Roster-Mirror (siehe roster.py): players ist die Ally-Code-Registrierung
-- pro Discord-Nutzer, roster_units der zuletzt synchronisierte Kaderstand.
-- Wie counters/reports/zones aktuell OHNE guild_id -- konsistent mit dem
-- restlichen Schema, das ebenfalls (noch) von genau einer Gilde ausgeht.
-- Bei der später geplanten Mandantenfähigkeit müsste guild_id hier genauso
-- ergänzt werden wie bei counters, nicht isoliert vorgezogen.
CREATE TABLE IF NOT EXISTS players (
    discord_id    TEXT PRIMARY KEY,
    ally_code     TEXT NOT NULL UNIQUE,
    player_name   TEXT,
    registered_at INTEGER NOT NULL,
    last_synced   INTEGER
);

-- roster_units wird bei jedem Refresh komplett ersetzt (DELETE + INSERT,
-- siehe save_roster()), nicht inkrementell gepflegt -- ein Refresh liefert
-- ohnehin den vollständigen aktuellen Kaderstand, Diffing brächte hier
-- keinen Vorteil.
CREATE TABLE IF NOT EXISTS roster_units (
    discord_id  TEXT NOT NULL REFERENCES players(discord_id) ON DELETE CASCADE,
    unit_id     TEXT NOT NULL,
    rarity      INTEGER,
    gear_tier   INTEGER,
    relic_tier  INTEGER,
    omicrons    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (discord_id, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_roster_units_discord ON roster_units(discord_id);
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
        conn.executescript(SCHEMA)
        _migrate_reports_add_banners(conn)


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
    Durchschnittliche Banner-Anzahl pro Angreifer gegen defending_leader.
    ANDERS als get_bucket_stats() NICHT nach Bucket gruppiert: Banner ist
    ein optionales, vom Relic-Delta unabhängiges Feld -- es steht als
    eigene, vierte Spalte neben den drei Buckets, nicht als deren
    Aufteilung (Stakeholder-Vorgabe, siehe Chat).

    WHERE r.banners IS NOT NULL filtert NUR Reports ohne jede Banner-
    Angabe heraus -- das betrifft ausschließlich Siege ohne manuell
    eingetragenen Wert (banner ist dort optional). Niederlagen tragen seit
    bot.py's tw_report IMMER eine konkrete 0 (kein NULL mehr, siehe dortiger
    Kommentar zu banner_overridden) und fließen damit korrekt in den
    Schnitt ein, statt fälschlich ausgeschlossen zu werden -- ein
    Verteidiger mit vielen Niederlagen soll einen entsprechend niedrigeren
    Banner-Schnitt zeigen, nicht künstlich nur aus den Siegen berechnet
    werden.
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
            WHERE c.defending_leader = ? AND r.banners IS NOT NULL
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


def register_player(discord_id: str, ally_code: str) -> None:
    """
    Legt einen Spieler an oder aktualisiert den Ally-Code, falls sich
    dieser geändert hat (z.B. Accountwechsel) -- discord_id ist der
    stabile Schlüssel, nicht der Ally-Code. Setzt bewusst KEIN
    last_synced -- das passiert erst in save_roster(), nach dem ersten
    tatsächlich erfolgreichen Comlink-Fetch, nicht schon bei der reinen
    Registrierung.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO players (discord_id, ally_code, registered_at)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET ally_code = excluded.ally_code
            """,
            (discord_id, ally_code, int(time.time())),
        )


def get_all_registered_players() -> list[sqlite3.Row]:
    """Für refresh_rosters_task und /tw_roster_refresh -- alle registrierten (discord_id, ally_code)."""
    with get_connection() as conn:
        return conn.execute("SELECT discord_id, ally_code FROM players").fetchall()


def save_roster(discord_id: str, player_name: str, units: list[dict]) -> None:
    """
    Ersetzt das gespeicherte Roster eines Spielers vollständig. `units`:
    Liste von Dicts mit unit_id/rarity/gear_tier/relic_tier/omicrons, wie
    von roster.fetch_roster() geliefert -- diese Funktion selbst weiß
    nichts von Comlink, nur vom bereits normalisierten Format.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE players SET player_name = ?, last_synced = ? WHERE discord_id = ?",
            (player_name, int(time.time()), discord_id),
        )
        conn.execute("DELETE FROM roster_units WHERE discord_id = ?", (discord_id,))
        conn.executemany(
            """
            INSERT INTO roster_units
                (discord_id, unit_id, rarity, gear_tier, relic_tier, omicrons)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    discord_id,
                    u["unit_id"],
                    u["rarity"],
                    u["gear_tier"],
                    u["relic_tier"],
                    json.dumps(u["omicrons"], ensure_ascii=False),
                )
                for u in units
            ],
        )