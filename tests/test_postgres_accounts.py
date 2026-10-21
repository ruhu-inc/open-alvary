"""MAC-03–05 accounts against PostgreSQL: sign-in, sessions, access, keys, apps, roles, audit."""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from accounts import keys, policies
from accounts.store import PERSONAL_KEYS_PER_MEMBER, SERVICE_KEYS_PER_ORGANISATION, AccountError, Accounts
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
PEPPER = b"p" * 32
ISSUER = "https://login.example.test/"
TIER = {
    "id": "pilot",
    "name": "Pilot access",
    "description": "Placeholder tier until plans are set.",
    "includes": ["rest", "mcp"],
    "allowance": {"requests_per_minute": 120, "burst": 60, "concurrency": 8, "bytes_per_day": 1_000_000_000},
    "published": True,
}


@pytest.fixture
def accounts(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        ns = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    store = Accounts(pg_engine, ns, key_pepper=PEPPER)
    store.set_tiers([TIER])
    return store


def person(accounts, name="amina", subject=None):
    signed = accounts.sign_in(
        issuer=ISSUER,
        subject=subject or f"email|{name}",
        method="email",
        email=f"{name}@example.org",
        label=f"{name}@example.org",
    )
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    organisation = accounts.welcome(signed["id"], name.title(), name.title())
    return signed["id"], organisation


def check(accounts, request):
    with accounts.engine.begin() as conn:
        fn = accounts.namespace.qualified(conn, "policy", "check_credential")
        return conn.scalar(text(f"SELECT {fn}(CAST(:r AS jsonb))"), {"r": json.dumps(request)})


def check_key(accounts, secret):
    return check(
        accounts,
        {
            "kind": "key",
            "key_id": keys.parse(secret) or "",
            "secret_hash": keys.secret_hash(secret, PEPPER).hex(),
        },
    )


def grant(accounts, organisation, ends_at=None):
    return accounts.grant_access(
        "op-test",
        organisation,
        tier="pilot",
        scope={"all": True},
        source="manual",
        ends_at=ends_at,
        reason="Pilot",
    )


def sql(accounts, statement, **params):
    with accounts.engine.begin() as conn:
        names = {
            k: accounts.namespace.qualified(conn, "accounts", k)
            for k in (
                "person",
                "api_key",
                "entitlement",
                "audit_event",
                "console_session",
                "sign_in_flow",
                "membership",
                "usage_day",
                "invitation",
            )
        }
        return conn.execute(text(statement.format(**names)), params)


def test_sign_in_creates_once_and_never_links_by_email(accounts):
    first = accounts.sign_in(
        issuer=ISSUER,
        subject="google-oauth2|1",
        method="google",
        email="Amina@Example.ORG",
        label="Amina@Example.ORG",
    )
    again = accounts.sign_in(
        issuer=ISSUER, subject="google-oauth2|1", method="google", email="amina@example.org", label="x"
    )
    assert first["created"] and not again["created"] and again["id"] == first["id"]
    assert accounts.person(first["id"])["email"] == "Amina@example.org"  # local part kept, domain lowered
    with pytest.raises(AccountError) as refused:
        accounts.sign_in(
            issuer=ISSUER,
            subject="email|other",
            method="email",
            email="Amina@example.org",
            label="Amina@example.org",
        )
    assert refused.value.code == "email_in_use"


def test_terms_come_before_the_personal_organisation_which_is_made_once(accounts):
    signed = accounts.sign_in(
        issuer=ISSUER, subject="email|a", method="email", email="a@example.org", label="a"
    )
    assert set(accounts.person(signed["id"])["policies_pending"]) == set(policies.CURRENT)
    with pytest.raises(AccountError, match="Accept the terms"):
        accounts.welcome(signed["id"], "A", "A")
    with pytest.raises(AccountError) as changed:
        accounts.accept_policies(signed["id"], {"terms": "2000-01-01"})
    assert changed.value.code == "policy_changed"
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    first = accounts.welcome(signed["id"], "Amina Uwase", "Amina Uwase")
    assert accounts.welcome(signed["id"], "Amina", "Other") == first
    [organisation] = accounts.organisations(signed["id"])
    assert (
        organisation["personal"]
        and organisation["role"] == "owner"
        and organisation["access"]["state"] == "none"
    )


def test_sessions_resume_end_and_lapse_when_idle(accounts):
    person_id, _ = person(accounts)
    token, csrf = accounts.create_session(person_id, second_factor=False)
    found = accounts.resume(token)
    assert found["person_id"] == person_id and found["csrf"] == csrf and found["second_factor_at"] is None
    accounts.refresh_session(token, second_factor=True)
    assert accounts.resume(token)["second_factor_at"] is not None
    assert accounts.resume("wrong") is None
    sql(accounts, "UPDATE {console_session} SET last_seen_at = statement_timestamp() - interval '8 days'")
    assert accounts.resume(token) is None
    other, _ = accounts.create_session(person_id, second_factor=False)
    accounts.end_all_sessions(person_id)
    assert accounts.resume(other) is None


def test_sign_in_flow_is_single_use_bound_to_its_browser_and_expires(accounts):
    flow = accounts.begin_flow("sign_in", "/console")
    assert accounts.take_flow(flow["state"], "another-browser") is None
    assert accounts.take_flow(flow["state"], flow["browser"]) is None  # consumed by the failed attempt
    flow = accounts.begin_flow("sign_in", "/console/keys")
    taken = accounts.take_flow(flow["state"], flow["browser"])
    assert taken["verifier"] == flow["verifier"] and taken["return_to"] == "/console/keys"
    flow = accounts.begin_flow("sign_in", "/console")
    sql(accounts, "UPDATE {sign_in_flow} SET expires_at = statement_timestamp() - interval '1 second'")
    assert accounts.take_flow(flow["state"], flow["browser"]) is None
    with pytest.raises(DBAPIError):
        accounts.begin_flow("sign_in", "https://evil.example/console")


def test_access_request_grant_lapse_and_revocation_drive_the_credential_check(accounts):
    owner, organisation = person(accounts)
    with pytest.raises(AccountError) as no_access:
        accounts.create_key(owner, organisation, kind="personal", name="laptop")
    assert no_access.value.code == "entitlement_inactive"
    request = accounts.request_access(
        owner,
        organisation,
        {
            "purpose": "Research",
            "expected_volume": "1,000 a day",
            "scope": {"all": True},
            "reduced": True,
            "contact_name": "Amina",
            "contact_email": "amina@example.org",
        },
    )
    with pytest.raises(AccountError) as second:
        accounts.request_access(
            owner,
            organisation,
            {
                "purpose": "Again",
                "expected_volume": "1",
                "contact_name": "A",
                "contact_email": "a@example.org",
            },
        )
    assert second.value.code == "request_open"
    assert [r["id"] for r in accounts.open_requests()] == [request]
    accounts.grant_access(
        "op-test",
        organisation,
        tier="pilot",
        scope={"all": True},
        source="waiver",
        ends_at=None,
        reason="Research",
        request_id=request,
    )
    assert accounts.access_details(owner, organisation)["requests"][0]["state"] == "granted"
    key = accounts.create_key(owner, organisation, kind="personal", name="laptop")
    assert key["secret"].startswith("oa_live_") and key["id"] == "key_" + keys.parse(key["secret"])
    ok = check_key(accounts, key["secret"])
    assert ok["ok"] and ok["organisation"] == organisation and ok["tier"]["id"] == "pilot"
    assert ok["scope"] == {"all": True} and "email" not in json.dumps(ok)
    # The stored period ends in the past: refused at once, before any job records the lapse.
    sql(
        accounts,
        "UPDATE {entitlement} SET starts_at = statement_timestamp() - interval '2 days',"
        " ends_at = statement_timestamp() - interval '1 second'",
    )
    assert check_key(accounts, key["secret"]) == {"ok": False, "code": "entitlement_lapsed"}
    grant(accounts, organisation)  # renewal: the same key works again
    assert check_key(accounts, key["secret"])["ok"]
    accounts.revoke_access("op-test", organisation, "Abuse")
    assert check_key(accounts, key["secret"])["code"] == "entitlement_lapsed"
    assert accounts.list_keys(owner, organisation)[0]["state"] == "active"  # kept, not revoked


def test_access_periods_never_overlap(accounts):
    _, organisation = person(accounts)
    grant(accounts, organisation)
    with pytest.raises(DBAPIError, match="entitlement_overlap"):
        sql(
            accounts,
            "INSERT INTO {entitlement} (id, organisation_id, tier_id, source, scope, starts_at, state)"
            " VALUES ('ent_00000000000000000000000000', :o, 'pilot', 'manual', '{{\"all\": true}}',"
            " statement_timestamp(), 'active')",
            o=organisation,
        )
    grant(accounts, organisation)  # a change ends the current period in the same transaction
    states = sql(accounts, "SELECT state FROM {entitlement} ORDER BY created_at").scalars().all()
    assert states == ["ended", "active"]


def test_keys_are_checked_by_hash_and_refused_when_revoked_expired_or_malformed(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    key = accounts.create_key(owner, organisation, kind="service", name="server")
    assert check_key(accounts, key["secret"])["credential"] == key["id"]
    tampered = key["secret"][:-1] + ("A" if key["secret"][-1] != "A" else "B")
    assert check_key(accounts, tampered)["code"] == "invalid_credentials"
    assert (
        check(accounts, {"kind": "key", "key_id": "x' OR 1=1", "secret_hash": "00"})["code"]
        == "invalid_credentials"
    )
    assert sql(accounts, "SELECT octet_length(secret_hash) FROM {api_key}").scalar() == 32
    assert key["secret"] not in json.dumps(accounts.list_keys(owner, organisation), default=str)
    rotated = accounts.rotate_key(owner, organisation, key["id"], overlap_hours=0)
    assert check_key(accounts, rotated["secret"])["ok"]
    assert check_key(accounts, key["secret"])["code"] == "invalid_credentials"  # no overlap asked
    accounts.revoke_key(owner, organisation, rotated["id"])
    assert check_key(accounts, rotated["secret"])["code"] == "invalid_credentials"
    expiring = accounts.create_key(owner, organisation, kind="personal", name="short", days=1)
    sql(
        accounts,
        "UPDATE {api_key} SET created_at = statement_timestamp() - interval '2 days',"
        " expires_at = statement_timestamp() - interval '1 second' WHERE name = 'short'",
    )
    assert check_key(accounts, expiring["secret"])["code"] == "invalid_credentials"


def test_a_rotated_key_works_beside_its_replacement_through_the_overlap_and_then_stops(accounts):
    """MAC-05: rotation with at most 24 hours' overlap. Both keys authenticate during it; after it the
    old key is refused and the new one goes on."""
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    key = accounts.create_key(owner, organisation, kind="service", name="server")
    rotated = accounts.rotate_key(owner, organisation, key["id"], overlap_hours=6)
    assert rotated["name"] == "server" and rotated["kind"] == "service" and rotated["secret"] != key["secret"]
    assert check_key(accounts, key["secret"])["credential"] == key["id"]
    assert check_key(accounts, rotated["secret"])["credential"] == rotated["id"]
    old = key["id"].removeprefix("key_")
    left = sql(
        accounts, "SELECT expires_at - statement_timestamp() FROM {api_key} WHERE key_id = :k", k=old
    ).scalar()
    assert timedelta(hours=5, minutes=59) < left <= timedelta(hours=6)  # the old key ends with the overlap
    assert {k["id"]: k["state"] for k in accounts.list_keys(owner, organisation)} == {
        key["id"]: "active",
        rotated["id"]: "active",
    }
    # Six hours and a second later: the old key's times move back by that much.
    sql(
        accounts,
        "UPDATE {api_key} SET created_at = created_at - interval '6 hours 1 second',"
        " expires_at = expires_at - interval '6 hours 1 second' WHERE key_id = :k",
        k=old,
    )
    assert check_key(accounts, key["secret"])["code"] == "invalid_credentials"
    assert check_key(accounts, rotated["secret"])["credential"] == rotated["id"]
    assert {k["id"]: k["state"] for k in accounts.list_keys(owner, organisation)}[key["id"]] == "expired"
    [audit] = sql(accounts, "SELECT summary FROM {audit_event} WHERE action = 'key.rotated'").scalars().all()
    assert audit == {"replacement": rotated["id"], "overlap_hours": 6}


def test_a_rotation_overlap_is_at_most_24_hours_and_never_lengthens_a_key(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    key = accounts.create_key(owner, organisation, kind="service", name="server")
    for hours in (25, -1):
        with pytest.raises(AccountError) as refused:
            accounts.rotate_key(owner, organisation, key["id"], overlap_hours=hours)
        assert refused.value.code == "invalid_input"
    assert check_key(accounts, key["secret"])["credential"] == key["id"]  # a refused rotation changes nothing
    longest = accounts.rotate_key(owner, organisation, key["id"], overlap_hours=24)
    left = sql(
        accounts,
        "SELECT expires_at - statement_timestamp() FROM {api_key} WHERE key_id = :k",
        k=key["id"].removeprefix("key_"),
    ).scalar()
    assert timedelta(hours=23, minutes=59) < left <= timedelta(hours=24)
    assert check_key(accounts, key["secret"])["ok"] and check_key(accounts, longest["secret"])["ok"]
    # A key with two hours left keeps those two hours, not the overlap's 24.
    short = accounts.create_key(owner, organisation, kind="service", name="short", days=1)
    sql(
        accounts,
        "UPDATE {api_key} SET expires_at = statement_timestamp() + interval '2 hours' WHERE name = 'short'",
    )
    accounts.rotate_key(owner, organisation, short["id"], overlap_hours=24)
    left = sql(
        accounts,
        "SELECT expires_at - statement_timestamp() FROM {api_key} WHERE key_id = :k",
        k=short["id"].removeprefix("key_"),
    ).scalar()
    assert left <= timedelta(hours=2)


def refused(call, *args, **kwargs) -> str:
    with pytest.raises(AccountError) as error:
        call(*args, **kwargs)
    return error.value.code


def test_rotation_at_the_key_cap_succeeds_and_the_old_key_works_outside_it_until_its_overlap_ends(accounts):
    """MAC-05 (owner, 2026-10-21): no cap refuses a rotation. The new key takes the old one's place; the
    old key works beside it, uncounted, until its overlap ends, and only an overlap of 0 stops it at once.
    A new key at the cap is still refused."""
    owner, organisation = person(accounts)
    grant(accounts, organisation)

    def one_more(kind):
        return refused(accounts.create_key, owner, organisation, kind=kind, name="one more")

    full = [
        accounts.create_key(owner, organisation, kind="service", name=f"server {n}")
        for n in range(SERVICE_KEYS_PER_ORGANISATION)
    ]
    assert one_more("service") == "limit_reached"
    old = full[0]
    new = accounts.rotate_key(owner, organisation, old["id"], overlap_hours=6)
    assert check_key(accounts, old["secret"])["credential"] == old["id"]
    assert check_key(accounts, new["secret"])["credential"] == new["id"]
    assert one_more("service") == "limit_reached"
    listed = {k["id"]: k for k in accounts.list_keys(owner, organisation)}
    assert listed[old["id"]]["replaced"] and listed[old["id"]]["state"] == "active"
    assert not listed[new["id"]]["replaced"]
    # Six hours and a second later the old key is refused, and the cap is as full as before.
    sql(
        accounts,
        "UPDATE {api_key} SET created_at = created_at - interval '6 hours 1 second',"
        " expires_at = expires_at - interval '6 hours 1 second' WHERE key_id = :k",
        k=old["id"].removeprefix("key_"),
    )
    assert check_key(accounts, old["secret"])["code"] == "invalid_credentials"
    assert check_key(accounts, new["secret"])["credential"] == new["id"]
    assert one_more("service") == "limit_reached"
    # After a leak: no overlap, so the old key stops at once, still at the cap.
    leaked = full[1]
    replacement = accounts.rotate_key(owner, organisation, leaked["id"], overlap_hours=0)
    assert check_key(accounts, leaked["secret"])["code"] == "invalid_credentials"
    assert check_key(accounts, replacement["secret"])["credential"] == replacement["id"]
    assert one_more("service") == "limit_reached"
    # A member's personal keys have their own cap and rotate past it the same way.
    personal = [
        accounts.create_key(owner, organisation, kind="personal", name=f"laptop {n}")
        for n in range(PERSONAL_KEYS_PER_MEMBER)
    ]
    rotated = accounts.rotate_key(owner, organisation, personal[0]["id"], overlap_hours=1)
    assert check_key(accounts, personal[0]["secret"])["ok"] and check_key(accounts, rotated["secret"])["ok"]
    assert one_more("personal") == "limit_reached"


def test_a_key_has_at_most_one_predecessor_working_and_a_replaced_key_is_not_rotated_again(accounts):
    """Rotating a key again during its predecessor's overlap stops the predecessor now, so one key's place
    never holds more than one key outside the cap; the key already replaced is refused a second rotation."""
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    first = accounts.create_key(owner, organisation, kind="service", name="server")
    second = accounts.rotate_key(owner, organisation, first["id"], overlap_hours=6)
    assert (
        refused(accounts.rotate_key, owner, organisation, first["id"], overlap_hours=6) == "already_rotated"
    )
    assert check_key(accounts, first["secret"])["ok"]  # a refused rotation changes nothing
    third = accounts.rotate_key(owner, organisation, second["id"], overlap_hours=6)
    assert check_key(accounts, first["secret"])["code"] == "invalid_credentials"
    assert check_key(accounts, second["secret"])["credential"] == second["id"]
    assert check_key(accounts, third["secret"])["credential"] == third["id"]
    reasons = dict(sql(accounts, "SELECT 'key_' || key_id, revoked_reason FROM {api_key}").all())
    assert reasons == {first["id"]: "rotated", second["id"]: None, third["id"]: None}
    summaries = sql(
        accounts, "SELECT summary FROM {audit_event} WHERE action = 'key.rotated' ORDER BY seq"
    ).scalars()
    assert list(summaries) == [
        {"replacement": second["id"], "overlap_hours": 6},
        {"replacement": third["id"], "overlap_hours": 6, "ended_predecessor": first["id"]},
    ]


def test_roles_limit_keys_and_leaving_revokes_personal_keys_and_apps(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    dev, _ = person(accounts, "dev")
    viewer, _ = person(accounts, "viewer")
    for who, role in ((dev, "developer"), (viewer, "viewer")):
        invitation = accounts.invite(
            owner, organisation, f"{'dev' if who == dev else 'viewer'}@example.org", role
        )
        accounts.accept_invitation(who, invitation)
    with pytest.raises(AccountError) as viewer_key:
        accounts.create_key(viewer, organisation, kind="personal", name="v")
    assert viewer_key.value.code == "role_forbidden"
    with pytest.raises(AccountError):
        accounts.create_key(dev, organisation, kind="service", name="s")
    personal = accounts.create_key(dev, organisation, kind="personal", name="dev laptop")
    service = accounts.create_key(owner, organisation, kind="service", name="server")
    app = accounts.create_grant(
        dev,
        organisation,
        client_id="https://claude.ai/oauth/mcp",
        client_name="Claude",
        client_label="verified_publisher",
        client_host="claude.ai",
        resource="mcp",
    )
    accounts.link_identity(dev, issuer=ISSUER, subject="google-oauth2|dev", method="google", label="dev")
    token = {
        "kind": "grant",
        "grant_id": app,
        "resource": "mcp",
        "issuer": ISSUER,
        "subject": "google-oauth2|dev",
    }
    assert check(accounts, token)["ok"]
    assert check(accounts, token | {"resource": "api"})["code"] == "invalid_credentials"
    assert check(accounts, token | {"subject": "google-oauth2|someone"})["code"] == "invalid_credentials"
    assert [k["name"] for k in accounts.list_keys(dev, organisation)] == ["dev laptop"]
    assert (
        len(accounts.list_keys(owner, organisation)) == 2 and accounts.list_keys(viewer, organisation) == []
    )
    members = {m["display_name"]: m for m in accounts.members(owner, organisation)["members"]}
    assert members["Dev"]["email"] == "dev@example.org"
    assert all(m["email"] is None for m in accounts.members(dev, organisation)["members"])
    accounts.remove_member(dev, organisation, members["Dev"]["id"])  # leaving
    assert check_key(accounts, personal["secret"])["code"] == "invalid_credentials"
    assert check(accounts, token)["code"] == "invalid_credentials"
    assert check_key(accounts, service["secret"])["ok"]  # service keys survive member changes
    accounts.change_role(owner, organisation, members["Viewer"]["id"], "developer")
    with pytest.raises(AccountError):
        accounts.change_role(viewer, organisation, members["Viewer"]["id"], "admin")  # never raise yourself


def test_the_last_owner_cannot_leave_be_demoted_or_race_away(accounts):
    owner, organisation = person(accounts)
    [mine] = [m for m in accounts.members(owner, organisation)["members"] if m["you"]]
    for change in (
        lambda: accounts.remove_member(owner, organisation, mine["id"]),
        lambda: accounts.change_role(owner, organisation, mine["id"], "admin"),
    ):
        with pytest.raises(AccountError) as refused:
            change()
        assert refused.value.code == "last_owner"
    second, _ = person(accounts, "second")
    accounts.accept_invitation(second, accounts.invite(owner, organisation, "second@example.org", "owner"))
    members = {m["display_name"]: m["id"] for m in accounts.members(owner, organisation)["members"]}
    results, barrier = [], threading.Barrier(2)

    def leave(who, membership):
        barrier.wait()
        try:
            accounts.remove_member(who, organisation, membership)
            results.append("left")
        except AccountError as error:
            results.append(error.code)

    threads = [
        threading.Thread(target=leave, args=(owner, members["Amina"])),
        threading.Thread(target=leave, args=(second, members["Second"])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["last_owner", "left"]


def test_invitations_are_accepted_only_by_the_invited_verified_email(accounts):
    owner, organisation = person(accounts)
    invitation = accounts.invite(owner, organisation, "guest@example.org", "developer")
    stranger, _ = person(accounts, "stranger")
    with pytest.raises(AccountError) as wrong:
        accounts.accept_invitation(stranger, invitation)
    assert wrong.value.code == "invitation_invalid"
    with pytest.raises(AccountError) as duplicate:
        accounts.invite(owner, organisation, "guest@example.org", "viewer")
    assert duplicate.value.code == "already_invited"
    guest, _ = person(accounts, "guest")
    assert [i["id"] for i in accounts.invitations_for(guest)] == [invitation]
    accounts.accept_invitation(guest, invitation)
    with pytest.raises(AccountError):
        accounts.accept_invitation(guest, invitation)
    with pytest.raises(AccountError):
        accounts.invite(guest, organisation, "x@example.org", "viewer")  # developers do not invite


def test_audit_log_is_append_only_and_shown_to_managers(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    accounts.create_key(owner, organisation, kind="personal", name="k")
    actions = [e["action"] for e in accounts.audit_log(owner, organisation)]
    assert actions[:3] == ["key.created", "entitlement.granted", "organisation.created"]
    assert accounts.audit_log(owner, organisation)[0]["actor"] == "Amina"
    for statement in ("UPDATE {audit_event} SET action = 'x.y'", "DELETE FROM {audit_event}"):
        with pytest.raises(DBAPIError, match="append_only"):
            sql(accounts, statement)


def test_usage_counts_requests_bytes_and_refusals_by_day(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    key = accounts.create_key(owner, organisation, kind="personal", name="k")
    with accounts.engine.begin() as conn:
        fn = accounts.namespace.qualified(conn, "policy", "record_usage")
        for entry in ({"bytes": 100}, {"bytes": 50}, {"refusal": "rate_limited"}):
            conn.execute(
                text(f"SELECT {fn}(CAST(:e AS jsonb))"),
                {
                    "e": json.dumps(
                        {"organisation": organisation, "credential": key["id"], "class": "rest", **entry}
                    )
                },
            )
    with pytest.raises(DBAPIError), accounts.engine.begin() as conn:
        conn.execute(
            text(f"SELECT {fn}(CAST(:e AS jsonb))"),
            {
                "e": json.dumps(
                    {"organisation": organisation, "credential": key["id"], "class": "rest", "refusal": "a b"}
                )
            },
        )
    [day] = accounts.usage(owner, organisation)
    assert (day["requests"], day["bytes"], day["refusals"]) == (2, 150, {"rate_limited": 1})


def test_deleting_an_organisation_revokes_everything(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    key = accounts.create_key(owner, organisation, kind="service", name="k")
    accounts.delete_organisation(owner, organisation)
    assert check_key(accounts, key["secret"])["code"] == "invalid_credentials"
    assert accounts.organisations(owner) == []
    assert datetime.now(UTC) - timedelta(minutes=5) < datetime.now(UTC)


STARTER = {
    "id": "starter",
    "name": "Starter",
    "description": "Free",
    "includes": ["rest", "mcp"],
    "allowance": {"requests_per_minute": 60, "burst": 30, "concurrency": 4},
    "published": True,
}


def test_every_organisation_starts_with_free_starter_access(accounts):
    accounts.set_tiers([TIER, STARTER])
    owner, organisation = person(accounts)
    access = accounts.access(organisation)
    assert access["state"] == "active" and access["starter"] is True and access["tier"]["id"] == "starter"
    key = accounts.create_key(owner, organisation, kind="personal", name="first")  # no request first
    assert check_key(accounts, key["secret"])["tier"]["id"] == "starter"
    with pytest.raises(AccountError) as cannot_end:
        accounts.end_access(owner, organisation)
    assert cannot_end.value.code == "entitlement_inactive"
    # Approved access replaces starter while it lasts, then starter returns.
    grant(accounts, organisation)
    assert check_key(accounts, key["secret"])["tier"]["id"] == "pilot"
    sql(
        accounts,
        "UPDATE {entitlement} SET starts_at = statement_timestamp() - interval '2 days',"
        " ends_at = statement_timestamp() - interval '1 second'",
    )
    assert check_key(accounts, key["secret"])["tier"]["id"] == "starter"
    # The operator's revocation stops starter access too, until a new grant.
    accounts.revoke_access("op-test", organisation, "Abuse")
    assert check_key(accounts, key["secret"]) == {"ok": False, "code": "entitlement_lapsed"}
    assert accounts.access(organisation)["state"] == "revoked"
    grant(accounts, organisation)
    assert check_key(accounts, key["secret"])["ok"]


def test_an_organisation_with_only_starter_access_can_be_deleted(accounts):
    accounts.set_tiers([TIER, STARTER])
    owner, organisation = person(accounts)
    key = accounts.create_key(owner, organisation, kind="service", name="k")
    accounts.delete_organisation(owner, organisation)
    assert check_key(accounts, key["secret"])["code"] == "invalid_credentials"


def test_a_request_names_a_published_plan_and_the_operator_grants_it(accounts, monkeypatch):
    from accounts import operator

    developer = TIER | {"id": "developer", "name": "Developer", "published": True}
    hidden = TIER | {"id": "hidden", "name": "Hidden", "published": False}
    accounts.set_tiers([TIER, STARTER, developer, hidden])
    owner, organisation = person(accounts)
    ask = {
        "purpose": "Research",
        "expected_volume": "1",
        "contact_name": "A",
        "contact_email": "a@example.org",
    }
    for refused in ("starter", "hidden", "nonexistent", {"id": "developer"}):
        with pytest.raises(AccountError) as wrong:
            accounts.request_access(owner, organisation, ask | {"tier": refused})
        assert wrong.value.code == "invalid_input"
    request = accounts.request_access(owner, organisation, ask | {"tier": "developer"})
    [listed] = accounts.open_requests()
    assert listed["id"] == request and listed["tier_requested"] == "developer"
    assert accounts.access_details(owner, organisation)["requests"][0]["tier_requested"] == "developer"
    # Without --tier the operator grants the plan the request named.
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(operator, "engine_for", lambda: accounts.engine)
    argv = ["--operator", "op-test", "grant", organisation, "--reason", "Asked", "--request", request]
    assert operator.main(argv) == 0
    tier = accounts.access(organisation)["tier"]
    assert (tier["id"], tier["name"]) == ("developer", "Developer")
    assert accounts.open_requests() == []


def test_a_plan_with_countries_takes_the_requests_countries_and_changes_them_once_a_month(
    accounts, monkeypatch
):
    from accounts import operator

    developer = TIER | {
        "id": "developer",
        "name": "Developer",
        "allowance": TIER["allowance"] | {"jurisdictions": 2},
    }
    enterprise = TIER | {"id": "enterprise", "name": "Enterprise"}
    accounts.set_tiers([TIER, STARTER, developer, enterprise])
    owner, organisation = person(accounts)
    ask = {
        "purpose": "Research",
        "expected_volume": "1",
        "contact_name": "A",
        "contact_email": "a@example.org",
    }
    for wrong in ({"all": True}, {"jurisdictions": ["rw", "ng", "ke"]}, {"jurisdictions": ["rwa"]}):
        with pytest.raises(AccountError) as refused:
            accounts.request_access(owner, organisation, ask | {"tier": "developer", "scope": wrong})
        assert refused.value.code == "invalid_input"
    # A plan without a number of countries asks for all of them, whatever was sent.
    asked = accounts.request_access(
        owner, organisation, ask | {"tier": "enterprise", "scope": {"jurisdictions": ["rw"]}}
    )
    assert accounts.open_requests()[0]["scope_requested"] == {"all": True}
    accounts.withdraw_request(owner, organisation, asked)
    request = accounts.request_access(
        owner, organisation, ask | {"tier": "developer", "scope": {"jurisdictions": ["rw", "ng"]}}
    )
    # The operator's grant takes the plan and the countries the request named.
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(operator, "engine_for", lambda: accounts.engine)
    argv = ["--operator", "op-test", "grant", organisation, "--reason", "Asked", "--request", request]
    assert operator.main(argv) == 0
    assert accounts.access(organisation)["scope"] == {"jurisdictions": ["ng", "rw"]}
    with pytest.raises(AccountError) as too_many:
        accounts.grant_access(
            "op-test",
            organisation,
            tier="developer",
            scope={"all": True},
            source="manual",
            ends_at=None,
            reason="No",
        )
    assert too_many.value.code == "invalid_input"
    # An owner swaps a country; the next swap waits 30 days.
    assert accounts.access_details(owner, organisation)["country_change_after"] is None
    accounts.change_countries(owner, organisation, ["rw", "ke"])
    access = accounts.access(organisation)
    assert access["scope"] == {"jurisdictions": ["ke", "rw"]} and access["tier"]["id"] == "developer"
    with pytest.raises(AccountError) as soon:
        accounts.change_countries(owner, organisation, ["rw", "ug"])
    assert soon.value.code == "change_too_soon" and "once a month" in soon.value.message
    assert accounts.access_details(owner, organisation)["country_change_after"] is not None
    monkeypatch.setattr("accounts.store.COUNTRY_CHANGE_DAYS", 0)  # the audit log cannot be backdated
    accounts.change_countries(owner, organisation, ["ug"])
    assert accounts.access(organisation)["scope"] == {"jurisdictions": ["ug"]}
    states = [h["state"] for h in accounts.access_details(owner, organisation)["history"]]
    assert states[:3] == ["active", "ended", "ended"]


def test_an_organisation_can_set_its_website(accounts):
    owner, organisation = person(accounts)
    revision = accounts.organisation(owner, organisation)["revision"]
    accounts.update_organisation(owner, organisation, revision, {"website": "https://law.example.org/about"})
    assert accounts.organisation(owner, organisation)["website"] == "https://law.example.org/about"
