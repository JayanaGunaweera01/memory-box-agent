"""The family's private memory database: one folder on this computer.

    ~/.memorybox/          (or MEMORYBOX_HOME)
        memories.json      every object, every detail and its source, every correction, the notes
        photos/<id>.jpg    the photos, metadata stripped

`memories.json` is meant to be readable in a text editor: a family should be able to see, without
the app, everything it knows. Copy the folder to a USB stick and the archive goes with it. Delete the
folder and it is gone. No server ever holds a copy.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from memorybox.memory import now_iso, year_of

HOME_ENV = "MEMORYBOX_HOME"
VERSION = 1


def home_dir() -> Path:
    raw = os.environ.get(HOME_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".memorybox"


def archive_dir(home: Path) -> Path:
    """Where finished boxes go: beside the live one, so they are easy to find and copy."""
    return home.with_name(home.name + "-archive")


def _atomic_write(path: Path, text: str, attempts: int = 12) -> None:
    """Write via a temporary file and a rename, so a crash never leaves half a file.

    On Windows the rename fails with PermissionError for a moment whenever antivirus or the search
    indexer has the file open, which happens often for a file that changes every few seconds. So
    the rename is retried for up to about two seconds before giving up."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(attempts):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.03 * (attempt + 1))


class Store:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home or home_dir()
        self.photos = self.home / "photos"
        self.photos.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "memories.json"
        self._lock = threading.RLock()
        self.data = {"version": VERSION, "profile": {}, "items": {}, "facts": [], "next_item": 1, "next_fact": 1}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except ValueError:
                backup = self.path.with_suffix(".unreadable.json")
                self.path.replace(backup)  # never silently lose an archive
        # JSON keys are strings; ids are ints everywhere else.
        self.data["items"] = {int(k): v for k, v in self.data["items"].items()}

    def is_empty(self) -> bool:
        with self._lock:
            return not (self.data["items"] or self.data["facts"] or self.data["profile"])

    def start_new_box(self) -> Path | None:
        """Put the current box away and start an empty one. Nothing is deleted: the whole folder
        (memories.json and the photos) moves to `<home>-archive/<owner>-<date>/`, where it can be
        opened again by pointing MEMORYBOX_HOME at it. Returns where it went (None if it was empty).

        Item ids keep counting up across boxes, so a model call still running for an old keepsake
        can never land on a new one with the same number."""
        with self._lock:
            if self.is_empty():
                return None
            owner = re.sub(r"[^\w-]+", "-", self.data["profile"].get("owner", "") or "box").strip("-")[:40] or "box"
            dest = archive_dir(self.home) / f"{owner}-{datetime.now():%Y-%m-%d-%H%M%S}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.home), str(dest))
            self.photos.mkdir(parents=True, exist_ok=True)
            self.data = {"version": VERSION, "profile": {}, "items": {}, "facts": [],
                         "next_item": self.data["next_item"], "next_fact": self.data["next_fact"]}
            self.save()
            return dest

    def save(self) -> None:
        with self._lock:
            _atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    # --- the person the box belongs to -----------------------------------------------------------

    @property
    def profile(self) -> dict:
        return dict(self.data["profile"])

    def set_profile(self, **values: str) -> None:
        with self._lock:
            self.data["profile"].update({k: v.strip() for k, v in values.items() if v is not None})
            self.save()

    # --- objects ----------------------------------------------------------------------------------

    def add_item(self, jpeg: bytes, *, width: int, height: int) -> dict:
        with self._lock:
            item_id = self.data["next_item"]
            self.data["next_item"] += 1
            name = f"{item_id}.jpg"
            (self.photos / name).write_bytes(jpeg)
            item = {"id": item_id, "created_at": now_iso(), "photo": f"photos/{name}", "width": width,
                    "height": height, "sides": [], "status": "queued", "error": None, "fields": {}, "inscription": "",
                    "translation": "", "observations": [], "questions": [], "story": "", "history": [],
                    "notes": [], "runs": []}
            self.data["items"][item_id] = item
            self.save()
            return copy.deepcopy(item)

    def add_side(self, item_id: int, jpeg: bytes, *, width: int, height: int) -> dict | None:
        """Another photo of the same keepsake: the back of a photograph, page two of a letter."""
        with self._lock:
            it = self.data["items"].get(item_id)
            if it is None:
                return None
            sides = it.setdefault("sides", [])
            name = f"{item_id}-{len(sides) + 2}.jpg"
            (self.photos / name).write_bytes(jpeg)
            sides.append({"photo": f"photos/{name}", "width": width, "height": height})
            self.save()
            return copy.deepcopy(it)

    def remove_side(self, item_id: int, index: int) -> bool:
        with self._lock:
            it = self.data["items"].get(item_id)
            sides = it.get("sides", []) if it else []
            if not 0 <= index < len(sides):
                return False
            (self.home / sides.pop(index)["photo"]).unlink(missing_ok=True)
            self.save()
            return True

    def all_photos(self, item: dict) -> list[Path]:
        """Every photo of a keepsake, the main one first."""
        return [self.home / item["photo"]] + [self.home / s["photo"] for s in item.get("sides", [])]

    def item(self, item_id: int) -> dict | None:
        with self._lock:
            it = self.data["items"].get(item_id)
            return copy.deepcopy(it) if it else None

    def put(self, item: dict) -> None:
        with self._lock:
            if item["id"] in self.data["items"]:  # deleted while a job ran: let it stay deleted
                self.data["items"][item["id"]] = item
                self.save()

    def set_status(self, item_id: int, status: str, error: str | None = None) -> None:
        with self._lock:
            it = self.data["items"].get(item_id)
            if it:
                if it.get("status") != status:  # when this step began, for the live timer
                    it["status_at"] = now_iso()
                it["status"], it["error"] = status, error
                self.save()

    def items(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(v) for _, v in sorted(self.data["items"].items(), reverse=True)]

    def timeline(self) -> list[dict]:
        """Dated objects oldest first, then the undated ones in the order they were added."""
        all_items = sorted(self.items(), key=lambda i: i["id"])
        dated = sorted((i for i in all_items if year_of(i)), key=lambda i: year_of(i))
        return dated + [i for i in all_items if not year_of(i)]

    def delete_item(self, item_id: int) -> None:
        with self._lock:
            it = self.data["items"].pop(item_id, None)
            if it:
                for path in self.all_photos(it):
                    path.unlink(missing_ok=True)
                self.data["facts"] = [f for f in self.data["facts"] if f.get("item_id") != item_id]
                self.save()

    def photo_path(self, item: dict) -> Path:
        return self.home / item["photo"]

    # --- the memory notes ---------------------------------------------------------------------------

    @property
    def facts(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self.data["facts"])

    def add_fact(self, text: str, *, by: str, item_id: int | None = None) -> dict | None:
        text = " ".join(text.split())
        if not text:
            return None
        with self._lock:
            if any(f["text"].casefold() == text.casefold() for f in self.data["facts"]):
                return None
            fact = {"id": self.data["next_fact"], "text": text, "by": by, "item_id": item_id, "at": now_iso()}
            self.data["next_fact"] += 1
            self.data["facts"].append(fact)
            self.save()
            return fact

    def delete_fact(self, fact_id: int) -> None:
        with self._lock:
            self.data["facts"] = [f for f in self.data["facts"] if f["id"] != fact_id]
            self.save()
