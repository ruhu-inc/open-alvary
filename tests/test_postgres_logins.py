"""The restricted logins (rights.logins), signed in as themselves against a migrated database: the real
service and jobs work on them, and they cannot do what they should not."""

import base64
import json
import re
import secrets
import string
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from postgres_support import migrate, validated_test_url
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from test_postgres import pg_engine as pg_engine
from test_postgres_console import FakeProvider, app_provider, onboard
from test_v1_text import UNITS, WORK, publish_act, withdraw

from accounts import policies, privacy, seals
from accounts.access_admin import AccessAdmin, Action
from accounts.ids import new_id
from accounts.store import TIERS, Accounts
from api import traffic
from api.catalogue import LegacyCatalogue
from api.console import create_console_app
from api.repositories import LegacyRepository
from api.service import create_service
from api.store import Store, seed
from api.text_index import Filters
from changes import admin, worker
from changes.watches import Watches
from changes.webhooks import Sealer, Sender, Webhooks
from retrieval import indexer
from retrieval import worker as retrieval_worker
from retrieval.pack import Request, Retriever
from retrieval.vectors import HashingEmbedder, Profile
from rights import logins
from rights.database import PolicyNamespace, provision_roles

pytestmark = pytest.mark.postgres
PEPPER = b"v" * 32
ALPHABET = string.ascii_letters + string.digits
PASSWORDS = {role: "".join(secrets.choice(ALPHABET) for _ in range(40)) for role in logins.LOGINS}
EVERY_LOGIN = list(logins.LOGINS)
SERVICE, JOBS, RETRIEVAL, PUBLISH = EVERY_LOGIN
CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


class Signed:
    """The database, as its owner and as each login."""

    def __init__(self, owner, schema):
        self.owner = owner
        self.schema = schema
        self.namespace = PolicyNamespace(schema)
        self.engines = {}

    def url(self, role):
        return self.owner.url.set(username=role, password=PASSWORDS[role])

    def urls(self):
        return {role: self.url(role).render_as_string(hide_password=False) for role in logins.LOGINS}

    def engine(self, role):
        if role not in self.engines:
            self.engines[role] = create_engine(
                self.url(role),
                poolclass=NullPool,
                hide_parameters=True,
                connect_args={"options": f"-csearch_path={self.schema}"},
            )
        return self.engines[role]

    def t(self, conn, kind, name):
        return self.namespace.qualified(conn, kind, name)

    def close(self):
        for engine in self.engines.values():
            engine.dispose()


def drop_logins(owner):
    with owner.begin() as conn:
        for role in logins.LOGINS:
            if conn.scalar(text("SELECT to_regrole(:r)"), {"r": role}):
                conn.execute(text(f'DROP OWNED BY "{role}"'))
                conn.execute(text(f'DROP ROLE "{role}"'))


@pytest.fixture
def signed(pg_engine):
    migrate(pg_engine)
    with pg_engine.connect() as conn:
        schema = conn.scalar(text("SELECT current_schema()"))
    world = Signed(pg_engine, schema)
    drop_logins(pg_engine)
    assert set(logins.provision(pg_engine, world.urls())) == set(logins.LOGINS)
    yield world
    world.close()
    drop_logins(pg_engine)


def person(accounts, name="amina"):
    done = accounts.sign_in(
        issuer="https://login.example.test/",
        subject=f"google-oauth2|{name}",
        method="google",
        email=f"{name}@example.org",
        label=f"{name}@example.org",
    )
    accounts.accept_policies(done["id"], dict(policies.CURRENT))
    return done["id"], accounts.welcome(done["id"], name.title(), f"{name.title()} Org")


def paid_plan(signed, organisation):
    """Granted by an operator, in the workbench's Access section: not by any of these logins."""
    plans = json.loads(TIERS.read_text())
    plan = next(tier for tier in plans if tier["id"] != "starter" and tier.get("published"))
    Accounts(signed.owner, signed.namespace).grant_access(
        "op",
        organisation,
        tier=plan["id"],
        scope={"jurisdictions": ["ng"]},
        source="manual",
        ends_at=None,
        reason="Test",
    )


def receiver(url, address, body, headers):
    payload = json.loads(body)
    if payload["type"] == "endpoint.verification":
        return 200, {}, json.dumps({"challenge": payload["challenge"]}).encode()
    return 200, {}, b"ok"


class Mailer:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True


def admission_config(folder: Path, name: str) -> Path:
    config = json.loads(Path("deployment/site-admission.example.json").read_text())
    config |= {"trusted_proxies": 0, "hmac_secret": "ab" * 32, "policy_revision": name}
    path = folder / f"{name}.json"
    path.write_text(json.dumps(config))
    return path


# --- what each can do ---------------------------------------------------------------------------------


def test_the_public_service_serves_every_door_on_its_own_login(signed, monkeypatch, tmp_path):
    """Migrations and the seed as the owner, then the real service signed in as open_alvary_service, as it
    runs live: admission, byte allowance and traffic counts on, keys and the change feed in use."""
    monkeypatch.setenv("ALVARY_KEY_PEPPER", base64.b64encode(PEPPER).decode())
    monkeypatch.setenv("ALVARY_ACCOUNTS", "1")
    monkeypatch.setenv("ALVARY_TRAFFIC_COUNTS", "1")
    monkeypatch.setenv("ALVARY_ADMISSION_CONFIG", str(admission_config(tmp_path, "api")))
    monkeypatch.setenv("ALVARY_SITE_ADMISSION_CONFIG", str(admission_config(tmp_path, "site")))
    monkeypatch.setenv("ALVARY_POLICY_SCHEMA", signed.schema)
    monkeypatch.setenv("PUBLIC_DOMAIN", "open.example.test")
    monkeypatch.delenv("ALVARY_CONSOLE_CLIENT_ID", raising=False)
    seed(Store(signed.owner))
    Accounts(signed.owner, signed.namespace).set_tiers(json.loads(TIERS.read_text()))
    publish_act(Store(signed.engine(PUBLISH)), UNITS)

    engine = signed.engine(SERVICE)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT session_user")) == SERVICE
    frontend = tmp_path / "site"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html></html>")
    browser = {"user-agent": CHROME, "sec-fetch-site": "same-origin"}
    accounts = Accounts(engine, signed.namespace, key_pepper=PEPPER)
    with TestClient(
        create_service(store=Store(engine), frontend=frontend), client=("203.0.113.9", 4321)
    ) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert (
            client.get("/", headers={"user-agent": CHROME, "sec-fetch-dest": "document"}).status_code == 200
        )
        for path in (
            "/site/v1/project",
            "/site/v1/sources",
            f"/site/v1/sources/{WORK}",
            f"/site/v1/sources/{WORK}/versions",
            f"/site/v1/sources/{WORK}/relationships",
            f"/site/v1/rights/{WORK}",
            "/site/v1/search?q=enacted",
            "/site/v1/coverage",
            "/site/v1/changes",
            "/site/v1/change-coverage",
            "/site/v1/releases",
            "/sitemap.xml",
        ):
            assert client.get(path, headers=browser).status_code == 200, path

        who, organisation = person(accounts)
        secret = accounts.create_key(who, organisation, kind="personal", name="laptop")["secret"]
        headers = {"Authorization": f"Bearer {secret}"}
        for path in (
            "/api/v1/sources",
            f"/api/v1/sources/{WORK}",
            f"/api/v1/sources/{WORK}/versions",
            "/api/v1/search?q=enacted",
            "/api/v1/retrieve?q=enacted",
            "/api/v1/changes",
            "/api/v1/coverage",
        ):
            assert client.get(path, headers=headers).status_code == 200, path
        assert client.get("/api/v1/sources", headers={"Authorization": "Bearer oa_wrong"}).status_code == 401
        listed = client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert listed.status_code == 200 and "result" in listed.json()
        assert accounts.usage(who, organisation)[0]["requests"] >= 1
        assert accounts.list_keys(who, organisation)[0]["last_used_at"] is not None
    # Counted through the two functions it holds, leaving the service as it does at shutdown.
    with signed.owner.connect() as conn:
        days = signed.t(conn, "accounts", "traffic_day")
        assert conn.scalar(text(f"SELECT count(*) FROM {days}")) >= 1
    # A lease taken and handed back: admission reached the database as this login.
    with signed.owner.connect() as conn:
        buckets = signed.t(conn, "policy", "admission_bucket")
        assert conn.scalar(text(f"SELECT count(*) FROM {buckets}")) >= 1


def test_the_console_works_for_a_person_on_the_services_login(signed):
    """Sign-in, policies, an organisation, a key, a monitor, a webhook, an export, closing an account: through
    the console's own routes and a stand-in identity provider."""
    seed(Store(signed.owner))
    Accounts(signed.owner, signed.namespace).set_tiers(json.loads(TIERS.read_text()))
    accounts = Accounts(signed.engine(SERVICE), signed.namespace, key_pepper=PEPPER)
    fake = FakeProvider()
    parent = FastAPI()
    parent.mount("/console", create_console_app(accounts, app_provider(fake)))
    base = "https://open.example.test/console"
    browser = TestClient(parent, base_url=base, follow_redirects=False)
    organisation, headers, _ = onboard(browser, fake)
    root = f"/api/v1/organisations/{organisation}"
    assert browser.get("/api/v1/me").json()["person"]["email"] == "amina@example.org"
    made = browser.post(f"{root}/keys", json={"name": "laptop"}, headers=headers)
    assert made.status_code in (200, 201), made.text
    assert browser.get(f"{root}/keys").json()["keys"][0]["name"] == "laptop"
    rotated = browser.post(f"{root}/keys/{made.json()['id']}/rotate", json={}, headers=headers)
    assert rotated.status_code in (200, 201), rotated.text
    assert browser.post(f"{root}/keys/{rotated.json()['id']}/revoke", headers=headers).status_code == 204
    assert browser.get(f"{root}/usage").status_code == 200
    assert browser.get(f"{root}/audit").status_code == 200
    assert browser.get(f"{root}/members").status_code == 200
    assert browser.get(f"{root}/access").status_code == 200
    assert browser.get("/api/v1/tiers").status_code == 200
    invited = browser.post(
        f"{root}/invitations", json={"email": "bola@example.org", "role": "developer"}, headers=headers
    )
    assert invited.status_code in (200, 201), invited.text
    assert browser.delete(f"{root}/invitations/{invited.json()['id']}", headers=headers).status_code in (
        200,
        204,
    )
    assert browser.get("/api/v1/me/sessions").status_code == 200

    who = browser.get("/api/v1/me").json()["person"]["id"]
    paid_plan(signed, organisation)
    sender = Sender(resolver=lambda host: ["93.184.216.34"], transport=receiver)
    sealer = Sealer({"k": b"k" * 32}, "k")
    watch = Watches(accounts).create(
        who, organisation, {"name": "Nigeria", "filters": {"jurisdiction": ["ng"]}}
    )
    Watches(accounts).set_preferences(who, {"timezone": "UTC"})
    assert Watches(accounts).listing(who, organisation)["watches"][0]["id"] == watch["id"]
    assert Watches(accounts).inbox(who, organisation, "https://open.example.test")["items"] == []
    hooks = Webhooks(accounts, sender, sealer)
    hook = hooks.create(
        who, organisation, {"url": "https://hooks.example.org/in", "watch_ids": [watch["id"]]}
    )
    assert hook["verification"]["verified"]
    endpoint = hook["endpoint"]["id"]
    assert hooks.listing(who, organisation)["endpoints"][0]["id"] == endpoint
    assert hooks.test(who, organisation, endpoint)["delivered"]
    assert hooks.rotate(who, organisation, endpoint)["secret"].startswith("whsec_")
    assert hooks.deliveries(who, organisation, endpoint)["deliveries"] == []
    assert privacy.export_person(accounts, who)["person"]["email"] == "amina@example.org"
    hooks.delete(who, organisation, endpoint)

    other, _ = person(accounts, "bola")
    assert privacy.close_account(accounts, other, None)["email"] == "bola@example.org"
    assert browser.post("/auth/sign-out", headers=headers).status_code == 204
    assert browser.get("/api/v1/me").status_code == 401


def test_the_worker_daily_and_terms_jobs_run_on_their_own_login(signed, monkeypatch):
    seed(Store(signed.owner))
    Accounts(signed.owner, signed.namespace).set_tiers(json.loads(TIERS.read_text()))
    console = Accounts(signed.engine(SERVICE), signed.namespace, key_pepper=PEPPER)
    who, organisation = person(console)
    console.create_key(who, organisation, kind="personal", name="laptop")
    watch = Watches(console).create(who, organisation, {"name": "All", "filters": {"jurisdiction": ["ng"]}})
    sealer = Sealer({"k": b"k" * 32}, "k")
    sender = Sender(resolver=lambda host: ["93.184.216.34"], transport=receiver)
    paid_plan(signed, organisation)
    made = Webhooks(console, sender, sealer).create(
        who, organisation, {"url": "https://hooks.example.org/in", "watch_ids": [watch["id"]]}
    )
    assert made["verification"]["verified"]

    engine = signed.engine(JOBS)
    accounts = worker.accounts_for(engine)
    assert accounts.namespace.base == signed.schema
    mailer = Mailer()
    hooks = Webhooks(accounts, sender, sealer)
    assert (
        worker.run(accounts, mailer, "https://open.example.org", hooks)["matched"] == 0
    )  # starts it at the end
    publish_act(Store(signed.engine(PUBLISH)), UNITS)
    done = worker.run(accounts, mailer, "https://open.example.org", hooks)
    assert done["matched"] >= 1 and done["webhooks"]["delivered"] >= 1, done

    # The daily job's steps, in its order; a record's rights lapsing would come through the sweep.
    with signed.owner.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {signed.t(conn, 'accounts', 'audit_event')} (id, at, actor_type, actor_ref, action)"
                " VALUES (:id, statement_timestamp() - interval '3 days', 'system', 'test', 'test.old')"
            ),
            {"id": new_id("evt")},
        )

    class Quiet:
        def write(self, _):
            return 0

    assert seals.seal(accounts, out=Quiet())
    monkeypatch.setattr("changes.admin.engine_for", lambda *a, **k: engine)
    assert admin.main(["sweep"]) == 0
    assert admin.main(["purge"]) == 0
    assert admin.main(["status"]) == 0
    assert privacy.purge(accounts, None)["accounts"] == 0
    assert seals.verify(accounts)["ok"]
    today = datetime.now(UTC).date()
    later = traffic.maintain(engine, signed.namespace, today=today + timedelta(days=130))
    assert later["months_dropped"] >= 1
    # Today's month back, for what is still to be counted; this is what the job does every day.
    assert traffic.maintain(engine, signed.namespace)["months_dropped"] == 0

    # An account closed long ago goes with its identity.
    closed, _ = person(console, "bola")
    privacy.close_account(console, closed, None)
    with signed.owner.begin() as conn:
        conn.execute(
            text(f"UPDATE {signed.t(conn, 'accounts', 'person')} SET closed_at = :at WHERE id = :p"),
            {"at": datetime.now(UTC) - timedelta(days=60), "p": closed},
        )

    class Deleter:
        issuer = "https://login.example.test/"

        def delete(self, subject):
            return True

    assert privacy.purge(accounts, Deleter())["accounts"] == 1

    # New terms: the one thing the terms job can be told to do.
    told = len(mailer.sent)
    notice = AccessAdmin(accounts, mailer).terms_notice(
        Action("Operator", "Test"), next(iter(policies.CHANGES)), today + timedelta(days=40)
    )
    assert notice["owners"] >= 1 and len(mailer.sent) == told + notice["owners"]


def test_the_retrieval_jobs_run_on_their_own_login_and_never_reach_accounts(signed):
    publish_act(Store(signed.engine(PUBLISH)), UNITS)
    engine = signed.engine(RETRIEVAL)
    # The index dropped back to unbuilt, so the job has it all to do, as after an analyzer change.
    with signed.owner.begin() as conn:
        state = signed.t(conn, "changes", "text_index_state")
        conn.execute(text(f"UPDATE {state} SET built = false"))
    report = retrieval_worker.run(db=engine)
    assert "error" not in json.dumps(report), report
    assert report["section_index"] == {"built": True}
    assert indexer.status(engine) == []
    assert indexer.purge(engine) == 0
    with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            conn.execute(text(f"SELECT * FROM {signed.t(conn, 'accounts', 'person')}"))


def test_a_generation_prepared_by_the_owner_is_filled_on_the_retrieval_login_and_searched_on_the_services(
    signed, monkeypatch
):
    """A database-held generation's table needs the owner to make it (the engine's needs nobody); the grants step
    brings it to the logins, and the rest of its life is theirs."""
    monkeypatch.delenv("PUBLIC_DOMAIN", raising=False)
    monkeypatch.delenv("OPEN_ALVARY_VECTOR_PROFILE", raising=False)
    profile = Profile.from_dict(
        {
            "id": "test-hashing-64",
            "model": "hashing",
            "revision": "1",
            "dims": 64,
            "embedder": "hashing",
            "test": True,
        }
    )
    embedders = {profile.id: HashingEmbedder(profile)}
    publish_act(Store(signed.engine(PUBLISH)), UNITS)
    worker_engine = signed.engine(RETRIEVAL)
    with pytest.raises(DBAPIError, match="permission denied"):
        indexer.prepare(worker_engine, profile, backend="exact")
    generation = indexer.prepare(signed.owner, profile, backend="exact")
    assert set(logins.grants(signed.owner)) == set(logins.LOGINS)
    assert indexer.work(worker_engine, batches=5, embedders=embedders)["failed"] == 0
    assert indexer.validate(worker_engine, generation, embedder=embedders[profile.id])["passed"]
    indexer.activate(worker_engine, generation, allow_test=True)
    assert [row["generation_id"] for row in indexer.status(worker_engine)] == [generation]
    with signed.engine(SERVICE).begin() as conn:
        retriever = Retriever(
            LegacyCatalogue(LegacyRepository(conn)), vector_profile=profile.id, embedder=embedders[profile.id]
        )
        assert retriever.retrieve(Request(query="enacted", filters=Filters())).model_dump(mode="json")
    assert indexer.purge(worker_engine) == 0


def test_the_publish_job_writes_the_catalogue_and_the_gate_still_decides_what_is_read(signed, monkeypatch):
    """A text published on the job's login reads through the service's; withdrawn, it does not, for the same
    reason as before: the rights gate is in what the service runs, and it is unchanged."""
    for name in ("ALVARY_ACCOUNTS", "ALVARY_ADMISSION_CONFIG", "ALVARY_SITE_ADMISSION_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PUBLIC_DOMAIN", "open.example.test")
    publisher = Store(signed.engine(PUBLISH))
    publish_act(publisher, UNITS)
    client = TestClient(create_service(store=Store(signed.engine(SERVICE))))
    assert client.get(f"/site/v1/sources/{WORK}").status_code == 200
    versions = client.get(f"/site/v1/sources/{WORK}/versions").json()
    assert versions["items"] and "ENACTED" in json.dumps(versions)
    withdraw(publisher)
    assert "ENACTED" not in client.get(f"/site/v1/sources/{WORK}/versions").text
    with signed.owner.connect() as conn:
        log = signed.t(conn, "changes", "change_event")
        kinds = set(conn.scalars(text(f"SELECT event_type FROM {log}")))
    assert "withdrawal" in kinds


# --- what none of them can do ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", EVERY_LOGIN)
def test_each_login_passes_its_own_check(signed, role):
    assert logins.check_login(signed.engine(role), logins.LOGINS[role]) == []


def refused(conn, statements, match="permission denied"):
    for statement in statements:
        with pytest.raises(DBAPIError, match=match), conn.begin():
            conn.exec_driver_sql(statement)


@pytest.mark.parametrize("role", EVERY_LOGIN)
def test_no_login_can_change_the_schema_or_grant_itself_more(signed, role):
    with signed.engine(role).connect() as conn:
        base = conn.dialect.identifier_preparer.quote_identifier(signed.schema)
        accounts = conn.dialect.identifier_preparer.quote_identifier(signed.namespace.schema("accounts"))
        refused(
            conn,
            (
                f"CREATE TABLE {base}.intruder (id int)",
                f"CREATE TABLE {accounts}.intruder (id int)",
                f"CREATE FUNCTION {base}.intruder() RETURNS int LANGUAGE sql AS 'SELECT 1'",
                "CREATE TEMPORARY TABLE intruder (id int)",
                "CREATE DOMAIN pg_temp.intruder AS int",
                "CREATE FUNCTION pg_temp.intruder() RETURNS int LANGUAGE sql AS 'SELECT 1'",
                "CREATE SCHEMA intruder",
                "CREATE ROLE intruder",
                "GRANT pg_read_all_data TO CURRENT_USER",
                "GRANT open_alvary TO CURRENT_USER",
                "SET ROLE open_alvary",
                "SET SESSION AUTHORIZATION open_alvary",
                "CREATE EXTENSION btree_gist",
            ),
            match="permission denied|must be|not have permission",
        )
        refused(
            conn,
            (
                f"ALTER TABLE {base}.sources ADD COLUMN intruder text",
                f"DROP TABLE {base}.sources",
                f"ALTER ROLE {role} SUPERUSER",
                f"ALTER ROLE {role} CREATEROLE",
            ),
            match="must be owner|permission denied|must have|not have permission",
        )
        refused(conn, (f"TRUNCATE {base}.versions",))


@pytest.mark.parametrize("role", EVERY_LOGIN)
def test_no_login_reads_a_key_hash_or_the_private_schemas(signed, role):
    with signed.engine(role).connect() as conn:
        refused(
            conn,
            (
                f"SELECT secret_hash FROM {signed.t(conn, 'accounts', 'api_key')}",
                f"SELECT * FROM {signed.t(conn, 'accounts', 'api_key')}",
                f"SELECT * FROM {signed.t(conn, 'policy', 'collection_decision')}",
                f"SELECT * FROM {signed.t(conn, 'policy', 'evidence')}",
                f"SELECT * FROM {signed.t(conn, 'policy', 'actor')}",
                f"SELECT * FROM {signed.t(conn, 'staging', 'staged_artifact')}",
                f"SELECT * FROM {signed.t(conn, 'corpus', 'approved_version')}",
            ),
        )


def test_only_the_service_reads_a_session_or_a_sign_in_flow_and_only_it_and_the_jobs_a_sealed_secret(signed):
    guarded = {
        ("accounts", "console_session", "token_hash"): {SERVICE},
        ("accounts", "console_session", "csrf"): {SERVICE},
        ("accounts", "sign_in_flow", "verifier"): {SERVICE},
        ("accounts", "sign_in_flow", "nonce"): {SERVICE},
        ("accounts", "unsubscribe_token", "token_hash"): {SERVICE},
        ("accounts", "webhook_key", "secret_sealed"): {SERVICE, JOBS},
        ("accounts", "identity", "subject"): {SERVICE, JOBS},
        ("accounts", "person", "email"): {SERVICE, JOBS},
    }
    for (kind, table, column), readers in guarded.items():
        for role in EVERY_LOGIN:
            with signed.engine(role).connect() as conn:
                statement = f"SELECT {column} FROM {signed.t(conn, kind, table)} LIMIT 0"
                if role in readers:
                    conn.exec_driver_sql(statement)
                else:
                    refused(conn, (statement,))


def test_the_service_cannot_write_what_it_only_reads_or_rewrite_a_credential(signed):
    with signed.engine(SERVICE).connect() as conn:
        base = conn.dialect.identifier_preparer.quote_identifier(signed.schema)
        refused(
            conn,
            (
                f"UPDATE {base}.sources SET payload = payload",
                f"DELETE FROM {base}.rights",
                f"INSERT INTO {signed.t(conn, 'changes', 'feed_state')} DEFAULT VALUES",
                f"DELETE FROM {signed.t(conn, 'changes', 'change_event')}",
                f"UPDATE {signed.t(conn, 'accounts', 'tier')} SET published = true",
                f"UPDATE {signed.t(conn, 'accounts', 'api_key')} SET secret_hash = '\\x00'",
                f"UPDATE {signed.t(conn, 'accounts', 'console_session')} SET token_hash = '\\x00'",
                f"DELETE FROM {signed.t(conn, 'accounts', 'audit_event')}",
                f"UPDATE {signed.t(conn, 'accounts', 'audit_event')} SET action = 'x'",
                f"SELECT * FROM {signed.t(conn, 'accounts', 'audit_seal')}",
                f"SELECT * FROM {signed.t(conn, 'accounts', 'traffic_day')}",
                f"SELECT {signed.t(conn, 'policy', 'allocate_publication_revision')}()",
                f"SELECT {signed.t(conn, 'accounts', 'traffic_months')}(current_date, 0, 90)",
            ),
        )


def test_the_jobs_login_cannot_write_the_catalogue_or_rewrite_who_may_do_what(signed):
    with signed.engine(JOBS).connect() as conn:
        base = conn.dialect.identifier_preparer.quote_identifier(signed.schema)
        refused(
            conn,
            (
                f"UPDATE {base}.sources SET payload = payload",
                f"UPDATE {base}.rights SET payload = payload",
                f"UPDATE {signed.t(conn, 'accounts', 'person')} SET state = 'active'",
                f"UPDATE {signed.t(conn, 'accounts', 'membership')} SET role = 'owner'",
                f"INSERT INTO {signed.t(conn, 'accounts', 'api_key')} DEFAULT VALUES",
                f"INSERT INTO {signed.t(conn, 'accounts', 'console_session')} DEFAULT VALUES",
                f"INSERT INTO {signed.t(conn, 'accounts', 'entitlement')} DEFAULT VALUES",
                f"SELECT {signed.t(conn, 'policy', 'check_credential')}('{{}}')",
            ),
        )


def test_a_function_running_as_the_tables_owner_looks_nowhere_else_for_a_name(signed):
    with signed.owner.connect() as conn:
        months = signed.t(conn, "accounts", "traffic_months")
        config, body = conn.execute(
            text("SELECT proconfig, prosrc FROM pg_proc WHERE oid = to_regprocedure(:f)"),
            {"f": f"{months}(date, integer, integer)"},
        ).one()
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert "pg_catalog.pg_inherits" in body and "pg_catalog.pg_class" in body


def test_every_function_running_as_its_owner_fixes_its_search_path_unless_no_login_can_reach_it(signed):
    """Listed from the catalog, so a new one is covered the day it is made: it must fix its path whoever may run it.
    Those pinned before the rule stay as they are only while no login can call them or fire them."""
    with signed.owner.connect() as conn:
        functions = conn.execute(
            text(
                "SELECT p.oid, p.oid::regprocedure::text AS name, p.proconfig AS settings FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE p.prosecdef AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
            )
        ).all()
        assert len(functions) >= 123
        earlier = set()
        for manifest in Path("rights/sql").glob("v*/manifest.json"):
            if manifest.parent.name < "v0050":
                pinned = json.loads(manifest.read_text())
                for entry in pinned["functions"] + pinned.get("new_functions", []):
                    earlier.add(
                        conn.scalar(
                            text("SELECT to_regprocedure(:f)::oid"),
                            {"f": signed.namespace.schema("policy") + "." + entry["signature"]},
                        )
                    )
        unfixed = [f for f in functions if logins.SAFE_SEARCH_PATH not in (f.settings or ())]
        assert not [f.name for f in unfixed if f.oid not in earlier]
        for function in unfixed:
            for role in EVERY_LOGIN:
                assert not conn.scalar(
                    text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"), {"r": role, "f": function.oid}
                ), f"{role} can run {function.name}"
        fired = conn.execute(
            text(
                "SELECT DISTINCT p.oid::regprocedure::text AS name, r.rolname FROM pg_trigger t"
                " JOIN pg_proc p ON p.oid = t.tgfoid JOIN pg_roles r ON r.rolname = ANY(:logins)"
                " WHERE NOT t.tgisinternal AND p.prosecdef AND NOT (:safe = ANY(coalesce(p.proconfig, '{}')))"
                " AND has_table_privilege(r.oid, t.tgrelid, 'INSERT, UPDATE, DELETE, TRUNCATE')"
            ),
            {"logins": EVERY_LOGIN, "safe": logins.SAFE_SEARCH_PATH},
        ).all()
        assert fired == []


def test_a_login_that_could_run_a_function_with_no_fixed_path_is_refused(signed):
    with signed.owner.begin() as conn:
        admit = signed.t(conn, "policy", "admit")
        conn.exec_driver_sql(f"ALTER FUNCTION {admit}(jsonb) SET search_path TO pg_catalog")
    try:
        with pytest.raises(SystemExit, match="does not fix its search path"):
            logins.grants(signed.owner)
    finally:
        with signed.owner.begin() as conn:
            conn.exec_driver_sql(f"ALTER FUNCTION {admit}(jsonb) SET search_path TO pg_catalog, pg_temp")


def test_the_search_paths_are_set_with_no_lock_that_would_hold_up_what_is_running(pg_engine):
    """On the old image's tables and functions, up and down, with nothing stronger than a writer's lock on any table."""
    migrate(pg_engine)
    with pg_engine.begin() as conn:
        schema = conn.scalar(text("SELECT current_schema()"))
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.downgrade(config, "0049")
        fence = conn.scalar(
            text("SELECT proconfig FROM pg_proc WHERE oid = to_regprocedure(:f)"),
            {"f": f'"{schema}".identity_legacy_write_fence()'},
        )
        assert fence == ["search_path=pg_catalog"]
    with pg_engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.upgrade(config, "0050")
        held = conn.execute(
            text(
                "SELECT DISTINCT c.relname, l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE l.pid = pg_backend_pid() AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
            )
        ).all()
        assert {mode for _, mode in held} <= {"AccessShareLock", "RowShareLock", "RowExclusiveLock"}, held


def test_a_grant_of_temporary_objects_is_found_and_taken_back(signed):
    with signed.owner.begin() as conn:
        database = conn.scalar(text("SELECT current_database()"))
        conn.exec_driver_sql(f'GRANT TEMPORARY ON DATABASE "{database}" TO PUBLIC')
        conn.exec_driver_sql(f'GRANT TEMPORARY ON DATABASE "{database}" TO {JOBS}')
    with signed.owner.connect() as conn:
        assert "open_alvary_jobs can make temporary objects" in logins.problems(
            conn, signed.namespace, logins.JOBS
        )
    assert set(logins.grants(signed.owner)) == set(logins.LOGINS)
    assert logins.check_login(signed.engine(JOBS), logins.JOBS) == []


def test_seals_and_audit_entries_stay_append_only_for_the_job_that_keeps_them(signed):
    with signed.owner.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {signed.t(conn, 'accounts', 'audit_event')} (id, actor_type, actor_ref, action)"
                " VALUES (:id, 'system', 'test', 'test.new')"
            ),
            {"id": new_id("evt")},
        )
    with signed.engine(JOBS).connect() as conn:
        refused(
            conn,
            (
                f"UPDATE {signed.t(conn, 'accounts', 'audit_seal')} SET entries = 0",
                f"DELETE FROM {signed.t(conn, 'accounts', 'audit_seal')}",
                f"UPDATE {signed.t(conn, 'accounts', 'audit_event')} SET action = 'x'",
            ),
        )
        # DELETE it holds, for the purge: the trigger keeps every entry inside its two years.
        refused(conn, (f"DELETE FROM {signed.t(conn, 'accounts', 'audit_event')}",), match="append_only")


def test_the_retrieval_and_publish_logins_cannot_reach_accounts_at_all(signed):
    for role in (RETRIEVAL, PUBLISH):
        with signed.engine(role).connect() as conn:
            refused(
                conn,
                [
                    f"SELECT 1 FROM {signed.t(conn, 'accounts', table)} LIMIT 0"
                    for table in (
                        "person",
                        "api_key",
                        "console_session",
                        "webhook_key",
                        "identity",
                        "audit_event",
                    )
                ],
            )


# --- that the plan stays true --------------------------------------------------------------------------


def test_every_table_has_been_decided(signed):
    """A migration's new table fails here until it is named: who may reach it is decided, not defaulted."""
    with signed.owner.connect() as conn:
        for kind in ("base", "accounts", "changes", "corpus"):
            schema = signed.schema if kind == "base" else signed.namespace.schema(kind)
            present = set(
                conn.scalars(
                    text(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " WHERE n.nspname = :s AND c.relkind IN ('r', 'p', 'v', 'm')"
                    ),
                    {"s": schema},
                )
            )
            planned = {table for login in logins.LOGINS.values() for table in login.tables.get(kind, {})}
            unreached = logins.NO_LOGIN.get(kind, set())
            names = {name for name in unreached if not name.endswith("*")}
            prefixes = tuple(name[:-1] for name in unreached if name.endswith("*")) or ("\0",)
            undecided = {table for table in present - planned - names if not table.startswith(prefixes)}
            assert not undecided, f"{kind}: name these in rights/logins.py: {sorted(undecided)}"
            gone = (planned | names) - present
            assert not gone, f"{kind}: no such table: {sorted(gone)}"


def test_running_it_again_repairs_what_drifted(signed):
    with signed.owner.begin() as conn:
        quote = conn.dialect.identifier_preparer.quote_identifier
        base, staging = quote(signed.schema), quote(signed.namespace.schema("staging"))
        api_key = signed.t(conn, "accounts", "api_key")
        for drift in (
            f"GRANT UPDATE ON {base}.sources TO {SERVICE}",
            f"GRANT SELECT (secret_hash) ON {api_key} TO {SERVICE}",
            f"GRANT USAGE ON SCHEMA {staging} TO {SERVICE}",
            f"GRANT SELECT ON {signed.t(conn, 'policy', 'collection_decision')} TO {SERVICE}",
        ):
            conn.exec_driver_sql(drift)
    # The repair job would stop on it if asked to leave it; it goes, and the login still signs in.
    assert set(logins.provision(signed.owner, signed.urls())) == set(logins.LOGINS)
    assert logins.check_login(signed.engine(SERVICE), logins.LOGINS[SERVICE]) == []
    with signed.engine(SERVICE).connect() as conn:
        refused(conn, (f"UPDATE {base}.sources SET payload = payload", f"SELECT secret_hash FROM {api_key}"))


def test_the_grants_step_brings_a_generations_table_to_the_logins_and_leaves_passwords_alone(signed):
    with signed.owner.begin() as conn:
        table = signed.t(conn, "changes", "vectors_0123")
        conn.execute(text(f"CREATE TABLE {table} (version_id text)"))
    assert set(logins.grants(signed.owner)) == set(logins.LOGINS)
    with signed.engine(SERVICE).connect() as conn:
        assert conn.exec_driver_sql(f"SELECT version_id FROM {table}").all() == []
        conn.rollback()
        refused(conn, (f"DELETE FROM {table}",))
    with signed.engine(RETRIEVAL).begin() as conn:
        conn.exec_driver_sql(f"INSERT INTO {table} VALUES ('v')")
    with signed.engine(JOBS).connect() as conn:
        refused(conn, (f"SELECT version_id FROM {table}",))


def test_the_grants_step_has_nothing_to_do_before_the_logins_exist(pg_engine):
    migrate(pg_engine)
    drop_logins(pg_engine)
    assert logins.grants(pg_engine) == {}


def test_a_password_must_be_generated_and_a_url_the_logins_own(signed):
    url = signed.owner.url
    with pytest.raises(SystemExit, match="generated"):
        logins.provision(
            signed.owner,
            {
                SERVICE: url.set(username=SERVICE, password="short'; DROP").render_as_string(
                    hide_password=False
                )
            },
        )
    with pytest.raises(SystemExit, match="someone else"):
        logins.provision(
            signed.owner,
            {
                SERVICE: url.set(username="open_alvary", password="a" * 40).render_as_string(
                    hide_password=False
                )
            },
        )


def test_a_login_that_has_come_to_hold_more_is_refused_and_nothing_changes(signed):
    with signed.owner.begin() as conn:
        conn.execute(text(f"ALTER ROLE {JOBS} CREATEDB"))
    try:
        with pytest.raises(SystemExit, match="role attribute"):
            logins.provision(signed.owner, signed.urls())
    finally:
        with signed.owner.begin() as conn:
            conn.execute(text(f"ALTER ROLE {JOBS} NOCREATEDB"))
    assert logins.check_login(signed.engine(JOBS), logins.LOGINS[JOBS]) == []


def test_the_commands_print_names_and_counts_and_never_a_password(signed, monkeypatch, capsys):
    for role, url in signed.urls().items():
        monkeypatch.setenv(logins.LOGINS[role].environment, url)
    monkeypatch.setattr("api.store.engine_for", lambda *a, **k: signed.owner)
    assert logins.main(["provision"]) == 0
    assert logins.main(["grants"]) == 0
    printed = capsys.readouterr().out
    assert SERVICE in printed
    for password in PASSWORDS.values():
        assert password not in printed


def test_they_can_be_taken_away_again_but_not_while_signed_in(signed):
    with signed.engine(JOBS).connect() as conn:
        conn.execute(text("SELECT 1"))
        with pytest.raises(SystemExit, match=JOBS):
            logins.remove(signed.owner)
    signed.close()
    assert logins.remove(signed.owner) == EVERY_LOGIN
    with signed.owner.connect() as conn:
        assert not conn.scalar(
            text("SELECT count(*) FROM pg_roles WHERE rolname = ANY(:r)"), {"r": EVERY_LOGIN}
        )
    assert logins.remove(signed.owner) == []


def test_the_check_with_nothing_to_check_is_no_pass(monkeypatch):
    for login in logins.LOGINS.values():
        monkeypatch.delenv(login.environment, raising=False)
    assert logins.main(["check"]) == 1


# --- as on the live database ----------------------------------------------------------------------------


def job_command(jobs_tf: str, name: str) -> str:
    """A job's command as Terraform gives it, as one line for a shell."""
    found = re.search(rf"^\s+{name} = \{{\n\s+command = (\[.*\])$", jobs_tf, re.M)
    assert found, name
    command = json.loads(found.group(1))
    return command[2] if command[:2] == ["sh", "-c"] else " ".join(command)


def run_job(command: str, url: str, settings: dict) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "ALVARY_RUNTIME_MODE": "persistent",
        "DATABASE_URL": url,
        **settings,
    }
    return subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("capability_roles", [False, True])
def test_on_a_database_owned_by_an_ordinary_login_as_on_cloud_sql(capability_roles, monkeypatch, capsys):
    """Everything belongs to a login that may create roles and is no superuser, as open_alvary is on Cloud
    SQL; the capability roles may or may not have been made there, and their functions then belong to the
    guard role, of which the owner is a member. Both hold, and the check signs in with the URLs alone."""
    url = validated_test_url()
    name = "oa_" + uuid4().hex + "_test"
    owner_role = "cloud_owner_" + uuid4().hex[:8]
    owner_password = "".join(secrets.choice(ALPHABET) for _ in range(32))
    administrator = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with administrator.connect() as conn:
        conn.execute(text(f"CREATE ROLE {owner_role} LOGIN CREATEROLE CREATEDB PASSWORD '{owner_password}'"))
        conn.execute(text(f'CREATE DATABASE "{name}" OWNER {owner_role}'))
    owner = create_engine(
        url.set(database=name, username=owner_role, password=owner_password), poolclass=NullPool
    )
    namespace = PolicyNamespace("public")
    world = Signed(owner, "public")
    roles = {}
    try:
        migrate(owner)
        with owner.begin() as conn:
            assert not conn.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
            if capability_roles:
                roles = provision_roles(conn, namespace)
        assert set(logins.provision(owner, world.urls())) == set(logins.LOGINS)
        for role, url_text in world.urls().items():
            monkeypatch.setenv(logins.LOGINS[role].environment, url_text)
        assert logins.main(["check"]) == 0, capsys.readouterr().out
        assert not any(password in capsys.readouterr().out for password in PASSWORDS.values())

        # Each scheduled job's own command, as jobs.tf writes it, in a process with only what it is given.
        jobs_tf = Path("deployment/gcp-terraform/env/jobs.tf").read_text()
        keys = json.dumps({"w1": base64.b64encode(b"k" * 32).decode()})
        for job, role, settings in (
            ("retrieval", RETRIEVAL, {}),
            ("worker", JOBS, {"ALVARY_WEBHOOK_KEYS": keys, "ALVARY_WEBHOOK_KEY_ID": "w1"}),
            ("daily", JOBS, {}),
        ):
            done = run_job(job_command(jobs_tf, job), world.urls()[role], settings)
            assert done.returncode == 0, f"{job}: {done.stdout}\n{done.stderr}"
            assert not any(password in done.stdout + done.stderr for password in PASSWORDS.values())

        # The functions, whoever owns them now: a key checked, a text published, a month added.
        Accounts(owner, namespace).set_tiers(json.loads(TIERS.read_text()))
        accounts = Accounts(world.engine(SERVICE), namespace, key_pepper=PEPPER)
        who, organisation = person(accounts)
        key = accounts.create_key(who, organisation, kind="personal", name="laptop")
        assert accounts.list_keys(who, organisation)[0]["id"] == key["id"]
        answer = accounts.check_credential({"kind": "key", "key_id": "0" * 32, "secret_hash": "0" * 64})
        assert answer == {"ok": False, "code": "invalid_credentials"}
        publish_act(Store(world.engine(PUBLISH)), UNITS)
        assert traffic.maintain(world.engine(JOBS), namespace)["months_dropped"] == 0
        # What the migrate job's last step does, and the owner keeps what it had.
        assert set(logins.grants(owner)) == set(logins.LOGINS)
        with owner.begin() as conn:
            conn.execute(text("CREATE TABLE public.still_mine (id int)"))
            conn.execute(text("DROP TABLE public.still_mine"))
        # The way all the way back, as this owner: it made them, and cannot act as them.
        world.close()
        assert logins.remove(owner) == EVERY_LOGIN
    finally:
        world.close()
        owner.dispose()
        with administrator.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            for role in (*logins.LOGINS, *roles.values(), owner_role):
                conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        administrator.dispose()
