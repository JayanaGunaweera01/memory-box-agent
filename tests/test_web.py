"""The whole app through HTTP, with the fake model: add, read, correct, remember, the book, restart."""

import json

from fastapi.testclient import TestClient

from memorybox.fakes import FakeClient
from memorybox.store import Store
from memorybox.web.app import create_app
from tests.photos import jpeg, keepsake


def make(tmp_path, client=None):
    store = Store(tmp_path)
    fake = client or FakeClient()
    return TestClient(create_app(store, fake, run_now=True, model_name="test")), store, fake


def photo(n=1):
    return [("photos", (f"k{i}.jpg", jpeg(keepsake(), gps=True), "image/jpeg")) for i in range(n)]


def test_first_visit_asks_whose_box_it_is(tmp_path):
    web, store, _ = make(tmp_path)
    assert "Whose keepsakes are these?" in web.get("/").text
    web.post("/about", data={"owner": "Seeya", "about": "Ran the 400 metres for Richmond College in 1997.", "voice": "to"})
    assert store.profile["owner"] == "Seeya" and "Seeya's box" in web.get("/").text


def test_add_read_correct_and_remember(tmp_path):
    web, store, fake = make(tmp_path)
    web.post("/about", data={"owner": "Seeya", "voice": "to"})
    assert web.post("/items", files=photo(), follow_redirects=False).status_code == 303
    item = store.item(1)
    assert item["status"] == "ready"
    # the photo on disk has no GPS left in it
    from memorybox.photos import has_metadata
    assert not has_metadata((tmp_path / "photos" / "1.jpg").read_bytes())
    assert item["fields"]["date"]["source"] == "guess" and item["fields"]["date"]["value"] == "1998"
    page = web.get("/items/1").text
    assert 'class="v v-guess">1998?' in page and "It is wondering" in page
    assert "appears to be from 1998" in item["story"]
    assert "Address: write to Seeya directly" in fake.prompts[-1]

    # "No, that was 1997" -> a family detail, a history line, a memory note, and a new story
    res = web.post("/items/1/tell", data={"words": "No, that was 1997. He ran the 400 metres and came second.",
                                          "by": "Nimal"}, follow_redirects=False)
    assert res.status_code == 303 and "mb_name" in res.headers["set-cookie"]
    item = store.item(1)
    assert item["fields"]["date"]["source"] == "family" and item["fields"]["date"]["value"] == "1997"
    assert item["history"][0]["old"] == "1998" and item["history"][0]["by"] == "Nimal"
    assert "It is from 1997." in item["story"]
    facts = [f["text"] for f in store.facts]
    assert "Athletics medal: when is 1997." in facts and any("ran the 400 metres" in f for f in facts)
    page = web.get("/items/1").text
    assert "from Nimal" in page and 'value="Nimal"' in page  # the name is remembered for next time

    # the next keepsake is read with what Nimal said
    web.post("/items", files=photo())
    assert "Athletics medal: when is 1997." in fake.prompts[-2]  # the look prompt for item 2


def test_words_it_cannot_place_are_kept_not_lost(tmp_path):
    web, store, _ = make(tmp_path)
    web.post("/items", files=photo())
    web.post("/items/1/tell", data={"words": "Aththamma always kept this in the almirah.", "by": "Nimal"})
    item = store.item(1)
    assert item["notes"][-1] == {**item["notes"][-1], "words": "Aththamma always kept this in the almirah.", "understood": False}
    assert "Kept as said" in web.get("/items/1").text


def test_direct_edits_need_no_model(tmp_path):
    web, store, fake = make(tmp_path)
    web.post("/items", files=photo())
    calls = len(fake.prompts)
    web.post("/items/1/edit", data={"place": "Galle", "by": "Aththamma", "title": "Athletics medal"})
    item = store.item(1)
    assert item["fields"]["place"] == {**item["fields"]["place"], "value": "Galle", "source": "family", "by": "Aththamma"}
    assert len(item["history"]) == 1  # the unchanged title is not a change
    assert len(fake.prompts) == calls + 1  # only the story was rewritten


def test_a_failed_read_can_be_retried(tmp_path):
    web, store, fake = make(tmp_path, FakeClient(fail_vision="transport: cannot reach Ollama"))
    web.post("/items", files=photo())
    assert store.item(1)["status"] == "failed" and "cannot reach Ollama" in web.get("/items/1").text
    fake.fail_vision = None
    web.post("/items/1/retry")
    assert store.item(1)["status"] == "ready"


def test_the_book_is_in_date_order_and_notes_can_be_removed(tmp_path):
    web, store, _ = make(tmp_path)
    web.post("/items", files=photo(3))  # medal 1998?, letter 1974, photo 1960s?
    book = web.get("/book").text
    assert book.index("1960s") < book.index("1970s") < book.index("1990s")
    web.post("/notes", data={"text": "Seeya married Aththamma in 1966.", "by": "Nimal"})
    fact = store.facts[-1]
    assert "Seeya married Aththamma in 1966." in web.get("/notes").text
    web.post(f"/notes/{fact['id']}/delete")
    assert "Seeya married Aththamma" not in web.get("/notes").text


def test_everything_survives_a_restart_and_deleting_removes_the_photo(tmp_path):
    web, store, _ = make(tmp_path)
    web.post("/items", files=photo())
    web.post("/items/1/tell", data={"words": "That was 1997.", "by": "Nimal"})
    again = Store(tmp_path)
    assert again.item(1)["fields"]["date"]["value"] == "1997" and again.facts
    assert json.loads((tmp_path / "memories.json").read_text())["version"] == 1
    web.post("/items/1/delete")
    assert not (tmp_path / "photos" / "1.jpg").exists() and not Store(tmp_path).facts


def test_bad_uploads_are_named(tmp_path):
    web, _, _ = make(tmp_path)
    res = web.post("/items", files=[("photos", ("notes.txt", b"hello", "text/plain"))])
    assert "notes.txt is not a photo" in web.get(res.headers["location"]).text if res.status_code == 303 else res.text


def test_the_test_photo_really_carries_gps_before_upload():
    from memorybox.photos import has_metadata, prepare
    tagged = jpeg(keepsake(100, 80), gps=True)
    assert has_metadata(tagged) and not has_metadata(prepare(tagged, 1600).jpeg)


def test_a_new_box_starts_empty_and_keeps_the_old_one(tmp_path):
    home = tmp_path / ".memorybox"
    web, store, _ = make(home)
    web.post("/about", data={"owner": "Seeya", "voice": "to"})
    web.post("/items", files=photo(2))
    web.post("/items/1/tell", data={"words": "That was 1997.", "by": "Nimal"})
    assert "Put this box away" in web.get("/about").text

    web.post("/about/new-box")
    assert store.items() == [] and store.facts == [] and store.profile == {}
    assert "Whose keepsakes are these?" in web.get("/").text  # a fresh start
    # nothing was deleted: the old box, photos and all, is in the archive folder
    (archived,) = (tmp_path / ".memorybox-archive").iterdir()
    assert archived.name.startswith("Seeya-")
    old = json.loads((archived / "memories.json").read_text())
    assert old["items"]["1"]["fields"]["date"]["value"] == "1997" and (archived / "photos" / "1.jpg").exists()
    # new keepsakes never reuse an old number, so a late model reply cannot land on the wrong one
    web.post("/items", files=photo())
    assert [i["id"] for i in store.items()] == [3]
    assert Store(home).items()[0]["id"] == 3  # and it all survives a restart


def test_an_empty_box_is_not_archived(tmp_path):
    store = Store(tmp_path / ".memorybox")
    assert store.start_new_box() is None and not (tmp_path / ".memorybox-archive").exists()


def test_saving_survives_windows_briefly_locking_the_file(tmp_path, monkeypatch):
    """Antivirus or the search indexer can hold memories.json for a moment; a save must retry, not fail."""
    from pathlib import Path

    from memorybox import store as store_module

    real_replace, failures = Path.replace, {"left": 3}

    def flaky_replace(self, target):
        if failures["left"]:
            failures["left"] -= 1
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real_replace(self, target)

    monkeypatch.setattr(store_module.Path, "replace", flaky_replace)
    web, store, _ = make(tmp_path)
    web.post("/items", files=photo(3))
    failures["left"] = 3
    assert web.post("/items/3/edit", data={"date": "1999", "by": "Jayana"}, follow_redirects=False).status_code == 303
    assert [f["text"] for f in Store(tmp_path).facts] == ["Studio portrait of a young couple: when is 1999."]


def test_the_back_of_a_photo_is_read_together_with_the_front(tmp_path):
    web, store, fake = make(tmp_path)
    web.post("/items", files=photo())
    assert fake.image_counts[-1] == 1
    web.post("/items/1/edit", data={"date": "1997", "by": "Nimal"})  # the family's word must survive a re-read
    back = [("photos", ("back.jpg", jpeg(keepsake()), "image/jpeg"))]
    res = web.post("/items/1/sides", files=back, follow_redirects=False)
    assert res.headers["location"] == "/items/1?done=side"
    assert fake.image_counts[-1] == 2  # front and back in one message
    item = store.item(1)
    assert len(item["sides"]) == 1 and item["fields"]["date"]["value"] == "1997"
    assert web.get("/photos/1/2.jpg").status_code == 200 and web.get("/photos/1/3.jpg").status_code == 404
    page = web.get("/items/1").text
    assert "Back" in page and "Remove last photo" in page and "2 photos" in web.get("/").text
    web.post("/items/1/sides/2/delete")
    assert store.item(1)["sides"] == [] and not (tmp_path / "photos" / "1-2.jpg").exists()


def test_waiting_says_how_many_are_ahead_and_roughly_how_long(tmp_path):
    from memorybox.web.app import duration

    store = Store(tmp_path)
    app = create_app(store, FakeClient(), model_name="t")  # a real queue, not run inline
    worker = app.state.worker
    worker.jobs.put = lambda job: None  # hold the queue still so the test can look at it
    worker._thread = type("Alive", (), {"is_alive": lambda self: True})()
    web = TestClient(app)
    web.post("/items", files=photo(3))
    worker.current, worker.waiting = 1, [2, 3]
    fragment = web.get("/items/3/status").text
    assert "Waiting its turn" in fragment and "2 photos ahead of this one" in fragment and "minutes to go" in fragment
    assert "Reading 3 keepsakes" in web.get("/").text
    assert duration(30) == "30 seconds" and duration(70) == "about a minute" and duration(200) == "about 3 minutes"


def test_take_a_copy_is_a_zip_with_an_offline_memory_book(tmp_path):
    import io
    import zipfile

    web, store, _ = make(tmp_path)
    web.post("/about", data={"owner": "Appachchi", "voice": "about"})
    web.post("/items", files=photo(2))
    web.post("/items/1/sides", files=[("photos", ("back.jpg", jpeg(keepsake()), "image/jpeg"))])
    res = web.get("/export")
    assert res.headers["content-type"] == "application/zip" and "Appachchi-memorybox-" in res.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(res.content))
    names = set(z.namelist())
    assert {"memory-book.html", "memories.json", "README.txt", "photos/1.jpg", "photos/1-2.jpg", "photos/2.jpg"} <= names
    book = z.read("memory-book.html").decode()
    assert 'src="photos/1.jpg"' in book and 'src="photos/1-2.jpg"' in book
    assert "/static/" not in book and 'href="/items' not in book  # nothing that needs the app to be running
    assert "Appachchi" in book


def test_saving_shows_a_confirmation(tmp_path):
    web, store, _ = make(tmp_path)
    web.post("/items", files=photo())
    res = web.post("/items/1/tell", data={"words": "That was 1997.", "by": "Nimal"})
    assert "Thank you. Updating the card" in res.text
    assert "Added to what it knows." in web.post("/notes", data={"text": "Seeya married Aththamma in 1966.", "by": "N"}).text


def test_the_box_can_be_searched(tmp_path):
    web, store, _ = make(tmp_path)
    web.post("/items", files=photo(2))
    page = web.get("/").text
    assert 'id="find"' in page and 'data-search="' in page and "kandy" in page  # the letter's place, lower-cased


def test_a_new_photo_with_a_reused_number_is_never_shown_from_the_browser_cache(tmp_path):
    web, store, _ = make(tmp_path)
    web.post("/items", files=photo())
    first = web.get("/photos/1.jpg")
    assert first.headers["cache-control"] == "private, no-cache" and first.headers.get("etag")
    # same number, different photo (the demo box, or a box started again): the browser must get the new one
    store.delete_item(1)
    store.data["next_item"] = 1
    web.post("/items", files=[("photos", ("other.jpg", jpeg(keepsake(500, 900)), "image/jpeg"))])
    again = web.get("/photos/1.jpg", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 200 and again.content != first.content
