"""Approved works reach the public catalogue as records, with their changes in the change log; a work
whose approval lapses is taken down (operations.publish)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_publish import decisions as decisions  # noqa: F401  (fixture)
from test_publish import rwanda_law

from api.main import create_app
from api.store import Store, seed
from changes.record import namespace
from operations import publish

pytestmark = pytest.mark.postgres
WORK = "rw-law-055-2024"


@pytest.fixture
def store(pg_engine):
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)  # Nigeria, and the change log's baseline
    return store


def events(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "change_event")
        return [
            (r.event_type, r.work_id)
            for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY publication_revision, event_id"))
        ]


def test_an_approved_work_is_published_as_a_record_and_taken_down_when_approval_lapses(store, decisions):
    decision = publish.decide("rw", "Ijidai Lassa", 12, [], "", "")
    approved = [publish.Approved(WORK, rwanda_law(), ["en", "fr", "rw"], "Ijidai Lassa", datetime.now(UTC))]
    dry = publish.publish_records(store, "rw", approved, set(), decision, dry_run=True)
    assert dry["published"] == [WORK] and events(store) == []
    report = publish.publish_records(store, "rw", approved, set(), decision)
    assert report["published"] == [WORK]
    # Its rights record's first revision is announced with it.
    assert sorted(events(store)) == [
        ("catalogue_added", WORK),
        ("coverage_changed", "rw"),
        ("rights_changed", WORK),
    ]
    client = TestClient(create_app(store=store))
    record = client.get(f"/sources/{WORK}").json()
    assert record["title"].startswith("Law n° 055/2024") and record["has_full_text"] is False
    assert client.get(f"/sources/{WORK}/versions").json()["items"] == []  # never any text
    assert {"id": "rw", "name": "Rwanda"}.items() <= next(
        j for j in client.get("/jurisdictions").json() if j["id"] == "rw"
    ).items()
    # Publishing again changes nothing; a lapsed approval takes the record down.
    assert publish.publish_records(store, "rw", approved, set(), decision)["updated"] == [WORK]
    assert len(events(store)) == 3
    assert publish.publish_records(store, "rw", [], {WORK}, decision)["taken_down"] == [WORK]
    # One save, one revision: the withdrawal and Rwanda losing its last public record (any order).
    assert set(events(store)[3:]) == {("withdrawal", WORK), ("coverage_changed", "rw")}
    assert client.get(f"/sources/{WORK}").status_code == 404


def test_the_workbench_approval_is_what_publishes(store, decisions, tmp_path, monkeypatch):
    from test_workbench import ACT, approve, confirm_details, flag_id, save, signed_in
    from test_workbench import world as workbench_world  # noqa: F401

    from schema.jurisdictions import registry
    from workbench.review import Review

    world = workbench_world.__wrapped__(tmp_path, monkeypatch)
    review_store, library = world
    review = Review(review_store, library, registry())
    approved, waiting = publish.from_workbench(review, library, review_store, "ng")
    assert approved == [] and ACT in waiting  # nothing approved yet
    approver = signed_in(world, "approver@example.org")
    save(approver, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    save(approver, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    confirm_details(approver)
    assert approve(approver).status_code == 200
    approved, waiting = publish.from_workbench(review, library, review_store, "ng")
    assert [a.work for a in approved] == [ACT] and approved[0].approver == "App" and ACT not in waiting


def test_a_link_its_records_no_longer_state_is_withdrawn_and_judicial_links_are_reported(store, decisions):
    from schema.record import Reference, Relation

    def item(work, *relations):
        return publish.Approved(
            work, rwanda_law(relations=relations), ["en"], "Ijidai Lassa", datetime.now(UTC)
        )

    def link(kind, work):
        return Relation(kind=kind, target=Reference(citation=f"Instrument {work}", work_uri=work))

    def effects():
        with store.engine.connect() as conn:
            table = namespace(conn).qualified(conn, "changes", "change_event")
            return [
                (r.work_id, r.event_revision, r.payload["legal_effect"].get("review_state"))
                for r in conn.execute(
                    text(
                        f"SELECT * FROM {table} WHERE event_type = 'legal_effect_recorded' ORDER BY work_id, event_revision"
                    )
                )
            ]

    decision = publish.decide("rw", "Ijidai Lassa", 12, [], "", "")
    law, treaty = "rw-law-domesticating", "rw-treaty-synthetic"
    approved = [item(law, link("domesticates", treaty), link("follows", "rw-judgment")), item(treaty)]
    report = publish.publish_records(store, "rw", approved, set(), decision)
    assert report["links"] == 1 and report["links_withdrawn"] == []
    [withheld] = report["withheld"]
    assert (withheld["work"], withheld["kind"], withheld["other"]) == (law, "follows", "rw-judgment")
    assert "reviewed citations" in withheld["reason"]  # treatment comes with its paragraph and review
    assert effects() == [(law, 1, None), (treaty, 1, None)]  # one on each work
    # Approved again without the link: it is withdrawn, and each work's event says so once.
    report = publish.publish_records(store, "rw", [item(law), item(treaty)], set(), decision)
    assert report["links_withdrawn"] == [f"link-{law}-domesticates-{treaty}"] and report["withheld"] == []
    assert not next(a for a in store.load().amendments if a.id == report["links_withdrawn"][0]).verified
    assert effects() == [(law, 1, None), (law, 2, "withdrawn"), (treaty, 1, None), (treaty, 2, "withdrawn")]
    assert (
        publish.publish_records(store, "rw", [item(law), item(treaty)], set(), decision)["links_withdrawn"]
        == []
    )
    assert len(effects()) == 4


def test_a_run_publishes_what_the_workbench_approved_and_records_where_each_document_stands(
    store, decisions, tmp_path, monkeypatch
):
    """The publish job's run, end to end: it looks at the documents an approval names, publishes their
    details under the country's decision (no text decision: no text), records the run and each
    document's standing for the workbench, takes up a run asked for there, and takes a document down
    when its approval is revoked."""
    from test_workbench import ACT, approve, confirm_details, flag_id, save, signed_in
    from test_workbench import world as workbench_world  # noqa: F401

    from schema.jurisdictions import registry
    from workbench.review import Review

    world = workbench_world.__wrapped__(tmp_path, monkeypatch)
    review_store, library = world
    review = Review(review_store, library, registry())
    log = publish.PublicationLog(review_store.engine)
    publish.decide("ng", "Ijidai Lassa", 12, [], "", "")
    quiet = publish.run(review, library, review_store, store, log)  # nothing approved: nothing to do
    assert quiet["trigger"] == "schedule" and quiet["countries"] == {}
    approver = signed_in(world, "approver@example.org")
    save(approver, resolutions={flag_id(): {"decision": "use_check", "reader": "mineru"}})
    save(approver, page=2, units=[{"kind": "paragraph", "text": "2. Citation."}])
    official = {
        "publication.kind": "gazette",
        "publication.name": "Federal Republic of Nigeria Official Gazette",
        "publication.url": "https://placng.org/i/documents/cybercrimes-2024",
    }
    confirm_details(approver, **official)
    assert approve(approver).status_code == 200
    review_store.request_publication("approver@example.org")
    summary = publish.run(review, library, review_store, store, log)
    assert summary["trigger"] == "manual" and summary["requested_by"] == "approver@example.org"
    assert summary["countries"]["ng"]["records"]["published"] == 1
    assert "No text decision" in summary["countries"]["ng"]["texts"]["blocked"]
    standing = review_store.publication_statuses()[ACT]
    assert (standing["details"], standing["text"]) == ("public", "no_decision") and standing["details_since"]
    assert [r["status"] for r in review_store.publication_runs()] == ["succeeded", "succeeded"]
    client = TestClient(create_app(store=store))
    assert client.get(f"/sources/{ACT}").status_code == 200
    # The workbench says so, on the document and in the list.
    shown = approver.get(f"/api/documents/{ACT}").json()["publication"]
    assert shown["state"] == "public" and shown["text"].startswith("Text not published: No text decision")
    listed = {d["id"]: d for d in approver.get("/api/documents?limit=200").json()["documents"]}
    assert listed[ACT]["publication"] == "public"
    page = approver.get("/api/publishing").json()
    assert [d["id"] for d in page["countries"][0]["documents"]] == [ACT] and len(page["runs"]) == 2
    # Revoked: the next run takes it down, and says so.
    assert approve(approver, expected=1, decision="revoked").status_code == 200
    assert approver.get(f"/api/documents/{ACT}").json()["publication"]["state"] == "taken_down_next"
    after = publish.run(review, library, review_store, store, log)
    assert after["countries"]["ng"]["records"]["taken_down"] == 1
    assert review_store.publication_statuses()[ACT]["details"] == "taken_down"
    assert client.get(f"/sources/{ACT}").status_code == 404
