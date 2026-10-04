"""Which adapter serves a model, which model serves a stage, and one failover to a smaller model.

Two operations, `vision` and `text`. The pipeline calls these and never imports an adapter, so a
test passes its own client and the config decides everything else.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Protocol

from memorybox.adapters.base import DEFAULT_MAX_TOKENS, CallResult
from memorybox.config import PROMPTS_DIR, Model, load_config

ADAPTERS: dict[str, str] = {
    "ollama": "memorybox.adapters.ollama:OllamaClient",
}

Progress = Callable[[str], None]


class ModelClient(Protocol):
    def vision(self, model: Model, prompt: str, image_jpeg: bytes | list[bytes], *, max_tokens: int = DEFAULT_MAX_TOKENS,
               on_progress: Progress | None = None, schema: dict | None = None) -> CallResult: ...

    def text(self, model: Model, prompt: str, input_text: str, *, max_tokens: int = DEFAULT_MAX_TOKENS,
             on_progress: Progress | None = None, schema: dict | None = None) -> CallResult: ...


def load_prompt(name: str) -> tuple[str, str]:
    """(version, text) for prompts/<name>.md. The version is the filename, logged on every item."""
    path = PROMPTS_DIR / f"{name}.md"
    return path.name, path.read_text()


@cache
def client_for(adapter: str) -> ModelClient:
    try:
        target = ADAPTERS[adapter]
    except KeyError:
        raise SystemExit(f"Unknown adapter {adapter!r}. Known: {', '.join(ADAPTERS)}") from None
    module_name, class_name = target.split(":")
    return getattr(importlib.import_module(module_name), class_name)()


FAILOVER_ERROR_PREFIXES = ("http ", "transport", "no choices")


def should_fail_over(res: CallResult) -> bool:
    """A provider-side failure (the model is not pulled, Ollama ran out of memory, a cut-off reply).
    A reply the model gave and got wrong is not retried elsewhere."""
    if res.ok:
        return False
    return res.truncated or (res.error or "").startswith(FAILOVER_ERROR_PREFIXES)


@dataclass(frozen=True)
class StageResult:
    result: CallResult
    model: Model
    failover_from: str | None = None
    failover_error: str | None = None


def run(stage_name: str, call: Callable[[Model], CallResult], on_progress: Progress | None = None) -> StageResult:
    cfg = load_config()
    st = cfg.stage(stage_name)
    primary = cfg.model(st.primary)
    res = call(primary)
    if res.ok or st.fallback is None or not should_fail_over(res):
        return StageResult(res, primary)
    fallback = cfg.model(st.fallback)
    if on_progress:
        on_progress(f"{primary.alias} failed, trying {fallback.alias}")
    return StageResult(call(fallback), fallback, primary.slug, res.error)


def vision(stage_name: str, prompt: str, jpeg: bytes | list[bytes], *, client: ModelClient | None = None,
           schema: dict | None = None, on_progress: Progress | None = None) -> StageResult:
    return run(stage_name, lambda m: (client or client_for(m.adapter)).vision(
        m, prompt, jpeg, schema=schema, on_progress=on_progress), on_progress)


def text(stage_name: str, prompt: str, input_text: str, *, client: ModelClient | None = None,
         schema: dict | None = None, on_progress: Progress | None = None) -> StageResult:
    return run(stage_name, lambda m: (client or client_for(m.adapter)).text(
        m, prompt, input_text, schema=schema, on_progress=on_progress), on_progress)
