"""Case law (EDT-01, EDT-02, DAT-06): a judgment's reviewed citations, published from a citations file,
are served from both ends with the citing paragraph and the part cited; a treatment only from a
qualified doctrinal review, announced on the treating judgment and, for an overruling, on the overruled
one; withdrawing it corrects those announcements. A proposal is never served, a treatment never
inferred."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_citations import ACT, CITED, CITING, COURT, REVIEWED, case_law
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_workbench_citations import proposed as proposed
from test_workbench_citations import signed_in
from test_workbench_citations import world as world

from api.main import create_app
from api.store import Store
from api.v1 import create_v1_app
from changes.record import namespace
from operations import citations

pytestmark = pytest.mark.postgres
ADVOCATE = "Barrister and Solicitor of the Supreme Court of Nigeria"
TO_ACT = "cit_00000000-0000-4000-8000-00000000000a"
TO_JUDGMENT = "cit_00000000-0000-4000-8000-00000000000b"
OVERRULES = "trt_00000000-0000-4000-8000-00000000000c"
PARAGRAPH_3 = (
    "[3] In Bello v. The State [2019] NGSC 4 at [4], this court held that the first issue was resolved "
    "against the appellant. That decision is overruled."
)


@pytest.fixture
def store(pg_engine, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    return Store(pg_engine)


@pytest.fixture
def v1(store, tmp_path):
    return TestClient(create_v1_app(store, tmp_path, lambda: []))


def to_act(**values):
    return {
        "id": TO_ACT, "citing_work": CITING, "citing_locator": "para_2", "cited_work": ACT,
        "cited_locator": "sec_2__subsec_1", "printed": "section 2(1) of the Nigeria Data Protection Act 2023",
        "quote": "[2] Under section 2(1) of the Nigeria Data Protection Act 2023, the Act applies to the "
        "processing of personal data. The bank processed the appellant's data.",
        "evidence_url": f"{COURT}{CITING}.pdf", "reviewed_by": "Test Owner", "reviewer_role": "owner",
        "reviewed_at": REVIEWED.isoformat(), **values,
    }  # fmt: skip


def overruling(**values):
    return {
        "id": OVERRULES, "kind": "overrules", "opinion": "majority", "quote": "That decision is overruled.",
        "reviewed_by": "Test Advocate", "reviewer_role": "Nigerian advocate", "reviewer_qualification": ADVOCATE,
        "reviewed_at": REVIEWED.isoformat(), **values,
    }  # fmt: skip


def to_judgment(*treatments, **values):
    return {
        "id": TO_JUDGMENT, "citing_work": CITING, "citing_locator": "para_3", "cited_work": CITED,
        "cited_locator": "para_4", "printed": "[2019] NGSC 4 at [4]", "quote": PARAGRAPH_3,
        "evidence_url": f"{COURT}{CITING}.pdf", "reviewed_by": "Test Owner", "reviewer_role": "owner",
        "reviewed_at": REVIEWED.isoformat(), "treatments": list(treatments), **values,
    }  # fmt: skip


def publish(store, *items, dry_run=False):
    file = citations.CitationFile.model_validate({"citations": list(items)})
    return citations.publish(store.engine, file, dry_run=dry_run)


def related(v1, work, **params):
    answer = v1.get(f"/sources/{work}/relationships", params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def treatment_events(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "change_event")
        return [
            dict(r)
            for r in conn.execute(
                text(
                    f"SELECT event_id, event_revision, work_id, logical_event_key, change_reason, payload FROM {table}"
                    " WHERE event_type = 'treatment_recorded' ORDER BY publication_revision, event_id, event_revision"
                )
            ).mappings()
        ]


def test_a_proposal_is_not_served_until_confirmed_then_both_ends_serve_the_citing_paragraph(store, v1):
    case_law(store)
    with store.engine.begin() as conn:
        proposed = citations.propose(conn, CITING)["proposals"]
    assert {p["cited_locator"] for p in proposed} == {"sec_2__subsec_1", "para_4"}
    assert related(v1, ACT, kind="cited_by")["items"] == []  # proposed only: nothing is public
    assert related(v1, CITING, kind="cites")["items"] == []
    assert publish(store, to_act(), dry_run=True)["citations_added"] == 1
    assert related(v1, ACT, kind="cited_by")["items"] == []  # a dry run writes nothing
    report = publish(store, to_act())
    assert report["citations_added"] == 1 and report["announced"] == 0  # a citation is no treatment

    [cited_by] = related(v1, ACT, kind="cited_by")["items"]
    assert cited_by == {
        "kind": "cited_by", "source_id": CITING, "title": "Adeyemi v. Example Bank", "citation": "[2024] NGSC 7",
        "legal_status": "unknown", "year": None, "effective_date": None, "evidence_url": f"{COURT}{CITING}.pdf",
        "verified": True, "assertion_id": TO_ACT, "citing_locator": "para_2", "citing_pinpoint": "[2]",
        "cited_locator": "sec_2__subsec_1", "cited_pinpoint": "s. 2(1)",
        "printed": "section 2(1) of the Nigeria Data Protection Act 2023", "quote": to_act()["quote"],
        "review": {"level": "structural", "reviewed_by": "Test Owner", "reviewer_role": "owner",
                   "reviewed_at": "2026-10-21T09:00:00Z"},
    }  # fmt: skip
    # "Cited by" on a provision: the part itself, or a part within it; never another part.
    assert [i["assertion_id"] for i in related(v1, ACT, locator="sec_2")["items"]] == [TO_ACT]
    assert related(v1, ACT, locator="sec_2__subsec_1", kind="cited_by")["total"] == 1
    assert related(v1, ACT, locator="sec_1")["items"] == []
    assert related(v1, ACT, locator="sec_2__subsec_10")["items"] == []
    assert v1.get(f"/sources/{ACT}/relationships", params={"locator": "sec 2"}).status_code == 400
    [cites] = related(v1, CITING, kind="cites")["items"]
    assert (cites["source_id"], cites["citing_pinpoint"], cites["cited_pinpoint"]) == (ACT, "[2]", "s. 2(1)")
    assert related(v1, CITING, locator="para_2")["total"] == 1
    assert related(v1, CITING, locator="para_3")["total"] == 0

    # MCP answers the same page.
    client = TestClient(create_app(store=store))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "get_relationships", "arguments": {"source_id": ACT, "relation_type": "cited_by", "locator": "sec_2"}}}  # fmt: skip
    answer = json.loads(client.post("/mcp", json=body).json()["result"]["content"][0]["text"])
    assert answer == related(v1, ACT, kind="cited_by", locator="sec_2")

    # The paragraph's words are the judgment's text: once that is not public, the citation stays, its words go.
    corpus = store.load()
    rights = next(r for r in corpus.rights if r.source_id == CITING)
    corpus.rights[corpus.rights.index(rights)] = rights.model_copy(update={"approved_content_hashes": []})
    store.save(corpus)
    [hidden] = related(v1, ACT, kind="cited_by")["items"]
    assert hidden["citing_locator"] == "para_2" and "quote" not in hidden and "printed" not in hidden


def test_published_citations_are_withdrawn_never_edited(store, v1):
    case_law(store)
    publish(store, to_act())
    with pytest.raises(citations.Refused, match="not edited"):
        publish(store, to_act(cited_locator="sec_1"))
    assert publish(store, to_act())["unchanged"] == 1
    assert publish(store, to_act(review_state="withdrawn"))["citations_withdrawn"] == 1
    assert related(v1, ACT, kind="cited_by")["items"] == [] and related(v1, CITING)["items"] == []
    with pytest.raises(citations.Refused, match="not reinstated"):
        publish(store, to_act())
    with pytest.raises(citations.Refused, match="has a paragraph para_9"):
        publish(store, to_act(id="cit_00000000-0000-4000-8000-0000000000f1", citing_locator="para_9"))
    with pytest.raises(citations.Refused, match="quote is not the words"):
        publish(store, to_act(id="cit_00000000-0000-4000-8000-0000000000f2", quote="Words never written."))
    with pytest.raises(citations.Refused, match="has sec_9"):
        publish(store, to_act(id="cit_00000000-0000-4000-8000-0000000000f3", cited_locator="sec_9"))
    with pytest.raises(citations.Refused, match="not a judgment"):
        publish(
            store,
            to_act(id="cit_00000000-0000-4000-8000-0000000000f4", citing_work=ACT, citing_locator="sec_1",
                   cited_work=CITING, cited_locator=None),
        )  # fmt: skip


def test_a_treatment_is_of_a_judgment_from_a_qualified_review_and_never_inferred(store, v1):
    case_law(store)
    on_act = to_act(treatments=[overruling(quote="the Act applies to the processing of personal data")])
    with pytest.raises(citations.Refused, match="judicial treatment is of a judgment"):
        publish(store, on_act)
    with pytest.raises(citations.Refused, match="passage is not the words"):
        publish(store, to_judgment(overruling(quote="Words never written.")))
    # Without a treatment the citation is served as a citation, and no treatment at all is inferred.
    publish(store, to_judgment())
    assert [i["kind"] for i in related(v1, CITED)["items"]] == ["cited_by"]
    assert related(v1, CITED, kind="overruled_by")["items"] == [] and treatment_events(store) == []
    # The catalogue itself refuses a treatment without the reviewer's qualification.
    with store.engine.begin() as conn, pytest.raises(IntegrityError):
        table = namespace(conn).qualified(conn, "corpus", "judicial_treatment")
        conn.execute(
            text(
                f"INSERT INTO {table} (treatment_id, citation_id, treatment, opinion, quote, review_state,"
                " reviewed_by, reviewer_role, reviewer_qualification, reviewed_at) VALUES (:t, :c, 'overrules',"
                " 'majority', 'That decision is overruled.', 'reviewed', 'Someone', 'owner', '', now())"
            ),
            {"t": OVERRULES, "c": TO_JUDGMENT},
        )


def test_an_overruled_judgment_carries_the_overruling_one_and_withdrawing_it_corrects_both_events(store, v1):
    case_law(store)
    report = publish(store, to_judgment(overruling()))
    assert report["treatments_added"] == 1 and report["announced"] == 2

    [overruled_by] = related(v1, CITED, kind="overruled_by")["items"]
    assert overruled_by["source_id"] == CITING and overruled_by["assertion_id"] == OVERRULES
    assert (overruled_by["citing_pinpoint"], overruled_by["cited_pinpoint"]) == ("[3]", "[4]")
    assert overruled_by["quote"] == "That decision is overruled." and overruled_by["opinion"] == "majority"
    assert overruled_by["evidence_url"] == f"{COURT}{CITING}.pdf"
    assert overruled_by["review"] == {
        "level": "doctrinal", "reviewed_by": "Test Advocate", "reviewer_role": "Nigerian advocate",
        "reviewer_qualification": ADVOCATE, "reviewed_at": "2026-10-21T09:00:00Z",
    }  # fmt: skip
    # The treated judgment's history: how a later judgment treated it first, then who cites it.
    assert [i["kind"] for i in related(v1, CITED)["items"]] == ["overruled_by", "cited_by"]
    assert [i["kind"] for i in related(v1, CITING)["items"]] == ["overrules", "cites"]
    assert related(v1, CITED)["current"] is None  # overruled is not repealed: no record replaces it

    first = treatment_events(store)
    by_work = {e["work_id"]: e for e in first}
    assert set(by_work) == {CITING, CITED}
    treating, treated = by_work[CITING], by_work[CITED]
    assert treating["logical_event_key"] == OVERRULES
    assert treated["logical_event_key"] == OVERRULES + ":treated"
    assert treating["payload"]["summary"] == "Adeyemi v. Example Bank overrules Bello v. The State, at [3]."
    said = "Bello v. The State was overruled by Adeyemi v. Example Bank, at [3]."
    assert treated["payload"]["summary"] == said
    assert treating["payload"]["treatment"]["of"]["id"] == CITED
    told = treated["payload"]["treatment"]
    assert told["by"]["id"] == CITING
    assert told["assertion"] == "direct_treatment" and told["review_level"] == "doctrinal"
    assert (told["citing_locator"], told["cited_locator"], told["opinion"]) == (
        "para_3",
        "para_4",
        "majority",
    )
    assert "quote" not in told  # the passage is served with the relationship, not announced
    feed = TestClient(create_app(store=store)).get("/changes", params={"type": "treatment_recorded"}).json()
    served = {e["work"]["id"]: e for e in feed["events"]}
    assert served[CITED]["treatment"]["by"] == {"id": CITING, "title": "Adeyemi v. Example Bank",
                                                "citation": "[2024] NGSC 7"}  # fmt: skip
    assert served[CITING]["treatment"]["kind"] == "overrules" and served[CITING]["legal_effect"] is None

    # Published treatments are not edited: withdraw it (and record another in its place, if need be).
    with pytest.raises(citations.Refused, match="not edited"):
        publish(store, to_judgment(overruling(kind="doubts")))
    assert publish(store, to_judgment(overruling()))["announced"] == 0  # unchanged: said once
    report = publish(store, to_judgment(overruling(review_state="withdrawn")))
    assert report["treatments_withdrawn"] == 1 and report["announced"] == 2
    corrected = treatment_events(store)[2:]
    assert {(e["event_id"], e["event_revision"]) for e in corrected} == {(e["event_id"], 2) for e in first}
    for event in corrected:
        assert event["change_reason"] == "corrected" and "was withdrawn" in event["payload"]["summary"]
        assert event["payload"]["treatment"]["review_state"] == "withdrawn"
    assert related(v1, CITED, kind="overruled_by")["items"] == []
    assert [i["kind"] for i in related(v1, CITED)["items"]] == ["cited_by"]  # the citation itself stands
    assert publish(store, to_judgment(overruling(review_state="withdrawn")))["announced"] == 0
    with pytest.raises(citations.Refused, match="not reinstated"):
        publish(store, to_judgment(overruling()))


def test_a_withdrawn_citation_takes_its_treatment_with_it(store, v1):
    case_law(store)
    publish(store, to_judgment(overruling()))
    report = publish(store, to_judgment(overruling(review_state="withdrawn"), review_state="withdrawn"))
    assert report["citations_withdrawn"] == 1 and report["treatments_withdrawn"] == 1
    assert related(v1, CITED)["items"] == [] and len(treatment_events(store)) == 4


def test_migration_0042_steps_back_only_without_treatment_events(pg_engine):
    from alembic import command
    from alembic.config import Config

    migrate(pg_engine)
    config = Config("alembic.ini")

    def run(step, revision):
        with pg_engine.begin() as conn:
            config.attributes["connection"] = conn
            step(config, revision)

    def tables():
        with pg_engine.connect() as conn:
            ns = namespace(conn)
            return [
                conn.scalar(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": ns.qualified(conn, kind, name)})
                for kind, name in (("corpus", "citation"), ("corpus", "judicial_treatment"), ("changes", "treatment_state"))
            ]  # fmt: skip

    run(command.downgrade, "0041")
    assert tables() == [False, False, False]
    run(command.upgrade, "head")
    assert tables() == [True, True, True]
    with pg_engine.begin() as conn:
        events = namespace(conn).qualified(conn, "changes", "change_event")
        conn.execute(
            text(
                f"INSERT INTO {events} (event_id, event_revision, event_type, logical_event_key, reviewed_revision,"
                " publication_revision, feed_epoch, jurisdiction_id, work_id, payload, payload_hash) VALUES"
                " (gen_random_uuid(), 1, 'treatment_recorded', 'trt', 1, 1, gen_random_uuid(), 'ng', 'w',"
                " '{}'::jsonb, repeat('0', 64))"
            )
        )
    with pytest.raises(RuntimeError, match="Treatment events exist"):
        run(command.downgrade, "0041")


def test_what_the_workbench_confirms_is_what_is_published(store, v1, world, proposed):
    approver = signed_in(world, "approver@example.org")
    advocate = signed_in(world, "advocate@example.org")
    approver.post(f"/api/documents/{CITING}/citations/proposals", json=proposed)
    listing = approver.get(f"/api/documents/{CITING}/citations").json()["citations"]
    items = {c["citation"]["cited_work"]: c for c in listing}
    for item in items.values():
        body = {"citation_id": item["citation_id"], "expected_revision": 1, "status": "confirmed",
                "reviewer_role": "owner"}  # fmt: skip
        assert approver.post(f"/api/documents/{CITING}/citations", json=body).status_code == 200
    treatment = {"expected_revision": 2, "action": "record", "review_level": "doctrinal", "kind": "overrules",
                 "opinion": "majority", "quote": "That decision is overruled.", "reviewer_role": "Nigerian advocate",
                 "reviewer_qualification": ADVOCATE}  # fmt: skip
    path = f"/api/documents/{CITING}/citations/{items[CITED]['citation_id']}/treatment"
    assert advocate.post(path, json=treatment).status_code == 200
    exported = approver.get(f"/api/documents/{CITING}/citations/export").json()
    report = citations.publish(store.engine, citations.CitationFile.model_validate(exported))
    assert report["citations_added"] == 2 and report["treatments_added"] == 1 and report["announced"] == 2
    [cited_by] = related(v1, ACT, kind="cited_by", locator="sec_2")["items"]
    assert cited_by["review"]["reviewed_by"] == "App" and cited_by["cited_pinpoint"] == "s. 2(1)"
    [overruled_by] = related(v1, CITED, kind="overruled_by")["items"]
    assert overruled_by["review"]["reviewer_qualification"] == ADVOCATE
    assert (
        overruled_by["review"]["reviewed_by"] == "Test Advocate" and overruled_by["citing_pinpoint"] == "[3]"
    )
