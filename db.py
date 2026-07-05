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

import sqlite3
import time
from contextlib import contextmanager

from config import DB_PATH, OVER_THRESHOLD, UNDER_THRESHOLD


class CounterExistsError(Exception):
    """/tw_add: Matchup (defending_leader, attacking_leader) existiert bereits."""


class CounterNotFoundError(Exception):
    """/tw_report oder /tw_delete: referenziertes Matchup existiert nicht."""


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
    reported_by_id TEXT NOT NULL,
    reported_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_counter ON reports(counter_id);
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
) -> int:
    """
    Fügt eine Report-Zeile ein. result: 1 = Sieg, 0 = Niederlage.
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
            "(counter_id, attacker_relic, defender_relic, result, reported_by_id, reported_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                counter["id"],
                attacker_relic,
                defender_relic,
                result,
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
