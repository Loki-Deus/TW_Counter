"""
Charakterliste für den TW-Counter-Bot — Autocomplete-Datenquelle für
/tw_add, /tw_report, /tw_lookup.

swgoh.gg/characters/ läuft hinter einer aktiven Cloudflare-JS-Challenge.
Kein automatisierter HTTP-Client kommt daran vorbei — bestätigt durch
direkten Test (curl unter Windows und WSL, beide Header-Varianten). Es gibt
deshalb keinen Live-Fetch. Die Datenquelle ist eine manuell im Browser
gespeicherte Kopie der Seite.

Workflow: https://swgoh.gg/characters/ im Browser öffnen, als HTML
speichern, als `swgoh_characters.html` in DATA_DIR ablegen. Der Bot liest
und parst diese Datei beim Start und beim wöchentlichen Refresh-Task in
bot.py — "Refresh" bedeutet hier: erneutes Einlesen der aktuell in DATA_DIR
liegenden Datei, kein Netzwerkzugriff. Das Ergebnis wird zusätzlich in
characters.json gecacht, damit ein zeitweise fehlendes
swgoh_characters.html (z.B. nach einem Volume-Reset) nicht sofort zum
Totalausfall der Autocomplete führt, solange ein vorheriger Parse-Durchlauf
gespeichert ist.
"""

import json
import logging
import os
import re

from bs4 import BeautifulSoup

from config import DATA_DIR

logger = logging.getLogger(__name__)

# Nur Referenz für den manuellen Download — der Code fragt diese URL nie an.
CHARACTERS_SOURCE_URL = "https://swgoh.gg/characters/"

LOCAL_HTML_PATH = os.path.join(DATA_DIR, "swgoh_characters.html")
CACHE_PATH = os.path.join(DATA_DIR, "characters.json")

# Jeder Charakter-Link zeigt auf /units/<slug>/. Slug-Zeichensatz: Kleinbuchstaben,
# Ziffern, Bindestriche — beobachtet an allen ~330 Einträgen der realen Seite.
_UNIT_HREF_RE = re.compile(r"^/units/([a-z0-9]+(?:-[a-z0-9]+)*)/$")

# Linktext-Format, verifiziert gegen die reale Seite: "<Name> <Role> • <Tag> …"
# oder ohne Tags: "<Name> <Role> •". Die vier Rollen sind erschöpfend.
_NAME_ROLE_RE = re.compile(r"^(.*?)\s+(Attacker|Support|Tank|Healer)\b")


class CharacterParseError(Exception):
    """swgoh_characters.html existiert, aber ergibt null parsebare Charaktere."""


class CharacterDataUnavailableError(Exception):
    """Weder swgoh_characters.html noch ein bestehender Cache waren verfügbar/gültig."""


def parse_character_entry(link_text: str, href: str) -> tuple[str, str] | None:
    """
    Extrahiert (name, slug) aus einem einzelnen Charakter-Link.
    Gibt None zurück (statt zu werfen), wenn der Linktext nicht dem erwarteten
    "<Name> <Role> • ..." Muster entspricht — ein einzelner unerwarteter
    Eintrag soll den gesamten Parse-Durchlauf nicht zum Absturz bringen,
    sondern nur sich selbst überspringen.
    """
    href_match = _UNIT_HREF_RE.match(href)
    if not href_match:
        return None
    slug = href_match.group(1)

    name_match = _NAME_ROLE_RE.match(link_text.strip())
    if not name_match:
        return None
    name = name_match.group(1).strip()
    if not name:
        return None

    return name, slug


def parse_characters_html(html: str) -> dict[str, str]:
    """
    Parst eine gespeicherte Kopie von swgoh.gg/characters/ zu {slug: name}.
    Raises CharacterParseError bei null gefundenen Charakteren — das deutet
    auf eine falsch gespeicherte Datei hin (z.B. die Cloudflare-Challenge-
    Seite statt der echten Seite gespeichert) oder eine geänderte
    Seitenstruktur, nicht auf "keine Charaktere existieren".
    """
    soup = BeautifulSoup(html, "html.parser")

    characters: dict[str, str] = {}
    skipped = 0
    for anchor in soup.find_all("a", href=_UNIT_HREF_RE):
        entry = parse_character_entry(anchor.get_text(" ", strip=True), anchor["href"])
        if entry is None:
            skipped += 1
            continue
        name, slug = entry
        characters[slug] = name

    if not characters:
        raise CharacterParseError(
            f"{LOCAL_HTML_PATH} enthält 0 parsebare Charaktere — vermutlich wurde "
            f"die Cloudflare-Challenge-Seite ('Just a moment...') statt der "
            f"echten Seite gespeichert, oder die Seitenstruktur hat sich geändert."
        )

    if skipped:
        logger.warning(
            "%d Link(s) auf /units/ konnten nicht geparst werden und wurden "
            "übersprungen (%d erfolgreich).",
            skipped,
            len(characters),
        )

    return characters


def load_local_html() -> str | None:
    """None, wenn swgoh_characters.html (noch) nicht abgelegt wurde."""
    if not os.path.exists(LOCAL_HTML_PATH):
        return None
    with open(LOCAL_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


def save_local_html(html: str) -> None:
    """
    Schreibt eine (bereits validierte) HTML-Kopie atomar nach LOCAL_HTML_PATH
    (tmp-Datei + os.replace) — verhindert, dass ein Absturz mitten im
    Schreiben eine halb geschriebene, korrupte Datei hinterlässt. Der
    Aufrufer ist dafür verantwortlich, den Inhalt VOR diesem Aufruf über
    parse_characters_html() zu validieren — diese Funktion selbst prüft
    nichts, sie schreibt nur.
    """
    tmp_path = LOCAL_HTML_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp_path, LOCAL_HTML_PATH)


def load_cache() -> dict[str, str] | None:
    """Liest den zuletzt gespeicherten Parse-Cache. None, wenn er fehlt oder korrupt ist."""
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data:
            return None
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "characters.json konnte nicht gelesen werden (%s) — Cache gilt als leer.", e
        )
        return None


def save_cache(characters: dict[str, str]) -> None:
    """
    Schreibt den Cache atomar (tmp-Datei + os.replace), damit ein Absturz
    mitten im Schreiben nie eine halb geschriebene, korrupte characters.json
    hinterlässt.
    """
    tmp_path = CACHE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(characters, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CACHE_PATH)


def get_characters(force_refresh: bool = False) -> dict[str, str]:
    """
    Zentrale Zugriffsfunktion für bot.py: liefert {slug: name}.

    force_refresh=False -> Bot-Start: nutzt characters.json direkt, falls
                            vorhanden, ohne swgoh_characters.html neu einzulesen.
    force_refresh=True  -> wöchentlicher Refresh-Task: liest swgoh_characters.html
                            neu ein, unabhängig davon, ob ein Cache existiert.

    Reihenfolge bei force_refresh=True: swgoh_characters.html einlesen und
    parsen -> bei Erfolg Cache aktualisieren und zurückgeben -> bei
    fehlendem/nicht parsebarem File auf bestehenden Cache zurückfallen ->
    bei beidem nicht verfügbar: harter Fehler.
    """
    if not force_refresh:
        cached = load_cache()
        if cached is not None:
            return cached

    html = load_local_html()
    if html is not None:
        try:
            characters = parse_characters_html(html)
            save_cache(characters)
            logger.info(
                "Charakterliste aus %s eingelesen: %d Charaktere.",
                LOCAL_HTML_PATH,
                len(characters),
            )
            return characters
        except CharacterParseError as e:
            logger.warning("%s — falle auf bestehenden Cache zurück.", e)
    else:
        logger.warning(
            "%s nicht gefunden — falle auf bestehenden Cache zurück.", LOCAL_HTML_PATH
        )

    cached = load_cache()
    if cached is not None:
        logger.info(
            "Fallback auf Cache erfolgreich: %d Charaktere (ggf. veraltet).",
            len(cached),
        )
        return cached

    raise CharacterDataUnavailableError(
        f"Weder {LOCAL_HTML_PATH} noch ein bestehender Cache waren verfügbar — "
        f"keine Charakterdaten für Autocomplete. {CHARACTERS_SOURCE_URL} im "
        f"Browser öffnen, Seite speichern, als {LOCAL_HTML_PATH} ablegen."
    )
