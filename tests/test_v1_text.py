"""The keyed API's text routes (API-02): versions, a text, its structure and its anchors, each rechecked
for eligibility immediately before it is sent, with conditional requests."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api.catalogue import LegacyCatalogue, SourceUnavailable
from api.v1 import create_v1_app
from operations import publish
from schema.models import Authority, Corpus, Jurisdiction, Language, RightsRecord, Source
from workbench import publication

WORK = "ng-example-act-2023"
UNITS = [
    {"kind": "heading", "text": "EXAMPLE ACT, 2023"},
    {"kind": "paragraph", "text": "ENACTED by the National Assembly —"},
    {"kind": "paragraph", "text": "1. (1) The Fund shall lend to students.", "notes": ["Loans"]},
    {"kind": "paragraph", "text": "(2) A loan is repaid —"},
    {"kind": "list_item", "text": "(a) after study; and"},
    {"kind": "list_item", "text": "(b) from salary — café, naïve, 中文."},
    {"kind": "paragraph", "text": "2. In this Act —", "notes": ["Interpretation"]},
    {"kind": "paragraph", "text": "“Fund” means the Students Loans Fund."},
    {"kind": "heading", "text": "SCHEDULES"},
    {"kind": "heading", "text": "First Schedule"},
    {"kind": "list_item", "marker": "1.", "text": "Lagos office"},
]


@pytest.fixture
def published(store):
    return publish_act(store, UNITS)


def catalogue(store, work, title, language):
    store.save(
        Corpus(
            jurisdictions=[Jurisdiction(id="ng", name="Nigeria", country_code="NG", languages=["en", "fr"])],
            authorities=[
                Authority(
                    id="ng-nass", jurisdiction_id="ng", name="National Assembly", kind="legislature",
                    official_url="https://nass.gov.ng/",
                )
            ],
            languages=[
                Language(id=code, name=name, tag=code)
                for code, name in (("en", "English"), ("fr", "French"), ("rw", "Kinyarwanda"))
            ],
            sources=[
                Source(
                    id=work, jurisdiction_id="ng", authority_id="ng-nass", title=title,
                    document_type="act", canonical_url="https://www.nass.gov.ng/example-act.pdf",
                    citation=title, language=language,
                )
            ],
            rights=[
                RightsRecord(
                    id=f"rights-{work}", source_id=work, metadata_public=True,
                    metadata_basis="Independently authored catalogue entry",
                )
            ],
        )
    )  # fmt: skip


def publish_act(
    store, units, *, work=WORK, title="Example Act, 2023", language="en", effective_from=None, record=True,
    pages=None,
):  # fmt: skip
    """A catalogue record and a reviewed text published through operations.publish; ``record=False``
    publishes another text of a record already catalogued; ``pages`` gives the text on several pages."""
    if record:
        catalogue(store, work, title, language)
    now = datetime.now(UTC).replace(microsecond=0)
    decision = {
        "document_types": ["act"],
        "rights_holder": "Federal Republic of Nigeria",
        "licence": "Not eligible for copyright (Copyright Act 2022, s. 3(b))",
        "licence_url": None,
        "attribution_required": False,
        "attribution_text": None,
        "database_rights_notes": "No database right is claimed in the texts.",
        "privacy_status": "cleared",
        "basis": "Official legislative texts are not eligible for copyright in Nigeria.",
        "evidence": ["https://www.copyright.gov.ng/copyright-act-2022"],
        "decided_by": "Ijidai Lassa",
        "decided_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=365)).isoformat(),
    }
    text = publication.build(
        {"id": work, "jurisdiction": "ng", "type": "act"}, pages or [{"page": 1, "units": units}]
    )
    item = publish.ApprovedText(
        document_id=work, work=work, language=language, document_type="act", text=text,
        approver="Ijidai Lassa", approved_at=now - timedelta(minutes=5), effective_from=effective_from,
        provenance={
            "url": "https://www.nass.gov.ng/example-act.pdf",
            "retrieved_at": "2026-10-09T06:41:17+00:00",
            "sha256": "7" * 64,
        },
    )  # fmt: skip
    publish.publish_texts(store, "ng", publish.Texts(approved=[item]), decision)
    corpus = store.load()
    digest = hashlib.sha256(text.serialized.text.encode()).hexdigest()
    version = next(v for v in corpus.versions if v.source_id == work and v.content_hash == digest)
    return store, version, {n.locator: n for n in corpus.structure if n.version_id == version.id}


def test_a_long_act_is_served_whole_with_its_structure(store, tmp_path):
    # More parts than the pilot's 1,000-row bound: the Electricity Act 2023 has 2,144.
    units = [
        {"kind": "paragraph", "text": text}
        for n in range(1, 501)
        for text in (f"{n}. (1) Section {n} applies.", "(2) It applies everywhere.")
    ]
    store, version, nodes = publish_act(store, units)
    page = TestClient(create_v1_app(store, tmp_path, lambda: [])).get(f"/sources/{WORK}/versions")
    assert page.status_code == 200 and len(page.json()["structure"]) == len(nodes) > 1500


def withdraw(store):
    corpus = store.load()
    corpus.rights[0].status = "RIGHTS_UNDER_REVIEW"
    store.save(corpus)


def test_a_text_is_its_exact_bytes_with_its_hash_as_etag(published, tmp_path):
    store, version, _ = published
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))
    text = client.get(f"/versions/{version.id}/text")
    assert text.status_code == 200 and text.headers["content-type"] == "text/plain; charset=utf-8"
    assert text.content == version.canonical_text.encode() and text.content.endswith(b"office\n")
    assert hashlib.sha256(text.content).hexdigest() == version.content_hash
    assert text.headers["etag"] == f'"{version.content_hash}"' and text.headers["cache-control"] == "no-store"
    unchanged = client.get(f"/versions/{version.id}/text", headers={"If-None-Match": text.headers["etag"]})
    assert unchanged.status_code == 304 and unchanged.content == b""
    withdraw(store)
    for headers in ({}, {"If-None-Match": text.headers["etag"]}):  # a 304 is never given for a withdrawn text
        gone = client.get(f"/versions/{version.id}/text", headers=headers)
        assert gone.status_code == 404 and gone.json()["code"] == "unavailable"
    unknown = client.get("/versions/ver_no-such-version/text")
    assert unknown.status_code == 404 and unknown.json()["detail"] == gone.json()["detail"]


def test_a_structure_is_paged_in_document_order(published, tmp_path):
    store, version, nodes = published
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))
    first = client.get(f"/versions/{version.id}/structure?limit=3").json()
    assert first["total"] == len(nodes) and first["content_hash"] == version.content_hash
    assert [n["locator"] for n in first["items"]] == ["document", "preamble", "sec_1"]
    rest = client.get(f"/versions/{version.id}/structure?limit=500&cursor={first['next_cursor']}").json()
    assert rest["next_cursor"] is None and len(first["items"]) + len(rest["items"]) == len(nodes)
    assert [n["locator"] for n in rest["items"]][:3] == [
        "sec_1__subsec_1",
        "sec_1__subsec_2",
        "sec_1__subsec_2__para_a",
    ]
    assert client.get(f"/versions/{version.id}/structure?cursor=bogus").json()["code"] == "invalid_input"
    page = client.get(f"/versions/{version.id}/structure")
    again = client.get(f"/versions/{version.id}/structure", headers={"If-None-Match": page.headers["etag"]})
    assert again.status_code == 304


def test_an_anchor_is_one_part_exactly_with_a_pinpoint_citation(published, tmp_path):
    store, version, nodes = published
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))
    paragraph = client.get(f"/anchors/{nodes['sec_1__subsec_2__para_b'].id}").json()
    raw = version.canonical_text.encode()
    assert paragraph["text"] == "(b) from salary — café, naïve, 中文."
    assert raw[paragraph["start_byte"] : paragraph["end_byte"]].decode() == paragraph["text"]
    assert paragraph["span_hash"] == hashlib.sha256(paragraph["text"].encode()).hexdigest()
    assert paragraph["offset_unit"] == "utf-8-bytes" and paragraph["content_hash"] == version.content_hash
    assert paragraph["citation"] == "Example Act, 2023, s. 1(2)(b)"
    assert paragraph["official_url"] == "https://www.nass.gov.ng/example-act.pdf"
    assert paragraph["attribution_required"] is False and paragraph["licence"].startswith("Not eligible")
    section = client.get(f"/anchors/{nodes['sec_1'].id}").json()
    assert section["citation"] == "Example Act, 2023, s. 1" and section["heading"] == "Loans"
    assert section["text"].startswith("Loans\n\n1. (1) The Fund")
    definition = client.get(f"/anchors/{nodes['sec_2__def_fund'].id}").json()
    assert definition["citation"] == "Example Act, 2023, s. 2, “Fund”"
    schedule = client.get(f"/anchors/{nodes['sch_1__para_1'].id}").json()
    assert schedule["citation"] == "Example Act, 2023, First Schedule, para. 1"
    assert client.get(f"/anchors/{nodes['document'].id}").json()["citation"] == "Example Act, 2023"
    withdraw(store)
    gone = client.get(f"/anchors/{nodes['sec_1'].id}")
    assert (
        gone.status_code == 404
        and client.get("/anchors/nod_unknown").json()["detail"] == gone.json()["detail"]
    )


def test_texts_are_rechecked_immediately_before_they_are_sent(cleared, store, tmp_path, monkeypatch):
    store.save(cleared)
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))
    source = cleared.sources[0].id
    sent = client.get(f"/sources/{source}/versions")
    assert sent.status_code == 200 and sent.json()["items"][0]["canonical_text"].startswith("Synthetic")
    assert sent.headers["cache-control"] == "no-store"
    read = LegacyCatalogue.versions
    calls = []

    def withdrawn_after_reading(self, source_id, **selection):
        calls.append(source_id)
        if len(calls) > 1:  # the recheck: the record stopped being public after it was read
            raise SourceUnavailable(source_id)
        return read(self, source_id, **selection)

    monkeypatch.setattr(LegacyCatalogue, "versions", withdrawn_after_reading)
    refused = client.get(f"/sources/{source}/versions")
    assert calls == [source, source]
    assert refused.status_code == 503 and refused.headers["content-type"] == "application/problem+json"
    assert refused.json()["code"] == "temporarily_unavailable" and refused.json()["retryable"] is True
    assert "Synthetic" not in refused.text and refused.headers["cache-control"] == "no-store"


def test_the_mcp_tool_answers_the_same_anchor_and_refuses_one_too_large(published, tmp_path, monkeypatch):
    from api import mcp
    from api.main import create_app

    store, _, nodes = published
    client = TestClient(create_app(store, tmp_path))

    def call(anchor_id):
        body = {"name": "get_anchor", "arguments": {"anchor_id": anchor_id}}
        return client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "id": 1, "params": body})

    result = call(nodes["sec_1__subsec_2__para_a"].id).json()["result"]
    assert (
        not result["isError"]
        and '"citation": "Example Act, 2023, s. 1(2)(a)"' in result["content"][0]["text"]
    )
    monkeypatch.setattr(mcp, "MAX_TOOL_BYTES", 100)
    refused = call(nodes["sec_1"].id).json()["result"]
    assert refused["isError"] and refused["content"][0]["text"].startswith("result_too_large")
    assert call("nod_unknown").json()["result"]["isError"]


def test_a_citation_of_a_part_resolves_to_its_anchor_and_says_why_when_it_cannot(published, tmp_path):
    from urllib.parse import quote

    store, version, nodes = published
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))

    def provision(citation, **params):
        answer = client.get(f"/citations/{quote(citation)}", params=params)
        assert answer.status_code == 200, answer.text
        return answer.json()

    found = provision("Example Act, 2023, s. 1(2)(b)")
    assert found["status"] == "resolved" and [m["id"] for m in found["matches"]] == [WORK]
    [part] = found["provision"]["candidates"]
    assert (
        found["provision"]["status"] == "resolved"
        and part["anchor_id"] == nodes["sec_1__subsec_2__para_b"].id
    )
    raw = version.canonical_text.encode()
    assert raw[part["start_byte"] : part["end_byte"]].decode() == "(b) from salary — café, naïve, 中文."
    assert part["span_hash"] == hashlib.sha256(raw[part["start_byte"] : part["end_byte"]]).hexdigest()
    assert (
        part["citation"] == "Example Act, 2023, s. 1(2)(b)" and part["locator"] == "sec_1__subsec_2__para_b"
    )
    for other in ("section 1(2)(b) of the Example Act, 2023", "EXAMPLE ACT, 2023  S.1 (2) (b)"):
        assert provision(other)["provision"]["candidates"][0]["anchor_id"] == part["anchor_id"]
    assert provision("Example Act, 2023 s. 2 “Fund”")["provision"]["candidates"][0]["kind"] == "definition"
    assert provision("Example Act, 2023, First Schedule, para. 1")["provision"]["status"] == "resolved"

    # Every numbered part resolves back to itself from the citation its anchor gives; parts cited by
    # their heading (the preamble) have no pinpoint.
    from api import pinpoint

    numbered = 0
    for node in nodes.values():
        cited = client.get(f"/anchors/{node.id}").json()["citation"]
        if pinpoint.read(cited.removeprefix("Example Act, 2023, ")):
            numbered += 1
            assert provision(cited)["provision"]["candidates"][0]["anchor_id"] == node.id, cited
    assert numbered >= 8

    missing = provision("Example Act, 2023, s. 1(2)(z)")["provision"]
    assert missing["status"] == "unresolved" and missing["reason"] == "not_found"
    assert missing["nearest"]["pinpoint"] == "s. 1(2)" and missing["candidates"] == []
    assert provision("Example Act, 2023, s. 9")["provision"]["nearest"] is None
    assert provision("Example Act, 2023, s. 1", language="fr")["provision"]["reason"] == "no_text_in_scope"
    assert (
        provision("Example Act, 2023, s. 1", as_of="2024-01-01")["provision"]["reason"] == "no_text_in_scope"
    )
    assert "provision" not in provision("Example Act, 2023")  # the pilot's answer, unchanged
    assert provision("Unknown Act s. 1") == {"status": "unresolved", "matches": []}
    withdraw(store)
    assert provision("Example Act, 2023, s. 1")["provision"]["reason"] == "no_public_text"


def test_a_part_in_two_languages_is_two_alternatives_until_a_language_is_given(store, tmp_path):
    _, english, _ = publish_act(store, UNITS)
    french_units = [{**u, "text": u["text"].replace("The Fund shall lend", "Le Fonds prête")} for u in UNITS]
    _, french, _ = publish_act(store, french_units, language="fr", record=False)
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))
    both = client.get("/citations/Example Act, 2023, s. 1(1)").json()["provision"]
    assert both["status"] == "ambiguous" and both["reason"] == "several_texts"
    assert {c["language"] for c in both["candidates"]} == {"en", "fr"}
    one = client.get("/citations/Example Act, 2023, s. 1(1)", params={"language": "fr"}).json()["provision"]
    assert one["status"] == "resolved" and one["candidates"][0]["version_id"] == french.id

    page = client.get(f"/sources/{WORK}/versions", params={"language": "en"}).json()
    assert page["selection"] == {
        "status": "selected", "as_of": None, "language": "en", "version_id": english.id,
        "alternatives": [], "reason": None,
    }  # fmt: skip
    assert [v["id"] for v in page["items"]] == [english.id]
    assert {n["version_id"] for n in page["structure"]} == {english.id}
    either = client.get(f"/sources/{WORK}/versions", params={"as_of": "2024-01-01"}).json()["selection"]
    assert either["status"] == "ambiguous" and either["reason"] == "effective_dates_unknown"
    none = client.get(f"/sources/{WORK}/versions", params={"language": "rw"}).json()
    assert none["selection"]["reason"] == "none_in_language" and none["items"] == []
    assert "selection" not in client.get(f"/sources/{WORK}/versions").json()  # the pilot's answer, unchanged
    assert client.get(f"/sources/{WORK}/versions", params={"language": "not a tag"}).status_code == 400

    # Language editions (API-02 expressions): one per language, each text labelled with its edition.
    editions = client.get(f"/sources/{WORK}/expressions").json()
    assert [(e["expression_id"], e["version_ids"], e["preferred_version_id"], e["preferred_reason"])
            for e in editions["items"]] == [
        (f"{WORK}:en", [english.id], english.id, "only_text"),
        (f"{WORK}:fr", [french.id], french.id, "only_text"),
    ]  # fmt: skip
    assert all(e["authority"] == "unknown" for e in editions["items"])
    edition = client.get(f"/sources/{WORK}/versions", params={"expression_id": f"{WORK}:fr"}).json()
    assert [v["id"] for v in edition["items"]] == [french.id] and edition["items"][0][
        "expression_id"
    ] == f"{WORK}:fr"
    assert (
        client.get(f"/sources/{WORK}/versions", params={"expression_id": "other-act:fr"}).status_code == 400
    )
    assert {v["expression_id"] for v in client.get(f"/sources/{WORK}/versions").json()["items"]} == {
        f"{WORK}:en",
        f"{WORK}:fr",
    }


def test_a_date_selects_the_text_in_force_and_never_one_without_a_date():
    from datetime import date
    from types import SimpleNamespace

    from api.catalogue import select_versions

    def text(id_, language="en", start=None, until=None, prior=None, raw="a"):
        return SimpleNamespace(
            id=id_, language=language, effective_from=start, effective_until=until, prior_version_id=prior,
            raw_hash=raw,
        )  # fmt: skip

    old = text("ver_old", start=date(2010, 1, 1), until=date(2020, 1, 1))
    new = text("ver_new", start=date(2020, 1, 1), prior="ver_old", raw="b")
    picked, chosen = select_versions([old, new], date(2015, 6, 1))
    assert picked.status == "selected" and picked.version_id == "ver_old" and chosen == [old]
    assert select_versions([old, new], date(2020, 1, 1))[0].version_id == "ver_new"  # the day it starts
    assert select_versions([old, new], date(2009, 1, 1))[0].reason == "none_in_force"
    assert select_versions([old, new])[0].reason == "several_texts"  # no date: never "the latest"
    undated = text("ver_undated")
    assert select_versions([old, undated], date(2015, 6, 1))[0].alternatives == ("ver_old", "ver_undated")
    french = text("ver_fr", language="fr", start=date(2010, 1, 1))
    assert select_versions([old, french], date(2015, 6, 1))[0].reason == "several_languages"
    assert select_versions([old, french], date(2015, 6, 1), "en-NG")[0].reason == "none_in_language"
    regional = text("ver_ng", language="en-NG", start=date(2010, 1, 1))
    assert select_versions([regional], date(2015, 6, 1), "en")[0].version_id == "ver_ng"
    # A later reading of the same official file corrects the earlier one: one text, not two.
    reread = text("ver_reread", start=date(2010, 1, 1), until=date(2020, 1, 1), prior="ver_old")
    assert select_versions([old, reread], date(2015, 6, 1))[0].version_id == "ver_reread"
