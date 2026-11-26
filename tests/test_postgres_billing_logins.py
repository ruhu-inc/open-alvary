"""What the logins may do about billing and seats (stripe-billing §4.2, §8 item 3), signed in as each: the jobs
login ends paid access Stripe has ended, writes its history and counts seats; the catalogue's, retrieval's and
the publish job's reach none of it. The daily reconciliation, run as the jobs login against a stand-in Stripe."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from stripe_stand_in import StandInStripe, billing_with, send
from test_postgres import pg_engine as pg_engine
from test_postgres_logins import CATALOGUE, JOBS, PUBLISH, RETRIEVAL, SERVICE, person, signed  # noqa: F401

from accounts.store import TIERS, Accounts
from changes import worker
from rights import logins

pytestmark = pytest.mark.postgres
TEAM = ["gh", "ke", "ng", "rw", "za"]


def attempt(engine, statement, **params):
    try:
        with engine.connect() as conn:
            conn.execute(text(statement), params)
            conn.rollback()
        return True
    except DBAPIError:
        return False


def test_the_jobs_login_ends_access_and_counts_seats_and_the_others_reach_none_of_it(signed):  # noqa: F811
    Accounts(signed.owner, signed.namespace).set_tiers(json.loads(TIERS.read_text()))
    owner_store = Accounts(signed.owner, signed.namespace)
    _, organisation = person(owner_store)
    granted = owner_store.grant_access(
        "op",
        organisation,
        tier="team",
        scope={"jurisdictions": TEAM},
        source="manual",
        ends_at=None,
        reason="T",
    )
    with signed.owner.connect() as conn:
        names = {
            k: signed.t(conn, "accounts", k)
            for k in (
                "entitlement",
                "entitlement_event",
                "organisation",
                "billing_customer",
                "billing_subscription",
            )
        }
        names["limit"] = signed.t(conn, "policy", "organisation_seat_limit")
        names["webhook"] = signed.t(conn, "accounts", "stripe_webhook_event")
    end = f"UPDATE {names['entitlement']} SET state = 'ended', ends_at = statement_timestamp() WHERE id = :e"
    history = (
        f"INSERT INTO {names['entitlement_event']} (entitlement_id, actor_type, actor_ref, to_state)"
        " VALUES (:e, 'system', 'stripe:reconcile', 'ended')"
    )
    seats = f"SELECT {names['limit']}(:o)"
    grace = f"UPDATE {names['organisation']} SET seat_grace_until = NULL WHERE id = :o"
    for statement in (end, history, seats, grace):
        assert attempt(signed.engine(JOBS), statement, e=granted, o=organisation), statement
    # The jobs login changes no organisation's name, and grants nothing.
    assert not attempt(
        signed.engine(JOBS), f"UPDATE {names['organisation']} SET name = 'x' WHERE id = :o", o=organisation
    )
    assert not attempt(
        signed.engine(JOBS),
        f"INSERT INTO {names['entitlement']} (id, organisation_id, tier_id, source, scope, starts_at, state)"
        " VALUES ('ent_00000000000000000000000000', :o, 'team', 'payment', '{{\"all\": true}}', now(), 'active')",
        o=organisation,
    )
    with signed.engine(SERVICE).connect() as conn:
        assert conn.scalar(text(seats), {"o": organisation}) == 5
    for role in (CATALOGUE, RETRIEVAL, PUBLISH):
        for statement in (
            end,
            history,
            seats,
            grace,
            f"SELECT 1 FROM {names['billing_customer']}",
            f"SELECT 1 FROM {names['billing_subscription']}",
            f"SELECT 1 FROM {names['webhook']}",
        ):
            assert not attempt(signed.engine(role), statement, e=granted, o=organisation), (role, statement)
    for role in logins.LOGINS:
        assert logins.check_login(signed.engine(role), logins.LOGINS[role]) == [], role


def test_the_daily_reconciliation_runs_on_the_jobs_login(signed):  # noqa: F811
    owner_store = Accounts(signed.owner, signed.namespace)
    owner_store.set_tiers(json.loads(TIERS.read_text()))
    service = Accounts(signed.engine(SERVICE), signed.namespace)
    stripe = StandInStripe()
    alerts: list = []
    console = billing_with(service, stripe, alerts)

    def subscribed(name):
        owner, organisation = person(service, name)
        url = console.start_checkout(
            owner,
            organisation,
            {
                "tier": "team",
                "interval": "monthly",
                "jurisdictions": TEAM,
                "terms_agreed": True,
                "dialog_nonce": "n" * 20,
            },
            site="https://open.example.test",
        )["url"]
        session, sub = stripe.complete(url.rsplit("/", 1)[1])
        assert send(console, "checkout.session.completed", session)[1]["outcome"] == "granted"
        return owner, organisation, sub

    # A cancellation whose webhook never came.
    _, cancelled, cancelled_sub = subscribed("amina")
    stripe.subscriptions[cancelled_sub["id"]]["status"] = "canceled"
    # A deleted organisation whose subscription is still live in Stripe (the cancel after its deletion failed).
    _, deleted, deleted_sub = subscribed("bola")
    with signed.owner.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {signed.t(conn, 'accounts', 'organisation')} SET state = 'deleted', deleted_at = now()"
                " WHERE id = :o"
            ),
            {"o": deleted},
        )
        # Its deletion ended its access, as _delete_organisation does.
        conn.execute(
            text(
                f"UPDATE {signed.t(conn, 'accounts', 'entitlement')} SET state = 'ended', ends_at = now()"
                " WHERE organisation_id = :o"
            ),
            {"o": deleted},
        )
    # A subscription Stripe made whose local write failed: only Stripe knows it.
    _, found, found_sub = subscribed("chidi")
    with signed.owner.begin() as conn:
        conn.execute(
            text(
                f"DELETE FROM {signed.t(conn, 'accounts', 'billing_subscription')} WHERE stripe_subscription_id = :s"
            ),
            {"s": found_sub["id"]},
        )

    jobs = billing_with(worker.accounts_for(signed.engine(JOBS)), stripe, alerts)
    done = jobs.reconcile()
    assert done["ended"] == 1 and done["recorded"] == 1 and done["cancelled"] == 1, done
    assert service.access(cancelled)["starter"]
    assert stripe.subscriptions[deleted_sub["id"]]["status"] == "canceled"
    assert any(what == "reconcile_org_deleted" for what, _ in alerts)
    with signed.owner.connect() as conn:
        recorded = conn.scalar(
            text(
                f"SELECT status FROM {signed.t(conn, 'accounts', 'billing_subscription')}"
                " WHERE stripe_subscription_id = :s"
            ),
            {"s": found_sub["id"]},
        )
        ended_by = conn.scalar(
            text(
                f"SELECT e.actor_ref FROM {signed.t(conn, 'accounts', 'entitlement_event')} e"
                f" JOIN {signed.t(conn, 'accounts', 'entitlement')} n ON n.id = e.entitlement_id"
                " WHERE n.organisation_id = :o AND e.to_state = 'ended'"
            ),
            {"o": cancelled},
        )
    assert recorded == "active" and ended_by == "stripe:reconcile"
    assert service.access(found)["tier"]["id"] == "team"  # recording it changed nothing else
    # Run again: nothing more to do.
    assert jobs.reconcile()["ended"] == 0


def test_a_timed_grant_that_lapses_starts_the_seat_grace_on_the_daily_run(signed):  # noqa: F811
    owner_store = Accounts(signed.owner, signed.namespace)
    owner_store.set_tiers(json.loads(TIERS.read_text()))
    service = Accounts(signed.engine(SERVICE), signed.namespace)
    owner, organisation = person(service)
    entitlement = owner_store.grant_access(
        "op",
        organisation,
        tier="team",
        scope={"jurisdictions": TEAM},
        source="manual",
        ends_at=None,
        reason="T",
    )
    invitation = service.invite(owner, organisation, "bola@example.org", "developer")
    bola, _ = person(service, "bola")
    service.accept_invitation(bola, invitation)
    with signed.owner.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {signed.t(conn, 'accounts', 'entitlement')} SET ends_at = statement_timestamp()"
                " - interval '1 hour', starts_at = statement_timestamp() - interval '2 days' WHERE id = :e"
            ),
            {"e": entitlement},
        )
    jobs = billing_with(worker.accounts_for(signed.engine(JOBS)), StandInStripe())
    jobs.client = None  # Stripe not set up: the seat grace still runs
    assert jobs.reconcile()["graces"] == 1
    with signed.owner.connect() as conn:
        until = conn.scalar(
            text(
                f"SELECT seat_grace_until - statement_timestamp() FROM {signed.t(conn, 'accounts', 'organisation')}"
                " WHERE id = :o"
            ),
            {"o": organisation},
        )
    assert 6.9 < until.total_seconds() / 86400 <= 7
    # The same login tells the owners, once: it reads their addresses and writes the record of the notice.
    sent: list = []

    class Outbox:
        def send(self, message):
            sent.append(message)
            return True

    assert jobs.seat_notices(Outbox(), "https://open.example.test") == 1
    assert [m.to for m in sent] == ["amina@example.org"] and "2 members" in sent[0].text
    assert jobs.seat_notices(Outbox(), "https://open.example.test") == 0 and len(sent) == 1
