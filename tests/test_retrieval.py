"""The retrieval service's rules without a database (RSV-03, RSV-05, RSV-06, RSV-08): routes, fusion,
signals, bands, diversification, derived links and record-level currentness."""

from datetime import date
from types import SimpleNamespace

import pytest

from api.text_index import fold, name_forms
from retrieval import links, router
from retrieval.legal_time import (
    Event,
    Profile,
    aggregate,
    provision_currentness,
    provision_state,
    record_currentness,
)
from retrieval.ranking import BAND_WIDTH, PER_WORK, RRF_K, Hit, apply_signals, diversify, fuse


def hit(version="ver_a", start=0, end=10, work="w1", node=None, unit="provision", status="current",
        replaced=False, jurisdiction="ng", document_type="act", locator=None, language="en"):  # fmt: skip
    return Hit(
        version_id=version, start_byte=start, end_byte=end, work_id=work, node_id=node or f"n{start}",
        unit=unit, kind="section", locator=locator or f"sec_{start}", pinpoint=None, heading=None,
        language=language, config="english", content_hash="0" * 64, legal_status=status, replaced=replaced,
        jurisdiction_id=jurisdiction, document_type=document_type,
    )  # fmt: skip


def test_routes_are_decided_by_rules_and_recorded():
    assert router.definition_term('meaning of "personal data" in the NDPA') == "personal data"
    assert router.definition_term("What is a “data subject”?") == "data subject"
    assert router.definition_term("data breach notification") is None
    cited = SimpleNamespace(provision=SimpleNamespace(status="resolved"))
    assert router.plan("NDPA s. 23", citation=cited, works={}, as_of=None, jurisdictions=None).routes == (
        "citation",
        "topic",
    )
    named = router.plan("the NDPA", citation=None, works={"w": "ndpa"}, as_of=None, jurisdictions=None)
    assert named.routes == ("instrument",) and named.residual == "the"
    both = router.plan(
        "consent under the NDPA",
        citation=None,
        works={"w": "ndpa"},
        as_of=date(2024, 1, 1),
        jurisdictions=("ng", "rw"),
    )
    assert both.routes == ("instrument", "topic", "historical", "comparative") and both.comparative
    assert router.plan("x", citation=None, works={}, as_of=None, jurisdictions=None).routes == ("topic",)


def test_names_fold_to_letters_and_digits_with_and_without_the_year():
    assert fold("Nigeria Data Protection Act, 2023") == "nigeria data protection act 2023"
    assert name_forms(["Nigeria Data Protection Act, 2023", "NDPA", "Act No. 37 of 2023", "Act"]) == [
        "nigeria data protection act 2023",
        "nigeria data protection act",
        "ndpa",
        "ndpa 2023",
        "act no 37 of 2023",
    ]
    # Initials as cited: of every word, and of the significant ones; three letters only with a year.
    assert "foia" in name_forms(["Freedom of Information Act 2011"])
    assert {"cama", "cama 2020"} <= set(name_forms(["Companies and Allied Matters Act, 2020"]))
    assert "isa" not in name_forms(["Investments and Securities Act, 2025"])
    assert "isa 2025" in name_forms(["Investments and Securities Act, 2025"])


def test_fusion_collapses_each_arm_to_units_then_adds_reciprocal_ranks():
    a, b = hit(start=0), hit(start=20)
    fused = fuse({"lexical": [a, a, b], "document": [b]})
    assert [c.hit.start_byte for c in fused] == [20, 0]  # b is in both arms
    assert fused[0].arms == {"lexical": 2, "document": 1}
    assert abs(fused[0].fused - (1 / (RRF_K + 2) + 1 / (RRF_K + 1))) < 1e-12


def test_signals_lead_with_the_route_then_the_instrument_and_order_bands_by_authority():
    units = [hit(start=n * 10, document_type="regulation") for n in range(BAND_WIDTH - 1)]
    units.append(hit(version="ver_c", start=0, document_type="constitution"))
    ranked = apply_signals(fuse({"lexical": units}), set())
    assert ranked[0].hit.document_type == "constitution"  # within the first band, the higher instrument
    assert any(s[0] == "instrument_authority" for s in ranked[0].signals)
    repealed = hit(version="ver_r", start=0, status="repealed")
    defined = hit(version="ver_d", start=5)
    ranked = apply_signals(fuse({"lexical": [repealed, hit(start=30)], "definition": [defined]}), {"w9"})
    assert ranked[0].hit.version_id == "ver_d" and ranked[0].signals[0][0] == "route_match"
    assert next(c for c in ranked if c.hit.version_id == "ver_r").signals[0] == (
        "legal_status",
        "repealed",
        "x0.8",
    )


def test_diversification_collapses_overlaps_and_duplicates_caps_texts_and_interleaves():
    section, inside = hit(start=0, end=100), hit(start=10, end=20, node="def")
    kept = diversify(fuse({"a": [section, inside]}), scoped=True, comparative=False)
    assert len(kept) == 1 and "collapsed into it" in kept[0].diversification[0]
    first, again = hit(version="ver_1", locator="sec_5"), hit(version="ver_2", locator="sec_5")
    kept = diversify(fuse({"a": [first, again]}), scoped=True, comparative=False)
    assert len(kept) == 1 and kept[0].also_in == ["ver_2"]
    many = [hit(start=n * 10, end=n * 10 + 5) for n in range(PER_WORK + 2)]
    other = hit(version="ver_b", work="w2", start=0)
    order = diversify(fuse({"a": [*many, other]}), scoped=False, comparative=False)
    assert [c.hit.version_id for c in order] == ["ver_a"] * PER_WORK + ["ver_b"] + ["ver_a"] * 2
    assert "deferred" in order[-1].diversification[0]  # moved behind the other text, never dropped
    assert len(diversify(fuse({"a": many}), scoped=True, comparative=False)) == PER_WORK + 2
    mixed = [
        hit(version="v1", jurisdiction="ng"),
        hit(version="v2", work="w2", jurisdiction="ng"),
        hit(version="v3", work="w3", jurisdiction="rw"),
    ]
    order = diversify(fuse({"a": mixed}), scoped=False, comparative=True)
    assert [c.hit.jurisdiction_id for c in order] == ["ng", "rw", "ng"]


def node(id_, kind, start, end, locator, parent=None):
    return SimpleNamespace(
        id=id_, kind=kind, start_byte=start, end_byte=end, locator=locator, parent_id=parent, order=start
    )


def test_definitions_in_scope_and_references_are_derived_from_the_text():
    raw = (
        "1. The Fund shall pay. See section 2 and s. 9.\n\n2. “Fund” means the Students Loans Fund.".encode()
    )
    defined_at = raw.index("“Fund”".encode())
    nodes = [
        node("n1", "section", 0, defined_at - 5, "sec_1"),
        node("n2", "section", defined_at - 3, len(raw), "sec_2"),
        node("d", "definition", defined_at, len(raw), "sec_2__def_fund", "n2"),
    ]
    defined = links.definitions(raw, nodes)
    assert [(t, n.id) for t, n in defined] == [("Fund", "d")]
    words = raw[: defined_at - 5].decode()
    assert [n.id for _, n in links.in_scope(words, defined, set())] == ["d"]
    assert links.in_scope("The fundamental rule", defined, set()) == []  # whole words only
    assert links.in_scope(words, defined, {"d"}) == []  # never its own definition
    by_locator = {n.locator: n for n in nodes}
    assert [(shown, n.id) for shown, n in links.references(words, by_locator, "sec_1")] == [("s. 2", "n2")]


def test_record_currentness_speaks_for_the_whole_text_only():
    version = SimpleNamespace(effective_from=date(2023, 6, 12), effective_until=None)
    today = date(2026, 10, 19)
    now = record_currentness("current", version, None, today)
    assert now.state == "in_force" and now.basis == "record" and now.assessed_at == today
    assert record_currentness("repealed", version, None, today).state == "not_in_force"
    assert record_currentness("unknown", version, None, today).state == "unknown"
    assert record_currentness("current", version, date(2020, 1, 1), today).state == "not_in_force"
    assert record_currentness("current", version, date(2024, 1, 1), today).state == "in_force"
    undated = SimpleNamespace(effective_from=None, effective_until=None)
    assert record_currentness("current", undated, date(2024, 1, 1), today).state == "unknown"


def event(
    id_,
    kind,
    day,
    *,
    scope="whole",
    locators=(),
    precision="day",
    until=None,
    conditions=None,
    supersedes=None,
):
    return Event(id_, kind, scope, tuple(locators), day, until, precision, conditions, supersedes)


def test_currentness_v1_decides_each_provision_from_reviewed_events():
    at = date(2024, 1, 1)
    whole = [event("e1", "commenced", date(2023, 6, 12))]
    assert provision_state(whole, "sec_5", at, None)[0] == "in_force"
    assert provision_state(whole, "sec_5", date(2023, 1, 1), None)[0] == "not_in_force"  # commences later
    assert provision_state([], "sec_5", at, None)[0] == "unknown"  # no commencement evidence
    partial = [
        event("e1", "commenced", date(2023, 6, 12), scope="explicit_nodes", locators=["sec_1", "sec_2"]),
        event("e2", "commenced", date(2025, 1, 1), scope="explicit_nodes", locators=["sec_3"]),
    ]
    assert provision_state(partial, "sec_2__subsec_1", at, None)[0] == "in_force"  # inside a named provision
    assert provision_state(partial, "sec_3", at, None)[0] == "not_in_force"
    assert provision_state(partial, "sec_4", at, None)[0] == "unknown"  # unenumerated: never assumed
    repealed = [*whole, event("e3", "repealed", date(2023, 9, 1), scope="explicit_nodes", locators=["sec_5"])]
    assert provision_state(repealed, "sec_5", at, None)[0] == "not_in_force"
    assert provision_state(repealed, "sec_6", at, None)[0] == "in_force"
    assert provision_state(repealed, "sec_5", date(2023, 8, 1), None)[0] == "in_force"
    two = [*whole, event("e4", "commenced", date(2023, 7, 1))]
    assert provision_state(two, "sec_1", at, None)[0] == "contested"
    corrected = [*whole, event("e4", "commenced", date(2023, 7, 1), supersedes="e1")]
    assert (
        provision_state(corrected, "sec_1", at, None)[0] == "in_force"
    )  # a superseded event no longer counts
    revived = [
        *repealed,
        event("e5", "revived", date(2023, 10, 1), scope="explicit_nodes", locators=["sec_5"]),
    ]
    assert provision_state(revived, "sec_5", at, None)[0] == "contested"  # no reviewed supersession
    suspended = [*whole, event("e6", "suspended", date(2023, 11, 1), until=date(2024, 2, 1))]
    assert provision_state(suspended, "sec_1", at, None)[0] == "not_in_force"
    assert provision_state(suspended, "sec_1", date(2024, 3, 1), None)[0] == "in_force"
    conditional = [
        event("e7", "commenced", None, precision="unknown", conditions="On a date the Minister appoints")
    ]
    assert provision_state(conditional, "sec_1", at, None)[0] == "unknown"
    imprecise = [event("e8", "commenced", date(2023, 12, 1), precision="month")]
    assert (
        provision_state(imprecise, "sec_1", date(2023, 12, 15), None)[0] == "unknown"
    )  # sometime in December
    assert provision_state(imprecise, "sec_1", at, None)[0] == "in_force"
    assent = [event("e9", "assented", date(2023, 6, 12))]
    assert provision_state(assent, "sec_1", at, None)[0] == "unknown"
    assert provision_state(assent, "sec_1", at, Profile("p", "assent"))[0] == "in_force"  # the reviewed rule


def test_a_unit_aggregates_the_parts_inside_it_that_events_name():
    events = [
        event("e1", "commenced", date(2023, 6, 12)),
        event("e2", "repealed", date(2023, 9, 1), scope="explicit_nodes", locators=["sec_5__subsec_2"]),
    ]
    found = provision_currentness(
        events, None, "sec_5", ["sec_5__subsec_1", "sec_5__subsec_2"], date(2024, 1, 1), date(2026, 10, 19)
    )
    assert (
        found.state == "partly_in_force"
        and found.basis == "provision"
        and found.algorithm == "currentness-v1"
    )
    assert set(found.evidence) == {"e1", "e2"}
    assert (
        aggregate(["in_force", "unknown"]) == "unknown"
        and aggregate(["contested", "in_force"]) == "contested"
    )


def test_within_a_band_decisions_order_by_court_relation_without_displacing_legislation():
    act = hit(version="ver_act", start=0, document_type="act")
    persuasive = hit(version="ver_p", start=0, work="w2", document_type="judgment")
    binding = hit(version="ver_b", start=0, work="w3", document_type="judgment")
    fused = fuse({"lexical": [persuasive, act, binding]})
    for candidate in fused:
        candidate.court_relation = {"ver_p": "persuasive", "ver_b": "binding"}.get(candidate.hit.version_id)
    ranked = apply_signals(fused, set())
    assert [c.hit.version_id for c in ranked] == ["ver_b", "ver_act", "ver_p"]  # the Act keeps its place
    assert ranked[0].signals[-1] == ("court_authority", "binding", "tie-break in band")


def test_the_http_embedder_sends_a_key_only_when_it_has_one(monkeypatch):
    import httpx

    from retrieval.vectors import HttpEmbedder, Profile

    monkeypatch.delenv("OPEN_ALVARY_EMBEDDER_KEY", raising=False)
    seen = []

    def answer(request):
        seen.append(request)
        inputs = __import__("json").loads(request.content)["input"]
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [3.0, 4.0] + [0.0] * 6} for i in range(len(inputs))]},
        )

    profile = Profile.from_dict(
        {"id": "local", "model": "m", "revision": "1", "dims": 8, "embedder": "http", "query_prompt": "Q: "}
    )
    local = HttpEmbedder(profile, url="http://embedder.test", transport=httpx.MockTransport(answer))
    assert local.embed(["salary"], query=True) == [[0.6, 0.8] + [0.0] * 6]
    assert "authorization" not in seen[-1].headers
    assert __import__("json").loads(seen[-1].content)["input"] == ["Q: salary"]
    keyed = HttpEmbedder(profile, url="http://embedder.test", key="k", transport=httpx.MockTransport(answer))
    keyed.embed(["salary"], query=False)
    assert seen[-1].headers["authorization"] == "Bearer k"
    assert "dimensions" not in __import__("json").loads(seen[-1].content)
    truncated = Profile.from_dict(profile.as_dict() | {"request_dims": True})
    HttpEmbedder(truncated, url="http://embedder.test", transport=httpx.MockTransport(answer)).embed(
        ["x"], query=False
    )
    assert __import__("json").loads(seen[-1].content)["dimensions"] == 8


def test_every_input_is_embedded_whole_within_the_servers_limit():
    import re
    from pathlib import Path

    import httpx

    from retrieval import vectors
    from retrieval.vectors import HttpEmbedder, Profile, fitted, input_bytes

    # Both clouds' GPU machines take the inputs the code allows: MAX_INPUT_TOKENS.
    root = Path(__file__).resolve().parents[1]
    for cloud in ("gcp-terraform", "azure"):
        template = (root / "deployment" / cloud / "env" / "inference-cloud-init.yaml.tftpl").read_text()
        args = re.search(r'embedder = \{[^}]*args = "([^"]*)"', template).group(1)
        assert args == f"--max-batch-tokens {vectors.MAX_INPUT_TOKENS} --auto-truncate"
        # The reranker warms up on one input of its model's 8,192 tokens, and never truncates.
        assert (
            re.search(r'reranker = \{[^}]*args = "([^"]*)"', template).group(1) == "--max-batch-tokens 8192"
        )
    # The longest window, even in four-byte characters, fits an input whole; a longer one is refused.
    base = {"id": "p", "model": "m", "revision": "1", "dims": 8, "embedder": "http"}
    longest = (vectors.INPUT_BYTES - 2) // 4
    assert Profile.from_dict(base | {"window_chars": longest}).window_chars == longest >= vectors.WINDOW_CHARS
    with pytest.raises(ValueError, match="embedded whole"):
        Profile.from_dict(base | {"window_chars": longest + 1})
    # A window's context gives way to its words, never the words to the context.
    words = "\U0001d504" * longest  # four bytes each
    assert fitted("Act — s. 1", words) == words and input_bytes(words) <= vectors.INPUT_BYTES
    tight = fitted("Data Protection Act — s. 1", "\U0001d504" * (longest - 4))
    assert tight.startswith("Data Pr") and "…\n\n" in tight and input_bytes(tight) <= vectors.INPUT_BYTES
    assert fitted("Act — s. 1", "A person.") == "Act — s. 1\n\nA person."
    # An input the server would cut short is refused, and none of its batch is sent.
    seen = []
    embedder = HttpEmbedder(
        Profile.from_dict(base), url="http://embedder.test", transport=httpx.MockTransport(seen.append)
    )
    with pytest.raises(ValueError, match="longer than the embedder takes whole"):
        embedder.embed(["x", "y" * (vectors.INPUT_BYTES + 1)], query=False)
    assert not seen


def test_a_long_unit_is_reranked_by_its_opening_and_the_windows_with_the_query_words():
    from retrieval import rerank

    filler = "The Commission may publish guidance on any matter. " * 60  # several windows
    words = (
        "Section 42 opens here. " + filler + "A controller shall not transfer personal data abroad. " + filler
    )
    found = rerank.passages(words, {"transfer", "abroad"})
    assert len(found) == rerank.PASSAGES and found[0].startswith("Section 42")
    assert any("transfer personal data abroad" in p for p in found)
    assert all(len(p) <= rerank.MAX_CHARS for p in found)
    assert rerank.passages("A short section.", {"short"}) == ["A short section."]


def test_weak_matches_need_the_rerankers_support_and_strong_ones_never_do():
    from retrieval import rerank
    from retrieval.pack import Retriever
    from retrieval.ranking import Candidate

    def candidate(start, arms, score=None):
        return Candidate(hit(start=start), arms=dict.fromkeys(arms, 1), rerank_score=score)

    low = rerank.SUPPORT_FLOOR / 10
    kept = Retriever._supported([
        candidate(0, ["lexical"], 0.0),  # every word: never held to the floor
        candidate(10, ["citation"], 0.0),
        candidate(20, ["dense"], low),  # weak and scored low
        candidate(30, ["dense", "lexical_any"], low),
        candidate(40, ["lexical_any"], 0.9),
        candidate(50, ["dense"]),  # the reranker ran but did not score it
    ])  # fmt: skip
    assert [c.hit.start_byte for c in kept] == [0, 10, 40]
    # Without a reranker, meaning alone is evidence only when the query's words found something.
    assert [c.hit.start_byte for c in Retriever._supported([candidate(0, ["dense"])])] == []
    both = Retriever._supported([candidate(0, ["dense"]), candidate(10, ["lexical_any"])])
    assert [c.hit.start_byte for c in both] == [0, 10]


def test_a_unit_gives_the_pages_of_the_original_file_it_is_on():
    from retrieval.pack import unit_pages

    def node(id, start, end, page, page_end=None):
        return SimpleNamespace(id=id, start_byte=start, end_byte=end, page=page, page_end=page_end)

    document = node("doc", 0, 100, 1, 5)
    heading, section = node("h", 0, 10, 1), node("s", 20, 90, 3, 5)
    nodes = [document, heading, section]
    assert unit_pages(20, 90, section, nodes) == (3, 5)  # a section across pages
    assert unit_pages(0, 10, document, nodes) == (1, 1)  # the document's own words: not its last page
    assert unit_pages(0, 10, node("x", 0, 10, None), [node("x", 0, 10, None)]) is None  # not read from pages


def test_the_retrieval_job_runs_each_step_and_reports_one_that_fails(monkeypatch):
    from contextlib import contextmanager

    from api import text_index
    from retrieval import indexer, projector, worker

    calls = []

    class Database:
        @contextmanager
        def begin(self):
            yield None

    monkeypatch.setattr(text_index, "ensure", lambda conn: calls.append("ensure") or False)
    monkeypatch.setattr(projector, "application", lambda conn: "app")
    monkeypatch.setattr(projector, "application_hash", lambda app: "new")
    monkeypatch.setattr(projector, "state", lambda conn: {"application_hash": "old"})
    monkeypatch.setattr(projector, "deploy", lambda db, engine: calls.append("deploy"))

    def broken(db, engine):
        raise ConnectionError("engine down")

    monkeypatch.setattr(projector, "sync", broken)
    monkeypatch.setattr(indexer, "work", lambda db, batches, searcher: calls.append("work") or {"done": 0})
    report = worker.run(Database(), search=object())
    # The application changed, so it is deployed; the failing sync is reported; embedding still runs.
    assert calls == ["ensure", "deploy", "work"]
    assert report["engine"] == {"error": "ConnectionError"} and report["embeddings"] == {"done": 0}


def test_the_engine_analyses_each_config_as_the_section_index_does():
    # RSV-04 "same words, same matching": every language the section index stems, the engine stems in
    # the same language; a language matched word for word there is never stemmed or guessed at here,
    # whatever the text's own language.
    from api import text_index
    from retrieval.engine import LOCALES, WORD_FOR_WORD, document, locale

    assert {config: language for language, config in text_index.CONFIGS.items()} == LOCALES
    assert text_index.ANALYSES == (*LOCALES, "simple")
    assert [text_index.config_for(t) for t in ("pt-BR", "es", "AR", "sw", "rw", "de")] == [
        "portuguese", "spanish", "arabic", "simple", "simple", "simple",
    ]  # fmt: skip
    assert locale("arabic") == "ar" and locale("simple") == WORD_FOR_WORD
    assert WORD_FOR_WORD not in text_index.CONFIGS  # Kiswahili: no analysis in either
    row = {"version_id": "ver_de", "work_id": "w", "start_byte": 0, "end_byte": 9, "language": "de-CH",
           "config": "simple", "analyzer": text_index.ANALYZER, "jurisdiction_id": "ng",
           "document_type": "act", "legal_status": "current", "content_hash": "0" * 64}  # fmt: skip
    unit = document(row, "Gesetze", "", "", "")
    assert unit["language"] == "de" and unit["locale"] == WORD_FOR_WORD  # filtered as German, never stemmed
    assert unit["analyzer"] == text_index.ANALYZER
    assert document(row | {"language": "pt", "config": "portuguese"}, "", "", "", "")["locale"] == "pt"


def test_the_engine_schema_and_queries_carry_each_analysis_language():
    import json

    import httpx

    from api.text_index import Filters
    from retrieval.engine import WORD_FOR_WORD, Application, Engine

    schema = Application().schema()
    fields = [line.split()[1] for line in schema.splitlines() if line.strip().startswith("field ")]
    assert fields[0] == "locale" and "set_language" in schema.split("field language")[0]
    assert "set_language" not in schema.split("field language")[1].split("}")[0]
    asked = []

    def answer(request):
        asked.append(json.loads(request.content))
        return httpx.Response(200, json={"root": {"fields": {"totalCount": 0}}})

    engine = Engine("http://engine.test", transport=httpx.MockTransport(answer))
    terms = {"arabic": "موظف", "portuguese": "trabalhador", "simple": "mikataba", "spanish": ""}
    engine.lexical(terms, Filters(), 10)
    yql = asked[-1]["yql"]
    assert 'config contains "arabic" and ({grammar: "all", language: "ar"}userInput(@q0))' in yql
    assert 'config contains "portuguese" and ({grammar: "all", language: "pt"}userInput(@q1))' in yql
    simple = f'config contains "simple" and ({{grammar: "all", language: "{WORD_FOR_WORD}"}}userInput(@q2))'
    assert simple in yql and "spanish" not in yql  # no word the Spanish analysis indexes
    engine.lexical({"simple": "haki"}, Filters(), 10, every=False)
    assert f'language: "{WORD_FOR_WORD}"' in asked[-1]["yql"] and "weakAnd" in asked[-1]["yql"]


def test_a_new_analyzer_is_a_new_application_and_reanalyses_held_units(monkeypatch):
    # Bumping the analyzer redeploys and reconciles every work (retrieval.worker); units fed under the
    # old one are analysed again by partial update, keeping their vectors; new texts are fed whole.
    from contextlib import contextmanager

    from api import text_index
    from retrieval import projector
    from retrieval.engine import Application

    before = projector.application_hash(Application())
    monkeypatch.setattr(text_index, "ANALYZER", "oa-search-test")
    assert projector.application_hash(Application()) != before

    class Database:
        @contextmanager
        def begin(self):
            yield None

    class Engine:
        def __init__(self):
            self.calls = []

        def visit(self, selection, fields):
            assert fields == "version_id,analyzer"
            return [
                {"version_id": "ver_old", "analyzer": "oa-search-4"},
                {"version_id": "ver_same", "analyzer": "oa-search-test"},
                {"version_id": "ver_mixed", "analyzer": "oa-search-test"},
                {"version_id": "ver_mixed"},  # fed before units carried their analyzer
                {"version_id": "ver_gone", "analyzer": "oa-search-test"},
            ]

        def put(self, doc):
            self.calls.append(("put", doc["unit_id"]))

        def reanalyse(self, doc):
            self.calls.append(("reanalyse", doc["unit_id"]))

        def delete_version(self, version_id):
            self.calls.append(("delete", version_id))
            return 1

    wanted = {v: [{"unit_id": f"{v}:0"}] for v in ("ver_old", "ver_same", "ver_mixed", "ver_new")}
    monkeypatch.setattr(projector, "_documents", lambda conn, work_id: wanted)
    engine = Engine()
    report = projector.reconcile(Database(), engine, ["w"])
    assert engine.calls == [
        ("delete", "ver_gone"), ("reanalyse", "ver_mixed:0"), ("put", "ver_new:0"), ("reanalyse", "ver_old:0"),
    ]  # fmt: skip
    assert report == {"works": 1, "fed": 1, "removed": 1, "reanalysed": 2}


def test_reanalysing_a_unit_assigns_every_field_and_creates_a_missing_one():
    import json

    import httpx

    from retrieval.engine import Engine

    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, json={})

    Engine("http://engine.test", transport=httpx.MockTransport(answer)).reanalyse(
        {"unit_id": "ver_a:0", "locale": "pt", "words": "Os trabalhadores"}
    )
    [request] = seen
    assert request.method == "PUT" and request.url.path == "/document/v1/oa/unit/docid/ver_a:0"
    assert request.url.params["create"] == "true"
    assert json.loads(request.content) == {
        "fields": {
            "unit_id": {"assign": "ver_a:0"},
            "locale": {"assign": "pt"},
            "words": {"assign": "Os trabalhadores"},
        }
    }


def test_each_language_matched_word_for_word_has_a_notice():
    from api.text_index import word_for_word

    assert word_for_word(["en", "fr", "pt", "es", "ar"]) == []
    assert word_for_word(["ar", "sw", "rw-RW"]) == [
        "Texts in sw are matched word for word (no stemming or common words) until the language passes the "
        "search tests.",
        "Texts in rw-RW are matched word for word (no stemming or common words) until the language passes "
        "the search tests.",
    ]
