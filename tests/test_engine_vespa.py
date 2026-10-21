"""RSV-04 and RSV-10 on the dedicated engine (Vespa): the projector makes the engine hold exactly the
public units and catches up from the change log; lexical, document and dense searches run in the
engine with the section index's words and match semantics and the hard filters in the query and report their ranking features; a generation's vectors
are a field of the units, validated and activated; a withdrawal leaves the engine at the next sync;
the pack degrades to the system of record when the engine is down.

Needs a Vespa to test against: OPEN_ALVARY_TEST_ENGINE_URL (query and feed, e.g. http://127.0.0.1:28080)
and OPEN_ALVARY_TEST_ENGINE_CONFIG_URL (deploy, e.g. http://127.0.0.1:29071). The engine is emptied
first; never point it at an engine in use."""

import os

import pytest
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_v1_text import UNITS, publish_act, withdraw

from api.catalogue import LegacyCatalogue
from api.repositories import LegacyRepository
from api.store import Store
from api.text_index import Filters
from retrieval import indexer, projector
from retrieval.engine import Engine
from retrieval.pack import Request, Retriever
from retrieval.vectors import HashingEmbedder, Profile

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("OPEN_ALVARY_TEST_ENGINE_URL"), reason="no test engine configured"),
]
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


@pytest.fixture
def store(pg_engine, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    monkeypatch.delenv("OPEN_ALVARY_VECTOR_PROFILE", raising=False)
    migrate(pg_engine)
    return Store(pg_engine)


@pytest.fixture
def engine(store):
    found = Engine(
        os.environ["OPEN_ALVARY_TEST_ENGINE_URL"], os.environ["OPEN_ALVARY_TEST_ENGINE_CONFIG_URL"]
    )
    projector.deploy(store.engine, found)
    for version in {d["version_id"] for d in found.visit(fields="version_id")}:
        found.delete_version(version)
    return found


def pack(store, engine, query, **options):
    with store.engine.begin() as conn:
        service = LegacyCatalogue(LegacyRepository(conn))
        return Retriever(service, search_engine=engine, **options).retrieve(
            Request(query=query, filters=Filters())
        )


def test_the_engine_holds_exactly_the_public_units_and_follows_the_change_log(store, engine):
    _, version, nodes = publish_act(store, UNITS)
    first = projector.sync(store.engine, engine)
    assert first["rebuilt"] and first["fed"] > 0
    held = {d["version_id"] for d in engine.visit(fields="version_id")}
    assert held == {version.id}
    assert projector.sync(store.engine, engine)["fed"] == 0  # nothing new in the change log
    found = pack(store, engine, "repaid from salary")
    assert found.revisions.engine == "vespa" and found.evidence[0].anchor_id == nodes["sec_1"].id
    features = found.evidence[0].ranking.features["lexical"]
    assert features["bm25(words)"] > 0 and set(features) == {
        "bm25(headings)", "bm25(words)", "bm25(title)", "bm25(identifiers)",
    }  # fmt: skip
    named = pack(store, engine, "Example Act")
    assert any(u.unit == "document" for u in named.evidence)
    # The engine matches the words the section index matches: all of them first, any of them when few
    # units have all; a stopword alone matches nothing.
    question = pack(store, engine, "when is a loan repaid from wages")
    assert question.evidence and "lexical_any" in question.evidence[0].ranking.arms
    assert "lexical" not in question.evidence[0].ranking.arms
    assert pack(store, engine, "in the of").evidence == ()
    assert not any(u.unit == "document" for u in pack(store, engine, "repaid from salary").evidence)
    withdraw(store)
    assert projector.sync(store.engine, engine)["removed"] > 0
    assert engine.visit(fields="version_id") == []
    assert pack(store, engine, "repaid from salary").evidence == ()


def test_a_generation_in_the_engine_is_built_validated_and_searched(store, engine):
    _, version, nodes = publish_act(store, UNITS)
    projector.sync(store.engine, engine)
    generation = indexer.prepare(store.engine, PROFILE, backend="vespa", engine=engine)
    embedders = {PROFILE.id: HashingEmbedder(PROFILE)}
    assert indexer.work(store.engine, batches=3, embedders=embedders, searcher=engine)["failed"] == 0
    results = indexer.validate(store.engine, generation, embedder=embedders[PROFILE.id], searcher=engine)
    assert results["passed"], results
    assert results["missing_units"] == 0 and results["ann_recall_at_20"] >= 0.98
    indexer.activate(store.engine, generation, allow_test=True)
    found = pack(store, engine, "repaid salary", vector_profile=PROFILE.id, embedder=HashingEmbedder(PROFILE))
    assert found.revisions.vector == f"{PROFILE.id}@{generation}"
    dense = [u for u in found.evidence if "dense" in u.ranking.arms]
    assert dense and any(k.startswith("closeness") for k in dense[0].ranking.features["dense"])
    # A resolved citation is the answer: meaning adds nothing to it.
    cited = pack(store, engine, "Example Act, 2023, s. 1(2)(b)", vector_profile=PROFILE.id,
                 embedder=HashingEmbedder(PROFILE))  # fmt: skip
    assert cited.evidence[0].ranking.arms.get("citation") == 1
    assert not any("dense" in u.ranking.arms for u in cited.evidence)


def test_each_language_is_analysed_as_in_the_section_index_and_again_on_a_new_analyzer(
    store, engine, monkeypatch
):
    # Same words, same matching (RSV-04, MIN-02): Portuguese and Arabic are stemmed in the engine as in
    # the section index, and Kiswahili, matched word for word there, is never stemmed here. A new
    # analyzer is a new application: every unit held is analysed again, its vectors kept.
    from api import text_index

    texts = {
        "pt": ("ng-lei-exemplo-2023", "Lei exemplo, 2023", "1. Os trabalhadores têm direito a contratos escritos."),
        "ar": ("ng-qanun-mithal-2023", "قانون مثال، 2023", "1. يحق للموظفين الحصول على البيانات الشخصية لدى الحكومة."),
        "sw": ("ng-sheria-mfano-2023", "Sheria ya Mfano, 2023", "1. Wafanyakazi wana haki ya mikataba ya maandishi."),
    }  # fmt: skip
    anchors, versions = {}, {}
    for language, (work, title, words) in texts.items():
        _, versions[language], nodes = publish_act(store, [{"kind": "paragraph", "text": words}], work=work,
                                                   title=title, language=language)  # fmt: skip
        anchors[language] = nodes["sec_1"]
    projector.sync(store.engine, engine)

    def first(query):
        found = pack(store, engine, query)
        assert found.revisions.engine == "vespa" and "engine:unavailable" not in found.coverage.degraded
        return next((u.anchor_id for u in found.evidence if u.unit == "provision"), None)

    assert first("trabalhador contrato escrito") == anchors["pt"].id
    assert first("موظف شخصي حكومة") == anchors["ar"].id
    assert first("haki ya mikataba") == anchors["sw"].id and first("mkataba") is None
    generation = indexer.prepare(store.engine, PROFILE, backend="vespa", engine=engine)
    embedders = {PROFILE.id: HashingEmbedder(PROFILE)}
    assert indexer.work(store.engine, batches=3, embedders=embedders, searcher=engine)["failed"] == 0
    with store.engine.begin() as conn:
        vectors = indexer.generation(conn, generation)["vectors_table"]
    unit = Engine.doc_id(versions["sw"].id, anchors["sw"].start_byte)
    held = engine.get(unit, vectors)[vectors]
    monkeypatch.setattr(text_index, "ANALYZER", "oa-search-test")
    with store.engine.begin() as conn:
        text_index.ensure(conn)
    projector.deploy(store.engine, engine)
    again = projector.sync(store.engine, engine)
    assert again["rebuilt"] and again["fed"] == 0 and again["reanalysed"] > 0
    assert {d.get("analyzer") for d in engine.visit(fields="analyzer")} == {"oa-search-test"}
    assert engine.get(unit, vectors)[vectors] == held
    assert first("trabalhador contrato escrito") == anchors["pt"].id


def test_the_pack_falls_back_to_the_system_of_record_when_the_engine_is_down(store):
    publish_act(store, UNITS)
    down = Engine("http://127.0.0.1:9", None)
    found = pack(store, down, "repaid from salary")
    assert "engine:unavailable" in found.coverage.degraded and found.evidence
    assert found.revisions.engine is None
