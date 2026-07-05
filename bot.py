"""
TW-Counter-Bot — Discord-Bot für SWGOH-Territory-War-Konter.
Verdrahtet config.py (Env/Konstanten), db.py (Event-Log-Modell) und
character_list.py (Autocomplete-Datenquelle) zu fünf Slash-Commands:
/tw_add, /tw_report, /tw_lookup, /tw_delete, /tw_help.

Berechtigungsmodell: zwei unabhängige Rollen ohne Administrator-Override.
SPECIALIST_ROLE_ID gate für /tw_add, MEMBER_ROLE_ID für /tw_report.
/tw_delete erfordert Administrator ODER Mitgliedschaft in MANAGER_IDS.
/tw_lookup und /tw_help sind für alle offen.

/tw_report deklariert den Parameter `verteidiger` vor `angreifer`, weil die
angreifer-Autocomplete interaction.namespace.verteidiger liest und damit nur
funktioniert, wenn das Feld beim Ausfüllen bereits gesetzt ist.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Bot

import config
import db
import character_list

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Keine Intents.members nötig: nichts im Code enumeriert role.members, alle
# Berechtigungsprüfungen laufen über interaction.user direkt. Das Privileged
# Gateway Intent "Server Members" muss im Developer Portal nicht aktiviert werden.
intents = discord.Intents.default()
bot: Bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# In-Memory-Cache der Charakternamen fürs Autocomplete. Wird beim Start aus
# character_list.get_characters() befüllt und wöchentlich über
# refresh_characters_task erneuert. Absichtlich nur die Namen (nicht die
# Slugs) — die DB speichert defending_leader/attacking_leader als TEXT ohne
# Fremdschlüssel auf eine Charaktertabelle, der Slug wird hier nicht gebraucht.
character_names: list[str] = []


def _set_character_names(characters: dict[str, str]) -> None:
    global character_names
    character_names = sorted(characters.values())


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


def is_owner(interaction: discord.Interaction) -> bool:
    """/tw_characterrefresh: nur die exakte OWNER_ID. Kein Administrator-Override,
    keine Überschneidung mit MANAGER_IDS — bewusst der engste Kreis im Bot."""
    return interaction.user.id == config.OWNER_ID


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
    """Voller Charakter-Katalog — für /tw_add (beide Parameter) und /tw_lookup/verteidiger."""
    return [
        app_commands.Choice(name=n, value=n)
        for n in filter_autocomplete(current, character_names)
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


# ── Lookup-Formatierung ───────────────────────────────────────────────────

BUCKET_ORDER = ("under", "even", "over")
BUCKET_LABELS = {"under": "Unterlegen", "even": "Ausgeglichen", "over": "Überlegen"}
_DISCORD_MESSAGE_LIMIT = 1900  # Sicherheitsabstand zum harten 2000-Zeichen-Limit


def format_lookup_table(
    defending_leader: str, attackers: list[str], bucket_rows: list
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

    Primärsortierung: ausgeglichen-Quote absteigend; Konter ohne
    ausgeglichen-Daten sinken ans Ende.
    """
    stats = {a: {b: {"wins": 0, "losses": 0} for b in BUCKET_ORDER} for a in attackers}
    for row in bucket_rows:
        attacker = row["attacking_leader"]
        bucket = row["bucket"]
        if attacker in stats and bucket in stats[attacker]:
            stats[attacker][bucket]["wins"] = row["wins"] or 0
            stats[attacker][bucket]["losses"] = row["losses"] or 0

    def even_sort_key(attacker: str) -> tuple[bool, float]:
        wins = stats[attacker]["even"]["wins"]
        losses = stats[attacker]["even"]["losses"]
        total = wins + losses
        return (total > 0, (wins / total) if total > 0 else 0.0)

    ordered = sorted(
        attackers,
        key=lambda a: (not even_sort_key(a)[0], -even_sort_key(a)[1], a),
    )

    def cell(attacker: str, bucket: str) -> str:
        wins = stats[attacker][bucket]["wins"]
        losses = stats[attacker][bucket]["losses"]
        total = wins + losses
        if total == 0:
            return "keine Berichte"
        pct = round((wins / total) * 100)
        return f"{pct}% ({wins}/{losses})"

    col_attacker, col_bucket = 28, 16
    header_line = (
        f"{'Angreifer':<{col_attacker}} "
        f"{BUCKET_LABELS['under']:<{col_bucket}} "
        f"{BUCKET_LABELS['even']:<{col_bucket}} "
        f"{BUCKET_LABELS['over']:<{col_bucket}}"
    )
    separator_line = "-" * (col_attacker + 3 * col_bucket + 3)
    body_lines = [
        f"{a:<{col_attacker}} "
        f"{cell(a, 'under'):<{col_bucket}} "
        f"{cell(a, 'even'):<{col_bucket}} "
        f"{cell(a, 'over'):<{col_bucket}}"
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
):
    if not is_member(interaction):
        await interaction.response.send_message(
            "Dieser Befehl ist auf die Rolle der Report-berechtigten Mitglieder beschränkt.",
            ephemeral=True,
        )
        return

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
    await interaction.response.send_message(
        f"Report gespeichert: **{angreifer}** ({angreifer_relic}) vs **{verteidiger}** ({verteidiger_relic}) "
        f"→ {ergebnis.name}, Bucket **{bucket_label}** (Δ{delta:+d}).",
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
    messages = format_lookup_table(verteidiger, attackers, bucket_rows)

    await interaction.response.send_message(messages[0])
    for extra in messages[1:]:
        await interaction.followup.send(extra)


tw_lookup.autocomplete("verteidiger")(character_autocomplete)


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


# ── /tw_characterrefresh ─────────────────────────────────────────────────
# swgoh.gg/characters/ läuft hinter einer aktiven Cloudflare-JS-Challenge,
# die kein automatisierter Client lösen kann. Die Charakterliste kommt daher
# aus einer manuell im Browser gespeicherten Kopie, die hier hochgeladen
# wird. Reihenfolge ist bewusst: erst parsen und validieren, DANN erst die
# bestehende Datei überschreiben — eine fehlgeschlagene Validierung darf
# niemals eine funktionierende swgoh_characters.html zerstören.

_MAX_CHARACTER_UPLOAD_BYTES = 5 * 1024 * 1024  # reale Seite liegt bei ~550 KB


@tree.command(
    name="tw_characterrefresh",
    description="Aktualisiert die Charakterliste aus einer hochgeladenen Kopie von swgoh.gg/characters/ (nur Owner)",
)
@app_commands.describe(
    datei="Im Browser gespeicherte HTML-Kopie von https://swgoh.gg/characters/"
)
async def tw_characterrefresh(
    interaction: discord.Interaction, datei: discord.Attachment
):
    if not is_owner(interaction):
        await interaction.response.send_message(
            "Dieser Befehl ist auf den Bot-Owner beschränkt.", ephemeral=True
        )
        return

    if datei.size > _MAX_CHARACTER_UPLOAD_BYTES:
        await interaction.response.send_message(
            f"Datei zu groß ({datei.size / 1024:.0f} KB, Limit "
            f"{_MAX_CHARACTER_UPLOAD_BYTES // 1024} KB). Nichts wurde verändert.",
            ephemeral=True,
        )
        return

    raw = await datei.read()
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        await interaction.response.send_message(
            f"Datei ist kein gültiges UTF-8-Text/HTML ({e}). Nichts wurde verändert.",
            ephemeral=True,
        )
        return

    # Validierung VOR jedem Dateizugriff — die bestehende, funktionierende
    # Datei bleibt bei einer fehlgeschlagenen Validierung unangetastet.
    try:
        character_list.parse_characters_html(html)
    except character_list.CharacterParseError as e:
        await interaction.response.send_message(
            f"Datei konnte nicht geparst werden, bestehende Charakterliste bleibt "
            f"unverändert: {e}",
            ephemeral=True,
        )
        return

    character_list.save_local_html(html)

    try:
        characters = character_list.get_characters(force_refresh=True)
    except character_list.CharacterDataUnavailableError as e:
        # Sollte nach einer erfolgreichen Validierung+Speicherung praktisch
        # nie auftreten — Absicherung gegen unerwartete I/O-Fehler zwischen
        # save_local_html() und dem erneuten Einlesen.
        await interaction.response.send_message(
            f"Datei gespeichert, aber erneutes Einlesen ist fehlgeschlagen: {e}",
            ephemeral=True,
        )
        return

    _set_character_names(characters)
    await interaction.response.send_message(
        f"Charakterliste aktualisiert: {len(characters)} Charaktere geladen, "
        f"sofort für Autocomplete aktiv.",
        ephemeral=True,
    )


# ── /tw_help ──────────────────────────────────────────────────────────────


@tree.command(
    name="tw_help", description="Zeigt alle verfügbaren Bot-Befehle und ihre Verwendung"
)
async def tw_help(interaction: discord.Interaction):
    help_text = (
        "## TW-Counter Bot — Befehlsübersicht\n\n"
        "**`/tw_add verteidiger angreifer`** — *Spezialisten-Rolle*\n"
        "Legt einen leeren Konter an (kein Ergebnis, keine Relic-Werte).\n\n"
        "**`/tw_report verteidiger verteidiger_relic angreifer angreifer_relic ergebnis`** — *Mitglieder-Rolle*\n"
        "Meldet ein Kampfergebnis für einen bereits existierenden Konter.\n\n"
        "**`/tw_lookup verteidiger`** — *alle*\n"
        "Zeigt alle Konter gegen einen Verteidiger, sortiert nach Ausgeglichen-Quote.\n\n"
        "**`/tw_delete verteidiger angreifer`** — *Manager/Admin*\n"
        "Löscht einen Konter samt aller Reports, nach Bestätigung.\n\n"
        "**`/tw_characterrefresh datei`** — *Owner*\n"
        "Lädt eine manuell gespeicherte Kopie von swgoh.gg/characters/ hoch und "
        "aktualisiert die Autocomplete-Daten sofort.\n\n"
        "**`/tw_help`** — Zeigt diese Übersicht.\n\n"
        "-# Relic-Delta = Angreifer-Relic − Verteidiger-Relic. "
        "≤ -3 unterlegen, -2..+2 ausgeglichen, ≥ +3 überlegen."
    )
    await interaction.response.send_message(help_text, ephemeral=True)


# ── Wöchentlicher Charakter-Refresh ───────────────────────────────────────


@tasks.loop(hours=168)
async def refresh_characters_task():
    try:
        characters = character_list.get_characters(force_refresh=True)
        _set_character_names(characters)
        logger.info(
            "Wöchentlicher Charakter-Refresh erfolgreich: %d Charaktere.",
            len(character_names),
        )
    except character_list.CharacterDataUnavailableError as e:
        logger.error(
            "Wöchentlicher Charakter-Refresh fehlgeschlagen UND kein Cache vorhanden: %s "
            "— Autocomplete bleibt leer bis zum nächsten Versuch.",
            e,
        )


@refresh_characters_task.before_loop
async def before_refresh_characters_task():
    await bot.wait_until_ready()


# ── Startup ───────────────────────────────────────────────────────────────


@bot.event
async def on_ready():
    db.init_db()

    try:
        characters = character_list.get_characters(force_refresh=False)
        _set_character_names(characters)
        logger.info("Charakterliste geladen: %d Charaktere.", len(character_names))
    except character_list.CharacterDataUnavailableError as e:
        logger.warning(
            "Keine Charakterdaten verfügbar (%s) — Autocomplete liefert bis zum "
            "nächsten erfolgreichen Refresh keine Vorschläge.",
            e,
        )

    if not refresh_characters_task.is_running():
        refresh_characters_task.start()

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
