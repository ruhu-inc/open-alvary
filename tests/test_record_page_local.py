"""The local site for the record page (scripts.record_page_local): the website's real data path over an
invented Rwandan law in three languages and a text with no structure. The browser tests in
tests/browser/specs/record-page.spec.js run against this server."""

from datetime import date
from hashlib import sha256
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api import site
from api.catalogue import reader_text
from scripts import record_page_local


def _client(tmp_path, dist) -> TestClient:
    return TestClient(record_page_local.build_app(tmp_path, dist))


def test_the_invented_law_is_served_in_three_languages_with_the_structure_rwandan_laws_get(tmp_path):
    client = _client(tmp_path, tmp_path / "no-build")
    record = client.get(f"/site/v1/sources/{record_page_local.LAW_ID}").json()
    assert record["has_full_text"] and record["language"] == "rw" and record["jurisdiction_name"] == "Rwanda"
    page = client.get(f"/site/v1/sources/{record_page_local.LAW_ID}/versions").json()
    texts = {version["language"]: version for version in page["items"]}
    assert sorted(texts) == ["en", "fr", "rw"]
    # The record's own language is not the first served: the page has to choose it.
    assert page["items"][0]["language"] != record["language"]
    for language, version in texts.items():
        raw = version["canonical_text"].encode()
        nodes = [node for node in page["structure"] if node["version_id"] == version["id"]]
        kinds = [node["kind"] for node in nodes]
        assert kinds.count("article") == 4 and kinds.count("chapter") == 2, language
        assert kinds.count("division") == 1 and kinds.count("contents") == 1, language
        for node in nodes:
            span = raw[node["start_byte"] : node["end_byte"]]
            assert sha256(span).hexdigest() == node["span_hash"]
            if node["kind"] in ("article", "chapter", "division"):
                # A heading's number and words open its span, as the page's layout expects.
                words = span.decode()
                assert words.startswith(node["label"]) and node["heading"] in words.split("\n\n")[0]
        # Bytes and characters differ in every language: ’ in Kinyarwanda, “ ” in English, é in French.
        assert len(raw) > len(version["canonical_text"]), language
    # The printed table of contents is one node holding its lines: none of them is an article.
    french = [node for node in page["structure"] if node["version_id"] == texts["fr"]["id"]]
    contents = next(node for node in french if node["kind"] == "contents")
    assert contents["heading"] == "TABLE DES MATIÈRES"
    assert all(
        not (contents["start_byte"] <= node["start_byte"] < contents["end_byte"])
        for node in french
        if node["kind"] == "article"
    )


def test_the_record_page_is_served_one_text_at_a_time_and_told_of_the_others(tmp_path):
    client = _client(tmp_path, tmp_path / "no-build")
    law = f"/site/v1/sources/{record_page_local.LAW_ID}"
    whole = client.get(law + "/versions").json()
    by_language = {version["language"]: version for version in whole["items"]}

    def one(query=""):
        answer = client.get(law + "/text" + query)
        assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store", query
        page = answer.json()
        # Whatever is asked, every public text is listed, in the order they are kept, without its words.
        assert page["texts"] == [
            {name: version[name] for name in ("id", "language", "effective_from", "retrieved_at")}
            for version in whole["items"]
        ], query
        (version,) = page["items"]
        # The one text is whole, and the structure is its own and nobody else's.
        assert version == by_language[version["language"]], query
        assert page["structure"] == [n for n in whole["structure"] if n["version_id"] == version["id"]], query
        assert page["structure"] and page["rights"] == whole["rights"] and page["notice"] is None, query
        return version["language"]

    # Unasked: the record's own language, which is not the first kept.
    assert one() == "rw" and whole["items"][0]["language"] == "en"
    assert one("?language=fr") == "fr" and one("?language=FR") == "fr"
    assert one("?version=" + by_language["en"]["id"]) == "en"
    assert one("?language=fr&version=" + by_language["en"]["id"]) == "en"
    # A text the record does not have is not an error: an old link still opens the record's text.
    assert one("?language=pt&version=ver-gone") == "rw"
    for query in ("?lang=fr", "?language=", "?version=a%20b", "?language=" + "f" * 161):
        refused = client.get(law + "/text" + query)
        assert refused.status_code == 400 and refused.json()["code"] == "invalid_input", query


def test_a_law_too_large_in_all_its_languages_together_still_opens_in_each(tmp_path, monkeypatch):
    client = _client(tmp_path, tmp_path / "no-build")
    law = f"/site/v1/sources/{record_page_local.LAW_ID}"
    whole = client.get(law + "/versions")
    each = {language: client.get(f"{law}/text?language={language}") for language in ("en", "fr", "rw")}
    largest = max(len(answer.content) for answer in each.values())
    assert largest < len(whole.content)
    monkeypatch.setattr(site, "MAX_VERSIONS_BYTES", largest)
    refused = client.get(law + "/versions")
    assert refused.status_code == 413 and refused.json()["code"] == "result_too_large"
    for language, before in each.items():
        again = client.get(f"{law}/text?language={language}")
        assert again.status_code == 200 and again.content == before.content, language
    # One text larger than a page may be is still refused.
    monkeypatch.setattr(site, "MAX_VERSIONS_BYTES", largest - 1)
    assert sorted(client.get(f"{law}/text?language={language}").status_code for language in each) == [
        200,
        200,
        413,
    ]


def test_a_record_with_no_public_text_has_an_empty_reading(tmp_path):
    client = _client(tmp_path, tmp_path / "no-build")
    records = client.get("/site/v1/sources?access=metadata").json()["items"]
    assert records, "the local site keeps at least one record without a public text"
    page = client.get(f"/site/v1/sources/{records[0]['id']}/text").json()
    assert page["items"] == [] and page["texts"] == [] and page["structure"] == []
    assert page["notice"] == "No version has passed both rights and content review."
    assert client.get("/site/v1/sources/no-such-record/text").status_code == 404


def test_the_text_opened_first_is_the_one_asked_for_then_the_records_language_then_the_first():
    def text(id, language, **more):
        return SimpleNamespace(id=id, language=language, effective_from=None, prior_version_id=None, **more)

    served = [text("ver_en", "en"), text("ver_fr", "fr"), text("ver_rw", "rw")]
    assert reader_text(served, "rw").id == "ver_rw"
    assert reader_text(served, "sw").id == "ver_en"
    assert reader_text(served).id == "ver_en"
    assert reader_text([], "en") is None
    assert reader_text(served, "rw", language="fr").id == "ver_fr"
    assert reader_text(served, "rw", language="FR").id == "ver_fr"
    assert reader_text(served, "rw", "ver_en", "fr").id == "ver_en"
    # A language or version the record does not have changes nothing.
    assert reader_text(served, "rw", "ver_gone", "pt").id == "ver_rw"

    def dated(id, day, prior=None):
        return SimpleNamespace(id=id, language="en", effective_from=day, prior_version_id=prior)

    # Within a language: the latest text that no later one replaces.
    several = [
        dated("ver_2016", date(2016, 1, 1)),
        dated("ver_2020_misread", date(2020, 6, 1)),
        dated("ver_2020", date(2020, 6, 1), "ver_2020_misread"),
        dated("ver_undated", None),
        text("ver_fr", "fr"),
    ]
    assert reader_text(several, "en").id == "ver_2020"
    assert reader_text(several, "en", "ver_2016").id == "ver_2016"


def test_the_second_law_has_a_long_printed_contents_and_a_table_inside_an_article(tmp_path):
    client = _client(tmp_path, tmp_path / "no-build")
    page = client.get(f"/site/v1/sources/{record_page_local.LONG_ID}/versions").json()
    (version,) = page["items"]
    raw = version["canonical_text"].encode()
    nodes = {node["locator"]: node for node in page["structure"]}
    assert sum(node["kind"] == "article" for node in page["structure"]) == record_page_local.LONG_ARTICLES
    # The printed contents: its heading and one line an article, more than the page shows unfolded.
    listed = raw[nodes["contents"]["start_byte"] : nodes["contents"]["end_byte"]].decode().split("\n\n")
    assert listed[0] == "TABLE OF CONTENTS" and len(listed) == 1 + record_page_local.LONG_ARTICLES
    # The table is a block of article 2 with no node of its own, as rw-law-1 leaves Rwandan tables.
    second = raw[nodes["art_2"]["start_byte"] : nodes["art_2"]["end_byte"]].decode()
    assert "Code\tMeaning\n310\tFiling number\n320\tFiling date" in second.split("\n\n")
    assert not any(node["kind"] == "table" for node in page["structure"])


def test_the_other_text_has_no_structure(tmp_path):
    client = _client(tmp_path, tmp_path / "no-build")
    page = client.get(f"/site/v1/sources/{record_page_local.PLAIN_ID}/versions").json()
    assert [version["language"] for version in page["items"]] == ["en"]
    assert page["structure"] == []


def test_every_address_is_the_built_site_and_a_missing_build_says_so(tmp_path):
    assert _client(tmp_path / "a", tmp_path / "no-build").get("/sources/x").status_code == 503
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>site</title>")
    (dist / "assets" / "main.js").write_text("// built")
    client = _client(tmp_path / "b", dist)
    assert "site" in client.get(f"/sources/{record_page_local.LAW_ID}").text
    assert client.get("/assets/main.js").text == "// built"
    # A path that climbs out of the build is the page, never another file.
    assert "site" in client.get("/assets/%2e%2e/%2e%2e/pyproject.toml").text
