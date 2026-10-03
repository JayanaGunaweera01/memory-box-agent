"""A stand-in for the model, for the tests and for `memorybox --demo` (no Ollama needed).

It answers in the same shapes the real prompts ask for, with canned keepsakes, so the whole app
(queue, provenance, corrections, the book) can be tried and screenshotted on any machine. It is
never used unless asked for.
"""

from __future__ import annotations

import json
import re
import time
from itertools import cycle
from typing import Any

from memorybox.adapters.base import DEFAULT_MAX_TOKENS, CallResult
from memorybox.config import Model

KEEPSAKES: list[dict[str, Any]] = [
    {"kind": "a bronze medal on a blue and gold ribbon", "title": "Athletics medal",
     "visible_text": "INTER-SCHOOL ATHLETICS 400 M", "translation": "",
     "observations": ["Bronze with worn edges", "The ribbon is faded on one side", "Stamped lettering, no engraving"],
     "guesses": {"date": {"value": "1998", "basis": "the family notes mention a school sports meet in 1998",
                          "confidence": "medium"},
                 "place": {"value": "", "basis": "", "confidence": "low"},
                 "people": {"value": "", "basis": "", "confidence": "low"},
                 "occasion": {"value": "school athletics competition, 400 metres", "basis": "the stamped words",
                              "confidence": "high"}},
     "questions": ["What year was this race?", "Did he win, or place?"]},
    {"kind": "a handwritten letter on blue airmail paper", "title": "Airmail letter from Kandy",
     "visible_text": "Kandy, 12th March 1974. My dear Nimal,", "translation": "",
     "observations": ["Folded in three", "Blue ink, a little faded", "Airmail paper"],
     "guesses": {"date": {"value": "12 March 1974", "basis": "the date at the top", "confidence": "high"},
                 "place": {"value": "Kandy", "basis": "written at the top", "confidence": "high"},
                 "people": {"value": "written to Nimal", "basis": "the greeting", "confidence": "high"},
                 "occasion": {"value": "", "basis": "", "confidence": "low"}},
     "questions": ["Who wrote this letter to Nimal?"]},
    {"kind": "a black-and-white studio photograph", "title": "Studio portrait of a young couple",
     "visible_text": "", "translation": "",
     "observations": ["Studio backdrop with a painted pillar", "Matte paper with a deckled edge",
                      "She wears a saree with a wide border"],
     "guesses": {"date": {"value": "1960s", "basis": "the paper and the clothing", "confidence": "low"},
                 "place": {"value": "", "basis": "", "confidence": "low"},
                 "people": {"value": "a young man and woman, dressed formally", "basis": "the photo",
                            "confidence": "high"},
                 "occasion": {"value": "possibly a wedding or engagement", "basis": "formal studio portrait",
                              "confidence": "low"}},
     "questions": ["Who are the two people in this photo?", "Was this taken for a wedding?"]},
]


def _result(model: Model, parsed: dict) -> CallResult:
    return CallResult(model.id_for_adapter, "fake", json.dumps(parsed), parsed, 100, 50, 0.0, 5, None, "stop",
                      adapter="fake")


class FakeClient:
    def __init__(self, keepsakes: list[dict] | None = None, *, delay_s: float = 0.0, fail_vision: str | None = None) -> None:
        self._next = cycle(keepsakes or KEEPSAKES)
        self.delay_s = delay_s
        self.fail_vision = fail_vision
        self.prompts: list[str] = []
        self.image_counts: list[int] = []

    def vision(self, model: Model, prompt: str, image_jpeg: bytes | list[bytes], *, max_tokens: int = DEFAULT_MAX_TOKENS,
               on_progress=None, schema: dict | None = None) -> CallResult:
        self.prompts.append(prompt)
        self.image_counts.append(1 if isinstance(image_jpeg, bytes) else len(image_jpeg))
        time.sleep(self.delay_s)
        if self.fail_vision:
            return CallResult(model.id_for_adapter, "fake", None, None, None, None, 0.0, 5, self.fail_vision, adapter="fake")
        return _result(model, json.loads(json.dumps(next(self._next))))

    def text(self, model: Model, prompt: str, input_text: str, *, max_tokens: int = DEFAULT_MAX_TOKENS,
             on_progress=None, schema: dict | None = None) -> CallResult:
        self.prompts.append(f"{prompt}\n\n{input_text}")
        time.sleep(self.delay_s)
        if schema and "story" in schema.get("properties", {}):
            return _result(model, {"story": self._story(input_text)})
        return _result(model, self._understand(input_text))

    @staticmethod
    def _story(text: str) -> str:
        details = dict(re.findall(r"^- (\w[\w ]*?): (.+?) \[(?:family|read|seen)\]", text, re.M))
        guesses = dict(re.findall(r"^- (\w[\w ]*?): (.+?) \[guess\]", text, re.M))
        kind = details.get("What it is", "an object")
        out = [f"This is {kind}."]
        if "When" in details:
            out.append(f"It is from {details['When']}.")
        elif "When" in guesses:
            out.append(f"It appears to be from {guesses['When']}.")
        if "Occasion" in details:
            out.append(f"It was for the {details['Occasion']}.")
        return " ".join(out)

    @staticmethod
    def _understand(text: str) -> dict:
        words = text.split("said:\n", 1)[-1]
        updates, facts = [], []
        year = re.search(r"\b(1[89]\d\d|20\d\d)\b", words)
        if year:
            updates.append({"field": "date", "value": year.group(1)})
        place = re.search(r"\bin ([A-Z][a-z]+)", words)
        if place:
            updates.append({"field": "place", "value": place.group(1)})
        for sentence in re.split(r"(?<=[.!])\s+", words):
            if re.search(r"\b(ran|won|worked|married|lived|studied)\b", sentence):
                facts.append(sentence.strip())
        return {"updates": updates, "facts": facts}
