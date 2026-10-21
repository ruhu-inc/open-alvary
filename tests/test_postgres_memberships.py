"""Membership of the African Union and the regional bodies (JUR-01, JUR-04): reviewed rows of the register
are published to the catalogue, served in both directions on a date, with their dates (and, when a date is
uncertain, its bounds), basis and evidence, and announced in the change log, one event per change;
proposals never are. A suspension from participation is served under its membership as context, never
changes whether it is held, and is announced under its own type. The register here is a synthetic
fixture: its dates and evidence are not research."""

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from api.catalogue import LegacyCatalogue, bodies_on
from api.main import create_app
from api.repositories import LegacyRepository
from api.store import Store, seed
from api.v1 import create_v1_app
from changes.record import namespace
from operations import memberships

pytestmark = pytest.mark.postgres
NOTE = "Synthetic fixture: not research."
BASIS = {
    "kind": "entry_into_force",
    "instrument": "Synthetic Treaty",
    "pinpoint": "art. 1",
    "evidence_url": "https://treaty.example.org/text",
    "quote": "Synthetic words.",
}


def row(member, body, start, end=None, *, status="reviewed", evidence=None):
    return {
        "id": f"{member}:{body}:1",
        "subject_id": member,
        "target_id": body,
        "kind": "member_of",
        "status": status,
        "valid_from": start,
        "valid_to": end,
        "basis": BASIS if status == "reviewed" else None,
        "evidence_url": evidence or f"https://{body}.example.org/members",
        "note": NOTE,
        "reviewed_by": "Test Reviewer" if status == "reviewed" else None,
        "reviewed_at": "2026-10-21T09:00:00Z" if status == "reviewed" else None,
    }


def suspended(membership_id, start, end=None, *, n=1, status="reviewed"):
    return {
        "id": f"{membership_id}:s{n}",
        "membership_id": membership_id,
        "kind": "suspended_from_participation",
        "status": status,
        "valid_from": start,
        "valid_to": end,
        "legal_basis": [
            {"instrument": "Synthetic Act", "pinpoint": "art. 30", "evidence_url": "https://act.example.org/"}
        ],
        "evidence_url": "https://decision.example.org/suspended",
        "lifted_evidence_url": "https://decision.example.org/lifted" if end else None,
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
    row("mr", "aa-ecowas", "1975-05-28", "2000-12"),  # an end known to the month
    row("ss", "aa-eac", "2016-08-15/2016-09-05"),  # a start known to a window
    row("ga", "aa-au", "2001-05-26"),
    row("sn", "aa-censad", None, status="proposal"),
    row("gh", "aa-ecowas", None, status="proposal"),
]
SUSPENSIONS = [
    suspended("ga:aa-au:1", "2023-08-31"),
    suspended("ml:aa-au:1", None, status="proposal"),
]
REVIEWED = [r["id"] for r in FIXTURE if r["status"] == "reviewed"]


def write(path, rows, suspensions=()):
    path.write_text(
        json.dumps(
            {
                "standard": "memberships-2",
                "description": NOTE,
                "memberships": rows,
                "suspensions": suspensions,
            }
        )
    )
    return path


@pytest.fixture
def published(pg_engine, tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)  # the change log's baseline
    register = write(tmp_path / "memberships.json", FIXTURE, SUSPENSIONS)
    report = memberships.publish(pg_engine, register)
    return store, register, report, TestClient(create_v1_app(store, tmp_path, lambda: []))


def events(store, kind="membership_recorded"):
    with store.engine.connect() as conn:
        table = namespace(conn).qualified(conn, "changes", "change_event")
        return [
            dict(r)
            for r in conn.execute(
                text(
                    f"SELECT * FROM {table} WHERE event_type = :kind"
                    " ORDER BY publication_revision, event_id, event_revision"
                ),
                {"kind": kind},
            ).mappings()
        ]


def bodies(listing, part="items"):
    return [m["body_id"] for m in listing[part]]


def labels(store, member, day):
    """Each body's label in the one answer applicable law reads (api.catalogue.bodies_on); a body with no
    reviewed membership is absent, which is unknown."""
    with store.engine.connect() as conn:
        held = LegacyCatalogue(LegacyRepository(conn)).memberships(member)
    return {body: basis[0].held for body, basis in bodies_on(held, member, day).items()}


def test_a_country_lists_its_bodies_on_a_date_with_dates_basis_evidence_and_review(published):
    _, _, report, client = published
    assert report["published"] == len(REVIEWED) and report["proposals_not_published"] == 2
    senegal = client.get("/jurisdictions/sn", params={"as_of": "2026-01-01"}).json()
    assert senegal["level"] == "national" and senegal["as_of"] == "2026-01-01" and "members" not in senegal
    held = senegal["member_of"]
    assert held["status"] == "reviewed" and held["ended"] == [] and held["uncertain"] == []
    assert bodies(held) == ["aa-au", "aa-ecowas", "aa-ohada", "aa-uemoa"]
    ecowas = held["items"][1]
    assert (
        ecowas["body_name"] == "Economic Community of West African States"
        and ecowas["member_name"] == "Senegal"
    )
    assert ecowas["valid_from"] == "1975-05-28" and ecowas["valid_to"] is None
    assert ecowas["valid_from_bounds"] == {
        "earliest": "1975-05-28",
        "latest": "1975-05-28",
        "precision": "day",
    }
    assert ecowas["basis"] == BASIS and ecowas["suspensions"] == []
    assert ecowas["evidence_url"] == "https://aa-ecowas.example.org/members" and ecowas["note"] == NOTE
    assert ecowas["reviewed_by"] == "Test Reviewer" and ecowas["reviewed_at"].startswith("2026-10-21T09:00")
    # Before a membership began, it is not held.
    early = client.get("/jurisdictions/sn", params={"as_of": "1990-01-01"}).json()["member_of"]
    assert bodies(early) == ["aa-ecowas"]


def test_a_withdrawal_ends_a_membership_after_its_last_day(published):
    _, _, _, client = published
    mali = client.get("/jurisdictions/ml", params={"as_of": "2026-01-01"}).json()["member_of"]
    assert bodies(mali) == ["aa-au", "aa-ohada"]
    assert [(m["body_id"], m["valid_to"]) for m in mali["ended"]] == [("aa-ecowas", "2025-01-28")]
    last_day = client.get("/jurisdictions/ml", params={"as_of": "2025-01-28"}).json()["member_of"]
    assert "aa-ecowas" in bodies(last_day) and last_day["ended"] == []


def test_an_uncertain_end_is_unknown_inside_its_window_and_definite_outside_it(published):
    _, _, _, client = published

    def mauritania(day):
        return client.get("/jurisdictions/mr", params={"as_of": day}).json()["member_of"]

    assert bodies(mauritania("2000-11-30")) == ["aa-ecowas"]
    inside = mauritania("2000-12-15")
    assert inside["items"] == [] and inside["ended"] == [] and bodies(inside, "uncertain") == ["aa-ecowas"]
    [uncertain] = inside["uncertain"]
    assert uncertain["valid_to"] == "2000-12"
    assert uncertain["valid_to_bounds"] == {
        "earliest": "2000-12-01",
        "latest": "2000-12-31",
        "precision": "month",
    }
    after = mauritania("2001-01-01")
    assert after["items"] == [] and after["uncertain"] == [] and bodies(after, "ended") == ["aa-ecowas"]
    # The body's side answers the same.
    ecowas = client.get("/jurisdictions/aa-ecowas", params={"as_of": "2000-12-15"}).json()["members"]
    assert [m["member_id"] for m in ecowas["uncertain"]] == ["mr"]


def test_a_range_start_is_unknown_inside_its_window(published):
    store, _, _, client = published

    def south_sudan(day):
        return client.get("/jurisdictions/ss", params={"as_of": day}).json()["member_of"]

    assert south_sudan("2016-08-01")["items"] == [] and south_sudan("2016-08-01")["uncertain"] == []
    assert bodies(south_sudan("2016-08-20"), "uncertain") == ["aa-eac"]
    assert bodies(south_sudan("2016-09-05")) == ["aa-eac"]
    [eac] = south_sudan("2016-09-05")["items"]
    assert eac["valid_from"] == "2016-08-15/2016-09-05" and eac["valid_from_bounds"]["precision"] == "range"
    # The one answer the applicable-law scope reads: held, not held or unknown per body.
    assert labels(store, "ss", date(2016, 8, 20)) == {"aa-eac": "unknown"}
    assert labels(store, "ss", date(2016, 9, 6)) == {"aa-eac": "held"}  # AU: none reviewed, absent
    assert labels(store, "ss", date(2016, 8, 1)) == {"aa-eac": "not_held"}
    assert labels(store, "ml", date(2026, 1, 1)) == {
        "aa-au": "held",
        "aa-ecowas": "not_held",
        "aa-ohada": "held",
    }


def test_a_body_lists_its_members_and_answers_unknown_when_none_is_reviewed(published):
    _, _, _, client = published
    ecowas = client.get("/jurisdictions/aa-ecowas", params={"as_of": "2026-01-01"}).json()
    assert ecowas["level"] == "regional" and ecowas["country_code"] is None and "member_of" not in ecowas
    assert [m["member_id"] for m in ecowas["members"]["items"]] == ["sn"]
    assert [m["member_id"] for m in ecowas["members"]["ended"]] == ["ml", "mr"]
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
        ns = namespace(conn)
        stored = set(
            conn.scalars(text(f"SELECT membership_id FROM {ns.qualified(conn, 'corpus', 'membership')}"))
        )
        held = ns.qualified(conn, "corpus", "membership_suspension")
        stored_suspensions = set(conn.scalars(text(f"SELECT suspension_id FROM {held}")))
    assert stored == set(REVIEWED) and stored_suspensions == {"ga:aa-au:1:s1"}
    assert report["suspension_proposals_not_published"] == 1
    listing = client.get("/jurisdictions", params={"as_of": "2026-01-01"}).json()
    served = json.dumps(listing) + json.dumps(client.get("/jurisdictions/sn").json())
    served += json.dumps(client.get("/jurisdictions/ml").json())
    assert "aa-censad" not in served and "gh:aa-ecowas" not in served and "ml:aa-au:1:s1" not in served
    # Publishing added the catalogue rows of members and bodies; a body's row has no country code.
    by_id = {j["id"]: j for j in listing}
    assert {"sn", "ml", "aa-au", "aa-ecowas", "aa-ohada", "aa-uemoa"} <= set(by_id)
    assert by_id["aa-ohada"]["country_code"] is None and "member_of" not in by_id["aa-ohada"]
    assert bodies(by_id["sn"]["member_of"]) == ["aa-au", "aa-ecowas", "aa-ohada", "aa-uemoa"]
    assert by_id["ng"]["member_of"]["status"] == "unknown"
    assert set(report["jurisdictions_added"]) == {
        "sn", "ml", "mr", "ss", "ga", "aa-au", "aa-ecowas", "aa-eac", "aa-ohada", "aa-uemoa",
    }  # fmt: skip
    # The anonymous pilot API's answer is unchanged: no memberships.
    legacy = TestClient(create_app(store=store)).get("/jurisdictions").json()
    assert all("member_of" not in j for j in legacy)


def test_a_suspension_is_served_as_context_and_never_changes_whether_a_membership_is_held(published):
    store, _, _, client = published
    during = client.get("/jurisdictions/ga", params={"as_of": "2024-01-01"}).json()["member_of"]
    assert bodies(during) == ["aa-au"] and during["uncertain"] == [] and during["ended"] == []
    [context] = during["items"][0]["suspensions"]
    assert context["id"] == "ga:aa-au:1:s1" and context["kind"] == "suspended_from_participation"
    assert (
        context["in_effect"] is True and context["valid_from"] == "2023-08-31" and context["valid_to"] is None
    )
    assert context["legal_basis"] == [
        {"instrument": "Synthetic Act", "pinpoint": "art. 30", "evidence_url": "https://act.example.org/"}
    ]
    before = client.get("/jurisdictions/ga", params={"as_of": "2020-01-01"}).json()["member_of"]
    assert bodies(before) == ["aa-au"] and before["items"][0]["suspensions"][0]["in_effect"] is False
    # Under the body too, and never in the body's list of those who left.
    au = client.get("/jurisdictions/aa-au", params={"as_of": "2024-01-01"}).json()["members"]
    assert "ga" in [m["member_id"] for m in au["items"]] and au["ended"] == []
    assert labels(store, "ga", date(2024, 1, 1)) == {"aa-au": "held"}


def test_each_membership_change_is_exactly_one_event(published):
    store, register, _, client = published
    first = events(store)
    assert sorted(e["work_id"] for e in first) == sorted(REVIEWED)
    assert len({e["publication_revision"] for e in first}) == 1 and all(
        e["change_reason"] is None for e in first
    )
    mali = next(e for e in first if e["work_id"] == "ml:aa-ecowas:1")
    assert mali["jurisdiction_id"] == "ml" and mali["payload"]["membership"]["valid_to"] == "2025-01-28"
    assert mali["legal_effective"] == {"from": "1975-05-28", "until": "2025-01-28", "precision": "day"}
    assert mali["payload"]["membership"]["basis"] == {k: v for k, v in BASIS.items() if k != "quote"}
    mauritania = next(e for e in first if e["work_id"] == "mr:aa-ecowas:1")["payload"]
    assert mauritania["membership"]["valid_to_bounds"] == {
        "earliest": "2000-12-01",
        "latest": "2000-12-31",
        "precision": "month",
    }
    assert "until a day in 2000-12" in mauritania["summary"]

    # Publishing again changes nothing.
    assert memberships.publish(store.engine, register)["announced"] == 0 and events(store) == first

    # Its end is one new event.
    ended = [r | {"valid_to": "2026-06-30"} if r["id"] == "sn:aa-ecowas:1" else r for r in FIXTURE]
    assert memberships.publish(store.engine, write(register, ended, SUSPENSIONS))["announced"] == 1
    [end] = events(store)[len(first) :]
    assert (
        end["work_id"] == "sn:aa-ecowas:1" and end["change_reason"] == "ended" and end["event_revision"] == 1
    )
    assert end["payload"]["changed_fields"] == ["valid_to"] and "ended" in end["payload"]["summary"]

    # A corrected date is one correction of that event: same event_id, next revision.
    corrected = [r | {"valid_to": "2026-07"} if r["id"] == "sn:aa-ecowas:1" else r for r in FIXTURE]
    assert memberships.publish(store.engine, write(register, corrected, SUSPENSIONS))["announced"] == 1
    [fix] = events(store)[len(first) + 1 :]
    assert (fix["event_id"], fix["event_revision"], fix["change_reason"]) == (end["event_id"], 2, "corrected")
    assert (fix["supersedes_event_id"], fix["supersedes_revision"]) == (end["event_id"], 1)
    assert fix["legal_effective"]["until"] == "2026-07"

    # A corrected basis is a correction too; the words it quotes are not.
    quoted = [
        r | {"basis": BASIS | {"quote": "Other words."}} if r["id"] == "sn:aa-au:1" else r for r in corrected
    ]
    assert memberships.publish(store.engine, write(register, quoted, SUSPENSIONS))["announced"] == 0
    rebased = [r | {"basis": BASIS | {"kind": "deposit"}} if r["id"] == "sn:aa-au:1" else r for r in quoted]
    assert memberships.publish(store.engine, write(register, rebased, SUSPENSIONS))["announced"] == 1
    assert events(store)[-1]["payload"]["changed_fields"] == ["basis"]

    # A membership no longer reviewed is withdrawn from the catalogue: one correction, marked retracted.
    retracted = [r for r in rebased if r["id"] != "ml:aa-ohada:1"]
    assert memberships.publish(store.engine, write(register, retracted, SUSPENSIONS))["removed"] == 1
    gone = events(store)[-1]
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
    assert one["sources"] == ["https://aa-ecowas.example.org/members"] and one["suspension"] is None
    coverage = client.get("/change-coverage").json()
    assert "membership_recorded" in coverage["supported_event_types"]


def test_a_suspension_and_its_lifting_are_each_announced_once_under_their_own_type(published):
    store, register, _, client = published
    [announced] = events(store, "suspension_recorded")
    assert announced["work_id"] == "ga:aa-au:1:s1" and announced["jurisdiction_id"] == "ga"
    assert announced["change_reason"] is None and "remains a member" in announced["payload"]["summary"]
    told = announced["payload"]["suspension"]
    assert (
        told["membership_id"] == "ga:aa-au:1" and told["body"]["id"] == "aa-au" and told["valid_to"] is None
    )
    # The suspension is not a membership event: Gabon's membership was announced once, unchanged.
    assert [e["work_id"] for e in events(store) if e["jurisdiction_id"] == "ga"] == ["ga:aa-au:1"]

    # Publishing again changes nothing.
    assert memberships.publish(store.engine, register)["announced"] == 0

    # Its lifting is one new event, and the membership is untouched.
    lifted = [suspended("ga:aa-au:1", "2023-08-31", "2025-04-29"), *SUSPENSIONS[1:]]
    report = memberships.publish(store.engine, write(register, FIXTURE, lifted))
    assert (
        report["announced"] == 1
        and report["suspensions"]["updated"] == 1
        and report["unchanged"] == len(REVIEWED)
    )
    [_, lift] = events(store, "suspension_recorded")
    assert (lift["change_reason"], lift["event_revision"], lift["logical_event_key"]) == (
        "lifted",
        1,
        "ga:aa-au:1:s1:lifted",
    )
    assert "was lifted" in lift["payload"]["summary"] and lift["payload"]["changed_fields"] == [
        "valid_to",
        "lifted_evidence_url",
    ]
    after = client.get("/jurisdictions/ga", params={"as_of": "2026-01-01"}).json()["member_of"]
    assert bodies(after) == ["aa-au"] and after["items"][0]["suspensions"][0]["in_effect"] is False

    # The feed serves it under the member and under the body, apart from memberships.
    for place in ("ga", "aa-au"):
        page = client.get("/changes", params={"jurisdiction": place, "type": "suspension_recorded"}).json()
        assert [e["suspension"]["id"] for e in page["events"]] == ["ga:aa-au:1:s1"] * 2, place
        assert all(e["membership"] is None for e in page["events"])
    only = client.get("/changes", params={"type": "membership_recorded", "jurisdiction": "ga"}).json()[
        "events"
    ]
    assert [e["membership"]["id"] for e in only] == ["ga:aa-au:1"]
    assert "suspension_recorded" in client.get("/change-coverage").json()["supported_event_types"]

    # Withdrawing the suspension's review is one correction of the latest, marked retracted; the
    # membership stays.
    report = memberships.publish(store.engine, write(register, FIXTURE, SUSPENSIONS[1:]))
    assert report["suspensions"]["removed"] == 1 and report["announced"] == 1
    gone = events(store, "suspension_recorded")[-1]
    assert (gone["event_id"], gone["event_revision"]) == (lift["event_id"], 2)
    assert gone["payload"]["suspension"]["retracted"] is True
    assert bodies(client.get("/jurisdictions/ga").json()["member_of"]) == ["aa-au"]


def test_a_dry_run_writes_and_announces_nothing(pg_engine, tmp_path):
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)
    report = memberships.publish(pg_engine, write(tmp_path / "m.json", FIXTURE, SUSPENSIONS), dry_run=True)
    assert report["published"] == len(REVIEWED) and report["announced"] == len(REVIEWED) + 1
    assert events(store) == [] and events(store, "suspension_recorded") == []
    with pg_engine.connect() as conn:
        table = namespace(conn).qualified(conn, "corpus", "membership")
        assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0


def test_migration_0042_refuses_memberships_published_without_a_basis_and_steps_back_cleanly(pg_engine):
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from api.store import tables
    from schema.jurisdictions import catalogue_entry

    migrate(pg_engine)
    config = Config("alembic.ini")
    before = ScriptDirectory.from_config(config).get_revision("0042").down_revision

    def run(step, revision):
        with pg_engine.begin() as conn:
            config.attributes["connection"] = conn
            step(config, revision)

    def checks(conn):
        events = namespace(conn).qualified(conn, "changes", "change_event")
        return " ".join(
            conn.scalars(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)"
                ),
                {"t": events},
            )
        )

    run(command.downgrade, before)
    with pg_engine.begin() as conn:
        assert "membership_recorded" in checks(conn) and "suspension_recorded" not in checks(conn)
        assert "'lifted'" not in checks(conn)
        conn.execute(
            tables["jurisdictions"].insert(),
            [{"id": c, "payload": catalogue_entry(c).model_dump(mode="json")} for c in ("sn", "aa-au")],
        )
        table = namespace(conn).qualified(conn, "corpus", "membership")
        conn.execute(
            text(
                f"INSERT INTO {table} (membership_id, member_id, body_id, valid_from, evidence_url, note,"
                " reviewed_by, reviewed_at) VALUES ('sn:aa-au:1', 'sn', 'aa-au', '2001-05-26',"
                " 'https://au.example.org/', 'Synthetic.', 'Test Reviewer', now())"
            )
        )
    with pytest.raises(RuntimeError, match="no basis"):
        run(command.upgrade, "0042")
    with pg_engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table}"))
    run(command.upgrade, "head")
    with pg_engine.connect() as conn:
        assert "suspension_recorded" in checks(conn) and "membership_recorded" in checks(conn)
        assert "'lifted'" in checks(conn) and "'ended'" in checks(conn)
