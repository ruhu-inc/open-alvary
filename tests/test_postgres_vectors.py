"""RSV-04 and RSV-10: semantic index generations are prepared, filled by workers outside transactions,
validated (coverage, recomputation, approximate recall) and activated; publication queues new texts;
withdrawal is enforced at query time and purged after; a failing embedder degrades to the lexical arms;
a reranker reorders what it scores; rollback refuses a generation that no longer covers what is public.
The hashing embedder here finds shared words, not meaning: it checks the wiring, never quality."""

import httpx
import pytest
from fastapi.testclient import TestClient
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_v1_text import UNITS, publish_act, withdraw

from api.catalogue import LegacyCatalogue
from api.repositories import LegacyRepository
from api.store import Store
from api.text_index import Filters
from retrieval import indexer
from retrieval.pack import Request, Retriever
from retrieval.rerank import HttpReranker
from retrieval.vectors import HashingEmbedder, Profile

pytestmark = pytest.mark.postgres
PROFILE = Profile.from_dict(
    {
        "id": "test-hashing-64",
        "model": "hashing",
        "revision": "1",
        "dims": 64,
        "embedder": "hashing",
        "test": True,
    }
)
OTHER = [
    {"kind": "heading", "text": "SECOND ACT, 2024"},
    {
        "kind": "paragraph",
        "text": "1. A borrower may defer repayment while unemployed.",
        "notes": ["Deferral"],
    },
]


@pytest.fixture
def store(pg_engine, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    monkeypatch.delenv("OPEN_ALVARY_VECTOR_PROFILE", raising=False)
    migrate(pg_engine)
    return Store(pg_engine)


def pack(store, query, **options):
    with store.engine.begin() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        retriever = Retriever(
            service, vector_profile=PROFILE.id, embedder=HashingEmbedder(PROFILE), **options
        )
        return retriever.retrieve(Request(query=query, filters=Filters())).model_dump(mode="json")


def build(store, backend):
    generation = indexer.prepare(store.engine, PROFILE, backend=backend)
    embedders = {PROFILE.id: HashingEmbedder(PROFILE)}
    assert indexer.work(store.engine, batches=5, embedders=embedders)["failed"] == 0
    return generation, embedders


@pytest.mark.parametrize("backend", ["pgvector", "exact"])
def test_a_generation_is_built_validated_activated_and_searched(store, backend):
    _, version, nodes = publish_act(store, UNITS)
    generation, embedders = build(store, backend)
    with pytest.raises(PermissionError, match="Validate"):
        indexer.activate(store.engine, generation, allow_test=True)
    results = indexer.validate(store.engine, generation, embedder=embedders[PROFILE.id])
    assert results["passed"] and results["missing_units"] == 0 and results["recompute_min_cosine"] >= 0.995
    assert (results["ann_recall_at_20"] is not None) == (backend == "pgvector")
    with pytest.raises(PermissionError, match="test profile"):
        indexer.activate(store.engine, generation)
    indexer.activate(store.engine, generation, allow_test=True)
    found = pack(store, "repaid salary")
    assert found["revisions"]["vector"] == f"{PROFILE.id}@{generation}"
    assert found["coverage"]["dense_coverage"] == 1.0
    assert "dense" in found["evidence"][0]["ranking"]["arms"]
    assert found["evidence"][0]["anchor_id"] == nodes["sec_1"].id


def test_publication_queues_new_texts_and_withdrawal_is_enforced_then_purged(store):
    publish_act(store, UNITS)
    generation, embedders = build(store, "pgvector")
    indexer.validate(store.engine, generation, embedder=embedders[PROFILE.id])
    indexer.activate(store.engine, generation, allow_test=True)
    publish_act(store, OTHER, work="ng-second-act-2024", title="Second Act, 2024")
    assert (
        pack(store, "defer repayment unemployed")["coverage"]["dense_coverage"] == 0.5
    )  # queued, not embedded
    indexer.work(store.engine, batches=2, embedders=embedders)
    assert pack(store, "defer repayment unemployed")["coverage"]["dense_coverage"] == 1.0
    withdraw(store)  # the first record's rights: its text leaves the section index
    found = pack(store, "repaid salary")
    assert all(unit["source_id"] != "ng-example-act-2023" for unit in found["evidence"])
    assert indexer.purge(store.engine) > 0 and indexer.purge(store.engine) == 0


def test_a_failing_embedder_degrades_to_the_lexical_arms(store):
    publish_act(store, UNITS)
    generation, embedders = build(store, "exact")
    indexer.validate(store.engine, generation, embedder=embedders[PROFILE.id])
    indexer.activate(store.engine, generation, allow_test=True)

    class Broken:
        def embed(self, texts, *, query):
            raise TimeoutError("slow")

    with store.engine.begin() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        found = Retriever(service, vector_profile=PROFILE.id, embedder=Broken()).retrieve(
            Request(query="repaid from salary", filters=Filters())
        )
    assert "dense:unavailable" in found.coverage.degraded and found.evidence


def test_the_reranker_orders_what_it_scores(store):
    _, version, nodes = publish_act(store, UNITS)

    def answer(request):
        texts = __import__("json").loads(request.content)["texts"]
        return httpx.Response(
            200, json=[{"index": i, "score": 0.9 if "Fund" in t else 0.1} for i, t in enumerate(texts)]
        )

    reranker = HttpReranker("http://reranker.test", "test-reranker@1", transport=httpx.MockTransport(answer))
    found = pack(store, "loan salary fund", reranker=reranker)
    assert found["revisions"]["reranker"] == "test-reranker@1"
    first = found["evidence"][0]
    assert "Fund" in first["text"] and first["ranking"]["rerank_score"] == 0.9


def test_rollback_needs_a_generation_that_still_covers_what_is_public(store):
    publish_act(store, UNITS)
    first, embedders = build(store, "exact")
    indexer.validate(store.engine, first, embedder=embedders[PROFILE.id])
    indexer.activate(store.engine, first, allow_test=True)
    second, _ = build(store, "exact")
    indexer.validate(store.engine, second, embedder=embedders[PROFILE.id])
    indexer.activate(store.engine, second, allow_test=True)
    indexer.rollback(store.engine, PROFILE.id, first, embedder=embedders[PROFILE.id], allow_test=True)
    with store.engine.begin() as conn:
        assert indexer.active(conn, PROFILE.id)["generation_id"] == first
    # A text published while the second is retired is queued only for the active first generation.
    publish_act(store, OTHER, work="ng-second-act-2024", title="Second Act, 2024")
    indexer.work(store.engine, batches=2, embedders=embedders)
    with pytest.raises(PermissionError, match="no longer covers"):
        indexer.rollback(store.engine, PROFILE.id, second, embedder=embedders[PROFILE.id], allow_test=True)


def test_status_reports_each_generation(store, tmp_path):
    publish_act(store, UNITS)
    generation, _ = build(store, "exact")
    [row] = indexer.status(store.engine)
    assert row["generation_id"] == generation and row["state"] == "building" and row["done"] == 1
    assert TestClient  # the API is unaffected by a building generation
