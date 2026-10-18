"""OA-S1, MIN-02 and D15: the section index follows what is public in the transaction that changes the
catalogue, and the text search answers ranked passages with exact snippets, telling no result apart
from a scope with no published text and from a search that could not run."""

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_v1_text import UNITS, WORK, publish_act, withdraw

from api import text_index
from api.catalogue import LegacyCatalogue
from api.store import Store
from api.v1 import create_v1_app
from changes.record import namespace

pytestmark = pytest.mark.postgres


@pytest.fixture
def store(pg_engine, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    return Store(pg_engine)


@pytest.fixture
def client(store, tmp_path):
    return TestClient(create_v1_app(store, tmp_path, lambda: []))


def units(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "text_unit")
        return [
            dict(row)
            for row in conn.execute(
                text(
                    f"SELECT version_id, start_byte, end_byte, node_id, kind, locator, pinpoint, legal_status,"
                    f" language, config FROM {table} ORDER BY version_id, start_byte"
                )
            ).mappings()
        ]


def search(client, q, **params):
    answer = client.get("/search/text", params={"q": q, **params})
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_a_published_text_is_indexed_by_provision_and_a_withdrawn_one_is_not(store):
    _, version, nodes = publish_act(store, UNITS)
    found = units(store)
    assert {u["version_id"] for u in found} == {version.id} and {u["config"] for u in found} == {"english"}
    assert [u["pinpoint"] for u in found if u["pinpoint"]] == [
        "s. 1",
        "s. 2",
        "First Schedule",
        "First Schedule, para. 1",
    ]
    assert all(
        a["end_byte"] <= b["start_byte"] for a, b in zip(found, found[1:], strict=False)
    )  # never overlapping
    assert nodes["sec_1"].id in {u["node_id"] for u in found}
    withdraw(store)
    assert units(store) == []  # the same transaction that took the text out of view


def test_a_search_answers_ranked_passages_with_exact_snippets(store, client):
    _, version, nodes = publish_act(store, UNITS)
    raw = version.canonical_text.encode()
    page = search(client, "salary")
    assert page["status"] == "results" and page["total"] == 1 and page["total_exact"] is True
    [hit] = page["items"]
    assert hit["anchor_id"] == nodes["sec_1"].id and hit["citation"] == "Example Act, 2023, s. 1"
    assert hit["matched_by"] == "text" and 0 < hit["score"] <= 1 and hit["legal_status"] == "unknown"
    assert raw[hit["snippet_start_byte"] : hit["snippet_end_byte"]].decode() == hit["snippet"]
    assert [raw[a:b].decode() for a, b in hit["highlights"]] == ["salary"]
    assert hit["start_byte"] <= hit["snippet_start_byte"] <= hit["snippet_end_byte"] <= hit["end_byte"]
    assert hit["official_url"] == "https://www.nass.gov.ng/example-act.pdf" and hit["licence"].startswith(
        "Not"
    )
    assert page["coverage"] == {
        "records_in_scope": 1, "records_with_text": 1, "texts_searched": 1,
        "notice": "Only published texts are searched: 1 of the 1 records in this scope have one. Find records "
        "without a published text with /search.",
    }  # fmt: skip
    assert page["analysis"] == {"en": "english"} and page["ranking"] == "oa-rank-1" and page["index_built_at"]
    assert search(client, "lends")["items"][0]["anchor_id"] == nodes["sec_1"].id  # English stemming
    assert (
        search(client, '"from salary"')["total"] == 1
        and search(client, "salary -café")["status"] == "no_result"
    )
    assert search(client, "zebra")["status"] == "no_result"
    common = search(client, "the")
    assert common["status"] == "no_result" and "common words" in common["notices"][0]
    assert search(client, "salary", jurisdiction="rw")["status"] == "unsupported_scope"
    unsupported = search(client, "salary", type="regulation")
    assert unsupported["status"] == "unsupported_scope" and unsupported["coverage"]["texts_searched"] == 0
    # Golden queries (OA-S1): a statute by its short title, a section by number, a concept phrase.
    assert search(client, "Example Act")["items"][0]["kind"] == "document"  # its long title comes first
    assert nodes["sec_2"].id in [h["anchor_id"] for h in search(client, "Example Act section 2")["items"][:3]]
    assert search(client, "students fund lend")["items"][0]["anchor_id"] == nodes["sec_1"].id
    assert client.get("/search/text", params={"q": "salary", "cursor": "bogus"}).status_code == 400
    assert client.get("/search/text", params={"q": "x" * 2001}).status_code == 400


def test_a_citation_of_a_part_comes_first_and_is_not_repeated(store, client):
    _, version, nodes = publish_act(store, UNITS)
    page = search(client, "Example Act, 2023, s. 1(2)(b)")
    first, *rest = page["items"]
    assert first["matched_by"] == "citation" and first["anchor_id"] == nodes["sec_1__subsec_2__para_b"].id
    assert first["score"] is None and first["citation"] == "Example Act, 2023, s. 1(2)(b)"
    raw = version.canonical_text.encode()
    assert raw[first["snippet_start_byte"] : first["snippet_end_byte"]].decode() == first["snippet"]
    assert page["citation"]["provision"]["status"] == "resolved" and page["total"] == len(page["items"])
    assert all((h["start_byte"], h["end_byte"]) != (first["start_byte"], first["end_byte"]) for h in rest)
    second = search(client, "Example Act, 2023, s. 1(2)(b)", limit=1)
    assert [h["matched_by"] for h in second["items"]] == ["citation"] and second["next_cursor"]
    after = search(client, "Example Act, 2023, s. 1(2)(b)", limit=1, cursor=second["next_cursor"])
    assert after["items"][0]["matched_by"] == "text"


def test_pages_continue_without_repeating_a_passage(store, client):
    publish_act(store, UNITS)
    seen, cursor = [], None
    while True:
        page = search(client, "fund", limit=1, **({"cursor": cursor} if cursor else {}))
        seen += [(h["version_id"], h["start_byte"]) for h in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == page["total"] >= 2


def test_a_correction_of_the_record_reaches_the_index_and_a_repealed_text_scores_less(store, client):
    publish_act(store, UNITS)
    before = search(client, "salary")["items"][0]["score"]
    corpus = store.load()
    source = next(s for s in corpus.sources if s.id == WORK)
    source.title, source.legal_status = "Students Loans (Access) Act, 2023", "repealed"
    store.save(corpus)
    assert {u["legal_status"] for u in units(store)} == {"repealed"}
    renamed = search(client, "access salary")  # the new title, from the corrected record
    assert renamed["total"] == 1 and renamed["items"][0]["title"] == "Students Loans (Access) Act, 2023"
    assert search(client, "salary")["items"][0]["score"] == pytest.approx(
        before * text_index.NOT_IN_FORCE, rel=0.01
    )
    assert search(client, "salary", status="current")["status"] == "unsupported_scope"
    assert search(client, "salary", status="repealed")["total"] == 1


def test_the_final_schedule_of_a_long_statute_ranks_first_with_an_exact_excerpt(store, client):
    # MIN-02's acceptance: more than 16,383 words, and the final schedule is the only matching unit.
    sections = [
        {"kind": "paragraph", "text": f"{n}. The Minister may by order make provision for any matter under this "
         f"section {n} of the Act."}
        for n in range(1, 1000)  # the structure rules number sections up to 999
    ]  # fmt: skip
    schedule = [
        {"kind": "heading", "text": "SCHEDULES"},
        {"kind": "heading", "text": "First Schedule"},
        {"kind": "list_item", "marker": "1.", "text": "The quokka register is kept at the Lagos office."},
    ]
    _, version, nodes = publish_act(store, sections + schedule)
    words = len(version.canonical_text.split())
    assert words > 16_383 and all(
        u["end_byte"] - u["start_byte"] <= text_index.MAX_UNIT_BYTES for u in units(store)
    )
    started = time.perf_counter()
    page = search(client, "quokka")
    elapsed = time.perf_counter() - started
    assert page["total"] == 1 and page["items"][0]["anchor_id"] == nodes["sch_1__para_1"].id
    hit = page["items"][0]
    raw = version.canonical_text.encode()
    assert raw[hit["snippet_start_byte"] : hit["snippet_end_byte"]].decode() == hit["snippet"]
    assert "quokka" in hit["snippet"] and hit["citation"] == "Example Act, 2023, First Schedule, para. 1"
    busy = search(client, "minister order provision")
    assert busy["total"] == 999 and busy["next_cursor"] and elapsed < 3


def test_a_french_text_is_searched_in_french_and_other_languages_word_for_word(store, client):
    # The French search tests (MIN-02, LNG-01): verb and plural forms find each other, common words
    # alone find nothing, and accents are kept as written.
    publish_act(store, UNITS)
    french = [
        {"kind": "paragraph", "text": "1. Le Fonds prête aux étudiants inscrits dans les universités."},
        {"kind": "paragraph", "text": "2. Les données personnelles sont traitées loyalement."},
    ]
    publish_act(store, french, work="ng-loi-exemple-2023", title="Loi exemple, 2023", language="fr")
    page = search(client, "prêter")
    assert page["items"][0]["language"] == "fr" and page["analysis"] == {"en": "english", "fr": "french"}
    assert not any("Texts in fr" in n for n in page["notices"])  # French is no longer word for word
    assert search(client, "étudiant inscrit")["total"] == 1  # singular finds plural
    assert search(client, "donnée personnelle traiter")["items"][0]["citation"] == "Loi exemple, 2023, s. 2"
    common = search(client, "le la des du", language="fr")
    assert common["status"] == "no_result" and "common words" in common["notices"][0]
    assert search(client, "etudiants", language="fr")["status"] == "no_result"  # accents are kept
    kinyarwanda = [{"kind": "paragraph", "text": "1. Ikigega gitanga inguzanyo ku banyeshuri."}]
    publish_act(store, kinyarwanda, work="ng-itegeko-2023", title="Itegeko, 2023", language="rw")
    other = search(client, "inguzanyo")
    assert other["analysis"]["rw"] == "simple" and any(
        "Texts in rw are matched word for word" in n for n in other["notices"]
    )


def test_a_date_searches_only_texts_in_force_on_it(store, client):
    publish_act(store, UNITS, effective_from=date(2023, 6, 1))
    assert search(client, "salary", as_of="2024-01-01")["total"] == 1
    assert search(client, "salary", as_of="2020-01-01")["status"] == "unsupported_scope"


def test_an_index_not_yet_built_says_so_and_the_worker_builds_it(store, client):
    publish_act(store, UNITS)
    with store.engine.begin() as conn:
        state = namespace(conn).qualified(conn, "changes", "text_index_state")
        conn.execute(text(f"UPDATE {state} SET built = false"))
    page = search(client, "salary")
    assert page["status"] == "degraded" and page["items"] == [] and "being built" in page["notices"][0]
    with store.engine.begin() as conn:
        assert text_index.ensure(conn) is True and text_index.ensure(conn) is False
    assert search(client, "salary")["status"] == "results"


def test_a_text_withdrawn_while_the_answer_is_sent_is_refused(store, client, monkeypatch):
    publish_act(store, UNITS)
    monkeypatch.setattr(LegacyCatalogue, "readable", lambda self, version_id: None)
    refused = client.get("/search/text", params={"q": "salary"})
    assert refused.status_code == 503 and refused.json()["code"] == "temporarily_unavailable"


def test_the_mcp_tools_search_texts_and_resolve_a_pinpoint(store, tmp_path):
    from api.main import create_app

    _, _, nodes = publish_act(store, UNITS)
    client = TestClient(create_app(store, tmp_path))

    def call(name, arguments):
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {"name": name, "arguments": arguments},
        }
        return client.post("/mcp", json=body).json()["result"]

    found = call("search_text", {"query": "salary", "limit": 3})
    assert not found["isError"] and nodes["sec_1"].id in found["content"][0]["text"]
    resolved = call("resolve_citation", {"citation": "Example Act, 2023, s. 1(2)(b)"})
    assert not resolved["isError"] and nodes["sec_1__subsec_2__para_b"].id in resolved["content"][0]["text"]
    invalid = {"name": "search_text", "arguments": {"query": "salary", "as_of": "yesterday"}}
    answer = client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "id": 2, "params": invalid})
    assert answer.json()["error"]["code"] == -32602


def test_the_integration_acceptance_checks_pass_on_a_published_text(store, client, capsys):
    from operations import acceptance

    publish_act(store, UNITS)
    golden = [
        {"query": "salary", "expect": ["s. 1"]},
        {"query": "Lagos office", "expect": ["First Schedule, para. 1"]},
    ]
    report = acceptance.run(client, "Example Act, 2023, s. 1(2)(b)", golden)
    assert not report.failed and [r[1] for r in report.results].count("PASS") == 10
    missing = acceptance.run(client, "Example Act, 2023, s. 9", golden)
    assert missing.failed and missing.results[0][:2] == ("OA-S2 provision lookup", "FAIL")
