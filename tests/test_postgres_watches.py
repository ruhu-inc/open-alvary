"""SUB C2: watches, their inbox and digest email (changes-contracts.md)."""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from accounts import policies
from accounts.store import TIERS, AccountError, Accounts
from api.store import Store, seed
from changes import admin, worker
from changes.watches import Watches
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
BASE = "https://open.example.org"


class Mailer:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True


@pytest.fixture
def world(pg_engine):
    migrate(pg_engine)
    store = Store(pg_engine)
    seed(store)  # the change log's baseline
    with pg_engine.connect() as conn:
        accounts = Accounts(pg_engine, PolicyNamespace(conn.scalar(text("SELECT current_schema()"))))
    accounts.set_tiers(json.loads(TIERS.read_text()))
    return store, accounts, Watches(accounts)


def person(accounts, name):
    signed = accounts.sign_in(
        issuer="https://login.example.test/",
        subject=f"email|{name}",
        method="email",
        email=f"{name}@example.org",
        label=name,
    )
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    return signed["id"], accounts.welcome(signed["id"], name.title(), name.title())


def change(store, summary, work="ng-foi-act-2011"):
    corpus = store.load()
    next(s for s in corpus.sources if s.id == work).summary = summary
    store.save(corpus)


def grant(accounts, organisation, tier):
    accounts.grant_access(
        "op",
        organisation,
        tier=tier,
        scope={"jurisdictions": ["ng"]},
        source="manual",
        ends_at=None,
        reason="Test",
    )


NIGERIA = {"jurisdiction": ["ng"]}


def test_starter_limits_and_what_a_watch_must_name(world):
    _, accounts, watches = world
    owner, organisation = person(accounts, "amina")
    for refused, code in (
        ({"name": "x", "filters": {}}, "invalid_input"),
        ({"name": "x", "filters": {"jurisdiction": ["ke"]}}, "invalid_filter"),
        ({"name": "x", "filters": NIGERIA, "email": "daily"}, "limit_reached"),
        ({"name": "x", "filters": NIGERIA, "visibility": "organisation"}, "limit_reached"),
    ):
        with pytest.raises(AccountError) as error:
            watches.create(owner, organisation, refused)
        assert error.value.code == code
    for i in range(3):
        watches.create(owner, organisation, {"name": f"Nigeria {i}", "filters": NIGERIA, "email": "weekly"})
    with pytest.raises(AccountError) as full:
        watches.create(owner, organisation, {"name": "One more", "filters": NIGERIA})
    assert full.value.code == "limit_reached"
    listed = watches.listing(owner, organisation)
    assert listed["limits"] == {"watches": 3, "digest": "weekly", "shared": False, "used": 3}


def test_a_watch_matches_only_later_changes_once_and_not_while_paused(world):
    store, accounts, watches = world
    owner, organisation = person(accounts, "amina")
    change(store, "Before the watch.")
    watch = watches.create(owner, organisation, {"name": "Nigeria", "filters": NIGERIA})
    worker.match(accounts)  # the first run starts the matcher at the current end
    change(store, "After the watch.")
    assert worker.match(accounts) == 1
    assert worker.match(accounts) == 0  # nothing twice
    [item] = watches.inbox(owner, organisation, BASE)["items"]
    assert item["event"]["changed_fields"] == ["summary"] and item["watch"]["name"] == "Nigeria"
    assert item["match_reasons"]["matched"] == ["jurisdiction"] and not item["read"]
    watches.mark_read(owner, organisation)
    assert watches.inbox(owner, organisation, BASE, unread=True)["items"] == []
    watches.update(owner, organisation, watch["id"], {"state": "paused"})
    change(store, "While paused.")
    assert worker.match(accounts) == 0
    watches.update(owner, organisation, watch["id"], {"state": "active"})  # continues from now
    change(store, "After resuming.")
    assert worker.match(accounts) == 1
    # A record that stops being public leaves the inbox too.
    corpus = store.load()
    next(r for r in corpus.rights if r.source_id == "ng-foi-act-2011").metadata_public = False
    store.save(corpus)
    assert all(i["event"]["work"] is None for i in watches.inbox(owner, organisation, BASE)["items"])


def test_private_watches_stay_private_and_shared_ones_need_a_team_plan(world):
    store, accounts, watches = world
    owner, organisation = person(accounts, "amina")
    stranger, _ = person(accounts, "bola")
    private = watches.create(owner, organisation, {"name": "Mine", "filters": NIGERIA})
    with pytest.raises(AccountError) as hidden:
        watches.update(stranger, organisation, private["id"], {"name": "x"})
    assert hidden.value.code == "not_found"
    grant(accounts, organisation, "team")
    shared = watches.create(
        owner, organisation, {"name": "Ours", "filters": NIGERIA, "visibility": "organisation"}
    )
    colleague, _ = person(accounts, "chidi")
    invitation = accounts.invite(owner, organisation, "chidi@example.org", "developer")
    accounts.accept_invitation(colleague, invitation)
    seen = {w["name"] for w in watches.listing(colleague, organisation)["watches"]}
    assert seen == {"Ours"}
    with pytest.raises(AccountError) as not_owner:
        watches.update(colleague, organisation, shared["id"], {"name": "Renamed"})
    assert not_owner.value.code == "role_forbidden"
    watches.set_email(colleague, organisation, shared["id"], "daily")  # their own email, their choice


def test_a_digest_groups_matches_carries_one_click_unsubscribe_and_never_repeats(world):
    store, accounts, watches = world
    owner, organisation = person(accounts, "amina")
    watches.set_preferences(owner, {"timezone": "Africa/Kigali", "daily_time": "07:00", "weekly_day": 1})
    watch = watches.create(owner, organisation, {"name": "Nigeria", "filters": NIGERIA, "email": "weekly"})
    worker.match(accounts)
    change(store, "One.")
    change(store, "Two.", work="ng-constitution-1999")
    assert worker.match(accounts) == 2
    mailer = Mailer()
    later = datetime.now(UTC) + timedelta(days=8)
    assert worker.digests(accounts, mailer, BASE, now=later) == 1
    [message] = mailer.sent
    assert message.to == "amina@example.org" and "2 changes" in message.subject
    headers = dict(message.headers)
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "Nigeria" in message.text and "/console/monitor" in message.text
    assert worker.digests(accounts, mailer, BASE, now=later) == 0 and len(mailer.sent) == 1
    # The one-click link: describing changes nothing, posting unsubscribes, a bad token never succeeds.
    token = headers["List-Unsubscribe"].strip("<>").rsplit("/", 1)[1]
    assert watches.describe_unsubscribe(token)["scope"] == "all"
    assert watches.preferences(owner)["email_on"] is True
    assert watches.unsubscribe(token)["done"] and watches.preferences(owner)["email_on"] is False
    with pytest.raises(AccountError) as unknown:
        watches.unsubscribe("x" * 43)
    assert "couldn't verify" in unknown.value.message
    # With email off, matches are kept in the inbox but nothing is sent.
    change(store, "Three.")
    worker.match(accounts)
    assert worker.digests(accounts, mailer, BASE, now=later + timedelta(days=7)) == 0
    assert len(watches.inbox(owner, organisation, BASE)["items"]) == 3
    assert watch["email"] == "weekly"


def test_scheduled_sends_survive_clock_changes():
    london = ZoneInfo("Europe/London")
    skipped = worker.local_instant(london, date(2026, 3, 29), 90)  # 01:30 does not exist that day
    assert skipped.astimezone(london).strftime("%H:%M") == "02:00"
    repeated = worker.local_instant(london, date(2026, 10, 25), 90)  # 01:30 happens twice
    assert repeated == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # the first, in summer time
    kigali = ZoneInfo("Africa/Kigali")
    monday = worker.last_send(kigali, 420, 1, "weekly", datetime(2026, 10, 21, 12, tzinfo=UTC))
    assert monday.astimezone(kigali).isoweekday() == 1 and monday.astimezone(kigali).hour == 7


def test_going_down_a_plan_pauses_the_newest_watches_and_tells_the_owner(world):
    _, accounts, watches = world
    owner, organisation = person(accounts, "amina")
    grant(accounts, organisation, "developer")
    for i in range(5):
        watches.create(owner, organisation, {"name": f"W{i}", "filters": NIGERIA})
    accounts.end_access(owner, organisation)  # back to Starter: 3 watches
    mailer = Mailer()
    assert worker.enforce_limits(accounts, mailer, BASE) == 2
    states = {w["name"]: w["state"] for w in watches.listing(owner, organisation)["watches"]}
    assert states == {"W0": "active", "W1": "active", "W2": "active", "W3": "paused", "W4": "paused"}
    assert "2 monitors were paused" in mailer.sent[0].subject


def test_a_feed_reset_stops_matching_until_the_operator_confirms(world, monkeypatch):
    store, accounts, watches = world
    owner, organisation = person(accounts, "amina")
    watches.create(owner, organisation, {"name": "Nigeria", "filters": NIGERIA})
    worker.match(accounts)
    monkeypatch.setattr(admin, "engine_for", lambda: store.engine)
    admin.main(["new-epoch", "--reason", "Restore drill"])
    with pytest.raises(worker.EpochChanged):
        worker.match(accounts)
    worker.confirm_epoch(accounts)
    change(store, "After the reset.")
    assert worker.match(accounts) == 1


def test_the_console_routes_and_one_click_unsubscribe(world):
    from fastapi.testclient import TestClient
    from test_postgres_console import FakeProvider, app_provider, client, onboard

    from api.console import create_console_app

    store, accounts, watches = world
    fake = FakeProvider()
    browser = client(create_console_app(accounts, app_provider(fake)))
    organisation, headers, _ = onboard(browser, fake)
    path = f"/api/v1/organisations/{organisation}/watches"
    preview = browser.post(path + "/preview", json={"filters": NIGERIA}, headers=headers)
    assert preview.status_code == 200 and "notice" in preview.json(), preview.text
    created = browser.post(
        path, json={"name": "Nigeria", "filters": NIGERIA, "email": "weekly"}, headers=headers
    )
    assert created.status_code == 201 and created.json()["email"] == "weekly"
    assert browser.post(path, json={"name": "No CSRF", "filters": NIGERIA}).status_code in (401, 403)
    listed = browser.get(path).json()
    assert [w["name"] for w in listed["watches"]] == ["Nigeria"] and listed["limits"]["used"] == 1
    prefs = browser.patch("/api/v1/me/notifications", json={"timezone": "Africa/Lagos"}, headers=headers)
    assert prefs.json()["timezone"] == "Africa/Lagos"
    assert (
        browser.patch("/api/v1/me/notifications", json={"timezone": "Mars/Base"}, headers=headers).status_code
        == 400
    )
    # A token from a digest works with no session at all.
    with accounts.engine.begin() as conn:
        person = conn.scalar(text(f"SELECT owner_person_id FROM {accounts._t(conn, 'watch')}"))
        token = watches.issue_token(conn, person, created.json()["id"])
    anonymous = TestClient(create_console_app(accounts, app_provider(fake)))
    assert anonymous.get(f"/api/v1/unsubscribe/{token}").json() == {"scope": "watch", "watch_name": "Nigeria"}
    assert anonymous.post(f"/unsubscribe/{token}").json()["done"]
    assert browser.get(path).json()["watches"][0]["email"] == "off"
    assert anonymous.post("/unsubscribe/" + "y" * 43).status_code == 404
    assert browser.delete(f"{path}/{created.json()['id']}", headers=headers).status_code == 204
    assert browser.get(path).json()["watches"] == []


def test_old_matches_digest_records_and_links_are_purged(world):
    store, accounts, watches = world
    owner, organisation = person(accounts, "amina")
    watches.create(owner, organisation, {"name": "Nigeria", "filters": NIGERIA})
    worker.match(accounts)
    change(store, "One.")
    worker.match(accounts)
    with accounts.engine.begin() as conn:
        watches.issue_token(conn, owner, None)
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'inbox_item')} SET matched_at = matched_at - interval '401 days'"
            )
        )
        conn.execute(
            text(f"UPDATE {accounts._t(conn, 'unsubscribe_token')} SET expires_at = now() - interval '1 day'")
        )
    assert worker.purge(accounts) == 2
    assert watches.inbox(owner, organisation, BASE)["items"] == []


def test_bounces_and_complaints_from_the_mail_provider_stop_digests(world, monkeypatch):
    import base64
    import hashlib
    import hmac
    import time

    from fastapi.testclient import TestClient
    from test_postgres_console import FakeProvider, app_provider

    from api.console import create_console_app

    _, accounts, watches = world
    owner, _ = person(accounts, "amina")
    key = b"resend-signing-key-for-tests-123"
    secret = "whsec_" + base64.b64encode(key).decode()
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", secret)
    client = TestClient(create_console_app(accounts, app_provider(FakeProvider())))

    def post(event, signing_key=key):
        body = json.dumps(event).encode()
        stamp = str(int(time.time()))
        signature = base64.b64encode(
            hmac.new(signing_key, b"msg_1." + stamp.encode() + b"." + body, hashlib.sha256).digest()
        )
        headers = {"svix-id": "msg_1", "svix-timestamp": stamp, "svix-signature": "v1," + signature.decode()}
        return client.post("/mail-events", content=body, headers=headers)

    assert (
        post(
            {"type": "email.bounced", "data": {"to": ["amina@example.org"]}}, signing_key=b"x" * 32
        ).status_code
        == 401
    )
    transient = {
        "type": "email.bounced",
        "data": {"to": ["amina@example.org"], "bounce": {"type": "Transient"}},
    }
    assert post(transient).status_code == 204 and not watches.preferences(owner)["bounced"]
    assert post({"type": "email.bounced", "data": {"to": ["AMINA@example.org"]}}).status_code == 204
    assert watches.preferences(owner)["bounced"] and watches.preferences(owner)["email_on"]
    watches.set_preferences(owner, {"email_on": True})  # turning email on again clears the bounce
    assert not watches.preferences(owner)["bounced"]
    assert post({"type": "email.complained", "data": {"to": ["amina@example.org"]}}).status_code == 204
    assert watches.preferences(owner)["email_on"] is False
