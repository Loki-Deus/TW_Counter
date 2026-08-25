"""
TW-Counter-Bot — Discord-Bot für SWGOH-Territory-War-Konter.
Verdrahtet config.py (Env/Konstanten), db.py (Event-Log-Modell), roster.py
(Ally-Code-Roster UND Charakter-Katalog über eine selbst gehostete
comlink-Instanz) und smartbot.py (natürlichsprachlicher Query-Layer über
Claude) zu den Slash-Commands /tw_add, /tw_report, /tw_lookup,
/tw_zone_add, /tw_zone_attack, /tw_ask, /tw_delete, /tw_celebrate,
/tw_help.

character_list.py (swgoh.gg-HTML-Scrape, /tw_characterrefresh) wurde
retired, siehe Chat-Verlauf -- der Charakter-Katalog für Autocomplete kommt
jetzt aus db.get_owned_unit_display_names(): Comlinks Einheitenkatalog
(roster.fetch_unit_names(), wöchentlich aktualisiert) gejoint gegen
roster_units, also gefiltert auf tatsächlich von der Gilde besessene
Einheiten statt des vollen ~11000-Einheiten-Katalogs. Kein manueller
HTML-Upload mehr nötig. beautifulsoup4 ist damit keine Abhängigkeit mehr
(siehe requirements.txt).

/tw_register und /tw_roster_refresh existieren im Code vollständig, sind
aber über ROSTER_FEATURE_ENABLED (unten, False) deaktiviert, bis eine
comlink-Instanz produktiv läuft -- Discord bekommt sie aktuell also gar
nicht erst zum Registrieren angeboten. Auf True setzen, sobald comlink
deployed ist. WICHTIG, seit der Ablösung von character_list.py: der
gesamte Charakter-Katalog (character_autocomplete(), /tw_ask) hängt jetzt
ebenfalls an comlink UND an mindestens einem abgeschlossenen Roster-
Refresh -- ohne das ist die Autocomplete-Liste leer, nicht nur die
Roster-Funktionen. Das ist eine echte neue Abhängigkeit gegenüber vorher
(character_list.py war komplett unabhängig von comlink).

Wenn aktiv, läuft der Roster-Refresh guild-weit über comlinks
Mitgliederliste (roster.fetch_guild_members()), nicht nur für einzeln per
/tw_register verknüpfte Discord-Nutzer -- /tw_guild_set hinterlegt dafür
einmalig die interne SWGOH-Gilden-ID. Kein manuelles Signup pro Spieler
nötig; /tw_register verknüpft nur noch optional Discord-ID und Ally-Code.

Bewusst keine feste Anzahl mehr genannt (frühere Version sagte "fünf" und
lief der tatsächlichen Command-Liste zweimal in Folge hinterher) -- bei der
nächsten Erweiterung reicht es, den Namen oben in die Liste einzufügen,
ohne eine Zahl mitpflegen zu müssen.

Berechtigungsmodell: zwei unabhängige Rollen ohne Administrator-Override.
SPECIALIST_ROLE_ID gate für /tw_add und /tw_zone_add (Katalogpflege),
MEMBER_ROLE_ID für /tw_report. /tw_register ist bewusst für ALLE offen,
nicht nur MEMBER_ROLE_ID -- reine Selbstauskunft (den eigenen Ally-Code
verknüpfen) ohne Schreibzugriff auf Konter-/Report-Daten, verträgt eine
niedrigere Hürde als tatsächliches Reporten. /tw_delete und /tw_zone_attack
erfordern Administrator ODER Mitgliedschaft in MANAGER_IDS --
/tw_zone_attack, weil es eine taktische Kriegsnacht-Entscheidung ist statt
Katalogpflege. /tw_roster_refresh erfordert aus demselben Grund ebenfalls
Manager-Rechte: echte Netzwerklast auf der eigenen comlink-Instanz, kein
Selbstbedienungs-Command wie /tw_register.
/tw_lookup, /tw_ask, /tw_celebrate und /tw_help sind für alle offen.

Natürlichsprachliche Anfragen laufen über zwei Trigger auf denselben
Query-Layer: den Slash-Command /tw_ask und eine @mention des Bots in einer
normalen Nachricht ("@TW-Counter was kontert Darth Vader?") -- siehe
on_message() und smartbot.py.

/tw_report deklariert den Parameter `verteidiger` vor `angreifer`, weil die
angreifer-Autocomplete interaction.namespace.verteidiger liest und damit nur
funktioniert, wenn das Feld beim Ausfüllen bereits gesetzt ist.
"""

import asyncio
import logging
import math
from collections import Counter

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Bot

import config
import db
import roster
import smartbot

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Keine Intents.members nötig: nichts im Code enumeriert role.members, alle
# Berechtigungsprüfungen laufen über interaction.user direkt. Das Privileged
# Gateway Intent "Server Members" muss im Developer Portal nicht aktiviert werden.
#
# Ebenso KEIN Intents.message_content nötig, obwohl bot.py weiter unten einen
# eigenen on_message-Handler für @mention-Anfragen definiert: Discord liefert
# message.content für Nachrichten, die den Bot mentionen, auch ohne dieses
# privilegierte Intent (dokumentierte Ausnahme, siehe Kommentar bei
# on_message). Zwei Features (/tw_ask und @mention), null privilegierte
# Intents -- bewusst so gehalten, nicht aus Versehen unvollständig.
intents = discord.Intents.default()
bot: Bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Feature-Flag: der Roster-Mirror (comlink-Anbindung, siehe roster.py) ist
# bewusst inaktiv, bis eine echte comlink-Instanz läuft (siehe Chat-
# Verlauf) -- /tw_register und /tw_roster_refresh sollen als Slash-
# Commands aktuell gar nicht erst bei Discord auftauchen, nicht nur
# fehlschlagen, wenn man sie aufruft. Auf True setzen, sobald comlink
# deployed ist -- Commands, Refresh-Task und die zugehörigen /tw_help-
# Einträge sind vollständig fertig, nur inaktiv, keine weiteren
# Codeänderungen nötig.
ROSTER_FEATURE_ENABLED = True

# Kein In-Memory-Cache für den Charakter-Katalog mehr (siehe Chat-Verlauf,
# character_list.py wurde retired) -- character_autocomplete() und
# smartbot.answer_query() lesen db.get_owned_unit_display_names() direkt,
# analog zu zone_autocomplete()'s bereits etabliertem Muster. Die Liste ist
# klein genug (nur tatsächlich besessene Einheiten, nicht der volle
# 11000+-Comlink-Katalog), dass ein Cache hier keinen echten Vorteil
# brächte, nur eine zusätzliche Invalidierungsquelle.


# ── Berechtigungs-Helfer ──────────────────────────────────────────────────


def is_tw_specialist(interaction: discord.Interaction) -> bool:
    """/tw_add: erfordert Rolle SPECIALIST_ROLE_ID. Kein Administrator-Override."""
    role_ids = {r.id for r in interaction.user.roles}
    return config.SPECIALIST_ROLE_ID in role_ids


def is_member(interaction: discord.Interaction) -> bool:
    """
    /tw_report: erfordert Rolle MEMBER_ROLE_ID. Unabhängig von
    is_tw_specialist() — die beiden Rollen implizieren sich nicht gegenseitig.
    Kein Administrator-Override.
    """
    role_ids = {r.id for r in interaction.user.roles}
    return config.MEMBER_ROLE_ID in role_ids


def is_manager(interaction: discord.Interaction) -> bool:
    """/tw_delete: Administrator-Rechte ODER Mitgliedschaft in MANAGER_IDS."""
    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.id in config.MANAGER_IDS
    )


# is_owner() entfernt (siehe Chat-Verlauf): war ausschließlich für
# /tw_characterrefresh, das mit der Ablösung von character_list.py
# ebenfalls entfernt wurde. config.OWNER_ID bleibt vorerst als Pflicht-
# Env-Var bestehen (siehe config.py) -- absichtlich nicht in derselben
# Änderung mit angefasst, um den Deploy-Blast-Radius klein zu halten.


# ── Autocomplete ──────────────────────────────────────────────────────────


def filter_autocomplete(current: str, options: list[str], limit: int = 25) -> list[str]:
    """
    Groß-/Kleinschreibungs-unabhängiger Teilstring-Match. Präfix-Treffer
    zuerst, dann alphabetisch — Discord zeigt maximal 25 Choices, daher der
    harte Cutoff.
    """
    current_lower = current.lower()
    matches = [o for o in options if current_lower in o.lower()]
    matches.sort(key=lambda o: (not o.lower().startswith(current_lower), o.lower()))
    return matches[:limit]


async def character_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """
    Katalog für /tw_add (beide Parameter), /tw_lookup/verteidiger,
    /tw_zone_attack/verteidiger_*. Direkt aus der DB gelesen
    (db.get_owned_unit_display_names()) statt aus einem In-Memory-Cache --
    siehe dortige Docstring zum Tradeoff (nur tatsächlich von der Gilde
    besessene Einheiten, kein voller Comlink-Katalog).
    """
    return [
        app_commands.Choice(name=n, value=n)
        for n in filter_autocomplete(current, db.get_owned_unit_display_names())
    ]


async def attacker_for_defender_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """
    Für /tw_report und /tw_delete, Parameter `angreifer`: zeigt nur Angreifer,
    für die gegen den bereits gewählten `verteidiger` ein Konter existiert.
    Liefert eine leere Liste, solange `verteidiger` noch nicht gesetzt ist.
    """
    verteidiger = getattr(interaction.namespace, "verteidiger", None)
    if not verteidiger:
        return []
    attackers = db.get_attackers_for_defender(verteidiger)
    return [
        app_commands.Choice(name=n, value=n)
        for n in filter_autocomplete(current, attackers)
    ]


async def zone_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Zonen-Katalog für /tw_zone_add und /tw_zone_attack. Direkt aus der DB
    gelesen statt aus einem In-Memory-Cache wie character_names — siehe
    Begründung bei db.get_zone_names()."""
    return [
        app_commands.Choice(name=n, value=n)
        for n in filter_autocomplete(current, db.get_zone_names())
    ]


# ── Lookup-Formatierung ───────────────────────────────────────────────────

BUCKET_ORDER = ("under", "even", "over")
BUCKET_LABELS = {"under": "Unterlegen", "even": "Ausgeglichen", "over": "Überlegen"}
_DISCORD_MESSAGE_LIMIT = 1900  # Sicherheitsabstand zum harten 2000-Zeichen-Limit


def _aggregate_bucket_stats(attackers: list[str], bucket_rows: list) -> dict:
    """
    {attacker: {bucket: {"wins", "losses"}}} für alle `attackers`, befüllt
    aus `bucket_rows` (db.get_bucket_stats() — INNER JOIN, lässt reportlose
    Konter aus). Attacker ohne Zeile in bucket_rows bleiben bei 0/0 statt zu
    fehlen — Grundlage sowohl für die /tw_lookup-Tabelle als auch für
    rank_attackers() (siehe dort).
    """
    stats = {a: {b: {"wins": 0, "losses": 0} for b in BUCKET_ORDER} for a in attackers}
    for row in bucket_rows:
        attacker = row["attacking_leader"]
        bucket = row["bucket"]
        if attacker in stats and bucket in stats[attacker]:
            stats[attacker][bucket]["wins"] = row["wins"] or 0
            stats[attacker][bucket]["losses"] = row["losses"] or 0
    return stats


def _wilson_lower_bound(wins: int, losses: int, z: float = 1.96) -> float:
    """
    Untere Grenze des Wilson-Score-Konfidenzintervalls -- Sortiergrundlage
    statt der rohen Gewinnquote (siehe Chat-Verlauf: eine 1/1-Quote landete
    zuvor gleichrangig mit einer 15/15-Quote, weil beide "100%" zeigten,
    und schlug sogar eine echte 9/10-Quote von 90% -- die Rohquote allein
    ignoriert Stichprobengröße komplett). z=1.96 entspricht einem
    95%-Konfidenzniveau, der übliche Standardwert für dieses Verfahren.

    Verifiziert gegen die realen Zahlen aus dem Third-Sister-Beispiel im
    Chat-Verlauf: Darth Bane (15/0) bleibt vorn, aber Hera Syndulla (15/2)
    und Bo-Katan (9/1) überholen jetzt korrekt die 1/1-Einträge, statt von
    ihnen wegen höherer Rohquote verdrängt zu werden.
    """
    n = wins + losses
    if n == 0:
        return 0.0
    phat = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    center = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return (center - margin) / denom


def _even_sort_key(attacker: str, stats: dict) -> tuple[bool, float]:
    """Wilson-Score der Ausgeglichen-Quote als Sortierschlüssel (siehe
    _wilson_lower_bound()); Konter ohne Daten in diesem Bucket sinken ans
    Ende (siehe Aufrufer für die Sortierrichtung)."""
    wins = stats[attacker]["even"]["wins"]
    losses = stats[attacker]["even"]["losses"]
    total = wins + losses
    return (total > 0, _wilson_lower_bound(wins, losses) if total > 0 else 0.0)


def _aggregate_banner_stats(banner_rows: list) -> dict:
    """{attacker: {"avg", "count"}} aus db.get_banner_stats() -- getrennt
    von _aggregate_bucket_stats(), weil Banner nicht pro Bucket vorliegt,
    sondern als eigene vierte Spalte (siehe get_banner_stats()-Docstring).
    Attacker ohne jede Banner-Angabe fehlen hier komplett statt mit 0
    aufzutauchen -- der Aufrufer muss das als "keine Angabe" behandeln,
    nicht als "0 Banner"."""
    return {
        row["attacking_leader"]: {"avg": row["avg_banners"], "count": row["banner_count"]}
        for row in banner_rows
    }


def rank_attackers(defending_leader: str) -> list[dict]:
    """
    Gemeinsame Ranking-Grundlage für /tw_lookup UND /tw_zone_attack: alle
    bekannten Angreifer gegen defending_leader, sortiert nach
    Ausgeglichen-Quote absteigend (identische Sortierlogik wie bisher in
    format_lookup_table, hier herausgezogen statt ein zweites Mal in dieser
    Datei dupliziert -- smartbot.py dupliziert dieselbe Aggregation separat
    aus Zirkelimport-Gründen, siehe dortiger Kommentar; das gilt hier nicht,
    beide Aufrufer leben in bot.py).

    Gibt [] zurück, wenn kein Konter gegen defending_leader existiert.
    Jeder Eintrag: {"attacker": str, "buckets": {...}, "banner": {"avg", "count"}}.
    banner fehlt nie als Schlüssel, ist aber {"avg": None, "count": 0}, wenn
    keine Banner-Angabe vorliegt -- Aufrufer müssen nicht extra auf Existenz
    des Schlüssels prüfen, nur auf count.
    """
    attackers = db.get_attackers_for_defender(defending_leader)
    if not attackers:
        return []

    bucket_rows = db.get_bucket_stats(defending_leader)
    stats = _aggregate_bucket_stats(attackers, bucket_rows)
    banner_stats = _aggregate_banner_stats(db.get_banner_stats(defending_leader))

    ordered = sorted(
        attackers,
        key=lambda a: (
            not _even_sort_key(a, stats)[0],
            -_even_sort_key(a, stats)[1],
            a,
        ),
    )
    return [
        {
            "attacker": a,
            "buckets": stats[a],
            "banner": banner_stats.get(a, {"avg": None, "count": 0}),
        }
        for a in ordered
    ]


def format_lookup_table(
    defending_leader: str, attackers: list[str], bucket_rows: list, banner_rows: list
) -> list[str]:
    """
    Baut die /tw_lookup-Ausgabe. Reine Funktion ohne discord.Interaction-
    Abhängigkeit, dadurch isoliert testbar.

    attackers: ALLE existierenden Konter gegen defending_leader (auch ohne
               Reports) — aus db.get_attackers_for_defender().
    bucket_rows: Aggregation NUR für Konter MIT Reports — aus
               db.get_bucket_stats(). Der INNER JOIN dort lässt reportlose
               Konter aus; hier werden sie über `attackers` ergänzt und als
               "keine Berichte" ausgegeben.
    banner_rows: aus db.get_banner_stats() -- Angreifer ohne jede
               Banner-Angabe fehlen darin, werden hier als leere vierte
               Spalte dargestellt, nicht als 0.

    Primärsortierung: ausgeglichen-Quote absteigend; Konter ohne
    ausgeglichen-Daten sinken ans Ende. Banner fließt bewusst NICHT in die
    Sortierung ein -- es ist ein optionales Zusatzfeld, keine Ranking-Basis
    (Stakeholder-Vorgabe: rein informativ als vierte Spalte).
    """
    stats = _aggregate_bucket_stats(attackers, bucket_rows)
    banner_stats = _aggregate_banner_stats(banner_rows)

    ordered = sorted(
        attackers,
        key=lambda a: (
            not _even_sort_key(a, stats)[0],
            -_even_sort_key(a, stats)[1],
            a,
        ),
    )

    def cell(attacker: str, bucket: str) -> str:
        wins = stats[attacker][bucket]["wins"]
        losses = stats[attacker][bucket]["losses"]
        total = wins + losses
        if total == 0:
            return ""
        pct = round((wins / total) * 100)
        return f"{pct}% ({wins}/{losses})"

    def banner_cell(attacker: str) -> str:
        b = banner_stats.get(attacker)
        if not b or not b["count"]:
            return ""
        return f"{b['avg']:.1f}"

    col_attacker, col_bucket, col_banner = 28, 15, 8
    header_line = (
        f"{'Angreifer':<{col_attacker}} "
        f"{BUCKET_LABELS['under']:<{col_bucket}} "
        f"{BUCKET_LABELS['even']:<{col_bucket}} "
        f"{BUCKET_LABELS['over']:<{col_bucket}} "
        f"{'Banner':<{col_banner}}"
    )
    separator_line = "-" * (col_attacker + 3 * col_bucket + col_banner + 4)
    body_lines = [
        f"{a:<{col_attacker}} "
        f"{cell(a, 'under'):<{col_bucket}} "
        f"{cell(a, 'even'):<{col_bucket}} "
        f"{cell(a, 'over'):<{col_bucket}} "
        f"{banner_cell(a):<{col_banner}}"
        for a in ordered
    ]

    title = f"**Konter gegen {defending_leader}**\n"
    messages: list[str] = []
    chunk = [header_line, separator_line]
    chunk_len = len(title) + sum(len(l) + 1 for l in chunk)

    for line in body_lines:
        if chunk_len + len(line) + 1 > _DISCORD_MESSAGE_LIMIT and len(chunk) > 2:
            messages.append(title + "```\n" + "\n".join(chunk) + "\n```")
            title = ""  # nur der erste Chunk trägt den Titel
            chunk = [header_line, separator_line]
            chunk_len = sum(len(l) + 1 for l in chunk)
        chunk.append(line)
        chunk_len += len(line) + 1

    if len(chunk) > 2:
        messages.append(title + "```\n" + "\n".join(chunk) + "\n```")

    return messages


# ── /tw_add ───────────────────────────────────────────────────────────────


@tree.command(
    name="tw_add", description="Legt einen neuen TW-Konter an (nur TW-Spezialisten)"
)
@app_commands.describe(
    verteidiger="Verteidigender Anführer", angreifer="Angreifender Anführer"
)
async def tw_add(interaction: discord.Interaction, verteidiger: str, angreifer: str):
    if not is_tw_specialist(interaction):
        await interaction.response.send_message(
            "Dieser Befehl ist auf die Rolle TW-Spezialisten beschränkt.",
            ephemeral=True,
        )
        return

    try:
        db.add_counter(
            verteidiger,
            angreifer,
            str(interaction.user.id),
            interaction.user.display_name,
        )
    except db.CounterExistsError:
        await interaction.response.send_message(
            f"Der Konter **{angreifer}** vs **{verteidiger}** existiert bereits. "
            f"Nutze `/tw_report`, um ein Ergebnis zu melden.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Konter angelegt: **{angreifer}** greift **{verteidiger}** an. "
        f"Nutze `/tw_report`, um Ergebnisse zu melden."
    )


tw_add.autocomplete("angreifer")(character_autocomplete)
tw_add.autocomplete("verteidiger")(character_autocomplete)


# ── /tw_report ────────────────────────────────────────────────────────────
# verteidiger ist als Parameter zuerst deklariert, damit die
# angreifer-Autocomplete beim Ausfüllen bereits interaction.namespace.verteidiger
# lesen kann (siehe attacker_for_defender_autocomplete).


@tree.command(
    name="tw_report",
    description="Meldet ein Kampfergebnis für einen bestehenden Konter",
)
@app_commands.describe(
    verteidiger="Verteidigender Anführer",
    verteidiger_relic="Relic-Level des Verteidiger-Anführers (0-20)",
    angreifer="Angreifender Anführer",
    angreifer_relic="Relic-Level des Angreifer-Anführers (0-20)",
    ergebnis="Sieg oder Niederlage",
    banner="Erzielte Banner (optional bei Sieg; bei Niederlage automatisch 0)",
)
@app_commands.choices(
    ergebnis=[
        app_commands.Choice(name="Sieg", value=1),
        app_commands.Choice(name="Niederlage", value=0),
    ]
)
async def tw_report(
    interaction: discord.Interaction,
    verteidiger: str,
    verteidiger_relic: app_commands.Range[int, config.MIN_RELIC, config.MAX_RELIC],
    angreifer: str,
    angreifer_relic: app_commands.Range[int, config.MIN_RELIC, config.MAX_RELIC],
    ergebnis: app_commands.Choice[int],
    banner: int | None = None,
):
    if not is_member(interaction):
        await interaction.response.send_message(
            "Dieser Befehl ist auf die Rolle der Report-berechtigten Mitglieder beschränkt.",
            ephemeral=True,
        )
        return

    if banner is not None and banner < 0:
        await interaction.response.send_message(
            "Banner darf nicht negativ sein.", ephemeral=True
        )
        return

    # Eine Niederlage bringt in SWGOH TW keine Banner -- das ist eine feste
    # Spielregel, kein Schätzwert, also hier erzwungen statt dem manuellen
    # Reporting überlassen. Discord kann das banner-Feld nicht abhängig vom
    # gewählten ergebnis aus-/einblenden, das Feld bleibt also immer
    # sichtbar -- ein hier trotzdem eingetragener Wert wird bei einer
    # Niederlage überschrieben, nicht stillschweigend verworfen: die
    # Bestätigungsnachricht unten weist explizit darauf hin, damit niemand
    # rätselt, warum der gemeldete Wert vom gespeicherten abweicht.
    banner_overridden = ergebnis.value == 0 and banner is not None and banner != 0
    if ergebnis.value == 0:
        banner = 0

    # app_commands.Range erzwingt MIN_RELIC..MAX_RELIC bereits clientseitig
    # (Discord zeigt ein Zahlenfeld mit diesen Grenzen) und serverseitig beim
    # Parsen der Interaction — keine manuelle Range-Prüfung hier nötig.
    try:
        db.add_report(
            verteidiger,
            angreifer,
            angreifer_relic,
            verteidiger_relic,
            ergebnis.value,
            str(interaction.user.id),
            banners=banner,
        )
    except db.CounterNotFoundError:
        await interaction.response.send_message(
            f"Kein Konter **{angreifer}** vs **{verteidiger}** hinterlegt. "
            f"Lege ihn zuerst mit `/tw_add` an.",
            ephemeral=True,
        )
        return

    delta = angreifer_relic - verteidiger_relic
    bucket_label = BUCKET_LABELS[config.bucket_for_delta(delta)]
    if banner_overridden:
        banner_suffix = ", Banner: 0 (Niederlage — eingegebener Wert wurde überschrieben)"
    elif banner is not None:
        banner_suffix = f", Banner: {banner}"
    else:
        banner_suffix = ""
    await interaction.response.send_message(
        f"Report gespeichert: **{angreifer}** ({angreifer_relic}) vs **{verteidiger}** ({verteidiger_relic}) "
        f"→ {ergebnis.name}, Bucket **{bucket_label}** (Δ{delta:+d}){banner_suffix}.",
        ephemeral=True,
    )


tw_report.autocomplete("verteidiger")(character_autocomplete)
tw_report.autocomplete("angreifer")(attacker_for_defender_autocomplete)


# ── /tw_lookup ────────────────────────────────────────────────────────────


@tree.command(
    name="tw_lookup", description="Zeigt alle bekannten Konter gegen einen Verteidiger"
)
@app_commands.describe(verteidiger="Verteidigender Anführer")
async def tw_lookup(interaction: discord.Interaction, verteidiger: str):
    attackers = db.get_attackers_for_defender(verteidiger)
    if not attackers:
        await interaction.response.send_message(
            f"Keine Konter gegen **{verteidiger}** hinterlegt.", ephemeral=True
        )
        return

    bucket_rows = db.get_bucket_stats(verteidiger)
    banner_rows = db.get_banner_stats(verteidiger)
    messages = format_lookup_table(verteidiger, attackers, bucket_rows, banner_rows)

    await interaction.response.send_message(messages[0])
    for extra in messages[1:]:
        await interaction.followup.send(extra)


tw_lookup.autocomplete("verteidiger")(character_autocomplete)


# ── /tw_zone_add ──────────────────────────────────────────────────────────
# Zonen sind Kartengeometrie, kein Match-Ergebnis -- selbe Berechtigungs-
# Kategorie wie /tw_add (Katalog kuratieren), nicht wie /tw_report.


@tree.command(
    name="tw_zone_add",
    description="Legt eine TW-Zone an (nur TW-Spezialisten)",
)
@app_commands.describe(
    name="Name der Zone, z.B. 'Territorium 3 – Zone B'",
    bild_url="Optionale Bild-URL zur Zone (Kartenausschnitt o.ä.)",
)
async def tw_zone_add(
    interaction: discord.Interaction, name: str, bild_url: str | None = None
):
    if not is_tw_specialist(interaction):
        await interaction.response.send_message(
            "Dieser Befehl ist auf die Rolle TW-Spezialisten beschränkt.",
            ephemeral=True,
        )
        return

    try:
        db.add_zone(name, bild_url)
    except db.ZoneExistsError:
        await interaction.response.send_message(
            f"Zone **{name}** existiert bereits.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Zone **{name}** angelegt.", ephemeral=True
    )


# ── /tw_zone_attack ───────────────────────────────────────────────────────
# Offiziers-Werkzeug für den TW-Abend: bis zu drei mögliche Verteidiger für
# eine Zone (Unsicherheit beim Scouten -- der Angreifer weiß oft nicht mit
# letzter Sicherheit, welcher Anführer tatsächlich dahintersteckt), liefert
# pro Verteidiger die Top-Angreifer aus dem bestehenden Konter-Katalog
# (rank_attackers(), s.o.) sowie eine Hedge-Zeile für Angreifer, die bei
# mehr als einem der genannten Verteidiger unter den Top-Treffern liegen --
# die lohnen sich zuerst zuzuteilen, wenn die Scouting-Lage unsicher ist.
#
# Gate: is_manager(), nicht is_tw_specialist() -- das hier ist eine
# taktische Kriegsnacht-Entscheidung (Offiziers-Ebene), keine
# Katalogpflege wie /tw_add/tw_zone_add.
#
# `mitgliederliste` ist bewusst vom Grundaufruf getrennt: die Empfehlung
# selbst kommt komplett aus counters/reports, ohne zusätzlichen Datenpfad.
# Welche Mitglieder die empfohlenen Angreifer-Anführer tatsächlich besitzen,
# bräuchte eine Roster-Quelle (Ally-Code -> Einheitenbesitz), die es aktuell
# nicht gibt -- der Parameter ist hier bereits als Schnittstelle angelegt,
# liefert aber einen expliziten Hinweis statt erfundener Namen, bis diese
# Datenquelle existiert.

_ZONE_TOP_N = 3
_ZONE_MAX_MEMBERS = 5
# Mindest-Relic-Level für eine Angriffsempfehlung (Stakeholder-Vorgabe:
# Level-1-Besitz ohne Relic zählt nicht als einsatzfähig), formuliert als
# ECHTE, im Spiel angezeigte Relic-Stufe -- die Umrechnung auf comlinks
# rohen DB-Wert übernimmt config.display_relic_to_raw(), VERIFIZIERT gegen
# die offizielle relicTierDefinition-Tabelle UND einen echten Live-
# Datenpunkt (siehe dortiger Kommentar). Um den Schwellwert zu ändern,
# NUR diese Zahl anpassen, nicht den Rohwert von Hand ausrechnen.
_MIN_DISPLAY_RELIC_FOR_ATTACK = 7
_MIN_RELIC_TIER_FOR_ATTACK = config.display_relic_to_raw(_MIN_DISPLAY_RELIC_FOR_ATTACK)


def format_zone_attack(
    zone_name: str,
    zone_image_url: str | None,
    verteidiger_liste: list[str],
    mitgliederliste: bool,
) -> tuple[list[str], list[tuple[str, str]]]:
    """
    Reine Funktion ohne discord.Interaction-Abhängigkeit, wie format_lookup_table.

    mitgliederliste=True hängt an jede empfohlene Angreifer-Zeile an, wer
    in der Gilde die Einheit besitzt UND einen verknüpften Discord-Account
    hat (db.get_owners_of_unit() filtert das bereits so). Der Lookup
    Anzeigename -> unit_id kann fehlschlagen (siehe roster.fetch_unit_names()
    -- unverifiziert, ob comlinks Lokalisierung exakt character_list.py's
    swgoh.gg-Namen trifft); ein Fehltreffer wird als sichtbarer Hinweis
    ausgegeben, nicht stillschweigend als "niemand besitzt das" verwechselt.

    Gibt (messages, callable_entries) zurück.

    messages ist eine Liste, nicht ein einzelner String: bis zu drei
    Verteidiger mit je bis zu drei Angreifern plus vollständiger
    Mitgliederliste kommen in der Praxis nah an Discords harte
    2000-Zeichen-Grenze pro Nachricht heran (ein realistischer Testfall lag
    bei ~1843 Zeichen -- ein einziger etwas längerer Anführername, ein
    "keine Zuordnung"-Hinweis statt einer kurzen Mitgliederliste, oder ein
    Zonen-Bild reichen, um drüber zu rutschen). Wird deshalb wie /tw_lookup
    und /tw_help in mehrere Nachrichten aufgeteilt (_DISCORD_MESSAGE_LIMIT),
    pro Verteidiger als kleinste unteilbare Einheit -- ein einzelner
    Verteidiger-Block bleibt bei aktuellen _ZONE_TOP_N/_ZONE_MAX_MEMBERS-
    Grenzen realistisch weit unter dem Limit, wird also nicht weiter
    aufgesplittet.

    callable_entries: Liste von (attacker_display_name, unit_id,
    defender_names, owner_count) für JEDEN Angreifer mit mindestens einem
    qualifizierenden Besitzer -- Grundlage für das Dropdown in
    tw_zone_attack(), über das sich für jeden empfohlenen Angreifer (nicht
    nur die mit gekürzter Inline-Liste) die vollständige Besitzerliste
    abrufen UND ein "Zum Angriff aufrufen"-Button auslösen lässt. Auch
    ein nischiger Konter mit nur 1-2 Besitzern landet hier -- gerade bei
    knappen Fällen ist ein gezielter Aufruf am nützlichsten. Per unit_id
    dedupliziert, falls derselbe Angreifer unter mehreren der genannten
    Verteidiger auftaucht -- defender_names sammelt dann alle betroffenen
    Verteidiger, owner_count bleibt der zuerst ermittelte Wert.
    """
    header_lines = [f"## Zonen-Angriff: {zone_name}"]
    if zone_image_url:
        header_lines.append(zone_image_url)  # Discord rendert eine alleinstehende Bild-URL als Vorschau
    header_block = "\n".join(header_lines)

    name_to_unit_id = db.get_display_name_to_unit_id_map() if mitgliederliste else {}
    # Erfasst JEDEN Angreifer mit mindestens einem qualifizierenden
    # Besitzer, nicht nur die, deren Inline-Liste gekürzt wurde -- ein
    # "nischiger" Konter mit z.B. nur 2 Besitzern soll genauso aufrufbar
    # sein wie einer mit 40 (siehe Chat-Verlauf: gerade die knappen Fälle
    # sind es, bei denen ein gezielter Aufruf am meisten bringt).
    callable_by_unit_id: dict[str, tuple[str, list[str], int]] = {}
    per_defender_top: dict[str, list[str]] = {}
    defender_blocks: list[str] = []

    for verteidiger in verteidiger_liste:
        ranked = rank_attackers(verteidiger)
        block_lines = [f"**Gegen {verteidiger}:**"]

        if not ranked:
            block_lines.append("Keine Konter hinterlegt.")
            per_defender_top[verteidiger] = []
            defender_blocks.append("\n".join(block_lines))
            continue

        top = ranked[:_ZONE_TOP_N]
        per_defender_top[verteidiger] = [r["attacker"] for r in top]

        for r in top:
            even = r["buckets"]["even"]
            total = even["wins"] + even["losses"]
            if total == 0:
                line = f"- {r['attacker']} — keine Berichte im ausgeglichenen Bucket"
            else:
                pct = round((even["wins"] / total) * 100)
                line = f"- {r['attacker']} — {pct}% ({even['wins']}/{even['losses']}) ausgeglichen"

            if mitgliederliste:
                unit_id = name_to_unit_id.get(r["attacker"])
                if unit_id is None:
                    line += (
                        "\n  -# Keine Einheiten-Zuordnung gefunden "
                        "(Namensraum-Brücke unvollständig für diesen Anführer)."
                    )
                else:
                    owners = db.get_owners_of_unit(unit_id, min_relic_tier=_MIN_RELIC_TIER_FOR_ATTACK)
                    if not owners:
                        line += (
                            "\n  -# Niemand in der Gilde besitzt diese Einheit auf "
                            "einsatzfähigem Relic-Level (laut letztem Roster-Refresh)."
                        )
                    else:
                        # Nur tatsächlich per /tw_register verknüpfte
                        # Mitglieder werden gepingt (<@discord_id>) -- das
                        # ist ein bewusstes Opt-in, niemand wird ungefragt
                        # in Discord markiert. Unregistrierte Besitzer
                        # erscheinen trotzdem, nur als Klartext-Ingame-Name
                        # statt als Mention.
                        labels = [
                            f"<@{o['discord_id']}>" if o["discord_id"] else o["player_name"]
                            for o in owners[:_ZONE_MAX_MEMBERS]
                        ]
                        extra = len(owners) - len(labels)

                        # Aufrufbar, sobald es überhaupt einen Besitzer
                        # gibt -- nicht erst ab Überlauf.
                        existing = callable_by_unit_id.get(unit_id)
                        if existing is None:
                            callable_by_unit_id[unit_id] = (r["attacker"], [verteidiger], len(owners))
                        else:
                            _, existing_defenders, _ = existing
                            if verteidiger not in existing_defenders:
                                existing_defenders.append(verteidiger)

                        suffix = f" (+{extra} weitere)" if extra > 0 else ""
                        line += f"\n  -# Mögliche Angreifer: {', '.join(labels)}{suffix}"

            block_lines.append(line)
        defender_blocks.append("\n".join(block_lines))

    hedge_block = None
    if len(verteidiger_liste) > 1:
        counts = Counter(
            attacker for tops in per_defender_top.values() for attacker in tops
        )
        hedges = sorted(a for a, c in counts.items() if c > 1)
        if hedges:
            hedge_block = (
                f"**Hedge (deckt mehrere der genannten Verteidiger ab):** {', '.join(hedges)}"
            )

    all_blocks = [header_block] + defender_blocks + ([hedge_block] if hedge_block else [])

    messages: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in all_blocks:
        block_len = len(block) + 2  # "\n\n"-Trenner beim Zusammenfügen
        if current and current_len + block_len > _DISCORD_MESSAGE_LIMIT:
            messages.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(block)
        current_len += block_len
    if current:
        messages.append("\n\n".join(current))

    callable_entries = [
        (name, unit_id, defenders, owner_count)
        for unit_id, (name, defenders, owner_count) in callable_by_unit_id.items()
    ]
    return messages, callable_entries


class ZoneAttackAttackerSelect(discord.ui.Select):
    """
    Dropdown über JEDEN empfohlenen Angreifer einer /tw_zone_attack-
    Antwort (mitgliederliste=True), nicht nur die mit gekürzter Inline-
    Liste -- Auswahl zeigt die vollständige Besitzerliste privat (ephemeral)
    plus einen "Zum Angriff aufrufen"-Button. min_relic_tier MUSS mit dem
    Wert übereinstimmen, der die Hauptnachricht erzeugt hat
    (_MIN_RELIC_TIER_FOR_ATTACK zur Aufrufzeit) -- sonst zeigt die
    vollständige Liste andere Leute als die (falls vorhandene) gekürzte
    Inline-Liste, was mehr verwirrt als hilft.

    Antwort ephemeral: nur der Klickende sieht die vollständige Liste,
    keine Kanal-Nachricht mit potenziell 30+ Namen. Trägt zusätzlich den
    "Zum Angriff aufrufen"-Button (ZoneAttackCallButton), über den genau
    diese Person die Liste dann bewusst öffentlich machen kann -- die
    Sichtung (ephemeral) und die Entscheidung, tatsächlich zu pingen
    (öffentlich), sind zwei getrennte Schritte, kein Automatismus.
    """

    def __init__(
        self,
        zone_name: str,
        callable_entries: list[tuple[str, str, list[str], int]],
        min_relic_tier: int,
    ):
        self._zone_name = zone_name
        self._min_relic_tier = min_relic_tier
        # Discord erlaubt maximal 25 Optionen pro Select -- bei mehr als 25
        # aufrufbaren Angreifern (unrealistisch bei _ZONE_TOP_N=3 und bis
        # zu 3 Verteidigern -> max. 9, aber zur Sicherheit) werden die
        # restlichen schlicht nicht anwählbar, statt einen Fehler zu werfen.
        limited = callable_entries[:25]
        self._by_value = {str(i): entry for i, entry in enumerate(limited)}
        options = [
            discord.SelectOption(
                label=name[:100],
                value=value,
                description=f"{owner_count} Besitzer",
            )
            for value, (name, _, _, owner_count) in self._by_value.items()
        ]
        super().__init__(placeholder="Angreifer für Aufruf auswählen...", options=options)

    async def callback(self, interaction: discord.Interaction):
        name, unit_id, defender_names, _ = self._by_value[self.values[0]]
        owners = db.get_owners_of_unit(unit_id, min_relic_tier=self._min_relic_tier)
        labels = [
            f"<@{o['discord_id']}>" if o["discord_id"] else o["player_name"] for o in owners
        ]
        header = f"**Alle möglichen Angreifer für {name}** ({len(owners)}):\n"
        body = ", ".join(labels)

        call_view = ZoneAttackCallView(
            self._zone_name, name, unit_id, defender_names, self._min_relic_tier
        )

        if len(header) + len(body) <= 2000:
            await interaction.response.send_message(
                header + body, view=call_view, ephemeral=True
            )
            return

        # Zu lang für eine einzelne Nachricht -- in Chunks aufteilen, statt
        # abzuschneiden oder zu crashen. Der Aufruf-Button hängt am Kopf-
        # Chunk, nicht an jedem einzelnen Folge-Chunk.
        await interaction.response.send_message(header, view=call_view, ephemeral=True)
        chunk = ""
        for label in labels:
            addition = (", " if chunk else "") + label
            if len(chunk) + len(addition) > 1900:
                await interaction.followup.send(chunk, ephemeral=True)
                chunk = label
            else:
                chunk += addition
        if chunk:
            await interaction.followup.send(chunk, ephemeral=True)


class ZoneAttackCallButton(discord.ui.Button):
    """
    Macht aus einer privat gesichteten Besitzerliste einen tatsächlichen,
    für alle sichtbaren Angriffsaufruf mit echten Pings. Holt die
    Besitzerliste beim Klick NEU (statt die zuvor im Dropdown gezeigte
    Liste zu cachen) -- zwischen Sichtung und Klick können Sekunden bis
    Minuten liegen, in denen sich am Roster nichts Wesentliches ändert,
    aber es kostet nichts, hier konsistent zu sein statt zu vertrauen, dass
    sich nichts geändert hat.
    """

    def __init__(
        self,
        zone_name: str,
        attacker_name: str,
        unit_id: str,
        defender_names: list[str],
        min_relic_tier: int,
    ):
        super().__init__(label="Zum Angriff aufrufen", style=discord.ButtonStyle.danger)
        self._zone_name = zone_name
        self._attacker_name = attacker_name
        self._unit_id = unit_id
        self._defender_names = defender_names
        self._min_relic_tier = min_relic_tier

    async def callback(self, interaction: discord.Interaction):
        owners = db.get_owners_of_unit(self._unit_id, min_relic_tier=self._min_relic_tier)
        mentions = [
            f"<@{o['discord_id']}>" if o["discord_id"] else o["player_name"] for o in owners
        ]
        defender_text = " / ".join(self._defender_names) if self._defender_names else "?"
        header = (
            f"📣 **Angriffsaufruf — {self._zone_name}**\n"
            f"Gegen **{defender_text}**: bringt **{self._attacker_name}**!\n"
        )
        body = " ".join(mentions)

        # Button in der ephemeralen Nachricht deaktivieren und beschriften
        # -- macht sichtbar, dass der Aufruf bereits raus ist, ohne ein
        # zweites Klicken technisch zu verhindern (das würde nur denselben
        # Aufruf wiederholen, kein Datenrisiko).
        self.disabled = True
        self.label = "Aufruf gesendet ✅"
        await interaction.response.edit_message(view=self.view)

        full = header + body
        if len(full) <= 2000:
            await interaction.followup.send(full, ephemeral=False)
            return

        await interaction.followup.send(header, ephemeral=False)
        chunk = ""
        for mention in mentions:
            addition = (" " if chunk else "") + mention
            if len(chunk) + len(addition) > 1900:
                await interaction.followup.send(chunk, ephemeral=False)
                chunk = mention
            else:
                chunk += addition
        if chunk:
            await interaction.followup.send(chunk, ephemeral=False)


class ZoneAttackCallView(discord.ui.View):
    """View mit genau einem "Zum Angriff aufrufen"-Button, gebunden an
    einen konkreten Angreifer/Verteidiger-Kontext."""

    def __init__(
        self,
        zone_name: str,
        attacker_name: str,
        unit_id: str,
        defender_names: list[str],
        min_relic_tier: int,
    ):
        super().__init__(timeout=300)
        self.add_item(
            ZoneAttackCallButton(zone_name, attacker_name, unit_id, defender_names, min_relic_tier)
        )


class ZoneAttackView(discord.ui.View):
    """Enthält das Angreifer-Dropdown NUR, wenn es überhaupt einen
    aufrufbaren Angreifer gibt (mindestens ein qualifizierender Besitzer
    irgendwo) -- kein leeres, nutzloses Menü, wenn niemand etwas besitzt."""

    def __init__(
        self,
        zone_name: str,
        callable_entries: list[tuple[str, str, list[str], int]],
        min_relic_tier: int,
    ):
        super().__init__(timeout=300)
        if callable_entries:
            self.add_item(ZoneAttackAttackerSelect(zone_name, callable_entries, min_relic_tier))


@tree.command(
    name="tw_zone_attack",
    description="Empfiehlt Angreifer für eine Zone anhand bis zu drei möglicher Verteidiger",
)
@app_commands.describe(
    zone="TW-Zone",
    verteidiger_1="Erster möglicher Verteidiger-Anführer",
    verteidiger_2="Zweiter möglicher Verteidiger-Anführer (optional)",
    verteidiger_3="Dritter möglicher Verteidiger-Anführer (optional)",
    mitgliederliste="Zusätzlich Mitglieder auflisten, die die empfohlenen Konter besetzen können",
)
async def tw_zone_attack(
    interaction: discord.Interaction,
    zone: str,
    verteidiger_1: str,
    verteidiger_2: str | None = None,
    verteidiger_3: str | None = None,
    mitgliederliste: bool = False,
):
    if not is_manager(interaction):
        await interaction.response.send_message(
            "Dieser Befehl erfordert Administrator-Rechte oder Manager-Status.",
            ephemeral=True,
        )
        return

    zone_row = db.get_zone(zone)
    if zone_row is None:
        await interaction.response.send_message(
            f"Zone **{zone}** ist nicht hinterlegt. Nutze `/tw_zone_add`, um sie anzulegen.",
            ephemeral=True,
        )
        return

    verteidiger_liste = [v for v in (verteidiger_1, verteidiger_2, verteidiger_3) if v]

    messages, callable_entries = format_zone_attack(
        zone_row["name"], zone_row["image_url"], verteidiger_liste, mitgliederliste
    )
    view = (
        ZoneAttackView(zone_row["name"], callable_entries, _MIN_RELIC_TIER_FOR_ATTACK)
        if callable_entries
        else None
    )

    # Das Dropdown (falls vorhanden) hängt NUR an der letzten Nachricht --
    # Discord erlaubt Components pro Nachricht, nicht "für die ganze
    # Antwort", und die letzte Nachricht ist die sinnvollste Stelle dafür.
    await interaction.response.send_message(messages[0], view=view if len(messages) == 1 else discord.utils.MISSING)
    for i, extra in enumerate(messages[1:], start=2):
        is_last = i == len(messages)
        await interaction.followup.send(extra, view=view if is_last else discord.utils.MISSING)


tw_zone_attack.autocomplete("zone")(zone_autocomplete)
tw_zone_attack.autocomplete("verteidiger_1")(character_autocomplete)
tw_zone_attack.autocomplete("verteidiger_2")(character_autocomplete)
tw_zone_attack.autocomplete("verteidiger_3")(character_autocomplete)


if ROSTER_FEATURE_ENABLED:
    # /tw_guild_set, /tw_register, /tw_roster_refresh, der Refresh-Task und
    # dessen before_loop-Hook -- vollständig fertig, aber inaktiv, bis
    # ROSTER_FEATURE_ENABLED oben auf True gesetzt wird (siehe dortiger
    # Kommentar). Absichtlich per if-Block statt einzeln auskommentierter
    # Zeilen deaktiviert: @tree.command-Decorators laufen zur Import-
    # zeit, ein deaktivierter Block heißt hier also "Discord bekommt
    # diese Commands nie zum Registrieren angeboten", nicht nur "schlägt
    # beim Aufruf fehl".
    # ── /tw_guild_set, /tw_register & Roster-Refresh ─────────────────────
    # Guild-weiter Ansatz statt Einzel-Selbstregistrierung: /tw_guild_set
    # hinterlegt EINMALIG die interne SWGOH-Gilden-ID (über einen
    # beliebigen bekannten Ally-Code aufgelöst, siehe roster.resolve_guild_id()
    # -- comlink hat keine Freitext-Gildensuche). Ab dann läuft der Refresh
    # automatisch über die GESAMTE Gilde (roster.fetch_guild_members()),
    # nicht nur über die, die zufällig /tw_register genutzt haben -- kein
    # manuelles Signup pro Spieler nötig (siehe Chat-Verlauf).
    #
    # /tw_register bleibt bestehen, aber mit anderer Rolle als vorher: es
    # verknüpft nur noch die Discord-ID eines Spielers mit seinem (ohnehin
    # schon bekannten) Ally-Code -- für eine spätere mitgliederliste=True-
    # Auswertung in /tw_zone_attack, die konkrete Discord-Nutzer nennen
    # will, nicht nur Ally-Codes. Es ist NICHT mehr die einzige Quelle für
    # Rosterdaten, nur noch für die Discord-Verknüpfung.
    #
    # Drei Wege, ein Roster zu aktualisieren:
    #   /tw_register       -- pro Spieler, verknüpft Discord-ID + lädt sein
    #                          Roster sofort zur Bestätigung (unabhängig
    #                          vom nächtlichen Task).
    #   refresh_rosters_task -- automatisch, nächtlich, für die GESAMTE
    #                          hinterlegte Gilde.
    #   /tw_roster_refresh  -- manuell, für die GESAMTE Gilde, für den Fall
    #                          "TW startet gleich, nicht auf den
    #                          nächtlichen Task warten wollen". Manager-
    #                          Rechte, da es echte Netzwerklast auf der
    #                          eigenen comlink-Instanz erzeugt (ein Call
    #                          pro Gildenmitglied), kein Selbstbedienungs-
    #                          Command wie /tw_register.

    @tree.command(
        name="tw_guild_set",
        description="Hinterlegt die SWGOH-Gilden-ID über einen bekannten Ally-Code (Manager/Admin)",
    )
    @app_commands.describe(
        ally_code="Ally-Code eines beliebigen Gildenmitglieds (z.B. dein eigener)"
    )
    async def tw_guild_set(interaction: discord.Interaction, ally_code: str):
        if not is_manager(interaction):
            await interaction.response.send_message(
                "Dieser Befehl erfordert Administrator-Rechte oder Manager-Status.",
                ephemeral=True,
            )
            return

        try:
            normalized = roster.normalize_ally_code(ally_code)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            guild_id, guild_name = await roster.resolve_guild_id(normalized)
        except roster.AllyCodeNotFoundError as e:
            await interaction.followup.send(str(e))
            return
        except roster.RosterFetchError:
            logger.exception(
                "Guild-ID-Auflösung über Ally-Code %s fehlgeschlagen.", normalized
            )
            await interaction.followup.send(
                "Comlink war gerade nicht erreichbar. Bitte später erneut versuchen."
            )
            return

        db.set_swgoh_guild(guild_id, guild_name)
        await interaction.followup.send(
            f"SWGOH-Gilde hinterlegt: **{guild_name or guild_id}**. Der nächste "
            f"Roster-Refresh lädt jetzt die gesamte Gilde, kein einzelnes "
            f"`/tw_register` pro Spieler mehr nötig."
        )

    @tree.command(
        name="tw_register",
        description="Verknüpft deinen Discord-Account mit deinem (bereits bekannten) Ally-Code",
    )
    @app_commands.describe(ally_code="Dein Ally-Code, z.B. 123456789 oder 123-456-789")
    async def tw_register(interaction: discord.Interaction, ally_code: str):
        # Bewusst offen für alle, keine Rollenprüfung (anders als
        # /tw_report) -- Verknüpfen des eigenen Ally-Codes ist reine
        # Selbstauskunft ohne Schreibzugriff auf Konter-/Report-Daten,
        # verträgt also eine niedrigere Hürde als tatsächliches Reporten.
        try:
            normalized = roster.normalize_ally_code(ally_code)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            resolved_ally_code, player_name, resolved_player_id, units = await roster.fetch_roster(
                ally_code=normalized
            )
        except roster.AllyCodeNotFoundError as e:
            await interaction.followup.send(str(e))
            return
        except roster.RosterFetchError:
            logger.exception("Roster-Fetch für Ally-Code %s fehlgeschlagen.", normalized)
            await interaction.followup.send(
                "Comlink war gerade nicht erreichbar. Bitte später erneut versuchen."
            )
            return

        db.upsert_player(resolved_ally_code, player_name, resolved_player_id)
        await asyncio.to_thread(db.save_roster, resolved_ally_code, player_name, units)
        db.link_discord_id(resolved_ally_code, str(interaction.user.id))

        await interaction.followup.send(
            f"Verknüpft mit **{player_name}** ({resolved_ally_code}) — {len(units)} Einheiten geladen. "
            f"Dein Roster wird ab jetzt auch beim nächtlichen Gilden-Refresh automatisch aktualisiert."
        )

    @tree.command(
        name="tw_roster_refresh",
        description="Aktualisiert die Rosterdaten der gesamten Gilde sofort (Manager/Admin)",
    )
    async def tw_roster_refresh(interaction: discord.Interaction):
        if not is_manager(interaction):
            await interaction.response.send_message(
                "Dieser Befehl erfordert Administrator-Rechte oder Manager-Status.",
                ephemeral=True,
            )
            return

        if db.get_swgoh_guild() is None:
            await interaction.response.send_message(
                "Keine SWGOH-Gilden-ID hinterlegt. Erst `/tw_guild_set` nutzen.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        updated, failed, removed = await _refresh_guild_rosters()

        await interaction.followup.send(
            f"Roster-Refresh abgeschlossen: {updated} aktualisiert, {failed} fehlgeschlagen, "
            f"{removed} wegen Gilden-Austritt entfernt."
        )

    async def _refresh_guild_rosters() -> tuple[int, int, int]:
        """
        Zieht die komplette Mitgliederliste der hinterlegten SWGOH-Gilde
        über comlink (roster.fetch_guild_members() liefert playerId-Werte,
        siehe dortiger Docstring) und aktualisiert JEDEN gefundenen
        Spieler -- nicht mehr beschränkt auf die, die zufällig
        /tw_register genutzt haben. Der Ally-Code jedes Mitglieds kommt
        erst aus der Antwort von roster.fetch_roster(player_id=...), nicht
        aus der Gilden-Mitgliederliste selbst (die liefert nur playerId).

        Reconciliation am Ende (siehe Chat-Verlauf): Spieler, deren
        player_id NICHT mehr in der aktuellen Mitgliederliste auftaucht,
        haben die Gilde verlassen -- ihre players-/roster_units-Zeilen
        werden entfernt (db.delete_players_not_in()), statt für immer als
        Karteileichen liegen zu bleiben und /tw_zone_attack irgendwann
        jemanden empfehlen zu lassen, der gar nicht mehr da ist. Läuft
        NACH der Update-Schleife, mit der VOLLSTÄNDIGEN player_ids-Liste
        (nicht nur den erfolgreich aktualisierten) -- ein einzelner
        fehlgeschlagener Fetch soll niemanden fälschlich als "ausgetreten"
        behandeln, siehe db.delete_players_not_in()-Docstring.

        Sequentiell statt parallel (asyncio.gather): das ist die selbst
        gehostete comlink-Instanz, absichtlich kein Ansturm aus N
        gleichzeitigen Requests gegen das eigentliche Spiel-Backend
        dahinter (Capital Games limitiert öffentlich ohnehin auf wenige
        Dutzend Requests/Sekunde pro IP). Ein einzelner fehlgeschlagener
        Spieler bricht den Rest des Durchlaufs nicht ab.

        Gibt (0, 0, 0) zurück, wenn keine Gilden-ID hinterlegt ist ODER die
        Mitgliederliste nicht geladen werden konnte -- kein Fehler in
        diesem Fall, der Aufrufer entscheidet, ob/wie das gemeldet wird.
        """
        guild_row = db.get_swgoh_guild()
        if guild_row is None:
            return 0, 0, 0

        try:
            player_ids = await roster.fetch_guild_members(guild_row["guild_id"])
        except (roster.AllyCodeNotFoundError, roster.RosterFetchError) as e:
            logger.warning("Gilden-Mitgliederliste konnte nicht geladen werden: %s", e)
            return 0, 0, 0

        updated = 0
        failed = 0
        for player_id in player_ids:
            try:
                ally_code, player_name, resolved_player_id, units = await roster.fetch_roster(
                    player_id=player_id
                )
                db.upsert_player(ally_code, player_name, resolved_player_id)
                await asyncio.to_thread(db.save_roster, ally_code, player_name, units)
                updated += 1
            except (roster.AllyCodeNotFoundError, roster.RosterFetchError) as e:
                logger.warning(
                    "Roster-Refresh für playerId %s fehlgeschlagen: %s", player_id, e
                )
                failed += 1

        removed = db.delete_players_not_in(player_ids)
        return updated, failed, removed

    @tasks.loop(hours=24)
    async def refresh_rosters_task():
        updated, failed, removed = await _refresh_guild_rosters()
        if updated == 0 and failed == 0 and removed == 0:
            return  # keine Gilden-ID hinterlegt -- kein Log-Rauschen jede Nacht
        logger.info(
            "Nächtlicher Gilden-Roster-Refresh abgeschlossen: %d aktualisiert, "
            "%d fehlgeschlagen, %d Spieler wegen Gilden-Austritt entfernt.",
            updated,
            failed,
            removed,
        )

    @refresh_rosters_task.before_loop
    async def before_refresh_rosters_task():
        await bot.wait_until_ready()

    # ── unit_names-Refresh (Anzeigename-Brücke) ───────────────────────────
    # Katalogdaten wie character_list.py's Charakterliste -- ändert sich mit
    # Patches, nicht pro Spieler -- deshalb wöchentliche statt nächtliche
    # Kadenz, analog zu refresh_characters_task. Trotzdem hier im
    # ROSTER_FEATURE_ENABLED-Block, weil die Datenquelle (comlink) dieselbe
    # Abhängigkeit hat wie der Rest dieses Blocks.

    @tasks.loop(hours=168)
    async def refresh_unit_names_task():
        try:
            names = await roster.fetch_unit_names()
        except roster.RosterFetchError as e:
            logger.warning("unit_names-Refresh fehlgeschlagen: %s", e)
            return
        # asyncio.to_thread(): mehrere tausend INSERTs synchron im Event-
        # Loop würden denselben Blockier-Effekt haben wie das Parsen in
        # roster.fetch_unit_names() (siehe dortiger Kommentar) -- diese
        # Zeile war Teil desselben "Unknown interaction"-Vorfalls.
        await asyncio.to_thread(db.save_unit_names, names)
        logger.info("unit_names-Refresh abgeschlossen: %d Einheiten.", len(names))

    @refresh_unit_names_task.before_loop
    async def before_refresh_unit_names_task():
        await bot.wait_until_ready()


# ── /tw_ask & @mention ──────────────────────────────────────────────────
# Zwei Trigger für denselben Query-Layer (smartbot.py): der Slash-Command
# /tw_ask und eine @mention der Bot-Identität in einer normalen Nachricht
# ("@TW-Counter was kontert Darth Vader?"). Beide rufen dieselbe
# smartbot.answer_query() auf -- keine doppelte Logik, nur zwei Einstiege.
#
# Der @mention-Weg braucht KEINEN privilegierten Message Content Intent:
# Discord liefert message.content für Nachrichten, die den Bot mentionen,
# auch ohne dieses Intent -- eine dokumentierte Ausnahme, kein Zufall
# (https://docs.discord.com/developers/gateway/you-might-not-need-a-privileged-intent,
# Abschnitt "messages in which it is mentioned"). intents bleibt deshalb
# unverändert bei Intents.default() -- siehe Kommentar bei dessen Definition
# oben, der jetzt für zwei Features statt einem gilt.


@tree.command(
    name="tw_ask",
    description="Stellt eine Frage in natürlicher Sprache, z.B. 'was kontert Darth Vader?'",
)
@app_commands.describe(frage="Deine Frage, auf Deutsch oder Englisch")
async def tw_ask(interaction: discord.Interaction, frage: str):
    await interaction.response.defer()
    try:
        answer = await smartbot.answer_query(frage, db.get_owned_unit_display_names())
    except Exception:
        logger.exception("smartbot.answer_query fehlgeschlagen für Frage: %s", frage)
        await interaction.followup.send(
            "Da ist etwas schiefgelaufen. Bitte versuch es später erneut."
        )
        return
    await interaction.followup.send(answer)


@bot.event
async def on_message(message: discord.Message):
    """
    Eigener on_message-Handler ersetzt commands.Bot's Default vollständig --
    deshalb der explizite bot.process_commands(message)-Aufruf am Ende, sonst
    würden eventuelle "!"-Prefix-Commands (command_prefix="!") stillschweigend
    nie mehr verarbeitet. Aktuell nutzt der Bot ausschließlich Slash-Commands
    über tree, aber das hier ist der dokumentierte discord.py-Standard, um
    das nicht unbeabsichtigt zu brechen, falls sich das mal ändert.

    channel.send() statt reply(): reply() erzeugt eine Message-Reference
    (die "antwortet auf ↩"-UI) und braucht dafür die Berechtigung "Read
    Message History" -- die die ursprüngliche OAuth2-Einladung nie vergeben
    hat (siehe README, Abschnitt "Discord-Anwendung": nur "Send Messages").
    Eine normale Nachricht statt eines Threads spart diese zusätzliche
    Berechtigung komplett ein, statt sie nachträglich anzufordern.
    """
    if message.author.bot:
        # Deckt auch den Bot selbst ab (bot.user.bot ist True) -- verhindert,
        # dass eine eigene Antwort sich selbst erneut mentioned und eine
        # Endlosschleife auslöst.
        return

    if bot.user in message.mentions:
        frage = message.content
        for pattern in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
            frage = frage.replace(pattern, "")
        frage = frage.strip()

        if not frage:
            await message.channel.send(
                "Ja? Frag mich etwas, z.B. 'was kontert Darth Vader?'"
            )
        else:
            async with message.channel.typing():
                try:
                    answer = await smartbot.answer_query(frage, db.get_owned_unit_display_names())
                except Exception:
                    logger.exception(
                        "smartbot.answer_query fehlgeschlagen für Mention-Frage: %s",
                        frage,
                    )
                    await message.channel.send(
                        "Da ist etwas schiefgelaufen. Bitte versuch es später erneut."
                    )
                    return
            await message.channel.send(answer)

    await bot.process_commands(message)


# ── /tw_delete ────────────────────────────────────────────────────────────


@tree.command(
    name="tw_delete",
    description="Löscht einen Konter samt aller Reports (Manager/Admin)",
)
@app_commands.describe(
    verteidiger="Verteidigender Anführer", angreifer="Angreifender Anführer"
)
async def tw_delete(interaction: discord.Interaction, verteidiger: str, angreifer: str):
    if not is_manager(interaction):
        await interaction.response.send_message(
            "Dieser Befehl erfordert Administrator-Rechte oder Manager-Status.",
            ephemeral=True,
        )
        return

    if db.get_counter(verteidiger, angreifer) is None:
        await interaction.response.send_message(
            f"Kein Konter **{angreifer}** vs **{verteidiger}** gefunden.",
            ephemeral=True,
        )
        return

    class ConfirmDeleteView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.confirmed = False

        @discord.ui.button(label="Löschen bestätigen", style=discord.ButtonStyle.red)
        async def confirm(
            self, confirm_interaction: discord.Interaction, button: discord.ui.Button
        ):
            # Löschen passiert HIER, vor dem edit_message — nicht erst nach
            # view.wait() im Aufrufer. Sonst gäbe es ein Fenster, in dem die
            # Nachricht bereits "gelöscht" meldet, der DB-Schreibvorgang aber
            # noch aussteht.
            db.delete_counter(verteidiger, angreifer)
            self.confirmed = True
            await confirm_interaction.response.edit_message(
                content=f"Konter **{angreifer}** vs **{verteidiger}** gelöscht.",
                view=None,
            )
            self.stop()

        @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.grey)
        async def cancel(
            self, cancel_interaction: discord.Interaction, button: discord.ui.Button
        ):
            self.confirmed = False
            await cancel_interaction.response.edit_message(
                content="Löschvorgang abgebrochen.", view=None
            )
            self.stop()

        async def on_timeout(self):
            self.confirmed = False
            try:
                await self.message.edit(
                    content="Zeit abgelaufen - nichts wurde gelöscht.", view=None
                )
            except Exception:
                pass

    view = ConfirmDeleteView()
    await interaction.response.send_message(
        f"Konter **{angreifer}** vs **{verteidiger}** wirklich löschen? "
        f"Alle zugehörigen Reports gehen dabei ebenfalls verloren.",
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()
    await view.wait()


tw_delete.autocomplete("verteidiger")(character_autocomplete)
tw_delete.autocomplete("angreifer")(attacker_for_defender_autocomplete)


# ── /tw_celebrate ─────────────────────────────────────────────────────────


async def resolve_display_name(guild: discord.Guild, user_id: str) -> str:
    """
    Löst eine reported_by_id zu einem aktuellen Displaynamen auf. Cache
    zuerst (guild.get_member), sonst API-Fetch. Schlägt beides fehl — z.B.
    Mitglied hat den Server verlassen — wird die rohe ID als Fallback
    angezeigt statt die ganze Auflösung abzubrechen.
    """
    try:
        member = guild.get_member(int(user_id))
        if member is None:
            member = await guild.fetch_member(int(user_id))
        return member.display_name
    except (discord.NotFound, discord.HTTPException, ValueError):
        return f"Unbekannter Nutzer ({user_id})"


@tree.command(
    name="tw_celebrate",
    description="Zeigt die Top 3 Melder der meisten TW-Reports",
)
async def tw_celebrate(interaction: discord.Interaction):
    top = db.get_top_reporters(limit=3)
    if not top:
        await interaction.response.send_message(
            "Noch keine Reports vorhanden.", ephemeral=True
        )
        return

    # defer, weil fetch_member() bei Cache-Miss einen API-Roundtrip macht —
    # das kann Discords 3-Sekunden-Fenster für die initiale Response reißen.
    await interaction.response.defer()

    medals = ("🥇", "🥈", "🥉")
    lines = []
    for medal, row in zip(medals, top):
        name = await resolve_display_name(interaction.guild, row["reported_by_id"])
        lines.append(f"{medal} **{name}** — {row['report_count']} Reports")

    await interaction.followup.send("## TW-Report-Champions\n\n" + "\n".join(lines))


# ── /tw_help ──────────────────────────────────────────────────────────────


@tree.command(
    name="tw_help", description="Zeigt alle verfügbaren Bot-Befehle und ihre Verwendung"
)
async def tw_help(interaction: discord.Interaction):
    # Als Liste einzelner Abschnitte statt eines einzelnen langen Strings,
    # damit sie sich wie in format_lookup_table (siehe dort) unter
    # _DISCORD_MESSAGE_LIMIT chunken lässt. Vorherige Version war ein
    # einziger String, der bei der letzten Erweiterung stillschweigend über
    # Discords harte 2000-Zeichen-Grenze pro Nachricht gewachsen ist --
    # send_message() wurde von Discord mit 400 abgelehnt, die Interaction
    # blieb unbeantwortet ("The application did not respond"). Diese
    # Chunking-Struktur soll dieselbe Klasse Fehler bei der nächsten
    # Command-Erweiterung von vornherein ausschließen, statt erneut auf die
    # Zeichengrenze zu stoßen.
    sections = [
        "## TW-Counter Bot — Befehlsübersicht\n",
        "**`/tw_add verteidiger angreifer`** — *Spezialisten-Rolle*\n"
        "Legt einen leeren Konter an (kein Ergebnis, keine Relic-Werte).",
        "**`/tw_report verteidiger verteidiger_relic angreifer angreifer_relic ergebnis banner`** — *Mitglieder-Rolle*\n"
        "Meldet ein Kampfergebnis für einen bereits existierenden Konter. `banner` ist optional bei "
        "Sieg; bei Niederlage wird er automatisch auf 0 gesetzt, ein trotzdem eingetragener Wert wird überschrieben.",
        "**`/tw_lookup verteidiger`** — *alle*\n"
        "Zeigt alle Konter gegen einen Verteidiger, sortiert nach Ausgeglichen-Quote, "
        "inklusive Durchschnitts-Banner als vierte Spalte (nur für Angreifer mit mindestens "
        "einer Banner-Angabe).",
        "**`/tw_zone_add name bild_url`** — *Spezialisten-Rolle*\n"
        "Legt eine TW-Zone an (Kartenreferenz für `/tw_zone_attack`).",
        "**`/tw_zone_attack zone verteidiger_1 verteidiger_2 verteidiger_3 mitgliederliste`** — *Manager/Admin*\n"
        "Empfiehlt Angreifer für eine Zone anhand von bis zu drei möglichen Verteidigern, "
        "inklusive Hedge-Hinweis bei mehrdeutiger Scouting-Lage. `mitgliederliste` ist als "
        "Schnittstelle angelegt, liefert aber noch keine echten Mitgliedernamen (fehlende Namensraum-Brücke).",
        "**`/tw_ask frage`** — *alle*\n"
        "Beantwortet eine Frage in natürlicher Sprache (Deutsch oder Englisch), "
        "z.B. 'was kontert Darth Vader?'. Alternativ: den Bot in einer normalen "
        "Nachricht @mentionen und die Frage direkt dranschreiben.",
        "**`/tw_delete verteidiger angreifer`** — *Manager/Admin*\n"
        "Löscht einen Konter samt aller Reports, nach Bestätigung.",
        "**`/tw_celebrate`** — *alle*\n"
        "Zeigt die Top 3 Melder der meisten TW-Reports.",
        "**`/tw_help`** — Zeigt diese Übersicht.",
        "-# Relic-Delta = Angreifer-Relic − Verteidiger-Relic. "
        "≤ -3 unterlegen, -2..+2 ausgeglichen, ≥ +3 überlegen.",
    ]

    # /tw_guild_set, /tw_register und /tw_roster_refresh nur auflisten,
    # wenn sie auch tatsächlich bei Discord registriert sind (siehe
    # ROSTER_FEATURE_ENABLED oben) -- sonst würde /tw_help Befehle
    # bewerben, die gar nicht existieren.
    if ROSTER_FEATURE_ENABLED:
        sections.insert(
            6,
            "**`/tw_guild_set ally_code`** — *Manager/Admin*\n"
            "Hinterlegt die SWGOH-Gilden-ID einmalig über einen bekannten Ally-Code. "
            "Danach lädt der Roster-Refresh automatisch die gesamte Gilde.",
        )
        sections.insert(
            7,
            "**`/tw_register ally_code`** — *alle*\n"
            "Verknüpft deinen Discord-Account mit deinem (bereits bekannten) Ally-Code -- "
            "kein manuelles Signup nötig, damit dein Roster erfasst wird, nur um ihn dir "
            "als Discord-Nutzer zuzuordnen.",
        )
        sections.insert(
            8,
            "**`/tw_roster_refresh`** — *Manager/Admin*\n"
            "Aktualisiert die Rosterdaten der gesamten Gilde sofort, statt auf den "
            "nächtlichen automatischen Refresh zu warten.",
        )

    messages: list[str] = []
    chunk: list[str] = []
    chunk_len = 0
    for section in sections:
        if chunk and chunk_len + len(section) + 2 > _DISCORD_MESSAGE_LIMIT:
            messages.append("\n\n".join(chunk))
            chunk = []
            chunk_len = 0
        chunk.append(section)
        chunk_len += len(section) + 2
    if chunk:
        messages.append("\n\n".join(chunk))

    await interaction.response.send_message(messages[0], ephemeral=True)
    for extra in messages[1:]:
        await interaction.followup.send(extra, ephemeral=True)


# ── Startup ───────────────────────────────────────────────────────────────


@bot.event
async def on_ready():
    db.init_db()

    if ROSTER_FEATURE_ENABLED and not refresh_rosters_task.is_running():
        refresh_rosters_task.start()

    if ROSTER_FEATURE_ENABLED and not refresh_unit_names_task.is_running():
        refresh_unit_names_task.start()

    guild = discord.Object(id=config.GUILD_ID)
    tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)

    logger.info("Eingeloggt als %s (ID: %s)", bot.user, bot.user.id)
    logger.info(
        "guild_id=%s specialist_role_id=%s member_role_id=%s manager_ids=%s owner_id=%s",
        config.GUILD_ID,
        config.SPECIALIST_ROLE_ID,
        config.MEMBER_ROLE_ID,
        config.MANAGER_IDS or "(keine)",
        config.OWNER_ID,
    )


if __name__ == "__main__":
    # Guard macht das Modul importierbar (z.B. für Tests) ohne einen echten
    # Verbindungsversuch zu Discord auszulösen. Im Container identisch, da
    # CMD ["python", "-u", "bot.py"] __name__ == "__main__" ohnehin setzt.
    bot.run(config.DISCORD_TOKEN)