"""The memory model: what is known about each keepsake, and where each piece of it came from.

The rule the whole app is built on: **the model never gets to invent a memory.** Every detail on an
item carries a source, and the sources have a fixed order of authority:

    family  >  read  >  seen  >  guess

- `family`: a person told us. Nothing the model does later can overwrite it.
- `read`: it is written on the object, and the code checked that the value really appears in the
  transcribed text (`grounded`). The model saying "I read 1998" is not enough; "1998" has to be in the
  transcription. The model's own say-so is never enough.
- `seen`: what the object physically is.
- `guess`: the model's inference. Shown to the family as a question, written in the story only with
  "appears to".

A correction ("No, that was 1997") turns a detail into `family`, keeps the old value in the item's
history, and adds a line to the family's memory notes, which every later object is read against.
All of this is plain Python; the model only proposes.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

SOURCES = ("family", "read", "seen", "guess")
AUTHORITY = {s: i for i, s in enumerate(reversed(SOURCES))}  # guess 0 ... family 3
FIELDS = ("title", "kind", "date", "place", "people", "occasion")
GUESSED = ("date", "place", "people", "occasion")
LABELS = {"title": "Title", "kind": "What it is", "date": "When", "place": "Where", "people": "Who",
          "occasion": "Occasion"}
MAX_FACTS_IN_PROMPT = 40

_GUESS = {"type": "object", "properties": {"value": {"type": "string"}, "basis": {"type": "string"},
                                            "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
          "required": ["value", "basis", "confidence"], "additionalProperties": False}
LOOK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "title": {"type": "string"},
        "visible_text": {"type": "string"},
        "translation": {"type": "string"},
        "observations": {"type": "array", "items": {"type": "string"}},
        "guesses": {"type": "object", "properties": {f: _GUESS for f in GUESSED},
                    "required": list(GUESSED), "additionalProperties": False},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind", "title", "visible_text", "translation", "observations", "guesses", "questions"],
    "additionalProperties": False,
}
STORY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"story": {"type": "string"}},
                                "required": ["story"], "additionalProperties": False}
UNDERSTAND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "updates": {"type": "array", "items": {
            "type": "object",
            "properties": {"field": {"type": "string", "enum": list(FIELDS)}, "value": {"type": "string"}},
            "required": ["field", "value"], "additionalProperties": False}},
        "facts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["updates", "facts"],
    "additionalProperties": False,
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


_YEAR = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")


def grounded(value: str, visible_text: str) -> bool:
    """Is `value` actually written on the object? Years must all appear in the transcription; anything
    else must appear in it as a whole phrase. Deliberately strict: a false `read` is the one label
    that would let a guess pass as a memory."""
    if not value or not visible_text:
        return False
    years = _YEAR.findall(value)
    if years:
        return all(y in visible_text for y in years)
    v, t = normalise(value), normalise(visible_text)
    return len(v) >= 3 and f" {v} " in f" {t} "


def field(value: str, source: str, basis: str = "", by: str | None = None) -> dict:
    return {"value": value.strip(), "source": source, "basis": basis.strip(), "by": by, "at": now_iso()}


def owner(item: dict, name: str) -> dict | None:
    return item.get("fields", {}).get(name)


def set_if_allowed(item: dict, name: str, new: dict) -> bool:
    """Write a detail unless the one already there has more authority (a family detail is never
    overwritten by the model). Returns whether it was written."""
    old = owner(item, name)
    if old and old.get("value") and AUTHORITY[old["source"]] > AUTHORITY[new["source"]]:
        return False
    item.setdefault("fields", {})[name] = new
    return True


def apply_look(item: dict, parsed: dict) -> None:
    """Turn the looking stage's reply into details, grounding each guess against the transcription."""
    visible = (parsed.get("visible_text") or "").strip()
    # A new reading replaces the old one completely, except for what the family said. Otherwise a
    # guess from the first look survives a second look that found nothing to support it.
    fields = item.setdefault("fields", {})
    for name in [n for n, f in fields.items() if f.get("source") != "family"]:
        del fields[name]
    item["inscription"] = visible
    item["translation"] = (parsed.get("translation") or "").strip()
    item["observations"] = [o.strip() for o in parsed.get("observations") or [] if isinstance(o, str) and o.strip()][:5]
    item["questions"] = [q.strip() for q in parsed.get("questions") or [] if isinstance(q, str) and q.strip()][:3]
    if parsed.get("kind"):
        set_if_allowed(item, "kind", field(parsed["kind"], "seen"))
    if parsed.get("title"):
        title = parsed["title"]
        # A year in the title must be on the object too; otherwise it is a guess dressed as a title.
        source = "seen" if not _YEAR.search(title) or grounded(title, visible) else "guess"
        set_if_allowed(item, "title", field(title, source))
    for name in GUESSED:
        g = (parsed.get("guesses") or {}).get(name) or {}
        value = (g.get("value") or "").strip() if isinstance(g, dict) else ""
        if not value:
            continue
        source = "read" if grounded(value, visible) else "guess"
        basis = g.get("basis", "") if source == "guess" else "written on the object"
        set_if_allowed(item, name, field(value, source, basis))


def apply_correction(item: dict, updates: list[dict], by: str, words: str) -> list[dict]:
    """Make each update a family detail; record what it replaced. Returns the history entries."""
    entries = []
    for u in updates:
        name, value = u.get("field"), (u.get("value") or "").strip()
        if name not in FIELDS or not value:
            continue
        old = owner(item, name) or {}
        if old.get("source") == "family" and old.get("value") == value:
            continue
        item.setdefault("fields", {})[name] = field(value, "family", by=by)
        entries.append({"at": now_iso(), "by": by, "field": name, "old": old.get("value"),
                        "old_source": old.get("source"), "new": value, "words": words})
    item.setdefault("history", []).extend(entries)
    # A guess the family has answered is no longer a question.
    if entries:
        answered = {e["field"] for e in entries}
        item["questions"] = [q for q in item.get("questions", []) if not _about(q, answered)]
    return entries


_QUESTION_WORDS = {"date": ("when", "year", "date"), "place": ("where", "place"), "people": ("who", "whom"),
                   "occasion": ("what race", "occasion", "event", "why", "what was")}


def _about(question: str, fields: set[str]) -> bool:
    q = question.casefold()
    return any(w in q for f in fields for w in _QUESTION_WORDS.get(f, ()))


def fact_for_update(item: dict, entry: dict) -> str:
    """The memory-notes line a correction leaves behind, so later objects are read against it."""
    title = (owner(item, "title") or {}).get("value") or f"object {item['id']}"
    return f"{title}: {LABELS[entry['field']].lower()} is {entry['new']}."


# --- the text the models are given -------------------------------------------------------------


def item_text(item: dict) -> str:
    lines = []
    for name in FIELDS:
        f = owner(item, name)
        if f and f.get("value"):
            basis = f" (basis: {f['basis']})" if f["source"] == "guess" and f.get("basis") else ""
            lines.append(f"- {LABELS[name]}: {f['value']} [{f['source']}]{basis}")
    if item.get("inscription"):
        lines.append(f"- Written on it: {item['inscription']} [read]")
    if item.get("translation"):
        lines.append(f"- Translation of the writing: {item['translation']} [read]")
    for o in item.get("observations", []):
        lines.append(f"- {o} [seen]")
    return "\n".join(lines) or "- (nothing known yet)"


def _tokens(text: str) -> set[str]:
    return {w for w in normalise(text).split() if len(w) > 3 or w.isdigit()}


def relevant_facts(facts: list[dict], item: dict | None, limit: int = MAX_FACTS_IN_PROMPT) -> list[dict]:
    """The memory notes most likely to matter for this object: overlap with its details first, then
    the most recent. Small enough to fit a small model's context with room to spare."""
    if len(facts) <= limit:
        return list(facts)
    if item is None:
        return facts[-limit:]
    want = _tokens(item_text(item))
    scored = sorted(enumerate(facts), key=lambda p: (len(want & _tokens(p[1]["text"])), p[0]), reverse=True)
    return [f for _, f in sorted(scored[:limit], key=lambda p: p[0])]


def context_text(profile: dict, facts: list[dict], item: dict | None = None) -> str:
    name = profile.get("owner") or "the person this box belongs to"
    parts = [f"The box belongs to: {name}."]
    if profile.get("about"):
        parts.append(f"About {name}, from the family: {profile['about'].strip()}")
    chosen = relevant_facts(facts, item)
    if chosen:
        parts.append("What the family has told you so far:")
        parts += [f"- {f['text']}" for f in chosen]
    else:
        parts.append("The family has not told you anything else yet.")
    return "\n".join(parts)


def address_line(profile: dict) -> str:
    name = profile.get("owner") or "them"
    if profile.get("voice") == "about":
        return f"Address: write about {name}, by name, in the third person."
    return f"Address: write to {name} directly, in the second person (\"you\", \"your\")."


# --- the timeline ------------------------------------------------------------------------------


def year_of(item: dict) -> int | None:
    f = owner(item, "date")
    m = _YEAR.search(f["value"]) if f and f.get("value") else None
    return int(m.group(1)) if m else None
