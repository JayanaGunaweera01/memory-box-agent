"""Models and stages, read from config/models.toml (`MEMORYBOX_CONFIG` to use another file).

Each stage names a primary model and a fallback, and each model names the adapter that serves it.
Swapping a model is an edit to the file, not to the code.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"
CONFIG_PATH = REPO_ROOT / "config" / "models.toml"
CONFIG_ENV = "MEMORYBOX_CONFIG"


def config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV, "").strip()
    if not raw:
        return CONFIG_PATH
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


@dataclass(frozen=True)
class Model:
    alias: str
    slug: str
    provider: str
    price_input: float = 0.0  # USD per million tokens; 0 for a local model
    price_output: float = 0.0
    reasoning_effort: str | None = None  # "none" turns thinking off
    adapter: str = "ollama"
    model_id: str | None = None
    num_ctx: int | None = None  # the context window a local runtime should allocate

    @property
    def id_for_adapter(self) -> str:
        return self.model_id or self.slug


@dataclass(frozen=True)
class Stage:
    name: str
    primary: str
    fallback: str | None


@dataclass(frozen=True)
class Config:
    max_edge: int
    models: dict[str, Model]
    stages: dict[str, Stage]

    def model(self, name: str) -> Model:
        if name in self.models:
            return self.models[name]
        for m in self.models.values():
            if name in (m.slug, m.model_id):
                return m
        raise SystemExit(f"Unknown model {name!r}. Known: {', '.join(self.models)}")

    def stage(self, name: str) -> Stage:
        try:
            return self.stages[name]
        except KeyError:
            raise SystemExit(f"Unknown stage {name!r}. Known: {', '.join(self.stages)}") from None


@lru_cache(maxsize=1)
def load_config() -> Config:
    raw = tomllib.loads(config_path().read_text())
    models = {alias: Model(alias=alias, **body) for alias, body in raw["models"].items()}
    stages = {name: Stage(name=name, primary=body["primary"], fallback=body.get("fallback"))
              for name, body in raw.get("stages", {}).items()}
    for st in stages.values():
        for alias in (st.primary, st.fallback):
            if alias is not None and alias not in models:
                raise SystemExit(f"Stage {st.name!r} names unknown model {alias!r}")
    return Config(max_edge=int(raw["settings"]["max_edge"]), models=models, stages=stages)
