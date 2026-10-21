"""The retrieval service (RSV-02, RSV-03, RSV-05–08): document units follow publication; /retrieve
returns a deterministic evidence pack whose units carry exact words, hashes, currentness, derived links
and the explanation of their position, within the byte budget; /currentness confirms cited parts."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_v1_text import RULE, STATED, UNITS, WORK, publish_act, publish_editions, withdraw

from api.main import create_app
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


def documents(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "document_unit")
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {table}")).mappings()]


def retrieve(client, q, **params):
    answer = client.get("/retrieve", params={"q": q, **params})
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_each_published_text_has_one_document_unit_and_a_withdrawn_one_none(store):
    _, version, nodes = publish_act(store, UNITS)
    [unit] = documents(store)
    raw = version.canonical_text.encode()
    assert unit["version_id"] == version.id and unit["work_id"] == WORK
    assert raw[unit["start_byte"] : unit["end_byte"]].decode() == "ENACTED by the National Assembly —"
    assert "example act 2023" in unit["names"] and "example act" in unit["names"]
    withdraw(store)
    assert documents(store) == []


def test_a_topic_query_answers_an_evidence_pack(store, client):
    _, version, nodes = publish_act(store, UNITS)
    raw = version.canonical_text.encode()
    pack = retrieve(client, "repaid from salary")
    assert pack["schema_version"] == "oa-evidence-1" and pack["status"] == "results"
    assert pack["query"]["routes"] == ["topic"] and pack["revisions"]["ranking"] == "oa-rank-3"
    assert pack["revisions"]["router"] == "oa-route-2" and pack["revisions"]["dataset"].count(":") == 1
    first = pack["evidence"][0]
    assert first["anchor_id"] == nodes["sec_1"].id and first["unit"] == "provision"
    assert first["citation"] == "Example Act, 2023, s. 1" and first["complete"] is True
    span = raw[first["start_byte"] : first["end_byte"]]
    assert first["text"] == span.decode() and first["span_hash"] == hashlib.sha256(span).hexdigest()
    assert first["expression_id"] == f"{WORK}:en" and first["content_hash"] == version.content_hash
    # The record states nothing of its editions: unknown, never taken from the text's being found.
    assert first["expression_authority"] == "unknown" and "expression_authority_evidence" not in first
    # Where to check it and read it: the original file's fingerprint, this part by the API, and the
    # record on the website at this part (the website shows the record's one text).
    assert first["original_sha256"] == version.raw_hash and first["retrieved_at"]
    assert first["links"] == {
        "web": f"https://open.alvary.ai/sources/{WORK}#{nodes['sec_1'].id}",
        "api": f"https://api.open.alvary.ai/api/v1/anchors/{nodes['sec_1'].id}",
        "text": f"https://api.open.alvary.ai/api/v1/versions/{version.id}/text",
    }
    assert first["currentness"]["basis"] == "record" and first["currentness"]["state"] == "unknown"
    assert first["authority"] == {
        "document_type": "act",
        "instrument_rank": 1,
        "court_relation": "not_applicable",
    }
    ranking = first["ranking"]
    assert ranking["position"] == 1 and ranking["arms"]["lexical"] == 1 and ranking["profile"] == "oa-rank-3"
    # The definition in force for a term the section uses travels with it, with its exact words.
    [fund] = first["context"]["definitions"]
    assert fund["term"] == "Fund" and fund["anchor_id"] == nodes["sec_2__def_fund"].id
    assert raw[fund["start_byte"] : fund["end_byte"]].decode() == fund["text"]
    assert first["context"]["heading_path"] == ["1. Loans"]
    assert pack["bytes"]["unique"] == len(span) + len(fund["text"].encode()) <= pack["bytes"]["budget"]
    assert "dense:not_configured" in pack["coverage"]["degraded"]
    # Deterministic: the same request over the same revisions gives the same pack.
    assert retrieve(client, "repaid from salary") == pack


def test_each_unit_carries_its_editions_authority_as_the_record_states_it(store, client, tmp_path):
    english, french = publish_editions(store)
    [unit] = retrieve(client, "repaid from salary", language="en", max_units=1)["evidence"]
    assert unit["version_id"] == english.id and unit["expression_id"] == f"{WORK}:en"
    assert unit["expression_authority"] == "authoritative" and unit["expression_authority_evidence"] == STATED
    [unit] = retrieve(client, "Le Fonds prête", language="fr", max_units=1)["evidence"]
    assert unit["version_id"] == french.id and unit["expression_authority"] == "unknown"
    assert "expression_authority_evidence" not in unit
    # Stored and served as recorded on PostgreSQL too.
    page = client.get(f"/sources/{WORK}/expressions").json()
    assert [(e["language"], e["authority"]) for e in page["items"]] == [
        ("en", "authoritative"),
        ("fr", "unknown"),
    ]
    assert page["language_rule"] == RULE
    body = {"name": "list_expressions", "arguments": {"source_id": WORK}}
    answer = TestClient(create_app(store, tmp_path)).post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": body}
    )
    assert json.loads(answer.json()["result"]["content"][0]["text"]) == page


def test_routes_for_an_instrument_a_definition_and_a_citation(store, client):
    _, version, nodes = publish_act(store, UNITS)
    named = retrieve(client, "Example Act")
    assert named["query"]["routes"] == ["instrument"]
    assert (
        named["evidence"][0]["unit"] == "document" and "document unit" in named["evidence"][0]["warnings"][0]
    )
    assert named["evidence"][0]["ranking"]["signals"][0]["name"] == "instrument_match"
    defined = retrieve(client, 'meaning of "Fund"')
    assert "definition" in defined["query"]["routes"]
    assert defined["evidence"][0]["anchor_id"] == nodes["sec_2__def_fund"].id
    cited = retrieve(client, "Example Act, 2023, s. 1(2)(b)")
    assert cited["query"]["routes"][0] == "citation"
    first = cited["evidence"][0]
    assert (
        first["anchor_id"] == nodes["sec_1__subsec_2__para_b"].id
        and first["ranking"]["arms"]["citation"] == 1
    )
    historical = retrieve(client, "salary", as_of="2024-01-01")
    assert "historical" in historical["query"]["routes"]


def test_a_long_unit_is_an_excerpt_within_the_byte_budget(store, client):
    filler = " ".join(f"word{n}" for n in range(1200))
    units = [
        {"kind": "heading", "text": "LONG ACT, 2023"},
        {
            "kind": "paragraph",
            "text": f"1. (1) {filler} the levy is payable monthly {filler}.",
            "notes": ["Levy"],
        },
    ]
    _, version, nodes = publish_act(store, units)
    raw = version.canonical_text.encode()
    pack = retrieve(client, "levy payable monthly", max_evidence_bytes=4096, include="ranking")
    [unit] = [u for u in pack["evidence"] if u["unit"] == "provision"]
    assert unit["complete"] is False and "excerpt" in unit["warnings"][0]
    assert raw[unit["text_start_byte"] : unit["text_end_byte"]].decode() == unit["text"]
    assert "levy is payable monthly" in unit["text"] and len(unit["text"].encode()) <= 4096
    assert "context" not in unit and pack["bytes"]["unique"] <= 4096


def test_a_withdrawn_text_vanishes_from_retrieval_and_the_check(store, client):
    _, version, nodes = publish_act(store, UNITS)
    anchor = nodes["sec_1"]
    raw = version.canonical_text.encode()
    span_hash = hashlib.sha256(raw[anchor.start_byte : anchor.end_byte]).hexdigest()
    check = client.post("/currentness", json={"items": [{"anchor_id": anchor.id, "span_hash": span_hash}]})
    assert check.status_code == 200, check.text
    [result] = check.json()["results"]
    assert result["result"] == "unchanged" and result["currentness"]["basis"] == "record"
    withdraw(store)
    assert retrieve(client, "salary")["evidence"] == []
    [gone] = client.post("/currentness", json={"items": [{"anchor_id": anchor.id}]}).json()["results"]
    assert gone == {"index": 0, "result": "unavailable", "anchor_id": anchor.id}


def test_the_check_finds_newer_texts_and_wrong_hashes(store, client):
    _, version, nodes = publish_act(store, UNITS)
    corrected = [dict(u) for u in UNITS]
    corrected[5] = {"kind": "list_item", "text": "(b) from wages — café, naïve, 中文."}
    _, newer, newer_nodes = publish_act(store, corrected, record=False)
    assert newer.prior_version_id == version.id
    answer = client.post(
        "/currentness",
        json={
            "items": [
                {"anchor_id": nodes["sec_1"].id},
                {"version_id": version.id, "locator": "sec_2"},
                {"anchor_id": nodes["sec_2"].id, "span_hash": "0" * 64},
                {"anchor_id": "nod_no-such-part"},
            ]
        },
    )
    assert answer.status_code == 200, answer.text
    first, second, wrong, missing = answer.json()["results"]
    assert first["result"] == "newer_version" and first["newer_version_id"] == newer.id
    assert first["newer_anchor_id"] == newer_nodes["sec_1"].id and first["same_words"] is False
    assert second["result"] == "newer_version" and second["same_words"] is True
    assert wrong["result"] == "span_mismatch" and missing["result"] == "unavailable"
    assert client.post("/currentness", json={"items": []}).status_code == 400
    assert client.post("/currentness", json={"items": [{"locator": "sec_1"}]}).status_code == 400


def test_mcp_answers_the_same_pack_and_check_as_rest(store, client, tmp_path):
    _, version, nodes = publish_act(store, UNITS)
    mcp = TestClient(create_app(store, tmp_path))

    def call(name, arguments):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        answer = mcp.post("/mcp", json=body).json()
        assert "result" in answer and not answer["result"].get("isError"), answer
        return json.loads(answer["result"]["content"][0]["text"])

    arguments = {"query": "repaid from salary", "max_units": 5, "max_evidence_bytes": 8192}
    rest = retrieve(client, "repaid from salary", max_units=5, max_evidence_bytes=8192)
    assert call("retrieve_evidence", arguments) == rest
    items = [{"anchor_id": nodes["sec_1"].id}]
    assert (
        call("check_currentness", {"items": items})
        == client.post("/currentness", json={"items": items}).json()
    )


LOANS_WORK = "ng-student-loans-access-act-2024"
LOANS_PAGES = [
    {"page": 1, "units": [
        {"kind": "heading", "text": "STUDENT LOANS ACCESS ACT, 2024"},
        {"kind": "heading", "text": "PART I"},
        {"kind": "paragraph", "text": "1. A borrower may defer repayment while unemployed.", "notes": ["Deferral"]},
        {"kind": "paragraph", "text": "2. A deferral ends when the borrower is employed.", "notes": ["End"]},
    ]},
    {"page": 2, "units": [
        {"kind": "heading", "text": "PART II"},
        {"kind": "paragraph", "text": "3. The Fund shall publish its accounts every year.", "notes": ["Accounts"]},
    ]},
]  # fmt: skip


def test_citations_as_people_write_them_pages_and_addresses_find_their_parts(store, client):
    _, version, nodes = publish_act(store, None, work=LOANS_WORK, title="Student Loans Access Act, 2024",
                                    pages=LOANS_PAGES)  # fmt: skip
    for query in (
        "section 2 of the Student Loans Access Act",  # the title without its year
        "SLAA s. 2",  # its initials
        "s. 2 Student Loans Access Act 2024",  # the pinpoint first
    ):
        pack = retrieve(client, query)
        assert pack["query"]["routes"][0] == "citation", query
        assert pack["evidence"][0]["anchor_id"] == nodes["sec_2"].id, query
    # A Part: its heading, then its provisions in order, each cited by its own number.
    part = retrieve(client, "Part I of the SLAA")
    assert [u["pinpoint"] for u in part["evidence"][:3]] == ["Part I", "s. 1", "s. 2"]
    # A page of the official file: the provisions on it, with their pages.
    paged = retrieve(client, "page 2 of the Student Loans Access Act")
    assert paged["query"]["routes"][0] == "page"
    assert [u["pinpoint"] for u in paged["evidence"][:2]] == ["Part II", "s. 3"]
    assert paged["evidence"][1]["page_start"] == paged["evidence"][1]["page_end"] == 2
    # A pinpoint alone, within the record the request is limited to.
    alone = retrieve(client, "s. 3", source_id=LOANS_WORK)
    assert (
        alone["query"]["routes"][0] == "citation" and alone["evidence"][0]["anchor_id"] == nodes["sec_3"].id
    )
    # Addresses: an Open Alvary page at a part, an API anchor, and the text's official address.
    for address in (
        f"https://open.alvary.ai/sources/{LOANS_WORK}#{nodes['sec_3'].id}",
        f"https://api.open.alvary.ai/api/v1/anchors/{nodes['sec_3'].id}",
    ):
        linked = retrieve(client, address)
        assert (
            linked["query"]["routes"] == ["link"] and linked["evidence"][0]["anchor_id"] == nodes["sec_3"].id
        )
    official = retrieve(client, str(version.canonical_url))
    assert official["query"]["routes"] == ["link"] and official["evidence"][0]["unit"] == "document"
    assert retrieve(client, "https://example.org/not-a-law.pdf")["evidence"] == []
