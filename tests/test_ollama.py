"""The Ollama adapter: what is sent, and how failures are reported."""

import httpx

from memorybox.adapters.ollama import OllamaClient
from memorybox.config import Model
from memorybox.memory import LOOK_SCHEMA

GEMMA = Model(alias="gemma4-e4b", slug="google/gemma-4-e4b", provider="local", reasoning_effort="none",
              model_id="gemma4:e4b", num_ctx=16384)


class Recorder:
    def __init__(self, status=200, payload=None, raise_=None):
        self.status, self.payload, self.raise_, self.calls = status, payload, raise_, []

    def __call__(self, url, *, json, timeout):
        self.calls.append({"url": url, "json": json})
        if self.raise_:
            raise self.raise_
        return httpx.Response(self.status, json=self.payload, request=httpx.Request("POST", url))


def reply(content, done_reason="stop"):
    return {"message": {"role": "assistant", "content": content}, "done": True, "done_reason": done_reason,
            "prompt_eval_count": 900, "eval_count": 120, "created_at": "t"}


def test_vision_sends_the_photo_the_schema_and_no_thinking(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    rec = Recorder(payload=reply('{"kind": "a letter"}'))
    res = OllamaClient(transport=rec).vision(GEMMA, "look", b"jpeg", schema=LOOK_SCHEMA)
    body = rec.calls[0]["json"]
    assert rec.calls[0]["url"] == "http://localhost:11434/api/chat"
    assert body["format"] == LOOK_SCHEMA and body["think"] is False and body["options"]["num_ctx"] == 16384
    assert body["messages"][0]["images"]
    assert res.ok and res.parsed == {"kind": "a letter"} and res.cost_usd == 0.0


def test_failures_say_how_to_fix_them():
    missing = OllamaClient(transport=Recorder(404, {"error": "model not found"})).text(GEMMA, "p", "t")
    assert missing.error.startswith("http 404") and "ollama pull gemma4:e4b" in missing.error
    down = OllamaClient(transport=Recorder(raise_=httpx.ConnectError("no"))).text(GEMMA, "p", "t")
    assert down.error.startswith("transport") and "ollama serve" in down.error
    cut = OllamaClient(transport=Recorder(payload=reply('{"story": "It was', "length"))).text(GEMMA, "p", "t")
    assert cut.truncated and cut.error.startswith("truncated")
