"""REL-01, F01: whether a release may be served is decided on every request from its own files and the
current rights of its records, never by loading the corpus. A withdrawal refuses it on the next answer."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from test_postgres_coverage import ASKED, RIGHTS_CHANGES, VERSION_CHANGES

from api import release_adapter
from api.repositories import LegacyRepository
from api.service import create_service
from api.store import Store
from exports import verify
from exports.release import write_release
from exports.verify import release_available
from rights.policy import public_corpus
from schema.models import Corpus, RightsRecord, SourceVersion

METADATA = "/downloads/2026-10-05/ng/metadata/sources.jsonl"


def whole_corpus_answer(directory, corpus):
    """What the release's records ask of the current ones, from the whole corpus: the answer to keep."""
    rows = {
        key: [json.loads(line) for line in (directory / f"{key}.jsonl").read_text().splitlines()]
        for key in ("sources", "versions", "rights")
    }
    public = public_corpus(corpus)
    sources = {s.id for s in public.sources}
    versions = {(v.id, v.content_hash) for v in public.versions}
    rights = {r.source_id: r for r in public.rights}
    return (
        all(s["id"] in sources for s in rows["sources"])
        and all((v["id"], v["content_hash"]) in versions for v in rows["versions"])
        and all(RightsRecord.model_validate(r) == rights.get(r["source_id"]) for r in rows["rights"])
    )


def rewrite(directory, name, rows):
    """A release file with other rows, with the manifest's hash to match: it passes the hash check."""
    path = directory / name
    path.write_text("".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows))
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["files"][name]["sha256"] = sha256(path.read_bytes()).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def with_rights(corpus, **changes):
    changed = corpus.model_copy(deep=True)
    changed.rights[0] = RightsRecord.model_validate({**changed.rights[0].model_dump(mode="json"), **changes})
    return changed


def with_version(corpus, **changes):
    changed = corpus.model_copy(deep=True)
    changed.versions[0] = SourceVersion.model_validate(
        {**changed.versions[0].model_dump(mode="json"), **changes}
    )
    return changed


def asked_at(monkeypatch, moment):
    class Asked(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr("rights.policy.datetime", Asked)


@pytest.fixture(autouse=True)
def nothing_validated_yet(monkeypatch):
    monkeypatch.setattr(verify, "_validated", {})


@pytest.fixture
def no_corpus_load(monkeypatch):
    def refused(self):
        raise AssertionError("A request loaded the whole corpus")

    monkeypatch.setattr(Store, "load", refused)


@pytest.fixture
def client(cleared, store, tmp_path):
    store.save(cleared)
    write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    return TestClient(create_service(store, tmp_path))


def statements(store):
    seen = []
    event.listen(
        store.engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: seen.append(statement),
    )
    return seen


def test_the_listing_and_downloads_answer_without_loading_the_corpus(client, tmp_path, no_corpus_load):
    [listed] = client.get("/site/v1/releases").json()
    assert listed["download_base"] == "/downloads/2026-10-05/ng/metadata"
    response = client.get(METADATA)
    assert response.status_code == 200
    assert response.content == (tmp_path / "2026-10-05/ng/metadata/sources.jsonl").read_bytes()
    assert client.get("/api/releases").json()[0]["release_id"] == "2026-10-05"


def test_the_listing_and_downloads_read_no_text_and_no_whole_table(client, store):
    seen = statements(store)
    assert client.get("/site/v1/releases").json() and client.get(METADATA).status_code == 200
    assert seen
    for statement in seen:
        assert "versions" not in statement and "canonical_text" not in statement
        if "FROM rights" in statement or "FROM sources" in statement:
            assert " IN " in statement


@pytest.mark.parametrize(
    "changes",
    [
        dict(status="RIGHTS_UNDER_REVIEW"),
        dict(status="RESTRICTED"),
        dict(metadata_public=False),
        dict(metadata_basis=None),
        dict(legal_note="A corrected note"),
    ],
)
def test_a_withdrawn_release_is_refused_on_the_next_request(cleared, client, store, changes):
    assert client.get(METADATA).status_code == 200
    store.save(with_rights(cleared, **changes))
    refused = client.get(METADATA)
    assert refused.status_code == 410 and refused.json()["code"] == "withdrawn"
    assert client.get("/site/v1/releases").json() == []
    assert client.get("/api/releases").json() == []
    store.save(cleared)  # and its return is as prompt: nothing about the withdrawal was kept
    assert client.get(METADATA).status_code == 200
    assert len(client.get("/site/v1/releases").json()) == 1


def test_a_review_that_expires_refuses_a_text_release_with_nothing_written(
    cleared, store, tmp_path, monkeypatch
):
    expiring = with_rights(cleared, expires_at="2030-01-01T00:00:00Z")
    store.save(expiring)
    text_release = write_release(expiring, tmp_path, "2026-10-05", "ng")
    metadata_release = write_release(expiring, tmp_path, "2026-10-05", "ng", metadata_only=True)
    assert release_available(text_release, store) and release_available(metadata_release, store)
    asked_at(monkeypatch, datetime(2030, 1, 1, tzinfo=UTC))
    assert not release_available(text_release, store)
    assert release_available(metadata_release, store)  # the policy lets the catalogue record stand
    assert whole_corpus_answer(metadata_release, store.load())


def test_a_release_whose_record_is_gone_is_refused(cleared, store, tmp_path):
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    with store.engine.begin() as connection:
        connection.execute(text("DELETE FROM rights WHERE source_id = :id"), {"id": cleared.sources[0].id})
    assert not release_available(path, store)


def test_a_release_naming_a_record_that_never_existed_is_refused(cleared, store, tmp_path):
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    sources = [json.loads(line) for line in (path / "sources.jsonl").read_text().splitlines()]
    rewrite(path, "sources.jsonl", [*sources, {**sources[0], "id": "ng-never-catalogued"}])
    assert not release_available(path, store)


def test_a_row_that_differs_from_its_columns_counts_as_no_record(cleared, store, tmp_path):
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE rights SET id = 'renamed-rights' WHERE source_id = :id"),
            {"id": cleared.sources[0].id},
        )
    assert not release_available(path, store)


@pytest.mark.parametrize("metadata_only", [True, False])
def test_every_rights_answer_is_the_one_the_whole_corpus_gives(
    cleared, store, tmp_path, monkeypatch, metadata_only
):
    """Current rights equal to the release's, so the policy decides, not the comparison between them."""
    asked_at(monkeypatch, ASKED)
    answers = set()
    for number, changes in enumerate(RIGHTS_CHANGES):
        store.save(cleared)
        path = write_release(cleared, tmp_path / str(number), "2026-10-05", "ng", metadata_only=metadata_only)
        changed = with_rights(cleared, **changes)
        store.save(changed)
        rows = [json.loads(line) for line in (path / "rights.jsonl").read_text().splitlines()]
        rewrite(
            path,
            "rights.jsonl",
            [
                changed.rights[0].model_dump(mode="json") if r["id"] == changed.rights[0].id else r
                for r in rows
            ],
        )
        answer = release_available(path, store)
        assert answer == whole_corpus_answer(path, store.load()), changes
        answers.add(answer)
    assert answers == {True, False}


def test_every_version_answer_is_the_one_the_whole_corpus_gives(cleared, store, tmp_path, monkeypatch):
    asked_at(monkeypatch, ASKED)
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng")
    assert release_available(path, store)
    for changes in VERSION_CHANGES:
        store.save(with_version(cleared, **changes))
        assert release_available(path, store) == whole_corpus_answer(path, store.load()), changes
    store.save(cleared)
    assert release_available(path, store)
    store.save(with_rights(cleared, approved_content_hashes=[]))
    assert not release_available(path, store)


def test_a_release_of_many_records_asks_in_batches(cleared, store, tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "BATCH", 2)
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    seen = statements(store)
    assert release_available(path, store)
    assert len(seen) >= 3 + 3  # five records, each asked about as a source and as a rights record
    store.save(with_rights(cleared, metadata_public=False))
    assert not release_available(path, store)


def test_a_file_changed_after_serving_is_refused_at_once(client, tmp_path):
    assert client.get(METADATA).status_code == 200
    (tmp_path / "2026-10-05/ng/metadata/citations.jsonl").write_text("")
    assert client.get(METADATA).status_code == 410
    assert client.get("/site/v1/releases").json() == []


@pytest.mark.parametrize(
    "name, rows",
    [
        ("sources.jsonl", [{"id": "only-an-id"}]),
        ("citations.jsonl", [{"id": "c", "source_id": "ng-not-in-this-release", "value": "A citation"}]),
        ("languages.jsonl", []),  # every record's language is then missing
    ],
)
def test_a_release_that_does_not_validate_is_refused_though_its_hashes_match(
    cleared, store, tmp_path, name, rows
):
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    assert release_available(path, store)
    rewrite(path, name, rows)
    assert not release_available(path, store)
    assert not release_available(path, store)


def test_a_line_separator_inside_a_value_is_no_new_row(cleared, store, tmp_path):
    cleared.sources[0].title = "An Act\u2028with a separator"
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    assert release_available(path, store)


def test_a_release_is_validated_once_for_its_bytes_however_many_ask(cleared, store, tmp_path, monkeypatch):
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    validate = Corpus.model_validate
    calls = []

    def counted(*args, **kwargs):
        calls.append(args)
        return validate(*args, **kwargs)

    monkeypatch.setattr(Corpus, "model_validate", counted)
    written = (path / "manifest.json").read_bytes()
    with ThreadPoolExecutor(8) as pool:
        assert all(pool.map(lambda _: verify._valid(path, written), range(8)))
    assert release_available(path, store)
    assert len(calls) == 1
    rewrite(path, "citations.jsonl", [])  # other bytes are other bytes
    assert release_available(path, store)
    assert len(calls) == 2


def test_a_database_that_cannot_answer_is_no_withdrawal(cleared, store, tmp_path, monkeypatch):
    store.save(cleared)
    path = write_release(cleared, tmp_path, "2026-10-05", "ng", metadata_only=True)
    client = TestClient(create_service(store, tmp_path), raise_server_exceptions=False)
    original = LegacyRepository.public_rights
    down = {"from_check": 1}
    checks = []

    def failing(self, source_ids):
        if len(checks) >= down["from_check"]:
            raise OperationalError("SELECT", {}, Exception("connection refused"))
        return original(self, source_ids)

    def counted(directory, current):
        checks.append(directory)
        return release_available(directory, current)

    monkeypatch.setattr(LegacyRepository, "public_rights", failing)
    monkeypatch.setattr(release_adapter, "release_available", counted)
    assert client.get(METADATA).status_code == 503
    with pytest.raises(OperationalError):
        release_available(path, store)
    checks.clear()
    down["from_check"] = 2  # the first check passes; the one just before the first byte cannot answer
    response = client.get(METADATA)
    assert response.status_code == 503 and b"ng-" not in response.content
