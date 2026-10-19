"""RSV-11 sovereign copies and REL-01: a corpus release built from the published store carries every
published and approved text with its structure (its anchors), hashed in the manifest; a withdrawn text
makes the release unavailable for serving."""

import json

import pytest
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_v1_text import UNITS, publish_act, withdraw

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
    assert release_available(release, store.load())
    withdraw(store)
    assert not release_available(release, store.load())  # never served once the text is withdrawn
    metadata = write_release(store.load(), tmp_path, "2026-10-19.1", "ng")
    assert lines(metadata / "versions.jsonl") == []
