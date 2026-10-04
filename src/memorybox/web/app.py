"""The web app: server-rendered pages with htmx for the live bits. No accounts and no cookies
beyond the name of whoever is typing, so the family's corrections say who remembered what.

    /                   the box: every keepsake, and the place to add more
    /items/<id>         one keepsake: the photo, its catalogue card, its story, "tell it what you know"
    /book               the memory book, oldest first, ready to print or read aloud
    /notes              everything MemoryBox has been told, and a delete button on each line
    /about              whose box this is, and how the stories address them
    /export             the whole archive as a zip: the data, the photos, and a memory book that opens offline
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, unquote

from anyio import to_thread
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image, UnidentifiedImageError

from memorybox import memory
from memorybox.config import load_config
from memorybox.photos import prepare
from memorybox.pipeline import Pipeline, Worker
from memorybox.store import Store

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent
MAX_PHOTO_BYTES = 20 * 1024 * 1024
NAME_COOKIE = "mb_name"
WORKING = ("queued", "looking", "writing", "understanding")
STATUS_TEXT = {"queued": "Waiting its turn", "looking": "Looking closely at the photo",
               "writing": "Writing the memory card", "understanding": "Taking in what you said"}
SOURCE_TEXT = {"read": "written on it", "seen": "from the photo", "guess": "a guess"}
FLASH = {"told": "Thank you. Updating the card with what you said.", "edited": "Saved. Rewriting the story to match.",
         "added": "Added to the box. It will be read in a moment.", "side": "Added. Reading it again with the new photo.",
         "note": "Added to what it knows."}


def duration(seconds: float) -> str:
    """'about 3 minutes', '40 seconds': for people, not for logs."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{max(5, round(seconds / 5) * 5)} seconds"
    minutes = round(seconds / 60)
    return "about a minute" if minutes <= 1 else f"about {minutes} minutes"


def since(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        return int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return 0


def create_app(store: Store | None = None, client=None, *, run_now: bool = False, model_name: str | None = None) -> FastAPI:
    store = store or Store()
    pipeline = Pipeline(store, client)
    worker = Worker(pipeline, run_now=run_now)
    app = FastAPI(title="MemoryBox", docs_url=None, redoc_url=None)
    app.state.store, app.state.pipeline, app.state.worker = store, pipeline, worker
    env = Environment(loader=FileSystemLoader(WEB_DIR / "templates"), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    if model_name is None:
        cfg = load_config()
        model_name = f"{cfg.model(cfg.stage('looking').primary).id_for_adapter}, an open model running on this computer"
    env.globals.update(model_name=model_name, LABELS=memory.LABELS, FIELDS=memory.FIELDS, SOURCE_TEXT=SOURCE_TEXT,
                       STATUS_TEXT=STATUS_TEXT, WORKING=WORKING, year_of=memory.year_of, search_text=search_text)

    def progress(item: dict) -> dict:
        """What to tell someone watching this keepsake: what is happening, for how long, what is ahead."""
        typical = worker.typical_seconds()
        ahead = worker.ahead_of(item["id"])
        elapsed = since(item.get("status_at") or item.get("created_at"))
        if item["status"] == "queued" or ahead:
            n = ahead or 0
            wait = f"{n} photo{'s' if n != 1 else ''} ahead of this one" if n else "next in line"
            return {"text": STATUS_TEXT["queued"], "detail": f"{wait}, {duration((n + 1) * typical)} to go",
                    "elapsed": None}
        left = max(typical - elapsed, 15)
        return {"text": STATUS_TEXT[item["status"]], "detail": f"{duration(elapsed)} so far, usually {duration(typical)}"
                if elapsed < typical * 2 else f"{duration(elapsed)} so far; this one is taking longer than usual",
                "elapsed": elapsed, "left": left}
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    def render(name: str, **ctx) -> str:
        profile = store.profile
        return env.get_template(name).render(profile=profile, owner=profile.get("owner") or "", **ctx)

    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        flash = FLASH.get(request.query_params.get("done", ""))
        html = render(name, request=request, me=unquote(request.cookies.get(NAME_COOKIE, "")), flash=flash, **ctx)
        return HTMLResponse(html, status_code=status)

    async def read_upload(up: UploadFile) -> tuple[object | None, str | None]:
        if not up.filename:
            return None, None
        data = await up.read(MAX_PHOTO_BYTES + 1)
        if len(data) > MAX_PHOTO_BYTES:
            return None, f"{up.filename} is over 20 MB"
        try:
            return await to_thread.run_sync(prepare, data, load_config().max_edge), None  # drops EXIF and GPS too
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
            return None, f"{up.filename} is not a photo"

    def remember_name(resp: Response, by: str) -> Response:
        resp.set_cookie(NAME_COOKIE, quote(by), max_age=60 * 60 * 24 * 365 * 5, samesite="lax", httponly=True)
        return resp

    def who(request: Request, by: str) -> str:
        by = " ".join(by.split())[:40]
        return by or unquote(request.cookies.get(NAME_COOKIE, "")) or "the family"

    worker.resume()

    # --- the box ----------------------------------------------------------------------------------

    @app.get("/")
    async def box(request: Request, error: str = ""):
        items = store.items()
        return page(request, "box.html", items=items, working=sum(i["status"] in WORKING for i in items),
                    error=error, first=not store.profile.get("owner") and not items,
                    eta=duration(worker.pending() * worker.typical_seconds()))

    @app.get("/box/status")
    async def box_status(working: int = 0):
        items = store.items()
        now = sum(i["status"] in WORKING for i in items)
        if now != working:  # something finished or was added: show the new tiles
            return Response(status_code=200, headers={"HX-Refresh": "true"})
        eta = duration(worker.pending() * worker.typical_seconds())
        return HTMLResponse(render("_box_progress.html", working=now, eta=eta, done=len(items) - now, total=len(items)))

    @app.post("/items")
    async def add_items(photos: Annotated[list[UploadFile], File()]):
        skipped = []
        added = 0
        for up in photos:
            img, problem = await read_upload(up)
            if problem:
                skipped.append(problem)
            if img is None:
                continue
            item = store.add_item(img.jpeg, width=img.width, height=img.height)
            worker.look(item["id"])
            added += 1
        if not added and not skipped:
            skipped.append("choose at least one photo")
        if skipped:
            return RedirectResponse(f"/?error={quote('Skipped: ' + '; '.join(skipped) + '.')}", status_code=303)
        return RedirectResponse("/?done=added", status_code=303)

    @app.get("/photos/{item_id}.jpg")
    async def photo(item_id: int):
        return await side_photo(item_id, 1)

    @app.get("/photos/{item_id}/{n}.jpg")
    async def side_photo(item_id: int, n: int):
        """Side n of a keepsake: 1 is the main photo, 2 the back or the next page, and so on."""
        item = store.item(item_id)
        paths = store.all_photos(item) if item else []
        if not 1 <= n <= len(paths) or not paths[n - 1].exists():
            raise HTTPException(404)
        # no-cache: the browser keeps the photo but asks before using it, and gets a quick "unchanged"
        # unless the file is new. Keepsake numbers can repeat (the demo box, a new box after a reset),
        # so a photo remembered for a day would show the wrong keepsake.
        return FileResponse(paths[n - 1], media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})

    # --- one keepsake -----------------------------------------------------------------------------

    @app.get("/items/{item_id}")
    async def item_page(request: Request, item_id: int, error: str = ""):
        item = store.item(item_id)
        if item is None:
            raise HTTPException(404)
        timeline = store.timeline()
        ids = [i["id"] for i in timeline]
        at = ids.index(item_id)
        prev_item = timeline[at - 1] if at > 0 else None
        next_item = timeline[at + 1] if at + 1 < len(ids) else None
        return page(request, "item.html", item=item, facts=[f for f in store.facts if f.get("item_id") == item_id],
                    prev_item=prev_item, next_item=next_item, error=error,
                    p=progress(item) if item["status"] in WORKING else None)

    @app.get("/items/{item_id}/status")
    async def item_status(item_id: int):
        item = store.item(item_id)
        if item is None or item["status"] not in WORKING:
            return Response(status_code=200, headers={"HX-Refresh": "true"})
        return HTMLResponse(render("_progress.html", item=item, p=progress(item)))

    @app.post("/items/{item_id}/sides")
    async def add_sides(item_id: int, photos: Annotated[list[UploadFile], File()]):
        if store.item(item_id) is None:
            raise HTTPException(404)
        added, skipped = 0, []
        for up in photos:
            img, problem = await read_upload(up)
            if problem:
                skipped.append(problem)
            if img is not None and store.add_side(item_id, img.jpeg, width=img.width, height=img.height):
                added += 1
        if added:  # read the whole keepsake again; what the family said is kept (memory.set_if_allowed)
            store.set_status(item_id, "queued")
            worker.look(item_id)
        if skipped:
            msg = quote("Skipped: " + "; ".join(skipped) + ".")
            return RedirectResponse(f"/items/{item_id}?error={msg}", status_code=303)
        return RedirectResponse(f"/items/{item_id}?done=side", status_code=303)

    @app.post("/items/{item_id}/sides/{n}/delete")
    async def delete_side(item_id: int, n: int):
        if store.remove_side(item_id, n - 2):
            store.set_status(item_id, "queued")
            worker.look(item_id)
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/tell")
    async def tell(request: Request, item_id: int, words: Annotated[str, Form()] = "", by: Annotated[str, Form()] = ""):
        item = store.item(item_id)
        if item is None:
            raise HTTPException(404)
        name = who(request, by)
        words = words.strip()
        if words:
            store.set_status(item_id, "understanding")
            worker.understand(item_id, words[:2000], name)
        return remember_name(RedirectResponse(f"/items/{item_id}?done=told" if words else f"/items/{item_id}",
                                              status_code=303), name)

    @app.post("/items/{item_id}/edit")
    async def edit(request: Request, item_id: int, by: Annotated[str, Form()] = "",
                   title: Annotated[str, Form()] = "", kind: Annotated[str, Form()] = "",
                   date: Annotated[str, Form()] = "", place: Annotated[str, Form()] = "",
                   people: Annotated[str, Form()] = "", occasion: Annotated[str, Form()] = ""):
        if store.item(item_id) is None:
            raise HTTPException(404)
        name = who(request, by)
        values = {"title": title, "kind": kind, "date": date, "place": place, "people": people, "occasion": occasion}
        changed = await to_thread.run_sync(pipeline.edit, item_id, values, name)
        if changed:
            store.set_status(item_id, "writing")
            worker.write(item_id)
        return remember_name(RedirectResponse(f"/items/{item_id}?done=edited" if changed else f"/items/{item_id}",
                                              status_code=303), name)

    @app.post("/items/{item_id}/retry")
    async def retry(item_id: int):
        if store.item(item_id) is None:
            raise HTTPException(404)
        store.set_status(item_id, "queued")
        worker.look(item_id)
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/rewrite")
    async def rewrite(item_id: int):
        if store.item(item_id) is None:
            raise HTTPException(404)
        store.set_status(item_id, "writing")
        worker.write(item_id)
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/delete")
    async def delete(item_id: int):
        store.delete_item(item_id)
        return RedirectResponse("/", status_code=303)

    # --- the book, the notes, the person ----------------------------------------------------------

    @app.get("/book")
    async def book(request: Request):
        items = [i for i in store.timeline() if i["status"] not in WORKING and i["status"] != "failed"]
        return page(request, "book.html", items=items)

    @app.get("/notes")
    async def notes(request: Request):
        items = {i["id"]: i for i in store.items()}
        return page(request, "notes.html", facts=store.facts[::-1], items=items)

    @app.post("/notes")
    async def add_note(request: Request, text: Annotated[str, Form()] = "", by: Annotated[str, Form()] = ""):
        name = who(request, by)
        added = store.add_fact(text[:500], by=name)
        return remember_name(RedirectResponse("/notes?done=note" if added else "/notes", status_code=303), name)

    @app.post("/notes/{fact_id}/delete")
    async def delete_note(fact_id: int):
        store.delete_fact(fact_id)
        return RedirectResponse("/notes", status_code=303)

    @app.get("/export")
    async def export():
        """The whole archive in one zip: what is in the folder, plus `memory-book.html`, which opens
        in any browser with no app and no internet. This is the copy that goes on the USB stick."""
        items = [i for i in store.timeline() if i["status"] not in WORKING and i["status"] != "failed"]
        book_html = render("book.html", request=None, items=items, me="", flash=None, export=True)

        def build() -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("memory-book.html", book_html)
                z.writestr("memories.json", json.dumps(store.data, ensure_ascii=False, indent=1))
                for it in store.items():
                    for k, path in enumerate(store.all_photos(it), start=1):
                        if path.exists():
                            z.write(path, f"photos/{it['id']}-{k}.jpg" if k > 1 else f"photos/{it['id']}.jpg")
                z.writestr("README.txt", "Open memory-book.html in any web browser to read the memory book.\n"
                                         "memories.json holds every detail, who said it and when.\n"
                                         "To open this archive in MemoryBox again, point MEMORYBOX_HOME at this folder.\n")
            return buf.getvalue()

        data = await to_thread.run_sync(build)
        slug = "".join(c if c.isalnum() else "-" for c in (store.profile.get("owner") or "memorybox")).strip("-")
        name = f"{slug}-memorybox-{datetime.now():%Y-%m-%d}.zip"
        return StreamingResponse(iter([data]), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/about")
    async def about(request: Request, error: str = ""):
        from memorybox.store import archive_dir

        return page(request, "about.html", count=len(store.items()), archive=archive_dir(store.home), error=error)

    @app.post("/about/new-box")
    async def new_box():
        """Put this box away (moved to the archive folder, never deleted) and start an empty one."""
        try:
            await to_thread.run_sync(store.start_new_box)
        except OSError as e:  # Windows will not move a folder while a file in it is open elsewhere
            msg = f"The box could not be moved ({e.strerror or e}). Close any open photos from it and try again."
            return RedirectResponse(f"/about?error={quote(msg)}", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/about")
    async def save_about(owner: Annotated[str, Form()] = "", about: Annotated[str, Form()] = "",
                         voice: Annotated[str, Form()] = "to"):
        store.set_profile(owner=owner[:80], about=about[:3000], voice="about" if voice == "about" else "to")
        return RedirectResponse("/", status_code=303)

    return app


def search_text(item: dict) -> str:
    """Everything a person might type to find a keepsake, for the box page's search."""
    parts = [f.get("value", "") for f in item.get("fields", {}).values()]
    parts += [item.get("inscription", ""), item.get("translation", ""), item.get("story", "")]
    return " ".join(p for p in parts if p).casefold()
