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
    assert sorted(events(store)) == [("catalogue_added", WORK), ("coverage_changed", "rw")]
    client = TestClient(create_app(store=store))
    record = client.get(f"/sources/{WORK}").json()
    assert record["title"].startswith("Law n° 055/2024") and record["has_full_text"] is False
    assert client.get(f"/sources/{WORK}/versions").json()["items"] == []  # never any text
    assert {"id": "rw", "name": "Rwanda"}.items() <= next(
        j for j in client.get("/jurisdictions").json() if j["id"] == "rw"
    ).items()
    # Publishing again changes nothing; a lapsed approval takes the record down.
    assert publish.publish_records(store, "rw", approved, set(), decision)["updated"] == [WORK]
    assert len(events(store)) == 2
    assert publish.publish_records(store, "rw", [], {WORK}, decision)["taken_down"] == [WORK]
    # One save, one revision: the withdrawal and Rwanda losing its last public record (any order).
    assert set(events(store)[2:]) == {("withdrawal", WORK), ("coverage_changed", "rw")}
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
