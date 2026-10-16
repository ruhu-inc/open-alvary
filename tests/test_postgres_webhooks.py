"""SUB C3: webhook endpoints, the challenge handshake, signed deliveries, retries, pausing,
withholding, replay and rotation (changes-contracts.md)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from accounts import policies
from accounts.store import TIERS, AccountError, Accounts
from api.store import Store, seed
from changes import worker
from changes.watches import Watches
from changes.webhooks import RETRIES, Sealer, Sender, Webhooks, deliver, verify_signature
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
BASE = "https://open.example.org"
URL = "https://hooks.example.org/open-alvary"
NIGERIA = {"jurisdiction": ["ng"]}


class Endpoint:
    """A fake receiver: answers the challenge, then whatever the test scripts."""

    def __init__(self):
        self.requests, self.answers = [], []

    def __call__(self, url, address, body, headers):
        self.requests.append((body, headers))
        payload = json.loads(body)
        if payload["type"] == "endpoint.verification":
            return 200, {}, json.dumps({"challenge": payload["challenge"]}).encode()
        status, extra = self.answers.pop(0) if self.answers else (200, {})
        return status, extra, b"ok"

    def deliveries(self):
        return [(json.loads(b), h) for b, h in self.requests if json.loads(b)["type"] == "change"]


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
    seed(store)
    with pg_engine.connect() as conn:
        accounts = Accounts(pg_engine, PolicyNamespace(conn.scalar(text("SELECT current_schema()"))))
    accounts.set_tiers(json.loads(TIERS.read_text()))
    signed = accounts.sign_in(
        issuer="https://login.example.test/",
        subject="email|a",
        method="email",
        email="a@example.org",
        label="a",
    )
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    organisation = accounts.welcome(signed["id"], "Amina", "Amina")
    receiver = Endpoint()
    hooks = Webhooks(
        accounts,
        Sender(resolver=lambda host: ["93.184.216.34"], transport=receiver),
        Sealer({"k": b"k" * 32}, "k"),
    )
    return store, accounts, hooks, receiver, signed["id"], organisation


def plan(accounts, organisation, tier):
    accounts.grant_access(
        "op",
        organisation,
        tier=tier,
        scope={"jurisdictions": ["ng"]},
        source="manual",
        ends_at=None,
        reason="Test",
    )


def change(store, summary):
    corpus = store.load()
    next(s for s in corpus.sources if s.id == "ng-foi-act-2011").summary = summary
    store.save(corpus)


def endpoint_with_monitor(world):
    store, accounts, hooks, receiver, owner, organisation = world
    plan(accounts, organisation, "developer")
    monitor = Watches(accounts).create(owner, organisation, {"name": "Nigeria", "filters": NIGERIA})
    created = hooks.create(owner, organisation, {"url": URL, "watch_ids": [monitor["id"]]})
    worker.match(accounts)  # starts the matcher at the current end
    return created, monitor


def delivery(accounts):
    with accounts.engine.connect() as conn:
        return dict(
            conn.execute(
                text(f"SELECT * FROM {accounts._t(conn, 'webhook_delivery')} ORDER BY created_at DESC")
            )
            .mappings()
            .first()
        )


def test_webhooks_come_with_paid_plans(world):
    store, accounts, hooks, receiver, owner, organisation = world
    monitor = Watches(accounts).create(owner, organisation, {"name": "N", "filters": NIGERIA})
    with pytest.raises(AccountError) as starter:
        hooks.create(owner, organisation, {"url": URL, "watch_ids": [monitor["id"]]})
    assert starter.value.code == "limit_reached" and "paid plans" in starter.value.message


def test_the_handshake_activates_an_endpoint_and_its_secret_is_shown_once(world):
    created, _ = endpoint_with_monitor(world)
    store, accounts, hooks, receiver, owner, organisation = world
    assert created["verification"]["verified"] and created["endpoint"]["state"] == "active"
    secret = created["secret"]
    assert secret.startswith("whsec_")
    listed = hooks.listing(owner, organisation)
    assert secret not in json.dumps(listed, default=str) and listed["used"] == 1 and listed["limit"] == 1
    with accounts.engine.connect() as conn:
        sealed = conn.scalar(text(f"SELECT secret_sealed FROM {accounts._t(conn, 'webhook_key')}"))
    assert secret not in sealed
    # The handshake itself is signed with the secret.
    body, headers = receiver.requests[0]
    assert verify_signature(secret, body, headers["Open-Alvary-Timestamp"], headers["Open-Alvary-Signature"])


def test_an_endpoint_that_does_not_echo_stays_unverified(world):
    store, accounts, hooks, receiver, owner, organisation = world
    plan(accounts, organisation, "developer")
    monitor = Watches(accounts).create(owner, organisation, {"name": "N", "filters": NIGERIA})
    hooks.sender.transport = lambda *a: (200, {}, b'{"challenge": "wrong"}')
    created = hooks.create(owner, organisation, {"url": URL, "watch_ids": [monitor["id"]]})
    assert created["endpoint"]["state"] == "pending_verification"
    assert not created["verification"]["verified"] and "challenge" in created["verification"]["error"]


def test_a_match_is_delivered_signed_once(world):
    created, monitor = endpoint_with_monitor(world)
    store, accounts, hooks, receiver, owner, organisation = world
    change(store, "Delivered.")
    worker.match(accounts)
    assert deliver(accounts, hooks, Mailer(), BASE)["delivered"] == 1
    [(payload, headers)] = receiver.deliveries()
    assert payload["watch_ids"] == [monitor["id"]] and payload["event"]["changed_fields"] == ["summary"]
    assert headers["Open-Alvary-Delivery"] == payload["delivery_id"]
    body = next(b for b, h in receiver.requests if h is headers)
    assert verify_signature(
        created["secret"], body, headers["Open-Alvary-Timestamp"], headers["Open-Alvary-Signature"]
    )
    assert delivery(accounts)["state"] == "delivered"
    assert deliver(accounts, hooks, Mailer(), BASE)["delivered"] == 0  # nothing twice


def test_failures_retry_on_schedule_and_rejections_stop(world):
    endpoint_with_monitor(world)
    store, accounts, hooks, receiver, owner, organisation = world
    change(store, "Retry me.")
    worker.match(accounts)
    now = datetime.now(UTC)
    receiver.answers = [(500, {})]
    deliver(accounts, hooks, Mailer(), BASE, now=now)
    first = delivery(accounts)
    assert first["state"] == "pending" and first["attempts"] == 1
    assert abs((first["next_attempt_at"] - now).total_seconds() - RETRIES[0]) < 2
    receiver.answers = [(429, {"retry-after": "900"})]
    deliver(accounts, hooks, Mailer(), BASE, now=first["next_attempt_at"])
    second = delivery(accounts)
    assert (second["next_attempt_at"] - first["next_attempt_at"]).total_seconds() >= 900
    receiver.answers = [(302, {})]
    deliver(accounts, hooks, Mailer(), BASE, now=second["next_attempt_at"])
    assert "Redirects are not followed" in delivery(accounts)["last_error"]
    receiver.answers = [(404, {})]
    deliver(accounts, hooks, Mailer(), BASE, now=delivery(accounts)["next_attempt_at"])
    assert delivery(accounts)["state"] == "rejected"


def test_a_delivery_is_abandoned_after_24_hours(world):
    endpoint_with_monitor(world)
    store, accounts, hooks, receiver, owner, organisation = world
    change(store, "Too late.")
    worker.match(accounts)
    deliver(accounts, hooks, Mailer(), BASE, now=datetime.now(UTC) + timedelta(hours=25))
    assert delivery(accounts)["state"] == "abandoned"


def test_410_disables_and_repeated_failure_pauses_with_an_email(world):
    created, _ = endpoint_with_monitor(world)
    store, accounts, hooks, receiver, owner, organisation = world
    change(store, "Gone.")
    worker.match(accounts)
    mailer = Mailer()
    receiver.answers = [(410, {})]
    deliver(accounts, hooks, mailer, BASE)
    assert hooks.get(owner, organisation, created["endpoint"]["id"])["state"] == "disabled"
    assert "disabled" in mailer.sent[0].subject
    hooks.verify(owner, organisation, created["endpoint"]["id"])  # the owner fixes it and verifies
    with accounts.engine.begin() as conn:
        conn.execute(text(f"UPDATE {accounts._t(conn, 'webhook_endpoint')} SET consecutive_failures = 19"))
    change(store, "Failing.")
    worker.match(accounts)
    receiver.answers = [(503, {})]
    deliver(accounts, hooks, mailer, BASE)
    assert hooks.get(owner, organisation, created["endpoint"]["id"])["state"] == "paused"
    assert "paused" in mailer.sent[-1].subject


def test_a_record_withdrawn_before_delivery_is_withheld(world):
    endpoint_with_monitor(world)
    store, accounts, hooks, receiver, owner, organisation = world
    change(store, "Then withdrawn.")
    worker.match(accounts)
    corpus = store.load()
    next(r for r in corpus.rights if r.source_id == "ng-foi-act-2011").metadata_public = False
    store.save(corpus)
    counts = deliver(accounts, hooks, Mailer(), BASE)
    assert counts["withheld"] >= 1 and all(
        p["event"]["work"] is None or p["event"]["work"]["id"] != "ng-foi-act-2011"
        for p, _ in receiver.deliveries()
    )


def test_replay_and_rotation(world):
    created, _ = endpoint_with_monitor(world)
    store, accounts, hooks, receiver, owner, organisation = world
    change(store, "Replay me.")
    worker.match(accounts)
    deliver(accounts, hooks, Mailer(), BASE)
    original = delivery(accounts)
    rotated = hooks.rotate(owner, organisation, created["endpoint"]["id"])
    assert len(rotated["endpoint"]["signing_keys"]) == 2 and rotated["secret"] != created["secret"]
    replay = hooks.replay(owner, organisation, created["endpoint"]["id"], original["id"])
    deliver(accounts, hooks, Mailer(), BASE)
    payload, headers = receiver.deliveries()[-1]
    assert payload["delivery_id"] == replay["id"] != original["id"]
    assert payload["event"]["event_id"] == str(original["event_id"])
    body = [b for b, h in receiver.requests if h is headers][0]
    for secret in (created["secret"], rotated["secret"]):  # both sign during the overlap
        assert verify_signature(
            secret, body, headers["Open-Alvary-Timestamp"], headers["Open-Alvary-Signature"]
        )
    for _ in range(9):
        hooks.replay(owner, organisation, created["endpoint"]["id"], original["id"])
    with pytest.raises(AccountError) as limited:
        hooks.replay(owner, organisation, created["endpoint"]["id"], original["id"])
    assert limited.value.code == "rate_limited"
