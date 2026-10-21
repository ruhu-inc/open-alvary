"""Membership of the African Union and the regional bodies (JUR-01, JUR-04): reviewed rows of the register
are published to the catalogue, served in both directions on a date, with their dates and evidence, and
announced in the change log, one event per change; proposals never are. The register here is a synthetic
fixture: its dates and evidence are not research."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from api.main import create_app
from api.store import Store, seed
from api.v1 import create_v1_app
from changes.record import namespace
from operations import memberships

pytestmark = pytest.mark.postgres
NOTE = "Synthetic fixture: not research."


def row(member, body, start, end=None, *, status="reviewed", evidence=None):
    return {
        "id": f"{member}:{body}:1",
        "subject_id": member,
        "target_id": body,
        "kind": "member_of",
        "status": status,
        "valid_from": start,
        "valid_to": end,
        "evidence_url": evidence or f"https://{body}.example.org/members",
        "note": NOTE,
        "reviewed_by": "Test Reviewer" if status == "reviewed" else None,
        "reviewed_at": "2026-10-21T09:00:00Z" if status == "reviewed" else None,
    }


FIXTURE = [
    row("sn", "aa-au", "2001-05-26"),
    row("sn", "aa-ecowas", "1975-05-28"),
    row("sn", "aa-uemoa", "1994-01-10"),
    row("sn", "aa-ohada", "1995-09-18"),
    row("ml", "aa-au", "2001-05-26"),
    row("ml", "aa-ecowas", "1975-05-28", "2025-01-28"),
    row("ml", "aa-ohada", "1995-09-18"),
    row("sn", "aa-censad", None, status="proposal"),
    row("gh", "aa-ecowas", None, status="proposal"),
]


def write(path, rows):
    path.write_text(json.dumps({"standard": "memberships-1", "description": NOTE, "memberships": rows}))
    return path


@pytest.fixture
def published(pg_engine, tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)  # the change log's baseline
    register = write(tmp_path / "memberships.json", FIXTURE)
    report = memberships.publish(pg_engine, register)
    return store, register, report, TestClient(create_v1_app(store, tmp_path, lambda: []))


def events(store):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "change_event")
        return [
            dict(r)
            for r in conn.execute(
                text(
                    f"SELECT * FROM {table} WHERE event_type = 'membership_recorded'"
                    " ORDER BY publication_revision, event_id, event_revision"
                )
            ).mappings()
        ]


def test_a_country_lists_its_bodies_on_a_date_with_dates_evidence_and_review(published):
    _, _, report, client = published
    assert report["published"] == 7 and report["proposals_not_published"] == 2
    senegal = client.get("/jurisdictions/sn", params={"as_of": "2026-01-01"}).json()
    assert senegal["level"] == "national" and senegal["as_of"] == "2026-01-01" and "members" not in senegal
    held = senegal["member_of"]
    assert held["status"] == "reviewed" and held["ended"] == []
    assert [m["body_id"] for m in held["items"]] == ["aa-au", "aa-ecowas", "aa-ohada", "aa-uemoa"]
    ecowas = held["items"][1]
    assert (
        ecowas["body_name"] == "Economic Community of West African States"
        and ecowas["member_name"] == "Senegal"
    )
    assert ecowas["valid_from"] == "1975-05-28" and ecowas["valid_to"] is None
    assert ecowas["evidence_url"] == "https://aa-ecowas.example.org/members" and ecowas["note"] == NOTE
    assert ecowas["reviewed_by"] == "Test Reviewer" and ecowas["reviewed_at"].startswith("2026-10-21T09:00")
    # Before a membership began, it is not held.
    early = client.get("/jurisdictions/sn", params={"as_of": "1990-01-01"}).json()["member_of"]
    assert [m["body_id"] for m in early["items"]] == ["aa-ecowas"]


def test_a_withdrawal_ends_a_membership_after_its_last_day(published):
    _, _, _, client = published
    mali = client.get("/jurisdictions/ml", params={"as_of": "2026-01-01"}).json()["member_of"]
    assert [m["body_id"] for m in mali["items"]] == ["aa-au", "aa-ohada"]
    assert [(m["body_id"], m["valid_to"]) for m in mali["ended"]] == [("aa-ecowas", "2025-01-28")]
    last_day = client.get("/jurisdictions/ml", params={"as_of": "2025-01-28"}).json()["member_of"]
    assert "aa-ecowas" in [m["body_id"] for m in last_day["items"]] and last_day["ended"] == []


def test_a_body_lists_its_members_and_answers_unknown_when_none_is_reviewed(published):
    _, _, _, client = published
    ecowas = client.get("/jurisdictions/aa-ecowas", params={"as_of": "2026-01-01"}).json()
    assert ecowas["level"] == "regional" and ecowas["country_code"] is None and "member_of" not in ecowas
    assert [m["member_id"] for m in ecowas["members"]["items"]] == ["sn"]
    assert [m["member_id"] for m in ecowas["members"]["ended"]] == ["ml"]
    # CEN-SAD has only a proposal: unknown, never an empty "no members".
    censad = client.get("/jurisdictions/aa-censad").json()["members"]
    assert censad["status"] == "unknown" and censad["items"] == [] and "unknown" in censad["notice"]
    # Ghana is in no catalogue row and has only a proposal: it answers, and its memberships are unknown.
    ghana = client.get("/jurisdictions/gh").json()
    assert ghana["name"] == "Ghana" and ghana["member_of"]["status"] == "unknown"
    missing = client.get("/jurisdictions/xx")
    assert missing.status_code == 404 and missing.json()["code"] == "unavailable"
    assert client.get("/jurisdictions/sn", params={"as_of": "2026-13-01"}).json()["code"] == "invalid_input"


def test_proposals_are_never_published_or_served(published):
    store, _, report, client = published
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "corpus", "membership")
        stored = set(conn.scalars(text(f"SELECT membership_id FROM {table}")))
    assert stored == {r["id"] for r in FIXTURE if r["status"] == "reviewed"}
    listing = client.get("/jurisdictions", params={"as_of": "2026-01-01"}).json()
    served = json.dumps(listing) + json.dumps(client.get("/jurisdictions/sn").json())
    assert "aa-censad" not in served and "gh:aa-ecowas" not in served
    # Publishing added the catalogue rows of members and bodies; a body's row has no country code.
    by_id = {j["id"]: j for j in listing}
    assert {"sn", "ml", "aa-au", "aa-ecowas", "aa-ohada", "aa-uemoa"} <= set(by_id)
    assert by_id["aa-ohada"]["country_code"] is None and "member_of" not in by_id["aa-ohada"]
    assert [m["body_id"] for m in by_id["sn"]["member_of"]["items"]] == [
        "aa-au",
        "aa-ecowas",
        "aa-ohada",
        "aa-uemoa",
    ]
    assert by_id["ng"]["member_of"]["status"] == "unknown"
    assert set(report["jurisdictions_added"]) == {"sn", "ml", "aa-au", "aa-ecowas", "aa-ohada", "aa-uemoa"}
    # The anonymous pilot API's answer is unchanged: no memberships.
    legacy = TestClient(create_app(store=store)).get("/jurisdictions").json()
    assert all("member_of" not in j for j in legacy)


def test_each_membership_change_is_exactly_one_event(published):
    store, register, _, client = published
    first = events(store)
    assert sorted(e["work_id"] for e in first) == sorted(
        r["id"] for r in FIXTURE if r["status"] == "reviewed"
    )
    assert len({e["publication_revision"] for e in first}) == 1 and all(
        e["change_reason"] is None for e in first
    )
    mali = next(e for e in first if e["work_id"] == "ml:aa-ecowas:1")
    assert mali["jurisdiction_id"] == "ml" and mali["payload"]["membership"]["valid_to"] == "2025-01-28"
    assert mali["legal_effective"] == {"from": "1975-05-28", "until": "2025-01-28", "precision": "day"}

    # Publishing again changes nothing.
    assert memberships.publish(store.engine, register)["announced"] == 0 and events(store) == first

    # Its end is one new event.
    ended = [r | {"valid_to": "2026-06-30"} if r["id"] == "sn:aa-ecowas:1" else r for r in FIXTURE]
    assert memberships.publish(store.engine, write(register, ended))["announced"] == 1
    [end] = events(store)[len(first) :]
    assert (
        end["work_id"] == "sn:aa-ecowas:1" and end["change_reason"] == "ended" and end["event_revision"] == 1
    )
    assert end["payload"]["changed_fields"] == ["valid_to"] and "ended" in end["payload"]["summary"]

    # A corrected date is one correction of that event: same event_id, next revision.
    corrected = [r | {"valid_to": "2026-07-31"} if r["id"] == "sn:aa-ecowas:1" else r for r in FIXTURE]
    assert memberships.publish(store.engine, write(register, corrected))["announced"] == 1
    [fix] = events(store)[len(first) + 1 :]
    assert (fix["event_id"], fix["event_revision"], fix["change_reason"]) == (end["event_id"], 2, "corrected")
    assert (fix["supersedes_event_id"], fix["supersedes_revision"]) == (end["event_id"], 1)

    # A membership no longer reviewed is withdrawn from the catalogue: one correction, marked retracted.
    retracted = [r for r in corrected if r["id"] != "ml:aa-ohada:1"]
    assert memberships.publish(store.engine, write(register, retracted))["removed"] == 1
    [gone] = events(store)[len(first) + 2 :]
    original = next(e for e in first if e["work_id"] == "ml:aa-ohada:1")
    assert gone["event_id"] == original["event_id"] and gone["event_revision"] == 2
    assert gone["payload"]["membership"]["retracted"] is True

    # The feed serves them under the member and under the body.
    for place in ("ml", "aa-ecowas"):
        page = client.get("/changes", params={"jurisdiction": place, "type": "membership_recorded"}).json()
        assert "ml:aa-ecowas:1" in [e["membership"]["id"] for e in page["events"]], place
    served = client.get("/changes", params={"jurisdiction": "aa-ecowas"}).json()["events"]
    assert {e["membership"]["body"]["id"] for e in served} == {"aa-ecowas"}
    one = next(e for e in served if e["membership"]["id"] == "ml:aa-ecowas:1")
    assert one["work"] is None and one["jurisdiction"] == {"id": "ml", "name": "Mali"}
    assert one["membership"]["body"] == {
        "id": "aa-ecowas",
        "name": "Economic Community of West African States",
    }
    assert one["sources"] == ["https://aa-ecowas.example.org/members"]
    coverage = client.get("/change-coverage").json()
    assert "membership_recorded" in coverage["supported_event_types"]


def test_a_dry_run_writes_and_announces_nothing(pg_engine, tmp_path):
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)
    report = memberships.publish(pg_engine, write(tmp_path / "m.json", FIXTURE), dry_run=True)
    assert report["published"] == 7 and report["announced"] == 7
    assert events(store) == []
    with pg_engine.connect() as conn:
        table = namespace(conn).qualified(conn, "corpus", "membership")
        assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
