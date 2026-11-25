"""The local site for the record page (scripts.record_page_local): the website's real data path over an
invented Rwandan law in three languages, a longer one and a text with no structure, and the answer the
record page reads from it (``reader-1``, /site/v1/sources/{id}/text). The browser tests in
tests/browser/specs/record-page.spec.js run against this server."""

import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from api import site
from scripts import record_page_local

LAW = f"/site/v1/sources/{record_page_local.LAW_ID}"
FULL = {"id", "kind", "locator", "start_byte", "end_byte", "label", "heading", "cell_states"}


def _client(tmp_path, dist=None) -> TestClient:
    return TestClient(record_page_local.build_app(tmp_path, dist or tmp_path / "no-build"))


def _kept(source_id):
    """The record's texts and their nodes as the corpus keeps them, by language."""
    held = record_page_local.corpus()
    texts = {v.language: v for v in held.versions if v.source_id == source_id}
    return texts, {
        language: [n for n in held.structure if n.version_id == v.id] for language, v in texts.items()
    }


def test_the_invented_law_is_kept_in_three_languages_with_the_structure_rwandan_laws_get(tmp_path):
    record = _client(tmp_path).get(LAW).json()
    assert record["has_full_text"] and record["language"] == "rw" and record["jurisdiction_name"] == "Rwanda"
    texts, structure = _kept(record_page_local.LAW_ID)
    assert sorted(texts) == ["en", "fr", "rw"]
    for language, version in texts.items():
        raw = version.canonical_text.encode()
        kinds = [node.kind for node in structure[language]]
        assert kinds.count("article") == 4 and kinds.count("chapter") == 2, language
        assert kinds.count("division") == 1 and kinds.count("contents") == 1, language
        for node in structure[language]:
            span = raw[node.start_byte : node.end_byte]
            assert sha256(span).hexdigest() == node.span_hash
            if node.kind in ("article", "chapter", "division"):
                # A heading's number and words open its span, as the page's layout expects.
                words = span.decode()
                assert words.startswith(node.label) and node.heading in words.split("\n\n")[0]
        # Bytes and characters differ in every language: ’ in Kinyarwanda, “ ” in English, é in French.
        assert len(raw) > len(version.canonical_text), language
    # The printed table of contents is one node holding its lines: none of them is an article.
    contents = next(node for node in structure["fr"] if node.kind == "contents")
    assert contents.heading == "TABLE DES MATIÈRES"
    assert all(
        not (contents.start_byte <= node.start_byte < contents.end_byte)
        for node in structure["fr"]
        if node.kind == "article"
    )


def test_the_record_page_is_sent_one_text_as_reader_1_and_told_of_the_others(tmp_path):
    client = _client(tmp_path)
    texts, structure = _kept(record_page_local.LAW_ID)

    def one(query=""):
        answer = client.get(LAW + "/text" + query)
        assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store", query
        page = answer.json()
        assert page["schema"] == "reader-1" and page["modes"] == ["reading"] and page["notice"] is None
        assert "rights" not in page and "items" not in page  # the page reads the rights record elsewhere
        # Whatever is asked, every public text is listed without its words: by language, then latest first.
        assert [(t["language"], t["id"]) for t in page["texts"]] == [(k, texts[k].id) for k in sorted(texts)]
        assert all(set(t) == {"id", "language", "effective_from", "retrieved_at"} for t in page["texts"])
        kept = texts[page["text"]["language"]]
        assert page["canonical_text"] == kept.canonical_text
        assert page["text"] == {
            "id": kept.id,
            "language": kept.language,
            "content_hash": kept.content_hash,
            "effective_from": None,
            "retrieved_at": page["text"]["retrieved_at"],
            "bytes": len(kept.canonical_text.encode()),
        }
        # Its nodes are its own, in document order, each with only what the page uses.
        nodes = page["nodes"]
        assert [n["id"] for n in nodes] == [
            n.id for n in sorted(structure[kept.language], key=lambda n: (n.start_byte, -n.end_byte, n.order))
        ]
        assert all(set(n) <= FULL and "parent_id" not in n and "span_hash" not in n for n in nodes)
        assert [n["start_byte"] for n in nodes] == sorted(n["start_byte"] for n in nodes)
        return page["text"]["language"], page["matched"], page["missed"]

    # Unasked: the record's own language, which is not the first by code.
    assert one() == ("rw", "default", [])
    assert one("?language=fr") == ("fr", "language", []) == one("?language=FR")
    assert one("?version=" + texts["en"].id) == ("en", "version", [])
    # A version that is found settles it: the language sent with it is not looked at.
    assert one("?language=fr&version=" + texts["en"].id) == ("en", "version", [])
    assert one("?language=pt&version=" + texts["en"].id) == ("en", "version", [])
    # What the record does not have is not an error: the answer says what was missed.
    assert one("?language=pt") == ("rw", "default", ["language"])
    assert one("?version=ver-gone") == ("rw", "default", ["version"])
    assert one("?language=pt&version=ver-gone") == ("rw", "default", ["version", "language"])
    assert one("?language=fr&version=ver-gone") == ("fr", "language", ["version"])
    for query in ("?lang=fr", "?language=", "?version=a%20b", "?language=" + "f" * 161, "?part=1"):
        refused = client.get(LAW + "/text" + query)
        assert refused.status_code == 400 and refused.json()["code"] == "invalid_input", query


def test_a_law_in_three_languages_is_bounded_one_text_at_a_time(tmp_path, monkeypatch):
    client = _client(tmp_path)
    each = {language: client.get(f"{LAW}/text?language={language}") for language in ("en", "fr", "rw")}
    sizes = sorted(len(answer.content) for answer in each.values())
    # The bound sits between the largest text and all three together: each still opens.
    monkeypatch.setattr(site, "MAX_TEXT_BYTES", sizes[-1])
    for language, before in each.items():
        again = client.get(f"{LAW}/text?language={language}")
        assert again.status_code == 200 and again.content == before.content, language
    # One text larger than an answer may be is refused, with a code the page reads.
    monkeypatch.setattr(site, "MAX_TEXT_BYTES", sizes[-1] - 1)
    refused = [client.get(f"{LAW}/text?language={language}") for language in each]
    assert sorted(answer.status_code for answer in refused) == [200, 200, 413]
    large = next(answer for answer in refused if answer.status_code == 413)
    assert large.json()["code"] == "result_too_large"
    assert large.headers["content-type"].startswith("application/problem+json")


def test_choosing_a_text_reads_no_other_text_and_the_check_before_sending_reads_none(tmp_path):
    store = record_page_local.build_store(tmp_path)
    client = TestClient(site.create_site_app(store, tmp_path))
    texts, _ = _kept(record_page_local.LAW_ID)
    read = []

    @event.listens_for(store.engine, "before_cursor_execute")
    def note(connection, cursor, statement, parameters, context, many):
        read.append((statement, json.dumps(parameters, default=str)))

    law = f"/sources/{record_page_local.LAW_ID}"
    assert client.get(law + "/text?language=fr").json()["text"]["id"] == texts["fr"].id
    # Whole stored texts are read by ID. Only the chosen one is, and once: choosing is done from the
    # texts' details, and the check before sending asks for the gate alone.
    whole = [
        parameters
        for statement, parameters in read
        if " ".join(statement.split()).startswith("SELECT versions.payload FROM versions")
    ]
    assert len(whole) == 1 and texts["fr"].id in whole[0]
    assert not any(texts[other].id in parameters for _, parameters in read for other in ("en", "rw"))


class Counted:
    def __init__(self):
        self.read = []

    def text_read(self, scope, source_id):
        self.read.append(source_id)

    def record_opened(self, *args):
        pass


def test_every_text_served_is_a_text_read_and_nothing_else_is(tmp_path, monkeypatch):
    traffic = Counted()
    store = record_page_local.build_store(tmp_path)
    client = TestClient(site.create_site_app(store, tmp_path, traffic=traffic))
    law = f"/sources/{record_page_local.LAW_ID}"
    french = client.get(law + "/text?language=fr").json()["text"]["id"]
    assert traffic.read == [record_page_local.LAW_ID]
    # A switch of language asks for another text, and that is another text read.
    assert client.get(f"{law}/text?version={french}").status_code == 200
    assert client.get(law + "/text").status_code == 200
    assert traffic.read == [record_page_local.LAW_ID] * 3
    # A record with no public text has none to read; nor has a refusal sent one.
    closed = client.get("/sources?access=metadata").json()["items"][0]["id"]
    assert client.get(f"/sources/{closed}/text").json()["text"] is None
    assert client.get("/sources/no-such-record/text").status_code == 404
    monkeypatch.setattr(site, "MAX_TEXT_BYTES", 10)
    assert client.get(law + "/text").status_code == 413
    assert len(traffic.read) == 3


def test_a_record_withdrawn_while_its_text_is_being_sent_is_refused_with_a_code_and_not_counted(
    tmp_path, monkeypatch
):
    from api.catalogue import LegacyCatalogue

    traffic = Counted()
    store = record_page_local.build_store(tmp_path)
    client = TestClient(site.create_site_app(store, tmp_path, traffic=traffic))
    monkeypatch.setattr(LegacyCatalogue, "still_served", lambda *args, **more: False)
    changed = client.get(LAW.removeprefix("/site/v1") + "/text")
    assert changed.status_code == 503 and changed.json()["code"] == "temporarily_unavailable"
    assert changed.json()["retryable"] is True and "changed" in changed.json()["detail"]
    assert changed.headers["content-type"].startswith("application/problem+json")
    assert "canonical_text" not in changed.text and traffic.read == []


def test_the_check_before_sending_asks_for_the_record_the_text_and_its_hash(tmp_path):
    from api.catalogue import LegacyCatalogue, SourceUnavailable
    from api.repositories import LegacyRepository

    store = record_page_local.build_store(tmp_path)
    texts, _ = _kept(record_page_local.LAW_ID)
    french = texts["fr"]
    with store.engine.begin() as connection:
        service = LegacyCatalogue(LegacyRepository(connection))
        law = record_page_local.LAW_ID
        assert service.still_served(law, french.id, french.content_hash)
        assert service.still_served(law)  # an answer with no text: the record must still be public
        assert not service.still_served(law, french.id, texts["en"].content_hash)  # read under another hash
        assert not service.still_served(law, "ver-gone", french.content_hash)
        # Another record's text is not this record's.
        other, _ = _kept(record_page_local.LONG_ID)
        assert not service.still_served(law, other["en"].id, other["en"].content_hash)
        with pytest.raises(SourceUnavailable):
            service.still_served("no-such-record")


def test_a_record_with_no_public_text_has_an_empty_reading(tmp_path):
    client = _client(tmp_path)
    records = client.get("/site/v1/sources?access=metadata").json()["items"]
    assert records, "the local site keeps at least one record without a public text"
    page = client.get(f"/site/v1/sources/{records[0]['id']}/text").json()
    assert page == {
        "schema": "reader-1",
        "modes": ["reading"],
        "text": None,
        "texts": [],
        "matched": None,
        "missed": None,
        "nodes": [],
        "notice": "No version has passed both rights and content review.",
    }
    # Asking for a language changes nothing: there is no line to show for a text that is not there.
    assert client.get(f"/site/v1/sources/{records[0]['id']}/text?language=fr").json() == page
    missing = client.get("/site/v1/sources/no-such-record/text")
    assert missing.status_code == 404 and missing.json()["code"] == "unavailable"


def test_the_second_law_has_a_long_printed_contents_and_a_table_inside_an_article(tmp_path):
    page = _client(tmp_path).get(f"/site/v1/sources/{record_page_local.LONG_ID}/text").json()
    raw = page["canonical_text"].encode()
    nodes = {node["locator"]: node for node in page["nodes"]}
    assert sum(node["kind"] == "article" for node in page["nodes"]) == record_page_local.LONG_ARTICLES
    # The printed contents: its heading and one line an article, more than the page shows unfolded.
    listed = raw[nodes["contents"]["start_byte"] : nodes["contents"]["end_byte"]].decode().split("\n\n")
    assert listed[0] == "TABLE OF CONTENTS" and len(listed) == 1 + record_page_local.LONG_ARTICLES
    # The table is a block of article 2 with no node of its own, as rw-law-1 leaves Rwandan tables.
    second = raw[nodes["art_2"]["start_byte"] : nodes["art_2"]["end_byte"]].decode()
    assert "Code\tMeaning\n310\tFiling number\n320\tFiling date" in second.split("\n\n")
    assert not any(node["kind"] == "table" for node in page["nodes"])


def test_the_other_text_has_no_structure(tmp_path):
    page = _client(tmp_path).get(f"/site/v1/sources/{record_page_local.PLAIN_ID}/text").json()
    assert [text["language"] for text in page["texts"]] == ["en"] and page["text"]["language"] == "en"
    assert page["nodes"] == [] and page["canonical_text"]


def test_every_address_is_the_built_site_and_a_missing_build_says_so(tmp_path):
    assert _client(tmp_path / "a").get("/sources/x").status_code == 503
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>site</title>")
    (dist / "assets" / "main.js").write_text("// built")
    client = _client(tmp_path / "b", dist)
    assert "site" in client.get(f"/sources/{record_page_local.LAW_ID}").text
    assert client.get("/assets/main.js").text == "// built"
    # A path that climbs out of the build is the page, never another file.
    assert "site" in client.get("/assets/%2e%2e/%2e%2e/pyproject.toml").text


@pytest.mark.parametrize(
    "blocked, status, code",
    [(("ip:a",), 429, "rate_limited"), (("global:bytes",), 503, "capacity_exhausted")],
)
def test_a_byte_budget_that_is_used_up_refuses_with_a_code_and_counts_no_text(
    tmp_path, monkeypatch, blocked, status, code
):
    from rights.bytes import Reservation
    from tests.test_emission import SCOPES

    class Spent:
        def reserve(self, admission_id, nbytes, scopes, lease):
            return Reservation(False, blocked=blocked, retry_after=9)

    def charged(payload, recheck, **more):
        budget = {"ledger": Spent(), "scopes": SCOPES, "lease_seconds": 60, "charged": True}
        return CheckedResponse(payload, recheck, **{**more, **budget})

    from api.emission import CheckedResponse

    traffic = Counted()
    monkeypatch.setattr(site, "CheckedResponse", charged)
    client = TestClient(
        site.create_site_app(record_page_local.build_store(tmp_path), tmp_path, traffic=traffic)
    )
    refused = client.get(f"/sources/{record_page_local.LAW_ID}/text")
    assert refused.status_code == status and refused.headers["Retry-After"] == "9"
    assert refused.headers["content-type"].startswith("application/problem+json")
    body = refused.json()
    assert body["code"] == code and body["retryable"] is True and body["title"] != "Request refused"
    assert "canonical_text" not in refused.text and traffic.read == []
