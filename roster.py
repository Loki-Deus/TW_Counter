"""
Roster-Mirror für den TW-Counter-Bot -- Ally-Code-basierte Spielerdaten aus
einer selbst gehosteten swgoh-comlink-Instanz (siehe docker-compose.yml,
Service `comlink`, Image ghcr.io/swgoh-utils/swgoh-comlink:latest).

Comlink liest ausschließlich öffentliche, unauthentifizierte Spieler-APIs --
nichts, was ein anderer Spieler nicht ohnehin durch Anklicken eines Profils
im Spiel sehen könnte (siehe swgoh-utils/swgoh-comlink, Abschnitt "What
Comlink does/doesn't do"). Ally-Codes sind vom Spieler selbst geteilte,
öffentliche IDs -- unproblematisch, sie hier zu verarbeiten.

Rechtlicher Hinweis, nicht technisch: Comlink ist nicht mit EA/Capital
Games affiliiert und spricht APIs an, die nie als öffentliches Developer-
API veröffentlicht wurden. Das ist seit Jahren De-facto-Standard im
gesamten SWGOH-Tool-Ökosystem (swgoh.gg eingeschlossen), aber "weithin
toleriert" ist nicht dasselbe wie "vertraglich abgesichert" -- eigene
Einschätzung nötig, bevor darauf ein bezahltes Produkt aufgebaut wird,
siehe Chat-Verlauf.

+++ VERIFIZIERT gegen einen echten Response (siehe Chat-Verlauf) +++
_parse_roster_unit()'s Feldannahmen (definitionId, currentRarity,
currentTier, relic.currentTier) stimmen exakt mit einem echten
get_player()-Response überein -- geprüft anhand eines realen Beispiels
(MAGMATROOPER, Ally-Code des Bot-Betreibers). Nur die Omicron-Erkennung
bleibt ein Platzhalter (immer []): die geprüfte Einheit hatte keine
Fähigkeit über Tier 6 hinaus, es fehlt also weiterhin ein Beispiel mit
tatsächlich gesetztem Omicron, um den dafür entscheidenden Tier-Wert zu
bestimmen.

+++ VERIFIZIERT, mit einer Korrektur gegenüber dem ursprünglichen Entwurf +++
fetch_guild_members() ist jetzt gegen einen echten get_guild()-Response
geprüft. Zwei Dinge bestätigt: (1) der comlink-Python-Client entfernt den
äußeren "guild"-Schlüssel selbst (siehe SwgohComlinkAsync.get_guild()-
Quelltext) -- ein rohes HTTP POST an /guild liefert zwar
{"guild": {"member": [...]}}, der Wrapper aber bereits das entpackte
{"member": [...]}; (2) jedes Mitglied trägt "playerId" -- ABER das ist
NICHT der Ally-Code, sondern eine andere, base64-artige interne ID (z.B.
"Xk7RXvj_SSSgst2qxR8lNQ"). Der comlink-Python-Client akzeptiert diese
playerId direkt als player_id=-Argument von get_player() (bestätigte
Signatur), und die volle Antwort enthält dann sowohl den echten Ally-Code
(als Klartextfeld "allyCode") als auch das komplette Roster in einem
einzigen Call -- kein zusätzlicher Auflösungsschritt nötig. fetch_roster()
unten unterstützt deshalb beide Zugriffswege (ally_code= für /tw_register,
player_id= für guild-weit entdeckte Mitglieder) und liest den Ally-Code
immer aus der Antwort selbst, nie vom Aufrufer übernommen.

Noch nicht geprüft: nichts mehr an dieser Stelle -- die ursprünglich offene
Frage nach dem Gildennamen hat sich als hinfällig herausgestellt:
guildName liegt direkt als Klartextfeld auf der player-Antwort,
resolve_guild_id() braucht dafür gar keinen separaten get_guild()-Call
mehr (siehe dortiger Docstring).

Speichert NUR normalisierte Werte (rarity, gear_tier, relic_tier,
omicrons) in db.py, nicht den rohen Comlink-Response.

WICHTIGE OFFENE LÜCKE, JETZT GESCHLOSSEN: unit_id hier ist Comlinks defId
(z.B. "ZEBS3") -- früher ein anderer Namensraum als der Anzeigename aus
character_list.py. fetch_unit_names() unten baut die Brücke über
Comlinks eigene Game-Data + Lokalisierung, verifiziert gegen einen echten
Response (ZEBS3 -> 'Garazeb "Zeb" Orrelios', siehe Chat-Verlauf). Bleibt
offen: ob dieser Name Zeichen für Zeichen mit character_list.py's
swgoh.gg-Scrape übereinstimmt -- siehe fetch_unit_names()-Docstring.
Das bedeutet: die in db.py gespeicherten Rosterdaten lassen sich noch
NICHT gegen counters.attacking_leader / defending_leader (Anzeigenamen)
abgleichen -- also auch /tw_zone_attack mit mitgliederliste=True noch
nicht mit echten Daten befüllen, selbst nachdem Roster-Daten fließen.
Diese Brücke (vermutlich über Comlinks /data-Endpunkt, der defId ->
Anzeigename mitliefert) ist ein eigener, noch offener Arbeitsschritt.
"""

import asyncio
import logging

from swgoh_comlink import SwgohComlinkAsync
from swgoh_comlink.helpers._data_items import DataItems

import config

logger = logging.getLogger(__name__)

# Spät instanziiert (nicht beim Modul-Import), aus demselben Grund wie
# smartbot.py's _client: eine nicht erreichbare COMLINK_URL soll erst beim
# ersten tatsächlichen Aufruf auffallen, nicht schon beim Bot-Start.
_client: SwgohComlinkAsync | None = None


def _get_client() -> SwgohComlinkAsync:
    global _client
    if _client is None:
        _client = SwgohComlinkAsync(url=config.COMLINK_URL)
    return _client


class AllyCodeNotFoundError(Exception):
    """Comlink kennt diesen Ally-Code nicht (Tippfehler, nie ein SWGOH-Account o.ä.)."""


class RosterFetchError(Exception):
    """Comlink erreichbar, Call aber aus anderem Grund fehlgeschlagen (Timeout, 5xx, ...)."""


def normalize_ally_code(raw: str) -> str:
    """
    Normalisiert Eingaben wie '123-456-789' oder '123456789' auf die reine
    9-stellige Ziffernfolge, die comlink erwartet. Raises ValueError bei
    allem, was nach der Bereinigung nicht genau 9 Ziffern ergibt -- ein
    Format-Fehler soll VOR dem Comlink-Call auffallen, nicht als
    verwirrende AllyCodeNotFoundError danach.
    """
    digits = raw.replace("-", "").replace(" ", "").strip()
    if not digits.isdigit() or len(digits) != 9:
        raise ValueError(
            f"'{raw}' ist kein gültiger Ally-Code (erwartet: 9 Ziffern, z.B. 123456789 oder 123-456-789)."
        )
    return digits


def _parse_roster_unit(raw_unit: dict) -> dict:
    """
    !!! UNVERIFIZIERT -- siehe Modul-Docstring, Abschnitt "WICHTIG". !!!
    Feldnamen nach bestem Wissen aus Comlink-Ökosystem-Dokumentation
    zusammengetragen, nicht gegen einen echten Response geprüft.
    """
    def_id = raw_unit.get("definitionId") or raw_unit.get("defId") or ""
    # definitionId trägt bei manchen Comlink-Versionen einen ":<rarity>"-
    # Suffix (z.B. "DARTHVADER:07") -- der Basisname vor dem ":" ist die
    # stabile Unit-ID, der Suffix ist redundant zu currentRarity unten.
    unit_id = def_id.split(":")[0]

    return {
        "unit_id": unit_id,
        "rarity": raw_unit.get("currentRarity"),
        "gear_tier": raw_unit.get("currentTier"),
        "relic_tier": (raw_unit.get("relic") or {}).get("currentTier"),
        # Platzhalter, siehe Modul-Docstring -- Skill-Struktur für
        # Omicron-Erkennung noch nicht recherchiert.
        "omicrons": [],
    }


async def fetch_roster(
    ally_code: str | None = None, player_id: str | None = None
) -> tuple[str, str, str, list[dict]]:
    """
    Holt das komplette Roster für einen Spieler -- entweder über Ally-Code
    ODER über comlinks interne playerId (genau eines von beiden). playerId
    ist der Weg für guild-weit entdeckte Mitglieder (siehe
    fetch_guild_members() -- die Gilden-Mitgliederliste enthält NUR
    playerId, keinen Ally-Code, bestätigt gegen einen echten Response).
    ally_code bleibt der Weg für /tw_register, wo der Nutzer ihn direkt angibt.

    Gibt (ally_code, player_name, player_id, units) zurück -- ally_code UND
    player_id werden IMMER aus der vollen Antwort gelesen
    (player_data['allyCode']/['playerId']), nicht vom Aufrufer übernommen:
    bei einem player_id-Aufruf kennt der Aufrufer den Ally-Code vorher noch
    gar nicht, und umgekehrt bei einem ally_code-Aufruf (z.B. /tw_register)
    die playerId nicht -- beide Felder liegen aber als Klartext auf der
    player-Antwort, unabhängig davon, wie abgefragt wurde (bestätigt gegen
    einen echten Response, siehe Chat-Verlauf). player_id wird jetzt
    IMMER mitgespeichert (siehe db.upsert_player()), auch bei /tw_register
    -- Grundlage für db.delete_players_not_in(), das echte Gilden-Abgänge
    erkennen muss, was ohne eine stabile playerId pro Spieler nicht
    zuverlässig von einem bloß fehlgeschlagenen Einzel-Fetch unterscheidbar
    wäre.

    Raises:
        AllyCodeNotFoundError -- Ally-Code/playerId comlink unbekannt.
        RosterFetchError -- Comlink-Call aus anderem Grund fehlgeschlagen.
    """
    if not ally_code and not player_id:
        raise ValueError("Entweder ally_code oder player_id muss angegeben werden.")

    client = _get_client()
    identifier = player_id or ally_code
    try:
        if player_id:
            player_data = await client.get_player(player_id=player_id)
        else:
            player_data = await client.get_player(allycode=ally_code)
    except Exception as e:
        logger.warning("Comlink-Fetch für %s fehlgeschlagen: %s", identifier, e)
        raise RosterFetchError(str(e)) from e

    if not player_data or "rosterUnit" not in player_data:
        raise AllyCodeNotFoundError(f"Kein Spieler gefunden ({identifier}).")

    resolved_ally_code = player_data.get("allyCode", ally_code or "")
    resolved_player_id = player_data.get("playerId", player_id or "")
    units = [_parse_roster_unit(u) for u in player_data["rosterUnit"]]
    return resolved_ally_code, player_data.get("name", ""), resolved_player_id, units


async def resolve_guild_id(seed_ally_code: str) -> tuple[str, str]:
    """
    Ermittelt die interne SWGOH-Gilden-ID über einen bereits bekannten
    Ally-Code (comlink hat keine Freitext-Gildensuche). Gibt (guild_id,
    guild_name) zurück -- BEIDES aus einem einzigen get_player()-Call:
    guildId UND guildName liegen als Klartextfelder direkt auf der
    player-Antwort, bestätigt gegen einen echten Response (siehe Chat-
    Verlauf). Kein zusätzlicher get_guild()-Call nötig, um an den
    Gildennamen zu kommen -- ursprünglich (unverifiziert) angenommen,
    inzwischen als unnötig erkannt: ein Aufruf weniger, ein Fehlerpfad
    weniger.

    Raises:
        AllyCodeNotFoundError -- Ally-Code unbekannt ODER in keiner Gilde.
        RosterFetchError -- Comlink-Call aus anderem Grund fehlgeschlagen.
    """
    client = _get_client()
    try:
        player_data = await client.get_player(allycode=seed_ally_code)
    except Exception as e:
        logger.warning("Guild-ID-Auflösung über Ally-Code %s fehlgeschlagen: %s", seed_ally_code, e)
        raise RosterFetchError(str(e)) from e

    guild_id = player_data.get("guildId")
    if not guild_id:
        raise AllyCodeNotFoundError(
            f"Ally-Code {seed_ally_code} ist laut Comlink in keiner Gilde "
            f"(kein guildId im Response)."
        )

    return guild_id, player_data.get("guildName", "")


async def fetch_guild_members(guild_id: str) -> list[str]:
    """
    Liefert eine Liste von playerId-Werten -- comlinks interne, base64-
    artige Spieler-ID (z.B. "Xk7RXvj_SSSgst2qxR8lNQ"), AUSDRÜCKLICH KEIN
    Ally-Code, bestätigt gegen einen echten Response (siehe Chat-Verlauf).
    Weder Ally-Code noch ein nutzbarer Spielername liegen in der
    Mitgliederliste selbst vor (playerName ist dort durchgängig leer) --
    beides muss über einen anschließenden fetch_roster(player_id=...)-Call
    pro Mitglied geholt werden, siehe bot.py's _refresh_guild_rosters().

    guild_data.get("member", ...) ist hier absichtlich flach, ohne einen
    äußeren "guild"-Schlüssel zu erwarten: der comlink-Python-Client
    (SwgohComlinkAsync.get_guild(), siehe dessen Quelltext) entfernt diesen
    äußeren Schlüssel bereits selbst, bevor er das Dict zurückgibt. Ein rohes
    HTTP POST an /guild liefert dagegen {"guild": {"member": [...]}} --
    dieser Unterschied ist keine Schema-Unsicherheit mehr, sondern eine
    bestätigte Eigenschaft des Wrapper-Pakets.

    Raises:
        AllyCodeNotFoundError -- guild_id bei comlink unbekannt.
        RosterFetchError -- Comlink-Call aus anderem Grund fehlgeschlagen.
    """
    client = _get_client()
    try:
        guild_data = await client.get_guild(guild_id)
    except Exception as e:
        logger.warning("get_guild(%s) fehlgeschlagen: %s", guild_id, e)
        raise RosterFetchError(str(e)) from e

    if not guild_data:
        raise AllyCodeNotFoundError(f"Keine Gilde für guild_id {guild_id} gefunden.")

    members = guild_data.get("member", [])
    return [m["playerId"] for m in members if m.get("playerId")]


async def fetch_unit_names(locale: str = "ENG_US") -> dict[str, str]:
    """
    Baut {unit_id: Anzeigename} für den gesamten Einheiten-Katalog --
    Grundlage für die unit_names-Tabelle (siehe db.py), die
    roster_units.unit_id (comlinks defId, z.B. "ZEBS3") auf einen
    Anzeigenamen abbildet.

    VERIFIZIERT gegen einen echten Response (siehe Chat-Verlauf): ZEBS3 ->
    nameKey "UNIT_ZEBS3_NAME" -> aufgelöst zu 'Garazeb "Zeb" Orrelios'.
    Zwei comlink-Aufrufe nötig, keine Abkürzung über GameDataBuilder (der
    ist für StatCalc gedacht, nicht für Namensauflösung, siehe dessen
    Quelltext):

    1. get_game_data(items=DataItems.SEGMENT3) -- SEGMENT3 statt der
       einzelnen DataItems.UNITS-Flagge: Comlink validiert `items` gegen
       ein serverseitiges Enum und akzeptiert laut DataItems' eigenem
       Docstring nur die SEGMENT1-4-Aggregate (und ALL), nicht rohe
       Einzel-Flags -- ein einzelnes DataItems.UNITS würde vermutlich mit
       HTTP 400 abgelehnt. SEGMENT3 enthält UNITS + RELIC_TIER_DEFINITION.
    2. get_localization(locale=..., unzip=True) -- liefert KEIN
       JSON-Dict von Schlüssel zu Text, sondern eine einzige, sehr große
       (~12,5 MB im Test) pipe-getrennte Textdatei unter dem Schlüssel
       "Loc_<LOCALE>.txt" ("SCHLÜSSEL|Text" pro Zeile, Kommentarzeilen mit
       "#"), die hier manuell geparst wird.

    locale bewusst ENG_US, NICHT Deutsch: character_list.py's bestehende
    Anzeigenamen kommen aus swgoh.gg (Englisch) und liegen so bereits in
    counters.attacking_leader/defending_leader -- ein anderes locale würde
    diese Brücke gegen die eigenen Bestandsdaten kaputt machen, nicht
    reparieren.

    OFFENE, NICHT durch diese Funktion lösbare Frage: ob comlinks
    lokalisierter Name für jede Einheit exakt mit character_list.py's
    swgoh.gg-Scrape-Namen übereinstimmt (z.B. volle vs. abgekürzte Form).
    Wird hier nicht geprüft -- bot.py's format_zone_attack() behandelt
    einen Nichttreffer beim Rückwärts-Lookup explizit sichtbar, nicht
    stillschweigend.

    Raises:
        RosterFetchError -- einer der beiden Comlink-Calls fehlgeschlagen.
    """
    client = _get_client()

    try:
        game_data = await client.get_game_data(items=DataItems.SEGMENT3)
    except Exception as e:
        logger.warning("get_game_data() fehlgeschlagen: %s", e)
        raise RosterFetchError(str(e)) from e

    units = game_data.get("units", [])

    try:
        loc = await client.get_localization(locale=locale, unzip=True)
    except Exception as e:
        logger.warning("get_localization() fehlgeschlagen: %s", e)
        raise RosterFetchError(str(e)) from e

    raw_text = loc.get(f"Loc_{locale}.txt", "")

    # Beides in Worker-Threads statt direkt hier synchron auszuführen --
    # das Parsen von ~12,5 MB Text zu ~70.000 Einträgen UND das
    # anschließende Durchlaufen von ~11.000 Einheiten sind reine CPU-Arbeit
    # ohne await dazwischen. Direkt im Event-Loop ausgeführt, blockiert das
    # den gesamten Bot für die Dauer des Parsens -- keine andere Discord-
    # Interaction kann in dieser Zeit bedient werden. Genau das hat einmal
    # einen parallelen /tw_lookup-Aufruf an Discords 3-Sekunden-
    # Antwortfenster vorbeirauschen lassen (siehe Chat-Verlauf,
    # "Unknown interaction"-Fehler trotz an sich schnellem Command).
    loc_map = await asyncio.to_thread(_parse_localization_text, raw_text)
    return await asyncio.to_thread(_build_unit_name_map, units, loc_map)


def _parse_localization_text(raw_text: str) -> dict[str, str]:
    """Reine, synchrone Funktion -- bewusst von fetch_unit_names()
    getrennt, damit sie via asyncio.to_thread() in einem Worker-Thread
    laufen kann (siehe dortiger Kommentar)."""
    loc_map: dict[str, str] = {}
    for line in raw_text.split("\n"):
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 1)
        if len(parts) == 2:
            loc_map[parts[0]] = parts[1]
    return loc_map


def _build_unit_name_map(units: list[dict], loc_map: dict[str, str]) -> dict[str, str]:
    """Reine, synchrone Funktion -- ebenfalls für asyncio.to_thread()
    ausgelagert, siehe fetch_unit_names()."""
    names: dict[str, str] = {}
    for unit in units:
        unit_id = unit.get("id")
        name_key = unit.get("nameKey")
        if not unit_id or not name_key:
            continue
        display_name = loc_map.get(name_key)
        if display_name:
            names[unit_id] = display_name
    return names
