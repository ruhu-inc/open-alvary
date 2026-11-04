"""RSV-11 sovereign copies and REL-01: a corpus release built from the published store carries every
published and approved text with its structure (its anchors), hashed in the manifest; a withdrawn text
makes the release unavailable for serving."""

import json

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_coverage import ASKED, RIGHTS_CHANGES, VERSION_CHANGES
from test_release_availability import asked_at, rewrite, whole_corpus_answer, with_rights, with_version
from test_v1_text import UNITS, publish_act, withdraw

from api.repositories import LegacyRepository
from api.store import Store
from exports.release import write_release
from exports.verify import release_available

pytestmark = pytest.mark.postgres


@pytest.fixture
def store(pg_engine, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    return Store(pg_engine)


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_corpus_release_from_the_published_store_carries_texts_and_structure(store, tmp_path):
    _, version, nodes = publish_act(store, UNITS)
    release = write_release(store.load(), tmp_path, "2026-10-19", "ng")
    [released] = lines(release / "versions.jsonl")
    assert released["id"] == version.id and released["canonical_text"] == version.canonical_text
    assert {n["id"] for n in lines(release / "structure.jsonl")} == {n.id for n in nodes.values()}
    manifest = json.loads((release / "manifest.json").read_text())
    assert manifest["mode"] == "corpus" and manifest["coverage"]["published_versions"] == 1
    assert release_available(release, store)
    withdraw(store)
    assert not release_available(release, store)  # never served once the text is withdrawn
    metadata = write_release(store.load(), tmp_path, "2026-10-19.1", "ng")
    assert lines(metadata / "versions.jsonl") == []


@pytest.fixture
def released(store, cleared, tmp_path):
    store.save(cleared)
    return write_release(cleared, tmp_path, "2026-10-05", "ng")


@pytest.mark.parametrize("gates", [True, False])
def test_every_answer_is_the_one_the_whole_corpus_gives(
    store, cleared, released, tmp_path, monkeypatch, gates
):
    """With a version's gates from their columns, and from its payload where a database has none."""
    if not gates:
        monkeypatch.setattr(LegacyRepository, "_gated", lambda self: False)
    asked_at(monkeypatch, ASKED)
    assert release_available(released, store)
    for changes in VERSION_CHANGES:
        store.save(with_version(cleared, **changes))
        assert release_available(released, store) == whole_corpus_answer(released, store.load()), changes
    store.save(cleared)
    answers = set()
    for number, changes in enumerate(RIGHTS_CHANGES):
        store.save(cleared)
        path = write_release(cleared, tmp_path / str(number), "2026-10-05", "ng")
        changed = with_rights(cleared, **changes)
        store.save(changed)
        rewrite(
            path,
            "rights.jsonl",
            [r.model_dump(mode="json") for r in changed.rights if r.id == changed.rights[0].id]
            + [
                json.loads(line)
                for line in (path / "rights.jsonl").read_text().splitlines()
                if json.loads(line)["id"] != changed.rights[0].id
            ],
        )
        answer = release_available(path, store)
        assert answer == whole_corpus_answer(path, store.load()), changes
        answers.add(answer)
    assert answers == {True, False}


def test_no_answer_parses_a_text_where_the_database_keeps_the_gates(store, released):
    seen = []
    event.listen(
        store.engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: seen.append(statement),
    )
    assert release_available(released, store)
    versions = [statement for statement in seen if "versions" in statement]
    assert versions
    assert not any("payload" in statement or "canonical_text" in statement for statement in versions)


def test_the_answer_waits_on_the_same_fence_as_every_read(store, released):
    with store.engine.begin() as connection:
        connection.execute(text("DELETE FROM identity_migration_state"))
    with pytest.raises(DBAPIError, match="write-fenced"):
        release_available(released, store)
