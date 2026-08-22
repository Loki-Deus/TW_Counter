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

+++ WICHTIG, vor Produktivbetrieb zwingend zu verifizieren +++
_parse_roster_unit() unten geht von Feldnamen aus, die aus Dokumentation
benachbarter Comlink-Ökosystem-Projekte zusammengetragen wurden
(swgoh-stat-calc, comlink-python), NICHT aus einem tatsächlichen Response
einer laufenden Instanz -- die Entwicklungsumgebung, in der dieses Modul
geschrieben wurde, hatte keinen Netzwerkzugriff auf eine comlink-Instanz.
Vor dem ersten produktiven /tw_register: einen echten get_player()-Call
gegen die laufende Instanz absetzen, die Rohstruktur von rosterUnit[0]
loggen (z.B. kurzzeitig ein `logger.info(json.dumps(raw_unit))` in
_parse_roster_unit() einfügen), und die Feldzuordnungen unten bei
Abweichung korrigieren. Die Omicron-Erkennung ist unterhalb davon sogar
nur ein Platzhalter (immer []), weil die exakte Skill-Struktur dafür noch
gar nicht recherchiert wurde -- nicht nur unverifiziert, sondern absichtlich
nicht implementiert, bis das nachgeholt ist.

Speichert NUR normalisierte Werte (rarity, gear_tier, relic_tier,
omicrons) in db.py, nicht den rohen Comlink-Response.

WICHTIGE OFFENE LÜCKE, nicht Teil dieser Änderung: unit_id hier ist
Comlinks defId (z.B. "DARTHVADER") -- ein anderer Namensraum als der
Anzeigename aus character_list.py (z.B. "Darth Vader", geparst aus
swgoh.gg-HTML) und wieder ein anderer als dessen dortiger URL-Slug. Es
gibt aktuell KEINE Zuordnungstabelle zwischen diesen drei Namensräumen.
Das bedeutet: die in db.py gespeicherten Rosterdaten lassen sich noch
NICHT gegen counters.attacking_leader / defending_leader (Anzeigenamen)
abgleichen -- also auch /tw_zone_attack mit mitgliederliste=True noch
nicht mit echten Daten befüllen, selbst nachdem Roster-Daten fließen.
Diese Brücke (vermutlich über Comlinks /data-Endpunkt, der defId ->
Anzeigename mitliefert) ist ein eigener, noch offener Arbeitsschritt.
"""

import logging

from swgoh_comlink import SwgohComlinkAsync

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


async def fetch_roster(ally_code: str) -> tuple[str, list[dict]]:
    """
    Holt das komplette Roster für einen (bereits normalisierten) Ally-Code.
    Gibt (player_name, units) zurück, units als Liste von
    _parse_roster_unit()-Dicts.

    Raises:
        AllyCodeNotFoundError -- comlink kennt den Ally-Code nicht.
        RosterFetchError -- Comlink-Call aus anderem Grund fehlgeschlagen.
    """
    client = _get_client()
    try:
        player_data = await client.get_player(allycode=ally_code)
    except Exception as e:
        # swgoh_comlink wirft je nach Fehlerart unterschiedliche
        # Exception-Klassen -- hier bewusst breit gefangen und in eine
        # eigene Exception übersetzt, damit bot.py nicht wissen muss,
        # welche Exceptions ausgerechnet dieses HTTP-Client-Paket intern
        # wirft.
        logger.warning("Comlink-Fetch für Ally-Code %s fehlgeschlagen: %s", ally_code, e)
        raise RosterFetchError(str(e)) from e

    if not player_data or "rosterUnit" not in player_data:
        raise AllyCodeNotFoundError(f"Kein Spieler für Ally-Code {ally_code} gefunden.")

    units = [_parse_roster_unit(u) for u in player_data["rosterUnit"]]
    return player_data.get("name", ""), units
