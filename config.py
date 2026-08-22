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

# Für smartbot.py (/tw_ask). Wie DISCORD_TOKEN bewusst über os.getenv (kein
# int()-Cast, kein Crash beim Bot-Start, falls der Key fehlt) -- der Fehler
# soll erst beim ersten tatsächlichen /tw_ask-Aufruf auftreten, nicht schon
# beim Start des gesamten Bots wegen einer Funktion, die noch niemand benutzt
# hat.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Für roster.py (/tw_register, Roster-Refresh). Zeigt auf die selbst
# gehostete swgoh-comlink-Instanz (siehe docker-compose.yml, Service
# `comlink`) -- im Compose-Netzwerk über den Service-Namen erreichbar,
# lokal (Bot außerhalb Docker) auf http://localhost:<comlink-Port>.
# Kein os.getenv-Crash beim Fehlen, aus demselben Grund wie
# ANTHROPIC_API_KEY: der Fehler soll erst beim ersten tatsächlichen
# /tw_register-Aufruf sichtbar werden, nicht schon beim Bot-Start.
COMLINK_URL = os.getenv("COMLINK_URL", "http://localhost:3000")

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


# Comlinks roher relic.currentTier-Wert (siehe roster.py's
# _parse_roster_unit()) entspricht NICHT direkt der im Spiel angezeigten
# Relic-Stufe. VERIFIZIERT gegen die offizielle relicTierDefinition-Tabelle
# (game_data via comlink): jeder Eintrag "STR_SUP_RELIC_TIER_NN" hat ein
# "tier"-Feld mit Wert NN + 2 (z.B. TIER_01 -> tier 3, TIER_10 -> tier 12,
# durchgängig ohne Ausnahme über alle 120 Einträge geprüft). Zusätzlich
# gegen einen echten Live-Datenpunkt bestätigt: ein Spieler mit rohem Wert
# 5 zeigt im Spiel tatsächlich Relic 3 (5 - 2 = 3), siehe Chat-Verlauf.
#
# Rohwerte unter 3 haben KEINE Entsprechung in relicTierDefinition -- sie
# bedeuten "noch kein Relic" (Einheit meist unterhalb Gear 13), nicht
# "Relic 1" oder "Relic 2". Deshalb 0 statt einer negativen Zahl.
RAW_RELIC_TIER_OFFSET = 2
MIN_RAW_RELIC_TIER = 3  # entspricht der niedrigsten echten Relic-Stufe, Relic 1


def relic_tier_to_display(raw_relic_tier: int | None) -> int:
    """Wandelt roster_units.relic_tier (comlinks Rohwert) in die
    tatsächliche, im Spiel angezeigte Relic-Stufe um. 0 für "kein Relic"
    (None oder Rohwert unter MIN_RAW_RELIC_TIER)."""
    if raw_relic_tier is None or raw_relic_tier < MIN_RAW_RELIC_TIER:
        return 0
    return raw_relic_tier - RAW_RELIC_TIER_OFFSET


def display_relic_to_raw(display_relic: int) -> int:
    """Umkehrung von relic_tier_to_display() -- für Schwellwerte, die in
    "Relic X" formuliert werden (z.B. bot.py's _MIN_RELIC_TIER_FOR_ATTACK),
    aber gegen den rohen DB-Wert gefiltert werden müssen."""
    return display_relic + RAW_RELIC_TIER_OFFSET
