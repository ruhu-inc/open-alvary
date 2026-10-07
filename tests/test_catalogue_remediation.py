"""Public page/filter completeness, Unicode parity and immutable same-day releases."""

from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from exports.release import write_release


def expanded(corpus, count=125):
    corpus = corpus.model_copy(deep=True)
    template, rights = corpus.sources[0], corpus.rights[0]
    for i in range(count):
        key = f"fixture-{i:05d}"
        corpus.sources.append(
            template.model_copy(
                update={
                    "id": key,
                    "title": f"Cataloguebatch {i:05d}",
                    "citation": f"Original synthetic citation {i}",
                }
            )
        )
        corpus.rights.append(
            rights.model_copy(update={"id": "r-" + key, "source_id": key, "approved_content_hashes": []})
        )
    return corpus


def test_all_sources_and_filtered_search_pages_are_reachable_without_duplicates(catalogue, store, tmp_path):
    corpus = expanded(catalogue)
    store.save(corpus)
    client = TestClient(create_app(store, tmp_path))
    expected = sorted(s.id for s in corpus.sources)
    ids = []
    for offset in range(0, len(expected), 25):
        page = client.get("/sources", params={"limit": 25, "offset": offset, "access": "metadata"}).json()
        assert page["total"] == len(expected)
        ids.extend(s["id"] for s in page["items"])
    assert ids == expected and len(ids) == len(set(ids))
    ids = []
    for offset in range(0, 125, 25):
        page = client.get(
            "/search", params={"q": "cataloguebatch", "limit": 25, "offset": offset, "access": "metadata"}
        ).json()
        assert page["total"] == 125
        ids.extend(s["id"] for s in page["items"])
    assert ids == [f"fixture-{i:05d}" for i in range(125)]
    assert client.get("/search?q=absent-term").json()["total"] == 0
    assert client.get("/search?q=cataloguebatch&access=invalid").status_code == 422


def test_access_filter_is_applied_before_total_and_window(cleared, store, tmp_path):
    corpus = expanded(cleared)
    store.save(corpus)
    client = TestClient(create_app(store, tmp_path))
    page = client.get("/sources?access=open&limit=1").json()
    assert page["total"] == 1 and page["items"][0]["id"] == cleared.versions[0].source_id
    metadata = client.get("/sources?access=metadata&limit=25&offset=125").json()
    assert metadata["total"] == len(corpus.sources) - 1
    assert all(not s["has_full_text"] for s in metadata["items"])
    assert client.get("/search?q=Section%201.%20Test%20only.&access=metadata").json()["total"] == 0


@pytest.mark.parametrize(
    "title,query",
    [
        ("Straße", "STRASSE"),
        ("ＦＯＯ  bar", "foo bar"),
        ("Café", "Cafe\u0301"),
        ("100%_literal", "%_"),
        ("No\t\n amount", "no amount"),
    ],
)
def test_metadata_normalization_and_wildcards_keep_exact_semantics(catalogue, store, tmp_path, title, query):
    catalogue.sources[0].title = title
    store.save(catalogue)
    page = TestClient(create_app(store, tmp_path)).get("/search", params={"q": query}).json()
    assert page["total"] == 1 and page["items"][0]["id"] == catalogue.sources[0].id


def test_confirmed_metadata_query_does_not_decode_the_whole_catalogue(
    catalogue, store, tmp_path, monkeypatch
):
    from api.repositories import LegacyRepository

    store.save(expanded(catalogue, 1000))
    original = LegacyRepository._pair
    decoded = []

    def pair(row):
        decoded.append(row.source["id"])
        return original(row)

    monkeypatch.setattr(LegacyRepository, "_pair", staticmethod(pair))
    page = TestClient(create_app(store, tmp_path)).get("/search?q=cataloguebatch&limit=25&offset=100").json()
    assert page["total"] == 1000 and len(decoded) == 25


def test_two_same_day_releases_and_legacy_links_keep_bytes_and_current_withdrawal(catalogue, store, tmp_path):
    store.save(catalogue)
    old = write_release(catalogue, tmp_path, "2026-10-07", "ng", True)
    first = write_release(catalogue, tmp_path, "2026-10-07.1", "ng", True)
    digest = sha256((first / "manifest.json").read_bytes()).hexdigest()
    second = write_release(catalogue, tmp_path, "2026-10-07.2", "ng", True)
    assert second != first and (old / "manifest.json").is_file()
    with pytest.raises(FileExistsError):
        write_release(catalogue, tmp_path, "2026-10-07.1", "ng", True)
    assert sha256((first / "manifest.json").read_bytes()).hexdigest() == digest
    client = TestClient(create_app(store, tmp_path))
    for day in ["2026-10-07", "2026-10-07.1", "2026-10-07.2"]:
        assert client.get(f"/releases/{day}/ng/metadata/manifest.json").status_code == 200
    source = catalogue.sources[0]
    catalogue.rights[0].metadata_public = False
    store.save(catalogue)
    assert client.get("/sources/" + source.id).status_code == 404
    assert all(s["id"] != source.id for s in client.get("/sources").json()["items"])
    assert all(
        s["id"] != source.id for s in client.get("/search", params={"q": source.title}).json()["items"]
    )
    assert client.get("/coverage").json()["catalogued_sources"] == 3
    assert client.get("/releases").json() == []
    for day in ["2026-10-07", "2026-10-07.1", "2026-10-07.2"]:
        assert client.get(f"/releases/{day}/ng/metadata/manifest.json").status_code == 410


@pytest.mark.parametrize(
    "value", ["2026-02-30.1", "2026-10-07.0", "2026-10-07.01", "2026-10-07.1/../../", "2026-10-07.1000000000"]
)
def test_release_revision_rejects_bad_dates_and_paths(catalogue, tmp_path, value):
    with pytest.raises(ValueError):
        write_release(catalogue, tmp_path, value, "ng", True)
