"""What the record page cannot show of a catalogue (operations.record_page): measured with the site's
own code, so a size reported by publishing is the size /site/v1/sources/{id}/text would send."""

from fastapi.testclient import TestClient

from api import catalogue, site
from operations import record_page
from scripts import record_page_local

LAW = record_page_local.LAW_ID


def _site(tmp_path):
    store = record_page_local.build_store(tmp_path)
    return store, TestClient(site.create_site_app(store, tmp_path))


def _asks(text):
    """Every way of asking for a record's text, with what the record does not have among them."""
    for version in (None, text.id, "ver_gone"):
        for language in (None, text.language, "zz"):
            asked = {"version": version, "language": language}
            yield {name: value for name, value in asked.items() if value is not None}


def _measured(store, work):
    held = store.load()
    source = next(s for s in held.sources if s.id == work)
    texts = sorted((v for v in held.versions if v.source_id == work), key=lambda v: v.id)
    nodes = {v.id: [n for n in held.structure if n.version_id == v.id] for v in texts}
    return {v.id: (v, record_page.largest_answer(v, texts, nodes[v.id], source.language)) for v in texts}


def test_a_texts_measure_is_the_largest_answer_the_site_sends_holding_it(tmp_path):
    store, client = _site(tmp_path)
    for text, measured in _measured(store, LAW).values():
        sent = []
        for asked in _asks(text):
            answer = client.get(f"/sources/{LAW}/text", params=asked)
            assert answer.status_code == 200
            if answer.json()["text"]["id"] == text.id:
                sent.append(len(answer.content))
        # Asked for by its ID alone the answer misses nothing; a link that misses is longer.
        assert measured == max(sent) > min(sent), text.language


def test_a_text_is_named_from_the_first_byte_over_the_bound_and_the_site_refuses_it_there(
    tmp_path, monkeypatch
):
    store, client = _site(tmp_path)
    measured = _measured(store, LAW) | _measured(store, record_page_local.LONG_ID)
    largest, size = max(measured.values(), key=lambda found: found[1])
    work = largest.source_id
    assert record_page.unshown(store.load(), "rw") == []
    monkeypatch.setattr(site, "MAX_TEXT_BYTES", size)  # just under: at the bound, as the site allows
    assert record_page.unshown(store.load(), "rw") == []
    monkeypatch.setattr(site, "MAX_TEXT_BYTES", size - 1)  # just over
    (found,) = record_page.unshown(store.load(), "rw")
    assert (found.work, found.language, found.version, found.size) == (
        work,
        largest.language,
        largest.id,
        size,
    )
    assert found.why == f"its answer is {size:,} bytes; the page reads at most {size - 1:,}"
    refused = [client.get(f"/sources/{work}/text", params=asked) for asked in _asks(largest)]
    assert 413 in {answer.status_code for answer in refused}
    # Every other text still opens, and is not named.
    for text, _ in measured.values():
        if text.id != largest.id:
            opened = client.get(f"/sources/{text.source_id}/text", params={"version": text.id})
            assert opened.status_code == 200


def test_a_text_with_more_nodes_than_one_answer_holds_is_named_as_the_site_refuses_it(tmp_path, monkeypatch):
    store, client = _site(tmp_path)
    held = store.load()
    works = {v.id: v.source_id for v in held.versions if v.source_id in (LAW, record_page_local.LONG_ID)}
    counts = {text: sum(n.version_id == text for n in held.structure) for text in works}
    most = max(counts.values())
    monkeypatch.setattr(catalogue, "MAX_STRUCTURE_ROWS", most)  # just under: every text opens
    assert record_page.unshown(held, "rw") == []
    monkeypatch.setattr(catalogue, "MAX_STRUCTURE_ROWS", most - 1)  # just over
    found = record_page.unshown(held, "rw")
    assert {u.version for u in found} == {text for text, count in counts.items() if count == most}
    for unshown in found:
        assert unshown.work == works[unshown.version] and unshown.size is None and unshown.nodes == most
        assert unshown.why == f"its structure has {most:,} nodes; one answer holds at most {most - 1:,}"
        refused = client.get(f"/sources/{unshown.work}/text", params={"version": unshown.version})
        assert refused.status_code == 413 and refused.json()["code"] == "result_too_large"
    for text in set(works) - {u.version for u in found}:
        assert client.get(f"/sources/{works[text]}/text", params={"version": text}).status_code == 200
    # Another country's texts are not this country's to name.
    assert record_page.unshown(held, "ke") == []
