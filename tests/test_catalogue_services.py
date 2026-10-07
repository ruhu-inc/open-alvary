"""Pilot wire compatibility, query bounds and publication-before-body regression tests."""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, text

from api.catalogue import LegacyCatalogue
from api.main import create_app
from api.repositories import LegacyRepository
from exports.release import coverage
from rights.policy import decide, public_corpus
from schema.models import RightsRecord


def source_view(corpus, source):
    has_text = any(v.source_id == source.id for v in corpus.versions)
    record = next(r for r in corpus.rights if r.source_id == source.id)
    return {
        **source.model_dump(mode="json"),
        "access_mode": "open_full_text" if has_text else "metadata_link_only",
        "rights_status": record.status.value,
        "has_full_text": has_text,
    }


@pytest.mark.parametrize("restricted", [False, True])
def test_scoped_services_preserve_pilot_wire_contract(cleared, store, tmp_path, restricted):
    if restricted:
        cleared.rights[1].status = "RESTRICTED"
    store.save(cleared)
    expected = public_corpus(cleared)
    client = TestClient(create_app(store, tmp_path))
    assert client.get("/sources").json() == {
        "total": len(expected.sources),
        "items": [source_view(expected, s) for s in sorted(expected.sources, key=lambda s: s.id)],
    }
    assert client.get("/coverage").json() == coverage(expected)
    assert client.get("/jurisdictions").json() == [j.model_dump(mode="json") for j in expected.jurisdictions]
    for source in expected.sources:
        record = next(r for r in expected.rights if r.source_id == source.id)
        versions = [v for v in expected.versions if v.source_id == source.id]
        assert client.get(f"/sources/{source.id}").json() == source_view(expected, source)
        assert client.get(f"/rights/{source.id}").json() == {
            "record": record.model_dump(mode="json"),
            "decision": decide(record, versions[0] if versions else None).model_dump(mode="json"),
        }
        assert client.get(f"/sources/{source.id}/versions").json() == {
            "items": [v.model_dump(mode="json") for v in versions],
            "rights": record.model_dump(mode="json"),
            "structure": [n.model_dump(mode="json") for n in expected.structure if n.source_id == source.id],
            "notice": None if versions else "No version has passed both rights and content review.",
        }
        assert client.get("/citations/" + quote(source.citation)).json()["matches"] == [
            source_view(expected, source)
        ]


def test_regular_reads_do_not_load_a_whole_corpus(cleared, store, tmp_path, monkeypatch):
    store.save(cleared)

    def forbidden():
        raise AssertionError("Whole-corpus loading is forbidden on catalogue paths")

    monkeypatch.setattr(store, "load", forbidden)
    client = TestClient(create_app(store, tmp_path))
    source_id = cleared.sources[0].id
    for path in (
        "/sources",
        "/jurisdictions",
        "/coverage",
        "/search?q=synthetic",
        f"/sources/{source_id}",
        f"/sources/{source_id}/versions",
        f"/rights/{source_id}",
        "/citations/" + quote(cleared.sources[0].citation),
    ):
        assert client.get(path).status_code == 200


def test_private_unapproved_bodies_are_never_selected(cleared, store, tmp_path):
    cleared.rights[0].status = "RIGHTS_UNDER_REVIEW"
    store.save(cleared)
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        client = TestClient(create_app(store, tmp_path))
        for path in (
            "/sources",
            "/coverage",
            "/search?q=Synthetic",
            f"/sources/{cleared.sources[0].id}/versions",
        ):
            assert client.get(path).status_code == 200
        assert not any("SELECT versions.payload \n" in statement for statement in statements)
        assert not any("canonical_text" in statement for statement in statements)
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)


def test_permission_filter_precedes_pagination_and_search_crosses_batches(catalogue, store, tmp_path):
    source = catalogue.sources[0]
    catalogue = catalogue.model_copy(update={"sources": [], "rights": [], "citations": []})
    for index in range(230):
        key = f"synthetic-{index:04d}"
        catalogue.sources.append(
            source.model_copy(
                update={"id": key, "title": f"Synthetic searchable {index}", "citation": f"Fixture {index}"}
            )
        )
        catalogue.rights.append(
            RightsRecord(
                id="r-" + key,
                source_id=key,
                status="RESTRICTED" if index % 3 == 0 else "LINK_ONLY",
                metadata_public=True,
                metadata_basis="Original synthetic metadata",
            )
        )
    store.save(catalogue)
    client = TestClient(create_app(store, tmp_path))
    visible = sorted(s.id for s in catalogue.sources if int(s.id[-4:]) % 3)
    seen = []
    for offset in range(0, len(visible), 50):
        response = client.get("/sources", params={"limit": 50, "offset": offset}).json()
        assert response["total"] == len(visible)
        seen.extend(s["id"] for s in response["items"])
    assert seen == visible
    search = client.get("/search", params={"q": "searchable", "limit": 25, "offset": 125}).json()
    assert search["total"] == len(visible)
    assert [s["id"] for s in search["items"]] == visible[125:150]
    assert client.get("/sources", params={"jurisdiction": ""}).json() == {"total": 0, "items": []}


def test_public_dtos_are_deeply_immutable(cleared, store):
    store.save(cleared)
    with store.engine.begin() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        page = service.sources()
        versions = service.versions(cleared.sources[0].id)
        counts = service.coverage()
    with pytest.raises(ValidationError):
        page.items[0].title = "mutated"
    with pytest.raises(AttributeError):
        versions.rights.approved_content_hashes.append("a" * 64)
    with pytest.raises(ValidationError):
        counts.jurisdictions[0].count = 99


def test_corrupt_authority_returns_safe_503_without_fallback(catalogue, store, tmp_path):
    store.save(catalogue)
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE rights SET payload=json_set(payload,'$.status','private-corrupt-status')"))
    client = TestClient(create_app(store, tmp_path))
    response = client.get("/sources")
    assert response.status_code == 503
    assert response.json() == {"detail": "Catalogue authority unavailable"}
    assert "private-corrupt-status" not in response.text


def test_byte_and_unpaged_limits_never_return_partial_text(cleared, store, tmp_path, monkeypatch):
    store.save(cleared)
    monkeypatch.setattr("api.repositories.MAX_BUFFER_BYTES", 1)
    response = TestClient(create_app(store, tmp_path)).get(f"/sources/{cleared.sources[0].id}/versions")
    assert response.status_code == 413
    assert cleared.versions[0].canonical_text not in response.text


def test_corrupt_node_ownership_fails_closed_in_scoped_version_read(cleared, store, tmp_path):
    store.save(cleared)
    wrong = cleared.sources[1].id
    with store.engine.begin() as conn:
        conn.execute(
            text("UPDATE structure SET source_id=:source,payload=json_set(payload,'$.source_id',:source)"),
            {"source": wrong},
        )
    response = TestClient(create_app(store, tmp_path)).get(f"/sources/{cleared.sources[0].id}/versions")
    assert response.status_code == 503
    assert cleared.versions[0].canonical_text not in response.text


def test_private_node_alias_without_version_cannot_resolve(cleared, store, tmp_path):
    store.save(cleared)
    with store.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE citations SET payload=json_set(payload,'$.node_id','test-node') WHERE source_id=:id"
            ),
            {"id": cleared.sources[0].id},
        )
    response = TestClient(create_app(store, tmp_path)).get("/citations/" + quote(cleared.sources[0].citation))
    assert response.status_code == 503
