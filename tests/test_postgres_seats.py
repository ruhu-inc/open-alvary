"""Seats per plan against PostgreSQL (docs: teams-and-seats §5, §6): one rule for the limit
(policy.organisation_seat_limit), invitations refused on one seat or a full team, operators' seat counts, the
grace an organisation over its seats gets and loses, the credential check once it has passed, and migration
0059 there and back."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_accounts import ISSUER, PEPPER, check, check_key, person

from accounts import privacy
from accounts.billing import Billing
from accounts.store import TIERS, AccountError, Accounts
from rights.database import PolicyNamespace
from scripts.seat_grace import give_grace

pytestmark = pytest.mark.postgres
TEAM = ["gh", "ke", "ng", "rw", "za"]


@pytest.fixture
def accounts(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    store = Accounts(pg_engine, namespace, key_pepper=PEPPER)
    store.set_tiers(json.loads(TIERS.read_text()))
    return store


def grant(accounts, organisation, tier="team", **kwargs):
    scope = {"jurisdictions": TEAM} if tier == "team" else {"jurisdictions": ["ng"]}
    if tier in ("enterprise", "pilot", "public-interest"):
        scope = {"all": True}
    return accounts.grant_access(
        "op", organisation, tier=tier, scope=scope, source="manual", ends_at=None, reason="Test", **kwargs
    )


def join(accounts, owner, organisation, name, role="developer"):
    invitation = accounts.invite(owner, organisation, f"{name}@example.org", role)
    who, _ = person(accounts, name)
    accounts.accept_invitation(who, invitation)
    return who


def seats(accounts, owner, organisation):
    return accounts.members(owner, organisation)["seats"]


def grace(accounts, organisation):
    with accounts.engine.connect() as conn:
        return conn.scalar(
            text(f"SELECT seat_grace_until FROM {accounts._t(conn, 'organisation')} WHERE id = :o"),
            {"o": organisation},
        )


def expire_grace(accounts, organisation):
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'organisation')} SET seat_grace_until = statement_timestamp()"
                " - interval '1 minute' WHERE id = :o"
            ),
            {"o": organisation},
        )


def test_one_seat_plans_cannot_invite_and_say_so(accounts):
    owner, organisation = person(accounts)
    for plan in ("starter", "developer"):
        if plan != "starter":
            grant(accounts, organisation, plan)
        with pytest.raises(AccountError) as refused:
            accounts.invite(owner, organisation, "bola@example.org", "developer")
        assert (refused.value.code, refused.value.status) == ("plan_single_seat", 403)
    assert seats(accounts, owner, organisation) == {
        "active": 1,
        "pending": 0,
        "used": 1,
        "limit": 1,
        "remaining": 0,
        "over_limit": False,
        "can_invite": False,
        "grace_until": None,
    }


def test_a_team_fills_its_five_seats_and_acceptance_checks_again(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    invitations = [
        accounts.invite(owner, organisation, f"{name}@example.org", "developer")
        for name in ("bola", "chidi", "dayo", "efe")
    ]
    assert seats(accounts, owner, organisation) == {
        "active": 1,
        "pending": 4,
        "used": 5,
        "limit": 5,
        "remaining": 0,
        "over_limit": False,
        "can_invite": False,
        "grace_until": None,
    }
    with pytest.raises(AccountError) as full:
        accounts.invite(owner, organisation, "funmi@example.org", "developer")
    assert (full.value.code, full.value.status) == ("limit_reached", 409)
    # An operator's contract of two seats: the second acceptance finds the team full.
    grant(accounts, organisation, seats=2)
    bola, _ = person(accounts, "bola")
    accounts.accept_invitation(bola, invitations[0])
    chidi, _ = person(accounts, "chidi")
    with pytest.raises(AccountError) as taken:
        accounts.accept_invitation(chidi, invitations[1])
    assert (taken.value.code, taken.value.status) == ("limit_reached", 409)


def test_an_operators_seat_count_overrides_the_plan_and_is_kept_through_a_country_change(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation, "enterprise")
    assert seats(accounts, owner, organisation)["limit"] == 10  # the plan's
    grant(accounts, organisation, "enterprise", seats=12)
    assert seats(accounts, owner, organisation)["limit"] == 12
    for wrong in (0, 100001, True, "12"):
        with pytest.raises(AccountError) as refused:
            grant(accounts, organisation, "enterprise", seats=wrong)
        assert refused.value.code == "invalid_input"
    grant(accounts, organisation, "team", seats=7)
    accounts.change_countries(owner, organisation, ["ng", "rw"])
    assert seats(accounts, owner, organisation)["limit"] == 7


def test_over_its_seats_an_organisation_gets_seven_days_then_only_the_first_owner_and_service_keys_work(
    accounts,
):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    bola = join(accounts, owner, organisation, "bola")
    second_owner = join(accounts, owner, organisation, "chidi", role="owner")
    keys = {
        who: accounts.create_key(who, organisation, kind="personal", name="k")["secret"]
        for who in (owner, bola, second_owner)
    }
    service = accounts.create_key(owner, organisation, kind="service", name="server")["secret"]
    app = accounts.create_grant(
        bola,
        organisation,
        client_id="https://claude.ai/oauth/mcp",
        client_name="Claude",
        client_label="verified_publisher",
        client_host="claude.ai",
        resource="mcp",
    )
    token = {"kind": "grant", "grant_id": app, "resource": "mcp", "issuer": ISSUER, "subject": "email|bola"}
    assert check(accounts, token)["ok"]

    grant(accounts, organisation, "developer")  # down to one seat: three members
    until = grace(accounts, organisation)
    assert timedelta(days=6, hours=23) < until - datetime.now(UTC) <= timedelta(days=7)
    listed = seats(accounts, owner, organisation)
    assert listed["over_limit"] and listed["grace_until"] == until and not listed["can_invite"]
    assert all(check_key(accounts, secret)["ok"] for secret in keys.values())  # within the grace

    # A second change while over keeps the first deadline.
    grant(accounts, organisation, "developer")
    assert grace(accounts, organisation) == until

    expire_grace(accounts, organisation)
    assert check_key(accounts, keys[owner])["ok"]  # the owner who joined first
    assert check_key(accounts, service)["ok"]
    assert check_key(accounts, keys[bola])["code"] == "seat_limit_exceeded"
    assert check_key(accounts, keys[second_owner])["code"] == "seat_limit_exceeded"  # promoted owners too
    assert check(accounts, token)["code"] == "seat_limit_exceeded"

    # Back within its seats: the grace ends at once, and a later downgrade gets a new seven days.
    grant(accounts, organisation)
    assert grace(accounts, organisation) is None and check_key(accounts, keys[bola])["ok"]
    grant(accounts, organisation, "developer")
    assert grace(accounts, organisation) - datetime.now(UTC) > timedelta(days=6, hours=23)


def test_every_path_that_changes_members_or_access_settles_the_grace(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    join(accounts, owner, organisation, "bola")
    chidi = join(accounts, owner, organisation, "chidi", role="admin")
    accounts.end_access(owner, organisation)  # Go back to Starter: three members on one seat
    assert grace(accounts, organisation) is not None
    listed = {m["display_name"]: m for m in accounts.members(owner, organisation)["members"]}
    accounts.remove_member(owner, organisation, listed["Bola"]["id"])
    assert grace(accounts, organisation) is not None  # two on one
    accounts.change_role(owner, organisation, listed["Chidi"]["id"], "viewer")
    assert grace(accounts, organisation) is not None
    accounts.remove_member(chidi, organisation, listed["Chidi"]["id"])  # leaving
    assert grace(accounts, organisation) is None

    # An account closing leaves its teams, and those within their seats again lose the grace.
    grant(accounts, organisation)
    dayo = join(accounts, owner, organisation, "dayo")
    accounts.revoke_access("op", organisation, "Checking")
    assert grace(accounts, organisation) is not None
    privacy.close_account(accounts, dayo)
    assert grace(accounts, organisation) is None


def test_naming_a_personal_workspace_on_team_makes_it_a_team(accounts):
    owner, organisation = person(accounts)

    def personal():
        with accounts.engine.connect() as conn:
            return conn.scalar(
                text(
                    f"SELECT personal_of IS NOT NULL FROM {accounts._t(conn, 'organisation')} WHERE id = :o"
                ),
                {"o": organisation},
            )

    revision = accounts.organisation(owner, organisation)["revision"]
    accounts.update_organisation(owner, organisation, revision, {"name": "Still mine"})
    assert personal()
    grant(accounts, organisation)
    revision = accounts.organisation(owner, organisation)["revision"]
    accounts.update_organisation(owner, organisation, revision, {"name": "Amina Chambers"})
    assert not personal() and accounts.organisation(owner, organisation)["name"] == "Amina Chambers"


class RecordingMailer:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True


def test_the_one_time_rollout_gives_thirty_days_and_emails_the_owners_once(accounts):
    owner, organisation = person(accounts)
    grant(accounts, organisation)
    join(accounts, owner, organisation, "bola")
    # Before the limits were enforced: the plan lapsed with two members (as Starter's one seat now counts).
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'entitlement')} SET state = 'ended', ends_at = statement_timestamp()"
                " WHERE organisation_id = :o"
            ),
            {"o": organisation},
        )
    mailer = RecordingMailer()
    assert give_grace(accounts, mailer, "https://open.example.test", dry_run=True) == {
        "over_limit": 1,
        "graces": 0,
        "emails": 0,
    }
    assert give_grace(accounts, mailer, "https://open.example.test") == {
        "over_limit": 1,
        "graces": 1,
        "emails": 1,
    }
    assert (
        timedelta(days=29, hours=23) < grace(accounts, organisation) - datetime.now(UTC) <= timedelta(days=30)
    )
    (message,) = mailer.sent
    assert message.to == "amina@example.org" and "2 members" in message.text and "1 seat." in message.text
    assert give_grace(accounts, mailer, "https://open.example.test") == {
        "over_limit": 1,
        "graces": 0,
        "emails": 0,
    }
    assert len(mailer.sent) == 1
    # Its notice is recorded as the daily job records its own, so the daily job does not tell them again.
    assert Billing(accounts, None).seat_notices(mailer, "https://open.example.test") == 0
    assert len(mailer.sent) == 1
    shown = accounts.audit_log(owner, organisation)
    assert {e["actor"] for e in shown if e["action"] == "seats.grace_started"} == {"System"}


def test_migration_0059_goes_back_and_forward_cleanly(pg_engine):
    migrate(pg_engine)
    config = Config("alembic.ini")
    with pg_engine.begin() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
        config.attributes["connection"] = conn
        command.downgrade(config, "0058")
        accounts = namespace.schema("accounts")
        policy = namespace.schema("policy")
        assert conn.scalar(text("SELECT to_regclass(:t)"), {"t": f'"{accounts}".billing_customer'}) is None
        assert (
            conn.scalar(
                text("SELECT to_regprocedure(:f)"), {"f": f'"{policy}".organisation_seat_limit(text)'}
            )
            is None
        )
        columns = conn.scalars(
            text(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = :s AND column_name IN ('seats', 'seat_grace_until')"
            ),
            {"s": accounts},
        ).all()
        assert columns == []
        definition = conn.scalar(
            text("SELECT pg_get_functiondef(to_regprocedure(:f))"),
            {"f": f'"{policy}".check_credential(jsonb)'},
        )
        assert "seat_limit_exceeded" not in definition
        command.upgrade(config, "head")
        definition = conn.scalar(
            text("SELECT pg_get_functiondef(to_regprocedure(:f))"),
            {"f": f'"{policy}".check_credential(jsonb)'},
        )
        assert "seat_limit_exceeded" in definition
        # With a billing record, going back is refused rather than losing it.
        organisation = "org_" + "0" * 26
        conn.execute(
            text(f'INSERT INTO "{accounts}".organisation (id, name) VALUES (:o, :n)'),
            {"o": organisation, "n": "X"},
        )
        conn.execute(
            text(
                f'INSERT INTO "{accounts}".billing_customer (organisation_id, stripe_customer_id) VALUES (:o, :c)'
            ),
            {"o": organisation, "c": "cus_test"},
        )
        with pytest.raises(RuntimeError, match="Billing records exist"):
            command.downgrade(config, "0058")
