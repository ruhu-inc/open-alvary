"""The local site for the record page (scripts.record_page_local): the website's real data path over an
invented Rwandan law in three languages and a text with no structure. The browser tests in
tests/browser/specs/record-page.spec.js run against this server."""

from hashlib import sha256

from fastapi.testclient import TestClient

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
