"""The retrieval service (RSV-02, RSV-03, RSV-05–08): document units follow publication; /retrieve
returns a deterministic evidence pack whose units carry exact words, hashes, currentness, derived links
and the explanation of their position, within the byte budget; /currentness confirms cited parts."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_v1_text import RULE, STATED, UNITS, WORK, publish_act, publish_editions, withdraw

from api import courts
from api.main import create_app
from api.store import Store
from api.v1 import create_v1_app
from changes.record import namespace
from operations import courts as review_cli
from operations import publish
from schema.models import Authority, Corpus, Jurisdiction, Language, RightsRecord, Source
from workbench import publication, structure

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
    assert pack["revisions"]["router"] == "oa-route-3" and pack["revisions"]["dataset"].count(":") == 1
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


def test_a_weak_etag_sent_back_as_it_came_answers_304(store, client):
    """An evidence pack's and a check's tags are weak (W/"..."): sent back as If-None-Match as they came,
    or without W/, an unchanged answer is 304, by the weak comparison of RFC 9110 (G13)."""
    _, version, nodes = publish_act(store, UNITS)
    params = {"q": "repaid from salary"}
    pack = client.get("/retrieve", params=params)
    tag = pack.headers["etag"]
    assert tag.startswith('W/"')
    for offered in (tag, tag.removeprefix("W/"), f'"other", {tag}'):
        again = client.get("/retrieve", params=params, headers={"If-None-Match": offered})
        assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == tag, offered
    body = {"items": [{"anchor_id": nodes["sec_1"].id}]}
    checked = client.post("/currentness", json=body)
    unchanged = client.post("/currentness", json=body, headers={"If-None-Match": checked.headers["etag"]})
    assert unchanged.status_code == 304
    assert client.get("/retrieve", params=params, headers={"If-None-Match": 'W/"other"'}).status_code == 200


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


def test_a_pack_says_which_languages_it_matched_word_for_word(store, client):
    # RSV-03 and MIN-02: a language without an analysis (Kiswahili) is matched word for word and the
    # pack says so; an analysed one (Portuguese) is matched by its stems without the notice, and the
    # analyzer that did it is in the pack's revisions.
    kiswahili = [{"kind": "paragraph", "text": "1. Wafanyakazi wana haki ya mikataba ya maandishi."}]
    _, _, nodes = publish_act(store, kiswahili, work="ng-sheria-mfano-2023", title="Sheria ya Mfano, 2023",
                              language="sw")  # fmt: skip
    pack = retrieve(client, "haki ya mikataba")
    assert pack["evidence"][0]["anchor_id"] == nodes["sec_1"].id
    assert pack["revisions"]["analyzer"] == "oa-search-5" and pack["coverage"]["notices"] == [
        "Texts in sw are matched word for word (no stemming or common words) until the language passes "
        "the search tests."
    ]
    assert not [u for u in retrieve(client, "mkataba")["evidence"] if u["unit"] == "provision"]
    portuguese = [{"kind": "paragraph", "text": "1. Os trabalhadores têm direito a contratos escritos."}]
    _, _, nodes = publish_act(store, portuguese, work="ng-lei-exemplo-2023", title="Lei exemplo, 2023",
                              language="pt")  # fmt: skip
    stemmed = retrieve(client, "trabalhador contrato", language="pt")
    assert stemmed["evidence"][0]["anchor_id"] == nodes["sec_1"].id
    assert not any("Texts in pt" in n for n in stemmed["coverage"]["notices"])


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


# --- a decision's court (CPR-04) ----------------------------------------------------------------------

COURT_SOURCE = {"id": "act", "title": "Synthetic Judicature Act", "url": "https://example.org/judicature.pdf"}
WORDS = [{"source": "act", "provision": "s. 1", "quote": "Synthetic words of the Act."}]
# Test data only, reviewed as reviewers would with operations.courts: a real register is entered from its
# official sources (ingestion/courts).
KE_REGISTER = {
    "jurisdiction": "ke",
    "precedent": {"binds": True, "note": "Synthetic: higher courts bind lower ones.", "sources": ["act"]},
    "courts": [
        {"id": "ke-hc", "name": "High Court of Kenya", "level": "superior", "sources": ["act"]},
        {"id": "ke-mc", "name": "Magistrates' Courts", "level": "subordinate", "appeals_to": ["ke-hc"],
         "sources": ["act"]},
    ],
    "relations": [{"id": "ke-hc-on-ke-mc", "court": "ke-hc", "bound": "ke-mc", "relation": "binding",
                   "rests_on": WORDS, "sources": ["act"]}],
    "sources": [COURT_SOURCE],
}  # fmt: skip
OHADA_REGISTER = {
    "jurisdiction": "aa-ohada",
    "precedent": {"binds": True, "note": "Synthetic: the treaty gives rulings that force.", "sources": ["act"]},
    "courts": [{"id": "aa-ohada-ccja", "name": "Common Court of Justice and Arbitration", "level": "regional",
                "sources": ["act"]}],
    "matters": [{"id": "uniform-acts", "name": "The Uniform Acts and regulations", "sources": ["act"]}],
    "relations": [{"id": "ccja-on-members", "court": "aa-ohada-ccja", "members": True, "matter": "uniform-acts",
                   "relation": "binding", "rests_on": WORDS, "sources": ["act"]}],
    "members": [{"id": "member-sn", "state": "sn", "sources": ["act"]}],
    "member_routes": [{"id": "route-cassation", "court": "aa-ohada-ccja", "kind": "cassation",
                       "matter": "uniform-acts", "sources": ["act"]}],
    "sources": [COURT_SOURCE],
}  # fmt: skip


def reviewed(register: dict, doctrinal: bool = True) -> dict:
    """Every structural entry reviewed by the owner and (with ``doctrinal``) every relation and precedent rule
    by an advocate qualified there, as with operations.courts."""
    on = date(2026, 10, 1)
    data = review_cli.review_structural(register, "Test Owner", "owner", on)
    if doctrinal:
        for entry in courts.CourtRegister.model_validate(data).entries():
            if entry.LEVEL == "doctrinal":
                data = review_cli.review(
                    data, entry.id, "Test Advocate", "advocate", "doctrinal", on, "Advocate"
                )
    return data


def publish_judgment(store, monkeypatch, jurisdiction, court, court_name, work, citation, paragraphs):
    """A judgment of ``court`` published through operations.publish, as tests/test_judgment_structure.
    Judgment structure rules are written for Nigeria only (TXP-03): the test lends them to this
    jurisdiction."""
    monkeypatch.setitem(structure.RULES, (jurisdiction, "judgment"), structure.JUDGMENT)
    store.save(
        Corpus(
            jurisdictions=[Jurisdiction(id=jurisdiction, name=jurisdiction, country_code=jurisdiction[:2].upper(),
                                        languages=["en"])],
            authorities=[Authority(id=court, jurisdiction_id=jurisdiction, name=court_name, kind="court",
                                   official_url="https://example.org/court/")],
            languages=[Language(id="en", name="English", tag="en")],
            sources=[Source(id=work, jurisdiction_id=jurisdiction, authority_id=court, title=citation,
                            document_type="judgment", canonical_url=f"https://example.org/court/{work}.pdf",
                            citation=citation, language="en")],
            rights=[RightsRecord(id=f"rights-{work}", source_id=work, metadata_public=True,
                                 metadata_basis="Independently authored catalogue entry")],
        )
    )  # fmt: skip
    now = datetime.now(UTC).replace(microsecond=0)
    decision = {
        "document_types": ["judgment"], "rights_holder": "Synthetic test rights holder",
        "licence": "Synthetic test decision", "licence_url": None, "attribution_required": False,
        "attribution_text": None, "database_rights_notes": "None claimed.", "privacy_status": "cleared",
        "basis": "Synthetic test judgment.", "evidence": ["https://example.org/decision"],
        "decided_by": "Test Reviewer", "decided_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=365)).isoformat(),
    }  # fmt: skip
    units = [{"kind": "heading", "text": "JUDGMENT"}] + [{"kind": "paragraph", "text": p} for p in paragraphs]
    text = publication.build({"id": work, "jurisdiction": jurisdiction, "type": "judgment"},
                             [{"page": 1, "units": units}])  # fmt: skip
    item = publish.ApprovedText(
        document_id=work, work=work, language="en", document_type="judgment", text=text,
        approver="Test Reviewer", approved_at=now - timedelta(minutes=5),
        provenance={"url": f"https://example.org/court/{work}.pdf", "retrieved_at": "2026-10-09T06:41:17+00:00",
                    "sha256": "7" * 64},
    )  # fmt: skip
    assert publish.publish_texts(store, jurisdiction, publish.Texts(approved=[item]), decision)["published"]


def authority(pack):
    return next(u for u in pack["evidence"] if u["unit"] == "provision")["authority"]


def test_a_decisions_unit_says_how_its_court_bears_on_other_courts_once_its_register_is_reviewed(
    store, client, tmp_path, monkeypatch
):
    folder = tmp_path / "courts"
    folder.mkdir()
    monkeypatch.setattr(courts, "COURTS", folder)
    publish_judgment(store, monkeypatch, "ke", "ke-hc", "High Court of Kenya", "ke-hc-2019-5", "[2019] KEHC 5",
                     ["[1] The appellant was convicted of handling stolen goods.", "[2] The appeal fails."])  # fmt: skip
    # No register: the court is not known, and nothing is answered.
    plain = authority(retrieve(client, "handling stolen goods", jurisdiction="ke"))
    assert plain == {"document_type": "judgment", "court_relation": "unknown"}
    # A proposal awaiting review: the court is named, its bearing is not answered.
    (folder / "ke.json").write_text(json.dumps(KE_REGISTER))
    proposed = authority(retrieve(client, "handling stolen goods", jurisdiction="ke"))
    assert proposed["court"] == {"id": "ke-hc", "name": "High Court of Kenya", "jurisdiction": "ke",
                                 "review_state": "unreviewed", "bears_on": []}  # fmt: skip
    # Its courts reviewed by the owner: served as reviewed, while the relation still answers nothing.
    (folder / "ke.json").write_text(json.dumps(reviewed(KE_REGISTER, doctrinal=False)))
    structural = authority(retrieve(client, "handling stolen goods", jurisdiction="ke"))
    assert structural["court"]["review_state"] == "reviewed" and structural["court"]["bears_on"] == []
    # The relation reviewed by a qualified reviewer: the High Court's decision binds a Kenyan magistrate's
    # court, in the words of the register's source.
    (folder / "ke.json").write_text(json.dumps(reviewed(KE_REGISTER)))
    found = authority(retrieve(client, "handling stolen goods", jurisdiction="ke"))
    assert found["court_relation"] == "unknown"  # its bearing on Kenya's courts generally is not recorded
    assert found["court"]["review_state"] == "reviewed" and found["court"]["reviewed_on"] == "2026-10-01"
    assert found["court"]["bears_on"] == [
        {"id": "ke-hc-on-ke-mc", "court": "ke-mc", "relation": "binding",
         "evidence": ["https://example.org/judicature.pdf"],
         "rests_on": [{"quote": "Synthetic words of the Act.", "provision": "s. 1",
                       "title": COURT_SOURCE["title"], "url": COURT_SOURCE["url"]}]}
    ]  # fmt: skip


def test_a_ccja_decision_binds_member_states_courts_on_the_uniform_acts_only(
    store, client, tmp_path, monkeypatch
):
    folder = tmp_path / "courts"
    folder.mkdir()
    monkeypatch.setattr(courts, "COURTS", folder)
    (folder / "aa-ohada.json").write_text(json.dumps(reviewed(OHADA_REGISTER)))
    publish_judgment(store, monkeypatch, "aa-ohada", "aa-ohada-ccja", "Common Court of Justice and Arbitration",
                     "aa-ohada-ccja-2020-1", "CCJA 2020-1",
                     ["[1] The Uniform Act on commercial companies applies to the share transfer."])  # fmt: skip
    found = authority(retrieve(client, "share transfer commercial companies", jurisdiction="aa-ohada"))
    assert found["court"]["jurisdiction"] == "aa-ohada"
    assert found["court"]["bears_on"] == [{"id": "ccja-on-members", "court": None, "members": True,
                                          "matter": "uniform-acts", "relation": "binding",
                                          "evidence": [COURT_SOURCE["url"]],
                                          "rests_on": [{"quote": "Synthetic words of the Act.", "provision": "s. 1",
                                                        "title": COURT_SOURCE["title"],
                                                        "url": COURT_SOURCE["url"]}]}]  # fmt: skip
    # Binding only on that matter: without the question's matter, the general answer stays unknown.
    assert found["court_relation"] == "unknown"
    register = courts.load("aa-ohada")
    assert courts.relation(register, "aa-ohada-ccja", "sn", matter="uniform-acts") == "binding"
    assert courts.relation(register, "aa-ohada-ccja", "sn") == "unknown"
    assert courts.relation(register, "aa-ohada-ccja", "ke", matter="uniform-acts") == "unknown"
