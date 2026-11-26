"""Stripe billing against PostgreSQL (docs: stripe-billing §8, items 1 to 3), with Stripe replaced by a stand-in
HTTP transport (tests/stripe_stand_in.py): no network. Every call the stand-in answers also checks that no
transaction is open on the database at that moment (stripe-billing §1.3)."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from stripe_stand_in import StandInStripe, billing_with, send, signed
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_accounts import PEPPER, check_key, country_numbers, person
from test_postgres_console import FakeProvider, app_provider, onboard, sign_in

from accounts import policies, privacy
from accounts.store import TIERS, AccountError, Accounts
from api.console import create_console_app
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
TEAM = ["gh", "ke", "ng", "rw", "za"]
SITE = "https://open.example.test"


@pytest.fixture
def world(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        database = conn.scalar(text("SELECT current_database()"))
    accounts = Accounts(pg_engine, namespace, key_pepper=PEPPER)
    accounts.set_tiers(json.loads(TIERS.read_text()))
    stripe = StandInStripe()
    alerts: list = []
    open_during_calls: list = []
    answer = stripe.handler

    def outside_transactions(request):
        # Asked on a connection of its own: is any session of this database inside a transaction?
        with pg_engine.connect() as probe:
            busy = probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid()"
                    " AND state LIKE 'idle in transaction%'"
                ),
                {"d": database},
            )
        if busy:
            open_during_calls.append(f"{request.method} {request.url.path}")
        return answer(request)

    stripe.handler = outside_transactions
    billing = billing_with(accounts, stripe, alerts)
    yield accounts, billing, stripe, alerts
    assert open_during_calls == [], "Stripe was called while a transaction was open"


def sql(accounts, statement, **params):
    with accounts.engine.begin() as conn:
        names = {
            name: accounts._t(conn, name)
            for name in (
                "organisation",
                "entitlement",
                "entitlement_event",
                "audit_event",
                "billing_customer",
                "billing_subscription",
                "stripe_webhook_event",
                "policy_acceptance",
                "membership",
            )
        }
        result = conn.execute(text(statement.format(**names)), params)
        return result.mappings().all() if result.returns_rows else None


def start(billing, who, organisation, *, tier="team", countries=TEAM, nonce="n" * 20, **extra):
    data = {
        "tier": tier,
        "interval": "monthly",
        "jurisdictions": list(countries),
        "terms_agreed": True,
        "dialog_nonce": nonce,
        **extra,
    }
    return billing.start_checkout(who, organisation, data, site=SITE)


def paid_checkout(billing, stripe, who, organisation, **kwargs):
    """Checkout started in the console and paid at Stripe; no webhook sent yet."""
    url = start(billing, who, organisation, **kwargs)["url"]
    session_id = url.rsplit("/", 1)[1]
    return stripe.complete(session_id)


def invoice_paid(stripe, sub, **changes):
    invoice = stripe.invoice(sub, status="paid")
    invoice.update(changes)
    return invoice


def live(accounts, organisation):
    return sql(
        accounts,
        "SELECT id, tier_id, source, scope, reference, ends_at, state FROM {entitlement}"
        " WHERE organisation_id = :o AND state IN ('scheduled','active') ORDER BY created_at",
        o=organisation,
    )


def actions(accounts, organisation, action):
    return sql(
        accounts,
        "SELECT actor_type, actor_ref, summary FROM {audit_event} WHERE organisation_id = :o AND action = :a"
        " ORDER BY seq",
        o=organisation,
        a=action,
    )


@pytest.mark.parametrize("order", ["checkout_first", "invoice_first"])
def test_checkout_and_invoice_paid_in_either_order_grant_once_as_stripe(world, order):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    session, sub = paid_checkout(billing, stripe, owner, organisation)
    invoice = invoice_paid(stripe, sub)
    events = [("checkout.session.completed", session), ("invoice.paid", invoice)]
    if order == "invoice_first":
        events.reverse()
    first = send(billing, *events[0])
    second = send(billing, *events[1])
    assert first == (200, {"received": True, "outcome": "granted"})
    assert second == (200, {"received": True, "outcome": "already_granted"})
    # The subscription was not known here, so the first event fetched it from Stripe.
    assert ("GET", f"/subscriptions/{sub['id']}", {}) in stripe.calls
    granted = live(accounts, organisation)
    assert len(granted) == 1
    assert (granted[0]["tier_id"], granted[0]["source"], granted[0]["reference"]) == (
        "team",
        "payment",
        sub["id"],
    )
    assert granted[0]["scope"] == {"jurisdictions": TEAM} and granted[0]["ends_at"] is None
    history = sql(
        accounts,
        "SELECT actor_type, actor_ref FROM {entitlement_event} WHERE entitlement_id = :e",
        e=granted[0]["id"],
    )
    assert [(h["actor_type"], h["actor_ref"][:11]) for h in history] == [("system", "stripe:evt_")]
    assert accounts.access(organisation)["tier"]["id"] == "team"
    shown = accounts.audit_log(owner, organisation)
    assert {e["actor"] for e in shown if e["action"] == "entitlement.granted"} == {"Stripe billing"}


def test_a_repeated_event_is_a_duplicate_and_a_failed_fetch_leaves_nothing_behind(world):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    _, sub = paid_checkout(billing, stripe, owner, organisation)
    body, signature = signed("invoice.paid", invoice_paid(stripe, sub), event_id="evt_retried1")
    stripe.down = True
    assert billing.handle_webhook(body, signature) == (500, {"retry": True})
    assert sql(accounts, "SELECT * FROM {stripe_webhook_event}") == []
    stripe.down = False
    assert billing.handle_webhook(body, signature)[1]["outcome"] == "granted"
    assert billing.handle_webhook(body, signature) == (200, {"duplicate": True})
    assert len(live(accounts, organisation)) == 1
    assert sql(accounts, "SELECT outcome FROM {stripe_webhook_event}")[0]["outcome"] == "granted"


def test_a_signature_that_does_not_match_is_refused(world):
    _, billing, _, _ = world
    body, signature = signed("invoice.paid", {"id": "in_1"})
    with pytest.raises(AccountError) as refused:
        billing.handle_webhook(body + b" ", signature)
    assert refused.value.code == "invalid_signature" and refused.value.status == 400


def annual_invoice(billing, who, organisation, nonce="i" * 20, **extra):
    data = {
        "legal_name": "Amina Legal Ltd",
        "billing_email": "accounts@amina.example.org",
        "billing_address": {"line1": "1 KN 5 Rd", "city": "Kigali", "country": "rw"},
        "jurisdictions": TEAM,
        "terms_agreed": True,
        "dialog_nonce": nonce,
        **extra,
    }
    return billing.request_invoice(who, organisation, data)


def test_an_annual_invoice_paid_after_its_due_date_activates_team_and_one_paid_after_cancelling_does_not(
    world,
):
    accounts, billing, stripe, alerts = world
    owner, organisation = person(accounts)
    sent = annual_invoice(billing, owner, organisation, team_name="Amina Chambers")
    assert sent["invoice_url"].startswith("https://invoice.stripe.test/") and sent["status"] == "open"
    assert accounts.access(organisation)["starter"]  # Starter until it is paid
    status = billing.status(owner, organisation)
    assert status["subscription"]["invoice_paid"] is False and status["billing_email"] == (
        "accounts@amina.example.org"
    )
    sub = next(iter(stripe.subscriptions.values()))
    assert sub["collection_method"] == "send_invoice"
    # Day 31: the subscription is past_due when the transfer arrives.
    sub["status"] = "past_due"
    send(billing, "customer.subscription.updated", sub)
    invoice = stripe.invoices[sub["latest_invoice"]]
    invoice["status"] = "paid"
    assert send(billing, "invoice.paid", invoice)[1]["outcome"] == "granted"
    assert accounts.access(organisation)["tier"]["id"] == "team"
    renamed = sql(accounts, "SELECT name, personal_of FROM {organisation} WHERE id = :o", o=organisation)[0]
    assert renamed == {"name": "Amina Chambers", "personal_of": None}

    # Another organisation's subscription, cancelled before its invoice is paid: no access, an alert.
    other, theirs = person(accounts, "bola")
    annual_invoice(billing, other, theirs, nonce="j" * 20)
    late = next(s for s in stripe.subscriptions.values() if s["id"] != sub["id"])
    late["status"] = "canceled"
    send(billing, "customer.subscription.deleted", late)
    paid = stripe.invoices[late["latest_invoice"]]
    paid["status"] = "paid"
    assert send(billing, "invoice.paid", paid)[1]["outcome"] == "subscription_cancelled"
    assert accounts.access(theirs)["starter"] and alerts[-1][0] == "paid_after_cancel"


def test_an_older_subscription_event_after_a_newer_one_changes_nothing(world):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    session, sub = paid_checkout(billing, stripe, owner, organisation)
    send(billing, "checkout.session.completed", session)
    newer = dict(sub, cancel_at_period_end=True)
    assert (
        send(billing, "customer.subscription.updated", newer, created=2_000_000_000)[1]["outcome"] == "synced"
    )
    older = dict(sub, status="unpaid")
    assert (
        send(billing, "customer.subscription.updated", older, created=1_900_000_000)[1]["outcome"] == "stale"
    )
    row = sql(accounts, "SELECT status, cancel_at_period_end FROM {billing_subscription}")[0]
    assert row == {"status": "active", "cancel_at_period_end": True}
    assert accounts.access(organisation)["tier"]["id"] == "team"


def test_an_operators_word_comes_first(world):
    accounts, billing, stripe, alerts = world
    # Revoked: paid access is held back.
    owner, organisation = person(accounts)
    session, sub = paid_checkout(billing, stripe, owner, organisation)
    accounts.revoke_access("op", organisation, "Abuse")
    assert send(billing, "checkout.session.completed", session)[1]["outcome"] == "revoked_hold"
    assert accounts.access(organisation)["state"] == "revoked" and alerts[-1][0] == "revoked_hold"

    # An operator's grant over a live subscription is kept; the subscription is still recorded.
    other, theirs = person(accounts, "bola")
    session, sub = paid_checkout(billing, stripe, other, theirs)
    accounts.grant_access(
        "op", theirs, tier="pilot", scope={"all": True}, source="manual", ends_at=None, reason="Pilot"
    )
    assert send(billing, "invoice.paid", invoice_paid(stripe, sub))[1]["outcome"] == "higher_grant_kept"
    assert accounts.access(theirs)["tier"]["id"] == "pilot"
    assert (
        sql(
            accounts, "SELECT count(*) AS n FROM {billing_subscription} WHERE organisation_id = :o", o=theirs
        )[0]["n"]
        == 1
    )

    # While such a grant is in place, neither a checkout nor an invoice can start.
    for attempt in (lambda: start(billing, other, theirs), lambda: annual_invoice(billing, other, theirs)):
        with pytest.raises(AccountError) as refused:
            attempt()
        assert (refused.value.code, refused.value.status) == ("has_grant", 409)

    # Paid access is ended in Stripe, never with Go back to Starter.
    third, ours = person(accounts, "chidi")
    session, _ = paid_checkout(billing, stripe, third, ours)
    send(billing, "checkout.session.completed", session)
    with pytest.raises(AccountError) as managed:
        accounts.end_access(third, ours)
    assert (managed.value.code, managed.value.status) == ("managed_by_billing", 409)


def test_the_same_checkout_request_twice_gives_the_same_session_and_each_purchase_records_its_price(world):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    first = start(billing, owner, organisation, nonce="a" * 20)
    again = start(billing, owner, organisation, nonce="a" * 20)  # a double click
    assert first == again
    assert len(stripe.posted("/checkout/sessions")) == 1  # one session made, nothing expired
    # Once that session has closed, the same request is told so, and the console opens a new dialog.
    stripe.sessions[first["url"].rsplit("/", 1)[1]]["status"] = "expired"
    with pytest.raises(AccountError) as closed:
        start(billing, owner, organisation, nonce="a" * 20)
    assert (closed.value.code, closed.value.status) == ("checkout_closed", 409)
    # A new dialog (a new nonce), for another plan, under the same terms: a second agreement.
    second = start(billing, owner, organisation, nonce="b" * 20, tier="developer", countries=["ng"])
    assert second != first and [s["status"] for s in stripe.sessions.values()] == ["expired", "open"]
    terms = policies.CURRENT["terms"]
    assert [a["summary"] for a in actions(accounts, organisation, "billing.price_agreed")] == [
        {"tier_id": "team", "interval": "month", "amount": 39900, "currency": "usd", "terms_version": terms},
        {
            "tier_id": "developer",
            "interval": "month",
            "amount": 4900,
            "currency": "usd",
            "terms_version": terms,
        },
    ]
    accepted = sql(
        accounts,
        "SELECT count(*) AS n FROM {policy_acceptance} WHERE person_id = :p AND document = 'terms'",
        p=owner,
    )
    assert accepted[0]["n"] == 1


def test_a_subscription_in_stripe_without_its_webhook_yet_refuses_a_second_checkout(world):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    paid_checkout(billing, stripe, owner, organisation)  # paid; no webhook has arrived
    with pytest.raises(AccountError) as refused:
        start(billing, owner, organisation, nonce="c" * 20)
    assert refused.value.code == "already_subscribed"


def test_a_suspended_organisations_payment_waits_for_the_suspension_to_lift(world):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    secret = accounts.create_key(owner, organisation, kind="personal", name="laptop")["secret"]
    session, sub = paid_checkout(billing, stripe, owner, organisation)
    sql(accounts, "UPDATE {organisation} SET state = 'suspended' WHERE id = :o", o=organisation)
    assert send(billing, "invoice.paid", invoice_paid(stripe, sub))[1]["outcome"] == "granted"
    assert live(accounts, organisation)[0]["tier_id"] == "team"
    assert check_key(accounts, secret)["code"] == "organisation_suspended"
    sql(accounts, "UPDATE {organisation} SET state = 'active' WHERE id = :o", o=organisation)
    assert check_key(accounts, secret)["tier"]["id"] == "team"


def test_cancelling_an_unpaid_invoice_voids_it_then_cancels_the_subscription(world):
    accounts, billing, stripe, alerts = world
    owner, organisation = person(accounts)
    annual_invoice(billing, owner, organisation)
    sub = next(iter(stripe.subscriptions.values()))
    invoice_id = sub["latest_invoice"]
    stripe.calls.clear()
    billing.cancel_invoice(owner, organisation)
    changes = [(m, p) for m, p, _ in stripe.calls if m in ("POST", "DELETE")]
    assert changes == [("POST", f"/invoices/{invoice_id}/void"), ("DELETE", f"/subscriptions/{sub['id']}")]
    assert sql(accounts, "SELECT status FROM {billing_subscription}")[0]["status"] == "canceled"
    assert billing.status(owner, organisation)["subscription"] is None
    # The way to a card checkout is open again.
    assert start(billing, owner, organisation, nonce="d" * 20)["url"].startswith(
        "https://checkout.stripe.test/"
    )
    # A payment that arrives anyway grants nothing and tells the operator.
    stripe.invoices[invoice_id]["status"] = "paid"
    assert (
        send(billing, "invoice.paid", stripe.invoices[invoice_id])[1]["outcome"] == "subscription_cancelled"
    )
    assert accounts.access(organisation)["starter"] and alerts[-1][0] == "paid_after_cancel"


def subscribed(accounts, billing, stripe, name):
    owner, organisation = person(accounts, name)
    session, sub = paid_checkout(billing, stripe, owner, organisation)
    send(billing, "checkout.session.completed", session)
    sql(
        accounts,
        "UPDATE {billing_customer} SET billing_email = 'pay@example.org' WHERE organisation_id = :o",
        o=organisation,
    )
    return owner, organisation, sub


def test_deleting_an_organisation_cancels_its_subscription_first_and_clears_its_billing_email(world):
    accounts, billing, stripe, alerts = world
    owner, organisation, sub = subscribed(accounts, billing, stripe, "amina")
    stripe.down = True
    with pytest.raises(AccountError) as unreachable:
        accounts.delete_organisation(owner, organisation, billing=billing)
    assert (unreachable.value.code, unreachable.value.status) == ("billing_unavailable", 503)
    assert (
        sql(accounts, "SELECT state FROM {organisation} WHERE id = :o", o=organisation)[0]["state"]
        == "active"
    )
    # Nor can it be deleted without asking Stripe while a subscription is live.
    with pytest.raises(AccountError) as live_subscription:
        accounts.delete_organisation(owner, organisation)
    assert live_subscription.value.code == "billing_unavailable"
    stripe.down = False
    accounts.delete_organisation(owner, organisation, billing=billing)
    assert stripe.subscriptions[sub["id"]]["status"] == "canceled"
    row = sql(
        accounts,
        "SELECT billing_email, open_checkout_session_id, open_checkout_key FROM {billing_customer}"
        " WHERE organisation_id = :o",
        o=organisation,
    )[0]
    assert row == {"billing_email": None, "open_checkout_session_id": None, "open_checkout_key": None}

    # Closing an account deletes its own organisation the same way.
    other, theirs, theirs_sub = subscribed(accounts, billing, stripe, "bola")
    privacy.close_account(accounts, other, None, billing)
    assert stripe.subscriptions[theirs_sub["id"]]["status"] == "canceled"
    assert sql(accounts, "SELECT billing_email FROM {billing_customer} WHERE organisation_id = :o", o=theirs)[
        0
    ] == {"billing_email": None}

    # A payment that arrives after the deletion: recorded, then the subscription is cancelled and the operator told.
    stripe.subscriptions[sub["id"]]["status"] = "active"  # say the cancel had not reached Stripe
    sql(
        accounts,
        "UPDATE {billing_subscription} SET status = 'active' WHERE stripe_subscription_id = :s",
        s=sub["id"],
    )
    stripe.calls.clear()
    outcome = send(billing, "invoice.paid", invoice_paid(stripe, stripe.subscriptions[sub["id"]]))
    assert outcome[1]["outcome"] == "org_deleted_cancelled"
    assert ("DELETE", f"/subscriptions/{sub['id']}", {}) in stripe.calls
    assert live(accounts, organisation) == [] and alerts[-1][0] == "org_deleted_cancelled"


def members(accounts, owner, organisation, *names, role="developer"):
    joined = []
    for name in names:
        invitation = accounts.invite(owner, organisation, f"{name}@example.org", role)
        who, _ = person(accounts, name)
        accounts.accept_invitation(who, invitation)
        joined.append(who)
    return joined


def test_a_plan_without_a_number_of_countries_covers_them_all_and_asks_for_none(world):
    accounts, billing, stripe, _ = world
    country_numbers(accounts, developer=None, team=None)
    amina, first = person(accounts, "amina")
    session, _ = paid_checkout(billing, stripe, amina, first, tier="developer", countries=())
    assert send(billing, "checkout.session.completed", session)[1]["outcome"] == "granted"
    assert [(e["tier_id"], e["scope"]) for e in live(accounts, first)] == [("developer", {"all": True})]
    with pytest.raises(AccountError) as refused:
        accounts.change_countries(amina, first, ["ng"])
    assert refused.value.code == "invalid_input"  # nothing to choose

    bola, second = person(accounts, "bola")
    annual_invoice(billing, bola, second, jurisdictions=None)
    sub = next(s for s in stripe.subscriptions.values() if s["collection_method"] == "send_invoice")
    assert not sub["metadata"].get("jurisdictions")
    invoice = stripe.invoices[sub["latest_invoice"]]
    invoice["status"] = "paid"
    assert send(billing, "invoice.paid", invoice)[1]["outcome"] == "granted"
    assert [(e["tier_id"], e["scope"]) for e in live(accounts, second)] == [("team", {"all": True})]


def test_team_to_pro_trims_countries_frees_one_change_starts_a_grace_and_then_refuses_extra_members(world):
    accounts, billing, stripe, _ = world
    owner, organisation, sub = subscribed(accounts, billing, stripe, "amina")
    bola, chidi = members(accounts, owner, organisation, "bola", "chidi")
    dayo = members(accounts, owner, organisation, "dayo", role="owner")[0]
    keys = {
        who: accounts.create_key(who, organisation, kind="personal", name="k")["secret"]
        for who in (owner, bola, chidi, dayo)
    }
    service = accounts.create_key(owner, organisation, kind="service", name="server")["secret"]
    accounts.change_countries(owner, organisation, ["gh", "ke", "ng", "rw", "tz"])  # a change this month

    # The portal moves the subscription to Pro.
    pro = dict(sub)
    pro["items"] = {"data": [{"price": stripe.prices["oa_pro_monthly"], "current_period_end": 2_000_000_000}]}
    assert send(billing, "customer.subscription.updated", pro)[1]["outcome"] == "plan_changed"
    now = live(accounts, organisation)
    assert [(e["tier_id"], e["scope"]) for e in now] == [("developer", {"jurisdictions": ["gh"]})]
    assert [a["summary"] for a in actions(accounts, organisation, "billing.plan_changed")] == [
        {"from": "team", "to": "developer"}
    ]
    with pytest.raises(AccountError) as two:
        accounts.change_countries(owner, organisation, ["ng", "rw"])  # Pro is one country
    assert two.value.code == "invalid_input"
    accounts.change_countries(owner, organisation, ["ng"])  # free after the switch
    with pytest.raises(AccountError) as again:
        accounts.change_countries(owner, organisation, ["za"])
    assert again.value.code == "change_too_soon"

    # Four members on one seat: seven days' grace, then only the first owner's own key and service keys work.
    grace = sql(
        accounts,
        "SELECT seat_grace_until - statement_timestamp() AS left FROM {organisation} WHERE id = :o",
        o=organisation,
    )
    assert 6.9 < grace[0]["left"].total_seconds() / 86400 <= 7
    assert all(check_key(accounts, secret)["ok"] for secret in keys.values())
    sql(
        accounts,
        "UPDATE {organisation} SET seat_grace_until = statement_timestamp() - interval '1 minute' WHERE id = :o",
        o=organisation,
    )
    answers = {who: check_key(accounts, secret).get("code") for who, secret in keys.items()}
    assert answers == {
        owner: None,
        bola: "seat_limit_exceeded",
        chidi: "seat_limit_exceeded",
        dayo: "seat_limit_exceeded",
    }
    assert check_key(accounts, service)["ok"]

    # Members leave: within the seats again, the grace is gone at once.
    listed = {m["id"]: m for m in accounts.members(owner, organisation)["members"]}
    for membership in [m for m in listed.values() if not m["you"]]:
        accounts.remove_member(owner, organisation, membership["id"])
    assert sql(accounts, "SELECT seat_grace_until FROM {organisation} WHERE id = :o", o=organisation)[0] == {
        "seat_grace_until": None
    }


def test_pro_to_team_keeps_the_country_and_lets_four_be_added_at_once(world):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    with pytest.raises(AccountError) as two:
        start(billing, owner, organisation, tier="developer", countries=["ng", "rw"])
    assert two.value.code == "invalid_input"  # Pro is one country
    session, sub = paid_checkout(billing, stripe, owner, organisation, tier="developer", countries=["ng"])
    send(billing, "checkout.session.completed", session)
    accounts.change_countries(owner, organisation, ["ke"])
    team = dict(stripe.subscriptions[sub["id"]])
    team["items"] = {
        "data": [{"price": stripe.prices["oa_team_monthly"], "current_period_end": 2_000_000_000}]
    }
    send(billing, "customer.subscription.updated", team)
    assert live(accounts, organisation)[0]["scope"] == {"jurisdictions": ["ke"]}
    accounts.change_countries(owner, organisation, ["gh", "ke", "ng", "rw", "za"])
    assert live(accounts, organisation)[0]["scope"] == {"jurisdictions": TEAM}


def test_cancelling_at_the_end_of_the_period_ends_only_this_subscriptions_access(world):
    accounts, billing, stripe, _ = world
    owner, organisation, sub = subscribed(accounts, billing, stripe, "amina")
    members(accounts, owner, organisation, "bola")
    gone = dict(stripe.subscriptions[sub["id"]], status="canceled")
    assert send(billing, "customer.subscription.deleted", gone)[1]["outcome"] == "ended"
    assert accounts.access(organisation)["starter"]
    grace = sql(accounts, "SELECT seat_grace_until FROM {organisation} WHERE id = :o", o=organisation)[0]
    assert grace["seat_grace_until"] is not None  # two members on Starter's one seat
    # A deleted event for a subscription that grants nothing ends nothing (here: an operator's grant).
    other, theirs = person(accounts, "chidi")
    session, other_sub = paid_checkout(billing, stripe, other, theirs)
    accounts.grant_access(
        "op", theirs, tier="pilot", scope={"all": True}, source="manual", ends_at=None, reason="Pilot"
    )
    assert (
        send(billing, "customer.subscription.deleted", dict(other_sub, status="canceled"))[1]["outcome"]
        == "cancel_noted"
    )
    assert accounts.access(theirs)["tier"]["id"] == "pilot"


def test_a_price_not_ours_is_never_guessed(world):
    accounts, billing, stripe, alerts = world
    owner, organisation = person(accounts)
    _, sub = paid_checkout(billing, stripe, owner, organisation)
    sub["items"]["data"][0]["price"] = {"id": "price_other", "lookup_key": "someone_else", "unit_amount": 1}
    assert send(billing, "invoice.paid", invoice_paid(stripe, sub))[1]["outcome"] == "unmapped_price"
    assert accounts.access(organisation)["starter"] and alerts[-1][0] == "unmapped_price"


def test_a_second_paid_checkout_is_cancelled_after_the_commit(world):
    accounts, billing, stripe, alerts = world
    owner, organisation = person(accounts)
    # Two tabs: the first session is paid in the moment before the second dialog expires it.
    first_url = start(billing, owner, organisation, nonce="e" * 20)["url"]
    second_url = start(billing, owner, organisation, nonce="g" * 20)["url"]
    first_session, first = stripe.complete(first_url.rsplit("/", 1)[1])
    second_session, second = stripe.complete(second_url.rsplit("/", 1)[1])
    assert send(billing, "checkout.session.completed", first_session)[1]["outcome"] == "granted"
    assert send(billing, "checkout.session.completed", second_session)[1]["outcome"] == "duplicate_cancelled"
    assert stripe.subscriptions[second["id"]]["status"] == "canceled"
    assert stripe.subscriptions[first["id"]]["status"] == "active"
    assert (
        live(accounts, organisation)[0]["reference"] == first["id"]
        and alerts[-1][0] == "duplicate_subscription"
    )


def test_billing_details_are_shown_to_managers_only(world):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    accounts.grant_access(
        "op",
        organisation,
        tier="team",
        scope={"jurisdictions": TEAM},
        source="manual",
        ends_at=None,
        reason="Test",
    )
    viewer = members(accounts, owner, organisation, "bola", role="viewer")[0]
    admin = members(accounts, owner, organisation, "chidi", role="admin")[0]
    sql(
        accounts,
        "INSERT INTO {billing_customer} (organisation_id, stripe_customer_id, billing_email) VALUES (:o, 'cus_x1', 'pay@example.org')",
        o=organisation,
    )
    assert billing.status(owner, organisation)["billing_email"] == "pay@example.org"
    assert billing.status(admin, organisation)["billing_email"] == "pay@example.org"
    assert billing.status(viewer, organisation)["billing_email"] is None
    with pytest.raises(AccountError) as owners_only:
        start(billing, admin, organisation)
    assert owners_only.value.code == "role_forbidden"


def test_an_organisations_export_has_its_billing_and_old_events_are_purged(world):
    accounts, billing, stripe, _ = world
    owner, organisation, _ = subscribed(accounts, billing, stripe, "amina")
    exported = privacy.export_organisation(accounts, owner, organisation)["billing"]
    assert exported["billing_email"] == "pay@example.org"
    assert [(s["tier_id"], s["status"]) for s in exported["subscriptions"]] == [("team", "active")]
    sql(
        accounts, "UPDATE {stripe_webhook_event} SET received_at = statement_timestamp() - interval '91 days'"
    )
    kept = sql(accounts, "SELECT count(*) AS n FROM {stripe_webhook_event}")[0]["n"]
    assert privacy.purge(accounts)["stripe_events"] == kept


# ---------- through the console ----------
@pytest.fixture
def console(world):
    accounts, billing, stripe, alerts = world
    fake = FakeProvider()
    app = create_console_app(accounts, app_provider(fake), billing=billing)
    parent = FastAPI()
    parent.mount("/console", app)
    browser = TestClient(parent, base_url=SITE + "/console", follow_redirects=False)
    return accounts, billing, stripe, fake, browser


def test_the_console_routes_ask_for_a_second_factor_and_the_webhook_needs_only_stripes_signature(console):
    accounts, billing, stripe, fake, browser = console
    organisation, headers, _ = onboard(browser, fake)
    root = f"/api/v1/organisations/{organisation}/billing"
    status = browser.get(root).json()
    assert status["enabled"] is True and status["has_customer"] is False and status["subscription"] is None
    asked = {
        "tier": "team",
        "interval": "annual",
        "jurisdictions": TEAM,
        "terms_agreed": True,
        "dialog_nonce": "f" * 20,
    }
    refused = browser.post(f"{root}/checkout", json=asked, headers=headers)
    assert refused.json()["error"]["code"] == "second_factor_required"  # even on Starter, before paying
    sign_in(browser, fake, purpose="second_factor", amr=["mfa"], code="m")
    prices = browser.get(f"{root}/prices").json()["prices"]  # what the dialog shows and the owner agrees to
    assert prices["team"]["year"] == {"amount": 399000, "currency": "usd"}
    assert prices["developer"]["month"] == {"amount": 4900, "currency": "usd"}
    made = browser.post(f"{root}/checkout", json=asked, headers=headers)
    assert made.status_code == 200 and made.json()["url"].startswith("https://checkout.stripe.test/")
    session, _ = stripe.complete(made.json()["url"].rsplit("/", 1)[1])
    body, signature = signed("checkout.session.completed", session)
    assert (
        browser.post(
            "/billing/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=0"}
        ).status_code
        == 400
    )
    answer = browser.post("/billing/webhooks/stripe", content=body, headers={"Stripe-Signature": signature})
    assert answer.status_code == 200 and answer.json()["outcome"] == "granted"
    assert browser.post(
        "/billing/webhooks/stripe", content=body, headers={"Stripe-Signature": signature}
    ).json() == {"duplicate": True}
    assert browser.get(root).json()["source"] == "payment"
    assert (
        browser.post(f"{root}/portal", headers=headers).json()["url"] == "https://billing.stripe.test/portal"
    )
    ended = browser.post(f"/api/v1/organisations/{organisation}/access/end", headers=headers)
    assert ended.json()["error"]["code"] == "managed_by_billing"


def moved_price(sub):
    """The plan's lookup key moved to a new price: this subscription's own price is no longer one of ours."""
    sub["items"]["data"][0]["price"] = {"id": "price_team_2025", "lookup_key": None, "unit_amount": 39900}


def test_a_cancelled_subscription_whose_price_is_no_longer_ours_still_ends_its_access(world):
    accounts, billing, stripe, alerts = world
    owner, organisation, sub = subscribed(accounts, billing, stripe, "amina")
    moved_price(sub)
    # Still running, it cannot be read: nothing changes, and the operator hears.
    assert send(billing, "customer.subscription.updated", sub)[1]["outcome"] == "unmapped_price"
    assert accounts.access(organisation)["tier"]["id"] == "team"
    # Cancelled, it ends as recorded here, with no plan guessed; the operator still hears why.
    gone = dict(sub, status="canceled")
    assert send(billing, "customer.subscription.deleted", gone)[1]["outcome"] == "ended"
    assert accounts.access(organisation)["starter"] and alerts[-1][0] == "unmapped_price"
    assert sql(accounts, "SELECT status, tier_id FROM {billing_subscription}") == [
        {"status": "canceled", "tier_id": "team"}
    ]
    # One never recorded here is still never guessed.
    other, theirs = person(accounts, "bola")
    _, unseen = paid_checkout(billing, stripe, other, theirs)
    moved_price(unseen)
    gone = dict(unseen, status="canceled")
    assert send(billing, "customer.subscription.deleted", gone)[1]["outcome"] == "unmapped_price"


def test_an_unpaid_subscription_that_cannot_be_read_ends_too_and_the_reconciliation_does_the_same(world):
    accounts, billing, stripe, alerts = world
    owner, organisation, sub = subscribed(accounts, billing, stripe, "amina")
    moved_price(sub)
    assert send(billing, "customer.subscription.updated", dict(sub, status="unpaid"))[1]["outcome"] == "ended"
    assert accounts.access(organisation)["starter"]
    # The cancellation's webhook never came: the daily run ends it.
    other, theirs, missed = subscribed(accounts, billing, stripe, "bola")
    moved_price(missed)
    missed["status"] = "canceled"
    done = billing.reconcile()
    assert done["ended"] == 1 and accounts.access(theirs)["starter"]
    assert ("reconcile_unmapped_price", {"subscription": missed["id"]}) in alerts
    ended_by = sql(
        accounts,
        "SELECT e.actor_ref FROM {entitlement_event} e JOIN {entitlement} n ON n.id = e.entitlement_id"
        " WHERE n.organisation_id = :o AND e.to_state = 'ended'",
        o=theirs,
    )
    assert [e["actor_ref"] for e in ended_by] == ["stripe:reconcile"]


def test_the_reconciliation_tells_the_operator_what_only_a_webhook_can_put_right(world):
    accounts, billing, stripe, alerts = world
    # A plan changed in the portal whose webhook never came.
    _, changed_org, changed = subscribed(accounts, billing, stripe, "amina")
    changed["items"]["data"][0]["price"] = stripe.prices["oa_pro_monthly"]
    # A card subscription paid at Stripe, none of whose webhooks came.
    bola, unseen_org = person(accounts, "bola")
    _, unseen = paid_checkout(billing, stripe, bola, unseen_org)
    # An annual invoice paid at Stripe whose invoice.paid never came, and one still waiting to be paid.
    chidi, paid_org = person(accounts, "chidi")
    annual_invoice(billing, chidi, paid_org)
    paid = next(s for s in stripe.subscriptions.values() if s["collection_method"] == "send_invoice")
    stripe.invoices[paid["latest_invoice"]]["status"] = "paid"
    dayo, waiting_org = person(accounts, "dayo")
    annual_invoice(billing, dayo, waiting_org, nonce="w" * 20)
    waiting = [s for s in stripe.subscriptions.values() if s["collection_method"] == "send_invoice"][-1]
    alerts.clear()
    done = billing.reconcile()
    said = {(what, details.get("subscription")) for what, details in alerts}
    assert said == {
        ("reconcile_plan_differs", changed["id"]),
        ("reconcile_recorded", unseen["id"]),
        ("reconcile_paid_without_access", unseen["id"]),
        ("reconcile_paid_without_access", paid["id"]),
    }, said
    assert waiting["id"] not in {subscription for _, subscription in said}
    assert done["alerts"] == 4 and done["recorded"] == 1 and done["ended"] == 0
    # It notices, and no more: no plan is changed and nothing is granted (its login could not).
    assert accounts.access(changed_org)["tier"]["id"] == "team"
    assert all(accounts.access(o)["starter"] for o in (unseen_org, paid_org, waiting_org))
    # Until a webhook puts it right, it says so again the next day; what it recorded, it does not repeat.
    alerts.clear()
    billing.reconcile()
    assert {(what, details.get("subscription")) for what, details in alerts} == {
        ("reconcile_plan_differs", changed["id"]),
        ("reconcile_paid_without_access", unseen["id"]),
        ("reconcile_paid_without_access", paid["id"]),
    }


def test_an_invoice_request_that_stopped_part_way_is_finished_by_its_retry(world):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    # Stripe makes the subscription and finalises its invoice; sending it cannot be reached.
    stripe.unreachable = lambda method, path: path.endswith("/send")
    with pytest.raises(AccountError) as stopped:
        annual_invoice(billing, owner, organisation)
    assert stopped.value.code == "billing_unavailable"
    assert sql(accounts, "SELECT * FROM {billing_subscription}") == []
    assert actions(accounts, organisation, "billing.price_agreed") == []
    # The same request again carries on: sent, recorded and agreed to, once.
    stripe.unreachable = None
    sent = annual_invoice(billing, owner, organisation)
    assert sent["status"] == "open" and sent["invoice_url"].startswith("https://invoice.stripe.test/")
    assert len(stripe.subscriptions) == 1 and len(stripe.posted("/subscriptions")) == 1  # none made twice
    assert [path for method, path, _ in stripe.calls if path.endswith("/send")] != []
    row = sql(accounts, "SELECT status, invoice_paid, latest_invoice_url FROM {billing_subscription}")
    assert row == [{"status": "active", "invoice_paid": False, "latest_invoice_url": sent["invoice_url"]}]
    assert billing.status(owner, organisation)["subscription"]["invoice_url"] == sent["invoice_url"]
    assert len(actions(accounts, organisation, "billing.price_agreed")) == 1
    assert len(actions(accounts, organisation, "billing.invoice_sent")) == 1
    # And a third time: the invoice it sent, with nothing written or sent again.
    sends = len([1 for _, path, _ in stripe.calls if path.endswith("/send")])
    assert annual_invoice(billing, owner, organisation) == sent
    assert len([1 for _, path, _ in stripe.calls if path.endswith("/send")]) == sends
    assert len(actions(accounts, organisation, "billing.price_agreed")) == 1
    # The owner can withdraw it, as any unpaid invoice.
    billing.cancel_invoice(owner, organisation)
    assert billing.status(owner, organisation)["subscription"] is None


def test_what_stripe_refuses_is_said_as_a_refusal_and_never_as_an_outage(world):
    accounts, billing, stripe, _ = world
    owner, organisation = person(accounts)
    stripe.refuse = lambda method, path: (
        (400, {"error": {"code": "customer_tax_location_invalid", "message": "Stripe's own words"}})
        if (method, path) == ("POST", "/subscriptions")
        else None
    )
    with pytest.raises(AccountError) as refused:
        annual_invoice(billing, owner, organisation)
    assert (refused.value.code, refused.value.status) == ("billing_refused", 400)
    assert "billing address" in str(refused.value) and "Stripe's own words" not in str(refused.value)
    # A refusal Stripe gives no known code for is still a refusal.
    stripe.refuse = lambda method, path: (
        (400, {"error": {"code": "parameter_invalid_string", "message": "x"}})
        if (method, path) == ("POST", "/subscriptions")
        else None
    )
    with pytest.raises(AccountError) as unknown:
        annual_invoice(billing, owner, organisation, nonce="u" * 20)
    assert unknown.value.code == "billing_refused" and "didn't accept" in str(unknown.value)
    # What is ours to fix (the key, a price not set up) stays an outage to the person: try again later.
    stripe.refuse = lambda method, path: (401, {"error": {"code": "api_key_expired", "message": "x"}})
    with pytest.raises(AccountError) as ours:
        annual_invoice(billing, owner, organisation, nonce="k" * 20)
    assert (ours.value.code, ours.value.status) == ("billing_unavailable", 503)
    stripe.refuse = None
    stripe.prices.pop("oa_team_annual")
    with pytest.raises(AccountError) as unpriced:
        annual_invoice(billing, owner, organisation, nonce="p" * 20)
    assert (unpriced.value.code, unpriced.value.status) == ("billing_unavailable", 503)


class Outbox:
    def __init__(self, working=True):
        self.sent, self.working = [], working

    def send(self, message):
        if self.working:
            self.sent.append(message)
        return self.working


def test_owners_are_told_once_when_a_seat_grace_starts(world):
    accounts, billing, stripe, _ = world
    owner, organisation, sub = subscribed(accounts, billing, stripe, "amina")
    members(accounts, owner, organisation, "bola")
    assert billing.seat_notices(Outbox(), SITE) == 0  # within its seats: nobody is told anything
    assert (
        send(billing, "customer.subscription.deleted", dict(sub, status="canceled"))[1]["outcome"] == "ended"
    )
    # Without a mail key nothing is delivered, so nothing is recorded and it is tried again.
    assert billing.seat_notices(Outbox(working=False), SITE) == 0
    assert actions(accounts, organisation, "seats.notice_sent") == []
    outbox = Outbox()
    assert billing.seat_notices(outbox, SITE) == 1
    (message,) = outbox.sent
    assert message.to == "amina@example.org" and "2 members" in message.text and "1 seat." in message.text
    assert f"{SITE}/console/access" in message.text
    (noted,) = actions(accounts, organisation, "seats.notice_sent")
    assert (noted["actor_type"], noted["actor_ref"]) == ("system", "seats")
    assert noted["summary"]["members"] == 2 and noted["summary"]["seats"] == 1
    # The next day, and every day after: nothing more.
    assert billing.seat_notices(outbox, SITE) == 0 and len(outbox.sent) == 1
    # Within its seats and then over them again is a new grace. One that has already passed is not announced
    # as if it were still to come; one still running is told again.
    sql(accounts, "UPDATE {organisation} SET seat_grace_until = NULL WHERE id = :o", o=organisation)
    with accounts.engine.begin() as conn:
        accounts._sync_seat_grace(conn, organisation)
    sql(
        accounts,
        "UPDATE {organisation} SET seat_grace_until = statement_timestamp() - interval '1 minute' WHERE id = :o",
        o=organisation,
    )
    assert billing.seat_notices(outbox, SITE) == 0
    sql(
        accounts,
        "UPDATE {organisation} SET seat_grace_until = statement_timestamp() + interval '3 days' WHERE id = :o",
        o=organisation,
    )
    assert billing.seat_notices(outbox, SITE) == 1 and len(outbox.sent) == 2
