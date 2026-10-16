"""MAC-11: operator access actions run under the workbench's own database role, which reads no
secrets, and every one is audited with its reason and evidence (accounts.access_admin)."""

import json
from hashlib import sha256

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool
from test_postgres import migrate
from test_postgres import pg_engine as pg_engine
from test_postgres_console import RecordingMailer

from accounts import keys, policies
from accounts.access_admin import AccessAdmin, Action, provision_role
from accounts.store import TIERS, AccountError, Accounts
from rights.database import PolicyNamespace

pytestmark = pytest.mark.postgres
PEPPER = b"p" * 32
ISSUER = "https://login.example.test/"


@pytest.fixture
def world(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        namespace = PolicyNamespace(conn.scalar(text("SELECT current_schema()")))
    owner_store = Accounts(pg_engine, namespace, key_pepper=PEPPER)
    owner_store.set_tiers(json.loads(TIERS.read_text()))
    role = "oa_" + sha256(namespace.base.encode()).hexdigest()[:12] + "_access"
    provision_role(pg_engine, role)
    restricted = create_engine(
        pg_engine.url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={namespace.base} -crole={role}"},
    )
    mailer = RecordingMailer()
    admin = AccessAdmin(Accounts(restricted, namespace, key_pepper=PEPPER), mailer, "https://site.test")
    try:
        yield owner_store, admin, restricted, mailer
    finally:
        restricted.dispose()
        with pg_engine.begin() as conn:
            conn.execute(text(f'DROP OWNED BY "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))


def person(accounts, name, organisation=None):
    signed = accounts.sign_in(
        issuer=ISSUER,
        subject=f"google-oauth2|{name}",
        method="google",
        email=f"{name}@example.org",
        label=name,
    )
    accounts.accept_policies(signed["id"], dict(policies.CURRENT))
    own = accounts.welcome(signed["id"], name.title(), organisation or f"{name.title()} Org")
    return signed["id"], own


def check(accounts, key):
    return accounts.check_credential(
        {"kind": "key", "key_id": keys.parse(key), "secret_hash": keys.secret_hash(key, PEPPER).hex()}
    )


def act(reason="Checked", evidence=None):
    return Action("operator@example.org", reason, evidence)


def test_the_workbench_role_reads_no_secrets(world):
    owner_store, _, restricted, _ = world
    amina, own = person(owner_store, "amina")
    owner_store.create_key(amina, own, kind="personal", name="laptop")
    owner_store.create_session(amina, second_factor=False)
    with restricted.connect() as conn:
        ns = owner_store.namespace
        for column, table in (
            ("secret_hash", "api_key"),
            ("token_hash", "console_session"),
            ("csrf", "console_session"),
            ("subject", "identity"),
        ):
            with pytest.raises(ProgrammingError, match="permission denied"), conn.begin():
                conn.execute(text(f"SELECT {column} FROM {ns.qualified(conn, 'accounts', table)}"))
        with pytest.raises(ProgrammingError, match="permission denied"), conn.begin():
            conn.execute(text(f"DELETE FROM {ns.qualified(conn, 'accounts', 'audit_event')}"))


def test_an_operator_answers_a_request_grants_and_revokes_and_each_step_is_audited(world):
    owner_store, admin, _, mailer = world
    amina, own = person(owner_store, "amina")
    request = owner_store.request_access(
        amina,
        own,
        {
            "purpose": "Research",
            "expected_volume": "1,000 a day",
            "scope": {"jurisdictions": ["ng"]},
            "tier": "developer",
            "contact_name": "Amina",
            "contact_email": "amina@example.org",
        },
    )
    assert [r["id"] for r in admin.requests()] == [request]
    admin.decide(act("Which project?"), own, request, "needs_information")
    assert "Which project?" in mailer.sent[-1].text
    entitlement = admin.grant(act("Research use confirmed", "ticket 42"), own, request_id=request)
    assert entitlement.startswith("ent_") and admin.requests() == []
    view = admin.organisation(own)
    assert view["access"]["tier"]["id"] == "developer" and view["entitlements"][0]["scope"] == {
        "jurisdictions": ["ng"]
    }
    assert view["entitlements"][0]["reference"] == "ticket 42" and "secret_hash" not in json.dumps(
        view, default=str
    )
    admin.revoke(act("Terms breached", "report 7"), own)
    assert owner_store.access(own)["state"] != "active"
    log = admin.audit(organisation_id=own)
    assert [e["action"] for e in log[:3]] == [
        "entitlement.revoked",
        "entitlement.granted",
        "access.needs_information",
    ]
    assert all(e["actor"] == "operator@example.org" for e in log[:3])
    with pytest.raises(AccountError, match="reason"):
        Action("operator@example.org", " ")


def test_suspending_refuses_keys_and_ends_sessions_and_restoring_brings_keys_back(world):
    owner_store, admin, _, _ = world
    amina, own = person(owner_store, "amina")
    key = owner_store.create_key(amina, own, kind="personal", name="laptop")["secret"]
    token, _ = owner_store.create_session(amina, second_factor=False)
    assert check(owner_store, key)["ok"]
    assert admin.set_organisation(act("Abuse report", "case 3"), own, "suspend") == "suspended"
    assert check(owner_store, key)["code"] == "organisation_suspended"
    assert admin.set_organisation(act("Resolved"), own, "restore") == "active"
    assert check(owner_store, key)["ok"]
    admin.set_person(act("Account takeover reported", "case 4"), amina, "suspend")
    assert not check(owner_store, key)["ok"] and owner_store.resume(token) is None
    with pytest.raises(AccountError) as again:
        admin.set_person(act("Twice"), amina, "suspend")
    assert again.value.code == "conflict"
    admin.set_person(act("Owner confirmed by phone"), amina, "restore")
    assert check(owner_store, key)["ok"]
    actions = [e["action"] for e in admin.audit(action="account.")]
    assert actions[:2] == ["account.restored", "account.suspended"]
    assert admin.audit(action="organisation.suspended")[0]["summary"] == {
        "reason": "Abuse report",
        "evidence": "case 3",
        "state": "suspended",
    }


def test_a_leaked_key_is_revoked_by_the_key_itself_without_storing_it(world):
    owner_store, admin, _, _ = world
    amina, own = person(owner_store, "amina")
    key = owner_store.create_key(amina, own, kind="personal", name="laptop")["secret"]
    revoked = admin.revoke_credential(act("Found in a public repository", "https://example.org/leak"), key)
    assert revoked == {"kind": "key", "id": keys.parse(key), "organisation_id": own}
    assert not check(owner_store, key)["ok"]
    entry = admin.audit(action="key.revoked")[0]
    assert entry["target_id"] == keys.parse(key) and key not in json.dumps(entry, default=str)
    with pytest.raises(AccountError) as twice:
        admin.revoke_credential(act("Again"), keys.parse(key))
    assert twice.value.code == "not_found"
    with pytest.raises(AccountError):
        admin.revoke_credential(act("Unknown"), "not-a-credential")


def test_an_owner_is_restored_only_where_no_active_person_owns_it_and_with_evidence(world):
    owner_store, admin, _, _ = world
    amina, own = person(owner_store, "amina")
    bola, _ = person(owner_store, "bola")
    invitation = owner_store.invite(amina, own, "bola@example.org", "developer")
    owner_store.accept_invitation(bola, invitation)
    bolas = next(m["id"] for m in admin.organisation(own)["members"] if m["person_id"] == bola)
    with pytest.raises(AccountError) as owned:
        admin.restore_owner(act("Asked", "letter"), own, bolas)
    assert owned.value.code == "conflict"
    admin.set_person(act("Left the company, cannot sign in", "email from HR"), amina, "suspend")
    with pytest.raises(AccountError, match="evidence"):
        admin.restore_owner(act("No evidence"), own, bolas)
    admin.restore_owner(act("Director's letter checked", "letter 2026-10-16"), own, bolas)
    roles = {m["person_id"]: m["role"] for m in admin.organisation(own)["members"]}
    assert roles[bola] == "owner"
    assert admin.audit(action="ownership.restored")[0]["summary"]["previous_role"] == "developer"


def test_operators_find_organisations_by_name_email_or_id(world):
    owner_store, admin, _, _ = world
    amina, own = person(owner_store, "amina", "Kigali Law Clinic")
    assert [o["id"] for o in admin.search("amina@example.org")] == [own]
    assert [o["id"] for o in admin.search("law clinic")] == [own]
    assert [o["id"] for o in admin.search(own)] == [own]
    assert admin.search("%") == [] and admin.search("zz-nothing") == []
