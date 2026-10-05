import json
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.main import create_app
from exports.release import write_release
from rights.policy import decide, public_corpus
from schema.models import Citation, Corpus


def test_seed_has_no_cleared_text(catalogue):
    public = public_corpus(catalogue)
    assert len(public.sources) == 4
    assert public.versions == []
    assert all(r.status == "RIGHTS_UNDER_REVIEW" for r in public.rights)
    assert next(s for s in public.sources if s.id == "ng-isa-2007").legal_status == "repealed"


def test_cleared_exact_version_is_published(cleared):
    assert decide(cleared.rights[0], cleared.versions[0]).full_text
    assert len(public_corpus(cleared).versions) == 1
    assert not public_corpus(cleared, metadata_only=True).versions


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "RIGHTS_UNDER_REVIEW"),
        ("status", "LINK_ONLY"),
        ("status", "RESTRICTED"),
        ("reviewer", None),
        ("reviewed_at", None),
        ("evidence", []),
        ("licence", None),
        ("redistribution_allowed", "unknown"),
        ("redistribution_allowed", "no"),
        ("commercial_reuse_allowed", "unknown"),
        ("derivatives_allowed", "no"),
        ("privacy_status", "pending"),
        ("privacy_status", "blocked"),
        ("attribution_text", None),
        ("approved_content_hashes", []),
        ("metadata_public", False),
        ("metadata_basis", None),
        ("database_rights_notes", None),
        ("legal_note", None),
        ("expires_at", datetime(2020, 1, 1, tzinfo=UTC)),
        ("reviewed_at", datetime(2099, 1, 1, tzinfo=UTC)),
    ],
)
def test_fail_closed(field, value, cleared):
    setattr(cleared.rights[0], field, value)
    assert not public_corpus(cleared).versions
    assert not public_corpus(cleared).structure


def test_conditions_must_be_recorded_and_satisfied(cleared):
    r = cleared.rights[0]
    r.status = "OPEN_WITH_CONDITIONS"
    assert not decide(r, cleared.versions[0]).full_text
    r.conditions = ["Include attribution in every export"]
    assert not decide(r, cleared.versions[0]).full_text
    r.conditions_satisfied = True
    assert decide(r, cleared.versions[0]).full_text


def test_content_review_is_independent(cleared):
    cleared.versions[0].verification_status = "pending"
    assert not public_corpus(cleared).versions


def test_unapproved_secret_not_searchable_or_downloadable(cleared, store, tmp_path):
    cleared.rights[0].status = "RIGHTS_UNDER_REVIEW"
    store.save(cleared)
    client = TestClient(create_app(store, tmp_path))
    source_id = cleared.sources[0].id
    assert client.get("/search", params={"q": "Synthetic fixture"}).json()["total"] == 0
    response = client.get(f"/sources/{source_id}/versions")
    assert response.json()["items"] == []
    for path in [
        "/sources",
        f"/sources/{source_id}",
        f"/rights/{source_id}",
        "/coverage",
        "/citations/Constitution%20of%20Nigeria%201999",
    ]:
        assert "Synthetic fixture — not legislation" not in client.get(path).text
    release = write_release(cleared, tmp_path, "2026-10-05", "ng")
    assert (release / "versions.jsonl").read_text() == ""
    assert (release / "structure.jsonl").read_text() == ""


def test_restricted_records_absent_from_every_public_surface(cleared, store, tmp_path):
    cleared.rights[0].status = "RESTRICTED"
    store.save(cleared)
    client = TestClient(create_app(store, tmp_path))
    sid = cleared.sources[0].id
    for path in [f"/sources/{sid}", f"/sources/{sid}/versions", f"/rights/{sid}"]:
        assert client.get(path).status_code == 404
    assert client.get("/sources").json()["total"] == 3
    assert client.get("/citations/" + quote(cleared.sources[0].citation)).json()["status"] == "unresolved"
    assert sid not in public_corpus(cleared).model_dump_json()


def test_api_endpoints_and_no_writes(catalogue, store, tmp_path):
    store.save(catalogue)
    client = TestClient(create_app(store, tmp_path))
    for route in [
        "/jurisdictions",
        "/sources",
        "/sources/ng-foi-act-2011",
        "/sources/ng-foi-act-2011/versions",
        "/coverage",
        "/rights/ng-foi-act-2011",
        "/releases",
        "/search?q=freedom",
        "/citations/FOI%20Act%202011",
    ]:
        assert client.get(route).status_code == 200
    assert client.get("/search?q=freedom").json()["total"] == 1
    assert client.get("/sources?limit=2&offset=2").json()["items"][0]["id"]
    assert client.get("/sources?limit=101").status_code == 422
    assert client.get("/search?q=%20%20").status_code == 422
    assert client.post("/sources", json={}).status_code == 405
    assert client.get("/sources/missing").status_code == 404


def test_citations_can_be_ambiguous(catalogue, store, tmp_path):
    catalogue.citations.append(
        Citation(id="duplicate-alias", source_id=catalogue.sources[1].id, value=catalogue.citations[0].value)
    )
    store.save(catalogue)
    client = TestClient(create_app(store, tmp_path))
    assert client.get("/citations/" + quote(catalogue.citations[0].value)).json()["status"] == "ambiguous"
    assert client.get("/citations/not-a-citation").json()["status"] == "unresolved"


def test_release_hashes_and_immutability(cleared, tmp_path):
    path = write_release(cleared, tmp_path, "2026-10-05", "ng")
    manifest = json.loads((path / "manifest.json").read_text())
    for name, details in manifest["files"].items():
        assert sha256((path / name).read_bytes()).hexdigest() == details["sha256"]
    assert manifest["files"]["versions.jsonl"]["records"] == 1
    with pytest.raises(FileExistsError):
        write_release(cleared, tmp_path, "2026-10-05", "ng")
    other = write_release(cleared, tmp_path / "repro", "2026-10-05", "ng")
    assert (path / "manifest.json").read_bytes() == (other / "manifest.json").read_bytes()


def test_metadata_export_never_contains_text(cleared, tmp_path):
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    assert (path / "versions.jsonl").read_text() == ""
    assert (path / "structure.jsonl").read_text() == ""


def test_schema_rejects_broken_provenance(cleared):
    payload = cleared.model_dump()
    payload["structure"][0]["source_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="provenance"):
        Corpus.model_validate(payload)
    payload = cleared.model_dump()
    payload["structure"][0]["end_byte"] = 9999
    with pytest.raises(ValidationError, match="offset"):
        Corpus.model_validate(payload)
    payload = cleared.model_dump()
    payload["versions"][0]["canonical_text"] = "tampered"
    with pytest.raises(ValidationError, match="hash"):
        Corpus.model_validate(payload)


def test_missing_rights_fails_closed(cleared):
    cleared.rights = []
    assert public_corpus(cleared).sources == []


def test_store_roundtrip_and_version_immutability(cleared, store):
    store.save(cleared)
    assert len(store.load().versions) == 1
    cleared.versions[0].parser = "different-parser"
    with pytest.raises(ValueError, match="immutable"):
        store.save(cleared)


def test_download_paths_allowlisted(catalogue, store, tmp_path):
    store.save(catalogue)
    write_release(catalogue, tmp_path, "2026-10-05", "ng")
    client = TestClient(create_app(store, tmp_path))
    assert client.get("/releases/2026-10-05/ng/corpus/manifest.json").status_code == 200
    assert client.get("/releases/2026-10-05/ng/corpus/ng.json").status_code == 404


def test_old_release_withdrawn_after_rights_change(cleared, store, tmp_path):
    store.save(cleared)
    write_release(cleared, tmp_path, "2026-10-05", "ng")
    client = TestClient(create_app(store, tmp_path))
    url = "/releases/2026-10-05/ng/corpus/versions.jsonl"
    assert client.get(url).status_code == 200
    cleared.rights[0].status = "RIGHTS_UNDER_REVIEW"
    store.save(cleared)
    assert client.get(url).status_code == 410
    assert client.get("/releases").json() == []


def test_tampered_release_is_not_served(catalogue, store, tmp_path):
    store.save(catalogue)
    path = write_release(catalogue, tmp_path, "2026-10-05", "ng")
    (path / "sources.jsonl").write_text("tampered")
    client = TestClient(create_app(store, tmp_path))
    assert client.get("/releases/2026-10-05/ng/corpus/sources.jsonl").status_code == 410


def test_pending_ingestion_can_be_reviewed_without_mutating_content(cleared, store):
    approved = cleared.model_copy(deep=True)
    cleared.versions[0].verification_status = "pending"
    cleared.versions[0].content_verifier = None
    cleared.versions[0].content_verified_at = None
    store.save(cleared)
    assert not public_corpus(store.load()).versions
    store.save(approved)
    assert len(public_corpus(store.load()).versions) == 1


def test_text_response_carries_attribution(cleared, store, tmp_path):
    store.save(cleared)
    client = TestClient(create_app(store, tmp_path))
    response = client.get(f"/sources/{cleared.sources[0].id}/versions")
    assert response.json()["rights"]["attribution_text"] == "Synthetic test author"
    assert response.headers["cache-control"] == "no-store"


def test_manifest_cannot_omit_hashes(catalogue, store, tmp_path):
    store.save(catalogue)
    path = write_release(catalogue, tmp_path, "2026-10-05", "ng")
    manifest = json.loads((path / "manifest.json").read_text())
    del manifest["files"]["versions.jsonl"]
    (path / "manifest.json").write_text(json.dumps(manifest))
    client = TestClient(create_app(store, tmp_path))
    assert client.get("/releases/2026-10-05/ng/corpus/manifest.json").status_code == 410
