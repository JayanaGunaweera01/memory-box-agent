"""The three things the models do, and the one worker that runs them, one at a time.

- `look`: the photo of one keepsake -> what it is, what is written on it, guesses with a basis,
  questions for the family. Then `write`.
- `write`: the details (each with its source) and the family's notes -> the memory card text.
- `understand`: a family member's words ("No, that was 1997") -> which details change, and what is
  worth remembering beyond this object. Applied by `memory.apply_correction`, then `write` again.

One worker thread, because a laptop runs one model call at a time well and two badly. A box of
twenty photos uploaded at once queues up and is read in order while the family keeps browsing.
"""

from __future__ import annotations

import logging
import queue
import statistics
import threading
from collections.abc import Callable

from memorybox import memory, router
from memorybox.router import ModelClient, StageResult
from memorybox.store import Store

log = logging.getLogger(__name__)

LOOK_PROMPT, WRITE_PROMPT, UNDERSTAND_PROMPT = "look_v1", "write_v1", "understand_v1"


def _log_run(item: dict, step: str, sr: StageResult, version: str) -> None:
    r = sr.result
    item.setdefault("runs", []).append({
        "step": step, "model": r.model, "prompt": version, "latency_ms": r.latency_ms,
        "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "error": r.error,
        "failover_from": sr.failover_from, "at": memory.now_iso()})
    item["runs"] = item["runs"][-20:]


class Pipeline:
    def __init__(self, store: Store, client: ModelClient | None = None) -> None:
        self.store = store
        self.client = client  # None: the adapter named in the config (Ollama)

    # --- the stages ---------------------------------------------------------------------------

    def look(self, item_id: int) -> None:
        item = self.store.item(item_id)
        if item is None:
            return
        self.store.set_status(item_id, "looking")
        version, prompt = router.load_prompt(LOOK_PROMPT)
        context = memory.context_text(self.store.profile, self.store.facts)
        photos = [p.read_bytes() for p in self.store.all_photos(item) if p.exists()]
        jpeg = photos[0] if len(photos) == 1 else photos
        sr = router.vision("looking", f"{prompt}\n\n{context}", jpeg, client=self.client, schema=memory.LOOK_SCHEMA)
        item = self.store.item(item_id)
        if item is None:
            return
        _log_run(item, "look", sr, version)
        if not sr.result.ok or not isinstance(sr.result.parsed, dict):
            item["status"], item["error"] = "failed", sr.result.error or "the model's reply was not an object"
            self.store.put(item)
            return
        memory.apply_look(item, sr.result.parsed)
        self.store.put(item)
        self.write(item_id)

    def write(self, item_id: int) -> None:
        item = self.store.item(item_id)
        if item is None:
            return
        self.store.set_status(item_id, "writing")
        version, prompt = router.load_prompt(WRITE_PROMPT)
        profile = self.store.profile
        text = (f"{memory.context_text(profile, self.store.facts, item)}\n{memory.address_line(profile)}\n\n"
                f"The object:\n{memory.item_text(item)}")
        sr = router.text("writing", prompt, text, client=self.client, schema=memory.STORY_SCHEMA)
        item = self.store.item(item_id)
        if item is None:
            return
        _log_run(item, "write", sr, version)
        story = (sr.result.parsed or {}).get("story") if sr.result.ok and isinstance(sr.result.parsed, dict) else None
        if story:
            item["story"], item["story_error"] = story.strip(), None
        else:  # the details still stand; only the paragraph is missing
            item["story_error"] = sr.result.error or "no story in the reply"
        item["status"], item["error"] = "ready", None
        self.store.put(item)

    def understand(self, item_id: int, words: str, by: str) -> None:
        item = self.store.item(item_id)
        if item is None:
            return
        self.store.set_status(item_id, "understanding")
        version, prompt = router.load_prompt(UNDERSTAND_PROMPT)
        text = f"The object:\n{memory.item_text(item)}\n\nWhat {by} said:\n{words}"
        sr = router.text("writing", prompt, text, client=self.client, schema=memory.UNDERSTAND_SCHEMA)
        item = self.store.item(item_id)
        if item is None:
            return
        _log_run(item, "understand", sr, version)
        parsed = sr.result.parsed if sr.result.ok and isinstance(sr.result.parsed, dict) else None
        entries = memory.apply_correction(item, parsed.get("updates", []) if parsed else [], by, words)
        # The words themselves are always kept, even when the model could not place them, so
        # nothing a person said is ever lost.
        item.setdefault("notes", []).append({"at": memory.now_iso(), "by": by, "words": words,
                                             "understood": bool(entries or (parsed and parsed.get("facts")))})
        self.store.put(item)
        for e in entries:
            self.store.add_fact(memory.fact_for_update(item, e), by=by, item_id=item_id)
        for f in (parsed or {}).get("facts", []):
            if isinstance(f, str):
                self.store.add_fact(f, by=by, item_id=item_id)
        self.write(item_id)

    def edit(self, item_id: int, values: dict[str, str], by: str) -> list[dict]:
        """Direct edits from the form: no model involved in deciding what changed."""
        item = self.store.item(item_id)
        if item is None:
            return []
        updates = [{"field": k, "value": v} for k, v in values.items()
                   if k in memory.FIELDS and v.strip() and v.strip() != (memory.owner(item, k) or {}).get("value")]
        entries = memory.apply_correction(item, updates, by, "edited the details")
        self.store.put(item)
        for e in entries:
            self.store.add_fact(memory.fact_for_update(item, e), by=by, item_id=item_id)
        return entries


class Worker:
    """Runs jobs in order on one background thread. `run_now=True` (tests) runs them inline."""

    def __init__(self, pipeline: Pipeline, *, run_now: bool = False) -> None:
        self.pipeline = pipeline
        self.run_now = run_now
        self.jobs: queue.Queue[tuple[int, Callable[[], None]]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._order_lock = threading.Lock()
        self.waiting: list[int] = []  # item ids in the order they will be worked on
        self.current: int | None = None

    def submit(self, item_id: int, job: Callable[[], None]) -> None:
        if self.run_now:
            self._run(job)
            return
        with self._order_lock:
            self.waiting.append(item_id)
        self.jobs.put((item_id, job))
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="memorybox-worker", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            item_id, job = self.jobs.get()
            with self._order_lock:
                if item_id in self.waiting:
                    self.waiting.remove(item_id)
                self.current = item_id
            try:
                self._run(job)
            finally:
                with self._order_lock:
                    self.current = None
                self.jobs.task_done()

    # --- what the pages say while the family waits -------------------------------------------

    def ahead_of(self, item_id: int) -> int | None:
        """How many jobs will run before this item's, counting the one running now. None if not queued."""
        with self._order_lock:
            if item_id not in self.waiting:
                return None
            return self.waiting.index(item_id) + (1 if self.current is not None else 0)

    def pending(self) -> int:
        with self._order_lock:
            return len(self.waiting) + (1 if self.current is not None else 0)

    def typical_seconds(self) -> int:
        """How long one keepsake usually takes on this computer: the median of recent read-and-write
        times. Ninety seconds until there is anything to go on."""
        per_item = []
        for item in self.pipeline.store.items()[:30]:
            runs = item.get("runs", [])
            looks = [r["latency_ms"] for r in runs if r["step"] == "look" and not r.get("error")]
            writes = [r["latency_ms"] for r in runs if r["step"] == "write" and not r.get("error")]
            if looks and writes:
                per_item.append((looks[-1] + writes[-1]) / 1000)
        return int(statistics.median(per_item)) if per_item else 90

    @staticmethod
    def _run(job: Callable[[], None]) -> None:
        try:
            job()
        except Exception:  # one bad photo must never stop the rest of the box
            log.exception("job failed")

    def look(self, item_id: int) -> None:
        self.submit(item_id, lambda: self.pipeline.look(item_id))

    def write(self, item_id: int) -> None:
        self.submit(item_id, lambda: self.pipeline.write(item_id))

    def understand(self, item_id: int, words: str, by: str) -> None:
        self.submit(item_id, lambda: self.pipeline.understand(item_id, words, by))

    def resume(self) -> None:
        """After a restart, anything that was mid-way is queued again."""
        for item in self.pipeline.store.items()[::-1]:
            if item["status"] in ("queued", "looking"):
                self.look(item["id"])
            elif item["status"] in ("writing", "understanding"):
                self.write(item["id"])
