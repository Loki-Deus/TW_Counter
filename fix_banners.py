"""
Banner-Diagnose und optionale Nachkorrektur für reports.banners.

Hintergrund: config.correct_banner_penalty() gleicht den Mehrfachangriffs-
Abzug nur beim SCHREIBEN aus (/tw_report). get_banner_stats() mittelt die
gespeicherten Werte unverändert (AVG(r.banners)), es gibt keine Korrektur
beim Lesen. Reports, die VOR der Korrektur gespeichert wurden, tragen daher
noch den Rohwert aus der Spiel-UI und drücken den Durchschnitt.

Eindeutigkeit: ein korrigierter Sieg liegt IMMER bei 16-20 (Bänder 16-20,
11-15 +5, 6-10 +10 landen alle in 16-20). Ein Sieg mit gespeichertem Wert
6-15 ist deshalb sicher ein unkorrigierter Altwert. Die Korrektur ist
idempotent -- ein zweiter Lauf ändert nichts mehr. Niederlagen (Wert 0) und
Werte außerhalb 6-20 werden nie angefasst, nur gemeldet.

Falls die Spalte reports.banner_penalty_corrected in deiner Datenbank existiert
(stammt aus einer früheren Version, die db.py im Repo kennt sie nicht mehr),
werden Zeilen mit Flag = 1 nie angefasst und nur gemeldet, wenn ihr Wert in
6-15 liegt (das wären dann keine Altwerte, sondern z.B. ein früher
akzeptierter Tippfehler unter 6).

Aufruf im laufenden Container (Standard = nur ansehen, nichts wird geändert):
    docker exec -i tw-counter python - < fix_banners.py
Anwenden (legt vorher ein Backup neben counters.db an):
    docker exec -i tw-counter python - --apply < fix_banners.py
"""

import os
import sqlite3
import sys
import time

APPLY = "--apply" in sys.argv
DB = os.path.join(os.getenv("DATA_DIR", "."), "counters.db")

# (untere Grenze, obere Grenze, Zuschlag) -- identisch zu
# config._BANNER_PENALTY_CORRECTIONS, ohne 16-20 (Zuschlag 0).
LEGACY_BANDS = ((11, 15, 5), (6, 10, 10))

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

_cols = {row[1] for row in conn.execute("PRAGMA table_info(reports)")}
HAS_FLAG = "banner_penalty_corrected" in _cols
# Zeilen, die ein früherer Lauf bereits als korrigiert markiert hat, bleiben
# unberührt. Ohne die Spalte gibt es nichts auszuschließen.
NOT_FLAGGED = " AND COALESCE(banner_penalty_corrected, 0) != 1" if HAS_FLAG else ""
NOT_FLAGGED_R = NOT_FLAGGED.replace("banner_penalty_corrected", "r.banner_penalty_corrected")

print(f"Datenbank: {DB}")
print(f"Spalte banner_penalty_corrected vorhanden: {'ja' if HAS_FLAG else 'nein'}\n")

print("Siege MIT Banner-Angabe, nach gespeichertem Wert:")
bands = [
    ("16-20  (korrekt)", "banners BETWEEN 16 AND 20"),
    ("11-15  (Altwert, +5)", "banners BETWEEN 11 AND 15"),
    (" 6-10  (Altwert, +10)", "banners BETWEEN 6 AND 10"),
    ("sonstige (nicht angefasst)", "(banners < 6 OR banners > 20)"),
]
for label, cond in bands:
    n = conn.execute(
        f"SELECT COUNT(*) FROM reports WHERE result = 1 AND banners IS NOT NULL AND {cond}{NOT_FLAGGED}"
    ).fetchone()[0]
    print(f"  {label:<28} {n:>5}")
if HAS_FLAG:
    skipped = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE result = 1 AND banners BETWEEN 6 AND 15 "
        "AND banner_penalty_corrected = 1"
    ).fetchone()[0]
    print(f"  {'als korrigiert markiert, Wert 6-15':<28} {skipped:>5}  (werden NICHT angefasst)")
no_banner = conn.execute(
    "SELECT COUNT(*) FROM reports WHERE result = 1 AND banners IS NULL"
).fetchone()[0]
losses = conn.execute("SELECT COUNT(*) FROM reports WHERE result = 0").fetchone()[0]
odd_losses = conn.execute(
    "SELECT COUNT(*) FROM reports WHERE result = 0 AND (banners IS NULL OR banners != 0)"
).fetchone()[0]
print(f"\nSiege OHNE Banner-Angabe (fehlen im Durchschnitt): {no_banner}")
print(f"Niederlagen (zählen als 0 im Durchschnitt):        {losses}"
      f"  (davon NULL oder != 0: {odd_losses})")

CORRECTED = """
    CASE
        WHEN r.result = 1 AND r.banners BETWEEN 11 AND 15{FLAG} THEN r.banners + 5
        WHEN r.result = 1 AND r.banners BETWEEN 6 AND 10{FLAG} THEN r.banners + 10
        ELSE r.banners
    END
""".replace("{FLAG}", NOT_FLAGGED_R)
rows = conn.execute(
    f"""
    SELECT c.defending_leader AS d, c.attacking_leader AS a,
           AVG(r.banners) AS before, AVG({CORRECTED}) AS after,
           COUNT(*) AS n
    FROM counters c JOIN reports r ON r.counter_id = c.id
    WHERE r.banners IS NOT NULL
    GROUP BY c.id
    HAVING ABS(before - after) > 0.0001
    ORDER BY c.defending_leader, c.attacking_leader
    """
).fetchall()
print(f"\nBetroffene Matchups (Durchschnitt ändert sich): {len(rows)}")
for r in rows[:40]:
    print(f"  {r['d']} <- {r['a']}: {r['before']:.1f} -> {r['after']:.1f}  (n={r['n']})")
if len(rows) > 40:
    print(f"  ... und {len(rows) - 40} weitere")

if not APPLY:
    print("\nNur Ansicht -- nichts geändert. Mit --apply korrigieren (Backup wird angelegt).")
    sys.exit(0)

backup = f"{DB}.bak-{int(time.time())}"
dest = sqlite3.connect(backup)
conn.backup(dest)
dest.close()
print(f"\nBackup: {backup}")

changed = 0
with conn:
    for lower, upper, add in LEGACY_BANDS:
        cur = conn.execute(
            "UPDATE reports SET banners = banners + ? "
            f"WHERE result = 1 AND banners BETWEEN ? AND ?{NOT_FLAGGED}",
            (add, lower, upper),
        )
        changed += cur.rowcount
print(f"Korrigiert: {changed} Reports.")
