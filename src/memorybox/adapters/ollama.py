"""Ollama adapter: open-weight models (Gemma 4 by default) running on the family's own computer.

One POST to Ollama's native `/api/chat` via httpx. No SDK, no key, no account. The photo of a
letter or a medal is sent to `localhost` (or `OLLAMA_HOST`, a desktop on the same Wi-Fi) and never
leaves the house.

Why the native endpoint and not Ollama's OpenAI-compatible one:
- `format` takes a JSON Schema, so the reply shapes are enforced by constrained
  decoding, not just asked for in the prompt. Small models need that more than big ones.
- `think` switches a thinking model's reasoning off. A model that thinks until it runs out of
  output budget never writes the JSON, and on a laptop the thinking is most of the wait.
- `options.num_ctx`: Ollama's default context is small, and the family's memory notes plus the
  prompt overflow it silently (the head of the prompt is dropped). Each model sets its own.

Cost is always 0.0: the inference runs on hardware the user already owns.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from memorybox.adapters.base import DEFAULT_MAX_TOKENS, CallResult, decode_reply, failed_call
from memorybox.config import Model

NAME = "ollama"
DEFAULT_HOST = "http://localhost:11434"
HOST_ENV = "OLLAMA_HOST"
# A laptop CPU transcribing a handwritten letter with a 4B model can take a couple of minutes; the
# work runs in the background and the page polls, so a generous ceiling is better than a false failure.
TIMEOUT_S = float(os.environ.get("MEMORYBOX_OLLAMA_TIMEOUT_S", "600"))


def host() -> str:
    """Where Ollama listens. `OLLAMA_HOST` may be a bare `host:port`, as Ollama itself accepts."""
    raw = os.environ.get(HOST_ENV, "").strip() or DEFAULT_HOST
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    if raw.startswith("http://0.0.0.0"):  # the server's bind address, not a place to connect to
        raw = raw.replace("0.0.0.0", "localhost", 1)
    return raw.rstrip("/")


def _finish_reason(done_reason: str | None) -> str | None:
    if done_reason in ("length", "limit"):
        return "length"
    if done_reason in ("stop", None, ""):
        return "stop" if done_reason is not None else None
    return done_reason


class OllamaClient:
    def __init__(self, transport: Callable[..., httpx.Response] | None = None) -> None:
        self._post = transport or httpx.post  # tests pass a stub

    def vision(self, model: Model, prompt: str, image_jpeg: bytes | list[bytes], *, max_tokens: int = DEFAULT_MAX_TOKENS,
               on_progress: Callable[[str], None] | None = None, schema: dict | None = None) -> CallResult:
        """One photo, or several photos of the same thing (front and back) in a single message."""
        images = [image_jpeg] if isinstance(image_jpeg, bytes) else list(image_jpeg)
        b64 = [base64.b64encode(i).decode("ascii") for i in images]
        return self._call(model, {"role": "user", "content": prompt, "images": b64}, max_tokens, schema, on_progress)

    def text(self, model: Model, prompt: str, input_text: str, *, max_tokens: int = DEFAULT_MAX_TOKENS,
             on_progress: Callable[[str], None] | None = None, schema: dict | None = None) -> CallResult:
        return self._call(model, {"role": "user", "content": f"{prompt}\n\n{input_text}"}, max_tokens, schema,
                          on_progress)

    def body(self, model: Model, message: dict[str, Any], max_tokens: int, schema: dict | None) -> dict[str, Any]:
        """The request body; separate so the tests can check exactly what is sent."""
        options: dict[str, Any] = {"num_predict": max_tokens, "temperature": 0}
        if model.num_ctx:
            options["num_ctx"] = model.num_ctx
        body: dict[str, Any] = {
            "model": model.id_for_adapter,
            "messages": [message],
            "stream": False,
            "options": options,
            "format": schema if schema is not None else "json",
        }
        # `think` only when the config says so: a model without a thinking mode rejects `think: true`.
        if model.reasoning_effort == "none":
            body["think"] = False
        elif model.reasoning_effort:
            body["think"] = model.reasoning_effort if model.reasoning_effort in ("low", "medium", "high") else True
        return body

    def _call(self, model: Model, message: dict[str, Any], max_tokens: int, schema: dict | None,
              on_progress: Callable[[str], None] | None) -> CallResult:
        model_id = model.id_for_adapter
        body = self.body(model, message, max_tokens, schema)
        if on_progress:
            on_progress(f"{model_id} on this computer")
        started = time.perf_counter()
        try:
            resp = self._post(f"{host()}/api/chat", json=body, timeout=TIMEOUT_S)
        except httpx.HTTPError as e:
            return failed_call(model_id, NAME, started,
                          f"transport: cannot reach Ollama at {host()} ({e!r}). Is `ollama serve` running?")
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            data = resp.json()
        except ValueError:
            return failed_call(model_id, NAME, started, f"http {resp.status_code}: non-JSON body {resp.text[:200]!r}")
        if resp.status_code != 200 or "error" in data:
            err = data.get("error", data)
            hint = f" (try: ollama pull {model_id})" if resp.status_code == 404 else ""
            return failed_call(model_id, NAME, started, f"http {resp.status_code}: {json.dumps(err)[:400]}{hint}")

        msg = data.get("message") or {}
        raw_text = msg.get("content")
        finish_reason = _finish_reason(data.get("done_reason"))
        thinking = msg.get("thinking")
        # Ollama does not count thinking tokens separately; a rough word count is enough to see
        # in the logs that a model spent its budget thinking (001 D9).
        reasoning_tokens = len(thinking.split()) if isinstance(thinking, str) and thinking else None
        if raw_text is None:
            return CallResult(model_id, "local", None, None, data.get("prompt_eval_count"), data.get("eval_count"),
                              0.0, latency_ms, f"no choices in response: {json.dumps(data)[:400]}",
                              finish_reason=finish_reason, adapter=NAME)
        parsed, error = decode_reply(raw_text, finish_reason, max_tokens, reasoning_tokens)
        return CallResult(
            model=model_id, provider="local", raw_text=raw_text, parsed=parsed,
            input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"),
            cost_usd=0.0, latency_ms=latency_ms, error=error, finish_reason=finish_reason,
            reasoning_tokens=reasoning_tokens, request_id=data.get("created_at"), adapter=NAME,
        )


def installed_models(timeout_s: float = 3.0) -> list[str] | None:
    """The model tags Ollama has pulled, or None when Ollama is not reachable. Used by the launcher."""
    try:
        resp = httpx.get(f"{host()}/api/tags", timeout=timeout_s)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return None
