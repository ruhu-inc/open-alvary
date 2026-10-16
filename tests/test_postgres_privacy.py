"""MAC-14: exporting your data, deleting your account, and the retention schedule (accounts.privacy)."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine

from accounts import policies, privacy
from accounts.store import TIERS, AccountError, Accounts
from changes.watches import Watches
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
ISSUER = "https://login.example.test/"


@pytest.fixture
def accounts(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        store = Accounts(
            pg_engine, PolicyNamespace(conn.scalar(text("SELECT current_schema()"))), key_pepper=b"k" * 32
        )
    store.set_tiers(json.loads(TIERS.read_text()))
    return store


def person(accounts, name):
    signed = accounts.sign_in(
        issuer=ISSUER,
        subject=f"google-oauth2|{name}",
        method="google",
        email=f"{name}@example.org",
        label=name,
    )
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    return signed["id"], accounts.welcome(signed["id"], name.title(), f"{name.title()} Org")


def join(accounts, owner, organisation, who, role="developer"):
    invitation = accounts.invite(owner, organisation, f"{who[1]}@example.org", role)
    accounts.accept_invitation(who[0], invitation)


def test_a_persons_export_has_their_data_and_nobody_elses(accounts):
    amina, own = person(accounts, "amina")
    bola, bolas = person(accounts, "bola")
    join(accounts, bola, bolas, (amina, "amina"))
    accounts.create_key(amina, own, kind="personal", name="laptop")
    Watches(accounts).create(amina, own, {"name": "Nigeria", "filters": {"type": ["catalogue_added"]}})
    Watches(accounts).create(bola, bolas, {"name": "Bola's private", "filters": {"type": ["withdrawal"]}})
    data = privacy.export_person(accounts, amina)
    flat = json.dumps(data, default=str)
    assert data["person"]["email"] == "amina@example.org" and data["schema"] == "account-export-1"
    assert {m["organisation"] for m in data["memberships"]} == {"Amina Org", "Bola Org"}
    assert [k["name"] for k in data["personal_keys"]] == ["laptop"] and "secret" not in flat
    assert [m["name"] for m in data["monitors"]] == ["Nigeria"] and "Bola's private" not in flat
    assert "google-oauth2|amina" not in flat  # provider subjects are never returned
    assert any(e["action"] == "key.created" for e in data["audit_entries"])


def test_an_organisation_export_is_for_owners_and_leaves_out_private_monitors(accounts):
    amina, own = person(accounts, "amina")
    chidi, _ = person(accounts, "chidi")
    join(accounts, amina, own, (chidi, "chidi"))
    Watches(accounts).create(chidi, own, {"name": "Chidi's private", "filters": {"type": ["withdrawal"]}})
    with pytest.raises(AccountError) as refused:
        privacy.export_organisation(accounts, chidi, own)
    assert refused.value.code == "role_forbidden"
    data = privacy.export_organisation(accounts, amina, own)
    assert {m["display_name"] for m in data["members"]["members"]} >= {"Amina", "Chidi"}
    assert "Chidi's private" not in json.dumps(data) and data["shared_monitors"] == []


def test_deleting_an_account_waits_for_shared_organisations_then_takes_everything_down(accounts):
    amina, own = person(accounts, "amina")
    bola, _ = person(accounts, "bola")
    join(accounts, amina, own, (bola, "bola"))
    plan = privacy.deletion_check(accounts, amina)
    assert [o["name"] for o in plan["blocking"]] == ["Amina Org"]
    with pytest.raises(AccountError) as blocked:
        privacy.close_account(accounts, amina)
    assert blocked.value.code == "transfer_required" and "Amina Org" in blocked.value.message
    members = accounts.members(amina, own)["members"]
    bolas_membership = next(m["id"] for m in members if m["display_name"] == "Bola")
    accounts.transfer_ownership(amina, own, bolas_membership, keep_owner=False)
    _, solo = person(accounts, "amina2")  # another account's org, unaffected
    key = accounts.create_key(amina, own, kind="personal", name="k")["secret"]
    Watches(accounts).create(amina, own, {"name": "Mine", "filters": {"type": ["withdrawal"]}})
    token, _ = accounts.create_session(amina, second_factor=False)
    closed = privacy.close_account(accounts, amina)
    assert closed["left"][0]["name"] == "Amina Org" and closed["deleted_organisations"] == []
    from accounts import keys

    check = accounts.check_credential(
        {"kind": "key", "key_id": keys.parse(key), "secret_hash": keys.secret_hash(key, b"k" * 32).hex()}
    )
    assert not check["ok"]
    assert Watches(accounts).listing(bola, own)["watches"] == []
    with accounts.engine.connect() as conn:
        assert (
            conn.scalar(
                text(f"SELECT count(*) FROM {accounts._t(conn, 'actor')} WHERE person_id = :p"), {"p": amina}
            )
            == 0
        )
        assert (
            conn.scalar(text(f"SELECT state FROM {accounts._t(conn, 'person')} WHERE id = :p"), {"p": amina})
            == "closed"
        )
    with pytest.raises(AccountError) as again:
        accounts.sign_in(
            issuer=ISSUER,
            subject="google-oauth2|amina",
            method="google",
            email="amina@example.org",
            label="a",
        )
    assert again.value.code == "account_inactive"
    assert accounts.resume(token) is None  # signed out everywhere
    assert solo


def test_a_sole_members_organisations_go_with_the_account(accounts):
    amina, own = person(accounts, "amina")
    closed = privacy.close_account(accounts, amina)
    assert [o["name"] for o in closed["deleted_organisations"]] == ["Amina Org"]
    with accounts.engine.connect() as conn:
        assert (
            conn.scalar(
                text(f"SELECT state FROM {accounts._t(conn, 'organisation')} WHERE id = :o"), {"o": own}
            )
            == "deleted"
        )


class Deleter:
    issuer = ISSUER

    def __init__(self, ok=True):
        self.deleted, self.ok = [], ok

    def delete(self, subject):
        self.deleted.append(subject)
        return self.ok


def test_closed_accounts_are_purged_after_30_days_with_their_provider_user(accounts):
    amina, _ = person(accounts, "amina")
    privacy.close_account(accounts, amina)
    assert privacy.purge(accounts, Deleter())["accounts"] == 0  # not yet 30 days
    later = datetime.now(UTC) + timedelta(days=31)
    assert privacy.purge(accounts, None, now=later)["provider_pending"] == 1  # no deletion client: wait
    failing = Deleter(ok=False)
    assert privacy.purge(accounts, failing, now=later)["provider_pending"] == 1
    deleter = Deleter()
    assert privacy.purge(accounts, deleter, now=later)["accounts"] == 1
    assert deleter.deleted == ["google-oauth2|amina"]
    with accounts.engine.connect() as conn:
        assert (
            conn.scalar(
                text(f"SELECT count(*) FROM {accounts._t(conn, 'person')} WHERE id = :p"), {"p": amina}
            )
            == 0
        )
    # The same Google account can now start afresh.
    assert accounts.sign_in(
        issuer=ISSUER, subject="google-oauth2|amina", method="google", email="amina@example.org", label="a"
    )["created"]


def test_the_console_deletes_only_with_the_email_typed_and_sends_a_confirmation(accounts):
    from test_postgres_console import FakeProvider, RecordingMailer, app_provider, client, onboard

    from api.console import create_console_app

    fake, mailer = FakeProvider(), RecordingMailer()
    browser = client(create_console_app(accounts, app_provider(fake), mailer=mailer))
    _, headers, _ = onboard(browser, fake)
    exported = browser.post("/api/v1/me/export", headers=headers)
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    assert browser.get("/api/v1/me/deletion").json()["deleted"][0]["name"] == "Amina Uwase"
    wrong = browser.post("/api/v1/me/delete", json={"confirm": "someone@else.org"}, headers=headers)
    assert wrong.status_code == 400
    me = browser.get("/api/v1/me").json()["person"]["email"]
    gone = browser.post("/api/v1/me/delete", json={"confirm": me.upper()}, headers=headers)
    assert gone.status_code == 200 and "oa_console=" in gone.headers.get("set-cookie", "")
    assert mailer.sent[-1].subject == "Your Open Alvary account was deleted"
    assert browser.get("/api/v1/me").status_code == 401
