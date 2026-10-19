"""CPR-02: reviewed provision events are published from a file, announced in the change log, and decide
the currentness of evidence and checked parts at provision level; published events are never edited,
only superseded or withdrawn; proposals come from the published text."""

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_v1_text import WORK, publish_act

from api.store import Store
from api.v1 import create_v1_app
from changes.record import namespace
from operations import events

pytestmark = pytest.mark.postgres
REVIEWED = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
UNITS = [
    {"kind": "heading", "text": "EXAMPLE ACT, 2023"},
    {"kind": "paragraph", "text": "1. (1) The Fund shall lend to students.", "notes": ["Loans"]},
    {"kind": "paragraph", "text": "(2) A loan is repaid from salary."},
    {"kind": "paragraph", "text": "2. The levy is payable monthly.", "notes": ["Levy"]},
    {"kind": "paragraph", "text": "3. This Act shall come into force on the 12th day of June, 2023.", "notes": ["Commencement"]},
    {"kind": "paragraph", "text": "4. The Old Loans Act is hereby repealed.", "notes": ["Repeal"]},
]  # fmt: skip


@pytest.fixture
def store(pg_engine, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    return Store(pg_engine)


def an_event(kind, day, **values):
    return {
        "id": f"evt_{uuid.uuid4()}", "type": kind, "affected_work": WORK, "scope": "whole",
        "date_from": day, "evidence": {"url": "https://www.nass.gov.ng/example-act.pdf", "locator": "sec_3"},
        "reviewed_by": "Editor", "reviewed_at": REVIEWED.isoformat(), **values,
    }  # fmt: skip


def publish(store, *items, dry_run=False):
    return events.publish(
        store.engine, events.EventFile.model_validate({"events": list(items)}), dry_run=dry_run
    )


def changes(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "change_event")
        return [
            dict(r)
            for r in conn.execute(
                text(f"SELECT event_id, event_revision, payload FROM {table} WHERE event_type = 'legal_effect_recorded'"
                     " ORDER BY publication_revision, event_revision")
            ).mappings()
        ]  # fmt: skip


def test_events_decide_currentness_at_provision_level_and_are_announced(store, tmp_path):
    _, version, nodes = publish_act(store, UNITS, effective_from=date(2023, 6, 12))
    client = TestClient(create_v1_app(store, tmp_path, lambda: []))
    before = client.get("/retrieve", params={"q": "levy payable monthly"}).json()["evidence"][0][
        "currentness"
    ]
    assert before["basis"] == "record"
    commenced = an_event("commenced", "2023-06-12")
    repeal = an_event("repealed", "2024-03-01", scope="explicit_nodes", locators=["sec_2"])
    assert publish(store, commenced, repeal, dry_run=True)["events_added"] == 2
    assert changes(store) == []  # a dry run writes nothing
    report = publish(store, commenced, repeal)
    assert report["events_added"] == 2 and report["announced"] == 2
    levy = client.get("/retrieve", params={"q": "levy payable monthly", "as_of": "2024-01-01"}).json()[
        "evidence"
    ][0]
    assert levy["currentness"]["basis"] == "provision" and levy["currentness"]["state"] == "in_force"
    now = client.get("/retrieve", params={"q": "levy payable monthly"}).json()["evidence"][0]["currentness"]
    assert now["state"] == "not_in_force" and repeal["id"] in now["evidence"]
    loans = client.post(
        "/currentness", json={"items": [{"anchor_id": nodes["sec_1"].id, "as_of": "2024-01-01"}]}
    )
    [checked] = loans.json()["results"]
    assert checked["currentness"] == {
        "state": "in_force", "basis": "provision", "assessed_at": "2024-01-01", "algorithm": "currentness-v1",
        "evidence": [commenced["id"]], "reasons": [],
    }  # fmt: skip
    announced = changes(store)
    assert [c["payload"]["legal_effect"]["kind"] for c in announced] == ["commenced", "repealed"]
    assert announced[1]["payload"]["legal_effect"]["provisions"] == ["sec_2"]


def test_published_events_are_superseded_or_withdrawn_never_edited(store):
    publish_act(store, UNITS)
    commenced = an_event("commenced", "2023-06-12")
    publish(store, commenced)
    with pytest.raises(events.Refused, match="not edited"):
        publish(store, {**commenced, "date_from": "2023-07-01"})
    assert publish(store, commenced)["unchanged"] == 1 and len(changes(store)) == 1
    correction = an_event("commenced", "2023-07-01", supersedes=commenced["id"])
    assert publish(store, correction)["events_added"] == 1
    assert publish(store, {**correction, "review_state": "withdrawn"})["events_withdrawn"] == 1
    announced = changes(store)
    assert len(announced) == 3
    assert announced[-1]["event_revision"] == 2 and "withdrawn" in announced[-1]["payload"]["summary"]
    with pytest.raises(events.Refused, match="no public text"):
        publish(store, an_event("repealed", "2024-01-01", scope="explicit_nodes", locators=["sec_99"]))
    with pytest.raises(events.Refused, match="not a public record"):
        publish(store, an_event("repealed", "2024-01-01", affected_work="ng-no-such-act"))


def test_commencement_and_repeal_are_proposed_from_the_text(store):
    publish_act(store, UNITS)
    with store.engine.connect() as conn:
        found = events.propose(conn, WORK)
    commenced = next(p for p in found if p["type"] == "commenced")
    assert (
        commenced["date_from"] == date(2023, 6, 12).isoformat()
        and commenced["evidence"]["locator"] == "sec_3"
    )
    repealed = next(p for p in found if p["type"] == "repealed")
    assert repealed["affected_title"] == "Old Loans Act" and repealed["affected_work"] is None
