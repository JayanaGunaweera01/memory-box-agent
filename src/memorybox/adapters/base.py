"""What every model call returns, and how a JSON reply is pulled out of a model's text."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

# A ceiling on how much a model may write. Thinking (when a model does it) counts against this too,
# so it is generous: running out halfway through the JSON is worse than a long wait.
DEFAULT_MAX_TOKENS = 8192


@dataclass(frozen=True)
class CallResult:
    """One model call, success or failure, in a shape that does not depend on who served it."""

    model: str
    provider: str | None
    raw_text: str | None
    parsed: Any  # the decoded JSON reply; None if the call failed or the reply was not JSON
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    error: str | None
    finish_reason: str | None = None  # "stop" when the model finished, "length" when it was cut off
    reasoning_tokens: int | None = None
    request_id: str | None = None
    adapter: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


def extract_json(text: str) -> Any:
    """The first JSON object in `text`. Small models sometimes wrap it in ```json fences or add a
    sentence before it; both are skipped. Raises json.JSONDecodeError when there is no object."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text, start)
            return value
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    raise json.JSONDecodeError("no JSON object in the reply", text, 0)


def decode_reply(raw_text: str | None, finish_reason: str | None, max_tokens: int,
                 reasoning_tokens: int | None) -> tuple[Any, str | None]:
    """(parsed, error). A reply that was cut off is reported as cut off, not as bad JSON, because
    the fix is different: a bigger budget or less thinking, not a better prompt."""
    try:
        return extract_json(raw_text or ""), None
    except json.JSONDecodeError as e:
        if finish_reason == "length":
            return None, f"truncated: the reply hit max_tokens={max_tokens} (thinking tokens: {reasoning_tokens})"
        return None, f"json parse: {e}"


def failed_call(model_id: str, adapter: str, started: float, error: str) -> CallResult:
    elapsed = int((time.perf_counter() - started) * 1000)
    return CallResult(model_id, None, None, None, None, None, None, elapsed, error, adapter=adapter)
