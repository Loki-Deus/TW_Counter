"""
Smartbot-Query-Layer für den TW-Counter-Bot — natürlichsprachliche Anfragen
("was kontert Darth Vader?" / "what counters Lord Vader?") über /tw_ask.

Rein lesend: berührt nie counters/reports schreibend, nutzt ausschließlich
db.get_attackers_for_defender() und db.get_bucket_stats(), dieselben Calls,
die auch /tw_lookup verwendet.

Architektur — zwei-Tool-Design statt freier Textantwort direkt vom Modell:
  lookup_counters(defending_leader: enum[character_names])
      -> Modell hat den Namen eindeutig einem Eintrag in character_names
         zugeordnet (inkl. Community-Slang wie "CLS", "GAS" etc.).
  report_unresolved(extracted_name, language)
      -> Modell konnte NICHT eindeutig zuordnen: entweder kein Treffer,
         oder mehrere plausible Treffer (z.B. "Lord Vader" vs. "Darth
         Vader" — beide real existierende, unterschiedliche Einträge).
         Löst absichtlich KEINEN zweiten API-Call aus; die Antwort ist ein
         lokal formatiertes Template, nicht vom Modell selbst formuliert.
         Das verhindert, dass das Modell bei der Ablehnung selbst wieder
         einen falschen/erfundenen Kandidatennamen nennt.

Das Modell wird über tool_choice={"type": "any"} gezwungen, IMMER genau
eines der beiden Tools aufzurufen, nie direkt in Freitext zu antworten —
die eigentliche Antwortformulierung passiert erst im zweiten Call
(Syntheseschritt), NACHDEM die echten DB-Daten vorliegen. Das hält das
Modell strikt an das, was in db.py tatsächlich steht, und verhindert
Halluzination von Konter-Ergebnissen.

Sprache: das Modell erkennt die Sprache der Anfrage selbst (de/en) und
antwortet in derselben Sprache — sowohl bei report_unresolved (über das
`language`-Feld, lokal ins passende Template gemappt) als auch im
Syntheseschritt (per Prompt-Instruktion).
"""

import json
import logging

from anthropic import AsyncAnthropic

import config

logger = logging.getLogger(__name__)

# Bewusst spät instanziiert (nicht beim Modul-Import), damit ein fehlender
# ANTHROPIC_API_KEY nicht schon beim Bot-Start crasht, sondern erst beim
# ersten tatsächlichen /tw_ask-Aufruf sichtbar wird. config.ANTHROPIC_API_KEY
# selbst ist über os.getenv geladen (kein Cast, kein Crash beim Import) --
# siehe config.py. Ist der Key None, wirft AsyncAnthropic() hier, nicht früher.
_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# Gepinnte Modell-ID, kein "-latest"-Alias — ein stillschweigender
# Modellwechsel soll dieses Verhalten (insb. die Ambiguitätsprüfung) nicht
# unbemerkt verändern können.
MODEL = "claude-haiku-4-5-20251001"

# Müssen mit den String-Literalen aus db.py's _BUCKET_CASE übereinstimmen
# ("under"/"even"/"over"). Bewusst hier lokal dupliziert statt aus bot.py
# importiert — ein Import aus bot.py würde einen Zirkelimport erzeugen,
# da bot.py umgekehrt dieses Modul importiert.
_BUCKET_ORDER = ("under", "even", "over")

_UNRESOLVED_MESSAGES = {
    "en": "I didn't get that — could you rephrase? I don't know who '{name}' is.",
    "de": "Das habe ich nicht verstanden — kannst du das anders formulieren? Ich kenne '{name}' nicht.",
}

_NO_CHARACTER_DATA_MESSAGE = (
    "Character data isn't loaded right now, so I can't resolve any names — "
    "try again later. / Charakterdaten sind aktuell nicht geladen, daher kann "
    "ich keine Namen zuordnen — bitte später erneut versuchen."
)

_SYSTEM_PROMPT = """You are a lookup assistant for a Star Wars: Galaxy of Heroes \
Territory War counter database. Users ask which attacking leaders counter a \
given defending leader, in English or German.

You are given the full list of valid leader names as the enum on the \
lookup_counters tool. You must call exactly one tool — never respond in \
plain text at this stage.

Call lookup_counters ONLY if the leader name in the question matches \
exactly one entry in that list with reasonable confidence. This includes \
matching SWGOH community shorthand and acronyms to their canonical entry \
(e.g. "CLS" -> "Commander Luke Skywalker", "GAS" -> "Grand Admiral Thrawn").

Call report_unresolved instead if:
- no entry plausibly matches what the user wrote, OR
- two or more entries are both plausible matches — near-identical names, \
one name being a substring of another, or a nickname that could refer to \
more than one distinct unit. Do not guess between them.

When calling report_unresolved, pass extracted_name exactly as the user \
wrote it (do not correct or normalize it), and language as the language \
the user's question was written in ("en" or "de")."""

_SYNTHESIS_SYSTEM_PROMPT = """Answer the user's question using ONLY the data \
in the tool result below — never state a win/loss figure or bucket that \
isn't present in it. Respond in the same language the user's question was \
written in. Keep it to a few sentences of prose, not a table."""


def _build_tools(character_names: list[str]) -> list[dict]:
    return [
        {
            "name": "lookup_counters",
            "description": (
                "Look up all known TW counters against a defending leader. "
                "Only call this when the defending leader is unambiguous."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "defending_leader": {
                        "type": "string",
                        "enum": character_names,
                        "description": (
                            "Canonical leader name, exactly as it appears "
                            "in this list."
                        ),
                    }
                },
                "required": ["defending_leader"],
            },
        },
        {
            "name": "report_unresolved",
            "description": (
                "Call this instead of lookup_counters when the leader name "
                "cannot be uniquely resolved against the provided list — "
                "no match, or more than one plausible match."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "extracted_name": {
                        "type": "string",
                        "description": "The leader name as the user wrote it, unmodified.",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["en", "de"],
                        "description": "Language of the user's question.",
                    },
                },
                "required": ["extracted_name", "language"],
            },
        },
    ]


def _build_counter_data(defending_leader: str) -> dict:
    """
    Strukturierte (JSON-taugliche) Konter-Daten für den Syntheseschritt —
    bewusst NICHT bot.py's format_lookup_table() wiederverwendet, das eine
    fixed-width ASCII-Tabelle für Discord-Codeblöcke erzeugt. Das ist die
    falsche Form, um sie einem Modell als Rohdaten zu übergeben.

    Duplikation der Aggregationslogik aus bot.py.format_lookup_table() ist
    hier bewusst in Kauf genommen statt eines gemeinsamen Imports, um genau
    den oben beschriebenen Zirkelimport zu vermeiden.
    """
    import db  # lokaler Import, siehe Hinweis zu Zirkelimporten oben

    attackers = db.get_attackers_for_defender(defending_leader)
    bucket_rows = db.get_bucket_stats(defending_leader)

    stats = {a: {b: {"wins": 0, "losses": 0} for b in _BUCKET_ORDER} for a in attackers}
    for row in bucket_rows:
        attacker = row["attacking_leader"]
        bucket = row["bucket"]
        if attacker in stats and bucket in stats[attacker]:
            stats[attacker][bucket]["wins"] = row["wins"] or 0
            stats[attacker][bucket]["losses"] = row["losses"] or 0

    return {"defending_leader": defending_leader, "attackers": stats}


async def answer_query(question: str, character_names: list[str]) -> str:
    """
    Haupteinstiegspunkt für /tw_ask. Führt den Resolve-dann-Synthese-Loop
    aus und gibt fertigen, sendefähigen Text zurück.

    Wirft absichtlich NICHTS für den normalen "konnte nicht auflösen"-Fall
    zurück (das ist ein gültiges Ergebnis, kein Fehler) — nur echte API-/
    Transportfehler propagieren nach oben. bot.py fängt diese und formt
    daraus eine generische Fehlermeldung für den Nutzer.
    """
    if not character_names:
        # Kein API-Call ohne validen (nicht-leeren) Enum -- vermeidet einen
        # Requestfehler und spart den Call in einem Zustand, in dem wir
        # ohnehin nichts auflösen könnten (siehe character_list.py).
        return _NO_CHARACTER_DATA_MESSAGE

    client = _get_client()
    tools = _build_tools(character_names)
    messages = [{"role": "user", "content": question}]

    response = await client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=_SYSTEM_PROMPT,
        tools=tools,
        tool_choice={"type": "any"},
        messages=messages,
    )

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        # Sollte bei tool_choice="any" nicht passieren -- defensiver Fallback.
        logger.error("Modell hat trotz tool_choice=any keinen Tool-Call geliefert.")
        return _UNRESOLVED_MESSAGES["en"].format(name=question)

    if tool_use.name == "report_unresolved":
        name = tool_use.input.get("extracted_name", question)
        language = tool_use.input.get("language", "en")
        template = _UNRESOLVED_MESSAGES.get(language, _UNRESOLVED_MESSAGES["en"])
        return template.format(name=name)

    # ── lookup_counters ────────────────────────────────────────────────
    defending_leader = tool_use.input["defending_leader"]

    # Verteidigungslinie: der Enum-Constraint im Tool-Schema sollte einen
    # Wert außerhalb von character_names bereits unmöglich machen, aber
    # ein String, der gleich in db.py's WHERE-Klausel landet, wird hier
    # trotzdem verifiziert -- billige Absicherung, kein Grund, sie
    # auszulassen.
    if defending_leader not in character_names:
        logger.warning(
            "Modell lieferte '%s' außerhalb von character_names -- abgelehnt.",
            defending_leader,
        )
        return _UNRESOLVED_MESSAGES["en"].format(name=defending_leader)

    counter_data = _build_counter_data(defending_leader)

    if not counter_data["attackers"]:
        # Keine Konter hinterlegt -- deterministische Antwort, kein
        # zweiter API-Call nötig (es gibt nichts zu synthetisieren).
        return f"No counters known for {defending_leader} yet."

    messages.append({"role": "assistant", "content": response.content})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": json.dumps(counter_data, ensure_ascii=False),
                }
            ],
        }
    )

    final = await client.messages.create(
        model=MODEL,
        max_tokens=400,
        system=_SYNTHESIS_SYSTEM_PROMPT,
        messages=messages,
    )

    text_block = next((b for b in final.content if b.type == "text"), None)
    if text_block is None:
        logger.error("Syntheseschritt lieferte keinen Text-Block zurück.")
        return _UNRESOLVED_MESSAGES["en"].format(name=defending_leader)

    return text_block.text
