"""
Zentrale Konfiguration für den TW-Counter-Bot.

Lädt Environment-Variablen und definiert die Konstanten, die von mehreren
Modulen referenziert werden — insbesondere die Bucket-Grenzen. Diese leben
hier als einzige Quelle und werden von db.py direkt übernommen (String-
Interpolation in den SQL-CASE), damit die SQL-Seite und der Python-Helfer
bucket_for_delta() nicht auseinanderlaufen können.
"""

import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

# Zwei getrennte Rollen, zwei getrennte Berechtigungsstufen:
#   SPECIALIST_ROLE_ID -> /tw_add    (Konter-Katalog kuratieren)
#   MEMBER_ROLE_ID     -> /tw_report (Ergebnisse melden)
# Bewusst zwei verschiedene IDs, nicht dieselbe Rolle für beides — wer
# Spezialist ist, ist nicht automatisch auch report-berechtigt und umgekehrt,
# es sei denn, der Discord-Server vergibt beide Rollen an dieselben Personen.
# Repurposed gegenüber der ursprünglichen CONTEXT.md-Benennung: MEMBER_ROLE_ID
# stand dort für "TW-Spezialisten" (verwirrend benannt) und deckte nur
# /tw_add ab. Jetzt: SPECIALIST_ROLE_ID für /tw_add, MEMBER_ROLE_ID für
# /tw_report — die Namen entsprechen jetzt tatsächlich ihrer Funktion.
SPECIALIST_ROLE_ID = int(os.getenv("SPECIALIST_ROLE_ID"))
MEMBER_ROLE_ID = int(os.getenv("MEMBER_ROLE_ID"))
MANAGER_IDS = set(int(i) for i in os.getenv("MANAGER_IDS", "").split(",") if i.strip())

# Narrower than MANAGER_IDS — a single ID, not a set. Gates /tw_characterrefresh
# specifically, which writes to the container's filesystem. Deliberately not
# folded into is_manager()/MANAGER_IDS: a filesystem-write action is a
# different category of trust than a DB delete, even for the same people.
OWNER_ID = int(os.getenv("OWNER_ID"))
DATA_DIR = os.getenv("DATA_DIR", ".")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Vienna")

DB_PATH = os.path.join(DATA_DIR, "counters.db")

# Relic-Eingabevalidierung (/tw_report). Fängt Tippfehler (z. B. "90") ab,
# schränkt die offenen Bucket-Grenzen selbst nicht ein.
MIN_RELIC = 0
MAX_RELIC = 20

# Delta = attacker_relic - defender_relic. An die Schadens-Chart des Spiels
# gekoppelt, fix — siehe CONTEXT.md, Abschnitt "Bucket-Grenzen".
#   delta <= UNDER_THRESHOLD        -> "under"  (stark unterlegen)
#   UNDER_THRESHOLD < delta < OVER_THRESHOLD -> "even" (ausgeglichen)
#   delta >= OVER_THRESHOLD         -> "over"   (stark überlegen)
UNDER_THRESHOLD = -3
OVER_THRESHOLD = 3


def bucket_for_delta(delta: int) -> str:
    """
    Python-seitiges Äquivalent zum SQL-CASE in db.py. Wird für Anzeigezwecke
    direkt nach einem /tw_report gebraucht (z. B. Bestätigungsnachricht:
    "Dieses Ergebnis fällt in Bucket X"), ohne dafür erst die Aggregations-
    Query erneut auszuführen. Muss mit dem CASE in db.py übereinstimmen —
    beide beziehen sich auf dieselben Konstanten oben.
    """
    if delta <= UNDER_THRESHOLD:
        return "under"
    elif delta >= OVER_THRESHOLD:
        return "over"
    else:
        return "even"
