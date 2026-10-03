"""The provenance rules: what counts as read, what stays a guess, and that the family always wins."""

from memorybox import memory
from memorybox.fakes import KEEPSAKES


def look(**overrides):
    parsed = {"kind": "a medal", "title": "Athletics medal", "visible_text": "", "translation": "", "observations": [],
              "questions": [], "guesses": {f: {"value": "", "basis": "", "confidence": "low"} for f in memory.GUESSED}}
    for k, v in overrides.items():
        if k in memory.GUESSED:
            parsed["guesses"][k] = {"value": v, "basis": "the ribbon", "confidence": "medium"}
        else:
            parsed[k] = v
    return parsed


def test_a_year_is_read_only_when_it_is_really_in_the_transcription():
    assert memory.grounded("1974", "Kandy, 12th March 1974. My dear Nimal,")
    assert memory.grounded("12 March 1974", "Kandy, 12th March 1974.")
    assert not memory.grounded("1998", "INTER-SCHOOL ATHLETICS 400 M")
    assert memory.grounded("Kandy", "Kandy, 12th March 1974.")
    assert not memory.grounded("Galle", "Kandy, 12th March 1974.")
    assert not memory.grounded("an", "Kandy")  # too short to mean anything


def test_the_models_claims_are_checked_not_trusted():
    item = {"id": 1}
    memory.apply_look(item, look(visible_text="INTER-SCHOOL ATHLETICS 400 M", date="1998", occasion="ATHLETICS 400 M"))
    assert item["fields"]["date"]["source"] == "guess"  # the model's 1998 is not on the medal
    assert item["fields"]["occasion"]["source"] == "read"
    assert item["fields"]["kind"]["source"] == "seen"


def test_a_year_in_the_title_that_is_not_on_the_object_makes_the_title_a_guess():
    item = {"id": 1}
    memory.apply_look(item, look(title="1998 athletics medal", visible_text="400 M"))
    assert item["fields"]["title"]["source"] == "guess"


def test_the_family_wins_and_the_old_value_is_kept():
    item = {"id": 1}
    memory.apply_look(item, look(date="1998", visible_text="ATHLETICS"))
    item["questions"] = ["What year was this race?", "Who gave it to him?"]
    entries = memory.apply_correction(item, [{"field": "date", "value": "1997"}], "Nimal", "No, that was 1997.")
    assert item["fields"]["date"] == {**item["fields"]["date"], "value": "1997", "source": "family", "by": "Nimal"}
    assert entries[0]["old"] == "1998" and entries[0]["old_source"] == "guess"
    assert item["questions"] == ["Who gave it to him?"]  # the answered question goes away
    # Looking again later never overwrites what the family said.
    memory.apply_look(item, look(date="1999", visible_text="ATHLETICS"))
    assert item["fields"]["date"]["value"] == "1997"
    assert memory.fact_for_update(item, entries[0]) == "Athletics medal: when is 1997."


def test_the_model_sees_every_detail_with_its_source():
    item = {"id": 1}
    memory.apply_look(item, KEEPSAKES[1])
    text = memory.item_text(item)
    assert "- When: 12 March 1974 [read]" in text and "- Where: Kandy [read]" in text
    assert "Written on it: Kandy, 12th March 1974. My dear Nimal, [read]" in text


def test_relevant_notes_prefer_ones_that_share_words_with_the_object():
    facts = [{"text": f"Unrelated note number {i}."} for i in range(60)] + [{"text": "Seeya ran the 400 metres."}]
    facts.insert(0, {"text": "The Kandy letters were from his brother Sunil."})
    item = {"id": 1}
    memory.apply_look(item, KEEPSAKES[1])
    chosen = memory.relevant_facts(facts, item, limit=5)
    assert {"text": "The Kandy letters were from his brother Sunil."} in chosen and len(chosen) == 5


def test_year_of_reads_decades_and_dates():
    for value, year in (("1997", 1997), ("12 March 1974", 1974), ("1960s", 1960), ("long ago", None)):
        assert memory.year_of({"fields": {"date": {"value": value}}}) == year


def test_a_new_reading_drops_old_guesses_but_keeps_the_family():
    item = {"id": 1}
    memory.apply_look(item, look(people="a young couple", place="Galle", date="1960s"))
    memory.apply_correction(item, [{"field": "place", "value": "Matara"}], "Nimal", "It was Matara.")
    memory.apply_look(item, look(date="1974"))  # the second look says nothing about who
    assert "people" not in item["fields"]
    assert item["fields"]["place"]["value"] == "Matara" and item["fields"]["date"]["value"] == "1974"
